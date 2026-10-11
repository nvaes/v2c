# Interface web V2FC — image de développement (serveur Vite en mode dev,
# pas un build de prod servi par nginx : cohérent avec le stade du projet,
# cf. Dockerfile racine). À revoir quand le produit sortira du stade lab.
FROM node:20-slim

WORKDIR /work

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

# Le proxy /api -> API (vite.config.ts) cible VITE_API_PROXY_TARGET,
# fixé par docker-compose.yml au nom du service ("api"), pas localhost.
EXPOSE 5173
CMD ["npm", "run", "dev"]
