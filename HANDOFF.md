# V2FC — Dossier de reprise de session

**À lire en premier par toute nouvelle session Claude reprenant ce projet.**

Ce document est autonome : il ne suppose ni accès à la conversation
précédente, ni mémoire, ni projet partagé. Tout ce qui compte est sur le
disque, dans `~/Downloads/APP/`.

| | |
|---|---|
| Projet | Application web de migration VMware vSphere → Huawei FusionCompute |
| Porteur | Nicolas, Nvaestech SRL (Belgique) — architecte solutions stockage/virtualisation/sauvegarde |
| Langue de travail | français, registre technique, interlocuteur expert |
| État au | 17/09/2026 |
| Avancement | **L1 (lecture source) validé deux fois sur plateforme réelle** : ESXi direct (15/09/2026, §5 quinquies) et **vCenter réel** (17/09/2026, §5 sexies — 172.16.0.245, cluster Labouptime, seed+delta réels, repli `moref` confirmé via vCenter). Écriture réelle vers FusionCompute non testée — voir la discordance signalée au §5 sexies sur le critère de sortie de L1 (`ARCHITECTURE.md` §14). Défaut trouvé et non corrigé : `remove_snapshot()` peut sauter silencieusement le nettoyage si la connexion est déjà rompue (§5 sexies). **L0b (socle web, partie source) clos** (16/09/2026, testé de bout en bout par Nicolas — §11). **D2 résolu** (17/09/2026, §12) : API REST FusionCompute documentée avec citation de source. **Driver cible FusionCompute (lecture) écrit le 17/09/2026** (§13) : connexion VRM, listage datastores/réseaux, exposé côté API et UI web — **non encore testé contre le lab réel** (aucun outil de cette session n'atteint 172.25.99.230, voir §10) ; c'est le test que Nicolas doit faire ensuite. Écriture (volumes, VM cible, copie) toujours non commencée. **Dossier `install/` ajouté le 17/09/2026** pour la livraison finale (packaging web + Data Mover/VDDK), à maintenir à jour — voir `install/README.md`. **Socle L2 (jobs, onglets Migration/Logs) construit le 17/09/2026** (§16), non exercé contre un relais réel (aucun relais en place). Bug remonté par Nicolas le 17/09/2026 soir sur le mapping cible + 404 Migration/Logs, diagnostiqué et corrigé le jour même (§17) : un vrai bug côté UI (mapping) corrigé dans le code, et un conteneur `api` à reconstruire côté Nicolas (`docker compose build api && docker compose up -d api`) pour les 404 — reconstruction qui a révélé un second vrai bug (`relay/` absent de `api/Dockerfile`, corrigé le jour même, voir §17). **Décision d'architecture prise le 17/09/2026 (nuit)** (§18) : le relais est une VM applicative colocalisée dans le cluster FusionCompute, pas un agent réseau séparé — ARCHITECTURE.md mis à jour (D7). |

---

## 1. Ce que l'application doit faire

Migrer des VM de VMware vSphere vers Huawei FusionCompute, en trois temps :

1. **Seed à chaud** — copie initiale pendant que la VM source tourne.
2. **Deltas à chaud** — une ou plusieurs resynchronisations incrémentales,
   avec possibilité de mettre en pause et de reprendre.
3. **Dernier delta à froid** — la VM source est éteinte, le dernier écart est
   transféré, la cible démarre.

Exigence structurante posée par Nicolas dès le départ : **la VM source n'est
jamais supprimée**, pour qu'un rollback consiste simplement à la rallumer.

---

## 2. Décisions actées

| Id | Sujet | Décision |
|---|---|---|
| — | Lecture VMware | **VDDK** (VMware Virtual Disk Development Kit), lié en `ctypes` depuis Python |
| — | Écriture FusionCompute | **VM relais + agent** : volumes cibles attachés en SCSI à une VM relais dans le cluster FC, agent écrivant par `pwrite()` aux offsets exacts. N'utilise que l'API REST VRM documentée. |
| — | Stack applicative | FastAPI + React + PostgreSQL/Redis |
| **D1** | Plateforme du Data Mover | **RÉSOLU** — VDDK Linux x86-64 validé, liaison `ctypes` fonctionnelle. Pas de composant Windows. |
| **D2** | Chemins de l'API REST FusionCompute | **OUVERT, BLOQUANT.** Nicolas a l'accès support Huawei et doit fournir le *FusionCompute Interface Reference* de la version cible. **Aucun chemin d'endpoint n'a été inventé, ne pas en inventer.** |
| **D3** | Version vSphere source | **RÉSOLU pour le labo** — ESXi 8.0.3 build 24022510, Enterprise Plus. Même version que le VDDK. À reposer pour chaque plateforme cliente. |
| **D4** | Version FusionCompute cible | Ouvert — conditionne la préparation invité et la limite de disques par VM. |
| **D5** | Appariement volume FC ↔ `/dev/sdX` dans le relais | Ouvert — à trancher en laboratoire FusionCompute. Détermine si l'attachement peut être parallèle ou doit rester séquentiel. |
| **D6** | Préparation invité | `virt-v2v --in-place` d'abord, logique propre seulement pour ce qu'il ne couvre pas. |

Les six points précis à extraire du document Huawei pour débloquer **D2** sont
listés au §12 de `ARCHITECTURE.md`.

---

## 3. Où sont les choses

```
~/Downloads/APP/
├── ARCHITECTURE.md                 ← dossier de conception, v0.6, ~56 Ko
│                                     LE document de référence. Tout y est.
├── HANDOFF.md                      ← ce fichier
├── v2fc/                           ← le code, ~4000 lignes
│   ├── README.md                   vue d'ensemble et démarrage
│   ├── LAB.md                      mode opératoire du relevé sur ESXi
│   ├── Dockerfile                  image linux/amd64
│   ├── datamover/
│   │   ├── vddk.py                 liaison ctypes du VDDK
│   │   ├── vsphere.py              driver source : inventaire, CBT, snapshots, SourceGuard
│   │   ├── preflight.py            contrôles avant migration
│   │   └── extents.py              planificateur CBT → plan de tranches
│   ├── relay/writer.py             agent relais : pwrite O_DIRECT, signature, thin
│   ├── tools/
│   │   ├── vddk_probe.py           contrôle d'environnement VDDK
│   │   ├── v2fc_source.py          CLI : inventory/preflight/provision/sync/cleanup
│   │   └── lab_report.py           relevé de plateforme
│   ├── tests/                      61 tests, sans vCenter
│   └── lab/                        banc d'essai hors ligne (VMDK local)
├── lab-out/                        rapports JSON du laboratoire
│   ├── probe.json                  relevé lecture seule
│   └── mesure.json                 cycle CBT + tentative VDDK
├── vmware-vix-disklib-distrib/     VDDK Linux décompressé (NE PAS REDISTRIBUER)
└── VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz
```

**Le VDDK est sous licence VMware et ne doit jamais être redistribué ni
embarqué dans une image.** Broadcom a retiré les téléchargements publics fin
août 2026 : cette archive est irremplaçable, à sauvegarder hors du poste.
Empreinte : `sha256 = aadcf8fcfa5fc0ca972283334a273aa49fff96fa2eaadb30b8cc0c71705c4754`

---

## 4. Ce qui fonctionne, prouvé

**POC du maillon critique** (`v2fc/lab/test_e2e.py`, hors vCenter, sur un VMDK
local ouvert par le transport `file`) — tous les contrôles passent :

| Contrôle | Résultat |
|---|---|
| Seed « used-blocks only » | 4 extents → 8 tranches, SHA-256 cible = référence, cible restée creuse |
| Delta avec CBT désaligné et en désordre | alignement par élargissement, fusion, découpage, SHA-256 conforme |
| Mise à zéro d'une région | perforation `PUNCH_HOLE`, thin préservé |
| Pause au milieu d'un round | reprise par curseur de tranche, résultat identique |
| Mauvais volume cible | refus par signature |
| Corruption en transport | somme de contrôle par tranche |

**61 tests unitaires** passent sans vCenter (`python3 -m pytest tests/ -q`).

**Sur l'ESXi réel** (172.25.100.91, ESXi 8.0.3, VM de test `ubuntu` 25 Gio EFI) :
inventaire, pré-vérifications, activation CBT, snapshot, `QueryChangedDiskAreas`
et empreinte de certificat fonctionnent. Mesures au §2.6 de `ARCHITECTURE.md`.

---

## 5. LE POINT DE BLOCAGE — RÉSOLU LE 15/09/2026 (voir §5 quater)

**Ce blocage est levé.** La cause et la correction sont au §5 quater :
en résumé, `VixDiskLib_Open` échouait non pas à cause du chemin du disque,
mais parce que le `vmxSpec` construit comme `<vmx>?dcPath=...&dsName=...`
est rejeté par le VDDK sur cette connexion directe à l'hôte (sans vCenter) ;
la forme `moref=<moRef de la VM>` fonctionne. Le texte original du
blocage est conservé ci-dessous pour l'historique.

`VixDiskLib_Open("[DS] ubuntu/ubuntu.vmdk")` échouait avec `VixError=0x1`
(`VIX_E_FAIL`, générique) sur l'ESXi de test, après une connexion `ConnectEx`
réussie avec `snapshotRef` et empreinte SHA-1 acceptée.

**Trois pistes, par ordre de probabilité, et l'outillage est déjà en place
pour les départager.** La dernière version de `tools/lab_report.py` ajoute :

1. **Un contrôle du port 902.** Le transport `nbd`/`nbdssl` n'utilise pas le
   port de l'API : il ouvre une session NFC sur le **902** de l'ESXi. Un 443
   joignable ne prouve rien. Depuis un conteneur Docker sur Mac, ce port peut
   être filtré — et le symptôme serait exactement ce `VIX_E_FAIL`.
   *C'est l'hypothèse principale.*

2. **Une échelle de stratégies d'ouverture.** On ne sait pas si le VDDK veut le
   chemin du disque figé dans le snapshot (`ubuntu.vmdk`) ou le chemin courant
   (`ubuntu-000001.vmdk`) quand on lui passe déjà un `snapshotRef`, et la
   documentation ne tranche pas pour une connexion directe à un hôte. L'outil
   essaie les deux et note laquelle passe.

3. **Le journal VDDK.** Les `VixError` sont souvent génériques ; la cause
   réelle n'existe que dans le journal interne de la bibliothèque. Un tampon
   circulaire le conserve et en affiche les dernières lignes à chaque échec.
   Vérifié hors ligne : `VixError=0x4` s'accompagne de
   `DiskLib error 25 (The system cannot find the file specified)`.

**Commande à relancer :**

```bash
cd ~/Downloads/APP
docker build --platform linux/amd64 -t v2fc-datamover v2fc/
docker run --rm -it --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" -v "$PWD/lab-out:/out" \
  -e V2FC_VC_HOST=172.25.100.91 -e V2FC_VC_USER=root v2fc-datamover \
  python3 /work/tools/lab_report.py --vm ubuntu --insecure \
          --transport nbdssl:nbd -o /out/mesure.json
```

Cette commande n'avait pas encore été exécutée avec ces trois ajouts au moment
de la bascule de compte. **C'est la toute première chose à faire.**

Si le 902 est injoignable : correction côté réseau, pas côté code. Un autre
hôte d'exécution (une VM Linux sur le même réseau que l'ESXi, plutôt qu'un
conteneur Docker sur le Mac) est alors la voie la plus simple.

### 5 bis. Suite au 15/09/2026 au soir — les trois diagnostics ont tranché

La commande ci-dessus a été relancée avec les trois ajouts. Résultat :

- **Port 902 : écarté.** Le relevé confirme les deux ports joignables
  (« port 443 (api) joignable », « port 902 (nfc) joignable »). Ce n'est pas
  un problème réseau.
- **Ambiguïté disque figé / disque courant : écartée.** Les deux stratégies
  échouent à l'identique (même `VixError=0x1`), que ce soit avec
  `ubuntu.vmdk` (chemin du snapshot) ou `ubuntu-000001.vmdk` (chemin
  courant). Ce n'est pas une question de *quel* nom de fichier.
- **Le journal VDDK, capturé pour la première fois, donne enfin la cause
  réelle** derrière le `VixError=0x1` générique :

  ```
  Error occurred when obtaining the file name for [DS] ubuntu/ubuntu.vmdk.
  Error 1 (Unknown error) (Invalid search specifier: ubuntu/
  ```

  Ce n'est pas un échec d'ouverture au sens strict : c'est un échec, en
  amont, de la **résolution** de la notation `[DS] dossier/fichier.vmdk` vers
  un emplacement réel. Le VDDK tente une recherche interne sur le datastore,
  et cette recherche rejette « ubuntu/ » (le dossier de la VM, avec le
  slash, sans nom de fichier) comme spécificateur invalide — de façon
  systématique pour les deux noms de fichiers essayés, ce qui pointe vers
  l'étape de résolution du dossier elle-même, pas vers le nom du disque.

**Recherche de confirmation officielle : sans résultat.** Tentative de
vérifier ce comportement contre la documentation VDDK (distinction
« managed disk » / « hosted disk », format de chemin attendu en connexion
directe à un hôte sans vCenter) — sans succès : les anciens fils VMTN
redirigent vers une page d'accueil Broadcom vide, les pages TechDocs
Broadcom qui semblaient pertinentes (« Datastore-Level Access to VMDK »,
« Managed Disk and Hosted Disk ») renvoient un 404, `developer.vmware.com`
redirige vers une page d'accueil sans contenu. Cohérent avec le §2.4 :
Broadcom a fermé l'accès public à cette documentation. **Aucune cause n'est
donc confirmée par une source officielle — ce qui suit est une hypothèse à
tester, pas un fait établi.**

**Hypothèse retenue et implémentée (à valider au prochain relevé) :** la
notation `[DS] dossier/fichier` a besoin, pour être résolue, d'une étape
interne au VDDK qui échoue en connexion directe à l'hôte (le WARN
`CONNEXION_HOTE` du relevé confirme l'absence de vCenter ici). Le chemin
VMFS absolu (`/vmfs/volumes/<uuid>/dossier/fichier.vmdk`) contourne cette
résolution. Ajouté :

- `SourceVm.absolute_disk_path()` dans `datamover/vsphere.py` — résout le
  datastore par son `vim.Datastore.info.url` réel (jamais deviné) et
  construit le chemin absolu.
- Une troisième candidate dans `_open_strategies()` de
  `tools/lab_report.py` : « chemin VMFS absolu (hypothèse §5) », essayée
  après les deux précédentes.

