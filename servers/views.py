import logging
import shlex
import shutil
import tarfile
import time
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from datetime import datetime, timedelta

from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django.urls import reverse
from django.utils.http import urlencode

from . import backups, configs, jobs, monitoring, podman, scheduler, status, upnp
from .forms import AppSettingsForm, BackupScheduleForm, CommandForm, PlayerForm, ServerCreateForm, ServerForm, next_free_port
from .models import AppSettings, DefaultWhitelistEntry, Server, WhitelistEntry, cf_key_file


log = logging.getLogger("servers")


def _podman_action(request, func, *args, success=None):
    try:
        result = func(*args)
    except (podman.PodmanError, OSError, tarfile.TarError) as exc:
        log.exception("%s%s a échoué", func.__name__, args)
        messages.error(request, str(exc) or exc.__class__.__name__)
        return None
    if success:
        messages.success(request, success)
    return result


@login_required
def server_list(request):
    servers = list(Server.objects.all())
    statuses = status.collect(servers)
    rows = [(s, statuses[s.slug]) for s in servers]
    return render(
        request, "servers/list.html",
        {"rows": rows, "initial_status": {k: status.as_json(v) for k, v in statuses.items()}},
    )


@login_required
def status_json(request):
    """État de tous les serveurs, interrogé par les pages toutes les quelques secondes."""
    statuses = status.collect(Server.objects.all())
    return JsonResponse({k: status.as_json(v) for k, v in statuses.items()})


@login_required
def monitoring_json(request, slug):
    """Historique (1, 6 ou 24 h) et valeurs instantanées du panneau de monitoring."""
    server = get_object_or_404(Server, slug=slug)
    hours = {"1": 1, "6": 6, "24": 24}.get(request.GET.get("h"), 1)
    st = status.for_server(server)
    players = monitoring.player_names(server) if st["key"] == "ready" and st.get("players") else []
    return JsonResponse({
        "series": monitoring.series(server, hours),
        "host": monitoring.host_now(AppSettings.get().data_root),
        "heap": server.memory,
        "max_players": st.get("max") or server.max_players,
        "players": players,
        "state": st["key"],
        "sample_seconds": scheduler.SAMPLE_SECONDS,
        "now": int(time.time() * 1000),  # l'horloge du serveur, pas celle du navigateur
    })


@login_required
@require_POST
def upnp_toggle(request, slug):
    """Active ou coupe l'ouverture automatique du port sur la box."""
    server = get_object_or_404(Server, slug=slug)
    server.upnp_enabled = request.POST.get("enabled") == "1"
    server.save(update_fields=["upnp_enabled"])
    upnp.sync_quietly()
    server.refresh_from_db()
    if not server.upnp_enabled:
        messages.success(request, "Accès depuis Internet fermé.")
    elif server.upnp_address:
        messages.success(request, f"Port ouvert : tes amis se connectent sur {server.upnp_address}.")
    elif server.upnp_error:
        messages.error(request, f"Ouverture du port impossible : {server.upnp_error}")
    else:
        messages.success(request, "Le port sera ouvert dès que le serveur sera démarré.")
    return redirect("server_detail", slug=slug)


def _launch(request, server, action, success):
    try:
        jobs.launch(server, action)
    except jobs.Busy as exc:
        messages.error(request, str(exc))
        return False
    messages.success(request, success)
    return True


@login_required
def server_create(request):
    if request.method == "POST":
        form = ServerCreateForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                server = form.save()
                if form.cleaned_data["use_default_whitelist"]:
                    WhitelistEntry.objects.bulk_create(
                        WhitelistEntry(server=server, name=p.name)
                        for p in DefaultWhitelistEntry.objects.all()
                    )
            # En cas d'échec, le conteneur sera recréé au premier démarrage.
            _podman_action(request, podman.create, server, success=f"Serveur « {server} » créé.")
            return redirect("server_detail", slug=server.slug)
    else:
        form = ServerCreateForm(initial={"port": next_free_port()})
    return render(request, "servers/create.html", {"form": form})


