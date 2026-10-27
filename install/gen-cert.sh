#!/usr/bin/env bash
# V2FC — certificat TLS auto-signé de l'interface web (HTTPS sur le port 5173).
# Écrit v2fc/certs/v2fc.crt et v2fc.key (jamais commités), monté en lecture
# seule dans le conteneur web. N'écrase pas un certificat existant, sauf
# avec --force (par ex. après un changement d'adresse IP du serveur).
#
#   install/gen-cert.sh [--force] [nom-ou-ip ...]
#
# Sans argument : nom de la machine + première adresse IP. localhost et
# 127.0.0.1 sont toujours inclus. Pour utiliser un certificat signé par une
# autorité interne, déposer simplement v2fc.crt / v2fc.key dans v2fc/certs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS="$(cd "$HERE/../v2fc" && pwd)/certs"
FORCE=0
[ "${1:-}" = "--force" ] && { FORCE=1; shift; }

if [ -f "$CERTS/v2fc.crt" ] && [ -f "$CERTS/v2fc.key" ] && [ "$FORCE" = 0 ]; then
  echo "certificat déjà présent ($CERTS/v2fc.crt) — rien à faire (--force pour le refaire)."
  exit 0
fi
command -v openssl >/dev/null || { echo "openssl introuvable" >&2; exit 1; }

names=("$@")
if [ ${#names[@]} -eq 0 ]; then
  names=("$(hostname)" "$(hostname -I 2>/dev/null | awk '{print $1}')")
fi
names+=(localhost 127.0.0.1)

san=""
seen=" "
for n in "${names[@]}"; do
  [ -z "$n" ] && continue
  case "$seen" in *" $n "*) continue ;; esac
  seen+="$n "
  if [[ "$n" =~ ^[0-9.]+$ || "$n" == *:* ]]; then san+="IP:$n,"; else san+="DNS:$n,"; fi
done
san="${san%,}"

mkdir -p "$CERTS"
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
  -keyout "$CERTS/v2fc.key" -out "$CERTS/v2fc.crt" \
  -subj "/CN=${names[0]}/O=V2FC" \
  -addext "subjectAltName=$san" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" 2>/dev/null
# Lisible par l'utilisateur node du conteneur web (volume en lecture seule).
chmod 644 "$CERTS/v2fc.crt" "$CERTS/v2fc.key"
echo "certificat auto-signé créé : $CERTS/v2fc.crt ($san, 10 ans)"
