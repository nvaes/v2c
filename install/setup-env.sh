#!/usr/bin/env bash
# V2FC — prépare v2fc/.env (secrets JWT_SECRET / V2FC_KEK).
# Reprend exactement la procédure validée dans docs/dev/JOURNAL.md §11 point 1.
# N'écrase jamais un .env existant : relancer ce script sur une installation
# déjà configurée ne touche à rien.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2FC="$(cd "$HERE/../v2fc" && pwd)"
ENV_FILE="$V2FC/.env"

if [ -f "$ENV_FILE" ]; then
  echo "v2fc/.env existe déjà — rien à faire."
  echo "(pour repartir de zéro : supprimer le fichier puis relancer ce script)"
  exit 0
fi

cp "$V2FC/.env.example" "$ENV_FILE"
echo "v2fc/.env créé à partir de .env.example."

JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
V2FC_KEK=$(python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")
V2FC_RUNNER_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")

# .env.example laisse JWT_SECRET= et V2FC_KEK= vides sur leur propre ligne —
# on les remplace en place (macOS/BSD sed : -i '' requiert un argument vide).
if sed --version >/dev/null 2>&1; then
  sed -i "s|^JWT_SECRET=$|JWT_SECRET=${JWT_SECRET}|" "$ENV_FILE"
  sed -i "s|^V2FC_KEK=$|V2FC_KEK=${V2FC_KEK}|" "$ENV_FILE"
  sed -i "s|^V2FC_RUNNER_TOKEN=$|V2FC_RUNNER_TOKEN=${V2FC_RUNNER_TOKEN}|" "$ENV_FILE"
else
  sed -i '' "s|^JWT_SECRET=$|JWT_SECRET=${JWT_SECRET}|" "$ENV_FILE"
  sed -i '' "s|^V2FC_KEK=$|V2FC_KEK=${V2FC_KEK}|" "$ENV_FILE"
  sed -i '' "s|^V2FC_RUNNER_TOKEN=$|V2FC_RUNNER_TOKEN=${V2FC_RUNNER_TOKEN}|" "$ENV_FILE"
fi

echo "JWT_SECRET, V2FC_KEK et V2FC_RUNNER_TOKEN générés et écrits dans v2fc/.env."
echo "Les identifiants Postgres (POSTGRES_USER/PASSWORD/DB) et CORS_ORIGINS"
echo "restent à leur valeur par défaut — les modifier dans v2fc/.env si besoin."
