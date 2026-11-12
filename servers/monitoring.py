"""Monitoring : CPU, RAM, TPS, joueurs par serveur, et état de la machine hôte.

Le noyau du Raspberry Pi n'active pas le contrôleur mémoire des cgroups (podman stats
affiche 0 o) : la RAM d'un serveur est la somme du RSS des processus de son cgroup.
"""

import logging
import os
import re
import shutil
import time
from datetime import timedelta

from django.utils import timezone

from . import podman

log = logging.getLogger("servers")
NCPU = os.cpu_count() or 1
KEEP = timedelta(hours=24)
ANSI = re.compile(r"\x1b\[[0-9;]*m")

_prev_cpu = {}  # clé -> (instant monotone, µs CPU cumulées)
_tps_cmd = {}  # slug -> commande TPS qui a marché


def _cpu_percent(key, usage_usec, now):
    """% de la machine entière depuis l'échantillon précédent."""
    prev = _prev_cpu.get(key)
    _prev_cpu[key] = (now, usage_usec)
    if not prev or now <= prev[0] or usage_usec < prev[1]:
        return None
    return round(100 * (usage_usec - prev[1]) / 1e6 / (now - prev[0]) / NCPU, 1)


# --- hôte ---------------------------------------------------------------------------

def _meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) / 1024  # Mo
    return info


def _temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        return None


def host_sample(now):
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:8]]
    idle = fields[3] + fields[4]
    busy_usec = (sum(fields) - idle) * 1e6 / os.sysconf("SC_CLK_TCK")
    m = _meminfo()
    return {
        "cpu": _cpu_percent("host", busy_usec, now),
        "mem_mb": round(m["MemTotal"] - m["MemAvailable"]),
        "swap_mb": round(m["SwapTotal"] - m["SwapFree"]),
        "temp": _temp(),
    }


