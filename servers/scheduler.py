"""Sauvegardes planifiées : un fil dans le service web, un seul pour tous les workers gunicorn."""

import fcntl
import logging
import threading
import time
from datetime import datetime, timedelta

from django.conf import settings

log = logging.getLogger("servers")
TICK_SECONDS = 60
_started = False
_last_check = {}  # slug -> datetime du dernier contrôle « rien n'a changé »


def run_due_backups(now=None):
    """Lance les sauvegardes dues. Renvoie [(slug, résultat)] ; appelable à la main pour tester."""
    from . import backups
    from .models import Server

    now = now or datetime.now()
    results = []
    for server in Server.objects.filter(backup_enabled=True):
        interval = timedelta(hours=server.backup_interval_hours)
        last = backups.latest(server)
        last_time = datetime.strptime(last.name, "%Y%m%d-%H%M%S") if last else None
        reference = max(filter(None, [last_time, _last_check.get(server.slug)]), default=None)
        if reference and now - reference < interval:
            continue
        try:
            name = backups.create_backup(server, reason="auto")
            results.append((server.slug, f"créée {name}"))
        except backups.BackupSkipped:
            # Rien n'a changé : on réessaiera à l'intervalle suivant.
            _last_check[server.slug] = now
            log.info("sauvegarde planifiée %s annulée : aucune modification", server.slug)
            results.append((server.slug, "annulée (aucune modification)"))
        except Exception as exc:  # un serveur en erreur ne bloque pas les autres
            log.exception("sauvegarde planifiée %s en échec", server.slug)
            results.append((server.slug, f"échec : {exc}"))
    return results


def _loop(lock_file):
    log.info("planificateur de sauvegardes démarré")
    while True:
        try:
            run_due_backups()
        except Exception:
            log.exception("planificateur de sauvegardes")
        time.sleep(TICK_SECONDS)


def start():
    """Démarre le fil si aucun autre processus ne l'a déjà (verrou fichier)."""
    global _started
    if _started:
        return
    _started = True
    lock_file = open(settings.BASE_DIR / ".scheduler.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return  # un autre worker gunicorn s'en occupe
    threading.Thread(target=_loop, args=(lock_file,), daemon=True, name="backup-scheduler").start()
