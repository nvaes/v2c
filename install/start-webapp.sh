#!/usr/bin/env bash
# V2FC — démarre le socle web (Postgres, Redis, API, web).
# Reprend la procédure validée dans HANDOFF.md §11 (points 2 à 4), sans rien
# y ajouter. Ne crée PAS le premier compte : cette étape reste interactive
# et manuelle à dessein (api/seed_admin.py n'accepte jamais de mot de passe
# en argument) — ce script l'affiche en dernière ligne, à lancer soi-même.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2FC="$(cd "$HERE/../v2fc" && pwd)"
cd "$V2FC"

if [ ! -f .env ]; then
  echo "v2fc/.env absent — lancer d'abord install/setup-env.sh" >&2
  exit 1
fi

echo "=== docker compose build (api + web) ==="
docker compose build

echo
echo "=== démarrage postgres + redis, attente de l'état healthy ==="
docker compose up -d postgres redis

for i in $(seq 1 30); do
  docker compose ps
  unhealthy=$(docker compose ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -E '^(postgres|redis) ' | grep -vc healthy || true)
  if [ "${unhealthy:-1}" = "0" ]; then
    echo "postgres et redis sont healthy."
    break
  fi
  sleep 2
done

echo
echo "=== démarrage api + web ==="
docker compose up -d api web

echo
echo "Stack lancée. Vérifier l'API :"
echo "  curl -s http://localhost:8000/api/v1/health"
echo
echo "Si c'est le tout premier démarrage (pas encore de compte), créer le"
echo "premier administrateur — mot de passe demandé de façon interactive :"
echo "  docker compose exec api python3 -m api.seed_admin --email TON_EMAIL"
echo
echo "Interface web : http://localhost:5173"
echo "Dépannage : voir install/README.md ou HANDOFF.md §11."
