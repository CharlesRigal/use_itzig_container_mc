"""Sauvegardes planifiées : un fil dans le service web, un seul même si plusieurs processus tournent."""

import fcntl
import logging
import threading
import time
from datetime import datetime, timedelta

from django.conf import settings

log = logging.getLogger("servers")
TICK_SECONDS = 60
SAMPLE_SECONDS = 20  # échantillonnage du monitoring
UPNP_SECONDS = 600  # renouvellement des redirections (bail d'une heure)
_started = False
_last_check = {}  # slug -> datetime du dernier contrôle « rien n'a changé »


def run_due_backups(now=None):
    """Lance les sauvegardes dues. Renvoie [(slug, résultat)] ; appelable à la main pour tester."""
    from django.utils import timezone

    from . import backups, jobs, status
    from .models import Server

    now = now or datetime.now()
    servers = list(Server.objects.all())
    # Le ping de chaque minute note quand des joueurs étaient connectés (players_seen_at).
    status.collect(servers)
    results = []
    for server in servers:
        if not server.backup_enabled or jobs.is_busy(server):
            continue
        interval = timedelta(hours=server.backup_interval_hours)
        last = backups.latest(server)
        last_time = datetime.strptime(last.name, "%Y%m%d-%H%M%S") if last else None
        reference = max(filter(None, [last_time, _last_check.get(server.slug)]), default=None)
        if reference and now - reference < interval:
            continue
        if server.backup_only_with_players and last_time:
            seen = server.players_seen_at
            if seen is None or timezone.localtime(seen).replace(tzinfo=None) < last_time:
                _last_check[server.slug] = now
                log.info("sauvegarde planifiée %s annulée : aucun joueur depuis la précédente", server.slug)
                results.append((server.slug, "annulée (aucun joueur)"))
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
    from . import monitoring, upnp

    log.info("planificateur (sauvegardes + monitoring + UPnP) démarré")
    last_backup_check = last_upnp = 0.0
    while True:
        started = time.monotonic()
        try:
            monitoring.sample()
        except Exception:
            log.exception("monitoring")
        if started - last_upnp >= UPNP_SECONDS:
            last_upnp = started
            upnp.sync_quietly()
        if started - last_backup_check >= TICK_SECONDS:
            last_backup_check = started
            try:
                run_due_backups()
            except Exception:
                log.exception("planificateur de sauvegardes")
        time.sleep(max(1.0, SAMPLE_SECONDS - (time.monotonic() - started)))


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
        return  # un autre processus s'en occupe
    threading.Thread(target=_loop, args=(lock_file,), daemon=True, name="backup-scheduler").start()
