"""Sauvegardes .tar.gz du dossier /data d'un serveur."""

import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from . import podman

BACKUP_NAME = re.compile(r"^\d{8}-\d{6}\.tar\.gz$")

# Fichiers que l'image retélécharge ou régénère : inutile de les sauvegarder.
SKIPPED = {"libraries", "versions", ".cache", "cache", "logs", ".rcon-cli.env", ".rcon-cli.yaml", ".mcmanager"}


def _skipped(path: Path):
    return path.name in SKIPPED or (path.is_file() and path.suffix == ".jar")


def list_backups(server):
    if not server.backup_dir.exists():
        return []
    items = []
    for p in sorted(server.backup_dir.iterdir(), reverse=True):
        if BACKUP_NAME.match(p.name):
            st = p.stat()
            items.append(
                {
                    "name": p.name,
                    "size_mb": round(st.st_size / 1_048_576, 1),
                    "date": datetime.strptime(p.name[:15], "%Y%m%d-%H%M%S"),
                }
            )
    return items


def backup_path(server, name):
    if not BACKUP_NAME.match(name):
        raise FileNotFoundError(name)
    path = server.backup_dir / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def create_backup(server):
    server.backup_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    target = server.backup_dir / name
    running = podman.is_running(server)
    if running:
        # Écritures du monde suspendues pendant la copie pour une sauvegarde cohérente.
        podman.rcon(server, "save-off")
        podman.rcon(server, "save-all", "flush")
    try:
        tmp = target.with_suffix(".part")
        with tarfile.open(tmp, "w:gz") as tar:
            if server.data_dir.exists():
                for entry in sorted(server.data_dir.iterdir()):
                    if not _skipped(entry):
                        tar.add(entry, arcname=entry.name)
        tmp.rename(target)
    finally:
        if running:
            podman.rcon(server, "save-on")
    return name


def restore_backup(server, name):
    """Remplace le monde par la sauvegarde. Une sauvegarde de l'état actuel est faite avant."""
    source = backup_path(server, name)
    was_running = podman.is_running(server)
    safety = create_backup(server)
    podman.stop(server)

    server.backup_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=server.backup_dir) as tmp:
        with tarfile.open(source, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        server.data_dir.mkdir(parents=True, exist_ok=True)
        for entry in server.data_dir.iterdir():
            if _skipped(entry):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for entry in Path(tmp).iterdir():
            shutil.move(str(entry), server.data_dir / entry.name)

    if was_running:
        podman.start(server)
    return safety


def delete_backup(server, name):
    backup_path(server, name).unlink()
