# MC Manager

Interface web Django pour gérer plusieurs serveurs Minecraft dans des conteneurs podman
(image `docker.io/itzg/minecraft-server:latest`) sur le Raspberry Pi.

## Fonctions

- Créer, démarrer, arrêter, redémarrer, supprimer des serveurs (un conteneur `mc-<nom>` par serveur).
- Paramètres de base : type (Vanilla, Paper, Fabric…), version, mémoire, joueurs max, difficulté,
  mode de jeu, MOTD, distance de vue, PvP, online-mode, whitelist.
- Console : logs en direct et commandes via `rcon-cli`.
- Whitelist par serveur, appliquée tout de suite si le serveur tourne.
- Whitelist par défaut, copiée dans chaque nouveau serveur.
- Sauvegardes `.tar.gz` à chaud (`save-off` / `save-all flush`), téléchargement, restauration
  (l'état actuel est sauvegardé avant), suppression.

## Fonctionnement

- Les paramètres sont des variables d'environnement de l'image itzg, figées à la création du conteneur.
  Chaque démarrage **recrée** donc le conteneur avec les paramètres actuels ; le monde est dans
  `data/servers/<nom>` (monté sur `/data`) et n'est jamais touché.
- La whitelist de l'interface fait foi : `whitelist.json` est réécrit à chaque démarrage
  (`EXISTING_WHITELIST_FILE=SYNCHRONIZE`).
- `--userns=keep-id` : les fichiers du monde appartiennent à l'utilisateur, pas à un sous-uid.
- Sauvegardes dans `data/backups/<nom>/`. Les fichiers retéléchargeables (jars, `libraries`, `logs`) sont exclus.

## Installation

```bash
cd ~/mcmanager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
mkdir -p ~/.config/systemd/user && cp deploy/mcmanager.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now mcmanager
sudo loginctl enable-linger $USER   # service et serveurs actifs sans session ouverte
```

Interface : `http://rp-01.local:8000`. Logs du service : `journalctl --user -u mcmanager -f`.

## Variables d'environnement (optionnelles)

`MCM_DEBUG=1`, `MCM_ALLOWED_HOSTS`, `MCM_CSRF_ORIGINS`, `MCM_IMAGE`, `MCM_DATA_ROOT`, `MCM_BACKUP_ROOT`.
