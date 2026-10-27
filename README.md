# V2FC — Migration VMware → FusionCompute

V2FC migre des VM VMware (vCenter/ESXi) vers Huawei FusionCompute : lecture
des disques source par le VDDK (CBT, seed + delta), écriture aux offsets
exacts côté cible, pause/reprise et contrôle d'intégrité, piloté par une
interface web.

**Utilisation : voir le [guide utilisateur](docs/GUIDE-UTILISATEUR.md).**

Ce README couvre l'**installation**. La procédure détaillée et le
dépannage sont dans [`install/README.md`](install/README.md), la conception
dans [`ARCHITECTURE.md`](ARCHITECTURE.md) et l'historique technique (pour la
maintenance) dans [`docs/dev/JOURNAL.md`](docs/dev/JOURNAL.md).

## Composants

| Composant | Contenu | Lancement |
|---|---|---|
| **Socle web** | API FastAPI + interface React (Vite) + Postgres 16 + Redis 7 | `v2fc/docker-compose.yml`, tourne en continu |
| **Runner** | Exécute les copies (seed, deltas, delta final) via le Data Mover, sur la VM relais | service `runner` de `v2fc/docker-compose.yml` |
| **Data Mover** | Driver VDDK (Python/ctypes), CLI `v2fc_source.py` | lancé par le runner (ou à la main, mode validation) |

```
.
├── README.md            ← ce fichier
├── ARCHITECTURE.md      dossier d'architecture
├── docs/
│   ├── GUIDE-UTILISATEUR.md guide d'utilisation
│   └── dev/JOURNAL.md       historique technique, constats sur VMware/FusionCompute réels
├── install.sh           installation complète en une commande
├── install/             scripts d'installation + README détaillé
│   ├── install-docker.sh    installe Docker + compose (Linux, selon la distribution)
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
    ├── tools/               CLI (v2fc_source.py, v2fc_runner.py, vddk_probe.py, lab_report.py)
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
  Docker Engine sur Linux : `sudo ./install/install-docker.sh`, qui l'installe
  selon la distribution).
- **Python 3** sur l'hôte (utilisé par `setup-env.sh` pour générer les
  secrets).
- **Pour le Data Mover uniquement** : la distribution VDDK Linux x86-64.
  Le VDDK n'existe qu'en x86-64 : sur Mac Apple Silicon, l'image est
  construite et lancée en `--platform linux/amd64` (Rosetta).
- Ports libres en local : `5432`, `6379`, `8000`, `5173`.

## Installation en une commande

Sur la VM relais (Linux x86-64, dans le cluster FusionCompute), en root :

```bash
git clone git@github.com:nvaes/v2c.git
cd v2c
sudo ./install.sh --vddk /chemin/VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz \
                  --admin-email vous@exemple.com
```

`install.sh` enchaîne : VDDK (dossier ou archive), Docker selon la
distribution (`install/install-docker.sh`), diagnostic, secrets
(`v2fc/.env`), accès réseau à l'interface (`--host`, défaut : première IP
du serveur), build et démarrage de tous les services (runner compris), puis
le premier administrateur (mot de passe demandé au clavier). Il peut être
relancé sans risque : ce qui est déjà fait est conservé. `./install.sh
--help` pour les options.

À la fin, l'adresse de l'interface s'affiche (`https://<ip>:5173`, certificat
auto-signé créé par `install/gen-cert.sh` : accepter l'avertissement du
navigateur à la première visite) : y créer
les environnements vCenter et FusionCompute (voir le
[guide utilisateur](docs/GUIDE-UTILISATEUR.md)) ; la VM relais
s'enregistre seule.

### Étape par étape

Les mêmes étapes, à la main :

```bash
sudo ./install/install-docker.sh   # Docker + compose selon la distribution
./install/check-prereqs.sh         # diagnostic, ne modifie rien
./install/setup-env.sh             # crée v2fc/.env et génère les secrets
#  accès depuis un autre poste : dans v2fc/.env,
#  CORS_ORIGINS=https://<ip-vm>:5173 et V2FC_WEB_BIND=0.0.0.0
./install/gen-cert.sh              # certificat HTTPS auto-signé (v2fc/certs)
./install/start-webapp.sh          # build + démarrage (runner si le VDDK est présent)
cd v2fc && docker compose exec api python3 -m api.seed_admin --email <votre-email>
```

Vérification :

```bash
curl -s http://localhost:8000/api/v1/health
```

Interface : <https://localhost:5173> — API : <http://localhost:8000> (locale
au serveur, l'interface y accède par son proxy `/api`).

HTTPS : l'interface est servie en HTTPS dès que `v2fc/certs/v2fc.crt` et
`v2fc.key` existent (sinon repli en HTTP, signalé dans `docker compose logs
web`). `install/gen-cert.sh --force [nom ip…]` refait le certificat auto-signé
(changement d'IP) ; un certificat d'une autorité interne se dépose sous les
mêmes noms. Redémarrer ensuite : `docker compose restart web`.
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
`docs/dev/JOURNAL.md` §17).

## Déploiement sur une VM du cluster FusionCompute

Cas cible de production (relais colocalisé, décision D7) : socle web et
Data Mover sur la même VM Ubuntu Server LTS x86-64, à l'intérieur du
cluster FusionCompute. Procédure complète — réseau, Docker, enregistrement
du relais via l'API — dans [`install/README.md`](install/README.md),
section « Déploiement complet sur une VM à l'intérieur du cluster
FusionCompute ».

## Dépannage

Voir `docs/dev/JOURNAL.md` §11 « Dépannage » : port déjà utilisé, API qui échoue à
la première connexion Postgres (relancer `docker compose up -d api`),
`JWT_SECRET non défini`, interface qui ne joint pas l'API, etc.

## Tests

```bash
# Dans l'image du Data Mover (VDDK monté) :
docker run --rm --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" \
  v2fc-datamover python3 -m pytest /work/tests -q
```