def host_now(data_root):
    m = _meminfo()
    try:
        du = shutil.disk_usage(data_root)
        disk = {"used_gb": round(du.used / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
    except OSError:
        disk = None
    with open("/proc/loadavg") as f:
        load = [float(x) for x in f.read().split()[:3]]
    return {
        "mem_total_mb": round(m["MemTotal"]),
        "mem_available_mb": round(m["MemAvailable"]),
        "swap_total_mb": round(m["SwapTotal"]),
        "swap_mb": round(m["SwapTotal"] - m["SwapFree"]),
        "temp": _temp(),
        "load": load,
        "ncpu": NCPU,
        "disk": disk,
    }


# --- conteneur ----------------------------------------------------------------------

def _cgroup_dir(pid):
    with open(f"/proc/{pid}/cgroup") as f:
        for line in f:
            if line.startswith("0::"):
                return "/sys/fs/cgroup" + line.strip()[3:]
    return None


def container_sample(server, ps, now):
    """CPU et RAM du conteneur à partir de son cgroup (pid lu dans `podman ps`)."""
    pid = (ps or {}).get("Pid")
    if not pid:
        return {}
    try:
        cg = _cgroup_dir(pid)
        with open(f"{cg}/cpu.stat") as f:
            usage = next(int(l.split()[1]) for l in f if l.startswith("usage_usec"))
        rss = 0
        with open(f"{cg}/cgroup.procs") as f:
            for p in f.read().split():
                try:
                    with open(f"/proc/{p}/status") as st:
                        rss += next((int(l.split()[1]) for l in st if l.startswith("VmRSS:")), 0)
                except OSError:
                    pass  # processus terminé entre-temps
    except (OSError, StopIteration, TypeError):
        return {}
    return {"cpu": _cpu_percent(server.slug, usage, now), "mem_mb": round(rss / 1024)}


def _rcon(server, *cmd):
    proc = podman._run(["exec", server.container_name, "rcon-cli", *cmd], check=False, timeout=15)
    return ANSI.sub("", proc.stdout) if proc.returncode == 0 else ""


def _parse_tps(text):
    """(tps, ms par tick) depuis forge/neoforge tps, tps (Paper) ou tick query (vanilla)."""
    if not text.strip() or "Unknown or incomplete command" in text or "Unknown command" in text:
        return None
    paper = re.search(r"TPS from last [^:]*:\s*\*?([\d.]+)", text)
    if paper:
        return float(paper.group(1)), None
    avg = re.search(r"Average time per tick:\s*([\d.]+)\s*ms", text)
    if avg:
        mspt = float(avg.group(1))
        return round(min(20.0, 1000 / max(mspt, 0.001)), 2), mspt
    # Forge : une ligne par dimension, puis « Overall: Mean tick time: … ms. Mean TPS: … ».
    lines = [l for l in text.splitlines() if "overall" in l.lower()]
    for line in lines:
        ms = re.search(r"([\d.]+)\s*ms", line)
        tps = re.search(r"TPS:?\s*([\d.]+)|([\d.]+)\s*TPS", line)
        if ms or tps:
            mspt = float(ms.group(1)) if ms else None
            tval = float(next(g for g in tps.groups() if g)) if tps else min(20.0, 1000 / max(mspt, 0.001))
            return round(tval, 2), mspt
    return None


def tps(server):
    candidates = [_tps_cmd[server.slug]] if server.slug in _tps_cmd else ["forge tps", "neoforge tps", "tps", "tick query"]
    for cmd in candidates:
        result = _parse_tps(_rcon(server, *cmd.split()))
        if result:
            _tps_cmd[server.slug] = cmd
            return result
    return None


def player_names(server):
    text = _rcon(server, "list")
    m = re.search(r"online:\s*(.*)", text)
    return [n.strip() for n in m.group(1).split(",") if n.strip()] if m else []


# --- échantillonnage ----------------------------------------------------------------

_samples = 0


def sample():
    """Un échantillon pour l'hôte et chaque serveur démarré (appelé par le planificateur)."""
    global _samples
    from . import status
    from .models import Metric, Server

    now_wall, now = timezone.now(), time.monotonic()
    rows = [Metric(server=None, at=now_wall, **host_sample(now))]
    servers = list(Server.objects.all())
    statuses = status.collect(servers)
    try:
        by_name = {n: c for c in podman.ps_json() for n in (c.get("Names") or [])}
    except podman.PodmanError:
        by_name = {}
    for s in servers:
        ps = by_name.get(s.container_name)
        if not ps or ps.get("State") != "running":
            _prev_cpu.pop(s.slug, None)
            continue
        row = Metric(server=s, at=now_wall, **container_sample(s, ps, now))
        st = statuses.get(s.slug, {})
        if st.get("key") == "ready":
            row.players = st.get("players")
            t = tps(s)
            if t:
                row.tps, row.mspt = t
        rows.append(row)
    Metric.objects.bulk_create(rows)
    from . import hub

    hub.publish("metrics", {})
    _samples += 1
    if _samples % 180 == 1:
        Metric.objects.filter(at__lt=now_wall - KEEP).delete()


FIELDS = ("cpu", "mem_mb", "swap_mb", "temp", "players", "tps", "mspt")


def series(server, hours, points=240):
    """Historique moyenné par tranches : {"server"|"host": {"t": [ms epoch], champ: [valeur|None]}}."""
    from .models import Metric

    since = timezone.now() - timedelta(hours=hours)
    bucket = hours * 3600 / points
    out = {}
    for key, qs in (("server", Metric.objects.filter(server=server)), ("host", Metric.objects.filter(server=None))):
        acc = {}
        for row in qs.filter(at__gte=since).order_by("at").values("at", *FIELDS):
            a = acc.setdefault(int(row["at"].timestamp() // bucket), {f: [] for f in FIELDS})
            for f in FIELDS:
                if row[f] is not None:
                    a[f].append(row[f])
        keys = sorted(acc)
        data = {"t": [int((b + 0.5) * bucket * 1000) for b in keys]}
        for f in FIELDS:
            data[f] = [round(sum(v) / len(v), 2) if (v := acc[b][f]) else None for b in keys]
        out[key] = data
    return out
