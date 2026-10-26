# V2FC — Architecture & spécification technique

**Application web de migration VMware vSphere → Huawei FusionCompute**
Seed en ligne · resynchronisations delta · bascule à froid · source intacte

| | |
|---|---|
| Version du document | 0.6 — 15/09/2026 (D1 et D3 résolus ; CBT mesuré sur plateforme réelle) |
| Auteur | Nvaestech SRL |
| Statut | Conception — à valider avant lotissement |
| Décisions actées | Lecture VMware par **VDDK** · Écriture FC par **VM relais + agent** · Stack **FastAPI + React + PostgreSQL/Redis** |

---

## 1. Résumé exécutif

L'application orchestre une migration **en trois temps** : une copie initiale complète pendant que la VM source tourne, une ou plusieurs resynchronisations incrémentales également à chaud, puis un dernier delta à froid au moment de la bascule. La source n'est jamais détruite ni modifiée en écriture : le rollback consiste à rallumer la VM d'origine.

Le mécanisme central est le **Changed Block Tracking (CBT)** de vSphere. Chaque cycle de synchronisation produit un `changeId` persisté ; le cycle suivant demande à vCenter uniquement les extents modifiés depuis ce `changeId`, ce qui réduit le dernier delta — donc la fenêtre d'indisponibilité — à quelques minutes. *(confiance : 9/10 — mécanisme documenté et universellement utilisé par les logiciels de sauvegarde)*

Côté FusionCompute, il n'existe pas d'équivalent public du VADP pour **écrire** à des offsets arbitraires sans SDK partenaire. La solution retenue contourne le problème : une **VM relais** dans le cluster FusionCompute reçoit les volumes cibles en attachement SCSI direct, et un agent y écrit les blocs par `pwrite()` aux offsets exacts. Seules les API REST VRM documentées (créer un volume, l'attacher, le détacher) sont nécessaires. *(confiance : 8/10 — architecture éprouvée par les outils de réplication tiers ; les chemins d'API exacts restent à confirmer, cf. §12)*

**Trois constats qui conditionnent le projet, à lire avant toute chose :** §2.

---

## 2. Constats préalables et points de blocage

### 2.1 VDDK Linux — présent, validé, exploitable en Python *(D1 résolu)*

Le dossier contient les **deux** distributions du VDDK 8.0.3 build 23950268 (mai 2024) :

| Archive | Plateforme | Usage |
|---|---|---|
| `VMware-vix-disklib-8.0.3-23950268.x86_64/` | Windows x86-64 (`.dll`, `.lib`, `vstor2-x64.sys`) | non utilisée |
| `VMware-vix-disklib-8.0.3-23950268.x86_64.tar.gz` | **Linux x86-64** (`libvixDiskLib.so.8.0.3`) | **retenue** |

Empreinte de l'archive Linux, à conserver avec la sauvegarde hors poste (§2.4) :
`sha256 = aadcf8fcfa5fc0ca972283334a273aa49fff96fa2eaadb30b8cc0c71705c4754`

Le Data Mover sera donc un composant **Linux**, comme le reste de l'application. Pas de service Windows à maintenir.

**Validations effectuées** (exécutées réellement, pas déduites) :

| Contrôle | Résultat |
|---|---|
| Nature du binaire | `ELF 64-bit LSB shared object, x86-64, stripped` |
| Dépendances partagées | toutes résolues, **aucune** `not found` (le paquet embarque OpenSSL 3, curl, expat, stdc++, vmacore, vmomi) |
| glibc minimale requise | **2.16** → compatible Debian 12, Rocky 9, Ubuntu 22.04+ |
| Symboles exportés `VixDiskLib_*` | 46, versionnés (`VIXDISKLIB_1.0` → `6.7`) |
| Fonctions critiques présentes | `InitEx`, `ConnectEx`, `Open`, `Read`, **`ReadAsync`** + `Wait`, `QueryAllocatedBlocks`, `FreeBlockList`, `PrepareForAccess`, `EndAccess`, `ListTransportModes`, `GetTransportMode`, `IsAttachPossible`, `AllocateConnectParams` |
| Binaires utilitaires | `vixDiskCheck`, `vddkReporter`, `vmware-vdiskmanager` s'exécutent |
| Liaison **`ctypes` Python** | `VixDiskLib_InitEx(8, 0, …)` → **`VIX_OK`**, allocation/libération de `ConnectParams` OK |

**Conséquence de conception :** pas besoin de `cgo`, ni de shim C, ni de service séparé. Le Data Mover peut être un **module Python liant le VDDK par `ctypes`**, dans le même écosystème que `pyVmomi`. Utiliser `VixDiskLib_AllocateConnectParams()` plutôt que de recomposer la structure `VixDiskLibConnectParams` à la main : sa disposition mémoire contient des champs `reserved`/`state` internes et une union, et elle a déjà changé entre versions majeures.

### 2.2 Deux pièges qui privent silencieusement des modes `san` et `hotadd`

Découverts en validation. C'est le genre de défaut qui coûte une semaine de diagnostic de performance, parce qu'il ne produit **aucune erreur** : `InitEx` renvoie `VIX_OK`, la migration fonctionne, elle est simplement trois à dix fois plus lente.

Le plugin de transport avancé `libdiskLibPlugin.so` — celui qui apporte `san` et `hotadd` — ne se charge que si **deux conditions indépendantes** sont réunies.

**Condition 1 — `libDir` = racine de la distribution.**
`VixDiskLib_InitEx()` concatène `lib64/` au chemin fourni. Passer `…/vmware-vix-disklib-distrib/lib64` le fait chercher `…/lib64/lib64/libdiskLibPlugin.so`.

**Condition 2 — `LD_LIBRARY_PATH` doit contenir `lib64` avant le démarrage du processus.**
Vérifié : le VDDK ne pose **aucun `RPATH` ni `RUNPATH`** sur `libvixDiskLib.so` ni sur `libdiskLibPlugin.so`. Les dépendances du plugin (`libvapi-core.so.2`, `libvmacore.so`, `libvmomi.so`, `libvim-types.so`, `liblookup-types.so`…) sont alors introuvables. `libvixDiskLib` elle-même se charge sans problème par chemin absolu — d'où le faux sentiment de sécurité.

```
libDir=…/distrib/lib64                    → file:nbdssl:nbd
  | Could not load default plugins from …/lib64/lib64/libdiskLibPlugin.so

libDir=…/distrib, sans LD_LIBRARY_PATH    → file:nbdssl:nbd
  | Could not load default plugins from …/lib64/libdiskLibPlugin.so:
  |   Cannot open library: libvapi-core.so.2: No such file or directory

libDir=…/distrib, LD_LIBRARY_PATH=…/lib64 → file:san:hotadd:nbdssl:nbd   ✓
  | Advanced transport plugin … was successfully loaded into vixDiskLib.
  | Enabling advanced transport modes.
```

**Ne pas tenter de contourner la condition 2 depuis Python.** Modifier `os.environ["LD_LIBRARY_PATH"]` à l'exécution est sans effet : l'éditeur de liens dynamique le lit au démarrage du processus. Et précharger les bibliothèques en `RTLD_GLOBAL` pour court-circuiter la résolution **provoque un SIGSEGV** (testé, avec et sans `fips.so`). La variable doit être posée par le lanceur : `Environment=` dans l'unité systemd, `ENV` dans le Dockerfile, ou un script d'entrée.

**Règles à graver dans le code :**

1. `libDir` = racine de la distribution ; `LD_LIBRARY_PATH` posé par le lanceur.
2. Au démarrage du Data Mover, appeler `VixDiskLib_ListTransportModes()` et **refuser de démarrer** si un mode exigé par la configuration est absent. Un démarrage qui réussit en silence sur un mode dégradé est un défaut, pas une tolérance.
3. Après ouverture de chaque disque, journaliser `VixDiskLib_GetTransportMode()` — le mode **réellement négocié**, qui peut différer du mode demandé.

Corollaire : la liste des transports dépend de l'environnement d'exécution, pas de la bibliothèque. Sur un hôte sans contexte vSphere ni LUN visible, `hotadd` et `san` peuvent être listés par la bibliothèque et échouer à la négociation. Le contrôle se fait **sur l'hôte du Data Mover**, à l'exécution — d'où l'outil `tools/vddk_probe.py` livré avec ce dossier.

### 2.3 bis Deux pièges de liaison `ctypes` qui font avorter le processus

Le VDDK renvoie certaines chaînes que l'appelant doit libérer lui-même. Avec
`restype = c_char_p`, **ctypes recopie la chaîne en objet `bytes` Python et
perd le pointeur d'origine** ; rendre cet objet à la fonction de libération
revient à appeler `free()` sur de la mémoire appartenant à Python. La glibc
tue alors le processus sur `munmap_chunk(): invalid pointer`.

Le cas s'est produit sur `VixDiskLib_GetErrorText` / `FreeErrorText`, et il
était particulièrement pervers : **c'est le chemin de remontée d'erreur qui
plantait**, donc le processus mourait avant d'avoir pu rapporter l'erreur
réelle qu'il était en train de traiter. Un plantage sans cause visible.

La forme correcte, pour toute chaîne à libérer :

```python
lib.VixDiskLib_GetErrorText.restype = c_void_p        # PAS c_char_p
lib.VixDiskLib_FreeErrorText.argtypes = [c_void_p]
...
ptr = lib.VixDiskLib_GetErrorText(err, None)
text = ctypes.string_at(ptr).decode(errors="replace")  # lecture sans copie du pointeur
lib.VixDiskLib_FreeErrorText(c_void_p(ptr))            # on rend le MÊME pointeur
```

`c_char_p` reste correct pour `ListTransportModes` et `GetTransportMode`, dont
la documentation précise que l'appelant ne doit **pas** libérer le retour.

