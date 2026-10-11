# Relevé de plateforme — mode opératoire

À exécuter **depuis ton Mac**. Ni le bac à sable cloud ni la VM Linux du pont
n'ont de route vers ton réseau local : cette dernière n'a que l'interface de
loopback, et le transport NBD du VDDK utilise le port 902 en TCP brut, qu'un
proxy HTTP ne relaie pas. Docker Desktop, lui, atteint bien ton LAN.

Le but est de **mesurer** ce qui est resté supposé dans `ARCHITECTURE.md`
plutôt que de le déduire.

| Question | Section du dossier |
|---|---|
| Version et build de l'hôte : le VDDK 8.0.3 les couvre-t-il ? | D3 |
| Licence : une licence ESXi gratuite rend l'API en lecture seule | §2.5 |
| Format réel du `changeId` | §6.2 |
| Nombre d'appels que la boucle `QueryChangedDiskAreas` exige vraiment | §6.3 |
| Mode de transport réellement négocié, et son débit | §6.4 |
| Empreinte acceptée par le VDDK : SHA-1 ou SHA-256 | §2.1 |

---

## 1. Préparer la VM de test

Sur l'ESXi, une VM **quelconque et jetable**, petite (10–20 Gio suffisent),
que tu acceptes de voir prendre un snapshot. Idéalement allumée : c'est le
cas réel d'un seed à chaud.

L'outil n'écrit **jamais** sur un disque source. Ses seules mutations sont
l'activation du CBT, la création puis la suppression d'un snapshot, et la
suppression a lieu dans un `finally`. Si elle échoue malgré tout, il te dit
la commande de rattrapage.

## 2. Construire l'image

```bash
cd ~/Downloads/APP/v2fc
docker build --platform linux/amd64 -t v2fc-datamover .
```

## 3. Phase 1 — lecture seule

Ne crée rien, ne modifie rien. À lancer en premier, toujours.

```bash
cd ~/Downloads/APP
docker run --rm -it --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" \
  -v "$PWD/lab-out:/out" \
  -e V2FC_VC_HOST=192.168.x.x \
  -e V2FC_VC_USER=root \
  v2fc-datamover \
  python3 /work/tools/lab_report.py --probe --insecure -o /out/probe.json
```

Le mot de passe est demandé de façon interactive : il n'apparaît ni dans
l'historique du shell, ni dans `ps`, ni dans le rapport. `-it` est donc
nécessaire.

Tu obtiens la version de l'hôte, l'état des licences, l'inventaire, et une
pré-vérification de chaque VM avec ses blocages éventuels.

## 4. Phase 2 — cycle de mesure

Sur la VM de test uniquement.

```bash
docker run --rm -it --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" \
  -v "$PWD/lab-out:/out" \
  -e V2FC_VC_HOST=192.168.x.x \
  -e V2FC_VC_USER=root \
  v2fc-datamover \
  python3 /work/tools/lab_report.py --vm NOM-DE-LA-VM --insecure \
          --transport nbdssl:nbd -o /out/mesure.json
```

`--transport nbdssl:nbd` est volontaire : `hotadd` exigerait que le conteneur
soit une VM du cluster source, ce qui n'est pas le cas ici. Le débit mesuré
sera donc celui de NBD sur SSL — le plancher, pas le plafond.

## 5. M'envoyer le résultat

Les deux fichiers atterrissent dans `~/Downloads/APP/lab-out/`, donc dans le
dossier partagé avec la session. Dis-moi simplement qu'ils sont là, je les
lis. Ils ne contiennent ni mot de passe ni jeton — les noms de VM, de
datastore et l'adresse de l'hôte y figurent, relis-les si c'est un
environnement client.

---

## Si ça bloque

**`RestrictedVersion`** — l'hôte est sous licence ESXi gratuite : l'API
vSphere y est en lecture seule, donc ni snapshot ni VDDK. C'est la cause la
plus fréquente sur un lab monté rapidement. Une licence d'évaluation de
60 jours lève le blocage.

**Empreinte refusée** — l'outil essaie SHA-1 puis SHA-256 et note laquelle
passe. Si les deux échouent, le message du VDDK est dans le rapport.

**Snapshot non supprimé** — l'outil le signale et te donne la commande :

```bash
docker run --rm -it --platform linux/amd64 \
  -v "$PWD/vmware-vix-disklib-distrib:/opt/vddk:ro" \
  -e V2FC_VC_HOST=192.168.x.x -e V2FC_VC_USER=root \
  v2fc-datamover \
  python3 /work/tools/v2fc_source.py cleanup --name NOM-DE-LA-VM --insecure
```

**Pas de route vers l'ESXi depuis le conteneur** — vérifier depuis le Mac
d'abord (`nc -vz 192.168.x.x 443`). Si le Mac passe mais pas le conteneur,
ajouter `--network bridge` explicitement, ou tester avec
`docker run --rm alpine ping -c2 192.168.x.x`.