@login_required
def server_detail(request, slug):
    server = get_object_or_404(Server, slug=slug)
    if request.method == "POST":
        form = ServerForm(request.POST, instance=server)
        if form.is_valid():
            form.save()
            if podman.is_running(server):
                _launch(request, server, "restart", "Paramètres enregistrés, redémarrage lancé.")
            else:
                messages.success(request, "Paramètres enregistrés, ils s'appliqueront au prochain démarrage.")
            return redirect("server_detail", slug=slug)
    else:
        form = ServerForm(instance=server)
    st = status.for_server(server)
    return render(
        request,
        "servers/detail.html",
        {
            "server": server,
            "form": form,
            "st": st,
            "initial_status": {server.slug: status.as_json(st)},
            "player_form": PlayerForm(),
            "command_form": CommandForm(),
            "backups": (blist := backups.list_backups(server)),
            "schedule_form": BackupScheduleForm(instance=server),
            "next_backup": _next_backup(server, blist),
        },
    )


def _next_backup(server, blist):
    if not server.backup_enabled:
        return None
    if not blist:
        return "dans moins d'une minute"
    nxt = blist[0]["date"] + timedelta(hours=server.backup_interval_hours)
    return "dans moins d'une minute" if nxt <= datetime.now() else f"vers {nxt:%d/%m %H:%M}"


@login_required
@require_POST
def server_action(request, slug, action):
    server = get_object_or_404(Server, slug=slug)
    messages_ok = {
        "start": "Démarrage lancé. Suis l'état en direct : installation, démarrage, puis « Prêt ».",
        "stop": "Arrêt lancé.",
        "restart": "Redémarrage lancé.",
    }
    if action not in messages_ok:
        raise Http404
    _launch(request, server, action, messages_ok[action])
    if request.POST.get("next") == "list":
        return redirect("server_list")
    return redirect("server_detail", slug=slug)


@login_required
def server_delete(request, slug):
    server = get_object_or_404(Server, slug=slug)
    if request.method == "POST":
        if jobs.is_busy(server):
            messages.error(request, "Une action est en cours sur ce serveur, attends qu'elle se termine.")
            return redirect("server_detail", slug=slug)
        try:
            podman.remove(server)
        except podman.PodmanError as exc:
            messages.error(request, f"Conteneur non supprimé : {exc}")
            return redirect("server_detail", slug=slug)
        if request.POST.get("delete_data"):
            shutil.rmtree(server.data_dir, ignore_errors=True)
            shutil.rmtree(server.backup_dir, ignore_errors=True)
        name = server.name
        server.delete()
        messages.success(request, f"Serveur « {name} » supprimé.")
        return redirect("server_list")
    return render(request, "servers/delete.html", {"server": server})


@login_required
def server_logs(request, slug):
    server = get_object_or_404(Server, slug=slug)
    try:
        text = podman.logs(server)
    except podman.PodmanError as exc:
        text = str(exc)
    return HttpResponse(text, content_type="text/plain; charset=utf-8")


@login_required
@require_POST
def server_command(request, slug):
    server = get_object_or_404(Server, slug=slug)
    form = CommandForm(request.POST)
    if form.is_valid():
        cmd = form.cleaned_data["command"].lstrip("/")
        out = _podman_action(request, podman.rcon, server, *shlex.split(cmd))
        if out is not None:
            messages.info(request, f"/{cmd} → {out or '(aucune réponse)'}")
    return redirect("server_detail", slug=slug)


# --- whitelist d'un serveur -------------------------------------------------

def _live_whitelist(request, server, *command):
    """Applique tout de suite si le serveur tourne ; sinon ce sera au prochain démarrage."""
    if server.whitelist_enabled and podman.is_running(server):
        _podman_action(request, podman.rcon, server, "whitelist", *command)


@login_required
@require_POST
def whitelist_add(request, slug):
    server = get_object_or_404(Server, slug=slug)
    form = PlayerForm(request.POST)
    if form.is_valid():
        name = form.cleaned_data["name"]
        _, created = WhitelistEntry.objects.get_or_create(server=server, name=name)
        if created:
            _live_whitelist(request, server, "add", name)
            messages.success(request, f"{name} ajouté à la whitelist.")
    else:
        for err in form.errors.get("name", []):
            messages.error(request, err)
    return redirect("server_detail", slug=slug)


@login_required
@require_POST
def whitelist_remove(request, slug, pk):
    server = get_object_or_404(Server, slug=slug)
    entry = get_object_or_404(WhitelistEntry, pk=pk, server=server)
    entry.delete()
    _live_whitelist(request, server, "remove", entry.name)
    messages.success(request, f"{entry.name} retiré de la whitelist.")
    return redirect("server_detail", slug=slug)


