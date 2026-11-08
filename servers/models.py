import re
from pathlib import Path

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

player_name_validator = RegexValidator(
    r"^[A-Za-z0-9_]{3,16}$",
    "Pseudo Minecraft invalide : 3 à 16 caractères, lettres, chiffres ou _.",
)


class AppSettings(models.Model):
    """Paramètres de l'application (une seule ligne, pk=1)."""

    data_root = models.CharField(
        "dossier des mondes", max_length=255,
        help_text="Un sous-dossier par serveur y est créé, monté sur /data dans le conteneur.",
    )
    backup_root = models.CharField(
        "dossier des sauvegardes", max_length=255,
        help_text="Un sous-dossier par serveur, avec les archives .tar.gz.",
    )

    class Meta:
        verbose_name = "paramètres"
        verbose_name_plural = "paramètres"

    def __str__(self):
        return "Paramètres"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"data_root": str(settings.MC_DATA_ROOT), "backup_root": str(settings.MC_BACKUP_ROOT)},
        )
        return obj


class Server(models.Model):
    TYPES = [
        ("VANILLA", "Vanilla"),
        ("PAPER", "Paper"),
        ("PURPUR", "Purpur"),
        ("FABRIC", "Fabric"),
        ("FORGE", "Forge"),
        ("NEOFORGE", "NeoForge"),
    ]
    DIFFICULTIES = [
        ("peaceful", "Paisible"),
        ("easy", "Facile"),
        ("normal", "Normal"),
        ("hard", "Difficile"),
    ]
    GAMEMODES = [
        ("survival", "Survie"),
        ("creative", "Créatif"),
        ("adventure", "Aventure"),
        ("spectator", "Spectateur"),
    ]

    name = models.CharField("nom", max_length=60)
    slug = models.SlugField(unique=True, editable=False)
    port = models.PositiveIntegerField(
        "port", unique=True, validators=[MinValueValidator(1024), MaxValueValidator(65535)]
    )
    type = models.CharField("type", max_length=10, choices=TYPES, default="VANILLA")
    version = models.CharField(
        "version",
        max_length=20,
        default="LATEST",
        validators=[RegexValidator(r"^[A-Za-z0-9._-]+$", "Version invalide.")],
        help_text="LATEST, SNAPSHOT ou un numéro comme 1.21.4",
    )
    JAVA = [
        ("auto", "Automatique selon la version"),
        ("latest", "latest (Java le plus récent)"),
        ("java21", "Java 21"),
        ("java17", "Java 17"),
        ("java8", "Java 8"),
    ]
    java = models.CharField(
        "Java (tag de l'image)",
        max_length=10,
        choices=JAVA,
        default="auto",
        help_text="Les vieux Forge/NeoForge plantent avec un Java trop récent.",
    )
    memory = models.CharField(
        "mémoire",
        max_length=6,
        default="2G",
        validators=[RegexValidator(r"^[1-9][0-9]*[MG]$", "Format : 2G ou 1536M.")],
    )
    max_players = models.PositiveIntegerField(
        "joueurs max", default=10, validators=[MinValueValidator(1), MaxValueValidator(1000)]
    )
    difficulty = models.CharField("difficulté", max_length=10, choices=DIFFICULTIES, default="normal")
    gamemode = models.CharField("mode de jeu", max_length=10, choices=GAMEMODES, default="survival")
    motd = models.CharField("message du serveur (MOTD)", max_length=120, blank=True, default="Un serveur Minecraft")
    view_distance = models.PositiveIntegerField(
        "distance de vue", default=10, validators=[MinValueValidator(3), MaxValueValidator(32)]
    )
    pvp = models.BooleanField("PvP", default=True)
    online_mode = models.BooleanField(
        "comptes officiels uniquement (online-mode)", default=True
    )
    whitelist_enabled = models.BooleanField("whitelist active", default=True)

    MODPACK_SOURCES = [
        ("none", "Aucun"),
        ("curseforge", "Modpack CurseForge"),
        ("modrinth", "Modpack Modrinth"),
    ]
    modpack_source = models.CharField("modpack", max_length=12, choices=MODPACK_SOURCES, default="none")
    modpack_ref = models.CharField(
        "lien ou slug du modpack", max_length=300, blank=True,
        help_text="Lien de la page, slug ou Project ID (ex. https://www.curseforge.com/minecraft/modpacks/all-the-mods-9). "
                  "Le modpack décide du type et de la version : choisis Java à la main.",
    )
    curseforge_mods = models.TextField(
        "mods CurseForge", blank=True,
        help_text="Un par ligne : Project ID ou id:fichier (les slugs et liens demandent une clé avec "
                  "accès à la recherche). Les dépendances ne sont pas ajoutées automatiquement : "
                  "liste-les aussi. # pour un commentaire.",
    )
    modrinth_mods = models.TextField(
        "mods Modrinth", blank=True,
        help_text="Un par ligne : slug ou id (ex. lithium). Les dépendances requises sont ajoutées "
                  "automatiquement.",
    )
    backup_enabled = models.BooleanField("sauvegarde automatique", default=False)
    backup_interval_hours = models.PositiveIntegerField(
        "toutes les (heures)", default=6, validators=[MinValueValidator(1), MaxValueValidator(720)]
    )
    backup_keep = models.PositiveIntegerField(
        "sauvegardes gardées", default=10, validators=[MinValueValidator(1), MaxValueValidator(1000)],
        help_text="Les plus anciennes sont supprimées au-delà.",
    )
    backup_only_with_players = models.BooleanField(
        "seulement si des joueurs sont venus", default=True,
        help_text="La sauvegarde planifiée est annulée si personne ne s'est connecté depuis la précédente.",
    )

    # Suivi d'état (écrit par le manager, pas par l'utilisateur).
    last_action = models.CharField(max_length=10, blank=True, editable=False)
    last_action_at = models.DateTimeField(null=True, blank=True, editable=False)
    job = models.CharField(max_length=10, blank=True, editable=False)  # action en cours
    job_detail = models.CharField(max_length=200, blank=True, editable=False)
    job_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    job_error = models.TextField(blank=True, editable=False)  # échec de la dernière action
    last_ready_at = models.DateTimeField(null=True, blank=True, editable=False)
    players_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["port"]
        verbose_name = "serveur"

    def __str__(self):
        return self.name

    @property
    def container_name(self):
        return f"mc-{self.slug}"

    @property
    def data_dir(self) -> Path:
        return Path(AppSettings.get().data_root) / self.slug

    @property
    def backup_dir(self) -> Path:
        return Path(AppSettings.get().backup_root) / self.slug

    def image_tag(self):
        """Java requis par Minecraft : <1.17 → 8, 1.17 à 1.20.4 → 17, 1.20.5 à 1.21.x → 21."""
        if self.java != "auto":
            return self.java
        m = re.fullmatch(r"1\.(\d+)(?:\.(\d+))?", self.version)
        if not m:
            return "latest"
        minor, patch = int(m.group(1)), int(m.group(2) or 0)
        if minor < 17:
            return "java8"
        if minor < 20 or (minor == 20 and patch <= 4):
            return "java17"
        if minor <= 21:
            return "java21"
        return "latest"

    @property
    def image(self):
        return f"{settings.MC_IMAGE.rsplit(':', 1)[0]}:{self.image_tag()}"

    def whitelist_names(self):
        return list(self.whitelist.values_list("name", flat=True))

    def container_env(self):
        """Variables lues par l'image itzg/minecraft-server à chaque démarrage."""
        env = {
            "EULA": "TRUE",
            "TZ": settings.MC_TZ,
            "TYPE": self.type,
            "VERSION": self.version,
            "MEMORY": self.memory,
            "MAX_PLAYERS": str(self.max_players),
            "DIFFICULTY": self.difficulty,
            "MODE": self.gamemode,
            "MOTD": self.motd,
            "VIEW_DISTANCE": str(self.view_distance),
            "PVP": str(self.pvp).lower(),
            "ONLINE_MODE": str(self.online_mode).lower(),
            "ENABLE_WHITELIST": str(self.whitelist_enabled).lower(),
            "ENFORCE_WHITELIST": str(self.whitelist_enabled).lower(),
        }
        names = self.whitelist_names()
        if names:
            env["WHITELIST"] = ",".join(names)
            # La liste de l'UI fait foi : whitelist.json est réécrit à chaque démarrage.
            env["EXISTING_WHITELIST_FILE"] = "SYNCHRONIZE"

        # Mods : l'image télécharge tout au démarrage.
        ref = self.modpack_ref.strip()
        if self.modpack_source == "curseforge":
            # Pack téléchargé par le manager (servers/curseforge.py) : l'image lit le manifest,
            # ce qui évite l'endpoint de recherche souvent refusé aux clés perso.
            env["TYPE"] = "AUTO_CURSEFORGE"
            env["CF_MODPACK_MANIFEST"] = "/data/.mcmanager/manifest.json"
            work = self.data_dir / ".mcmanager"
            if (work / "modpack-slug").is_file():
                env["CF_SLUG"] = (work / "modpack-slug").read_text().strip()
            # Exclusions déjà traduites en ids par le manager ; la liste par slug de l'image
            # déclencherait des recherches refusées.
            env["CF_EXCLUDE_INCLUDE_FILE"] = ""
            if (work / "exclude-ids").is_file() and (work / "exclude-ids").read_text().strip():
                env["CF_EXCLUDE_MODS"] = (work / "exclude-ids").read_text().strip()
            del env["VERSION"]
        elif self.modpack_source == "modrinth":
            env["TYPE"] = "MODRINTH"
            env["MODRINTH_MODPACK"] = ref
            del env["VERSION"]
        cf = mod_lines(self.curseforge_mods)
        if cf:
            env["CURSEFORGE_FILES"] = ",".join(cf)
        mr = mod_lines(self.modrinth_mods)
        if mr:
            env["MODRINTH_PROJECTS"] = ",".join(mr)
            env["MODRINTH_DOWNLOAD_DEPENDENCIES"] = "required"
        if self.uses_curseforge:
            env["CF_API_KEY_FILE"] = CF_KEY_IN_CONTAINER
        return env

    @property
    def uses_curseforge(self):
        return self.modpack_source == "curseforge" or bool(mod_lines(self.curseforge_mods))


CF_KEY_IN_CONTAINER = "/run/secrets/cf_api_key"


def cf_key_file() -> Path:
    return settings.BASE_DIR / "secrets" / "cf_api_key"


def mod_lines(text):
    """Lignes non vides, sans les commentaires # ."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


class WhitelistEntry(models.Model):
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="whitelist")
    name = models.CharField("pseudo", max_length=16, validators=[player_name_validator])

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["server", "name"], name="unique_player_per_server")
        ]

    def __str__(self):
        return self.name


class DefaultWhitelistEntry(models.Model):
    """Joueurs ajoutés automatiquement à la whitelist des nouveaux serveurs."""

    name = models.CharField("pseudo", max_length=16, unique=True, validators=[player_name_validator])

    class Meta:
        ordering = ["name"]
        verbose_name = "joueur de la whitelist par défaut"

    def __str__(self):
        return self.name
