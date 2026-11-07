from django import forms
from django.conf import settings
from django.utils.text import slugify

from .models import Server, player_name_validator

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


class PlayerForm(forms.Form):
    name = forms.CharField(label="pseudo", max_length=16, validators=[player_name_validator])


class CommandForm(forms.Form):
    command = forms.CharField(label="commande", max_length=200)
