#!/usr/bin/env bash
# V2FC — installation complète en une commande, sur la VM relais (Linux x86-64).
#
#   sudo ./install.sh --vddk /chemin/VMware-vix-disklib-*.tar.gz --admin-email moi@exemple.com
#
# Enchaîne : VDDK -> Docker (install/install-docker.sh) -> diagnostic
# (install/check-prereqs.sh) -> secrets (install/setup-env.sh) -> accès réseau
# dans v2fc/.env -> build et démarrage (install/start-webapp.sh, runner compris)
# -> premier compte administrateur. Peut être relancé : chaque étape déjà faite
# est conservée (.env existant, Docker présent, compte déjà créé).
#
# Options :
#   --vddk CHEMIN        dossier vmware-vix-disklib-distrib/ ou archive
#                        VMware-vix-disklib-*.tar.gz (sous licence Broadcom,
#                        non fourni). Inutile s'il est déjà à la racine du dépôt.
#   --host ADRESSE       IP ou nom par lequel l'interface sera ouverte
#                        (défaut : première IP du serveur). « localhost » :
#                        interface accessible depuis ce serveur seulement.
#   --admin-email EMAIL  crée le premier administrateur (mot de passe demandé
#                        au clavier, jamais en argument).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2FC="$ROOT/v2fc"
ENV_FILE="$V2FC/.env"
VDDK_DIR="$ROOT/vmware-vix-disklib-distrib"

say()  { echo; echo "=== $* ==="; }
warn() { echo "[WARN] $*" >&2; }
die()  { echo "[ERREUR] $*" >&2; exit 1; }

usage() { awk 'NR > 1 && !/^#/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0"; exit "${1:-0}"; }

VDDK_SRC="" HOST="" ADMIN_EMAIL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --vddk)        VDDK_SRC="${2:?--vddk demande un chemin}"; shift 2;;
    --host)        HOST="${2:?--host demande une adresse}"; shift 2;;
    --admin-email) ADMIN_EMAIL="${2:?--admin-email demande une adresse e-mail}"; shift 2;;
    -h|--help)     usage 0;;
    *)             echo "option inconnue : $1" >&2; usage 1;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "à lancer en root : sudo $0 $*"
[ -f "$V2FC/docker-compose.yml" ] || die "v2fc/docker-compose.yml introuvable : lancer ce script depuis le dépôt cloné"

# --- 1. VDDK -----------------------------------------------------------------
say "1/6 VDDK (lecture des disques VMware)"
if [ -d "$VDDK_DIR/lib64" ]; then
  echo "déjà en place : $VDDK_DIR"
elif [ -n "$VDDK_SRC" ] && [ -d "$VDDK_SRC/lib64" ]; then
  cp -r "$VDDK_SRC" "$VDDK_DIR"
  echo "copié depuis $VDDK_SRC"
elif [ -n "$VDDK_SRC" ] && [ -f "$VDDK_SRC" ]; then
  tar -xzf "$VDDK_SRC" -C "$ROOT"
  [ -d "$VDDK_DIR/lib64" ] || die "l'archive $VDDK_SRC ne contient pas vmware-vix-disklib-distrib/lib64"
  echo "décompressé depuis $VDDK_SRC"
elif [ -n "$VDDK_SRC" ]; then
  die "VDDK introuvable : $VDDK_SRC"
else
  warn "aucun VDDK (--vddk) : l'interface s'installera, mais pas le runner — aucune copie possible. Relancer avec --vddk pour l'ajouter."
fi

# --- 2. Docker ---------------------------------------------------------------
say "2/6 Docker"
"$ROOT/install/install-docker.sh"

# --- 3. Diagnostic -------------------------------------------------------------
say "3/6 Diagnostic des prérequis"
if ! "$ROOT/install/check-prereqs.sh"; then
  [ -d "$VDDK_DIR/lib64" ] && die "prérequis manquants, voir ci-dessus"
  warn "seul le VDDK manque : installation de l'interface sans runner"
fi

# --- 4. Secrets et accès réseau ----------------------------------------------
say "4/6 Configuration (v2fc/.env)"
"$ROOT/install/setup-env.sh"

if [ -z "$HOST" ]; then
  HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  HOST="${HOST:-localhost}"
fi
set_env() {  # set_env CLE VALEUR : remplace la ligne ou l'ajoute
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}
if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then
  set_env CORS_ORIGINS "http://localhost:5173"
  set_env V2FC_WEB_BIND "127.0.0.1"
  echo "interface accessible depuis ce serveur uniquement"
else
  set_env CORS_ORIGINS "http://$HOST:5173"
  set_env V2FC_WEB_BIND "0.0.0.0"
  echo "interface accessible sur http://$HOST:5173"
fi
chmod 600 "$ENV_FILE"

# --- 5. Build et démarrage -----------------------------------------------------
say "5/6 Build et démarrage (peut prendre plusieurs minutes la première fois)"
"$ROOT/install/start-webapp.sh"

cd "$V2FC"
echo -n "attente de l'API "
for _ in $(seq 1 60); do
  curl -fsS http://localhost:8000/api/v1/health >/dev/null 2>&1 && break
  echo -n "."; sleep 2
done
echo
curl -fsS http://localhost:8000/api/v1/health >/dev/null 2>&1 \
  || die "l'API ne répond pas : docker compose -f $V2FC/docker-compose.yml logs api"

# --- 6. Premier administrateur -------------------------------------------------
say "6/6 Compte administrateur"
users=$(docker compose exec -T postgres sh -c \
  'psql -At -U "${POSTGRES_USER:-v2fc}" -d "${POSTGRES_DB:-v2fc}" -c "select count(*) from users;"' \
  2>/dev/null | tr -d '[:space:]' || echo "?")
if [ "$users" != "0" ] && [ "$users" != "?" ]; then
  echo "des comptes existent déjà ($users) — étape sautée"
elif [ -n "$ADMIN_EMAIL" ] && [ -t 0 ]; then
  docker compose exec api python3 -m api.seed_admin --email "$ADMIN_EMAIL"
else
  echo "à lancer pour créer le premier administrateur (mot de passe demandé) :"
  echo "  cd $V2FC && docker compose exec api python3 -m api.seed_admin --email VOTRE_EMAIL"
fi

say "Installation terminée"
url="http://$HOST:5173"; [ "$HOST" = "127.0.0.1" ] && url="http://localhost:5173"
echo "Interface : $url"
if [ -d "$VDDK_DIR/lib64" ]; then
  echo "Runner    : démarré — il enregistre cette VM comme relais dès que l'environnement"
  echo "            FusionCompute est créé dans l'interface (10 min au plus)."
else
  echo "Runner    : NON démarré (pas de VDDK) — relancer : sudo $0 --vddk <archive>"
fi
echo "Ensuite   : créer les environnements vCenter et FusionCompute (docs/GUIDE-UTILISATEUR.md §3)."
