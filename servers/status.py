"""État réel des serveurs : podman + ping Minecraft + dernière action demandée.

Le ping « Server List Ping » est la requête de la liste multijoueur du jeu : il ne répond
qu'une fois le serveur prêt, et donne joueurs et version.
"""

import json
import re
import socket
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from django.utils import timezone as djtz

from . import podman

LONG_START = timedelta(minutes=20)
JOB_STALE = timedelta(minutes=45)  # au-delà, une action « en cours » est considérée perdue

# clé -> (libellé, couleur CSS)
STATES = {
    "stopped": ("Arrêté", "muted"),
    "preparing": ("Préparation", "work"),
    "installing": ("Installation", "work"),
    "starting": ("Démarrage", "work"),
    "ready": ("Prêt", "ok"),
    "unresponsive": ("Ne répond plus", "warn"),
    "stopping": ("Arrêt en cours", "work"),
    "restoring": ("Restauration", "work"),
    "error": ("Erreur", "err"),
    "unknown": ("Inconnu", "warn"),
}

ERROR_LINE = re.compile(r"\[init\] \[ERROR\]|ERROR|Exception|Caused by|FATAL|Crash", re.I)


# --- ping ---------------------------------------------------------------------------

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n = shift = 0
    for _ in range(5):
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connexion fermée")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7
    raise ValueError("varint invalide")


