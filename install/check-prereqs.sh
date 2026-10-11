#!/usr/bin/env bash
# V2FC — vérification des prérequis avant installation.
# Ne modifie rien, se contente de diagnostiquer. Voir install/README.md.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
V2FC="$ROOT/v2fc"

ok=0
warn=0
fail=0

pass() { echo "  [OK]   $1"; ok=$((ok+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }
soft() { echo "  [WARN] $1"; warn=$((warn+1)); }

echo "=== Socle web (API + web + Postgres + Redis) ==="

if [ -d "$V2FC" ] && [ -f "$V2FC/docker-compose.yml" ]; then
  pass "dossier v2fc/ trouvé ($V2FC)"
else
  bad "v2fc/docker-compose.yml introuvable — ce script doit rester dans install/ à côté de v2fc/"
fi

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    pass "Docker Desktop lancé et accessible"
  else
    bad "Docker installé mais injoignable — Docker Desktop n'est probablement pas démarré"
  fi
else
  bad "commande 'docker' introuvable — installer Docker Desktop"
fi

if docker compose version >/dev/null 2>&1; then
  pass "docker compose (plugin v2) disponible"
else
  bad "docker compose indisponible — nécessaire pour la stack web (HANDOFF.md §11)"
fi

if [ -f "$V2FC/.env" ]; then
  if grep -q '^JWT_SECRET=$' "$V2FC/.env" 2>/dev/null || grep -q '^V2FC_KEK=$' "$V2FC/.env" 2>/dev/null; then
    soft "v2fc/.env existe mais JWT_SECRET ou V2FC_KEK est encore vide — voir install/setup-env.sh"
  else
    pass "v2fc/.env présent et semble complété"
  fi
else
  soft "v2fc/.env absent — sera créé par install/setup-env.sh (copie de .env.example)"
fi

echo
echo "=== Data Mover (VDDK) ==="

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
  soft "architecture $ARCH (Apple Silicon) — le Data Mover doit être construit et lancé avec --platform linux/amd64 (Rosetta) ; voir v2fc/Dockerfile et v2fc/README.md"
else
  pass "architecture $ARCH"
fi

VDDK_ARCHIVE=$(ls "$ROOT"/VMware-vix-disklib-*.tar.gz 2>/dev/null | head -1 || true)
VDDK_DIR=$(ls -d "$ROOT"/vmware-vix-disklib-distrib 2>/dev/null | head -1 || true)

if [ -n "$VDDK_DIR" ] && [ -d "$VDDK_DIR/lib64" ]; then
  pass "VDDK déjà décompressé : $VDDK_DIR (lib64/ présent)"
elif [ -n "$VDDK_ARCHIVE" ]; then
  soft "archive VDDK trouvée ($VDDK_ARCHIVE) mais pas encore décompressée — voir install/README.md §Data Mover"
else
  bad "aucune distribution VDDK trouvée à la racine du projet — le VDDK est sous licence VMware, il n'est PAS fourni avec cette livraison : le télécharger depuis le portail VMware (Broadcom) et le placer ici avant de continuer"
fi

echo
echo "=== Résumé : $ok OK, $warn avertissement(s), $fail échec(s) ==="
if [ "$fail" -gt 0 ]; then
  echo "Corriger les échecs ci-dessus avant de lancer install/start-webapp.sh ou install/build-datamover.sh."
  exit 1
fi
exit 0