En revanche, `VixDiskLib_FreeConnectParams` ne libère **pas** les chaînes
membres de la structure : l'exemple officiel du VDDK y range directement des
pointeurs d'`argv`, qu'il serait interdit de libérer. Les affecter depuis des
`bytes` Python est donc sans danger, à condition de garder ces objets en vie
tant que la connexion existe.

### 2.3 Journalisation du VDDK — callback `va_list`

Les callbacks `log`/`warn`/`panic` de `InitEx` ont la signature `void (*)(const char *fmt, va_list args)`. Une liaison naïve en `ctypes` ne récupère que `"%s"` : le texte est dans la `va_list`. Il faut le reconstituer :

```python
libc.vsnprintf.argtypes = [c_char_p, c_size_t, c_char_p, c_void_p]
def log_cb(fmt, ap):
    buf = create_string_buffer(4096)
    libc.vsnprintf(buf, 4096, fmt, ap)
    logger.info(buf.value.decode(errors="replace").strip())
```

Sans cela, aucun diagnostic VDDK exploitable — et c'est précisément ce mécanisme qui a révélé le piège `libDir` du §2.2.

### 2.4 Broadcom a fermé l'accès public au VDDK

Les pages de téléchargement du VDDK renvoient 404 depuis fin août 2026, sans annonce de dépréciation. Red Hat a publié un article de support le 27/08/2026, plusieurs éditeurs (ShapeBlue, Platform9, Hystax) ont confirmé. L'accès passe désormais par le Technology Alliance Program de Broadcom. *(confiance : 8/10 — sources concordantes, mais situation mouvante)*

**Conséquences pratiques :**

- Les deux archives 8.0.3 en votre possession sont un **actif critique** : les sauvegarder hors du poste, avec l'empreinte SHA-256, et les versionner comme une dépendance figée. Elles ne sont plus téléchargeables.
- Le VDDK ne peut **pas** être redistribué : l'application ne doit jamais l'embarquer dans une image ou un paquet. Elle le charge depuis un chemin fourni par l'exploitant, qui reste responsable de la licence.
- Prévoir dès la conception une **abstraction `SourceReader`** permettant de substituer une autre implémentation (notamment `openvddk`, réimplémentation clean-room GPL-3.0 ABI-compatible de `libvixDiskLib`, qui annonce le support CBT et les transports NBD/SAN/HotAdd — *maturité non vérifiée indépendamment, confiance 5/10, à valider en laboratoire*).

### 2.5 Plateforme de laboratoire — relevée, pas supposée *(D3 résolu)*

Relevé du 15/09/2026 sur l'hôte de test, par `tools/lab_report.py --probe` :