# --- whitelist par défaut -----------------------------------------------------

@login_required
def default_whitelist(request):
    form = PlayerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        DefaultWhitelistEntry.objects.get_or_create(name=form.cleaned_data["name"])
        messages.success(request, f"{form.cleaned_data['name']} ajouté à la whitelist par défaut.")
        return redirect("default_whitelist")
    return render(
        request,
        "servers/default_whitelist.html",
        {"form": form, "players": DefaultWhitelistEntry.objects.all(), "servers": Server.objects.all()},
    )


@login_required
@require_POST
def default_whitelist_remove(request, pk):
    get_object_or_404(DefaultWhitelistEntry, pk=pk).delete()
    return redirect("default_whitelist")


@login_required
@require_POST
def default_whitelist_apply(request, slug):
    """Ajoute les joueurs par défaut à un serveur existant."""
    server = get_object_or_404(Server, slug=slug)
    added = []
    for p in DefaultWhitelistEntry.objects.all():
        _, created = WhitelistEntry.objects.get_or_create(server=server, name=p.name)
        if created:
            added.append(p.name)
            _live_whitelist(request, server, "add", p.name)
    messages.success(request, f"{len(added)} joueur(s) ajouté(s) à « {server} ».")
    return redirect("server_detail", slug=slug)


# --- fichiers de configuration ----------------------------------------------------

@login_required
def server_files(request, slug):
    server = get_object_or_404(Server, slug=slug)
    return render(
        request,
        "servers/files.html",
        {"server": server, "files": configs.list_files(server), "mods": configs.installed_mods(server),
         "pending": configs.pending_files(server)},
    )


def _container_up(server):
    """Le jeu tourne (ou démarre) : il pourrait réécrire ses configs à l'arrêt."""
    try:
        return podman.state(server)["state"] in ("running", "paused", "stopping")
    except podman.PodmanError:
        return True  # dans le doute, on ne risque pas une modification perdue


def _saved_message(rel, result):
    if result == "pending":
        return (f"{rel} enregistré, en attente : le jeu réécrit ce fichier quand il s'arrête, "
                "la modification sera donc copiée au prochain démarrage. Redémarre le serveur pour l'appliquer.")
    return f"{rel} enregistré. Redémarre le serveur pour l'appliquer (s'il tourne)."


@login_required
def file_edit(request, slug):
    server = get_object_or_404(Server, slug=slug)
    rel = request.GET.get("path", "")
    content = None
    if request.method == "POST":
        content = request.POST.get("content", "")
        try:
            result = configs.save(server, rel, content, running=_container_up(server))
        except configs.ConfigError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _saved_message(rel, result))
            return redirect(f"{request.path}?{urlencode({'path': rel})}")
    try:
        if content is None:
            content = configs.read(server, rel)
    except configs.ConfigError as exc:
        messages.error(request, str(exc))
        return redirect("server_files", slug=slug)
    return render(
        request,
        "servers/file_edit.html",
        {
            "server": server,
            "rel": rel,
            "content": content,
            "history": configs.history(server, rel),
            "pending": configs.is_pending(server, rel),
            "managed": configs.MANAGED_PROPERTIES if rel == "server.properties" else None,
        },
    )


@login_required
@require_POST
def file_restore(request, slug):
    server = get_object_or_404(Server, slug=slug)
    rel, ts = request.GET.get("path", ""), request.POST.get("ts", "")
    try:
        result = configs.restore(server, rel, ts, running=_container_up(server))
    except configs.ConfigError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Version du {ts} restaurée. " + _saved_message(rel, result))
    return redirect(f"{reverse('file_edit', args=[slug])}?{urlencode({'path': rel})}")


# --- paramètres de l'application ------------------------------------------------

def _disk(path):
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}


def _move_tree(old_root, new_root, slug):
    src, dst = Path(old_root) / slug, Path(new_root) / slug
    if not src.exists():
        return
    if dst.exists() and any(dst.iterdir()):
        raise OSError(f"{dst} existe déjà et n'est pas vide.")
    if dst.exists():
        dst.rmdir()
    shutil.move(str(src), str(dst))


