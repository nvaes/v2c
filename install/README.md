# V2FC — Installation

Ce dossier regroupe l'installation de la solution complète : le socle web
(API + interface + Postgres + Redis) et le Data Mover (VDDK). Il consolide
des procédures déjà validées sur le terrain — voir `HANDOFF.md` §11 pour le
socle web (validé le 16/09/2026) et `v2fc/README.md` pour le Data Mover
(validé le 15-17/09/2026, y compris contre un vCenter réel) — sans en
changer le contenu. En cas de divergence entre ce dossier et l'un de ces
deux documents, **HANDOFF.md et v2fc/README.md font foi** : le signaler
pour que ce dossier soit corrigé.

**À maintenir à jour jusqu'à la livraison finale du projet** : toute
modification du socle web (`v2fc/docker-compose.yml`, `.env.example`,
Dockerfiles) ou de la CLI du Data Mover qui changerait une commande
ci-dessous doit être répercutée ici.

## Vue d'ensemble

Deux composants distincts, livrés et installés séparément :

1. **Socle web** — API FastAPI + interface React + Postgres + Redis,
   orchestrés par `v2fc/docker-compose.yml`. Tourne en continu.
2. **Data Mover** — image Docker autonome (`v2fc/Dockerfile`) qui embarque
   le driver VDDK. Ne tourne **pas** en continu : lancée à la demande,
   pour une migration, via la CLI `tools/v2fc_source.py`. Nécessite le VDDK
   VMware, qui n'existe qu'en x86-64 (Rosetta sur Apple Silicon).

Le VDDK est sous licence VMware et sa redistribution est interdite : il
n'est **pas** inclus dans cette livraison. Il doit être obtenu séparément
(portail Broadcom/VMware) et placé à la racine du projet, à côté de
`v2fc/` — voir §Data Mover ci-dessous.

## Prérequis

Lancer d'abord, à tout moment, pour un diagnostic complet sans rien
modifier :

```bash
./install/check-prereqs.sh
```

- Docker Desktop installé et lancé (les deux composants en dépendent).
- Pour le Data Mover uniquement : la distribution VDDK décompressée
  (`vmware-vix-disklib-distrib/`) à la racine du projet, à côté de `v2fc/`.
  Sur Apple Silicon, aucune installation native possible : tout passe par
  Docker `--platform linux/amd64` (Rosetta) — pas une alternative, une
  obligation (VMware Fusion sur Apple Silicon ne sait pas exécuter de VM
  x86-64).

## Installer et démarrer le socle web

```bash
./install/setup-env.sh      # crée v2fc/.env et génère JWT_SECRET / V2FC_KEK
./install/start-webapp.sh   # build + démarre postgres, redis, api, web
```

`start-webapp.sh` n'crée PAS le premier compte automatiquement : la
commande à lancer soi-même s'affiche à la fin (mot de passe demandé de
façon interactive, jamais en argument — voir `api/seed_admin.py`).

Une fois démarré : interface sur `http://localhost:5173`, API sur
`http://localhost:8000` (`/api/v1/health` pour vérifier).

Pour arrêter :

```bash
./install/stop-webapp.sh          # garde les données (comptes, environnements, plans)
./install/stop-webapp.sh --wipe   # supprime tout, demande confirmation
```

Dépannage détaillé (ports déjà utilisés, API qui ne démarre pas au premier
essai, `.env` non trouvé, etc.) : `HANDOFF.md` §11, section « Dépannage ».

## Installer le Data Mover (VDDK)

1. Récupérer l'archive VDDK Linux x86-64 correspondant à la version
   FusionCompute/vSphere ciblée (portail Broadcom/VMware) et la
   décompresser à la racine du projet :

   ```bash
   tar xzf VMware-vix-disklib-<version>.x86_64.tar.gz
   ```

   Ceci doit produire `vmware-vix-disklib-distrib/` à côté de `v2fc/`.

