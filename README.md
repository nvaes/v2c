# V2FC — Migration VMware → FusionCompute

V2FC migre des VM VMware (vCenter/ESXi) vers Huawei FusionCompute : lecture
des disques source par le VDDK (CBT, seed + delta), écriture aux offsets
exacts côté cible, pause/reprise et contrôle d'intégrité, piloté par une
interface web.

Ce README couvre l'**installation**. La procédure détaillée et le
dépannage sont dans [`install/README.md`](install/README.md), la conception
dans [`ARCHITECTURE.md`](ARCHITECTURE.md) et l'historique/état du projet
dans [`HANDOFF.md`](HANDOFF.md).

## Composants

| Composant | Contenu | Lancement |
|---|---|---|
| **Socle web** | API FastAPI + interface React (Vite) + Postgres 16 + Redis 7 | `v2fc/docker-compose.yml`, tourne en continu |
| **Data Mover** | Driver VDDK (Python/ctypes), CLI `v2fc_source.py` | image Docker `v2fc-datamover`, lancée à la demande |

```
.
├── README.md            ← ce fichier
├── ARCHITECTURE.md      dossier d'architecture
├── HANDOFF.md           état du projet, procédures validées, dépannage (§11)
├── install/             scripts d'installation + README détaillé
│   ├── check-prereqs.sh     diagnostic, ne modifie rien
│   ├── setup-env.sh         crée v2fc/.env et génère les secrets
│   ├── start-webapp.sh      build + démarrage du socle web
│   ├── stop-webapp.sh       arrêt (--wipe pour supprimer les données)
│   └── build-datamover.sh   construit l'image du Data Mover
└── v2fc/
    ├── docker-compose.yml   socle web
    ├── Dockerfile           Data Mover (x86-64)
    ├── .env.example         modèle de configuration
    ├── api/                 API FastAPI + migrations Alembic
    ├── web/                 interface React
    ├── datamover/           drivers VDDK / vSphere / FusionCompute
    ├── relay/               écriture côté cible
    ├── tools/               CLI (v2fc_source.py, vddk_probe.py, lab_report.py)
    ├── lab/                 banc d'essai sans vCenter
    └── tests/               tests unitaires
```

## Non inclus dans le dépôt

- **Le VDDK VMware** — sous licence VMware, redistribution interdite.
  Le télécharger sur le portail Broadcom/VMware
  (`VMware-vix-disklib-<version>.x86_64.tar.gz`, testé avec 8.0.3-23950268).
- **`v2fc/.env`** — contient les secrets, généré par `install/setup-env.sh`.
- Les données de lab (images disque `.raw`, relevés JSON) et la
  documentation Huawei FusionCompute.

## Prérequis

- **Docker** avec le plugin `docker compose` v2 (Docker Desktop sur Mac,
  Docker Engine sur Linux : `curl -fsSL https://get.docker.com | sudo sh`).
- **Python 3** sur l'hôte (utilisé par `setup-env.sh` pour générer les
  secrets).
- **Pour le Data Mover uniquement** : la distribution VDDK Linux x86-64.
  Le VDDK n'existe qu'en x86-64 : sur Mac Apple Silicon, l'image est
  construite et lancée en `--platform linux/amd64` (Rosetta).
- Ports libres en local : `5432`, `6379`, `8000`, `5173`.

## Installation rapide

```bash
git clone git@github.com:nvaes/v2c.git v2fc-deploy
cd v2fc-deploy

# 1. Diagnostic (ne modifie rien)
./install/check-prereqs.sh

# 2. Configuration : crée v2fc/.env et génère JWT_SECRET / V2FC_KEK
./install/setup-env.sh
#    Si l'interface n'est pas ouverte via http://localhost:5173,
#    adapter CORS_ORIGINS dans v2fc/.env (ex. http://<ip-vm>:5173)

# 3. Socle web : build + démarrage de postgres, redis, api, web
./install/start-webapp.sh

# 4. Premier compte administrateur (mot de passe demandé interactivement)
cd v2fc && docker compose exec api python3 -m api.seed_admin --email <ton-email>
```

Vérification :

```bash
curl -s http://localhost:8000/api/v1/health
```

Interface : <http://localhost:5173> — API : <http://localhost:8000>.
Les migrations de base (Alembic) s'appliquent automatiquement au démarrage
de l'API.

### Data Mover (VDDK)

```bash
# Placer l'archive VDDK à la racine du dépôt, puis :
tar xzf VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz   # → vmware-vix-disklib-distrib/
./install/build-datamover.sh

# Contrôle d'environnement + tests (sans vCenter, sans risque)
docker run --rm --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" \
  v2fc-datamover
```

Utilisation contre un vCenter/ESXi réel (`inventory`, `preflight`,
`provision`, `sync`) : voir [`v2fc/README.md`](v2fc/README.md), section
« Sur une plateforme réelle ».

## Arrêt

```bash
./install/stop-webapp.sh          # conserve les données
./install/stop-webapp.sh --wipe   # supprime aussi Postgres/Redis (confirmation demandée)
```

## Mise à jour

```bash
git pull
./install/start-webapp.sh         # reconstruit les images api/web
./install/build-datamover.sh      # si le Data Mover a changé
```

Toujours reconstruire après un `git pull` : un conteneur `api` non
reconstruit sert l'ancien code (404 sur les nouvelles routes, cf.
`HANDOFF.md` §17).

## Déploiement sur une VM du cluster FusionCompute

Cas cible de production (relais colocalisé, décision D7) : socle web et
Data Mover sur la même VM Ubuntu Server LTS x86-64, à l'intérieur du
cluster FusionCompute. Procédure complète — réseau, Docker, enregistrement
du relais via l'API — dans [`install/README.md`](install/README.md),
section « Déploiement complet sur une VM à l'intérieur du cluster
FusionCompute ».

## Dépannage

Voir `HANDOFF.md` §11 « Dépannage » : port déjà utilisé, API qui échoue à
la première connexion Postgres (relancer `docker compose up -d api`),
`JWT_SECRET non défini`, interface qui ne joint pas l'API, etc.

## Tests

```bash
# Dans l'image du Data Mover (VDDK monté) :
docker run --rm --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" \
  v2fc-datamover python3 -m pytest /work/tests -q
```