@login_required
def app_settings(request):
    current = AppSettings.get()
    old = {"data_root": current.data_root, "backup_root": current.backup_root}
    form = AppSettingsForm(request.POST or None, instance=current)
    if request.method == "POST" and form.is_valid():
        changed = [k for k in old if form.cleaned_data[k] != old[k]]
        servers = list(Server.objects.all())
        if changed and servers and form.cleaned_data["move_existing"]:
            try:
                states = podman.states()
            except podman.PodmanError as exc:
                messages.error(request, str(exc))
                return redirect("app_settings")
            running = [s.name for s in servers if states.get(s.container_name, {}).get("state") == "running"]
            if running:
                messages.error(request, "Arrête d'abord ces serveurs : " + ", ".join(running))
                return redirect("app_settings")
            try:
                for key in changed:
                    for s in servers:
                        _move_tree(old[key], form.cleaned_data[key], s.slug)
            except OSError as exc:
                log.exception("déplacement des données")
                messages.error(request, f"Déplacement interrompu : {exc}. Paramètres non modifiés.")
                return redirect("app_settings")
            # Les conteneurs pointent vers l'ancien dossier : on les recrée (arrêtés).
            for s in servers:
                _podman_action(request, podman.recreate, s)
        form.save()
        log.info("paramètres : %s", {k: form.cleaned_data[k] for k in old})
        key_change = form.save_cf_key()
        messages.success(request, "Paramètres enregistrés." + (f" Clé CurseForge {key_change}." if key_change else ""))
        return redirect("app_settings")
    return render(
        request,
        "servers/settings.html",
        {
            "form": form,
            "disks": [
                ("Mondes", current.data_root, _disk(current.data_root)),
                ("Sauvegardes", current.backup_root, _disk(current.backup_root)),
            ],
            "has_servers": Server.objects.exists(),
            "has_cf_key": cf_key_file().is_file(),
        },
    )


# --- sauvegardes ---------------------------------------------------------------

@login_required
@require_POST
def backup_create(request, slug):
    server = get_object_or_404(Server, slug=slug)
    force = bool(request.POST.get("force"))
    st = status.for_server(server)
    if st["key"] in ("preparing", "installing", "starting", "stopping", "restoring"):
        messages.error(request, f"Sauvegarde impossible pendant : {st['label'].lower()}. Réessaie quand le serveur est prêt ou arrêté.")
        return redirect("server_detail", slug=slug)
    try:
        name = backups.create_backup(server, reason="manual", skip_unchanged=not force)
    except backups.BackupSkipped as exc:
        messages.info(request, str(exc))
    except (podman.PodmanError, OSError) as exc:
        log.exception("sauvegarde %s", slug)
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Sauvegarde {name} créée.")
    return redirect("server_detail", slug=slug)


@login_required
@require_POST
def backup_schedule(request, slug):
    server = get_object_or_404(Server, slug=slug)
    form = BackupScheduleForm(request.POST, instance=server)
    if form.is_valid():
        form.save()
        removed = backups.apply_retention(server)
        msg = "Planification enregistrée."
        if removed:
            msg += f" {len(removed)} ancienne(s) sauvegarde(s) supprimée(s) pour n'en garder que {server.backup_keep}."
        messages.success(request, msg)
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("server_detail", slug=slug)


@login_required
def backup_download(request, slug, name):
    server = get_object_or_404(Server, slug=slug)
    try:
        backups.snapshot_path(server, name)
    except FileNotFoundError:
        raise Http404
    response = StreamingHttpResponse(backups.stream_tar(server, name), content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="{server.slug}-{name}.tar.gz"'
    return response


@login_required
@require_POST
def backup_restore(request, slug, name):
    server = get_object_or_404(Server, slug=slug)
    try:
        jobs.restore(server, name)
    except FileNotFoundError:
        raise Http404
    except jobs.Busy as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Restauration de {name} lancée. L'état actuel est sauvegardé avant (« avant restauration »).")
    return redirect("server_detail", slug=slug)


@login_required
@require_POST
def backup_delete(request, slug, name):
    server = get_object_or_404(Server, slug=slug)
    try:
        backups.delete_backup(server, name)
    except FileNotFoundError:
        raise Http404
    messages.success(request, f"Sauvegarde {name} supprimée.")
    return redirect("server_detail", slug=slug)