| | |
|---|---|
| Hôte | VMware ESXi **8.0.3 build 24022510**, `embeddedEsx` |
| API | **8.0.3.0**, type **HostAgent** (connexion directe à l'hôte, pas à un vCenter) |
| Licence | **vSphere 8 Enterprise Plus** — l'API n'est donc pas bridée |
| Datacenter | `ha-datacenter`, ressource de calcul `ha-compute-res` |
| VM de test | `ubuntu`, 25 Gio, **firmware EFI**, CBT **déjà actif**, allumée |

**Le VDDK 8.0.3 et l'hôte sont de la même version exacte.** La question de la règle N-2 ne se pose donc pas pour ce laboratoire. Elle se reposera si la plateforme cliente est en 9.x.

Deux points relevés qui ont conduit à des contrôles supplémentaires :

**Licence.** Une licence ESXi *gratuite* rend l'API vSphere en lecture seule : ni snapshot par API, ni accès VDDK, et cela ne se voit qu'au premier appel mutant, sous la forme d'une `RestrictedVersion`. Ce laboratoire est en Enterprise Plus, donc hors de cause, mais `lab_report.py` détecte le cas explicitement — c'est la première cause d'échec d'un laboratoire monté rapidement.

**Connexion directe à l'hôte.** L'inventaire contient une VM `vCLS-…`, ce qui signale un hôte rattaché (au moins par le passé) à un cluster vCenter. Or, une session ouverte sur l'ESXi contourne ce vCenter : `VixDiskLib_PrepareForAccess` ne peut alors **pas** suspendre DRS ni vMotion, puisque ces décisions appartiennent au vCenter. Un déplacement décidé en cours de cycle invaliderait la lecture. Acceptable en laboratoire, à proscrire en production — la pré-vérification émet désormais un `CONNEXION_HOTE` de sévérité WARN.

**VM d'agent.** Cette même VM `vCLS` a révélé un manque : elle était refusée, mais pour la mauvaise raison (aucun disque). Les VM d'agent — vCLS, NSX, vSAN, EAM — sont déployées puis recréées automatiquement par vCenter ; les migrer n'a aucun sens. Détection ajoutée : `VM_AGENT`, sévérité BLOCK.

Le premier contrôle, fondé sur `config.managedBy`, **n'a pas déclenché sur la plateforme réelle** : cette propriété est renseignée par vCenter, et la session s'ouvre directement sur l'hôte. Juste en théorie, inopérant en pratique. Un second indice a donc été ajouté, qui survit à ce mode de connexion : le `guestId` **`crxSys1Guest`** désigne un conteneur système CRX, jamais une charge de travail ; plus une détection par préfixe de nom (`vCLS-`, `nsx-`, `vsan-`) en filet. Vérifié : la vCLS est désormais bloquée pour `VM_AGENT`.

### 2.6 Mesures CBT sur plateforme réelle

Relevé sur la VM `ubuntu` (25 Gio, EFI, allumée, CBT préexistant) :

| Grandeur | Valeur mesurée |
|---|---|
| Format du `changeId` | `52 b8 32 ae 98 2a 09 c8-24 28 c8 b6 b7 4d 33 dc/43` — UUID, puis `/` et un numéro de séquence |
| Appels `QueryChangedDiskAreas` pour couvrir 25 Gio | **1 seul** |
| Zones retournées | 70 |
| Volume alloué | 5,6 Gio, soit **22,5 %** de la capacité |
| Création du snapshot de migration | 1,1 s |
| Empreinte acceptée par le VDDK | **SHA-1** |
| `vmxSpec` produit | `ubuntu/ubuntu.vmx?dcPath=ha-datacenter&dsName=DS` |

Deux enseignements. Un seed « used-blocks only » ne transfère ici que 22,5 % du disque — l'économie est réelle et justifie à elle seule de passer par `changeId = "*"` plutôt que par une copie intégrale. Et la boucle de couverture de `QueryChangedDiskAreas` n'a pas été sollicitée à cette taille : elle reste néanmoins indispensable, puisque rien ne garantit ce comportement sur un disque de plusieurs téraoctets, et qu'une couverture partielle non détectée produirait une cible silencieusement incomplète.

---

## 3. Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────────────┐
│                          POSTE OPÉRATEUR                              │
│                        React SPA (WebSocket)                          │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ HTTPS / WSS
┌────────────────────────────────▼─────────────────────────────────────┐
│  ORCHESTRATEUR  (FastAPI, conteneur Linux)                            │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌────────────┐ │
│  │ API REST │ │ Auth/RBAC│ │ Scheduler │ │ State    │ │ Event bus  │ │
│  │          │ │          │ │ de jobs   │ │ machine  │ │ (Redis)    │ │
│  └──────────┘ └──────────┘ └───────────┘ └──────────┘ └────────────┘ │
│                    │                          │                       │
│        ┌───────────▼──────────┐   ┌───────────▼───────────┐          │
│        │ Driver SOURCE        │   │ Driver TARGET         │          │
│        │ (pyVmomi : inventaire│   │ (REST VRM : inventaire│          │
│        │  snapshots, CBT)     │   │  VM, volumes, attach) │          │
│        └───────────┬──────────┘   └───────────┬───────────┘          │
└────────────────────┼──────────────────────────┼───────────────────────┘
        PostgreSQL ──┤                          │
                     │ gRPC/mTLS                │ gRPC/mTLS
        ┌────────────▼─────────────┐  ┌─────────▼──────────────────────┐
        │ DATA MOVER (source)      │  │ AGENT RELAIS (cible)           │
        │ Windows ou Linux         │─▶│ VM Linux dans FusionCompute    │
        │ liaison VDDK             │  │ pwrite() O_DIRECT aux offsets  │
        │ NBD/NBDSSL/HotAdd/SAN    │  │ virt-v2v-in-place              │
        └────────────┬─────────────┘  └─────────┬──────────────────────┘
                     │                          │ SCSI (volumes attachés)
              ┌──────▼──────┐            ┌──────▼───────────────────────┐
              │ vCenter /   │            │ FusionCompute VRM + CNA      │
              │ ESXi        │            │ datastores                   │
              └─────────────┘            └──────────────────────────────┘
```

Le flux de données ne transite **pas** par l'orchestrateur : le Data Mover envoie directement les extents à l'Agent relais. L'orchestrateur ne voit que les métadonnées et la progression.

**Décision retenue le 17/09/2026 (soir), confirmée par l'utilisateur : le déploiement par défaut colocalise Data Mover et Agent relais sur une seule VM, à l'intérieur du cluster FusionCompute — le schéma ci-dessus avec deux boîtes reliées en gRPC/mTLS reste une variante possible, pas le déploiement retenu.** Un volume FusionCompute ne peut de toute façon être attaché qu'à une VM du même cluster (contrainte de l'hyperviseur) ; si cette VM est aussi celle qui exécute le Data Mover (lecture VDDK, pourvu qu'elle ait un accès réseau routé vers le vCenter/ESXi source — même condition que le poste de l'utilisateur aujourd'hui), lecture et écriture se retrouvent dans le **même processus**, sur la **même machine** : c'est exactement le POC « tout-en-un » déjà validé deux fois (`tools/v2fc_source.py sync`, docs/dev/JOURNAL.md §5 sexies), simplement exécuté depuis une VM du cluster plutôt que depuis un Mac. Plus besoin de séparer les deux rôles, donc plus besoin du protocole réseau Data-Mover-vers-Agent (§13) — qui n'a d'ailleurs jamais été écrit (docs/dev/JOURNAL.md §16 : `Relay.agent_url` reste un champ réservé, sans code réseau derrière). Cette VM s'enregistre simplement comme un `Relay` existant (`POST /relays`, déjà implémenté) via son propre `fc_vm_uri`, et exécute elle-même `job-run` localement — aucun nouveau composant à écrire.

Le schéma à deux boîtes séparées ne retrouverait un intérêt que si un seul Data Mover central devait un jour alimenter plusieurs clusters FusionCompute sans redéployer une VM chargée du VDDK dans chacun — situation hypothétique, pas celle de l'utilisateur aujourd'hui. Voir D7 (§12) et §7.1.

---

## 4. Invariants — la garantie de rollback

Ce sont les règles que le code doit faire respecter mécaniquement, pas par convention. Elles constituent la promesse fonctionnelle de l'outil.

| # | Invariant | Mise en œuvre |
|---|---|---|
| **I1** | La VM source n'est **jamais** supprimée, ni ses disques, ni ses fichiers. | Le driver source expose une allow-list de méthodes vSphere ; toute méthode destructive (`Destroy_Task`, `DeleteDatastoreFile_Task`, `ReconfigVM_Task` hors CBT…) lève une exception avant l'appel réseau. |
| **I2** | Aucune écriture n'est jamais émise vers un disque source. | Le VDDK est ouvert avec `VIXDISKLIB_FLAG_OPEN_READ_ONLY`. Assertion redondante côté Data Mover. |
| **I3** | Les seules mutations tolérées sur la source sont : activation du CBT (une fois), création/suppression de snapshots de migration, extinction de l'invité à la bascule. | Chacune est journalisée dans `audit_log` avec l'utilisateur, l'horodatage et l'état avant/après. |
| **I4** | Toute mutation source est réversible et sa procédure inverse est enregistrée dans le job. | Table `rollback_steps`, rejouable dans l'ordre inverse. |
| **I5** | Un job peut être interrompu à tout instant sans corrompre l'état ni la source. | Points de reprise persistés par extent (§8.3) ; toute transition d'état est une transaction PostgreSQL. |

**Conséquence sur la bascule :** à l'issue d'une bascule réussie, la VM source reste présente, éteinte, annotée, snapshots consolidés — prête à redémarrer. Le rollback est une extinction de la cible suivie d'un allumage de la source. *RTO = temps de boot de l'invité.*

Une seule mutation source est optionnelle et désactivée par défaut : positionner `connectAtPowerOn = false` sur les vNIC de la source après bascule, pour éviter un conflit d'adressage si quelqu'un rallume la VM par erreur. Réversible, journalisée, à activer explicitement par politique.

---

## 5. Machine à états du job

```
                    ┌──────────┐
                    │ CREATED  │
                    └────┬─────┘
                         │ validate()
                    ┌────▼─────────┐   échec
                    │ VALIDATING   ├──────────┐
                    └────┬─────────┘          │
                         │ ok                 │
                    ┌────▼─────────┐          │
              ┌────▶│ PROVISIONING │          │  (VM cible + volumes créés,
              │     └────┬─────────┘          │   attachés au relais)
              │          │                    │
              │     ┌────▼─────────┐          │
              │     │  SEEDING     │◀──┐      │   changeId = "*"
              │     └────┬─────────┘   │      │   → tous blocs alloués
              │          │ done        │resume│
              │     ┌────▼─────────┐   │      │
              │  ┌─▶│   SYNCED     │   │      │   ← état stable, cible à jour
              │  │  └──┬────┬───┬──┘   │      │
              │  │     │    │   │      │      │
              │  │ sync│ pause  │cutover      │
              │  │  ┌──▼──┐ │   │      │      │
              │  │  │SYNC-│ │   │      │      │   QueryChangedDiskAreas(changeId_n)
              │  └──┤ ING │ │   │      │      │
              │     └─────┘ │   │      │      │
              │        ┌────▼───┴┐     │      │
              │        │ PAUSED  ├─────┘      │   snapshot conservé, changeId figé
              │        └─────────┘            │
              │                │              │
              │        ┌───────▼─────────┐    │
              │        │ CUTOVER_PENDING │    │   pré-vérifications
              │        └───────┬─────────┘    │
              │        ┌───────▼─────────┐    │
              │        │   QUIESCING     │    │   ShutdownGuest → PowerOff
              │        └───────┬─────────┘    │
              │        ┌───────▼─────────┐    │
              │        │  FINAL_DELTA    │    │   dernier CBT, à froid
              │        └───────┬─────────┘    │
              │        ┌───────▼─────────┐    │
              │        │   FINALIZING    │    │   virt-v2v-in-place, détach,
              │        └───────┬─────────┘    │   attach VM cible, réseau
              │        ┌───────▼─────────┐    │
              │        │  CUTOVER_DONE   │    │
              │        └───────┬─────────┘    │
              │                │ rollback     │
              │        ┌───────▼─────────┐    │
              └────────┤    ROLLBACK     │    │
                       └───────┬─────────┘    │
                       ┌───────▼─────────┐ ┌──▼─────┐
                       │  ROLLED_BACK    │ │ FAILED │
                       └─────────────────┘ └────────┘
```

Points de conception importants :

- **`SYNCED` est l'état de croisière.** Un job peut y rester des semaines, avec une resynchronisation planifiée (cron) ou manuelle. C'est ce qui permet de préparer la bascule sans pression.
- **`PAUSED` ≠ arrêt.** La pause fige le cycle en cours et conserve le snapshot de migration + le `changeId` courant. La reprise repart de l'extent exact où elle s'est arrêtée (§8.3). Attention : un snapshot conservé longtemps sur la source fait croître le delta VMDK — l'application doit **alerter** au-delà d'un seuil (défaut : 24 h ou 50 Go de delta).
- **`FAILED` n'est jamais terminal côté source.** Toute sortie en échec déclenche le nettoyage des snapshots de migration.

---

## 6. Chaîne de lecture VMware

### 6.1 Préparation (une fois par VM)

1. Vérifier `config.changeTrackingEnabled`. Si faux, `ReconfigVM_Task` avec `changeTrackingEnabled = true`.
   **Contrainte :** l'activation ne prend effet qu'après un cycle de « stun » — en pratique une création puis suppression de snapshot, ou un cycle d'alimentation. La création/suppression de snapshot est la méthode non intrusive à privilégier. *(confiance : 8/10)*
2. Refuser la migration si un disque est en mode **Independent** (`diskMode = independent_persistent|independent_nonpersistent`) : ces disques sont exclus des snapshots, le CBT ne les suit pas. Les signaler explicitement à l'opérateur — pas de migration silencieusement incomplète.
3. Refuser également les **RDM en mode physique** (`physicalMode`) et signaler les disques **partagés multi-writer**.

### 6.2 Cycle de synchronisation

```
1. CreateSnapshot_Task(name="v2fc-<job>-<round>", memory=False, quiesce=<option>)
2. pour chaque disque :
     changedAreas = QueryChangedDiskAreas(
         vm, snapshot, deviceKey, startOffset=0,
         changeId = ("*" si round 0 sinon changeId du round n-1))
     → liste d'extents {start, length} + nextChangeId
3. ouvrir le disque via VDDK sur la référence de snapshot
     VixDiskLib_ConnectEx(connectParams{snapshotRef}, readOnly=True,
                          transportModes="hotadd:san:nbdssl:nbd")
     VixDiskLib_Open(path, VIXDISKLIB_FLAG_OPEN_READ_ONLY)
4. pour chaque extent, par blocs de 4 à 16 Mio :
     VixDiskLib_Read(handle, start/512, len/512, buf)   ← unité = secteur 512 o
     → détection de zéros → envoi à l'agent relais (ou PUNCH_HOLE)
5. persister nextChangeId par disque (table sync_rounds)
6. RemoveSnapshot_Task(consolidate=True)
```

`QueryChangedDiskAreas` boucle : si la réponse couvre moins que la taille du disque, relancer à `startOffset = start + length` du dernier extent retourné jusqu'à couvrir la capacité totale.

### 6.3 Pièges du CBT — à traiter explicitement

Le `changeId` peut être **invalidé** sans que rien ne le signale. Causes connues : Storage vMotion, extension d'un disque, montée de version matérielle de la VM, échec de consolidation, arrêt brutal d'un hôte. *(confiance : 8/10)*

**Garde-fou obligatoire :** avant chaque cycle, comparer la capacité du disque et son `changeId` de référence à ce qui est persisté. En cas d'incohérence, ou si `QueryChangedDiskAreas` renvoie une erreur `FileFault`/`NotSupported`, **basculer automatiquement en re-seed complet** avec une alerte visible, plutôt que de produire une cible silencieusement corrompue. C'est le seul comportement acceptable.

Option de sécurité pour les environnements sensibles : un mode « vérification » qui, après le dernier delta, relit et compare les empreintes (SHA-256 par tranche de 64 Mio) source et cible. Coûteux, mais c'est la seule preuve d'intégrité réelle.

### 6.4 Choix du transport VDDK

| Mode | Débit | Conditions | Usage recommandé |
|---|---|---|---|
| `san` | le meilleur | Data Mover physique avec accès FC/iSCSI aux LUN VMFS | si l'infrastructure le permet |
| `hotadd` | très bon | Data Mover **virtuel sur le cluster source**, pilote `vstor2` installé | défaut pour un proxy virtualisé |
| `nbdssl` | moyen | accès réseau à ESXi (902/443), chiffré | défaut universel, le plus simple |
| `nbd` | moyen+ | idem, non chiffré | réseau de gestion isolé uniquement |

Ordre de repli conseillé : `hotadd:nbdssl:nbd`. Le mode retenu doit être **journalisé par job** via `VixDiskLib_GetTransportMode()` — c'est la première chose à regarder devant un problème de débit, et le §2.2 explique pourquoi une erreur de configuration peut vous priver silencieusement de `hotadd` et `san`.

Deux fonctions à ne pas oublier autour du mode HotAdd : `VixDiskLib_PrepareForAccess()` avant et `VixDiskLib_EndAccess()` après. Elles désactivent temporairement les opérations qui déplaceraient la VM source (vMotion, DRS) pendant que ses disques sont attachés au proxy. Un `EndAccess` oublié laisse la VM source bloquée pour DRS — une mutation source involontaire, donc une violation de l'invariant I3. `VixDiskLib_Cleanup()` doit être appelé au démarrage de l'orchestrateur pour purger les états laissés par un arrêt brutal.

**Ports à ouvrir — le chemin de données n'emprunte pas celui de l'API.** `nbd` et `nbdssl` établissent une session NFC sur le **port 902** de l'ESXi, pas sur le 443 de l'API. Un 443 joignable ne garantit donc rien, et un 902 filtré se manifeste par un `VIX_E_FAIL` générique à l'ouverture du disque, sans la moindre indication de cause. Le Data Mover doit joindre le 443 du vCenter **et** le 902 de chaque hôte ESXi susceptible d'héberger une VM source — à inscrire dans les règles de filtrage dès la conception, pas au moment du diagnostic.

Pour le débit, privilégier `VixDiskLib_ReadAsync()` + `VixDiskLib_Wait()` (présents dans cette build, §2.1) à des lectures synchrones : une profondeur de file de 4 à 8 par disque change l'ordre de grandeur en NBD.

---

## 7. Chaîne d'écriture FusionCompute

### 7.1 Principe

FusionCompute n'expose pas publiquement d'API d'écriture à offset arbitraire dans un volume. On contourne en faisant faire l'écriture **depuis l'intérieur** du cluster :

1. L'orchestrateur crée, via l'API REST VRM, un volume par disque source, dimensionné à l'identique (arrondi supérieur au granule du datastore).
2. Il **attache** ces volumes à la VM relais, qui devient l'unique écrivain.
3. Le relais reçoit les extents et exécute `pwrite(fd, buf, len, offset)` en `O_DIRECT`.
4. À la fin, les volumes sont **détachés** du relais et **attachés** à la VM cible définitive.

Cette approche n'utilise que des opérations de gestion de volumes standard. Elle est indépendante d'un SDK partenaire.

**« Agent relais » = le Data Mover exécuté sur une VM du cluster, pas un service réseau séparé (décision du 17/09/2026, §3, D7).** `relay/writer.py` est un module local — il écrit dans un fichier ou un périphérique bloc du système où il tourne, il ne reçoit rien par le réseau. Dans le déploiement retenu, cette VM *est* la VM relais enregistrée (`POST /relays`), et c'est elle qui exécute `tools/v2fc_source.py job-run` : pas d'agent réseau, pas de protocole à concevoir. Les points 3 et 4 ci-dessus restent vrais tels quels, en remplaçant simplement « l'agent relais reçoit les extents (par le réseau) » par « le Data Mover, qui tourne déjà sur cette VM, écrit directement les extents qu'il vient de lire ».

### 7.2 Identification des volumes dans le relais — le piège n° 1

Après attachement, il faut relier de façon **certaine** l'UUID du volume FusionCompute au `/dev/sdX` correspondant. Une erreur ici écrit un disque sur un autre. Stratégie en trois niveaux :

1. Lire `/dev/disk/by-id/` et faire correspondre le numéro de série SCSI à l'UUID du volume. *(à valider : FusionCompute renseigne-t-il le serial SCSI depuis l'UUID du volume ? confiance 5/10 — **à tester en laboratoire dès le POC**)*
2. À défaut : attachement **strictement séquentiel**, un volume à la fois, avec diff de `lsblk --json` avant/après et rescan du bus SCSI. Plus lent, déterministe.
3. Contrôle final systématique : vérifier que le périphérique bloc est au moins aussi grand que la capacité source, et écrire une **signature de 512 octets** au provisionnement, relue avant le premier `pwrite` de chaque cycle.

Le niveau 3 est non négociable. Un contrôle de cohérence négatif interrompt le job.

**Où placer la signature — corrigé après le POC L1.** La première version de ce document plaçait la signature dans le dernier secteur de la *capacité du disque*. C'est faux, et le banc d'essai l'a montré en refusant le second round : la tranche de seed qui couvre la fin du disque écrase la signature, si bien que le contrôle devient faussement négatif dès le premier delta. Pire, la signature aurait corrompu le dernier secteur de l'invité migré — c'est là que vit la table de partition GPT de secours.

La signature va donc dans le **réservoir situé au-delà de la capacité source**, dans le dernier secteur du *volume* :

```
volume FusionCompute  [ ← capacité du disque source → ][ réservoir ]
                        zone de données, bornée                  ↑
                        par capacity                      signature 512 o
```

Le volume cible est provisionné à `arrondi_granule(capacité_source + 512)`. L'arrondi au granule du datastore fournit généralement ce réservoir sans coût ; à défaut on arrondit explicitement d'un mégaoctet. L'agent refuse d'ouvrir un volume dont la taille est inférieure à `capacité_source + 512`, ce qui attrape au passage un volume sous-provisionné. La signature est effacée avant le détachement du relais (§10 étape 8), pour ne pas livrer de résidu.

### 7.3 Écriture

- Blocs de 4 Mio, `O_DIRECT` avec tampons alignés, file de profondeur 4 à 8 par disque.
- **Détection de zéros** côté Data Mover : un extent entièrement nul n'est pas transmis ; l'agent exécute `fallocate(FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE)` si le périphérique le supporte, sinon rien lors du seed (le volume est neuf donc déjà nul) et une écriture de zéros lors d'un delta. Cette distinction **seed vs delta** est essentielle pour ne pas gonfler un volume thin.
- `fdatasync()` à la fin de chaque round, avant de valider le point de reprise.
- Intégrité de transport : CRC32C par extent, vérifié par l'agent, réémission en cas d'écart.

### 7.4 Dimensionnement du relais

**Résolu (D2), source : *FusionCompute 8.10.0 VRM APIs*, fournie par l'utilisateur le 17/09/2026.** Le nombre de disques attachables dépend du type de bus (`pciType`), pas d'une limite unique :

| Bus | Emplacements (`sequenceNum`) |
|---|---|
| IDE | 3 (numérotés 1 à 3) |
| VirtIO | 25 (numérotés 1 à 25) |
| SCSI | 128 (numérotés 1 à 128) |

Deux limites supplémentaires s'appliquent au niveau de l'**hôte**, pas de la VM (erreurs `10420021` et `10300912` de l'API : respectivement « 512 disques max par hôte » et « nombre total de disques VM sur l'hôte dépassé ») — donc le nombre de relais actifs sur un même hôte compte aussi, pas seulement les slots d'une VM relais isolée.

**Incohérence relevée dans la documentation elle-même, non résolue par supposition** : l'exemple de requête `attachvol` commente `sequenceNum` par « Volume slot number. The maximum number is 11 », alors que le tableau de description du même document indique 128 pour SCSI. Les deux passages sont dans le même fichier. À vérifier en pratique sur le lab FusionCompute (attacher un 12ᵉ disque SCSI à une VM relais de test et voir si l'API l'accepte) avant de dimensionner le pool de relais sur l'hypothèse haute.

→ Prévoir dès le départ un **pool de relais** et un ordonnanceur qui place les jobs selon les emplacements disponibles et le datastore cible. Ne pas coder « un relais unique ».

Gabarit de départ : 8 vCPU, 16 Gio, 2 × 10 GbE, distribution avec noyau récent et `libguestfs`/`virt-v2v` disponibles.

### 7.5 Interface REST FusionCompute (VRM) — résolu (D2)

Ajouté le 17/09/2026 à partir de la documentation constructeur *FusionCompute 8.10.0 VRM APIs* fournie par l'utilisateur (fichier interne, pas d'URL publique — le support Huawei exige un compte pour la documentation développeur, cf. §12). Rien ci-dessous n'est deviné : chaque chemin, en-tête et champ vient du texte de ce document.

**Base et format.** Toutes les requêtes sont en HTTPS, JSON, RFC 2616. `basic_uri` vaut `/service`. **Port confirmé le 17/09/2026 depuis son terminal (test direct curl contre le VRM réel) : 7443, pas 8443.** `POST https://192.0.2.10:7443/service/session` avec des identifiants faux renvoie `HTTP 400 {"errorCode":"10100103","errorDes":"用户名或密码不正确"}` — exactement le code documenté pour un échec de login (§7.5 ①, table d'erreurs), preuve que 7443 exécute bien la logique de connexion VRM. La même requête sur 8443 renvoie `HTTP 401 {"errorCode":"10000002","errorDes":""}` — un code absent de la documentation, cohérent avec 8443 = un autre service (console/portail), qui répond mais ne traite pas vraiment `/service/session`. Les deux ports sont ouverts (testé au `nc`), donc ce n'est pas une question de pare-feu : c'est bien 7443 qu'il faut utiliser pour l'API. Les ressources sont scopées par site : `<vm_uri>` et `<vol_uri>` s'obtiennent en général sous la forme `/service/sites/<site_id>/vms` et `/service/sites/<site_id>/volumes` (le `site_id` se lit dans l'URN `urn:sites:<site_id>:...` retourné par `GET /service/sites`).

**① Connexion / jeton.**
- `GET <basic_uri>/versions` — pas d'authentification requise, renvoie les versions supportées et leur `loginUri` (repli si l'URI de login diffère selon la version).
- `POST <basic_uri>/session` — pas de corps ; l'authentification passe entièrement par des en-têtes : `X-Auth-User` (utilisateur), `X-Auth-Key` (mot de passe, haché SHA-256 par défaut — `X-ENCRYPT-ALGORITHM: 1` pour l'envoyer en clair, à éviter), `X-Auth-UserType` (0 local / 1 domaine / 2 interconnexion — défaut 2), `X-Auth-AuthType` (0 local / 1 domaine en cascade — défaut 0). Réponse : en-tête `X-Auth-Token` (le jeton), corps avec `validity` (durée de validité en ms — **600 000 ms, soit 10 min**), `userId`, `roleList`, `rightType`. Le jeton est à renvoyer dans l'en-tête `X-Auth-Token` de chaque appel suivant.
- `DELETE <basic_uri>/session` (avec `X-Auth-Token`) — déconnexion explicite.
- Point d'implémentation : avec une validité de 10 min, le driver cible doit soit se reconnecter avant expiration, soit intercepter une 401/jeton expiré et rejouer une connexion — comportement à confirmer sur le lab (le document ne précise pas le code d'erreur exact de expiration, seulement `10100112 : login failed, please log in again`).

