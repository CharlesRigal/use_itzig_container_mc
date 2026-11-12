"""Sauvegardes incrémentales par snapshots à liens durs (principe de rsync --link-dest).

Chaque sauvegarde est un dossier complet <backup_dir>/<AAAAMMJJ-HHMMSS>/. Un fichier identique
(taille + date de modification) à celui de la sauvegarde précédente n'est pas recopié : c'est un
lien dur vers lui. Seuls les fichiers modifiés prennent de la place. Supprimer une sauvegarde ne
casse jamais les autres, et restaurer revient à recopier un dossier.
"""

import fcntl
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import hub, podman

log = logging.getLogger("servers")

SNAPSHOT_NAME = re.compile(r"^\d{8}-\d{6}$")
META = ".snapshot.json"

# Retéléchargé ou régénéré par l'image : jamais sauvegardé.
SKIPPED_TOP = {"libraries", "versions", ".cache", "cache", "logs", ".rcon-cli.env", ".rcon-cli.yaml", ".mcmanager"}
# Réécrits à chaque sauvegarde automatique du jeu même sans joueur : ne comptent pas comme
# une modification (ils sont quand même copiés quand une sauvegarde a lieu).
IGNORED_FOR_CHANGES = {"session.lock", "level.dat", "level.dat_old", "usercache.json"}

REASONS = {"manual": "manuelle", "auto": "planifiée", "pre-restore": "avant restauration"}


class BackupSkipped(Exception):
    """Aucune modification depuis la dernière sauvegarde."""


# --- utilitaires -------------------------------------------------------------------

def _skipped_top(path: Path):
    return path.name in SKIPPED_TOP or (path.is_file() and path.suffix == ".jar")


def _walk(root: Path):
    """Fichiers à sauvegarder : {chemin relatif: os.stat_result}."""
    files = {}
    if not root.exists():
        return files
    for top in root.iterdir():
        if _skipped_top(top):
            continue
        if top.is_symlink():
            continue
        if top.is_file():
            files[top.name] = top.stat()
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            for name in filenames:
                p = Path(dirpath) / name
                if not p.is_symlink():
                    files[p.relative_to(root).as_posix()] = p.stat()
    return files


def _same(a: os.stat_result, b: os.stat_result):
    return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns


def _counts_as_change(rel):
    return Path(rel).name not in IGNORED_FOR_CHANGES


RUNNING = set()  # slugs dont une sauvegarde ou restauration est en cours (processus unique)


