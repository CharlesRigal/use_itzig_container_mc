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
        return env


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
