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


def read(server, rel):
    path = _resolve(server, rel)
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


def save(server, rel, content):
    path = _resolve(server, rel)
    content = content.replace("\r\n", "\n")
    validate(rel, content)
    _snapshot(server, rel)
    path.write_text(content, encoding="utf-8")


def history(server, rel):
    root = _history_root(server)
    if not root.is_dir():
        return []
    return sorted(
        (d.name for d in root.iterdir() if SNAPSHOT_TS.match(d.name) and (d / rel).is_file()),
        reverse=True,
    )


def restore(server, rel, ts):
    if not SNAPSHOT_TS.match(ts) or ts not in history(server, rel):
        raise ConfigError("Version introuvable.")
    content = (_history_root(server) / ts / rel).read_text(encoding="utf-8")
    save(server, rel, content)


def installed_mods(server):
    out = {}
    for d in ("mods", "plugins"):
        base = server.data_dir / d
        if base.is_dir():
            out[d] = sorted(p.name for p in base.iterdir() if p.suffix == ".jar")
    return out
