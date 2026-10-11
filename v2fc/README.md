# V2FC — Data Mover, POC L1

Preuve du maillon critique de la migration VMware → FusionCompute :
lecture d'extents par le VDDK, planification depuis le CBT, écriture aux
offsets exacts côté cible, avec pause/reprise et contrôle d'intégrité.

La chaîne tourne **sans vCenter** : le banc d'essai utilise un VMDK local
ouvert par le transport `file` du VDDK, et un fichier creux comme volume
cible. Le jour où l'accès lab est disponible, seul le mode de connexion
change (`vddk.Connection.vsphere(...)` au lieu de `.local()`).

## Contenu

```
datamover/vddk.py      liaison ctypes du VDDK — SourceReader, lecture seule
datamover/vsphere.py   driver source : inventaire, CBT, snapshots, garde-fou I1
datamover/preflight.py contrôles avant migration (BLOCK / WARN / INFO)
datamover/extents.py   planificateur : CBT/blocs alloués → plan de tranches
relay/writer.py        agent relais : pwrite O_DIRECT, signature, thin
tools/vddk_probe.py    contrôle d'environnement (à lancer en premier)
tools/v2fc_source.py   CLI : inventory, preflight, provision, sync, cleanup
tools/lab_report.py    relevé de plateforme (cf. LAB.md)
lab/make_disk.py       fabrique le VMDK de test et sa référence
lab/test_e2e.py        POC : seed, delta, pause/reprise, garde-fous
tests/                 44 tests unitaires, sans vCenter
LAB.md                 mode opératoire du relevé sur ESXi réel
Dockerfile             image x86-64 pour poste Apple Silicon
```

## Prérequis

Hôte **x86-64** Linux, glibc ≥ 2.16, Python ≥ 3.10, et la distribution
VDDK Linux décompressée (`vmware-vix-disklib-distrib/`).

Le VDDK n'existe qu'en x86-64 : sur un Mac Apple Silicon, passer par Docker
avec `--platform linux/amd64` (Rosetta). VMware Fusion sur Apple Silicon ne
sait pas exécuter de VM x86-64.

## Lancer

```bash
# une seule fois : produit vmware-vix-disklib-distrib/
tar xzf VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz

export VDDK_DISTRIB="$PWD/vmware-vix-disklib-distrib"
export LD_LIBRARY_PATH="$VDDK_DISTRIB/lib64"     # obligatoire, cf. plus bas

python3 tools/vddk_probe.py "$VDDK_DISTRIB" --require nbdssl,hotadd,san
cd lab && python3 make_disk.py && python3 test_e2e.py
```

Par Docker :

```bash
docker build --platform linux/amd64 -t v2fc-datamover .
docker run --rm --platform linux/amd64 \
  -v "$PWD/../vmware-vix-disklib-distrib:/opt/vddk:ro" \
  v2fc-datamover
```

## Deux pièges qui coûtent cher

`LD_LIBRARY_PATH` doit contenir `<distrib>/lib64` **avant le démarrage du
processus**, et le chemin passé à `vddk.init()` doit être la **racine** de la
distribution, pas `lib64`. Si l'une des deux conditions manque, le plugin de
transport avancé ne se charge pas, `hotadd` et `san` disparaissent, et
`InitEx` renvoie quand même `VIX_OK` : la migration fonctionne, simplement
trois à dix fois plus lentement, sans aucun message. `vddk_probe.py` est là
pour ça — le câbler en test de démarrage plutôt que de le lancer à la main.

La modification de `os.environ["LD_LIBRARY_PATH"]` en cours d'exécution est
sans effet, et précharger les bibliothèques en `RTLD_GLOBAL` pour contourner
provoque un SIGSEGV. La variable vient du lanceur : `Environment=` systemd
ou `ENV` dans le Dockerfile.

## Ce que le POC démontre

| Contrôle | Portée |
|---|---|
| `QueryAllocatedBlocks` ne remonte que l'alloué | seed « used-blocks only », 1,4 % d'un disque de 2 Gio |
| Relecture bit-exacte | SHA-256 cible = SHA-256 référence |
| Zones creuses lues à zéro | pas de fausse allocation |
| CBT désaligné et en désordre | alignement par élargissement, fusion, découpage |
| Mise à zéro d'une région en delta | perforation (`PUNCH_HOLE`), le thin est préservé |
| Pause au milieu d'un round | reprise par curseur de tranche, résultat identique |
| Mauvais volume cible | refus par signature, hors zone de données |
| Corruption en transport | somme de contrôle par tranche |

## Sur une plateforme réelle

`tools/v2fc_source.py` branche le tout sur un vCenter. Il ne demande que des
variables d'environnement — le mot de passe n'est jamais accepté en argument,
il finirait dans l'historique du shell et dans `ps`.

```bash
export V2FC_VC_HOST=vcenter.exemple.be
export V2FC_VC_USER='v2fc@vsphere.local'
export V2FC_VC_PASSWORD='...'
export VDDK_DISTRIB=/opt/vddk
export LD_LIBRARY_PATH="$VDDK_DISTRIB/lib64"

python3 tools/v2fc_source.py inventory
python3 tools/v2fc_source.py preflight --name SRV-APP-01
python3 tools/v2fc_source.py provision --name SRV-APP-01 --target-dir /mnt/relay
python3 tools/v2fc_source.py sync  --name SRV-APP-01 --round seed  \
        --target-dir /mnt/relay --state ./srv-app-01.json
python3 tools/v2fc_source.py sync  --name SRV-APP-01 --round delta \
        --target-dir /mnt/relay --state ./srv-app-01.json
```

`preflight` sort en 1 s'il trouve un blocage, il est donc utilisable tel quel
dans une chaîne d'intégration. `sync` sort en 2 si le CBT a été invalidé :
l'état est alors réinitialisé et il faut relancer un `--round seed`, jamais
poursuivre en delta.

Le compte vCenter n'a besoin ni du droit de suppression ni de celui de
déplacement : voir la liste de privilèges au §13 du dossier d'architecture.
Le driver refuse de lui-même toute méthode destructrice, mais restreindre le
compte est la seconde barrière.

## Ce qu'il ne démontre pas encore

L'appel réel `QueryChangedDiskAreas` contre un vCenter, les transports
`hotadd`/`nbdssl` contre un ESXi, et l'appariement volume FusionCompute ↔
`/dev/sdX` — la décision **D5** du dossier d'architecture, à trancher par un
test sur plateforme. Le driver FusionCompute lui-même attend le
*FusionCompute Interface Reference* de la version cible (décision **D2**) :
aucun chemin d'endpoint n'a été inventé.
