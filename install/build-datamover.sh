#!/usr/bin/env bash
# V2FC — construit l'image Docker du Data Mover (VDDK, x86-64 uniquement).
# Reprend exactement les commandes documentées en tête de v2fc/Dockerfile
# et dans v2fc/README.md. Ne télécharge ni n'installe le VDDK : sous
# licence VMware, il doit être fourni séparément (portail Broadcom/VMware)
# et décompressé à la racine du projet AVANT de lancer ce script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
V2FC="$ROOT/v2fc"

VDDK_DIR="$ROOT/vmware-vix-disklib-distrib"

if [ ! -d "$VDDK_DIR/lib64" ]; then
  archive=$(ls "$ROOT"/VMware-vix-disklib-*.tar.gz 2>/dev/null | head -1 || true)
  if [ -n "$archive" ]; then
    echo "VDDK non décompressé. Décompresser d'abord :"
    echo "  cd '$ROOT' && tar xzf '$(basename "$archive")'"
  else
    echo "Aucune distribution VDDK trouvée dans '$ROOT'."
    echo "Le VDDK est sous licence VMware et n'est PAS fourni avec cette"
    echo "livraison : le récupérer sur le portail Broadcom/VMware"
    echo "(VMware-vix-disklib-<version>.x86_64.tar.gz) puis le décompresser"
    echo "à la racine du projet ('$ROOT')."
  fi
  exit 1
fi

echo "VDDK trouvé : $VDDK_DIR"
echo
echo "=== construction de l'image v2fc-datamover (--platform linux/amd64) ==="
cd "$V2FC"
docker build --platform linux/amd64 -t v2fc-datamover .

echo
echo "Image construite. Contrôle d'environnement + suite de tests (POC hors"
echo "vCenter, sans risque) :"
echo "  docker run --rm --platform linux/amd64 \\"
echo "    -v \"$VDDK_DIR:/opt/vddk:ro\" \\"
echo "    v2fc-datamover"
echo
echo "Utilisation contre un vCenter/ESXi réel : voir v2fc/README.md"
echo "(section « Sur une plateforme réelle ») pour les commandes"
echo "inventory / preflight / provision / sync — même image, mêmes montages,"
echo "en ajoutant les variables V2FC_VC_HOST / V2FC_VC_USER / V2FC_VC_PASSWORD."
