import getpass
from pathlib import Path

from django import forms
from django.conf import settings
from django.utils.text import slugify

from .models import AppSettings, Server, cf_key_file, mod_lines, player_name_validator

SERVER_FIELDS = [
    "name", "port", "type", "version", "java", "memory", "max_players", "difficulty",
    "gamemode", "motd", "view_distance", "pvp", "online_mode", "whitelist_enabled",
]
MOD_FIELDS = ["modpack_source", "modpack_ref", "curseforge_mods", "modrinth_mods"]


def next_free_port():
    used = set(Server.objects.values_list("port", flat=True))
    port = settings.MC_FIRST_PORT
    while port in used:
        port += 1
    return port


class ServerForm(forms.ModelForm):
    class Meta:
        model = Server
        fields = SERVER_FIELDS + MOD_FIELDS
        widgets = {
            "curseforge_mods": forms.Textarea(attrs={"rows": 5, "spellcheck": "false"}),
            "modrinth_mods": forms.Textarea(attrs={"rows": 5, "spellcheck": "false"}),
        }

    def base_fields_list(self):
        return [self[f] for f in SERVER_FIELDS]

    def mod_fields_list(self):
        return [self[f] for f in MOD_FIELDS]

    def clean(self):
        data = super().clean()
        source = data.get("modpack_source")
        if source and source != "none" and not data.get("modpack_ref", "").strip():
            self.add_error("modpack_ref", "Indique le lien ou le slug du modpack.")
        uses_cf = source == "curseforge" or mod_lines(data.get("curseforge_mods"))
        if uses_cf and not cf_key_file().is_file():
            raise forms.ValidationError("CurseForge demande une clé API : ajoute-la d'abord dans Paramètres.")
        return data


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
    cf_api_key = forms.CharField(
        label="clé API CurseForge",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Gratuite sur console.curseforge.com. Laisse vide pour garder la clé actuelle.",
    )
    cf_api_key_clear = forms.BooleanField(label="supprimer la clé enregistrée", required=False)

    class Meta:
        model = AppSettings
        fields = ["data_root", "backup_root"]

    def save_cf_key(self):
        path = cf_key_file()
        if self.cleaned_data.get("cf_api_key_clear"):
            path.unlink(missing_ok=True)
            return "supprimée"
        key = self.cleaned_data.get("cf_api_key", "").strip()
        if key:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.touch(mode=0o600, exist_ok=True)
            path.write_text(key)
            path.chmod(0o600)
            return "enregistrée"
        return None

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


class BackupScheduleForm(forms.ModelForm):
    class Meta:
        model = Server
        fields = ["backup_enabled", "backup_interval_hours", "backup_keep"]


class PlayerForm(forms.Form):
    name = forms.CharField(label="pseudo", max_length=16, validators=[player_name_validator])


class CommandForm(forms.Form):
    command = forms.CharField(label="commande", max_length=200)
