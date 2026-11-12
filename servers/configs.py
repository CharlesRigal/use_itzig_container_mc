"""Édition des fichiers de configuration d'un serveur (mods, plugins, server.properties)."""

import json
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path

# Dossiers dont tous les fichiers texte sont éditables.
CONFIG_DIRS = ["config", "defaultconfigs", "world/serverconfig", "plugins"]
EXTENSIONS = {".toml", ".json", ".json5", ".properties", ".cfg", ".conf", ".yml", ".yaml", ".txt", ".ini", ".snbt"}
# Gérés par le manager ou par le jeu : ne pas les éditer à la main.
EXCLUDED = {"whitelist.json", "usercache.json", "eula.txt", "banned-ips.json", "banned-players.json"}
MAX_SIZE = 1_000_000
SNAPSHOT_TS = re.compile(r"^\d{8}-\d{6}$")

# Clés de server.properties réécrites à chaque démarrage depuis les paramètres de l'interface.
MANAGED_PROPERTIES = [
    "max-players", "difficulty", "gamemode", "motd", "view-distance", "pvp",
    "online-mode", "white-list", "enforce-whitelist",
]


class ConfigError(Exception):
    pass


def list_files(server):
    root = server.data_dir
    found = set()
    if not root.exists():
        return []
    for entry in root.iterdir():
        if (entry.is_file() and entry.suffix in EXTENSIONS and entry.name not in EXCLUDED
                and not entry.name.startswith(".")):
            found.add(entry.name)
    for d in CONFIG_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(root)
            hidden = any(part.startswith(".") for part in rel.parts)
            if p.is_file() and not p.is_symlink() and p.suffix in EXTENSIONS and not hidden:
                found.add(rel.as_posix())
    return sorted(found, key=lambda s: (s.count("/") > 0, s.lower()))


def _resolve(server, rel):
    # Seuls les fichiers listés sont accessibles : pas de chemin arbitraire.
    if rel not in list_files(server):
        raise ConfigError("Fichier non modifiable.")
    return server.data_dir / rel


# Forge réécrit les configs « par monde » (world/serverconfig) avec ses valeurs en mémoire
# quand le serveur s'arrête : modifiées pendant qu'il tourne, elles seraient perdues. Elles sont
# donc mises en attente et copiées au prochain démarrage, serveur arrêté (podman.start).
PENDING_DIR = ".mcmanager/pending"


def needs_cold_write(rel):
    return "/serverconfig/" in f"/{rel}"


def _pending_path(server, rel):
    return server.data_dir / PENDING_DIR / rel


def is_pending(server, rel):
    return _pending_path(server, rel).is_file()


def pending_files(server):
    root = server.data_dir / PENDING_DIR
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def apply_pending(server):
    """Copie les modifications en attente (à n'appeler que serveur arrêté). Renvoie leur liste."""
    applied = []
    for rel in pending_files(server):
        src = _pending_path(server, rel)
        dst = server.data_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        src.unlink()
        applied.append(rel)
    return applied


def edited_files(server):
    """Fichiers déjà modifiés depuis l'éditeur (ils ont un historique)."""
    root = _history_root(server)
    if not root.is_dir():
        return set()
    return {
        p.relative_to(d).as_posix()
        for d in root.iterdir() if SNAPSHOT_TS.match(d.name)
        for p in d.rglob("*") if p.is_file()
    }


def read(server, rel):
    path = _resolve(server, rel)
    if is_pending(server, rel):
        return _pending_path(server, rel).read_text(encoding="utf-8")
    if path.stat().st_size > MAX_SIZE:
        raise ConfigError("Fichier trop gros pour l'éditeur (> 1 Mo).")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("Ce fichier n'est pas du texte UTF-8.") from exc


def validate(rel, content):
    suffix = Path(rel).suffix
    try:
        if suffix == ".toml":
            tomllib.loads(content)
        elif suffix == ".json":
            json.loads(content)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Syntaxe {suffix[1:].upper()} invalide : {exc}") from exc


def _history_root(server):
    return server.backup_dir / "configs"


def _snapshot(server, rel):
    src = server.data_dir / rel
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = _history_root(server) / ts / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return ts


def save(server, rel, content, running=False):
    """Enregistre ; renvoie "pending" si la modification attend le prochain démarrage."""
    path = _resolve(server, rel)
    content = content.replace("\r\n", "\n")
    validate(rel, content)
    pending = _pending_path(server, rel)
    if running and needs_cold_write(rel):
        if not pending.is_file():
            _snapshot(server, rel)  # la version en service, avant la première modification
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(content, encoding="utf-8")
        return "pending"
    _snapshot(server, rel)
    path.write_text(content, encoding="utf-8")
    pending.unlink(missing_ok=True)
    return "saved"


def history(server, rel):
    root = _history_root(server)
    if not root.is_dir():
        return []
    return sorted(
        (d.name for d in root.iterdir() if SNAPSHOT_TS.match(d.name) and (d / rel).is_file()),
        reverse=True,
    )


def restore(server, rel, ts, running=False):
    if not SNAPSHOT_TS.match(ts) or ts not in history(server, rel):
        raise ConfigError("Version introuvable.")
    content = (_history_root(server) / ts / rel).read_text(encoding="utf-8")
    return save(server, rel, content, running=running)


def installed_mods(server):
    out = {}
    for d in ("mods", "plugins"):
        base = server.data_dir / d
        if base.is_dir():
            out[d] = sorted(p.name for p in base.iterdir() if p.suffix == ".jar")
    return out
