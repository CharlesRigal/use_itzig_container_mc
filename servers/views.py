import logging
import shlex
import shutil
import tarfile
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django.urls import reverse
from django.utils.http import urlencode

from . import backups, configs, podman
from .forms import AppSettingsForm, CommandForm, PlayerForm, ServerCreateForm, ServerForm, next_free_port
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
    try:
        states = podman.states()
    except podman.PodmanError as exc:
        messages.error(request, str(exc))
        states = {}
    rows = [
        (s, states.get(s.container_name, {"state": "absent", "status": ""}))
        for s in Server.objects.all()
    ]
    return render(request, "servers/list.html", {"rows": rows})


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
                _podman_action(request, podman.restart, server, success="Paramètres appliqués, serveur redémarré.")
            else:
                messages.success(request, "Paramètres enregistrés, ils s'appliqueront au prochain démarrage.")
            return redirect("server_detail", slug=slug)
    else:
        form = ServerForm(instance=server)
    try:
        st = podman.state(server)
    except podman.PodmanError as exc:
        messages.error(request, str(exc))
        st = {"state": "unknown", "status": ""}
    return render(
        request,
        "servers/detail.html",
        {
            "server": server,
            "form": form,
            "st": st,
            "player_form": PlayerForm(),
            "command_form": CommandForm(),
            "backups": backups.list_backups(server),
        },
    )


@login_required
@require_POST
def server_action(request, slug, action):
    server = get_object_or_404(Server, slug=slug)
    actions = {
        "start": (podman.start, "Serveur démarré. Le premier lancement télécharge Minecraft : compte 1 à 3 minutes."),
        "stop": (podman.stop, "Serveur arrêté."),
        "restart": (podman.restart, "Serveur redémarré."),
    }
    if action not in actions:
        raise Http404
    func, msg = actions[action]
    _podman_action(request, func, server, success=msg)
    if request.POST.get("next") == "list":
        return redirect("server_list")
    return redirect("server_detail", slug=slug)


@login_required
def server_delete(request, slug):
    server = get_object_or_404(Server, slug=slug)
    if request.method == "POST":
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
        {"server": server, "files": configs.list_files(server), "mods": configs.installed_mods(server)},
    )


@login_required
def file_edit(request, slug):
    server = get_object_or_404(Server, slug=slug)
    rel = request.GET.get("path", "")
    content = None
    if request.method == "POST":
        content = request.POST.get("content", "")
        try:
            configs.save(server, rel, content)
        except configs.ConfigError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{rel} enregistré. Redémarre le serveur pour l'appliquer.")
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
            "managed": configs.MANAGED_PROPERTIES if rel == "server.properties" else None,
        },
    )


@login_required
@require_POST
def file_restore(request, slug):
    server = get_object_or_404(Server, slug=slug)
    rel, ts = request.GET.get("path", ""), request.POST.get("ts", "")
    try:
        configs.restore(server, rel, ts)
    except configs.ConfigError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Version du {ts} restaurée (la version remplacée est gardée dans l'historique).")
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
    name = _podman_action(request, backups.create_backup, server)
    if name:
        messages.success(request, f"Sauvegarde {name} créée.")
    return redirect("server_detail", slug=slug)


@login_required
def backup_download(request, slug, name):
    server = get_object_or_404(Server, slug=slug)
    try:
        path = backups.backup_path(server, name)
    except FileNotFoundError:
        raise Http404
    return FileResponse(open(path, "rb"), as_attachment=True, filename=f"{server.slug}-{name}")


@login_required
@require_POST
def backup_restore(request, slug, name):
    server = get_object_or_404(Server, slug=slug)
    safety = _podman_action(request, backups.restore_backup, server, name)
    if safety:
        messages.success(request, f"Sauvegarde {name} restaurée. L'état précédent est conservé dans {safety}.")
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