@contextmanager
def _lock(server):
    server.backup_dir.mkdir(parents=True, exist_ok=True)
    with open(server.backup_dir / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OSError("Une sauvegarde ou restauration de ce serveur est déjà en cours.") from exc
        RUNNING.add(server.slug)
        hub.notify()
        try:
            yield
        finally:
            RUNNING.discard(server.slug)
            hub.notify()  # « en cours » disparaît, la nouvelle sauvegarde apparaît


# --- lecture -----------------------------------------------------------------------

def _snapshot_dirs(server):
    if not server.backup_dir.is_dir():
        return []
    return sorted(
        (p for p in server.backup_dir.iterdir() if p.is_dir() and SNAPSHOT_NAME.match(p.name)),
        key=lambda p: p.name,
    )


def _meta(snapshot: Path):
    try:
        return json.loads((snapshot / META).read_text())
    except (OSError, ValueError):
        return {}


def list_backups(server):
    items = []
    for snap in reversed(_snapshot_dirs(server)):
        m = _meta(snap)
        items.append(
            {
                "name": snap.name,
                "date": datetime.strptime(snap.name, "%Y%m%d-%H%M%S"),
                "reason": REASONS.get(m.get("reason"), m.get("reason", "?")),
                "files": m.get("files"),
                "total_mb": round(m.get("total_bytes", 0) / 1_048_576, 1),
                "new_mb": round(m.get("new_bytes", 0) / 1_048_576, 1),
            }
        )
    return items


def latest(server):
    snaps = _snapshot_dirs(server)
    return snaps[-1] if snaps else None


def snapshot_path(server, name):
    if not SNAPSHOT_NAME.match(name or ""):
        raise FileNotFoundError(name)
    path = server.backup_dir / name
    if not path.is_dir():
        raise FileNotFoundError(name)
    return path


def has_changes(server, files=None):
    """Vrai si le monde a changé depuis la dernière sauvegarde."""
    prev = latest(server)
    if prev is None:
        return True
    current = files if files is not None else _walk(server.data_dir)
    previous = _walk(prev)
    previous.pop(META, None)
    for rel, st in current.items():
        if not _counts_as_change(rel):
            continue
        old = previous.get(rel)
        if old is None or not _same(st, old):
            return True
    return any(_counts_as_change(rel) and rel not in current for rel in previous)


# --- création ----------------------------------------------------------------------

def create_backup(server, reason="manual", skip_unchanged=True, retain=True):
    """Crée un snapshot. Lève BackupSkipped si rien n'a changé (et skip_unchanged)."""
    with _lock(server):
        running = podman.is_running(server)
        if running:
            # Écritures du monde suspendues pendant la copie : sauvegarde cohérente.
            try:
                podman.rcon(server, "save-off")
            except podman.PodmanError as exc:
                raise podman.PodmanError(
                    "Le serveur n'est pas encore prêt (console RCON indisponible) : "
                    "réessaie quand il a fini de démarrer."
                ) from exc
            podman.rcon(server, "save-all", "flush")
        try:
            name = _create_locked(server, reason, skip_unchanged)
        finally:
            if running:
                podman.rcon(server, "save-on")
        if retain:
            apply_retention(server)
        return name


def _create_locked(server, reason, skip_unchanged):
    files = _walk(server.data_dir)
    if skip_unchanged and not has_changes(server, files):
        raise BackupSkipped("Aucune modification depuis la dernière sauvegarde : sauvegarde annulée.")

    prev = latest(server)
    name = datetime.now().strftime("%Y%m%d-%H%M%S")
    while (server.backup_dir / name).exists():  # le nom est l'horodatage, à la seconde près
        time.sleep(0.2)
        name = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = server.backup_dir / f".{name}.part"
    shutil.rmtree(tmp, ignore_errors=True)
    new_bytes = total_bytes = linked = 0
    try:
        for rel, st in files.items():
            src = server.data_dir / rel
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            total_bytes += st.st_size
            old = prev / rel if prev else None
            if old is not None and old.is_file() and _same(st, old.stat()):
                os.link(old, dst)
                linked += 1
            else:
                try:
                    shutil.copy2(src, dst)
                except FileNotFoundError:
                    continue  # fichier supprimé pendant la copie
                new_bytes += st.st_size
        (tmp / META).write_text(json.dumps({
            "reason": reason, "files": len(files), "linked": linked,
            "new_bytes": new_bytes, "total_bytes": total_bytes,
        }))
        tmp.rename(server.backup_dir / name)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log.info("sauvegarde %s/%s (%s) : %d fichiers, %.1f Mo nouveaux",
             server.slug, name, reason, len(files), new_bytes / 1_048_576)
    return name


def apply_retention(server, protect=()):
    """Ne garde que les server.backup_keep sauvegardes les plus récentes (hors « protect »)."""
    snaps = [s for s in _snapshot_dirs(server) if s.name not in protect]
    keep = max(0, server.backup_keep - (len(_snapshot_dirs(server)) - len(snaps)))
    removed = []
    for snap in snaps[: max(0, len(snaps) - keep)]:
        shutil.rmtree(snap)
        removed.append(snap.name)
    if removed:
        log.info("rétention %s : supprimé %s", server.slug, ", ".join(removed))
    return removed


# --- restauration, suppression, téléchargement -------------------------------------

def restore_backup(server, name):
    """Remplace le monde par la sauvegarde. L'état actuel est sauvegardé avant s'il a changé."""
    source = snapshot_path(server, name)
    was_running = podman.is_running(server)
    safety = None
    try:
        # Sans rétention ici : elle pourrait supprimer la sauvegarde qu'on restaure.
        safety = create_backup(server, reason="pre-restore", retain=False)
    except BackupSkipped:
        pass
    podman.stop(server)
    with _lock(server):
        server.data_dir.mkdir(parents=True, exist_ok=True)
        for entry in server.data_dir.iterdir():
            if _skipped_top(entry):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        # Copie (et non lien) : le jeu modifiera ces fichiers, la sauvegarde doit rester intacte.
        for item in source.iterdir():
            if item.name == META:
                continue
            target = server.data_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, copy_function=shutil.copy2)
            else:
                shutil.copy2(item, target)
    log.info("restauration %s/%s (état précédent : %s)", server.slug, name, safety or "inchangé")
    # On ne supprime ni la sauvegarde restaurée, ni celle de sécurité.
    apply_retention(server, protect={name, safety})
    if was_running:
        podman.start(server)
    return safety


def delete_backup(server, name):
    with _lock(server):
        shutil.rmtree(snapshot_path(server, name))


def tar_command(server, name):
    """Commande produisant l'archive .tar.gz d'un snapshot sur sa sortie standard.

    `tar` en sous-processus plutôt que tarfile : mémoire constante quelle que soit la taille
    des fichiers, compression en C, et le débit suit celui du client (lecture asynchrone).
    """
    source = snapshot_path(server, name)
    return ["tar", "-czf", "-", f"--exclude=./{META}", "-C", str(source), "."]
