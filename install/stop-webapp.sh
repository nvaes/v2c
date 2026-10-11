#!/usr/bin/env bash
# V2FC — arrête le socle web. Par défaut garde les données Postgres/Redis.
# Usage : install/stop-webapp.sh [--wipe]
#   --wipe  supprime aussi les volumes (tous les comptes/environnements/plans
#            créés sont perdus) — équivaut à `docker compose down -v`,
#            demande une confirmation explicite avant d'agir.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2FC="$(cd "$HERE/../v2fc" && pwd)"
cd "$V2FC"

if [ "${1:-}" = "--wipe" ]; then
  read -r -p "Ceci supprime aussi les données Postgres/Redis (comptes, environnements, plans). Confirmer ? (o/N) " reply
  if [ "$reply" = "o" ] || [ "$reply" = "O" ]; then
    docker compose down -v
    echo "Stack arrêtée, volumes supprimés."
  else
    echo "Annulé."
  fi
else
  docker compose down
  echo "Stack arrêtée, données conservées. (--wipe pour tout supprimer)"
fi