def ping(port, host="127.0.0.1", timeout=1.5):
    """{"online": int, "max": int, "version": str} ou None si le serveur ne répond pas."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            hs = _varint(0) + _varint(763) + _varint(len(host)) + host.encode() + struct.pack(">H", port) + _varint(1)
            s.sendall(_varint(len(hs)) + hs + _varint(1) + _varint(0))
            _read_varint(s)  # longueur du paquet
            _read_varint(s)  # id du paquet
            length = _read_varint(s)
            data = b""
            while len(data) < length:
                chunk = s.recv(min(65536, length - len(data)))
                if not chunk:
                    break
                data += chunk
        st = json.loads(data)
        return {
            "online": int(st["players"]["online"]),
            "max": int(st["players"]["max"]),
            "version": str(st.get("version", {}).get("name", "")),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- calcul de l'état ----------------------------------------------------------------

def _ts(unix):
    return datetime.fromtimestamp(unix, tz=timezone.utc) if unix and unix > 0 else None


def _error_excerpt(server, lines=3000):
    text = re.sub(r"\x1b\[[0-9;]*m", "", podman.logs(server, lines))
    out = []
    # Crash Minecraft : description, l'exception qui la suit, chemin du rapport complet.
    desc = list(re.finditer(r"^Description: .*", text, re.M))
    if desc:
        d = desc[-1]
        out.append(d.group(0).strip()[:300])
        exc = re.search(r"^(\S+(?:Exception|Error)\b.*)", text[d.end():], re.M)
        if exc:
            out.append(exc.group(1).strip()[:300])
    saved = re.findall(r"crash report has been saved to: (\S+)", text)
    if saved:
        out.append(f"Rapport complet : {saved[-1].replace('/data/./', '').replace('/data/', '')}")
    if not out:
        out = [l.strip()[:300] for l in text.splitlines() if ERROR_LINE.search(l)][-6:]
    return "\n".join(out)[-1500:]


def _last_log_line(server):
    lines = [l for l in podman.logs(server, 5).splitlines() if l.strip()]
    return re.sub(r"\x1b\[[0-9;]*m", "", lines[-1])[:160] if lines else ""


def _compute(server, ps, pong):
    now = djtz.now()
    state = (ps or {}).get("State", "absent")
    info = {"players": None, "version": None, "detail": "", "warning": "", "error": ""}

    if server.job and server.job_started_at and now - server.job_started_at < JOB_STALE:
        if server.job == "stop":
            return "stopping", info
        if server.job == "restore":
            info["detail"] = server.job_detail
            return "restoring", info
        if server.job in ("start", "restart") and state != "running":
            info["detail"] = server.job_detail or "Préparation du conteneur…"
            return "preparing", info

    if server.job_error and state != "running":
        info["error"] = server.job_error
        return "error", info

    if state in ("absent", "created", "configured", "initialized"):
        return "stopped", info

    if state in ("exited", "stopped"):
        code = ps.get("ExitCode", 0)
        exited_at = _ts(ps.get("ExitedAt"))
        asked = server.last_action == "stop" and server.last_action_at and (
            exited_at is None or server.last_action_at <= exited_at + timedelta(seconds=5)
        )
        if asked or code == 0:
            return "stopped", info
        info["error"] = f"Arrêt inattendu (code {code})."
        excerpt = _error_excerpt(server)
        if excerpt:
            info["error"] += "\n" + excerpt
        return "error", info

    if state in ("running", "stopping"):
        started = _ts(ps.get("StartedAt"))
        if pong:
            info.update(players=pong["online"], max=pong["max"], version=pong["version"])
            return "ready", info
        if server.last_ready_at and started and server.last_ready_at > started:
            info["warning"] = "Était prêt, ne répond plus au ping."
            return "unresponsive", info
        # L'installeur de l'image est lui aussi en Java : c'est mc-server-runner (qui lance
        # Minecraft une fois l'installation finie) qui distingue les deux phases.
        runner = any(c.startswith("mc-server-runne") for c in podman.top_commands(server))
        key = "starting" if runner else "installing"
        info["detail"] = _last_log_line(server)
        if started and now - started > LONG_START:
            info["warning"] = f"Démarrage anormalement long ({int((now - started).total_seconds() // 60)} min)."
        info["since"] = started
        return key, info

    if state == "paused":
        info["warning"] = "Conteneur en pause."
        return "unresponsive", info

    info["detail"] = f"état podman : {state}"
    return "unknown", info


def _record(server, key, info):
    """Mémorise « prêt » et « joueurs vus » (pour « ne répond plus » et les sauvegardes)."""
    now = djtz.now()
    fields = {}
    if key == "ready":
        if not server.last_ready_at or now - server.last_ready_at > timedelta(seconds=30):
            fields["last_ready_at"] = now
        if info.get("players") and (not server.players_seen_at or now - server.players_seen_at > timedelta(seconds=30)):
            fields["players_seen_at"] = now
    if fields:
        type(server).objects.filter(pk=server.pk).update(**fields)
        for k, v in fields.items():
            setattr(server, k, v)


def collect(servers):
    """État de plusieurs serveurs : un seul `podman ps`, pings en parallèle."""
    servers = list(servers)
    try:
        try:
            ps_all = podman.ps_json()
        except podman.PodmanError:
            ps_all = podman.ps_json()  # un conteneur recréé à l'instant peut faire échouer ps
    except podman.PodmanError as exc:
        label, color = STATES["unknown"]
        return {s.slug: {"key": "unknown", "label": label, "color": color, "job": s.job,
                         "error": str(exc), "detail": "", "warning": ""} for s in servers}
    by_name = {n: c for c in ps_all for n in (c.get("Names") or [])}
    running = [s for s in servers if by_name.get(s.container_name, {}).get("State") == "running"]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(running)))) as pool:
        pongs = dict(zip([s.slug for s in running], pool.map(lambda s: ping(s.port), running)))
    out = {}
    for s in servers:
        try:
            key, info = _compute(s, by_name.get(s.container_name), pongs.get(s.slug))
        except podman.PodmanError as exc:
            key, info = "unknown", {"error": str(exc)}
        _record(s, key, info)
        label, color = STATES[key]
        out[s.slug] = {"key": key, "label": label, "color": color, "job": s.job, **info}
    return out


def for_server(server):
    return collect([server])[server.slug]


def as_json(st):
    """Version sérialisable (dates en ISO)."""
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in st.items()}
