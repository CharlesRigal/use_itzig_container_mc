"""Pilotage des conteneurs itzg/minecraft-server via la CLI podman (rootless)."""

import json
import subprocess

from . import curseforge
from .models import CF_KEY_IN_CONTAINER, cf_key_file

LABEL = "mcmanager=1"


class PodmanError(Exception):
    pass


def _run(args, timeout=120, check=True):
    try:
        proc = subprocess.run(
            ["podman", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PodmanError("podman est introuvable sur cette machine.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PodmanError(f"podman {args[0]} : délai dépassé ({timeout} s).") from exc
    if check and proc.returncode != 0:
        raise PodmanError(proc.stdout.strip() or f"podman {args[0]} a échoué ({proc.returncode}).")
    return proc


def states():
    """{nom_du_conteneur: {"state": "running", "status": "Up 3 minutes (healthy)"}}"""
    proc = _run(["ps", "-a", "--filter", f"label={LABEL}", "--format", "json"])
    result = {}
    for c in json.loads(proc.stdout or "[]"):
        for name in c.get("Names") or []:
            result[name] = {"state": c.get("State", "unknown"), "status": c.get("Status", "")}
    return result


def state(server):
    return states().get(server.container_name, {"state": "absent", "status": ""})


def is_running(server):
    return state(server)["state"] == "running"


def exists(server):
    return _run(["container", "exists", server.container_name], check=False).returncode == 0


def create(server):
    server.data_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "create",
        "--name", server.container_name,
        "--label", LABEL,
        "--label", f"mcmanager.slug={server.slug}",
        # uid 1000 du conteneur = utilisateur courant : les fichiers du monde restent lisibles.
        "--userns=keep-id",
        "--stop-timeout", "60",
        "-p", f"{server.port}:25565",
        "-v", f"{server.data_dir}:/data",
    ]
    env = server.container_env()
    if "CF_API_KEY_FILE" in env:
        key = cf_key_file()
        if not key.is_file():
            raise PodmanError("Ce serveur utilise CurseForge : renseigne la clé API dans Paramètres.")
        args += ["-v", f"{key}:{CF_KEY_IN_CONTAINER}:ro"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args.append(server.image)
    # Un tag absent (java17…) est téléchargé à la première création.
    _run(args, timeout=900)


def stop(server):
    if is_running(server):
        _run(["stop", "-t", "60", server.container_name], timeout=120)


def remove(server):
    if exists(server):
        stop(server)
        _run(["rm", server.container_name])


def recreate(server):
    """Les paramètres sont des variables d'environnement figées à la création :
    on recrée le conteneur (le monde est dans un dossier monté, il est conservé)."""
    remove(server)
    create(server)


def start(server):
    if server.modpack_source == "curseforge":
        try:
            curseforge.prepare_modpack(server)
        except curseforge.CurseForgeError as exc:
            raise PodmanError(f"Modpack CurseForge : {exc}") from exc
    recreate(server)
    _run(["start", server.container_name])


def restart(server):
    start(server)


def logs(server, lines=150):
    if not exists(server):
        return ""
    return _run(["logs", "--tail", str(lines), server.container_name], check=False).stdout


def rcon(server, *command):
    """Envoie une commande à la console du serveur (rcon-cli est fourni par l'image)."""
    if not is_running(server):
        raise PodmanError("Le serveur n'est pas démarré.")
    return _run(["exec", server.container_name, "rcon-cli", *command], timeout=60).stdout.strip()