2. Construire l'image :

   ```bash
   ./install/build-datamover.sh
   ```

   Vérifie la présence du VDDK, puis construit `v2fc-datamover` avec
   `--platform linux/amd64` (obligatoire, y compris sur Mac Intel : c'est
   le drapeau de build qui fixe l'architecture, pas la machine hôte).

3. Utilisation : voir `v2fc/README.md`, section « Sur une plateforme
   réelle », pour les commandes `inventory` / `preflight` / `provision` /
   `sync` (rounds `seed` et `delta`) contre un vCenter ou ESXi réel — ce
   dossier ne les duplique pas pour éviter qu'elles divergent.

## Déploiement complet sur une VM à l'intérieur du cluster FusionCompute (relais colocalisé)

**Décision d'architecture du 17/09/2026 (ARCHITECTURE.md §3/§7.1, D7 au
§12) : le relais n'est pas un agent réseau séparé, c'est la VM qui
exécute l'application elle-même, à condition qu'elle vive dans le cluster
FusionCompute cible.** Un volume FusionCompute ne peut être attaché qu'à
une VM du même cluster (contrainte de l'hyperviseur) — si cette VM est
aussi celle qui fait tourner le Data Mover, lecture (VDDK) et écriture se
font dans le même processus, sans protocole réseau à construire.

Ce qui suit est la procédure complète pour ce cas de figure : socle web
(API/web/Postgres/Redis) **et** Data Mover sur la même VM. L'installation
de l'OS reste à la charge de Nicolas (Ubuntu Server LTS x86-64, un noyau
récent — glibc ≥ 2.16 exigée par le VDDK, D1) ; ce qui suit démarre une
fois l'OS prêt et accessible en SSH.

### 1. Réseau — à valider avant tout le reste