Vérification faite dans ce bac à sable : syntaxe compilée
(`python3 -m py_compile`) sur les deux fichiers, pas de test unitaire
existant couplé à `_open_strategies` ou `absolute_disk_path` (recherché dans
`tests/`, aucune occurrence). **La suite `pytest` complète n'a pas été
relancée ici** (pas de `pyVmomi` ni de Docker dans ce bac à sable) — à
relancer par Nicolas si souhaité, mais la validation qui compte est le
relevé réel sur l'ESXi, prochaine étape immédiate.

**Prochaine commande à lancer** (identique à celle du §5, elle réutilisera
automatiquement la nouvelle stratégie) :

```bash
cd ~/Downloads/APP
docker build --platform linux/amd64 -t v2fc-datamover v2fc/
docker run --rm -it --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" -v "$PWD/lab-out:/out" \
  -e V2FC_VC_HOST=172.25.100.91 -e V2FC_VC_USER=root v2fc-datamover \
  python3 /work/tools/lab_report.py --vm ubuntu --insecure \
          --transport nbdssl:nbd -o /out/mesure.json
```

Si le chemin VMFS absolu réussit : le point de blocage du §5 est levé, la
lecture VDDK complète peut être mesurée (débit, transport négocié) et L0a
peut être considéré comme définitivement prouvé sur plateforme réelle. S'il
échoue aussi, regarder en priorité si `ds.info.url` renvoyé n'est pas de la
forme attendue (auquel cas l'hypothèse elle-même tombe) avant d'en chercher
une autre.

**Correction du 15/09/2026, avant même le premier vrai test de l'hypothèse.**
Le premier relevé relancé avec ce code a révélé que `ds.info.url` sur cet
ESXi renvoie directement `/vmfs/volumes/<uuid>` — **sans** le préfixe
`ds://` que le code supposait. La regex de `absolute_disk_path()` rejetait
donc systématiquement cette URL, tombait dans la branche d'erreur, et la
troisième stratégie n'était jamais ajoutée à la liste (visible dans les
logs : « résolution du chemin VMFS absolu impossible ... hypothèse non
applicable telle quelle »). Autrement dit, **l'hypothèse elle-même n'a pas
encore été testée** — seul le code qui la met en œuvre a été corrigé.
Regex élargie pour accepter les deux formes
(`^(?:ds://)?(/vmfs/volumes/[^/]+)/?$`). La commande du §5/§5 bis est à
relancer une nouvelle fois pour avoir le premier vrai résultat.

### 5 ter. L'hypothèse du chemin de disque tombe — le vrai suspect est le `vmxSpec`

Troisième relance : les trois stratégies échouent, **identiquement**. Lu en
entier dans `lab-out/mesure.json` (le terminal tronque à 150 caractères, ce
qui avait fait lire à tort le §5 bis comme concernant le dossier du disque) :

```
Invalid search specifier: ubuntu/ubuntu.vmx?dcPath:ha-datacenter&dsName=DS
```

Ce texte est **identique dans les trois tentatives**, quel que soit le
`diskPath` demandé (`[DS] ubuntu/ubuntu.vmdk`, `[DS] ubuntu/ubuntu-000001.vmdk`,
`/vmfs/volumes/.../ubuntu/ubuntu.vmdk`). **Conclusion : l'hypothèse du §5 bis
est réfutée.** Le chemin du disque n'a jamais été le problème — la chaîne
invalide est le `vmxSpec` (ou quelque chose de dérivé de lui), utilisé à
chaque `Open()` pour une résolution interne, avant même de regarder le
disque demandé.

**Recherche effectuée, avec une source enfin exploitable.** Un fil
Broadcom Community actuel (pas un fil VMTN mort) traite d'un cas très
proche : *« VixDiskLib_Open fails in VDDK 7.0 using a vmPath in the
vmxSpec »*. Résumé :

- Le style ancien `vmxSpec = "vmPath:/Dossier/vm/NomVM"` a été **déprécié
  entre VDDK 6.7.3 et 7.0** — exactement le même message
  « Invalid search specifier: vmPath:… » avec un **deux-points**, ce qui
  confirme que la reformulation `=` → `:` dans notre propre journal est un
  style d'écho interne au VDDK, pas une preuve de corruption côté Python.
- Remplacement recommandé par un intervenant (`goppi`) : `moRef`, ou
  `<fichier.vmx>?dcPath=<datacenter>&dsName=<datastore>` — **avec
  `ha-datacenter` explicitement pour une connexion ESXi directe sans
  vCenter**. C'est exactement le format que `vmx_spec` construit déjà
  dans `datamover/vsphere.py`. Cette piste ne pointe donc pas un bug côté
  format — elle confirme au contraire que le nôtre suit la recommandation
  moderne.
- Le même fil, et indépendamment un connecteur VDDK tiers open source
  (`m-barthelemy/BackO`, `VMWare/VDDK/Vddk.cs`), montrent un **format
  alternatif** qui fonctionne dans leur cas : `vmxSpec = "moref=" + moRefDeLaVM`
  — sans passer par le chemin `.vmx` ni par `dcPath`/`dsName` du tout.

