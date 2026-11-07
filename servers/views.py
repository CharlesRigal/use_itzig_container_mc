import shlex
import shutil
import tarfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import backups, podman
from .forms import CommandForm, PlayerForm, ServerCreateForm, ServerForm, next_free_port
from .models import DefaultWhitelistEntry, Server, WhitelistEntry


def _podman_action(request, func, *args, success=None):
    try:
        result = func(*args)
    except (podman.PodmanError, OSError, tarfile.TarError) as exc:
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