**② Création d'un volume.**
`POST <vol_uri>` (donc `POST /service/sites/<site_id>/volumes`), corps notamment : `name`, `quantityGB` (mandatoire), `datastoreUrn` (mandatoire), `isThin`, `type` (`normal`/`share`), `indepDisk`, `persistentDisk`, `volType`. Réponse **asynchrone** : `{urn, uri, taskUrn, taskUri}` — le volume n'est pas forcément prêt à la réponse HTTP, il faut suivre `taskUrn` (voir ci-dessous « Suivi de tâche »). Existe aussi une variante combinée, plus directe pour notre cas d'usage (un disque par disque source, jamais réutilisé) :
`POST <vm_uri>/<vm_id>/action/createattachvol` — crée le volume **et** l'attache à la VM relais en un seul appel, mêmes champs que la création plus `pciType`/`sequenceNum`/`scsiCommandPassthrough` de l'attachement.

**③ Attachement d'un volume.**
`POST <vm_uri>/<vm_id>/action/attachvol`, corps : `volUrn` (mandatoire, format `urn:sites:1:volumes:78`), `pciType` (`IDE`/`SCSI`/`VIRTIO`, défaut `VIRTIO` — sur KVM, RDM et eVol n'acceptent que VIRTIO/SCSI), `sequenceNum` (numéro de slot, voir §7.4 pour les bornes par bus, **et l'incohérence documentaire relevée là**), `ioMode`, `accessMode`, `cacheMode`. Réponse asynchrone `{taskUrn, taskUri}`.