**Sources consultées :**
- [VixDiskLib_Open fails in VDDK 7.0 using a vmPath in the vmxSpec — Broadcom Community](https://community.broadcom.com/vmware-cloud-foundation/discussion/vixdisklib-open-fails-in-vddk-70-using-a-vmpath-in-the-vmxspec)
- [BackO/VMWare/VDDK/Vddk.cs — GitHub](https://github.com/m-barthelemy/BackO/blob/master/VMWare/VDDK/Vddk.cs)

**Implémenté et prêt à tester.** `tools/lab_report.py` essaie maintenant
deux formes de `vmxSpec`, une connexion VDDK par forme (`_vmx_spec_candidates()`) :
« vmx + dcPath/dsName » (l'existante) puis « moref » (`f"moref={vm.moref}"`).
Pour chaque forme : reconnexion complète (empreinte SHA-1/SHA-256), puis
tentative d'ouverture de chaque disque via la boucle `_open_strategies()`
déjà en place. On s'arrête à la première forme qui ouvre au moins un
disque ; sinon le rapport JSON garde trace de chaque tentative sous
`mesure.vmx_spec_trials`. Vérifié dans ce bac à sable : compilation Python
propre, pas de test unitaire couplé à ce code. **Pas testé contre l'ESXi
réel d'ici** — même limitation que d'habitude (pas de Docker, pas de route
réseau dans ce sandbox).

Commande à relancer, identique aux précédentes :

```bash
cd ~/Downloads/APP
docker build --platform linux/amd64 -t v2fc-datamover v2fc/
docker run --rm -it --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" -v "$PWD/lab-out:/out" \
  -e V2FC_VC_HOST=172.25.100.91 -e V2FC_VC_USER=root v2fc-datamover \
  python3 /work/tools/lab_report.py --vm ubuntu --insecure \
          --transport nbdssl:nbd -o /out/mesure.json
```

### 5 quater. Résolu : `moref=<moRef>` fonctionne, `<vmx>?dcPath=...&dsName=...` non

Relancée avec les deux formes de `vmxSpec` en concurrence. Résultat sans
ambiguïté (`lab-out/mesure.json`, section `mesure.vmx_spec_trials`) :

| Forme de vmxSpec | Valeur essayée | Résultat |
|---|---|---|
| `vmx + dcPath/dsName` | `ubuntu/ubuntu.vmx?dcPath=ha-datacenter&dsName=DS` | échec — même « Invalid search specifier » qu'aux §5/§5 bis/§5 ter, sur les trois `diskPath` essayés |
| `moref` | `moref=22003` (le `_moId` de la VM) | **réussite** |

Avec `moref=22003` : `Hard disk 1` ouvert via « disque du snapshot »
(`[DS] ubuntu/ubuntu.vmdk`), transport négocié **`nbdssl`** (pas de
dégradation silencieuse en `nbd`, cf. piège §2.2), **58,1 Mio/s** sur un
échantillon de 256 Mio, `disk_uuid = 60 00 C2 9c 59 d6 42 17-e7 c8 61 3c 22
41 ee 08`, `num_links = 1`.

**Ce qui est établi :** sur une connexion directe à un hôte ESXi (sans
vCenter), avec ce VDDK 8.0.3 et cet ESXi 8.0.3, le `vmxSpec` doit être
`moref=<moRef de la VM>` — la forme `<fichier.vmx>?dcPath=...&dsName=...`,
bien que présentée comme une alternative valide dans un fil Broadcom
Community (§5 ter), échoue systématiquement ici.

**Ce qui n'est PAS établi, à ne pas généraliser sans nouvel essai :** si
cela vaut aussi en connexion **via vCenter** (où un `moRef` de VM est
également valide, mais où la forme `dcPath/dsName` pourrait se comporter
différemment) ; si c'est spécifique à cette version d'ESXi/VDDK ou plus
général. `tools/lab_report.py` essaie donc toujours les deux formes et
consigne laquelle a fonctionné (`mesure.vmx_spec_accepted`), plutôt que de
supposer `moref` acquis pour toute plateforme cliente future.

**Décision à prendre avec Nicolas, pas encore tranchée :** `SourceVm.vmx_spec`
dans `datamover/vsphere.py` — utilisé par `tools/v2fc_source.py` et le
driver de production, pas seulement par l'outil de lab — construit encore
la forme `dcPath/dsName` qui vient de se révéler non fonctionnelle ici.
Reste à décider : basculer le driver de production sur `moref` par défaut,
essayer les deux comme le fait désormais `lab_report.py`, ou attendre un
test via vCenter avant de généraliser.

**L1 (POC du maillon critique) est donc atteint, y compris sur
plateforme réelle** — voir le tableau du §8.

---

### 5 quinquies. Cycle complet réussi avec le driver de production — L1 clos

Après le §5 quater (résolution du blocage sur `lab_report.py`), deux défauts
propres au driver de production (`tools/v2fc_source.py`, jamais exercé de
bout en bout jusqu'ici) sont apparus au premier vrai cycle, et ont été
corrigés dans la foulée :

1. **`conn.prepare_access("v2fc")` appelé sans condition.** Cette fonction ne
   concerne que le mode HotAdd (suspension temporaire de vMotion/DRS pendant
   l'attachement au proxy, §6.4 d'ARCHITECTURE.md). Appelée hors HotAdd sur
   une connexion directe à l'hôte, elle échoue avec
   `VixError=0x14 : « The command is not supported on remote objects »`.
   **Corrigé** : n'appeler `prepare_access`/`end_access` que si `hotadd` fait
   partie des transports demandés (`args.transport`). Le nettoyage du
   snapshot (`finally`) s'est exécuté correctement malgré ce plantage — I5
   tient.
2. **`Connection.vsphere_auto()` ne testait que la réussite de
   `VixDiskLib_ConnectEx`, jamais celle de `VixDiskLib_Open`.** Or
   `ConnectEx` réussit pour n'importe quelle forme de vmxSpec (§5 ter) : la
   sélection retombait donc toujours sur la première forme, jamais sur
   `moref`, et l'échec ne se révélait qu'au milieu du cycle d'écriture.
   **Corrigé** : `vsphere_auto()` accepte maintenant un paramètre `probe`
   (fonction appelée juste après connexion, qui doit lever si la forme ne
   convient pas). `cmd_sync` lui passe une sonde qui ouvre puis referme
   immédiatement le premier disque réel avant de se lancer dans le cycle.

**Fonctionnalité ajoutée à la demande de Nicolas** : une classe `_Progress`
dans `tools/v2fc_source.py` affiche une ligne de progression réactualisée en
place (`\r`) pendant le transfert — pourcentage, Mio faits/total, débit
instantané, tranche en cours, ETA — rafraîchie au plus toutes les 2 s.
Avant, un cycle de plusieurs minutes ne donnait aucun signe de vie.

**Résultat, avec les deux correctifs et la sonde en place** :

- **Seed** (round 1) : 25 Gio, 70 zones → 1466 tranches, 5 512 Mio écrits,
  312 Mio de zéros ignorés, **139 s à 40 Mio/s**. `changeId` enregistré :
  `.../77`.
- **Delta** (round 2, après écriture de ~100 Mio sur la VM) : 97 zones → 97
  tranches (14 Mio, 0,06 % du disque), **6 s**, `changeId` réenregistré :
  `.../81`.
- Dans les deux cas : sonde rejetant « vmx + dcPath/dsName » puis repli sur
  « moref », comme prévu ; snapshot créé puis supprimé proprement à chaque
  round.

**L1 est clos** : le driver source vSphere fonctionne de bout en bout sur
plateforme réelle, avec le CLI de production, pas seulement l'outil de
diagnostic. Reste ouvert, sans lien avec ce qui précède : `moref=` n'a
toujours été vérifié qu'en connexion directe à l'hôte, jamais via vCenter
(voir la mise en garde du §5 quater, toujours valable).

**Reprise prévue le 16/09/2026 sur FusionCompute** — voir §5 (D2) et §12.

### 5 sexies. L1 validé sur vCenter réel (17/09/2026) — comble le dernier doute du §5 quinquies

Le §5 quinquies avait clos L1 sur ESXi en connexion directe, mais laissait
un point ouvert : « `moref=` n'a toujours été vérifié qu'en connexion
directe à l'hôte, jamais via vCenter ». Nicolas a validé ce point ce jour,
en lançant le CLI de production (`tools/v2fc_source.py`, via Docker
`--platform linux/amd64` sur son Mac Apple Silicon) contre son vCenter réel
(172.16.0.245, VMware vCenter Server 8.0.3 build 25197330, cluster
« Labouptime », 228 VM).

**Premier essai, `todelete2` (2 disques, 80 + 50 Gio)** — `preflight`,
activation automatique du CBT, `provision`, puis `sync --round seed` :
CBT réel via `QueryAllocatedBlocks` (191 zones → 6988 tranches sur le
premier disque, 33,65 % de 80 Gio alloués), transport `nbdssl` négocié,
transfert commencé. Interrompu à 0,4 % (29/6988 tranches, 2,7 Mio/s) par
une coupure réseau réelle entre le conteneur Docker (Rosetta) et
172.16.0.245 (`SSL: Unknown SSL Error` puis `NFC_NETWORK_ERROR`, suivi
d'une connexion SOAP vCenter séparée qui est tombée aussi
`SHUTDOWN_WHILE_IN_INIT`, puis d'un `ConnectionRefusedError` TCP sur la
tentative suivante) — le réseau est revenu de lui-même quelques minutes
plus tard. Le fichier d'état n'avait jamais été écrit (`commit()` n'a lieu
qu'après un disque entier terminé, cf. `tools/v2fc_source.py`, classe
`State`) : reprendre en `--round seed` depuis zéro était donc sûr, sans
perte.

**Défaut réel trouvé pendant cet incident (non corrigé dans le code — à
faire) :** `datamover/vsphere.py`, `remove_snapshot()` :

```python
def remove_snapshot(self, snapshot, consolidate: bool = True) -> None:
    log.info("suppression du snapshot sur %s (consolidate=%s)",
             self.name, consolidate)          # <- accès self.name : appel SOAP live
    task = snapshot.RemoveSnapshot_Task(removeChildren=False,
                                        consolidate=consolidate)
    ...
```

Python évalue les arguments d'un appel avant d'exécuter la fonction :
`self.name` est un accès de propriété pyVmomi, donc un appel SOAP live.
Avec la connexion déjà rompue, cet accès lève une exception **avant même**
que `RemoveSnapshot_Task` soit invoqué — le nettoyage en `finally` échoue
silencieusement, sans jamais atteindre l'appel de suppression réel. C'est
ce qui a laissé le snapshot `v2fc-r0001` orphelin sur `todelete2`, retiré
ensuite à la main via `tools/v2fc_source.py cleanup --name todelete2`. Ce
défaut peut se reproduire dès que la connexion vCenter est déjà tombée au
moment du nettoyage — à corriger (par ex. capturer `self.name` dans une
variable avant la tentative de connexion, ou l'omettre du message en cas
d'échec) ; pas fait ce jour, Nicolas ne l'a pas encore demandé.

**Second essai, `test_ubuntu`** (1 disque, 4,0 Gio, EFI, `ubuntu64Guest`,
sous tension) — choisi pour un test à petit disque, dans un sous-dossier
dédié (`lab-vcenter-out/test_ubuntu/`) pour éviter que son
`device_key=2000` n'écrase le volume déjà provisionné de `todelete2` (même
dossier plat sinon, même risque de collision) :

- **Seed (round 1)** : CBT réel — 16 zones → 542 tranches (52,29 % de
  4 Gio alloués). `cycle 1 (seed) terminé en 335 s — 2 142 Mio écrits,
  0 Mio de zéros ignorés, 6 Mio/s`. `changeId` enregistré : `.../5`.
  Vérifié indépendamment dans `lab-vcenter-out/test_ubuntu/state.json`
  (`round: 1`, `capacity: 4294967296`, `cursor: 0`).
- **Delta (round 2)**, relancé sans aucun changement volontaire sur la VM :
  **`QueryChangedDiskAreas` réel contre vCenter** — 31 zones → 31 tranches
  (6 Mio, 0,15 % du disque, écart non nul probablement dû à l'activité
  normale de l'invité Ubuntu sous tension). `cycle 2 (delta) terminé en
  6 s — 6 Mio écrits, 0 Mio de zéros ignorés, 1 Mio/s`. `changeId`
  réenregistré : `.../9`. Vérifié indépendamment dans `state.json`
  (`round: 2`, même `cursor: 0` après commit).
- Dans les deux rounds : snapshot de migration créé (`v2fc-r0002` pour le
  delta) puis **supprimé proprement** (la connexion étant restée saine
  cette fois, le défaut de `remove_snapshot()` ci-dessus ne s'est pas
  déclenché). `vmxSpec` « vmx + dcPath/dsName » rejeté à l'ouverture du
  disque (`VixError=0x1, code=1`) puis repli réussi sur `moref=` — **exactement
  le même comportement qu'en connexion directe à l'hôte (§5 ter/quater),
  maintenant confirmé via vCenter aussi**. Transport `nbdssl` négocié et
  utilisé dans les deux rounds ; `hotadd` reste listé comme disponible par
  le VDDK mais n'est pas réellement exerçable depuis ce poste (il exige que
  l'hôte lecteur soit lui-même une VM du cluster source, ce qui n'est pas
  le cas d'un conteneur Docker sur le Mac de Nicolas — pas un défaut, une
  limite du banc d'essai actuel).

**Ce que ça change pour L1** : le maillon critique (VDDK + CBT + agent
relais) est maintenant prouvé sur **deux** plateformes réelles distinctes —
ESXi en connexion directe (15/09) et vCenter (17/09) — avec seed et delta
réels dans les deux cas. Le doute spécifique laissé ouvert au §5 quinquies
(repli `moref` non vérifié via vCenter) est levé.

**Discordance à signaler, pas résolue ici** : `ARCHITECTURE.md` §14 définit
le critère de sortie « sur lab » de L1 comme « un delta lu sur vSphere et
**écrit au bon offset dans FusionCompute** » — or ni ce test ni celui du
15/09 n'écrivent réellement dans FusionCompute : les deux écrivent dans un
fichier brut local via `relay/writer.py` (le POC ne prétend pas plus, voir
`v2fc/README.md`). Le driver d'écriture FusionCompute n'existe pas encore
(§13, partie lecture seule). Donc, au sens strict de ce critère de sortie
tel qu'écrit, L1 n'est pas encore clos — seule sa moitié « lecture source »
l'est, deux fois. `ARCHITECTURE.md` a été annoté en conséquence (§14) sans
trancher la question à la place de Nicolas : soit redéfinir le critère de
sortie de L1 pour qu'il ne porte que sur la lecture source (ce qui
correspond à ce qui a été réellement demandé et testé jusqu'ici), soit
considérer que L1 ne sera formellement clos qu'une fois un test d'écriture
réelle vers FusionCompute effectué (ce qui suppose le driver cible écrit et
testé — travail de L2).

---

## 6. Pièges déjà payés — ne pas les repayer

Chacun a coûté du temps ; ils sont tous documentés en détail dans
`ARCHITECTURE.md`, résumés ici pour qu'une nouvelle session ne les réintroduise pas.

**§2.2 — Le VDDK dégrade silencieusement vers NBD.** Le plugin de transport
avancé (`san`, `hotadd`) ne se charge que si `libDir` pointe sur la **racine**
de la distribution (VDDK y concatène `lib64/` lui-même) **et** que
`LD_LIBRARY_PATH` contient `lib64` **avant le démarrage du processus** (le VDDK
ne pose aucun `RPATH`). Si l'une manque, `InitEx` renvoie quand même `VIX_OK`
et la migration tourne trois à dix fois plus lentement, sans aucun message.
Modifier `os.environ` à l'exécution est sans effet ; précharger les
bibliothèques en `RTLD_GLOBAL` **provoque un SIGSEGV** (testé).

**§2.3 bis — `restype = c_char_p` sur une chaîne à libérer fait avorter le
processus.** ctypes recopie alors la chaîne en objet `bytes` Python et perd le
pointeur d'origine ; le rendre à la fonction de libération revient à
`free()` de la mémoire Python → `munmap_chunk(): invalid pointer`. Le cas
s'est produit sur `GetErrorText`/`FreeErrorText`, et c'était le **chemin
d'erreur** qui plantait : le processus mourait avant de rapporter l'erreur
réelle. Utiliser `c_void_p` + `ctypes.string_at` + restitution du **même**
pointeur. En revanche `ListTransportModes` et `GetTransportMode` ne doivent
**pas** être libérés (`c_char_p` y est correct), et `FreeConnectParams` ne
libère **pas** les chaînes membres.

**§7.2 — La signature d'appariement de volume ne va PAS dans la zone de
données.** Première version fausse : placée dans le dernier secteur de la
capacité, elle était écrasée par le seed et aurait corrompu la table de
partition GPT de secours de l'invité migré. Elle vit dans le réservoir
au-delà de la capacité source ; le volume cible est provisionné à
`arrondi(capacité + 512)`.

**§6.4 — Le chemin de données n'emprunte pas le port de l'API.** `nbd` et
`nbdssl` passent par le **902** de chaque ESXi. À inscrire dans les règles de
filtrage dès la conception.

**`disks(snapshot=…)`** accepte indifféremment un `vim.vm.Snapshot` ou son
`.config` — les deux se ressemblent assez pour qu'on s'y trompe, et l'erreur
ne se voit qu'à l'exécution sur une plateforme réelle.

**`config.managedBy` est renseigné par vCenter**, donc absent en connexion
directe à un hôte. La détection des VM d'agent (vCLS…) s'appuie aussi sur le
`guestId` `crxSys1Guest` et sur le préfixe de nom.

---

## 7. Invariants à ne jamais assouplir

Ce sont la promesse fonctionnelle de l'outil, et ils sont appliqués
mécaniquement, pas par convention (§4 de `ARCHITECTURE.md`).

- **I1** — la VM source n'est jamais supprimée ni déplacée. `SourceGuard`
  (`datamover/vsphere.py`) intercepte les méthodes vSphere **par leur nom,
  avant tout appel réseau**. Liste blanche de cinq mutations tolérées :
  `CreateSnapshot_Task`, `RemoveSnapshot_Task`, `ReconfigVM_Task` (CBT
  uniquement), `ShutdownGuest`, `PowerOffVM_Task`. `PowerOnVM_Task` est
  délibérément **interdite** : rallumer la source est une décision de rollback
  qui appartient à l'orchestrateur.
- **I2** — aucune écriture n'est jamais émise vers un disque source.
- **I5** — un job peut être interrompu à tout instant sans corrompre l'état.
- **Règle d'or du `changeId`** (§8.2) : il n'est persisté qu'**après** le
  `fdatasync()` confirmant que les octets sont sur disque. L'ordre inverse
  crée un trou silencieux que le delta suivant ne rattraperait jamais.
- **CBT invalidé → re-seed complet obligatoire**, jamais de continuation
  optimiste. Une cible silencieusement incomplète est le pire mode de
  défaillance possible.

---

## 8. Suite prévue

| Lot | Contenu | État |
|---|---|---|
| L1 | POC du maillon critique | **Clos** (§5 quinquies) : cycle complet **seed et delta** réussi avec le driver de production sur l'ESXi de lab — 25 Gio, `changeId` correct d'un round à l'autre, snapshot toujours nettoyé. Écriture côté FusionCompute non commencée. |
| L0a | Driver source vSphere | Fait, éprouvé sur ESXi réel — inventaire, CBT, snapshot, lecture VDDK **et écriture du cycle complet via `v2fc_source.py sync`** |
| L0b | Socle web : FastAPI, PostgreSQL, Redis, auth/RBAC, docker-compose, schéma du §8, machine à états, progression WebSocket | **Clos pour la partie déjà en portée** (§11) : stack Docker complète testée par Nicolas contre l'ESXi de lab — connexion, inventaire, sélection multi-VM, plan, pré-vérification, tout validé dans le navigateur. Machine à états et progression WebSocket pas encore abordées : elles viendront avec l'exécution des jobs (L2) |
| — | Driver cible FusionCompute | **Partie lecture écrite le 17/09/2026** (§13) : `datamover/fusioncompute.py` (connexion, sites, clusters, hôtes, datastores, DVS/port groups), exposée par `api/fusioncompute_bridge.py` et deux routes (`/endpoints/{id}/datastores`, `/networks`), avec écran web de mapping par VM. **Non testé contre le lab réel** — voir §13. Écriture (créer/attacher un volume, créer la VM cible) non commencée : viendra avec L2, une fois le relais existant. |
| L2 | Cycle complet avec reprise | Non commencé |
| L3 | Bascule + `virt-v2v --in-place` + rollback | Non commencé |
| L4 | UI, plans multi-VM, pool de relais | Non commencé |
| L5 | Durcissement | Non commencé |

Nicolas avait choisi de faire le driver vSphere avant le socle web, et a
fourni la documentation *FusionCompute Interface Reference* (VRM APIs) le
17/09/2026 — voir §12.

---

## 9. Amorce pour la nouvelle session

À coller tel quel dans le nouveau compte, avec le dossier `~/Downloads/APP`
connecté :

> Je reprends un projet en cours : une application web de migration de VM de
> VMware vSphere vers Huawei FusionCompute. Le dossier `~/Downloads/APP` est
> connecté.
>
> Lis d'abord `APP/HANDOFF.md` — c'est le dossier de reprise, il explique où
> en est le projet, ce qui est prouvé, et le point de blocage actuel. Lis
> ensuite `APP/ARCHITECTURE.md`, qui est le document de conception de
> référence, en particulier les §2 (pièges déjà payés), §4 (invariants) et
> §12 (décisions ouvertes).
>
> Travaille en français, registre technique, je suis architecte solutions en
> virtualisation et stockage. Ne m'invente jamais de chemins d'API ni de
> chiffres : signale ce que tu ne sais pas. Quand tu affirmes qu'un truc
> marche, montre-le en l'exécutant.
>
> Le point de blocage est décrit au §5 du HANDOFF. Commence par là.

---

## 10. Contraintes d'environnement rencontrées

- Le poste est un **Mac Apple Silicon**. Le VDDK n'existe qu'en **x86-64** :
  Docker Desktop avec `--platform linux/amd64` (Rosetta) fonctionne et atteint
  le réseau local. Débit mesuré sous Rosetta sur le banc d'essai : 652 Mio/s.
  **VMware Fusion sur Apple Silicon ne sait pas exécuter de VM x86-64** — ce
  n'est pas une alternative.
- Le bac à sable cloud de Claude et la VM Linux du pont n'ont **aucune route
  vers le réseau local** (loopback seul, tout passe par un proxy HTTP). Tout
  test contre l'ESXi doit donc être lancé par Nicolas depuis son Mac. Ne pas
  lui demander d'identifiants : ils seraient inutilisables.
- Nicolas fournit les identifiants ESXi par variables d'environnement et
  saisie interactive du mot de passe ; les outils ne l'acceptent jamais en
  argument de ligne de commande.


---

## 11. Socle web L0b — clos (15-16/09/2026)

Suite à la demande de Nicolas de démarrer l'interface web (connexion à un
ESXi/vCenter, inventaire des VM, sélection multiple pour un plan de
migration), et à son choix explicite de construire tout de suite le socle
complet plutôt qu'une tranche minimale : **PostgreSQL + Redis + auth/RBAC +
docker-compose**, conformément à `ARCHITECTURE.md` §8, pas un prototype
posé sur du sable.

### Ce qui existe

- **`v2fc/api/`** — API FastAPI (async, SQLAlchemy 2.0 + `asyncpg`,
  migrations Alembic). Import et tests unitaires validés dans le bac à
  sable de la session (pas sur l'ESXi réel — voir plus bas). Surface :
  - `auth` — connexion (JWT), `/me`.
  - `endpoints` — CRUD des environnements source (vCenter ou ESXi
    autonome, `datamover.vsphere` traite les deux pareil), sonde de
    connexion, inventaire (datacenters → clusters → VM, chaque VM
    enrichie du statut `datamover.preflight.run_preflight`).
  - `plans` — création d'un plan à partir d'une sélection de VM,
    listing, **pré-vérification à la demande** (rejouée à chaque
    consultation, pas mise en cache : une VM peut avoir changé d'état
    depuis la création du plan).
  - **Pas encore** : lancement/suivi de jobs, machine à états, WebSocket
    de progression — c'est le lot suivant (exécution), pas traité ici.
  - RBAC à 3 rôles (viewer/operator/admin), mots de passe des
    environnements chiffrés en base par enveloppe (AES-256-GCM, DEK par
    secret, KEK dans `V2FC_KEK`) — §13 de `ARCHITECTURE.md`.
  - **Compléments au schéma du §8.1** : `ARCHITECTURE.md` référençait déjà
    `credentials` et `plans` par clé étrangère mais ne les définissait
    nulle part, et l'auth/RBAC du §9 n'avait pas de table `users`. Ajoutés
    ici (`api/models.py` : `User`, `Credential`, `Plan`, `PlanVm`) — pas
    une invention de comportement vSphere/FusionCompute, de la plomberie
    applicative ordinaire. **`ARCHITECTURE.md` §8.1 a été mis à jour en
    conséquence** (les quatre tables y sont maintenant définies, avec une
    note datée expliquant pourquoi elles ont été ajoutées après coup) —
    les deux documents sont de nouveau alignés.
  - **Bug trouvé et corrigé avant tout test réel** : `schemas.py`
    déclarait `vms` comme champ obligatoire de `InventoryCluster`, mais
    `vsphere_bridge.inventory()` rattache les VM au **datacenter**, jamais
    au cluster (un cluster n'affiche que ses hôtes). Avec
    `response_model=InventoryResponse` sur la route, ça aurait fait
    échouer **chaque** appel à `/endpoints/{id}/inventory` en 500
    (`ResponseValidationError`) — la fonctionnalité même demandée par
    Nicolas. Corrigé : `vms` déplacé sur `InventoryDatacenter`, plus rien
    d'obligatoire sur `InventoryCluster`. Revalidé par instanciation
    directe du schéma avec la forme réelle produite par le pont.

- **`v2fc/web/`** — interface React 18 + TypeScript + Vite. Pages :
  connexion, liste des environnements (ajout + sonde), inventaire d'un
  environnement (tableau des VM, case à cocher par VM migratable,
  « tout sélectionner les migratables », création d'un plan), liste des
  plans (VM du plan, bouton « rafraîchir la pré-vérification » par
  plan). Type TypeScript aligné sur `InventoryDatacenter`/`InventoryCluster`
  après la correction ci-dessus (un code mort qui référençait l'ancienne
  forme incohérente a aussi été retiré). **`npm install` puis
  `npm run build` (`tsc -b && vite build`) validés dans le bac à sable :
  compilation TypeScript propre, aucun avertissement.**

- **`v2fc/docker-compose.yml`** — `postgres`, `redis`, `api`
  (`api/Dockerfile`, déjà existant), `web` (nouveau `web/Dockerfile`,
  serveur de dev Vite, pas un build de prod nginx — cohérent avec le stade
  du projet). Le Data Mover (VDDK) reste volontairement **hors compose** :
  il continue de tourner à la demande via la CLI, monté avec
  `--platform linux/amd64` — ce n'est pas un service permanent.

- **`v2fc/.env.example`** — variables à copier en `.env` : `JWT_SECRET`
  et `V2FC_KEK` (commandes de génération incluses), identifiants Postgres,
  `CORS_ORIGINS`.

### Validé le 16/09/2026 sur matériel réel

Nicolas a construit et lancé la stack Docker complète depuis son Mac (la
procédure ci-dessous), puis testé dans le navigateur contre l'ESXi de lab
réel (172.25.100.91) : connexion à l'environnement, chargement de
l'inventaire (précisément le point que corrigeait le bug de schéma
ci-dessus — confirmé sans régression), sélection de la VM `ubuntu`,
création d'un plan, rafraîchissement de la pré-vérification. **Tout
fonctionne.** L0b est donc clos pour la portée actuelle (connexion,
inventaire, sélection multi-VM, plans).

### Ce qui reste hors de la portée de L0b (pas un manque, une portée non abordée)

- Lancement de job, machine à états, WebSocket de progression : c'est
  L2, qui suppose un driver cible FusionCompute existant et testé.
- Driver cible FusionCompute lui-même : l'API REST est documentée depuis
  le 17/09/2026 (D2 résolu, voir §12 et `ARCHITECTURE.md` §7.5), mais le
  driver n'est pas encore écrit.

### Relancer la stack — procédure (validée de bout en bout le 16/09/2026)

Cette procédure a déjà été suivie une fois avec succès (16/09/2026) ; elle
sert maintenant de référence pour relancer la stack (redémarrage du Mac,
nouvelle session de test, etc.) — pas d'étape supplémentaire à découvrir.

**0. Prérequis** — Docker Desktop lancé (l'icône baleine doit être stable,
pas en cours de démarrage). Aucune contrainte `--platform linux/amd64` ici
contrairement au Data Mover : l'API et le web sont du Python/Node ordinaire,
ils tournent nativement en `arm64` sur un Mac Apple Silicon.

```bash
cd ~/Downloads/APP/v2fc
docker info >/dev/null && echo "Docker OK" || echo "Docker Desktop n'est pas lancé"
```

**1. Configurer les secrets** — une seule fois (le fichier `.env` reste
ensuite en place, il n'est jamais recommité) :

```bash
cp .env.example .env

# Génère les deux valeurs et les affiche : les copier-coller dans .env
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))"
python3 -c "import os,base64; print('V2FC_KEK=' + base64.b64encode(os.urandom(32)).decode())"

# puis éditer le fichier (nano, vim, VS Code...) :
nano .env
```

Dans `.env`, remplacer les deux lignes vides `JWT_SECRET=` et `V2FC_KEK=`
par les valeurs générées ci-dessus. Les identifiants Postgres
(`POSTGRES_USER/PASSWORD/DB`) et `CORS_ORIGINS` peuvent rester à leur valeur
par défaut pour un premier test.

**2. Construire les images** :

```bash
docker compose build
```

Construit `api` (Python 3.12 + dépendances, ~1-2 min) et `web` (Node 20 +
`npm install`, ~30 s). `postgres` et `redis` sont des images officielles,
pas de build.

**3. Démarrer la base et le cache, attendre qu'ils soient prêts** :

```bash
docker compose up -d postgres redis
docker compose ps
```

Attendre que la colonne `STATUS` affiche `healthy` pour les deux (quelques
secondes, poll avec `docker compose ps` si besoin) avant l'étape suivante —
sinon l'API échouera à sa première tentative de connexion (voir dépannage
plus bas, ce n'est pas grave si ça arrive).

**4. Démarrer l'API et le web** :

```bash
docker compose up -d api web
docker compose logs -f api
```

Dans les logs de `api`, on doit voir Alembic appliquer la migration
(`Running upgrade  -> ...`) puis Uvicorn annoncer `Application startup
complete` sur `0.0.0.0:8000`. `Ctrl-C` pour sortir du suivi des logs (ça
n'arrête pas le conteneur, c'est juste `-f` qui suit).

Vérifier que l'API répond :

```bash
curl -s http://localhost:8000/api/v1/health
```

doit renvoyer un JSON avec un statut positif — sinon voir dépannage.

**5. Créer le premier compte** :

```bash
docker compose exec api python3 -m api.seed_admin --email <ton adresse>
```

Le mot de passe est demandé **de façon interactive** (deux fois, pour
confirmation) — `seed_admin.py` ne l'accepte jamais en argument de ligne de
commande, par cohérence avec le reste du projet (§10). `docker compose exec`
alloue un terminal par défaut, donc l'invite s'affiche normalement.

**6. Tester dans le navigateur** :

Ouvrir `http://localhost:5173`, se connecter avec le compte créé à
l'étape 5, puis :

1. Page **Environnements** : ajouter l'ESXi de lab — nom libre, adresse
   `172.25.100.91`, utilisateur/mot de passe du lab, **décocher** « Vérifier
   le certificat TLS » (certificat auto-signé). Cliquer « Tester » : doit
   afficher `OK` avec la version ESXi.
2. Cliquer « Voir les VM » : l'inventaire doit se charger et lister la VM
   `ubuntu`. **C'est le point précis que corrige le bug de schéma trouvé
   aujourd'hui** — si ça renvoie une erreur 500, se reporter à la section
   « Ce qui existe » ci-dessus et vérifier que `api/schemas.py` a bien
   `vms` sur `InventoryDatacenter` (pas sur `InventoryCluster`).
3. Cocher `ubuntu`, donner un nom au plan, cliquer « Créer le plan ».
4. Page **Plans** : le plan doit apparaître avec la VM listée. Cliquer
   « Rafraîchir la pré-vérification » : doit afficher un badge
   migratable/bloqué avec le détail si bloqué.

**7. Arrêter** (quand le test est terminé) :

```bash
docker compose down          # arrête les conteneurs, garde les données Postgres
# docker compose down -v     # (à n'utiliser QUE si on veut repartir de zéro : supprime aussi le volume Postgres, donc tous les comptes/environnements/plans créés)
```

### Dépannage

- **`docker compose up -d api` échoue à la connexion Postgres au premier
  démarrage** (base pas encore tout à fait prête malgré le `healthcheck`,
  arrive parfois sur un Mac au premier `up`) : relancer
  `docker compose up -d api` une fois — pas d'action plus poussée.
- **`Bind for 0.0.0.0:5432 failed: port is already allocated`** (ou 6379,
  8000, 5173) : un service local écoute déjà sur ce port (un Postgres ou
  Redis déjà installé sur le Mac, par exemple). Soit l'arrêter, soit changer
  le port publié dans `docker-compose.yml` (par ex. `"127.0.0.1:5433:5432"`)
  — le port interne au réseau Docker ne change pas, seul le premier chiffre
  (celui exposé sur le Mac) doit être libre.
- **La page web n'arrive pas à joindre l'API** (erreurs réseau dans la
  console du navigateur) : vérifier `docker compose logs api` d'abord: si
  l'API n'a pas fini de démarrer, le proxy Vite (`/api` → `VITE_API_PROXY_TARGET`,
  fixé à `http://api:8000` dans `docker-compose.yml`) renverra des erreurs
  de connexion tant que l'API n'écoute pas.
- **`JWT_SECRET non défini` ou `V2FC_KEK non défini` dans les logs de
  `api`** : `.env` n'a pas été rempli, ou `docker compose` ne le trouve pas
  (il doit être au même niveau que `docker-compose.yml`, c'est-à-dire
  `v2fc/.env`, pas `APP/.env`).
- **Message `KeyError` ou `ValidationError` côté API dans les logs, à
  l'appel d'inventaire** : signaler l'erreur complète — potentiellement un
  autre écart schéma/pont non détecté par les vérifications faites dans le
  bac à sable (qui ne peut pas parler à un ESXi réel).


---

## 12. D2 résolu — API REST FusionCompute (17/09/2026)

Nicolas a donné l'adresse de la console VRM du lab FusionCompute
(`172.25.99.230:8443`, utilisateur `admin`) pour commencer le travail sur le
driver cible. Ni le bac à sable cloud, ni le pont Linux (`device_bash`), ni
le navigateur intégré de l'app n'ont de route vers ce réseau (même
contrainte que pour l'ESXi de lab, §10) — impossible donc d'explorer l'API
en direct depuis cette session.

Nicolas a alors fourni trois documents constructeur : *FusionCompute 8.10.0
VRM APIs* (docx, ~1,5 Mo — la référence des API REST northbound), *KRM
Interface Documentation* (docx — couvre un sous-système distinct, la
gestion de clusters Kubernetes sur FusionCompute, **pas utile à D2**), et
*Monitoring KPI List* (xlsx — catalogue de métriques, utile plus tard pour
le durcissement L5, pas pour D2). Les trois sont archivés dans
`APP/docs-fusioncompute/` pour qu'une session future puisse les relire sans
re-upload.

Les six points bloquants de D2 sont désormais répondus **avec citation de
source**, sans rien deviner : le détail complet (chemins, en-têtes, corps
de requête, codes d'erreur pertinents) est dans `ARCHITECTURE.md` §7.5, et
la limite de disques par VM dans §7.4. Résumé :

1. **Session/jeton** — `POST /service/session`, authentification par
   en-têtes (`X-Auth-User`, `X-Auth-Key` haché SHA-256), jeton dans l'en-tête
   `X-Auth-Token`, validité **10 minutes**.
2. **Création de volume** — `POST /service/sites/<site_id>/volumes`,
   réponse asynchrone (`taskUrn`/`taskUri` à suivre).
3. **Attachement** — `POST <vm_uri>/<vm_id>/action/attachvol` (existe aussi
   `.../action/createattachvol` qui crée et attache en un appel).
4. **Détachement** — `POST <vm_uri>/<vm_id>/action/detachvol`.
5. **Création de VM avec firmware imposé** — `POST /service/sites/<site_id>/vms`,
   `vmConfig.properties.bootFirmware` = `"UEFI"` ou `"BIOS"`.
6. **Limite de disques par VM** — dépend du bus : IDE 3, VirtIO 25, SCSI 128
   emplacements (§7.4).

**Deux points restent à vérifier sur le lab avant d'écrire le driver en
confiance** (relevés dans le document lui-même, pas des suppositions de ma
part) :
- Le document affirme que le port des API REST est **7443**, alors que
  l'adresse donnée par Nicolas pour la console VRM est le port **8443** —
  vraisemblablement le port de la console web, distinct du port API. À
  confirmer avant le premier appel réel.
- Le document se contredit lui-même sur la limite de slots SCSI : le
  tableau de référence dit 128, mais le commentaire de l'exemple de requête
  `attachvol` dit 11. À vérifier en pratique (attacher un 12ᵉ disque SCSI à
  une VM de test) avant de dimensionner le pool de relais sur l'hypothèse
  haute (§7.4).

**Ce qui n'est pas fait** : le driver cible FusionCompute lui-même (aucun
code écrit), et donc tout ce qui en dépend (inventaire FusionCompute dans
l'UI, exécution de jobs, L2). D2 étant résolu, la prochaine étape naturelle
est d'écrire une première version du driver (connexion, création+attachement
d'un volume de test, création d'une VM relais) et de la valider contre le
lab FusionCompute réel — dans le même esprit que le POC L1 pour VDDK :
prouver chaque étape par une exécution réelle, pas par lecture de
documentation seule.

---

## 13. Driver cible FusionCompute (lecture) — écrit le 17/09/2026, NON TESTÉ sur le lab

Nicolas a précisé le flux complet voulu pour l'application (17/09/2026,
verbatim conservé pour référence) :

> 1. je me connecte a un ESX ou un vcenter
> 2. je liste les VM a migrer et je les selectionne
> 3. je me connecte a un environnement fusion compute
> 4. je liste les datastore présent et je choisis la destination de stockage pour chaque VM
> 5. je liste les réseau virtuel de l'environnement fusion compute et je choisis pour chaque VM le bon reseau
> 6. je commence la copie en ligne
> 7. une fois la copie de base faite, j'ai la possibilite de faire un update pour limiter le delta
> 8. j'ai l'option migrate now qui va arreter la VM sur vmware faire la derniere copie et avoir le choix ou non de redemarrer automatiquement la VM sur fusion compute

Les étapes 1-2 sont closes depuis L0b (§11). Cette section couvre ce qui a
été écrit pour les étapes **3 à 5** ce 17/09/2026, pendant que Nicolas
avait accès au lab FusionCompute mais plus à VMware. Les étapes 6-8 ne sont
pas commencées : elles dépendent du relais et du driver d'écriture
(§7.1-§7.3 d'ARCHITECTURE.md), pas encore de code.

### Ce qui a été écrit

- **`v2fc/datamover/fusioncompute.py`** — driver cible, lecture seule pour
  l'instant : connexion/session (`FusionComputeClient`, §7.5 ①), listage des
  sites/clusters/hôtes, `list_datastores()` (agrège les datastores de tous
  les hôtes du site, dédupliqués — pas de « lister tous les datastores du
  site » en un seul appel dans la documentation), `list_networks()` (DVS
  puis port groups de chaque DVS, aplati et annoté du DVS parent), et
  `wait_task()` (suivi de tâche asynchrone, §7.5, pas encore appelé nulle
  part — utile dès que ②③④⑤ seront câblées). Rien n'est deviné : chaque
  chemin vient de la table maîtresse « Object ID Description » du document
  *VRM APIs* (urn/uri par type d'objet) et des sections de requête
  correspondantes, citées en commentaire dans le fichier lui-même.
- **`v2fc/tests/test_fusioncompute_driver.py`** — 9 tests, sans VRM réel
  (une fausse `requests.Session` rejoue des réponses préparées). Vérifient
  la construction exacte des requêtes documentées : en-têtes de connexion
  (`X-Auth-User`, `X-Auth-Key` haché SHA-256, `X-Auth-UserType`/`AuthType`
  par défaut), extraction du jeton et du site, déduplication des
  datastores partagés entre hôtes, aplatissement des port groups par DVS,
  tolérance à l'incohérence documentaire `portGroups`/`portgroups`, et le
  cycle complet de suivi de tâche (poll jusqu'à `success`, exception sur
  `failed`). `python3 -m pytest tests/` : 67 passés (58 précédents + 9
  nouveaux), aucune régression.
- **`api/fusioncompute_bridge.py`** — pont synchrone vers le driver
  (`run_in_threadpool`, même motif que `vsphere_bridge.py`) : `probe`,
  `list_datastores`, `list_networks`.
- **`api/models.py`** — deux ajouts, plomberie applicative, pas des
  décisions d'API FusionCompute :
  - `Endpoint.port` (entier). Jusqu'ici le port était **toujours 443 en
    dur** dans `_connection_args`, jamais stocké — ça passait inaperçu tant
    que seul vCenter/ESXi existait. FusionCompute casse cette hypothèse
    (port d'API documenté 7443, distinct du port console 8443 donné par
    Nicolas — voir plus bas). `EndpointCreate.port` existait déjà dans le
    schéma mais était silencieusement ignoré par le routeur : c'était déjà
    un bug avant ce soir, corrigé au passage.
  - `PlanVm.tgt_datastore_urn/name`, `tgt_network_urn/name` — le mapping
    par VM des étapes 4-5. Uniquement urn + nom (ce que l'API FusionCompute
    renvoie déjà), le nom dupliqué pour l'affichage même si la ressource
    disparaît ensuite côté FusionCompute.
  - Migration Alembic `0002_fusioncompute_targets.py` — additive
    (`op.add_column`), pas un nouveau `create_all` : la base a déjà des
    données réelles depuis le test du 16/09/2026. **Nicolas doit lancer
    `alembic upgrade head` avant de redémarrer l'API** (voir procédure
    ci-dessous).
- **`api/routers/endpoints.py`** — `_connection_args` utilise désormais
  `endpoint.port` ; nouveau `_bridge_for(endpoint)` qui bascule entre
  `vsphere_bridge` et `fusioncompute_bridge` selon `endpoint.kind` (probe
  fonctionne maintenant pour les deux types, pas seulement vCenter/ESXi) ;
  `/inventory` refuse désormais explicitement un endpoint FusionCompute
  (400, message clair) — l'inventaire de VM n'a pas de sens côté cible ;
  deux nouvelles routes `GET /endpoints/{id}/datastores` et `.../networks`,
  réservées aux endpoints `kind=fusioncompute` (mis en cache Redis 30 s,
  même TTL que l'inventaire — `api/cache.py`).
- **`api/routers/plans.py`** — `PUT /plans/{id}/target` (choix de
  l'environnement FusionCompute cible d'un plan) et `PUT
  /plans/{id}/vms/{vm_id}/mapping` (datastore + réseau cible d'une VM du
  plan). Séparés de la création du plan : un plan existe déjà (créé depuis
  l'inventaire source) avant qu'une cible ne soit choisie.
- **Frontend** — `Endpoints.tsx` : sélecteur de type (vCenter/ESXi vs
  FusionCompute) et champ port dans le formulaire de création (le port par
  défaut change automatiquement : 443 pour vCenter, 7443 pour
  FusionCompute, avec un avertissement affiché sur l'incertitude du port
  côté FusionCompute) ; bouton « Voir les VM » masqué pour un endpoint
  FusionCompute (l'inventaire ne s'y applique pas). Nouvelle page
  **`PlanTarget.tsx`** (`/plans/:id/target`) : choix de l'environnement
  cible pour le plan, puis tableau par VM avec un menu déroulant datastore
  et un menu déroulant réseau (peuplés depuis les nouvelles routes), et un
  bouton d'enregistrement par ligne. Lien ajouté depuis chaque carte de
  `Plans.tsx`. `npm run build` (tsc -b + vite build) validé sans erreur.

### Ce qui n'a PAS été écrit (volontairement)

Les opérations d'écriture FusionCompute documentées en ARCHITECTURE.md
§7.5 (②③④⑤ : créer un volume, l'attacher, le détacher, créer la VM cible)
**ne sont pas appelées** depuis `fusioncompute.py` — seulement documentées
en commentaire. Les implémenter maintenant reviendrait à deviner leur usage
avant d'avoir un vrai appelant (le relais, §7.1, qui n'existe pas encore) :
même discipline que pour le driver source (`SourceGuard`, invariant I1).
Elles arriveront avec L2.

### CE QUI RESTE À FAIRE PAR NICOLAS — rien de ce qui précède n'a tourné contre le VRM réel

Aucun outil de cette session n'atteint `172.25.99.230` (§10 : ni le bac à
sable cloud, ni `device_bash`, ni le navigateur intégré n'ont de route vers
ce réseau). Tout le code ci-dessus est écrit à partir de la documentation
constructeur, jamais exécuté contre un VRM réel. **Avant de faire confiance
à ce code**, depuis un terminal avec accès au lab :

```bash
cd ~/Downloads/APP/v2fc
docker compose up -d --build
# --build est indispensable : c'est lui qui refait l'image api (nouveau
# datamover/fusioncompute.py, api/fusioncompute_bridge.py, nouvelles routes,
# requests dans requirements.txt) et l'image web. La migration 0002
# (port + mapping par VM) s'applique automatiquement à ce démarrage —
# le conteneur api lance `alembic upgrade head` avant uvicorn (api/Dockerfile),
# pas besoin de la lancer à la main.
docker compose logs -f api   # vérifier que la migration passe et qu'uvicorn démarre sans erreur
```

Puis dans l'UI web (`http://localhost:5173` ou le port configuré) :

1. Ajouter un endpoint de type « FusionCompute — VRM », adresse
   `172.25.99.230`, utilisateur `admin`. **Essayer d'abord le port 7443**
   (celui documenté pour l'API) ; si la connexion échoue, essayer 8443
   (celui de la console, au cas où le VRM ne sépare pas les deux ports en
   pratique, contrairement à ce que dit la documentation).
2. Cliquer « Tester » (bouton probe). Si ça échoue, le message d'erreur
   renvoyé par `fusioncompute_bridge.probe` inclut le détail brut de la
   réponse VRM — utile pour voir si c'est un problème de port, de mot de
   passe haché, ou autre chose.
3. Si la connexion réussit : créer un plan depuis un inventaire vCenter/ESXi
   existant (si l'accès VMware revient), ou réutiliser un plan déjà créé ;
   ouvrir « Choisir une cible FusionCompute », sélectionner l'endpoint, et
   vérifier que les datastores et les réseaux affichés correspondent
   vraiment à ce qui existe sur le lab.

**Mise à jour du 17/09/2026 (soir) — port confirmé.** Nicolas a testé en direct au VRM depuis son terminal (curl, identifiants volontairement faux pour observer juste le code d'erreur) :
- `POST https://172.25.99.230:7443/service/session` → `HTTP 400 {"errorCode":"10100103", ...}` — exactement le code documenté pour un login raté (§7.5 ①). **7443 est confirmé comme le port réel de l'API.**
- Le même appel sur `8443` → `HTTP 401 {"errorCode":"10000002", ...}` — code absent de la documentation, cohérent avec 8443 = la console, pas l'API.
- Les deux ports sont ouverts au réseau (`nc`), donc ce n'était pas un problème de pare-feu : c'est bien 7443 qu'il faut utiliser. `DEFAULT_API_PORT` dans `datamover/fusioncompute.py` et le port suggéré par le formulaire web étaient déjà réglés sur 7443 — pas de changement de code nécessaire, seule la documentation était incertaine.

Reste à confirmer, avec un vrai mot de passe cette fois :

1. ~~Le port réel de l'API~~ **CONFIRMÉ ci-dessus : 7443.**
2. **Le hachage exact du mot de passe.** Le document dit seulement « haché
   SHA-256 par défaut », sans préciser l'encodage. `fusioncompute.py`
   utilise l'hypothèse la plus directe : SHA-256 hexadécimal minuscule du
   mot de passe UTF-8 brut (`hashlib.sha256(password.encode()).hexdigest()`).
   Si la connexion échoue avec un mot de passe qu'on sait correct, c'est le
   premier suspect.
3. **La clé de réponse des port groups** (`portGroups` vs `portgroups` —
   incohérence entre la description et l'exemple du document lui-même,
   §7.5). Le code accepte les deux ; si aucune des deux n'apparaît en
   pratique, `list_networks()` renverra silencieusement une liste vide par
   DVS plutôt que d'échouer — à surveiller si les réseaux attendus
   n'apparaissent pas dans l'UI.

Une fois ces trois points vérifiés en pratique, les documenter ici (nouvelle
sous-section) avant d'attaquer l'écriture (②③④⑤, §7.5) — même discipline que
pour D2 : citer, jamais deviner.

**Ajout du 17/09/2026 (soir) — suppression d'un endpoint.** Nicolas a signalé
qu'il n'existait aucun moyen de retirer un environnement mal configuré
depuis l'UI (utile typiquement pour corriger un port de test avant le bon
7443). Ajouté : `DELETE /api/v1/endpoints/{id}` (refuse avec 409 si un plan
référence encore l'endpoint en source ou en cible, plutôt que de laisser
remonter une erreur de contrainte de clé étrangère ; nettoie aussi la
credential associée si elle devient orpheline), bouton « Supprimer » (avec
confirmation) dans `Endpoints.tsx`. `npm run build` et `pytest` revalidés
(67 tests, aucune régression).

**Correctif du 17/09/2026 (soir) — 500 sur toute sonde (`probe`), pas propre à FusionCompute.**
Nicolas a testé la connexion à l'endpoint FusionCompute et obtenu une 500.
Trace complète demandée (`docker compose logs api`) : ce n'était pas le
pont FusionCompute (qui attrape déjà toute exception et renvoie `ok:
false`), mais `probe_endpoint` (`api/routers/endpoints.py`) lui-même —
`endpoint.last_probe_at = datetime.now(timezone.utc)` écrit un datetime
*tz-aware* dans une colonne Postgres `TIMESTAMP WITHOUT TIME ZONE`
(`last_probe_at` dans `api/models.py` ne déclare pas `timezone=True`) ;
asyncpg refuse ce mélange (`can't subtract offset-naive and offset-aware
datetimes`). **Ce bug préexistait déjà pour vCenter/ESXi** — probe
FusionCompute ne l'a fait que découvrir, il n'est pas causé par le nouveau
code. Corrigé par `.replace(tzinfo=None)` (toujours UTC, juste rendu
naïf pour matcher la colonne). Revalidé : 67 tests, build web, imports
API (21 routes) tous propres.

**Ajout du 17/09/2026 (matin) — compte API dédié créé sur le VRM.**
Nicolas a créé un compte dédié sur le VRM FusionCompute pour cette intégration, plutôt que de réutiliser `admin` :

- Nom d'utilisateur : `api`
- Type d'utilisateur : *Interface interconnection user* — correspond exactement à `X-Auth-UserType: 2` (§7.5 ①), qui est déjà la valeur par défaut du driver (`user_type: int = 2` dans `FusionComputeClient.__init__`, `datamover/fusioncompute.py`). Aucun changement de code nécessaire de ce côté.
- Statut : Normal · authentification à deux facteurs : désactivée · connexions concurrentes max : illimitées · plage horaire autorisée : aucune restriction · adresses IP autorisées : aucune restriction · créé le 2026-09-17 08:24:31.
- Rôle : `administrator,AlarmPerformance,au…` — **tronqué dans la capture d'écran fournie, le rôle complet n'est pas confirmé.** À vérifier auprès de Nicolas si un rôle plus restreint que « administrator » suffirait pour ce compte technique : il n'a en principe besoin que des droits de lecture sur sites/hôtes/datastores/dvswitchs/portgroups, et plus tard (L2) de créer/attacher/détacher des volumes et créer des VM — pas nécessairement la totalité des droits admin.

**Conséquence pour le diagnostic de connexion en cours.** Les tentatives précédentes documentées ci-dessus utilisaient `admin` ; elles restent valables comme preuve que le port 7443 est le bon (le code d'erreur documenté `10100103` a bien été renvoyé), mais n'informent plus sur la validité d'un mot de passe pour le compte `api`, qui est un compte distinct avec son propre mot de passe. **Prochaine étape : retester la connexion (bouton « Tester » dans l'UI, ou curl direct) avec `api` et son mot de passe**, avec la même prudence que précédemment (`read -rs` pour ne jamais taper le mot de passe en clair dans l'historique, et surveiller `failCount` pour éviter un nouveau verrouillage de 5 minutes).

**Ajout du 17/09/2026 (matin) — taille des VM dans l'inventaire.**
Nicolas a demandé d'afficher la taille de chaque VM dans la liste d'inventaire (`Inventory.tsx`), utile pour juger si un datastore cible a assez de place. Nouvelle colonne « Taille » = somme des capacités des disques virtuels de la VM, en Go (même repli que `DiskSpec.capacity_bytes` dans `datamover/vsphere.py` : `capacityInBytes` si présent, sinon `capacityInKB * 1024`), converti en Go et arrondi à une décimale dans `api/vsphere_bridge.py` (`disk_capacity_gb`), homogène avec les tailles de datastore FusionCompute déjà affichées ailleurs. Champ ajouté à `InventoryVm` (`api/schemas.py`, `web/src/api.ts`) — pas de migration de base nécessaire, la valeur est recalculée à chaque inventaire, jamais stockée. `pytest tests` (67 passés) et `npm run build` revalidés.

**Ajout du 17/09/2026 (matin) — suppression d'un plan de migration.**
Nicolas a demandé la même chose que pour les endpoints (voir plus haut) mais côté plans : `DELETE /api/v1/plans/{plan_id}` (`api/routers/plans.py`), bouton « Supprimer » (avec confirmation) dans `Plans.tsx`. Les VM du plan disparaissent avec (cascade ORM `Plan.vms` + FK `plan_vms.plan_id` en `ON DELETE CASCADE`, déjà en place dans `api/models.py`, rien à changer là). Garde-fou symétrique à celui des endpoints : refuse en 409 si un job d'exécution référence encore ce plan (`jobs.plan_id` n'a pas de cascade, volontairement — §8.1) ; comme aucun job n'est encore créé par l'API (L2 pas écrit), ce cas ne se présente pas encore en pratique, mais le garde-fou est prêt pour quand L2 existera. `pytest tests` (67 passés, 22 routes) et `npm run build` revalidés.

**Ajout du 17/09/2026 (matin) — inventaire gardé en mémoire, tri et recherche.**
Deux demandes de Nicolas :

1. *Ne plus rescanner tout l'inventaire à chaque connexion.* Le cache Redis existait déjà (`api/cache.py`) mais avec un TTL de 30 s (`INVENTORY_TTL_SECONDS`) — en pratique, revenir sur la page quelques secondes après relançait un scan complet vCenter + pré-vérification par VM. Le TTL est supprimé : `get_cached_inventory` (et son équivalent FusionCompute `get_cached_fc_datastores`/`get_cached_fc_networks`) sert désormais la dernière valeur connue indéfiniment, jusqu'à un rafraîchissement explicite (le bouton « rafraîchir » existait déjà dans l'UI, `refresh=true` côté route — rien à ajouter là). Un volume Docker nommé (`v2fc-redis-data:/data`, `docker-compose.yml`) a été ajouté pour que cette dernière valeur survive aussi à un `docker compose down` + `up`, pas seulement à un redémarrage de conteneur — Redis reste un raccourci de confort, pas la source de vérité : sa perte force simplement un nouveau scan complet, rien de cassé si ça arrive.
2. *Trier/rechercher dans l'inventaire.* `Inventory.tsx` : colonnes d'en-tête cliquables (tri croissant/décroissant, indicateur ▲/▼) sur toutes les colonnes (VM, datacenter, état, disques, taille, CBT, firmware, migrable), et un champ de recherche libre (nom, datacenter, OS, état, firmware — insensible à la casse). Purement côté client : la liste est déjà entièrement récupérée par la page, pas besoin de toucher l'API pour ça. Le bouton « Tout sélectionner (migrables) » continue de porter sur la liste complète, pas seulement les VM filtrées affichées.

`pytest tests` (67 passés) et `npm run build` revalidés.

**Ajout du 17/09/2026 (matin) — statut de progression pendant l'inventaire vCenter.**
Nicolas a demandé un retour d'avancement pendant le scan (potentiellement long : connexion + pré-vérification de chaque VM). Le scan lui-même tourne toujours en un seul appel bloquant (`vsphere_bridge.inventory`, dans un thread via `run_in_threadpool` — rien à changer côté vCenter/pyVmomi), mais il publie maintenant sa progression dans Redis au fil de l'eau (`api/cache.py`, `set_inventory_progress_sync`, via un client Redis **synchrone** distinct du client asyncio existant — nécessaire car ce code tourne dans un thread bloquant sans boucle asyncio à qui parler). Nouvelle route de poll, légère et sans effet de bord : `GET /endpoints/{id}/inventory/progress` → `{phase, done, total}` (`phase` : idle/connecting/preflight/done). `Inventory.tsx` interroge cette route toutes les secondes tant que le chargement principal est en cours, et affiche « Pré-vérification des VM : X / Y » avec une petite barre de progression ; la réponse de `GET /inventory` reste la seule source de vérité sur la fin du scan, ce poll n'est qu'indicatif et s'arrête tout seul. TTL court (120 s) sur cette clé de progression (contrairement au cache d'inventaire lui-même, sans TTL depuis l'ajout précédent) : c'est un état transitoire « un scan tourne », pas une valeur à revoir indéfiniment — s'il expire (scan anormalement long, ou crash), l'UI retombe simplement sur l'état par défaut plutôt que d'afficher une progression figée pour toujours. `pytest tests` (67 passés, 23 routes) et `npm run build` revalidés.

**Correctif du 17/09/2026 (matin) — bouton « Enregistrer » grisé sur une VM déjà mappée (`PlanTarget.tsx`).** Nicolas a signalé le bouton toujours désactivé sur une VM dont le datastore/réseau cible avaient déjà été enregistrés lors d'une visite précédente. Cause : la condition ne regardait que l'état `pending` (les sélections faites *pendant cette visite-ci*), qui reste vide tant qu'un `<select>` HTML ne change pas réellement de valeur — or un menu déjà positionné sur la valeur déjà enregistrée ne déclenche jamais `onChange`, même si l'utilisateur re-clique dessus. Corrigé : la condition se base maintenant sur `currentDs`/`currentNet` (la valeur réellement affichée dans le menu, fraîche ou déjà enregistrée), pas seulement sur `pending`. Sans risque côté backend : `PUT /plans/{id}/vms/{vm_id}/mapping` n'écrase un champ que si la valeur envoyée n'est pas `null` (`api/schemas.py` `PlanVmMappingUpdate`, `api/routers/plans.py`) — cliquer « Enregistrer » sans avoir touché l'autre menu ne peut donc pas effacer sa valeur déjà enregistrée. `npm run build` revalidé.

**Ajout du 17/09/2026 (matin) — dossier `install/` pour la livraison finale.**
Nicolas a demandé un dossier pour installer la solution complète (socle web
+ Data Mover/VDDK), à maintenir à jour jusqu'à la fin du projet en vue de la
livraison. Créé à la racine du projet (`install/`, à côté de `v2fc/`) :
consolide en scripts et en un `README.md` unique des procédures déjà
validées sur le terrain, sans en changer le contenu ni en inventer de
nouvelles — `HANDOFF.md` §11 (socle web, validé 16/09/2026) et
`v2fc/README.md` (Data Mover, validé 15 et 17/09/2026, voir §5
quinquies/sexies) restent les sources de référence en cas de divergence.

- `install/check-prereqs.sh` — diagnostic seul (Docker lancé, `docker
  compose` disponible, VDDK présent/décompressé, `.env` déjà configuré ou
  non) ; ne modifie rien.
- `install/setup-env.sh` — crée `v2fc/.env` depuis `.env.example` et génère
  `JWT_SECRET`/`V2FC_KEK` (mêmes commandes que HANDOFF.md §11 point 1) ;
  n'écrase jamais un `.env` existant.
- `install/start-webapp.sh` / `install/stop-webapp.sh` — `docker compose
  build` puis démarrage `postgres`/`redis` (attente `healthy`) puis
  `api`/`web` (HANDOFF.md §11 points 2 à 4) ; arrêt avec ou sans
  suppression des volumes (`--wipe`, confirmation demandée). **Ne créent
  volontairement pas le premier compte** : `api/seed_admin.py` n'accepte
  jamais de mot de passe en argument (invariant du §10) — la commande à
  lancer soi-même s'affiche à la fin de `start-webapp.sh`.
- `install/build-datamover.sh` — vérifie la présence du VDDK puis
  `docker build --platform linux/amd64 -t v2fc-datamover .` (mêmes
  commandes que l'en-tête de `v2fc/Dockerfile`).

**Le VDDK n'est pas inclus dans cette livraison** (licence VMware,
redistribution interdite) : `install/README.md` documente qu'il doit être
obtenu séparément (portail Broadcom/VMware) et décompressé à la racine du
projet avant `build-datamover.sh` — ce dossier ne fait qu'automatiser sa
mise en œuvre, jamais sa fourniture. Rien d'autre n'a été inventé : les
commandes `inventory`/`preflight`/`provision`/`sync` contre un vCenter réel
restent documentées une seule fois, dans `v2fc/README.md`, pour éviter
qu'une copie ici ne diverge de l'original avec le temps.

**À faire avant la livraison finale** (pas fait aujourd'hui, pas encore
demandé par Nicolas) : ce dossier ne couvre encore que L0b + L1 tels
qu'ils existent maintenant. Il faudra le compléter au fil du projet — au
minimum quand le driver cible FusionCompute (écriture) et le lot L2
(machine à états, jobs) existeront, et si le service `web` passe un jour
d'un serveur de dev Vite à un build de production servi par nginx (note
déjà présente dans `web/Dockerfile`).

## 16. Socle L2 — jobs de migration, onglets Migration/Logs (17/09/2026, après-midi)

Demandé explicitement par Nicolas une fois L1 validé deux fois sur plateforme
réelle (§5 sexies), et après un point de phasage explicite : Nicolas a choisi
« tout le socle L2 en une fois » plutôt qu'un découpage progressif (driver
d'écriture seul, puis UI, puis exécution) — voir la discussion tenue dans ce
tour de session. Construit dans la foulée, avec la même discipline que
d'habitude (citer les sources, ne jamais deviner un comportement FusionCompute,
tester ce qui peut l'être sans lab réel).

### Ce qui existe maintenant

- **`datamover/fusioncompute.py`** — écriture cible ajoutée (§7.5 ②③④⑤⑦) :
  `create_and_attach_volume`, `attach_volume`, `detach_volume`, `create_vm`,
  `power_on`, `power_off`, plus `get_vm` (nécessaire pour situer la VM
  cible au même endroit que la VM relais — voir plus bas). Chaque champ
  vient du document constructeur (`docs-fusioncompute/FusionCompute_8.10.0
  _VRM_APIs.docx`, extrait et relu section par section ce jour), y compris
  **⑦ démarrage/arrêt de VM** (`action/start`, `action/stop`) qui manquait
  entièrement à la première extraction du §7.5 — ARCHITECTURE.md corrigé
  en conséquence. 7 nouveaux tests unitaires (FakeSession, même style que
  le reste du fichier) en plus des 15 déjà existants ; `pytest tests` :
  74 passés, 3 sautés au total (contre 67 avant ce jour).
- **`api/models.py`** — `JobLogEntry` (nouvelle table, migration `0003`) :
  seul ajout de schéma nécessaire, le reste (`jobs`, `job_disks`,
  `sync_rounds`, `disk_rounds`, `extents`, `relays`...) existait déjà en
  base depuis `0001` (modélisé le 16/09/2026, jamais exposé par l'API).
- **`api/routers/relays.py`** — CRUD minimal pour enregistrer une VM relais
  (`fc_vm_uri`, `agent_url`, `target_id`). Registre seul : voir plus bas ce
  qui manque encore pour qu'un relais serve à quelque chose de réel.
- **`api/routers/jobs.py`** — machine à états complète (§5) : création
  depuis une VM de plan, provisioning (lecture des disques source réels via
  `vsphere_bridge.list_disks`, nouvelle fonction ; appel réel à
  `create_and_attach_volume` si un relais est assigné), contexte
  d'exécution pour `job-run`, réception du rapport de fin de round,
  bascule (`POST /jobs/{id}/cutover` : extinction propre de la source via
  `shutdown_guest`, déjà existant et éprouvé), finalisation (détachement du
  relais, création de la VM cible, réattachement, démarrage optionnel),
  rollback (rallumage de la source, jamais de suppression côté cible — I1),
  journal (`POST`/`GET /jobs/{id}/logs`). 14 nouvelles routes, `GET
  /api/v1/openapi.json` vérifié sans erreur d'import (37 routes au total,
  contre 23 avant ce jour).
- **`tools/v2fc_source.py`** — nouvelle sous-commande `job-run` : exécute
  un round pour un job piloté par l'API, en appelant directement `cmd_sync`
  (aucune réimplémentation — la fonction déjà validée sur vCenter réel ce
  jour, §5 sexies) avec un contexte (VM, round à faire) obtenu de l'API
  plutôt que tapé à la main, et relaie chaque ligne de log vers `POST
  /jobs/{id}/logs` (`_ApiLogHandler`, best-effort). Rapporte le résultat
  (statut, octets, changeId par disque lu dans le fichier d'état déjà
  écrit) à l'API en fin de round. **Ne change rien à la discipline
  existante** : le mot de passe vCenter reste fourni par Nicolas via
  `V2FC_VC_PASSWORD`, jamais par l'API (§13) ; le jeton d'API
  (`V2FC_API_TOKEN`, obtenu par `POST /auth/login` comme l'UI web) suit la
  même règle de ne jamais passer en argument de ligne de commande.
  `cmd_sync` a reçu un paramètre optionnel `stats` (rempli en place,
  défaut `None`) pour remonter Mio écrits/sautés/durée à `job-run` sans
  changer son comportement pour la CLI manuelle existante.
- **`web/src/pages/Migration.tsx`** et **`web/src/pages/Logs.tsx`** —
  les deux onglets demandés. Migration : par plan ayant une cible, liste
  les VM, propose de créer un job (si datastore/réseau déjà choisis pour
  cette VM), de le provisionner, affiche la commande `job-run` exacte à
  copier-coller selon l'état du job (seed/delta/final), et les actions
  « Migrer maintenant » (avec case à cocher redémarrage automatique) et
  rollback. Logs : page par job (poll 2 s) + un onglet Logs global listant
  les jobs actifs. **Cette UI ne lance jamais elle-même le round** : le
  VDDK tourne dans un conteneur Docker séparé sur le poste de Nicolas
  (api/Dockerfile ne le charge pas), donc la page ne peut qu'afficher la
  commande à lancer, pas cliquer un bouton qui l'exécuterait à distance.
  `npm run build` : `tsc -b` propre (aucune erreur de type) ; le bundle
  `vite build` se construit correctement (vérifié en sortie alternative,
  `/tmp` de ce bac à sable) — le seul échec rencontré vient d'un `dist/`
  résiduel appartenant à un autre utilisateur (probablement un ancien
  `docker compose build` exécuté par Nicolas), un souci de permissions
  local à ce bac à sable, sans rapport avec le code ; supprimer
  `v2fc/web/dist/` avant le prochain `docker compose build` si le même
  symptôme apparaît chez Nicolas.

### Ce qui reste délibérément non exercé — pas un manque caché, une limite honnête

- **Aucune VM relais réelle n'existe** chez Nicolas à ce jour : le
  provisioning FusionCompute réel (`create_and_attach_volume`) et la
  finalisation (`create_vm`, `attach_volume`, `power_on`) sont du vrai
  code, cité, unitairement testé — mais **jamais exécutés contre un VRM
  réel**, exactement comme le driver de lecture avant le 17/09/2026. Sans
  relais assigné à un job, `POST /jobs/{id}/provision` saute sciemment la
  partie FusionCompute (log `warning` explicite) et le job passe directement
  en SEEDING : le round écrit alors dans le `--target-dir` local que
  Nicolas fournit à `job-run`, exactement comme la validation du jour
  (§5 sexies) — utilisable dès maintenant pour roder le flux complet
  (créer un job, seed, delta, bascule, logs) sans attendre le relais.
- **`agent_url` du modèle `Relay` reste un champ réservé** : aucun agent
  réseau tournant dans une VM FusionCompute pour recevoir des extents à
  distance n'a été écrit (ç'aurait été deviner un protocole sans
  spécification ni appelant réel — §13 mentionne gRPC/mTLS mais aucun
  détail de message n'existe). Tant que ce relais n'existe pas, un job
  provisionné écrit quand même en local via `job-run`, pas vers un vrai
  volume FusionCompute distant.
- **`_finalize` (bascule finale)** appelle `get_vm` sur la VM relais pour
  situer la VM cible au même endroit (cluster/hôte) — c'est la seule
  méthode documentée pour choisir un emplacement de VM sans qu'aucune
  autre option n'ait été demandée ; à revoir si Nicolas veut un
  emplacement différent pour la VM cible.
- **Aucun test d'intégration base de données** pour `api/routers/jobs.py`
  (pas d'infrastructure de test avec une vraie base dans ce dépôt — les
  tests existants n'en ont jamais eu besoin jusqu'ici, seulement des
  fausses sessions HTTP). Validé uniquement par l'import de l'application
  (37 routes, aucune erreur) et par relecture attentive — **à tester pour
  de vrai par Nicolas** dès qu'un relais (même provisoire) est disponible.

### Prochaine étape suggérée à Nicolas

Roder le flux dès maintenant sans relais (créer un plan avec cible +
mapping, créer un job, le provisionner, lancer `job-run --round seed`
puis `--round delta` depuis le terminal comme pour la validation L1,
regarder l'onglet Logs) — cela valide déjà la mécanique état/API/UI de
bout en bout. Le relais réel (VM dans FusionCompute + décision sur le
protocole agent, §13) reste la pièce qui manque pour une bascule
FusionCompute complète, à traiter quand Nicolas sera prêt à la construire.


## 17. Bug report Nicolas (17/09/2026, soir) — mapping "Enregistrer" muet + 404 sur Migration/Logs

Nicolas a testé les nouveaux onglets et signalé deux symptômes en même
temps, avec captures d'écran : (a) sur la page cible FusionCompute d'un
plan, cliquer "Enregistrer" pour le mapping datastore/réseau d'une VM ne
semble rien faire ; (b) les onglets Migration et Logs affichent tous les
deux `Error: 404 : Not Found`. Diagnostic fait par lecture de code (pas de
console navigateur disponible depuis cette session) — deux causes
distinctes, sans lien entre elles.

### (b) 404 sur Migration/Logs — conteneur `api` pas reconstruit

`docker-compose.yml` : le service `web` monte `./web:/work` en volume (Vite
tourne en mode dev, sert les fichiers depuis le disque — un nouvel onglet
comme `Migration.tsx`/`Logs.tsx` apparaît donc sans rebuild). Le service
`api` n'a **aucun montage de ce genre** : `api/Dockerfile` fait `COPY api/
/work/api/` à la construction de l'image, donc tout code Python ajouté
après coup (`api/routers/jobs.py`, `api/routers/relays.py`, la migration
`0003`) reste invisible tant que l'image n'est pas reconstruite. Un
conteneur `api` démarré avant le socle L2 de ce jour (§16) ne connaît
tout simplement pas les routes `/api/v1/jobs` et `/api/v1/relays` → 404
"Not Found" littéral (le format `${status} : ${detail}` de `web/src/api.ts`
correspond exactement à la réponse 404 par défaut de FastAPI pour une
route inexistante, pas à une erreur applicative). **Correctif côté
Nicolas, pas de code à changer** :

```
docker compose build api
docker compose up -d api
```

(`web` n'a pas besoin d'être reconstruit ni redémarré pour les fichiers de
ce jour.)

### (a) "Enregistrer" muet sur le mapping — vrai bug, corrigé

Bug réel trouvé dans `web/src/pages/PlanTarget.tsx`, indépendant du point
(b). `saveMapping(vmId)` lisait uniquement `pendingMapping[vmId]` (l'état
qui change quand on touche un `<select>`) pour construire la requête, avec
`ds?.urn ?? null`. Or les captures montrées par Nicolas affichaient déjà
les libellés "actuel : LUN-1000-1" / "actuel : managePortgroup" — ce qui,
d'après le JSX (`{vm.tgt_datastore_name && !pending.datastoreUrn && ...}`),
signifie que le mapping était **déjà enregistré** (d'une session
précédente) et que `pendingMapping` était vide puisque les `<select>`
n'avaient pas été retouchés depuis le chargement de la page. Dans ce cas,
`pending.datastoreUrn` vaut `""`, donc `ds` ne trouve rien, donc l'ancien
code envoyait `datastore_urn: null` / `network_urn: null`. Côté API,
`routers/plans.py::set_plan_vm_mapping` ignore volontairement un champ
`None` (`if body.datastore_urn is not None: ...`) — comportement correct
en soi (pensé pour un PATCH partiel), mais qui fait que ce clic précis ne
changeait rigoureusement rien, sans la moindre erreur : exactement le
symptôme "rien ne se passe" décrit par Nicolas.

Corrigé dans `saveMapping` : la valeur envoyée retombe maintenant sur
`vm.tgt_datastore_urn`/`vm.tgt_network_urn` (celle réellement affichée
dans le `<select>`, mêmes règles que le calcul JSX `currentDs`/`currentNet`)
quand `pendingMapping` est vide, au lieu de `null`. `pendingMapping[vmId]`
est aussi effacé après un enregistrement réussi (pour que le libellé
"actuel : ..." puisse réapparaître), et un petit "enregistré ✓" transitoire
(2 s) a été ajouté à côté du bouton — l'absence de tout retour visuel en
cas de succès (le `<select>` affichait déjà la valeur qu'il montre après)
contribuait aussi à donner l'impression que le clic ne servait à rien,
même dans les cas où l'enregistrement aurait réellement changé quelque
chose. Aucun changement côté API. `tsc -b` propre après correction.
**Rien à reconstruire côté Docker pour ce correctif** (fichier servi par
Vite en mode dev) : un rechargement de la page suffit.

### Mise à jour du 17/09/2026 (nuit) — vrai bug trouvé : `relay/` absent de l'image `api`

Après reconstruction de l'image (`docker compose build api`), le conteneur
`api` ne démarrait plus du tout : `ModuleNotFoundError: No module named
'relay'` au chargement de `api/routers/jobs.py`, ligne
`from relay.writer import VolumeWriter` (ce même import ajouté ce jour,
§16, pour réutiliser `VolumeWriter.required_volume_size()` plutôt que
réinventer le calcul de taille de volume). C'est ce qui produisait le 404
« Not Found » sur Migration/Logs : le conteneur `api` ne se lançait pas du
tout, faute d'import réussi, donc l'ancien binaire compilé continuait de
tourner (ou aucun conteneur `api` sain n'écoutait) — pas un problème de
route manquante en tant que tel, mais un crash au démarrage.

Cause : `api/Dockerfile` ne fait `COPY` que de `datamover/` et `api/` vers
`/work/` — jamais de `relay/`, qui vit à la racine du dépôt
(`v2fc/relay/writer.py`), à côté de `datamover/`. En développement local
(hors Docker), l'import réussissait par accident car le répertoire de
travail (`v2fc/`) contient déjà `relay/` et Python l'ajoute implicitement à
`sys.path` — un faux négatif qui a caché le problème jusqu'à ce que
Nicolas reconstruise réellement l'image.

**Corrigé** : `api/Dockerfile` copie maintenant aussi `relay/` :
```
COPY datamover/ /work/datamover/
COPY relay/     /work/relay/
COPY api/       /work/api/
```
Vérifié en reproduisant exactement l'arborescence `/work` de l'image dans
un répertoire neutre (`datamover/` + `api/` seuls → échec identique à celui
de Nicolas ; en ajoutant `relay/` → import réussi, 37 routes). `relay/
writer.py` ne dépend que de la bibliothèque standard (pas de VDDK, pas de
SDK vSphere) — l'inclure dans l'image `api` ne contredit pas l'invariant
« l'API ne charge pas le VDDK » (`api/Dockerfile`, commentaire d'en-tête).

**Prochaine étape pour Nicolas** : `docker compose build api && docker
compose up -d api`, puis vérifier `docker compose logs api --tail 30` (doit
se terminer par les lignes uvicorn "Application startup complete", plus de
traceback), puis recharger Migration/Logs.

### Mise à jour du 17/09/2026 (nuit, suite) — 500 sur Provisionner : même piège que `endpoints.last_probe_at`, réapparu dans `jobs.py`

Une fois l'image `api` reconstruite avec `relay/` (ci-dessus), Nicolas a pu
créer un job (`POST /jobs` → journalisé « job créé pour test_ubuntu depuis
le plan « test_ubuntu » », visible dans l'onglet Logs) mais cliquer
« Provisionner » renvoyait `500 : Internal Server Error`. Trace obtenue via
`docker compose logs api` :

```
asyncpg.exceptions.DataError: invalid input for query argument $2:
datetime.datetime(2026, 9, 17, 10, 36, 4, ..., tzinfo=datetime.timezone.utc)
(can't subtract offset-naive and offset-aware datetimes)
[SQL: UPDATE jobs SET state=$1::VARCHAR, state_since=$2::TIMESTAMP WITHOUT TIME ZONE ...]
```

C'est très exactement le même piège déjà rencontré et corrigé sur
`endpoints.last_probe_at` (commentaire déjà présent dans
`api/routers/endpoints.py`, ligne ~135) : les colonnes `Mapped[datetime]`
de `api/models.py` sont des `TIMESTAMP WITHOUT TIME ZONE` (pas de
`timezone=True` déclaré), et asyncpg refuse d'y écrire un datetime
tz-aware. `api/routers/jobs.py`, écrit ce jour (§16), ne suivait pas cette
convention déjà établie : `_set_state()` (appelée à chaque transition
d'état — provisioning, cutover, rollback, finalisation...) faisait
`job.state_since = datetime.now(timezone.utc)`, et `report_round()` faisait
pareil sur `round_row.ended_at`. Toute transition d'état d'un job, pas
seulement le provisioning, aurait fini en 500 de la même façon.

**Corrigé** : les deux affectations utilisent maintenant
`.replace(tzinfo=None)`, comme le fait déjà `endpoints.py` — un
commentaire renvoie vers cette référence à chaque endroit pour que le
prochain ajout de colonne datetime dans `jobs.py` ne retombe pas dans le
panneau. Vérifié : aucune autre affectation de champ `datetime` dans
`jobs.py` (`JobLogEntry.at`, `SyncRound.started_at` restent sur
`server_default=func.now()`, jamais assignés depuis Python) ; le
round-trip `since` de `GET /jobs/{id}/logs` (Logs.tsx) reste sûr car il
réutilise tel quel le datetime naïf déjà renvoyé par l'API, sans lui
ajouter de fuseau. `pytest tests` toujours 74 passés/3 sautés (ces routes
n'ont toujours pas de test d'intégration base de données réelle — seul un
vrai `docker compose` avec Postgres, comme celui de Nicolas, peut
détecter ce genre de défaut ; à garder en tête pour la suite de L2).

**Prochaine étape pour Nicolas** : `docker compose build api && docker
compose up -d api` (fichier Python, donc image à reconstruire — pas
comme le correctif `PlanTarget.tsx` plus haut, qui ne demandait qu'un
rechargement de page), puis reprendre le provisioning du job `test_ubuntu`.

### Mise à jour du 17/09/2026 (nuit, suite 2) — 500 sur Provisionner : colonnes octets en INTEGER au lieu de BIGINT

Après le correctif précédent, le provisioning a bien démarré et journalisé
« provisioning démarré », mais a échoué au moment d'enregistrer le disque
source lu réellement via vSphere. Trace :

```
asyncpg.exceptions.DataError: invalid input for query argument $6: 4294967296
(value out of int32 range)
[SQL: INSERT INTO job_disks (..., capacity_bytes, ...) VALUES (...)]
[parameters: (..., 4294967296, ...)]
```

`4294967296` = 4 Gio, la capacité réelle du disque de `test_ubuntu` — et
`job_disks.capacity_bytes` était un `INTEGER` (int4 Postgres, max
2 147 483 647), pas assez pour stocker une capacité de disque en octets dès
que le disque dépasse ~2 Gio. En creusant, le même défaut existait sur
toutes les autres colonnes « quantité d'octets » du schéma (§8.1, modélisé
depuis `0001`, jamais exercé contre une vraie base avant ce soir) :
`sync_rounds.bytes_planned`/`bytes_transferred` (le seed `test_ubuntu`
d'hier, 2 142 Mio, dépasse déjà 2^31 lui aussi — ce n'était encore jamais
passé par l'API), `disk_rounds.bytes_planned`/`bytes_transferred`/
`cursor_offset`, `extents.start_offset`/`length`. Ces trois derniers ne
sont pas encore écrits par `api/routers/jobs.py` aujourd'hui, mais
partagent exactement le même risque dès qu'ils le seront — corrigés
maintenant pour ne pas relaisser la même mine.

**Corrigé** :
- `api/models.py` : ces colonnes passent de `Mapped[int]` implicite
  (`INTEGER`) à `mapped_column(BigInteger)` explicite. Restent en
  `INTEGER` (ce ne sont pas des quantités d'octets) : `device_key`,
  `unit_number`, `round_no`, `extent_count`, `cursor_index`, `idx`, `seq`,
  `max_slots`/`used_slots`, `port`, `cbt_reseed_count`.
- `api/alembic/versions/0004_bigint_byte_columns.py` (nouvelle migration) :
  `ALTER COLUMN ... TYPE BIGINT` sur les 8 colonnes concernées.
  Élargissement pur (int4 → int8), aucune perte de données, pas de
  `postgresql_using` nécessaire.

Vérifié : `alembic history` résout bien la chaîne 0001→0002→0003→0004
(`PYTHONPATH=<racine v2fc> python3 -m alembic history` depuis `api/`) ;
import de l'application simulé dans l'arborescence exacte de l'image
Docker (`datamover/` + `relay/` + `api/`) confirmant les 8 colonnes en
`BIGINT` ; `pytest tests` toujours 74 passés/3 sautés. C'est la troisième
chose que le premier vrai test contre un Postgres réel (celui de Nicolas,
pas les fausses sessions HTTP des tests unitaires) a mise au jour ce
soir — cohérent avec la mise en garde déjà écrite au §16 sur ce point.

**Prochaine étape pour Nicolas** : `docker compose build api && docker
compose up -d api` (nouveau code Python + nouvelle migration), puis
reprendre le provisioning du job `test_ubuntu`.


## 18. Décision d'architecture : relais = VM applicative colocalisée, pas d'agent réseau (17/09/2026, nuit)

Nicolas a posé la question directement : pourquoi prévoir un relais séparé
si la VM Ubuntu qui héberge l'application elle-même vit déjà dans le
cluster FusionCompute (DCS) où la cible doit être écrite ? Après analyse,
la réponse est oui, ça marche, et c'est même préférable au design décrit
jusqu'ici — **Nicolas a confirmé, ARCHITECTURE.md mis à jour en
conséquence** (§3, §7.1, §12 nouvelle décision D7, §13, §14 L2).

Le raisonnement : un volume FusionCompute ne peut être attaché qu'à une VM
du même cluster (contrainte de l'hyperviseur, pas un choix d'architecture).
Si cette VM est aussi celle qui exécute le Data Mover (lecture VDDK — il ne
lui faut qu'un accès réseau routé vers le vCenter/ESXi source, exactement
la même condition que le poste utilisé aujourd'hui pour les validations
L1), lecture et écriture se retrouvent dans le même processus : c'est le
POC « tout-en-un » déjà validé deux fois (§5 sexies), simplement exécuté
depuis une VM du cluster plutôt que depuis un Mac. Le protocole réseau
Data-Mover-vers-Agent (gRPC/mTLS, §13 de ce document) — jamais écrit,
`Relay.agent_url` toujours un champ réservé sans code derrière (§16) — n'a
donc plus lieu d'être construit : cette VM s'enregistre simplement comme
`Relay` existant (`POST /relays`, déjà implémenté) via son propre
`fc_vm_uri`, et exécute `job-run` localement.

**Conséquence concrète pour la suite du projet** : le plus gros morceau
encore ouvert au §14 (« pièce qui manque pour une bascule FusionCompute
complète », dernière ligne du §16 ci-dessus) n'est plus « concevoir et
écrire un protocole d'agent réseau », mais bien plus simple : déployer une
VM Ubuntu x86-64 dans le cluster DCS avec Docker + le VDDK (comme le
Data Mover tourne déjà, `install/build-datamover.sh`), s'assurer qu'elle a
une route réseau vers le vCenter/ESXi source, l'enregistrer comme relais
(une fois son `fc_vm_uri` VRM connu), puis lancer `job-run` depuis
celle-ci au lieu du Mac de Nicolas. Aucun nouveau code à écrire pour cette
partie — seulement du déploiement.

**Ce qui n'a pas changé** : le driver d'écriture FusionCompute
(`create_and_attach_volume`/`attach_volume`/`detach_volume`/`create_vm`)
reste non testé contre un VRM réel (même limite que §16) ; D5 (sérialisation
SCSI des volumes dans le relais) reste ouvert et se testera sur cette même
VM une fois déployée.

**Procédure d'installation écrite** dans `install/README.md`, nouvelle section « Déploiement complet sur une VM à l'intérieur du cluster FusionCompute (relais colocalisé) » : réseau (accès aux deux environnements, source et cible), Docker, transfert de fichiers (pas de dépôt git, copie directe), secrets, `CORS_ORIGINS` à corriger pour un accès distant, enregistrement du relais via appel API direct (pas encore de formulaire dans l'interface). Nicolas gère lui-même l'installation de l'OS ; cette VM hébergera l'application complète (API/web/Postgres/Redis) en plus du rôle Data Mover/relais, à sa demande explicite du 17/09/2026.

## 19. Choix du relais à la création d'un job (26/10/2026)

À la demande de Nicolas, la page Migration propose maintenant un menu
« Relais » avant « Créer un job » : il liste les relais enregistrés sur
l'endpoint FusionCompute cible du plan (premier proposé par défaut), plus
« aucun (écriture locale) ». Avant, l'UI prenait silencieusement
`relays[0]`, quel que soit son endpoint. Le relais assigné est affiché sur
chaque job existant. Côté API, `POST /jobs` refuse désormais (400) un
relais qui n'appartient pas à l'endpoint cible du plan. L'enregistrement
d'un relais se fait toujours par `POST /relays` (pas de formulaire).

## 20. Provisioning FusionCompute : URN de datastore et échec masqué (26/10/2026)

Premier provisioning réel (job R00vSkypeEd, relais `i-0000000D`) :
`createattachvol` refusé par le VRM (`10000001`, « format de requête
invalide »), mais le job passait quand même en SEEDING — un seed aurait
écrit 100 Go en local sans le dire.

- **Cause** : `GET <host>/datastores` renvoie un identifiant nu (`"urn": "5"`),
  repris tel quel comme `datastoreUrn`. `GET <site>/datastores/5` donne la
  vraie URN `urn:sites:0A7D0FDA:datastores:5`. `list_datastores()` et
  `create_and_attach_volume()` complètent désormais l'URN (`_full_urn`),
  ce qui couvre aussi les plans déjà enregistrés avec `5`.
- **Échec visible** : une création de volume en erreur fait passer le job
  en FAILED (502), au lieu de SEEDING.
- **Pas encore vérifié** : la création d'un volume avec l'URN complète n'a
  pas été testée sur le VRM (essai bloqué côté permissions de la session).

## 21. Rounds automatiques : écriture sur le relais + service runner (26/10/2026)

Demande de Nicolas : plus de commande `job-run` à copier, « ça doit être
en automatique ». Validé sur le VRM réel avant d'écrire le code :
`createattachvol` (URN complète, §20) crée et attache un volume VIRTIO à la
VM relais `i-0000000D` (= `dc1-migration`).

- **D5 tranché** : le volume apparaît sous
  `/dev/disk/by-id/virtio-<20 premiers caractères de son UUID>` (série
  virtio). `fusioncompute.guest_disk_path()` ; le provisioning lit l'UUID
  (`get_volume`) et enregistre ce chemin dans `job_disks.tgt_dev_path`.
- **Écriture** : `run-context` renvoie les volumes ; `job-run` écrit dessus
  au lieu de `disk-<key>.raw`, après avoir déposé la signature §7.2 au seed
  (identifiant = URI du volume). Testé sur un volume réel de 1 Go depuis le
  conteneur runner : O_DIRECT, relecture identique, mauvaise signature
  refusée ; volume ensuite détaché et supprimé.
- **Correctif** : `Mode.SEED` (zones nulles sautées) seulement pour un état
  vierge — un re-seed après CBT invalidé écrit désormais les zéros.
- **Runner** (`tools/v2fc_runner.py`, service `runner`, privilégié, /dev et
  VDDK montés) : interroge `GET /api/v1/runner/work` (seed en SEEDING,
  reprise en SYNCING, delta toutes les `V2FC_DELTA_INTERVAL_MIN` minutes en
  SYNCED, final en FINAL_DELTA ; pause de 5 min après un échec) et lance
  `job-run`. Authentifié par `V2FC_RUNNER_TOKEN` ; seul lui reçoit les
  identifiants source (`/runner/jobs/{id}/source-credentials`). Seuls les
  jobs avec relais et volumes sont automatisés ; sans relais, la page
  affiche toujours la commande.
- **Non testé** : un seed réel bout en bout (snapshot + VDDK nbdssl depuis
  cette VM vers les ESXi source, port 902) ; `_finalize` (création de la VM
  cible) ; la signature n'est pas effacée avant la bascule.

## 22. Premier seed réel bout en bout + barre de progression (26/10/2026)

**Seed réel réussi** : R00vSkypeEd (PLAN1), 100 Gio provisionnés, 44,7 Gio
copiés en ~10 min à 69-73 Mio/s (nbdssl depuis `dc1-migration`, écriture
sur le volume FusionCompute 70 via le relais). Job en SYNCED.

**Bug corrigé** : `PATCH /jobs/{id}/rounds/{rid}` créait des `DiskRound`
sans `status` (colonne NOT NULL) → 500 ; le job restait en SEEDING et le
runner a relancé le seed 4 fois (chaque passage complet et correct). Corrigé
(`status=body.status`). Garde-fous : `job-run` sort en erreur si l'API
refuse le rapport, et le runner attend `V2FC_RUNNER_COOLDOWN_S` (300 s) avant
de relancer un round du même job.

**Barre de progression** (demande de Nicolas) : `cmd_sync` calcule les plans
de tous les disques avant la copie (total du round connu d'emblée) ;
`job-run` envoie `POST /jobs/{id}/rounds/{rid}/progress` toutes les 5 s
(`bytes_planned`, `bytes_done`, stockés dans `sync_rounds` tant que le round
tourne). La page Migration affiche barre, %, Gio, débit et temps restant.

## 23. Relais détecté automatiquement + compte FusionCompute (26/10/2026)

- **Auto-enregistrement du relais** : constaté sur le VRM de Nicolas,
  `/sys/class/dmi/id/product_uuid` de la VM = champ `uuid` de la VM dans
  FusionCompute (`DC1_MIGRATION`, `2ae09df9-…`). Le runner envoie cet UUID à
  `POST /api/v1/runner/register` au démarrage puis toutes les 10 min ; l'API
  vérifie d'abord les relais connus (`get_vm`), sinon parcourt les VM du site
  (`list_vms`, paginé), et crée ou passe ONLINE le relais correspondant.
  Remplace l'appel `POST /relays` manuel de `install/README.md`.
- **Compte d'interconnexion bloqué en `10000041`** (changement de mot de
  passe imposé, impossible pour ce type de compte qui n'a pas accès au
  portail) : procédure ajoutée dans `install/README.md` §5 (désactiver le
  changement obligatoire du mot de passe initial *avant* de créer le compte).
  Aucune API documentée sous la main pour modifier cette politique : c'est
  une étape manuelle. Le driver traduit désormais `10000041`, `10100103` et
  `10100380` en français dans ses messages d'erreur.

## 24. Première bascule réelle : finalisation corrigée (26/10/2026)

R00vSkypeEd (PLAN1) : bascule lancée par Nicolas, delta final OK (VM déjà
éteinte, 0 Mio), puis `create_vm` refusé par le VRM : `10300069` « le type
de système d'exploitation ne peut pas être vide ». `_finalize`, jamais
exercé, avait aussi d'autres défauts. Corrigé :

- `osOptions` obligatoire : code pris dans `GET /vms/osversions` (endpoint
  constaté sur le VRM) par `fusioncompute.pick_os_version()`, d'après le
  `guestFullName` vSphere (Tools si disponible) ; repli « Other ».
- CPU, mémoire et firmware repris de la VM source (étaient 2 vCPU / 4 Go /
  BIOS en dur).
- Disques attachés en **IDE** (3 premiers), VIRTIO au-delà : une VM venue de
  VMware n'a pas de pilote virtio (Windows : INACCESSIBLE_BOOT_DEVICE).
  Passer en VIRTIO après installation des outils FusionCompute.
- Finalisation reprenable : `POST /jobs/{id}/finalize` (bouton « Relancer la
  finalisation ») ; étapes déjà faites sautées (volume déjà détaché, VM déjà
  créée — `tgt_vm_uri` enregistré dès la création —, volume déjà attaché).

Résultat : VM `i-0000000E` « R00vSkypeEd » créée éteinte, 6 vCPU, 32 Go,
BIOS, Windows Server 2016, volume 70 (101 Gio) en IDE, une carte sur le port
group DMZ. **Pas encore vérifié** : le démarrage de la VM. Restent ouverts :
une seule carte réseau créée (la source en a deux), de type virtio (pas de
réseau sous Windows avant les outils FusionCompute) ; la signature V2FC
reste dans le dernier secteur du volume (hors des 100 Gio de données).