Cette VM a besoin, en sortie, d'un accès routé vers **deux** environnements
distincts (contrairement au poste de développement, qui n'en touchait
qu'un à la fois) :

- le vCenter/ESXi **source** (VDDK — NBD/NBDSSL/hotadd selon le transport
  choisi, voir `v2fc/README.md` et ARCHITECTURE.md §6.4) ;
- le VRM FusionCompute **cible** (API REST, port 7443 — ARCHITECTURE.md §7.5).

Et, en entrée, un accès pour toi depuis ton poste habituel vers cette VM,
au minimum les ports 8000 (API) et 5173 (interface web) — directement,
via VPN, ou via un reverse proxy, selon ce que permet le réseau du cluster.

### 2. Docker

Ubuntu ne fournit pas Docker par défaut. Dépôt officiel Docker (recommandé
plutôt que le paquet `docker.io` d'Ubuntu, pour avoir le plugin
`docker compose` v2) :

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # puis se reconnecter (ou `newgrp docker`)
docker compose version            # doit afficher la v2
```

### 3. Copier le projet sur la VM

Le projet est versionné avec git : cloner le dépôt sur la VM (les
artefacts de build, `.env`, le VDDK et les données de lab sont exclus par
`.gitignore`) :

```bash
# Sur la VM :
git clone git@github.com:nvaes/v2c.git ~/v2fc-deploy
```

`install/` et `v2fc/` restent ainsi côte à côte, comme le supposent tous
les scripts de ce dossier.

Le VDDK (licence VMware, pas dans ce dépôt) se copie séparément, à la
racine du même dossier que `v2fc/` sur la VM — voir §Data Mover plus haut,
même procédure, juste sur la VM plutôt que sur ton Mac :

```bash
rsync -av VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz nicolas@<ip-vm>:~/v2fc-deploy/
# puis, sur la VM :
cd ~/v2fc-deploy && tar xzf VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
```

### 4. Prérequis, secrets, build

Sur la VM, dans `~/v2fc-deploy` :

```bash
./install/check-prereqs.sh
```

Avant de lancer `setup-env.sh`, une différence importante avec ton Mac :
**`CORS_ORIGINS`** dans `v2fc/.env` doit correspondre à l'origine depuis
laquelle tu ouvriras vraiment l'interface (pas `http://localhost:5173`,
qui ne sera plus le bon si tu accèdes par l'IP de la VM ou un nom
d'hôte) — sans ça, le navigateur bloquera les appels à l'API en CORS et la
connexion échouera silencieusement. Éditer `v2fc/.env` après
`setup-env.sh` pour corriger cette ligne.

```bash
./install/setup-env.sh
# puis éditer v2fc/.env : CORS_ORIGINS=http://<ip-ou-nom-de-la-vm>:5173
#                         V2FC_WEB_BIND=0.0.0.0   (interface accessible depuis ton poste)
./install/build-datamover.sh   # x86-64 natif ici, pas de Rosetta : plus rapide que sur le Mac
./install/start-webapp.sh
docker compose exec api python3 -m api.seed_admin --email TON_EMAIL   # mot de passe demandé interactivement
```

Vérifier : `curl http://localhost:8000/api/v1/health`, puis ouvrir
`http://<ip-de-la-vm>:5173` depuis ton poste.

**Cette VM démarre avec une base vide** : les environnements
(vCenter/FusionCompute), identifiants et plans déjà créés sur ton Mac ne
sont pas repris automatiquement. Si tu veux les conserver plutôt que les
ressaisir, dis-le : ça se fait par `pg_dump`/`pg_restore` du volume
Postgres de ton Mac vers celui de la VM, mais ce n'est pas fait par
défaut pour ne pas mélanger silencieusement deux bases.

### 5. Compte FusionCompute de l'application

L'application se connecte au VRM (port 7443) avec un compte dédié de type
**« interconnexion d'interface »**. Un tel compte ne peut pas ouvrir le
portail web, donc il ne peut pas changer lui-même son mot de passe. Si le
VRM impose le changement du mot de passe initial, le compte se connecte
mais chaque appel d'API est refusé avec l'erreur `10000041` (« pour la
sécurité du compte, veuillez modifier le mot de passe »), et l'application
reste bloquée.

**Avant de créer le compte**, connecté au portail
(`https://<ip-vrm>:8443`) avec `admin` :

1. **Système → Gestion des droits → Politique de mot de passe** (ou
   « Politique de gestion des droits », selon la version) : désactiver
   « Le mot de passe initial doit être modifié à la première connexion » /
   « Forcer le changement du mot de passe après réinitialisation ».
   Si l'option existe, régler l'expiration sur « jamais » pour les comptes
   d'interconnexion : sinon le blocage revient à chaque expiration.
2. **Système → Gestion des droits → Gestion des utilisateurs → Créer** :

   | Champ | Valeur |
   |---|---|
   | Type | Utilisateur d'interconnexion d'interface |
   | Rôle | `administrator` (création de volumes et de VM) |
   | 2FA | désactivée |
   | Restriction d'IP | facultatif : l'IP de cette VM |

3. Dans l'interface V2FC, créer l'environnement FusionCompute avec ce
   compte, puis « Tester ». La liste des sites doit s'afficher.

Compte déjà créé et bloqué en `10000041` : appliquer l'étape 1, puis
réinitialiser son mot de passe depuis `admin`. L'interface V2FC affiche
alors ce diagnostic en clair au lieu du message chinois du VRM.

### 6. Relais : enregistrement automatique

Rien à saisir. Le service `runner` lit l'UUID matériel de cette VM
(`/sys/class/dmi/id/product_uuid`), que FusionCompute présente comme
`uuid` de la VM, et l'API la recherche dans chaque environnement
FusionCompute déclaré. Trouvée, elle est enregistrée comme relais
(statut ONLINE), au démarrage du runner puis toutes les 10 minutes : un
environnement FusionCompute ajouté après coup est donc pris en compte
sans redémarrage. Vérifier : `docker compose logs runner | grep relais`.

Une fois le relais enregistré, choisis-le à la création du job (onglet
Migration). Le service `runner` (démarré par `start-webapp.sh` dès que le
VDDK est présent) exécute alors tout seul le seed après le provisioning, un
delta toutes les `V2FC_DELTA_INTERVAL_MIN` minutes (60 par défaut, dans
`v2fc/.env`) et le delta final après « Migrer maintenant ». Il écrit
directement sur les volumes FusionCompute attachés à cette VM
(`/dev/disk/by-id/virtio-<UUID du volume>`). Suivi :
`docker compose logs -f runner` et l'onglet Logs.

## Contenu de ce dossier

```
check-prereqs.sh     diagnostic (Docker, VDDK, .env) — ne modifie rien
setup-env.sh         crée v2fc/.env et génère les secrets
start-webapp.sh       build + démarre le socle web
stop-webapp.sh        arrête le socle web (--wipe pour tout supprimer)
build-datamover.sh    construit l'image Docker du Data Mover
```

## Ce que ce dossier ne couvre pas

- La bascule finale (création de la VM cible sur FusionCompute) : écrite
  mais pas encore validée en réel. Création de volumes, écriture et seed
  le sont (`HANDOFF.md` §20-22).
- Un déploiement de production (le service `web` sert le serveur de
  développement Vite, pas un build statique servi par nginx — cf.
  commentaire en tête de `v2fc/web/Dockerfile` : à revoir quand le projet
  sortira du stade lab).
- Un relais sur une autre VM que celle qui héberge le runner : le runner
  n'enregistre que sa propre VM ; un relais distant reste à déclarer par
  `POST /api/v1/relays`.