**④ Détachement d'un volume.**
`POST <vm_uri>/<vm_id>/action/detachvol`, corps : `volUrn` (mandatoire), `isFormat` (formater le disque au détachement — laisser `false` : notre contrat est de ne jamais écrire ce qui n'est pas explicitement demandé). Réponse asynchrone `{taskUrn, taskUri}`.

**⑤ Création d'une VM avec firmware imposé.**
`POST <vm_uri>` (donc `POST /service/sites/<site_id>/vms`), corps notamment `name`, `location` (`clusterUrn` ou `hostUrn`), `vmConfig` (`cpu`, `memory`, `disks[]`, `nics[]`, `properties`), `osOptions`. Le firmware de démarrage est **`vmConfig.properties.bootFirmware`**, valeurs `"UEFI"` ou `"BIOS"` (défaut `BIOS`) ; `bootFirmwareTime` (délai de démarrage) n'est réglable qu'en BIOS, pas en UEFI. Erreur `10300432` si l'OS déclaré ne supporte pas UEFI, `10321120` si la VM est en architecture Arm avec BIOS demandé (Arm impose UEFI). Réponse asynchrone `{urn, uri, taskUrn, taskUri}`. La VM créée par cet appel **n'a pas d'OS** (creation « vide » — cohérent avec notre usage : le disque migré porte déjà l'OS, ici on crée seulement l'enveloppe VM avant d'y attacher le volume migré comme disque système).

**⑥ Limite de disques par VM.** Voir §7.4 (résolu, avec l'incohérence documentaire à vérifier en pratique).

**⑦ Démarrage / arrêt d'une VM — ajouté le 17/09/2026** (l'extraction initiale du §7.5 n'avait couvert que ①-⑥ ; ce point manquait pour l'étape 8 du flux voulu, « redémarrer automatiquement la VM sur FusionCompute » après bascule). Retrouvé dans le même document constructeur (*FusionCompute 8.10.0 VRM APIs*, sections « Starting a VM » et « Stopping a VM ») :
- `POST <vm_uri>/<vm_id>/action/start` — pas de corps. Réponse asynchrone `{taskUrn, taskUri}`.
- `POST <vm_uri>/<vm_id>/action/stop` — corps `{"mode": "safe"|"force"}` (`safe` par défaut, doc : « Indicates whether to safely or forcibly stop a VM »). Réponse asynchrone `{taskUrn, taskUri}`.

**Suivi de tâche (utile pour toutes les opérations ci-dessus, qui sont asynchrones).**
`GET <task_uri>/<task_id>` (donc `GET /service/sites/<site_id>/tasks/<id>`, d'après l'exemple `taskUri: "service/sites/1/tasks/759"`) renvoie `status` (`waiting`/`running`/`success`/`failed`/`cancelling`), `progress` (0-100), `reason`/`reasonDes` en cas d'échec. Le driver cible doit **toujours** attendre `success` avant de considérer une création/un attachement/un détachement comme effectif — ne jamais supposer la réussite au seul reçu du 200 OK initial, qui ne fait qu'accuser la prise en compte de la demande.

