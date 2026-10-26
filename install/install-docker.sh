#!/usr/bin/env bash
# V2FC — installe Docker Engine + plugin « docker compose » selon la
# distribution Linux, depuis le dépôt officiel Docker. Voir install/README.md.
#
#   sudo ./install/install-docker.sh
#
# Sans effet si Docker et docker compose v2 sont déjà installés (le service
# est seulement activé). Distributions prises en charge :
#   - Ubuntu, Debian et dérivées (Mint, Pop!_OS...)       apt
#   - RHEL, AlmaLinux, Rocky, Oracle Linux, CentOS Stream  dnf / yum
#   - Fedora                                               dnf
#   - SLES, openSUSE                                       zypper (paquets de la distribution)
set -euo pipefail

say()  { echo "==> $*"; }
warn() { echo "[WARN] $*" >&2; }
die()  { echo "[ERREUR] $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "à lancer en root : sudo $0"
[ -r /etc/os-release ] || die "/etc/os-release introuvable : distribution non reconnue"
# shellcheck disable=SC1091
. /etc/os-release

case "$(uname -m)" in
  x86_64|amd64) ;;
  *) warn "architecture $(uname -m) : le Data Mover (VDDK) n'existe qu'en x86-64 — la copie ne fonctionnera pas sur ce serveur" ;;
esac

has_systemd() { [ -d /run/systemd/system ]; }

start_docker() {
  if has_systemd; then
    systemctl enable --now docker >/dev/null
    say "service docker activé et démarré"
  else
    warn "systemd absent : démarrer le démon Docker manuellement (dockerd)"
  fi
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  say "Docker déjà installé : $(docker --version) / $(docker compose version)"
  start_docker
  exit 0
fi

# Famille de distribution : ID d'abord, puis ID_LIKE pour les dérivées.
family=""
for id in $ID ${ID_LIKE:-}; do
  case "$id" in
    ubuntu|debian)                          family="debian";;
    rhel|almalinux|rocky|ol|centos)         family="rhel";;
    fedora)                                 family="${family:-fedora}";;
    sles|opensuse*|suse)                    family="suse";;
  esac
  [ -n "$family" ] && break
done
[ -n "$family" ] || die "distribution « ${PRETTY_NAME:-$ID} » non prise en charge — installer Docker Engine et le plugin compose à la main : https://docs.docker.com/engine/install/"
say "distribution : ${PRETTY_NAME:-$ID} (famille $family)"

case "$family" in
  debian)
    # Dérivées d'Ubuntu (Mint...) : le dépôt Docker est celui d'Ubuntu, avec
    # le nom de code Ubuntu sous-jacent.
    if [ "$ID" = "debian" ] || { [ "$ID" != "ubuntu" ] && [ -z "${UBUNTU_CODENAME:-}" ]; }; then
      repo_os="debian"; codename="${VERSION_CODENAME:-}"
    else
      repo_os="ubuntu"; codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    fi
    [ -n "$codename" ] || die "nom de code de la distribution introuvable dans /etc/os-release"
    export DEBIAN_FRONTEND=noninteractive
    say "retrait des paquets en conflit (docker.io, podman-docker...) s'ils sont présents"
    apt-get remove -y docker.io docker-doc docker-compose docker-compose-v2 podman-docker \
      containerd runc >/dev/null 2>&1 || true
    apt-get update -q
    apt-get install -y -q ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$repo_os/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$repo_os $codename stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ;;

  rhel|fedora)
    pm=dnf; command -v dnf >/dev/null 2>&1 || pm=yum
    case "$ID" in
      fedora) repo_os="fedora";;
      centos) repo_os="centos";;
      *)      repo_os="rhel";;     # RHEL, AlmaLinux, Rocky, Oracle Linux
    esac
    # Fichier .repo posé directement : fonctionne avec dnf 4, dnf 5 et yum.
    say "ajout du dépôt https://download.docker.com/linux/$repo_os"
    curl -fsSL "https://download.docker.com/linux/$repo_os/docker-ce.repo" \
      -o /etc/yum.repos.d/docker-ce.repo
    # Podman/runc fournis par la distribution entrent en conflit avec
    # containerd.io : --allowerasing les remplace (dnf uniquement).
    extra=""; [ "$pm" = dnf ] && extra="--allowerasing"
    $pm install -y $extra docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ;;

  suse)
    # Docker ne publie pas de dépôt SUSE : paquets de la distribution.
    zypper --non-interactive refresh
    zypper --non-interactive install docker docker-compose
    ;;
esac

start_docker

# Utilisateur ayant lancé sudo : accès à docker sans sudo.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  usermod -aG docker "$SUDO_USER"
  say "$SUDO_USER ajouté au groupe docker (se reconnecter pour en profiter)"
fi

docker --version
docker compose version || die "plugin docker compose absent après installation"
if has_systemd && ! docker info >/dev/null 2>&1; then
  die "Docker installé mais le démon ne répond pas : journalctl -u docker"
fi
say "Docker prêt. Étape suivante : ./install/check-prereqs.sh"
