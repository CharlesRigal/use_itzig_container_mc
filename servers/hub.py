"""Diffusion en direct vers les pages (Server-Sent Events).

Le serveur pousse les changements au lieu d'être interrogé par chaque onglet :

- l'état des conteneurs vient de `podman events` (démarrage, arrêt, plantage dans la seconde) ;
- « prêt », joueurs et « ne répond plus » n'ont pas d'événement : un seul ping central toutes les
  4 s, uniquement pour les serveurs démarrés, quel que soit le nombre d'onglets ;
- les actions de l'app (jobs, sauvegardes) appellent `notify()` ;
- les logs viennent d'un `podman logs -f` par serveur, lancé tant qu'une console est ouverte.

Tout vit dans la boucle asyncio du processus uvicorn (unique). `notify()` et `publish()` peuvent
être appelés depuis n'importe quel fil (vues synchrones, jobs, planificateur). Sans abonné, rien ne
tourne : les processus podman et le ping s'arrêtent après IDLE_STOP secondes.
"""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone

from asgiref.sync import sync_to_async

log = logging.getLogger("servers")

TICK = 4  # s : ping des serveurs démarrés
HEARTBEAT = 15  # s : commentaire SSE pour garder la connexion
IDLE_STOP = 30  # s sans abonné avant de tout arrêter
PS_REFRESH = 60  # s : relecture de sécurité de `podman ps`
LOG_LINES = 500  # lignes gardées en mémoire par console
# Seuls ces événements changent l'état ; `exec` (rcon du monitoring, toutes les 20 s) est ignoré.
CONTAINER_EVENTS = {"create", "init", "start", "restart", "died", "stop", "kill", "remove", "pause", "unpause"}
TRANSITIONAL = {"preparing", "installing", "starting", "stopping", "restoring", "unresponsive"}

_loop = None
_subs = set()
_wake = None  # asyncio.Event : recalcul immédiat demandé
_runner = None
_force_ps = True  # relire `podman ps` au prochain calcul
_followers = {}  # slug -> _LogFollower
_cache = {"by_name": None, "ps_at": 0.0, "sig": {}, "status": {}}
_sent = {}  # slug -> dernier état envoyé (JSON)


class _Subscriber:
    def __init__(self, slug, logs):
        self.slug, self.logs = slug, logs
        self.queue = asyncio.Queue(maxsize=1000)
        self.closed = False
        self.needs_full = True  # premier envoi : l'état complet, ensuite seulement les changements

    def send(self, event, data):
        if self.closed:
            return
        try:
            self.queue.put_nowait((event, data))
        except asyncio.QueueFull:
            # Client trop lent : on coupe, il se reconnecte et repart d'un état complet.
            self.closed = True


# --- API appelable depuis n'importe quel fil -----------------------------------------------

def notify():
    """L'état a peut-être changé : recalcul et diffusion immédiats (sans effet sans abonné)."""
    global _force_ps
    _force_ps = True
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(_wake_up)


def publish(event, data, slug=None):
    """Envoie un événement aux pages abonnées (toutes, ou celles du serveur `slug`)."""
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(_broadcast, event, data, slug)


# --- abonnements (boucle asyncio) ----------------------------------------------------------

def subscribe(slug=None, logs=False):
    global _loop, _wake, _runner
    _loop = asyncio.get_running_loop()
    if _wake is None:
        _wake = asyncio.Event()
    sub = _Subscriber(slug, logs)
    _subs.add(sub)
    if _runner is None or _runner.done():
        _runner = _loop.create_task(_run())
    if logs:
        follower = _followers.get(slug)
        if follower is None:
            follower = _followers[slug] = _LogFollower(slug)
        follower.cancel_stop()
        sub.send("logs_init", {"lines": list(follower.lines)})
    _wake.set()  # état complet tout de suite
    return sub


def unsubscribe(sub):
    sub.closed = True
    _subs.discard(sub)
    if sub.logs and not any(s.logs and s.slug == sub.slug for s in _subs):
        follower = _followers.get(sub.slug)
        if follower:
            follower.schedule_stop()


def _wake_up():
    if _wake is not None:
        _wake.set()


def _broadcast(event, data, slug):
    for sub in list(_subs):
        if slug is None or sub.slug in (None, slug):
            sub.send(event, data)


# --- état des serveurs ---------------------------------------------------------------------

async def _run():
    global _runner
    events = asyncio.create_task(_podman_events())
    idle_since = None
    try:
        while True:
            try:
                await asyncio.wait_for(_wake.wait(), TICK)
            except TimeoutError:
                pass
            _wake.clear()
            if not _subs:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > IDLE_STOP:
                    break
                continue
            idle_since = None
            try:
                result = await sync_to_async(_compute, thread_sensitive=False)()
            except Exception:
                log.exception("état en direct")
                continue
            _dispatch(result)
    finally:
        events.cancel()
        _cache["by_name"] = None  # au réveil, relecture complète
        _sent.clear()
        _runner = None