**Ce qui reste à vérifier sur le lab avant d'écrire le driver cible** (rien de ce qui suit n'est bloquant pour commencer, mais tout doit être confirmé par un test réel avant la mise en production, dans le même esprit que le POC L1 pour VDDK) :
1. ~~Le bon port pour les appels REST~~ **CONFIRMÉ le 17/09/2026 : 7443** (voir ci-dessus).
2. La vraie limite de `sequenceNum` en SCSI (11 ou 128 selon l'endroit du document).
3. Le comportement exact à l'expiration du jeton (code HTTP, message).
4. Le format réel de `datastoreUrn`/`clusterUrn`/`hostUrn` sur ce lab précis (à lire via les endpoints de requête, non documentés ici faute de besoin immédiat pour D2).
5. **Nouveau, à vérifier** : la forme exacte du hachage du mot de passe (`X-Auth-Key`) — le document dit « SHA-256 » sans plus de précision ; `datamover/fusioncompute.py` suppose un hex minuscule du mot de passe UTF-8 brut, pas encore confirmé avec un vrai mot de passe sur ce lab.
6. **Ajout du 17/09/2026 (matin)** : l'utilisateur a créé sur le VRM un compte dédié `api`, de type *Interface interconnection user* (confirmant que `X-Auth-UserType: 2`, déjà la valeur par défaut du driver, est le bon choix pour ce type de compte), rôle `administrator,AlarmPerformance,au…` (tronqué dans la capture fournie — rôle complet non confirmé), 2FA désactivée, aucune restriction d'horaire ni d'IP. Les tests de connexion à venir doivent utiliser ce compte plutôt que `admin` ; les échecs `10100103` obtenus précédemment avec `admin` restent une preuve valide que le port 7443 est correct, mais ne présument rien du mot de passe du compte `api`.

---

## 8. Modèle de données (PostgreSQL)

### 8.1 Schéma principal

> Mis à jour le 16/09/2026 : les tables `users`, `credentials`, `plans` et
> `plan_vms` ci-dessous étaient référencées par clé étrangère plus bas dans
> ce même schéma (`endpoints.credential_id`, `jobs.plan_id`) mais n'avaient
> jamais été définies, et l'authentification/RBAC du §9 n'avait pas de table
> propre. Elles ont été implémentées lors du socle L0b (`v2fc/api/models.py`,
> testé de bout en bout contre l'ESXi de lab — voir docs/dev/JOURNAL.md §11), et sont
> reportées ici pour que ce document reste la source de vérité du schéma.
> Ce sont des ajouts de plomberie applicative ordinaire, pas des décisions
> sur le comportement vSphere/FusionCompute.

```sql
-- Authentification (absente du schéma original, exigée par le RBAC du §9)
CREATE TABLE users (
  id            uuid PRIMARY KEY,
  email         text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  role          text NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer','operator','admin')),
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Identifiants des environnements — référencée par endpoints.credential_id
-- ci-dessous, mais jamais définie dans le schéma original. Chiffrement
-- enveloppe (§13) : jamais journalisés, jamais renvoyés par l'API (écriture
-- seule après création).
CREATE TABLE credentials (
  id                 uuid PRIMARY KEY,
  label              text NOT NULL,
  username           text NOT NULL,
  encrypted_password bytea NOT NULL,   -- AES-256-GCM, DEK par secret
  encrypted_dek      bytea NOT NULL,   -- DEK elle-même chiffrée par la KEK (V2FC_KEK)
  nonce              bytea NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  created_by         uuid REFERENCES users(id)
);

-- Environnements
CREATE TABLE endpoints (
  id            uuid PRIMARY KEY,
  kind          text NOT NULL CHECK (kind IN ('vcenter','fusioncompute')),
  name          text NOT NULL,
  url           text NOT NULL,
  credential_id uuid NOT NULL REFERENCES credentials(id),
  tls_fingerprint text,
  verify_tls    boolean NOT NULL DEFAULT true,
  last_probe_at timestamptz,
  last_probe_ok boolean
);

CREATE TABLE relays (
  id            uuid PRIMARY KEY,
  target_id     uuid NOT NULL REFERENCES endpoints(id),
  fc_vm_uri     text NOT NULL,          -- identifiant de la VM relais dans FC
  agent_url     text NOT NULL,          -- gRPC mTLS
  max_slots     int  NOT NULL DEFAULT 12,
  used_slots    int  NOT NULL DEFAULT 0,
  status        text NOT NULL           -- ONLINE | OFFLINE | DRAINING
);

-- Plans — référencée par jobs.plan_id ci-dessous, mais jamais définie dans
-- le schéma original. Un plan fige un lot de VM sélectionnées pour
-- migration (POST /plans, §9) ; les mappings par VM (réseau, CPU/RAM,
-- firmware) viendront avec l'implémentation du driver cible (l'API REST
-- FusionCompute est documentée depuis le 17/09/2026, §7.5 — mais le driver
-- lui-même n'est pas encore écrit), pas avant, pour ne pas en deviner la
-- forme avant d'avoir du code qui s'en sert vraiment.
CREATE TABLE plans (
  id         uuid PRIMARY KEY,
  name       text NOT NULL,
  source_id  uuid NOT NULL REFERENCES endpoints(id),
  target_id  uuid REFERENCES endpoints(id),   -- optionnel tant que le driver cible n'est pas implémenté
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plan_vms (
  id                uuid PRIMARY KEY,
  plan_id           uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  src_vm_moref      text NOT NULL,
  src_instance_uuid text NOT NULL,
  src_vm_name       text NOT NULL,
  added_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (plan_id, src_instance_uuid)
);

-- Jobs
CREATE TABLE jobs (
  id             uuid PRIMARY KEY,
  plan_id        uuid REFERENCES plans(id),
  source_id      uuid NOT NULL REFERENCES endpoints(id),
  target_id      uuid NOT NULL REFERENCES endpoints(id),
  relay_id       uuid REFERENCES relays(id),
  src_vm_moref   text NOT NULL,
  src_vm_name    text NOT NULL,
  src_instance_uuid text NOT NULL,      -- identité stable, résiste au renommage
  tgt_vm_uri     text,                  -- créée en PROVISIONING
  state          text NOT NULL,
  state_since    timestamptz NOT NULL DEFAULT now(),
  spec           jsonb NOT NULL,        -- mapping réseau, CPU/RAM, firmware, options
  cbt_reseed_count int NOT NULL DEFAULT 0,
  created_by     uuid, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE job_disks (
  id            uuid PRIMARY KEY,
  job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  device_key    int  NOT NULL,          -- clé du VirtualDisk vSphere
  unit_number   int  NOT NULL,
  src_path      text NOT NULL,          -- [datastore] vm/vm.vmdk
  capacity_bytes bigint NOT NULL,
  disk_mode     text NOT NULL,
  tgt_volume_uri text,
  tgt_dev_path  text,                   -- /dev/sdX vu par le relais
  tgt_signature bytea,                  -- signature de contrôle §7.2
  UNIQUE (job_id, device_key)
);

-- Cycles de synchronisation
CREATE TABLE sync_rounds (
  id             uuid PRIMARY KEY,
  job_id         uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  round_no       int  NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('SEED','DELTA','FINAL')),
  snapshot_moref text,
  started_at     timestamptz NOT NULL DEFAULT now(),
  ended_at       timestamptz,
  status         text NOT NULL,
  bytes_planned  bigint, bytes_transferred bigint,
  UNIQUE (job_id, round_no)
);

CREATE TABLE disk_rounds (
  id             uuid PRIMARY KEY,
  round_id       uuid NOT NULL REFERENCES sync_rounds(id) ON DELETE CASCADE,
  job_disk_id    uuid NOT NULL REFERENCES job_disks(id),
  base_change_id text,                  -- '*' au seed
  next_change_id text,                  -- persisté À LA FIN du round
  extent_count   int,
  bytes_planned  bigint, bytes_transferred bigint,
  cursor_index   int NOT NULL DEFAULT 0,  -- ← point de reprise
  cursor_offset  bigint NOT NULL DEFAULT 0,
  status         text NOT NULL,
  UNIQUE (round_id, job_disk_id)
);

CREATE TABLE extents (                  -- plan de copie du round, matérialisé
  disk_round_id uuid NOT NULL REFERENCES disk_rounds(id) ON DELETE CASCADE,
  idx           int  NOT NULL,
  start_offset  bigint NOT NULL,
  length        bigint NOT NULL,
  PRIMARY KEY (disk_round_id, idx)
);

-- Traçabilité et réversibilité
CREATE TABLE rollback_steps (
  id        uuid PRIMARY KEY,
  job_id    uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  seq       int  NOT NULL,
  action    text NOT NULL,              -- ex. SOURCE_POWER_ON, SNAPSHOT_REMOVE
  payload   jsonb NOT NULL,
  undone_at timestamptz
);

CREATE TABLE audit_log (
  id uuid PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
  actor uuid, job_id uuid, action text NOT NULL,
  target text, before jsonb, after jsonb, result text
);
```

### 8.2 Règle d'or sur le `changeId`

`next_change_id` n'est écrit **qu'après** le `fdatasync()` de l'agent confirmant que tous les extents du round sont sur disque. Toute autre séquence crée un trou silencieux : on croirait avoir copié des blocs qu'on n'a pas écrits, et le delta suivant ne les redemanderait jamais.

### 8.3 Reprise après pause ou incident

Le plan de copie (`extents`) est matérialisé en base **avant** de commencer le transfert. La reprise lit `cursor_index` / `cursor_offset` et repart exactement là. Le snapshot de migration correspondant étant conservé pendant la pause, le plan reste valide.

Si le snapshot a disparu entre-temps (intervention manuelle), le round est invalidé et rejoué depuis le `base_change_id`, qui lui est toujours valide. Aucune perte, seulement du temps.

---

## 9. API REST — surface principale

Préfixe `/api/v1`. Authentification par JWT court + refresh ; RBAC à trois rôles (`viewer`, `operator`, `admin`).

```
POST   /endpoints                      déclarer un vCenter/ESXi ou un VRM FusionCompute
POST   /endpoints/{id}/probe           tester la connexion + empreinte TLS
GET    /endpoints/{id}/inventory       arbre datacenters/clusters/VM (vCenter/ESXi uniquement)
GET    /endpoints/{id}/datastores      datastores du site (FusionCompute uniquement — §7.5, écrit 17/09/2026)
GET    /endpoints/{id}/networks        port groups de tous les DVS du site (FusionCompute uniquement, idem)

POST   /relays                         enregistrer une VM relais
GET    /relays                         état et emplacements disponibles

POST   /plans                          un plan = un lot de VM + mappings communs
PUT    /plans/{id}/target              choisir l'endpoint FusionCompute cible du plan (écrit 17/09/2026)
PUT    /plans/{id}/vms/{vm_id}/mapping datastore + réseau cible d'une VM du plan (écrit 17/09/2026)
GET    /plans/{id}/preflight           contrôles : CBT, disques indépendants, RDM,
                                       espace cible, mapping réseau complet, firmware

POST   /jobs                           créer depuis une VM de plan (plan_vm_id, relais optionnel)
GET    /jobs
GET    /jobs/{id}                      état complet + disques + rounds
DELETE /jobs/{id}                      seulement avant tout round exécuté
POST   /jobs/{id}/provision            → PROVISIONING → SEEDING (crée les job_disks, et
                                        les volumes FusionCompute réels si un relais est assigné)
GET    /jobs/{id}/run-context?round=   contexte non-secret pour `tools/v2fc_source.py job-run`
                                       (le round lui-même tourne dans le Data Mover, pas l'API)
PATCH  /jobs/{id}/rounds/{round_id}    rapport de fin de round (appelé par job-run)
POST   /jobs/{id}/cutover              { auto_restart: bool } — extinction propre puis
                                        FINAL_DELTA ; la finalisation (détach/attach/VM
                                        cible/démarrage) s'enchaîne automatiquement après
                                        le rapport de round FINAL
POST   /jobs/{id}/rollback             rallume la source, ne supprime jamais la cible (I1)
POST   /jobs/{id}/logs                 ingestion de lignes de journal (job-run)
GET    /jobs/{id}/logs?since=          lues par l'onglet Logs (poll, pas de WebSocket — voir note)
```

**Écart avec la conception initiale de ce document (version 0.6) — signalé, pas corrigé
en douce.** Cette section décrivait à l'origine une surface plus riche
(`/jobs/{id}/pause`, `/resume`, `/schedule`, `/abort`, `/events`, `/report`, et un flux
WebSocket `/ws/jobs/{id}`) avant qu'aucune ligne de L2 ne soit écrite. Le 17/09/2026,
l'utilisateur a demandé le socle L2 complet en une fois (docs/dev/JOURNAL.md §16) ; ce qui a été
réellement construit couvre le cycle nominal (créer, provisionner, seed, delta,
bascule, rollback, journal) mais pas encore : la pause/reprise en cours de round
(PAUSED existe dans la machine à états du §5 mais aucune route ne l'atteint), un
delta planifié par cron, un abandon propre distinct du rollback, un rapport de
bascule exportable, ni un flux WebSocket de progression (l'onglet Logs interroge
`GET /jobs/{id}/logs` toutes les 2 s à la place — suffisant pour un usage manuel,
pas pour un tableau de bord temps réel à grande échelle). Aucune route n'est
encore protégée par `Idempotency-Key`. Rien de tout cela n'est un manque caché :
docs/dev/JOURNAL.md §16 documente précisément ce qui a été délibérément laissé de côté et
pourquoi (surtout : pas de VM relais réelle sur le lab à ce jour pour exercer le
provisioning/la finalisation FusionCompute pour de vrai).

---

## 10. Séquence de bascule

Le moment critique. Séquence détaillée, chaque étape étant un point de reprise :

| # | Étape | Détail | En cas d'échec |
|---|---|---|---|
| 1 | Pré-vérifications | Dernier delta < seuil configuré ; VM cible provisionnée ; mappings réseau complets ; relais en ligne ; espace datastore. | Refus, pas de bascule |
| 2 | Arrêt invité | `ShutdownGuest` via VMware Tools, attente `shutdown_timeout_s` (défaut 300 s), puis `PowerOffVM_Task`. | Option « forcer » explicite |
| 3 | Verrouillage | Marquer le job non reprenable en delta chaud. | — |
| 4 | Snapshot final | `CreateSnapshot_Task` même VM éteinte : garantit un point figé et une référence valide pour `QueryChangedDiskAreas`. | Abandon → étape 12 |
| 5 | **Dernier delta** | Cycle `FINAL` identique aux deltas chauds. C'est la durée d'indisponibilité réelle. | Abandon → étape 12 |
| 6 | Vérification | `fdatasync`, contrôle des signatures, contrôle d'empreintes si activé. | Abandon → étape 12 |
| 7 | Préparation invité | `virt-v2v-in-place` sur le relais, volumes encore attachés (§11). | Abandon → étape 12 |
| 8 | Détachement | Détacher les volumes du relais. | Reprise possible |
| 9 | Attachement cible | Attacher les volumes à la VM cible, dans le bon ordre de contrôleur et d'unité. | Reprise possible |
| 10 | Réseau | Rattacher les vNIC aux port groups mappés ; conservation ou régénération des MAC selon politique. | Reprise possible |
| 11 | Allumage + contrôle | Démarrer, attendre l'agent invité ou un test applicatif, horodater. | → rollback proposé |
| 12 | Clôture source | Supprimer les snapshots de migration (consolidation), annoter la VM source (`v2fc: migrée le … par … job …`), laisser **éteinte et intacte**. Option : déconnecter les vNIC. | — |

**Abandon avant l'étape 8** = la source est rallumée, la cible reste incomplète, rien n'est perdu.
**Rollback après l'étape 11** = extinction de la cible, allumage de la source, suppression optionnelle de la cible et de ses volumes.

Estimation de la fenêtre : `durée = arrêt invité + dernier delta + préparation invité + rattachement + boot`. Les étapes 7 à 11 représentent typiquement 3 à 10 minutes ; c'est l'étape 5 que les deltas préalables servent à réduire. L'application doit **afficher une estimation** basée sur la taille du delta précédent et le débit mesuré.

---

## 11. Préparation de l'invité — la première cause d'échec réelle

Copier les blocs est la partie facile. Un invité qui bootait sur des contrôleurs VMware (PVSCSI/LSI Logic, VMXNET3) ne démarrera pas tel quel sur l'hyperviseur FusionCompute, qui présente des périphériques virtio. *(FusionCompute repose sur un noyau KVM et un modèle de périphériques virtio — confiance 7/10, à confirmer sur la version cible)*

**Recommandation : utiliser `virt-v2v --in-place`** sur la VM relais, contre les volumes attachés. C'est exactement le rôle de cet outil : convertir un invité déjà copié, sans recopier les données. Il gère l'injection de pilotes, l'initramfs, les points de montage et le bootloader pour Linux et Windows. Réécrire cette logique soi-même est un piège classique. *(confiance : 8/10 sur l'adéquation du mode `--in-place` ; validation requise sur les invités réellement rencontrés)*

Ce que la préparation doit couvrir, quel que soit l'outil :

**Windows**
- Injection des pilotes virtio (`viostor`/`vioscsi`, `netkvm`) : copie des `.sys` dans `System32\drivers`, enregistrement du service et des entrées `CriticalDeviceDatabase` pour `PCI\VEN_1AF4&DEV_1001` et `DEV_1042`, par édition hors ligne de la ruche `SYSTEM`. Sans cela : écran bleu `INACCESSIBLE_BOOT_DEVICE`.
- Désinstallation programmée des VMware Tools au premier démarrage, installation des UVP Tools.
- Réactivation possible si la MAC ou le matériel changent : prévoir la conservation des MAC par défaut.

**Linux**
- `virtio_blk`, `virtio_scsi`, `virtio_net`, `virtio_pci` dans l'initramfs (`dracut --add-drivers` / `update-initramfs -u`).
- `/etc/fstab` et la ligne `root=` de GRUB en **UUID**, jamais en `/dev/sdX`.
- Suppression des règles udev de nommage réseau héritées ; le renommage d'interface (`ens192` → `ens3`) casse la configuration IP. Solution robuste : configuration réseau par adresse MAC, ou `net.ifnames=0`.
- Retrait de `open-vm-tools`.

**Les deux**
- **Firmware** : la VM cible doit être créée en BIOS ou UEFI **à l'identique de la source**. Une incohérence rend le disque non amorçable. À contrôler en pré-vérification, pas au moment de la bascule.
- UEFI : vérifier la présence du chargeur de secours `\EFI\BOOT\BOOTX64.EFI` ; les entrées NVRAM ne survivent pas à la migration.
- Nombre et type de contrôleurs, ordre des unités : le mapping `unit_number` source → cible doit être préservé.

---

## 12. Décisions ouvertes

| Id | Sujet | Options | Recommandation |
|---|---|---|---|
| **D1** | ~~Plateforme du Data Mover~~ | — | **RÉSOLU.** Le VDDK Linux x86-64 est disponible et validé (§2.1) : Data Mover **Python + `ctypes`**, sur x86-64 Linux, glibc ≥ 2.16. Conserver l'abstraction `SourceReader` pour pouvoir substituer `openvddk`. **Contrainte matérielle :** le VDDK n'existe qu'en x86-64 — un Mac Apple Silicon ne peut pas exécuter le Data Mover nativement, il faut une VM ou un hôte x86-64 pour développer et tester le chemin de données. |
| **D2** | ~~Chemins exacts de l'API REST FusionCompute~~ | — | **RÉSOLU le 17/09/2026.** L'utilisateur a fourni la documentation constructeur (*FusionCompute 8.10.0 VRM APIs*, *KRM Interface Documentation*, *Monitoring KPI List*). Les six points sont répondus avec citation de source, sans rien deviner : voir §7.5 pour le détail complet (session/jeton, création de volume, attachement, détachement, création de VM avec firmware imposé, limite de disques par bus §7.4). Deux points restent à vérifier sur le lab avant mise en production (le document contient lui-même une incohérence interne sur la limite SCSI, et le port REST annoncé — 7443 — diffère du port 8443 donné pour la console VRM) : voir la liste de vérifications en fin de §7.5. La *KRM Interface Documentation* couvre un sous-système distinct (gestion de clusters Kubernetes sur FusionCompute) et n'est pas utile à D2. **Suite le 17/09/2026 (soir) :** la partie lecture du driver cible (`datamover/fusioncompute.py` — connexion, sites, datastores, réseaux) est écrite et testée unitairement (9 tests, sans VRM réel), exposée par l'API et l'UI web (voir docs/dev/JOURNAL.md §13) — **mais jamais exécutée contre le VRM réel**, aucun outil de cette session n'atteignant 192.0.2.10. Trois hypothèses non confirmées à vérifier avant de faire confiance au driver : le port (7443 vs 8443), la forme exacte du hachage SHA-256 du mot de passe (non précisée par le document), et la clé de réponse des port groups (`portGroups` vs `portgroups`, incohérence documentaire, code tolérant aux deux). |
| **D3** | ~~Version de vSphere source~~ | — | **RÉSOLU pour le laboratoire.** ESXi 8.0.3 build 24022510, Enterprise Plus (§2.5) : même version que le VDDK. À reposer pour chaque plateforme cliente. |
| **D4** | Version de FusionCompute cible (8.x ?) et modèle de périphériques | — | À fournir. Conditionne §11 et la limite de disques par VM. |
| **D5** | Sérialisation SCSI des volumes FC dans le relais | Test en laboratoire | **Premier test à faire au POC** (§7.2). Détermine si l'attachement peut être parallèle ou doit rester séquentiel. |
| **D6** | Préparation invité : `virt-v2v --in-place` ou logique propre | — | `virt-v2v` d'abord, logique propre seulement pour les cas qu'il ne couvre pas. |
| **D7** | ~~Déploiement du relais : VM dédiée + agent réseau, ou VM applicative colocalisée~~ | — | **RÉSOLU le 17/09/2026 (soir), à la demande de l'utilisateur.** Le Data Mover tourne directement sur une VM Ubuntu à l'intérieur du cluster FusionCompute cible ; cette même VM s'enregistre comme `Relay` et reçoit les volumes attachés. Pas d'agent réseau gRPC/mTLS à écrire (§3, §7.1, §13). Prérequis côté l'utilisateur : cette VM doit avoir un accès réseau routé vers le vCenter/ESXi source (même condition que le poste utilisé aujourd'hui pour les validations L1), et son OS doit voir les volumes FusionCompute attachés comme périphériques bloc (D5, toujours ouvert). |

---

## 13. Sécurité

- **Secrets** : aucun identifiant en clair en base. Chiffrement enveloppe (DEK par secret, KEK depuis variable d'environnement ou coffre). Jamais journalisés, jamais renvoyés par l'API (écriture seule).
- **Comptes de service** : un compte vCenter dédié avec le strict nécessaire — lecture d'inventaire, `Virtual machine.Snapshot management`, `Virtual machine.Provisioning.Allow read-only disk access`, `Virtual machine.Configuration.Toggle disk change tracking`, `Virtual machine.Interaction.Power off`. **Pas** de droit de suppression : c'est l'invariant I1 appliqué aussi côté vCenter, en défense en profondeur.
- **Orchestrateur ↔ Data Mover/relais** : dans le déploiement retenu (D7, VM applicative colocalisée dans le cluster FusionCompute), pas d'agent réseau séparé à sécuriser — le Data Mover appelle l'API avec le même jeton JWT bearer que le reste (`V2FC_API_TOKEN`, `tools/v2fc_source.py job-run`), jamais passé en argument de ligne de commande. Le gRPC sur mTLS ci-dessous ne concernerait qu'une variante future à Data Mover et relais séparés (plusieurs clusters FusionCompute pilotés par un seul Data Mover), non retenue aujourd'hui : certificats émis par une CA interne à l'application, un certificat par agent, révocable — à concevoir seulement si ce scénario redevient d'actualité.
- **TLS sortant** : vérification activée par défaut ; épinglage d'empreinte pour les certificats auto-signés de VRM/vCenter, avec acceptation explicite et journalisée.
- **Audit** : toute action de job, tout appel mutant vers vCenter ou VRM, avec l'acteur. C'est ce qui rend le rapport de bascule opposable auprès d'un client hospitalier ou public.

---

## 14. Lotissement proposé

| Lot | Contenu | Critère de sortie |
|---|---|---|
| **L0a — Driver source vSphere** | **Fait.** `datamover/vsphere.py` (inventaire, CBT, snapshots, `SourceGuard`), `datamover/preflight.py`, `tools/v2fc_source.py` (inventory / preflight / provision / sync / cleanup). 39 tests unitaires sans vCenter. | Atteint hors lab. Sur lab : `preflight` puis `sync --round seed` puis `sync --round delta` sur une VM réelle. |
| **L0b — Socle web** | **Fait pour la partie source, testé de bout en bout sur l'ESXi de lab le 16/09/2026** (voir docs/dev/JOURNAL.md §11) : FastAPI + PostgreSQL + Redis + auth/RBAC + docker-compose, schéma du §8 (`users`/`credentials`/`plans`/`plan_vms` ajoutés, voir §8.1), inventaire vSphere avec sélection multi-VM et création de plans. **Partie cible (lecture) écrite le 17/09/2026, non testée sur le lab** (docs/dev/JOURNAL.md §13) : connexion FusionCompute, listage datastores/réseaux, écran de mapping par VM (datastore + réseau). **Pas encore fait** : machine à états d'exécution de job, progression WebSocket, et toute écriture côté FusionCompute (créer un volume, l'attacher, créer la VM cible) — ce sera L2. | Atteint côté source : connexion à l'ESXi de lab, inventaire affiché dans l'UI, VM sélectionnée, plan créé, pré-vérification affichée. Côté cible : code écrit et testé unitairement, reste à valider contre le lab FusionCompute réel (l'utilisateur, port/hachage/clé de réponse à confirmer — docs/dev/JOURNAL.md §13). |
| **L1 — POC du maillon critique** | **Fait hors vCenter** (`README.md` du dépôt) : liaison VDDK, `QueryAllocatedBlocks`, planificateur d'extents, agent relais `pwrite`, pause/reprise, signature, intégrité. **Lecture source validée deux fois sur plateforme réelle** : ESXi direct (15/09/2026, docs/dev/JOURNAL.md §5 quinquies — seed+delta réels, `nbdssl`) et **vCenter réel** (17/09/2026, docs/dev/JOURNAL.md §5 sexies — 192.0.2.20, seed+delta réels via `QueryAllocatedBlocks`/`QueryChangedDiskAreas`, repli `moref` confirmé). `hotadd` reste non exercé en pratique (exige un hôte lecteur interne au cluster source, hors de portée du banc d'essai Mac+Docker actuel) ; D5 (appariement volume FusionCompute ↔ `/dev/sdX`) reste ouvert, l'écriture côté FusionCompute n'existant pas encore. | Hors lab : atteint — seed, delta, pause/reprise et remise à zéro validés par SHA-256 sur un VMDK de 2 Gio. **Sur lab, tel qu'écrit ici (« écrit au bon offset dans FusionCompute ») : pas encore atteint** — les deux validations réelles ci-contre écrivent dans un fichier brut local (`relay/writer.py`), pas dans FusionCompute (le driver d'écriture cible n'est pas encore écrit, §13). *Discordance signalée, non tranchée ici (docs/dev/JOURNAL.md §5 sexies) : soit ce critère est reformulé pour ne porter que sur la lecture source (ce qui a réellement été demandé et testé), soit L1 n'est considéré clos qu'après un test d'écriture réelle vers FusionCompute — décision à prendre par l'utilisateur.* |
| **L2 — Cycle complet** | **Machine à états et API écrites le 17/09/2026** (docs/dev/JOURNAL.md §16, demande explicite de l'utilisateur) : création de job, provisioning (réel si un relais est assigné, sauté sinon), seed/delta/bascule exécutés via `tools/v2fc_source.py job-run` (le round tourne toujours dans le Data Mover, pas dans l'API), rollback, journal. **Pas encore** : pause/reprise en cours de round (l'état PAUSED existe au §5 mais aucune route ne l'atteint), repli automatique en re-seed au-delà du compteur `cbt_reseed_count`, plan d'extents matérialisé en base (`extents`/`disk_rounds` existent mais ne sont pas peuplés round par round), progression WebSocket (poll HTTP à la place). Le protocole réseau Data-Mover-vers-Agent envisagé au §13 n'est plus dans ce périmètre du tout : D7 (§12) retient un déploiement où le Data Mover tourne directement sur la VM relais. | **Non atteint tel qu'écrit ici** : aucune VM multi-disques n'a encore été synchronisée puis resynchronisée par ce nouveau code contre un vrai lab (aucun test d'intégration base de données ne l'a exercé non plus, faute d'infrastructure de test — docs/dev/JOURNAL.md §16) ; à valider par l'utilisateur dès que possible, y compris sans relais réel pour le cycle seed/delta/bascule en écriture locale. |
| **L3 — Bascule** | Séquence du §10, `virt-v2v --in-place`, détach/attach, mapping réseau, rollback. | Une VM Windows et une VM Linux démarrent sur FusionCompute ; le rollback les ramène sur VMware. |
| **L4 — Exploitation** | UI complète, plans multi-VM, pool de relais et ordonnanceur, deltas planifiés, pré-vérifications, rapport de bascule. | Un lot de 10 VM est migré par vagues. |
| **L5 — Durcissement** | Mode vérification d'empreintes, limitation de débit, reprise après redémarrage de l'orchestrateur, métriques, sauvegarde de configuration. | Tests de coupure : orchestrateur tué en plein round, relais redémarré, vCenter injoignable. |

L1 est le lot qui décide du projet. Tant qu'il n'est pas validé, tout le reste est de l'habillage.

---

## 15. Risques

| Risque | Impact | Traitement |
|---|---|---|
| Invalidation silencieuse du `changeId` | Cible corrompue sans alerte | Garde-fou §6.3, repli en re-seed obligatoire, jamais de continuation optimiste |
| Mauvais appariement volume FC ↔ `/dev/sdX` | Écriture sur le mauvais disque | Signature de contrôle §7.2, attachement séquentiel si D5 négatif |
| Invité qui ne démarre pas après bascule | Fenêtre dépassée | Pré-vérification du firmware, `virt-v2v` en amont, **répétition de la bascule sur un clone de test avant la vraie** |
| Snapshot de migration conservé trop longtemps | Saturation du datastore source | Alerte à 24 h / 50 Go, suppression forcée sur abandon |
| Accès VDDK perdu ou incompatible vSphere 9 | Projet bloqué | Archivage des deux builds avec empreinte, abstraction `SourceReader`, évaluation d'`openvddk` |
| Transport dégradé silencieusement en NBD | Migration 3 à 10× plus lente, fenêtre de bascule dépassée | `vddk_probe.py` au démarrage, refus de démarrer si le mode exigé manque, `GetTransportMode()` journalisé par disque (§2.2) |
| `EndAccess` non appelé après HotAdd | VM source bloquée pour DRS/vMotion — violation de I3 | Appel dans un `finally`, plus `VixDiskLib_Cleanup()` au démarrage de l'orchestrateur (§6.4) |
| API FusionCompute divergente entre versions | Reprise du driver cible | Driver cible versionné, tests d'intégration par version, aucun chemin codé en dur hors configuration |
| Disques indépendants ou RDM physiques | Migration incomplète | Détection en pré-vérification, refus explicite — implémenté dans `preflight.py` |
| VM source chiffrée | Le VDDK ne peut pas lire ses disques | Détection en pré-vérification, refus explicite |
| VM d'agent (vCLS, NSX, vSAN, EAM) embarquée dans un plan | Migration inutile d'une VM recréée automatiquement | Détection de `config.managedBy`, refus explicite (`VM_AGENT`) |
| Licence ESXi gratuite | API en lecture seule : ni snapshot ni VDDK, découvert au pire moment | Détection au relevé de plateforme et capture explicite de `RestrictedVersion` |
| vMotion pendant un cycle, sur une session ouverte directement sur l'hôte | Lecture invalidée en cours de copie | `PrepareForAccess` exige une session vCenter ; avertissement `CONNEXION_HOTE` en pré-vérification |
| Méthode destructrice appelée par erreur sur la source | Perte de la VM d'origine, rollback impossible | Liste blanche `SourceGuard` : toute méthode non autorisée lève avant l'appel réseau ; compte vCenter sans droit de suppression en seconde barrière |

---

## Sources

- [Broadcom Removes VDDK Pages Without Explanation — ShapeBlue](https://www.shapeblue.com/broadcom-vddk-download-vmware-to-kvm/)
- [Leaving VMware Just Got Harder After Broadcom Pulled VDDK Downloads — Virtualization Howto](https://www.virtualizationhowto.com/2026/09/leaving-vmware-just-got-harder-after-broadcom-pulled-vddk-downloads/)
- [Broadcom Pulled the VDDK: What It Means for VMware Exits — Hystax](https://hystax.com/broadcom-vddk-removed-vmware-migration-impact/)
- [Broadcom Cut Public Access of VDDK Overnight — Platform9](https://platform9.com/blog/vddk-no-longer-available/)
- [openvddk — implémentation open source de libvixDiskLib](https://github.com/maci0/openvddk)
- [Huawei FusionCompute — Storware Backup and Recovery 7.1](https://docs.storware.eu/backup-and-recovery-71/protecting-virtual-machines/virtual-machines/huawei-fusion-compute) *(VRM en 8443, snapshots `normal`/`CBTbackup`, mode de transfert LANSSL, CBT du VRM)*
- [Huawei FusionCompute — Commvault](https://documentation.commvault.com/11.40/commcell-console/huawei_fusioncompute.html) *(architecture à proxy VSA)*
- [FusionCompute 8.3.0 Product Documentation — Huawei](https://info.support.huawei.com/storage/docs/en-us/fc830/product-document/en-us_topic_0000001243353335.html)
- `Downloads/APP/VMware-vix-disklib-8.0.3-23950268.x86_64/include/vixDiskLib.h` *(inspection directe)*
