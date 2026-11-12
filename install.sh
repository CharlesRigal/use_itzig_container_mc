#!/usr/bin/env bash
# Installation, mise à jour et désinstallation de MC Manager.
#
#   ./install.sh                 installe (ou répare) : relançable sans risque
#   ./install.sh --update        git pull, dépendances, migrations, redémarrage
#   ./install.sh --uninstall     retire le service (mondes, sauvegardes et base conservés)
#
# Options : --port N (8000), --data DOSSIER (/data/minecraft), --name SERVICE (mcmanager),
#           --yes (aucune question, valeurs par défaut), --no-pull (ne télécharge pas l'image).
# Admin sans question : MCM_ADMIN_USER et MCM_ADMIN_PASSWORD dans l'environnement.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$APP_DIR/.install.env"
IMAGE="docker.io/itzg/minecraft-server:latest"
PY="$APP_DIR/.venv/bin/python"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;32m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mErreur : %s\033[0m\n' "$*" >&2; exit 1; }

# --- options ------------------------------------------------------------------------
MODE=install ASSUME_YES=0 PULL=1
PORT="" DATA="" NAME=""
[ -f "$CONF" ] && . "$CONF"  # réglages de l'installation précédente
while [ $# -gt 0 ]; do
  case "$1" in
    --update) MODE=update ;;
    --uninstall) MODE=uninstall ;;
    --port) PORT="$2"; shift ;;
    --data) DATA="$2"; shift ;;
    --name) NAME="$2"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-pull) PULL=0 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "option inconnue : $1 (voir --help)" ;;
  esac
  shift
done
[ -t 0 ] || ASSUME_YES=1  # pas de terminal : pas de questions

ask() {  # ask "Question" "défaut" -> réponse
  local answer=""
  if [ "$ASSUME_YES" = 0 ]; then read -r -p "$1 [$2] : " answer || true; fi
  echo "${answer:-$2}"
}

NAME="${NAME:-mcmanager}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"

# --- désinstallation ----------------------------------------------------------------
if [ "$MODE" = uninstall ]; then
  step "Arrêt et retrait du service $NAME"
  systemctl --user disable --now "$NAME" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  bold "Service retiré."
  echo "Conservés : $APP_DIR (base, clés), les mondes et les sauvegardes."
  # Seulement les serveurs de cette installation (une autre peut tourner à côté).
  ours=$([ -x "$PY" ] && cd "$APP_DIR" && "$PY" manage.py shell --verbosity 0 -c \
    "from servers.models import Server; print(*[s.container_name for s in Server.objects.all()])" 2>/dev/null || true)
  running=$(podman ps --filter label=mcmanager=1 --format '{{.Names}}' 2>/dev/null | grep -Fxf <(printf '%s\n' $ours) || true)
  if [ -n "$ours" ] && [ -n "$running" ]; then
    echo "Serveurs Minecraft encore démarrés (podman stop <nom> pour les arrêter) :"
    echo "$running" | sed 's/^/  - /'
  fi
  exit 0
fi

# --- prérequis ----------------------------------------------------------------------
step "Vérification des prérequis"
missing=()
command -v podman >/dev/null || missing+=(podman)
command -v python3 >/dev/null || missing+=(python3)
command -v systemctl >/dev/null || die "systemd est nécessaire (service utilisateur)."
if command -v python3 >/dev/null; then
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 ou plus requis ($(python3 --version))."
  python3 -c 'import ensurepip, venv' 2>/dev/null || missing+=(python3-venv)
