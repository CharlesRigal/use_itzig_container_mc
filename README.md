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
- Sauvegardes à chaud (`save-off` / `save-all flush`) incrémentales : snapshots à liens durs, seuls
  les fichiers modifiés prennent de la place. Liste, création (annulée si rien n'a changé, sauf
  « Forcer »), restauration (l'état actuel est sauvegardé avant), suppression, téléchargement
  `.tar.gz` à la volée, planification par serveur (intervalle en heures) et rétention (N max).
- Modpacks CurseForge installés sans l'endpoint de recherche (manifest + exclusions par id).
- Éditeur des fichiers de config avec historique.

## Fonctionnement

- Les paramètres sont des variables d'environnement de l'image itzg, figées à la création du conteneur.
  Chaque démarrage **recrée** donc le conteneur avec les paramètres actuels ; le monde est dans
  `data/servers/<nom>` (monté sur `/data`) et n'est jamais touché.
- La whitelist de l'interface fait foi : `whitelist.json` est réécrit à chaque démarrage
  (`EXISTING_WHITELIST_FILE=SYNCHRONIZE`).
- `--userns=keep-id` : les fichiers du monde appartiennent à l'utilisateur, pas à un sous-uid.
- Sauvegardes dans `data/backups/<nom>/`. Les fichiers retéléchargeables (jars, `libraries`, `logs`) sont exclus.

## Installation

Prérequis : Linux avec systemd, podman (rootless), Python 3.10+ et `python3-venv`.
Sur Raspberry Pi OS / Debian : `sudo apt install podman python3-venv git`.

```bash
git clone https://github.com/CharlesRigal/use_itzig_container_mc.git mcmanager
cd mcmanager && ./install.sh
```

Le script vérifie les prérequis, demande le port (8000) et le dossier des mondes
(`/data/minecraft`), crée l'environnement Python, la base et le compte admin, installe le
service systemd utilisateur, active le « linger » (sudo, une seule fois) et télécharge
l'image Minecraft. Il peut être relancé sans risque.

| Commande | Effet |
|---|---|
| `./install.sh --update` | `git pull`, dépendances, migrations, redémarrage |
| `./install.sh --uninstall` | retire le service ; base, mondes et sauvegardes conservés |
| `./install.sh --port 8001 --data /chemin --name autre` | autre port, dossier ou nom de service (plusieurs instances) |
| `./install.sh --yes` | aucune question (valeurs par défaut ou celles de `.install.env`) |

Admin sans question : `MCM_ADMIN_USER=… MCM_ADMIN_PASSWORD=… ./install.sh --yes`.

Interface : `http://<machine>.local:8000`. Logs du service : `journalctl --user -u mcmanager -f`.

## Variables d'environnement (optionnelles)

`MCM_DEBUG=1`, `MCM_ALLOWED_HOSTS`, `MCM_CSRF_ORIGINS`, `MCM_IMAGE`, `MCM_DATA_ROOT`, `MCM_BACKUP_ROOT`.