def _signature(server, ps, backup):
    ps = ps or {}
    return (ps.get("Id"), ps.get("State"), ps.get("ExitCode"), ps.get("ExitedAt"), ps.get("StartedAt"),
            server.job, server.job_detail, server.job_error, server.last_action, str(server.last_action_at),
            backup["backup"], backup["backup_busy"])


def _compute():
    """Calcul dans un fil : `podman ps` seulement si nécessaire, ping des serveurs démarrés."""
    global _force_ps
    from django.db import close_old_connections

    from . import podman, status
    from .models import Server

    close_old_connections()
    servers = list(Server.objects.all())
    now = time.monotonic()
    busy = any(st.get("key") in TRANSITIONAL or st.get("job") for st in _cache["status"].values())
    if _force_ps or busy or _cache["by_name"] is None or now - _cache["ps_at"] > PS_REFRESH:
        _force_ps = False
        try:
            _cache["by_name"], _cache["ps_at"] = status.ps_by_name(), now
        except podman.PodmanError as exc:
            log.warning("podman ps : %s", exc)
            if _cache["by_name"] is None:
                return {}
    by_name = _cache["by_name"]
    pongs = status.ping_running(servers, by_name)
    out = {}
    for s in servers:
        ps = by_name.get(s.container_name)
        backup = status.backup_info(s)
        sig = _signature(s, ps, backup)
        old = _cache["status"].get(s.slug)
        # Un serveur arrêté ou en erreur n'est recalculé que si quelque chose a changé
        # (l'extrait d'erreur lit 3000 lignes de logs).
        if old is None or s.job or (ps or {}).get("State") == "running" or sig != _cache["sig"].get(s.slug):
            out[s.slug] = status.as_json(status.describe(s, ps, pongs.get(s.slug)))
        else:
            out[s.slug] = old
        _cache["sig"][s.slug] = sig
    _cache["status"] = out
    return out


def _dispatch(result):
    changed = {}
    for slug, st in result.items():
        text = json.dumps(st, sort_keys=True)
        if _sent.get(slug) != text:
            _sent[slug] = text
            changed[slug] = st
    for sub in list(_subs):
        source = result if sub.needs_full else changed
        payload = source if sub.slug is None else {k: v for k, v in source.items() if k == sub.slug}
        sub.needs_full = False
        if payload:
            sub.send("status", payload)


async def _podman_events():
    """Événements des conteneurs gérés : chaque changement déclenche un recalcul immédiat."""
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "podman", "events", "--filter", "label=mcmanager=1", "--filter", "type=container",
                "--format", "json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            async for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("Status") in CONTAINER_EVENTS:
                    notify()
                    if ev.get("Status") == "start":
                        for follower in _followers.values():
                            if follower.container == ev.get("Name"):
                                follower.reattach()
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("podman events")
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
        await asyncio.sleep(5)  # podman events s'est arrêté : on le relance


# --- logs ------------------------------------------------------------------------------------

class _LogFollower:
    """Un `podman logs -f` partagé par toutes les consoles ouvertes d'un serveur."""

    STOP_DELAY = 10  # s : garde le flux pendant une navigation entre pages

    def __init__(self, slug):
        from .models import Server

        self.slug = slug
        self.container = Server(slug=slug).container_name
        self.lines = deque(maxlen=LOG_LINES)
        self.since = None
        self._reattach = asyncio.Event()
        self._stop_handle = None
        self.task = asyncio.get_running_loop().create_task(self._run())

    def reattach(self):
        self._reattach.set()

    def cancel_stop(self):
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None

    def schedule_stop(self):
        self.cancel_stop()
        self._stop_handle = asyncio.get_running_loop().call_later(self.STOP_DELAY, self._stop)

    def _stop(self):
        self.task.cancel()
        if _followers.get(self.slug) is self:
            del _followers[self.slug]

    async def _run(self):
        while True:
            # Premier passage : les dernières lignes ; ensuite (conteneur relancé ou recréé),
            # tout ce qui est arrivé depuis la fin du passage précédent.
            args = ["--since", self.since] if self.since else ["--tail", "300"]
            self._reattach.clear()  # avant le lancement : un `start` pendant la lecture n'est pas perdu
            proc = await asyncio.create_subprocess_exec(
                "podman", "logs", "-f", *args, self.container,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await self._pump(proc)
                await proc.wait()
            finally:
                if proc.returncode is None:
                    proc.kill()
            if proc.returncode == 0 or self.since:
                self.since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            # Conteneur arrêté ou absent : on attend qu'il redémarre (événement `start`).
            try:
                await asyncio.wait_for(self._reattach.wait(), 30)
            except TimeoutError:
                pass

    async def _pump(self, proc):
        """Lit les lignes et les envoie par paquets (un démarrage moddé en écrit des milliers)."""
        batch = []
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), 0.25 if batch else None)
            except TimeoutError:
                self._flush(batch)
                batch = []
                continue
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\r\n")
            self.lines.append(text)
            batch.append(text)
            if len(batch) >= 200:
                self._flush(batch)
                batch = []
        self._flush(batch)

    def _flush(self, batch):
        if batch:
            for sub in list(_subs):
                if sub.logs and sub.slug == self.slug:
                    sub.send("log", {"lines": batch})
