"""Actions longues (démarrer, arrêter, restaurer…) en arrière-plan, une seule à la fois par serveur.

L'action en cours est écrite en base : tous les workers gunicorn la voient, et un double clic
ne lance pas deux arrêts.
"""

import logging
import threading

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from . import backups, podman
from .models import Server
from .status import JOB_STALE

log = logging.getLogger("servers")

LABELS = {"start": "démarrage", "stop": "arrêt", "restart": "redémarrage", "restore": "restauration"}


class Busy(Exception):
    pass


def set_detail(server, text):
    Server.objects.filter(pk=server.pk).update(job_detail=text[:200])


def _start(server):
    if server.modpack_source == "curseforge":
        set_detail(server, "Téléchargement du modpack CurseForge…")
    else:
        set_detail(server, "Création du conteneur…")
    podman.start(server)


def _stop(server):
    podman.stop(server)


def _restart(server):
    set_detail(server, "Arrêt…")
    podman.stop(server)
    _start(server)


ACTIONS = {"start": _start, "stop": _stop, "restart": _restart}


def launch(server, action, func=None, detail=""):
    """Réserve le serveur pour `action` et l'exécute dans un fil. Lève Busy si occupé."""
    func = func or ACTIONS[action]
    now = timezone.now()
    fields = {"job": action, "job_detail": detail, "job_started_at": now, "job_error": ""}
    if action in ACTIONS:
        fields.update(last_action=action, last_action_at=now)
    taken = (
        Server.objects.filter(pk=server.pk)
        .filter(Q(job="") | Q(job_started_at__lt=now - JOB_STALE))
        .update(**fields)
    )
    if not taken:
        current = Server.objects.filter(pk=server.pk).values_list("job", flat=True).first()
        raise Busy(f"Une action est déjà en cours ({LABELS.get(current, current)}).")
    threading.Thread(target=_run, args=(server.pk, action, func), daemon=True, name=f"job-{server.slug}").start()


def _run(pk, action, func):
    close_old_connections()
    try:
        server = Server.objects.get(pk=pk)
        func(server)
        Server.objects.filter(pk=pk).update(job="", job_detail="")
        log.info("%s %s terminé", action, server.slug)
        if server.upnp_enabled:
            from . import upnp

            upnp.sync_quietly()  # port ouvert ou fermé tout de suite, sans attendre le planificateur
    except Exception as exc:  # noqa: BLE001 - tout échec doit libérer le serveur
        log.exception("%s du serveur %s en échec", action, pk)
        Server.objects.filter(pk=pk).update(job="", job_detail="", job_error=f"Échec du {LABELS.get(action, action)} : {exc}"[:2000])
    finally:
        close_old_connections()


def restore(server, name):
    backups.snapshot_path(server, name)  # 404 tout de suite si elle n'existe pas
    launch(server, "restore", lambda s: backups.restore_backup(s, name), detail=f"Restauration de {name}…")


def is_busy(server):
    return bool(server.job) and server.job_started_at and timezone.now() - server.job_started_at < JOB_STALE