fi
if [ ${#missing[@]} -gt 0 ]; then
  die "il manque : ${missing[*]}
  Installe-les avec :  sudo apt install ${missing[*]}"
fi
[ "$(id -u)" != 0 ] || die "lance ce script avec ton utilisateur, pas en root (podman rootless)."
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] \
  || die "podman ne fonctionne pas en rootless pour $USER (essaie : podman info)."
echo "podman $(podman --version | awk '{print $3}'), $(python3 --version), systemd : OK"

# --- mise à jour du code ------------------------------------------------------------
if [ "$MODE" = update ] && [ "${MCM_REEXEC:-}" != 1 ]; then
  [ -f "$CONF" ] || die "aucune installation trouvée ici : lance d'abord ./install.sh"
  step "Récupération de la dernière version"
  git -C "$APP_DIR" pull --ff-only
  # Relance avec le script qui vient peut-être de changer.
  exec env MCM_REEXEC=1 "$APP_DIR/install.sh" --update --yes $([ "$PULL" = 0 ] && echo --no-pull)
fi

# --- réglages -----------------------------------------------------------------------
FRESH=0
[ -f "$APP_DIR/db.sqlite3" ] || FRESH=1
if [ -z "$PORT" ]; then PORT=$(ask "Port de l'interface web" 8000); fi
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "port invalide : $PORT (1024-65535)"
if ! systemctl --user is-active --quiet "$NAME" && ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
  die "le port $PORT est déjà utilisé par un autre programme : choisis-en un autre avec --port"
fi
if [ -z "$DATA" ]; then
  default_data=/data/minecraft
  [ -w /data/minecraft ] || [ -w /data ] || default_data="$HOME/minecraft"
  DATA=$(ask "Dossier des mondes et sauvegardes" "$default_data")
fi
DATA="${DATA%/}"
mkdir -p "$DATA/servers" "$DATA/backups" 2>/dev/null \
  || die "impossible de créer $DATA. Donne-le à ton utilisateur : sudo mkdir -p $DATA && sudo chown $USER: $DATA"
printf 'PORT=%q\nDATA=%q\nNAME=%q\n' "$PORT" "$DATA" "$NAME" > "$CONF"

# --- application --------------------------------------------------------------------
step "Environnement Python"
[ -x "$PY" ] || python3 -m venv "$APP_DIR/.venv"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$APP_DIR/requirements.txt"

step "Base de données"
cd "$APP_DIR"
"$PY" manage.py migrate --noinput
"$PY" manage.py collectstatic --noinput --verbosity 0
if [ "$FRESH" = 1 ]; then
  MCM_DATA="$DATA" "$PY" manage.py shell --verbosity 0 -c "
import os
from servers.models import AppSettings
s = AppSettings.get()
s.data_root = os.path.join(os.environ['MCM_DATA'], 'servers')
s.backup_root = os.path.join(os.environ['MCM_DATA'], 'backups')
s.save()"
  echo "Mondes : $DATA/servers · sauvegardes : $DATA/backups (modifiable dans Paramètres)"
fi

has_admin=$("$PY" manage.py shell --verbosity 0 -c "from django.contrib.auth.models import User; print(int(User.objects.filter(is_superuser=True).exists()))" 2>/dev/null | tail -1)
if [ "$has_admin" != 1 ]; then
  step "Compte administrateur"
  if [ -n "${MCM_ADMIN_USER:-}" ] && [ -n "${MCM_ADMIN_PASSWORD:-}" ]; then
    DJANGO_SUPERUSER_PASSWORD="$MCM_ADMIN_PASSWORD" "$PY" manage.py createsuperuser \
      --noinput --username "$MCM_ADMIN_USER" --email ""
  elif [ -t 0 ]; then
    "$PY" manage.py createsuperuser --email ""
  else
    warn "Aucun admin créé (pas de terminal). Plus tard : cd $APP_DIR && .venv/bin/python manage.py createsuperuser"
  fi
fi

# --- service ------------------------------------------------------------------------
step "Service systemd ($NAME)"
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
# Généré par install.sh : relance ./install.sh pour le régénérer.
[Unit]
Description=MC Manager (Django) - gestion des serveurs Minecraft podman
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
# Un seul processus : les flux en direct (SSE) et le planificateur partagent sa mémoire.
ExecStart=$APP_DIR/.venv/bin/uvicorn mcmanager.asgi:application --host 0.0.0.0 --port $PORT --timeout-graceful-shutdown 5 --no-access-log
Restart=on-failure
# Les serveurs Minecraft démarrés depuis l'interface vivent dans le cgroup du service :
# au redémarrage, seul le serveur web doit être arrêté, pas les conteneurs.
KillMode=process

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable "$NAME" >/dev/null 2>&1
systemctl --user restart "$NAME"

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
  echo "Pour que le service et les serveurs tournent sans session ouverte (après un redémarrage),"
  echo "le « linger » doit être activé (une seule fois, demande le mot de passe sudo) :"
  sudo loginctl enable-linger "$USER" || warn "Non activé. Plus tard : sudo loginctl enable-linger $USER"
fi

# --- image Minecraft ----------------------------------------------------------------
if [ "$PULL" = 1 ] && [ "$MODE" = install ]; then
  step "Téléchargement de l'image Minecraft (premier démarrage plus rapide)"
  podman pull "$IMAGE" || warn "Échec du téléchargement : il se fera à la création du premier serveur."
fi

# --- vérification -------------------------------------------------------------------
step "Vérification"
for _ in $(seq 1 30); do
  code=$(python3 -c "import urllib.request,sys
try: print(urllib.request.urlopen('http://127.0.0.1:$PORT/login/', timeout=2).status)
except Exception as e: print(getattr(e, 'code', 0))" 2>/dev/null)
  [ "$code" = 200 ] && break
  sleep 1
done
if [ "$code" != 200 ]; then
  warn "L'interface ne répond pas sur le port $PORT. Logs : journalctl --user -u $NAME -n 50"
  exit 1
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
bold "MC Manager est prêt :"
echo "  http://$(hostname).local:$PORT"
[ -n "$ip" ] && echo "  http://$ip:$PORT"
echo
echo "Mise à jour : ./install.sh --update · logs : journalctl --user -u $NAME -f"
