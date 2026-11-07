import getpass
from pathlib import Path

from django import forms
from django.conf import settings
from django.utils.text import slugify

from .models import AppSettings, Server, player_name_validator

SERVER_FIELDS = [
    "name", "port", "type", "version", "memory", "max_players", "difficulty",
    "gamemode", "motd", "view_distance", "pvp", "online_mode", "whitelist_enabled",
]


def next_free_port():
    used = set(Server.objects.values_list("port", flat=True))
    port = settings.MC_FIRST_PORT
    while port in used:
        port += 1
    return port


class ServerForm(forms.ModelForm):
    class Meta:
        model = Server
        fields = SERVER_FIELDS


class ServerCreateForm(ServerForm):
    use_default_whitelist = forms.BooleanField(
        label="ajouter les joueurs de la whitelist par défaut", required=False, initial=True
    )

    def clean_name(self):
        name = self.cleaned_data["name"]
        slug = slugify(name)
        if not slug:
            raise forms.ValidationError("Le nom doit contenir au moins une lettre ou un chiffre.")
        if Server.objects.filter(slug=slug).exists():
            raise forms.ValidationError("Un serveur porte déjà ce nom.")
        self.instance.slug = slug
        return name


def _check_dir(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise forms.ValidationError("Donne un chemin absolu, par exemple /data/minecraft/servers.")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mcmanager-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise forms.ValidationError(
            f"Dossier inutilisable ({exc.strerror}). Vérifie qu'il existe et appartient à l'utilisateur {getpass.getuser()}."
        )
    return str(path.resolve())


class AppSettingsForm(forms.ModelForm):
    move_existing = forms.BooleanField(
        label="déplacer les mondes et sauvegardes existants vers les nouveaux dossiers",
        required=False,
        initial=True,
    )

    class Meta:
        model = AppSettings
        fields = ["data_root", "backup_root"]

    def clean_data_root(self):
        return _check_dir(self.cleaned_data["data_root"])

    def clean_backup_root(self):
        return _check_dir(self.cleaned_data["backup_root"])

    def clean(self):
        data = super().clean()
        a, b = data.get("data_root"), data.get("backup_root")
        if a and b:
            pa, pb = Path(a), Path(b)
            if pa == pb or pa in pb.parents or pb in pa.parents:
                raise forms.ValidationError("Les deux dossiers doivent être distincts et non imbriqués.")
        return data


class PlayerForm(forms.Form):
    name = forms.CharField(label="pseudo", max_length=16, validators=[player_name_validator])


class CommandForm(forms.Form):
    command = forms.CharField(label="commande", max_length=200)
