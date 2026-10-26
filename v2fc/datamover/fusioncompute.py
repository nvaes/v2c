"""
Driver cible FusionCompute — API REST du VRM (ARCHITECTURE.md §7.5, D2
résolu le 17/09/2026).

Rien ici n'est deviné : chaque chemin, en-tête et champ de réponse vient de
la documentation constructeur *FusionCompute 8.10.0 VRM APIs* fournie par
Nicolas (fichier interne, archivé dans docs-fusioncompute/, voir HANDOFF.md
§12 et ARCHITECTURE.md §7.5 pour les citations détaillées).

Portée de ce module : connexion/session, découverte en lecture seule
(sites, clusters, hôtes, datastores, DVS, port groups — étapes 3 à 5 du
flux voulu par Nicolas), et depuis le 17/09/2026 (Nicolas a demandé le
socle L2 complet en une fois — HANDOFF.md §5 sexies/L2) l'écriture cible
(§7.5 ②③④⑤⑦) : création+attachement de volume, détachement, création de
VM avec firmware imposé, démarrage/arrêt de VM. Appelant réel :
`api/routers/jobs.py` (machine à états du job, ARCHITECTURE.md §5).

Ce que ce module NE fait PAS, et qui reste un vrai manque non inventé ici :
il n'existe encore aucun agent réseau tournant à l'intérieur d'une VM
relais FusionCompute pour recevoir les extents envoyés par le Data Mover
(§7.1, §13 « orchestrateur ↔ agents : gRPC sur mTLS ») — ce module ne crée
que les volumes/VM côté VRM, il ne transporte aucune donnée. Tant qu'aucune
VM relais réelle n'existe chez Nicolas, `provision()`/`create_vm()` ne
peuvent pas être exercés contre un vrai VRM, exactement comme le driver de
lecture ne l'a été qu'après le lab du 17/09/2026 — voir HANDOFF.md pour
l'état d'avancement réel de ce test.

Points non vérifiés en laboratoire, à confirmer avant mise en production
(voir ARCHITECTURE.md §7.5 « Ce qui reste à vérifier ») :
  - le port réel de l'API REST (le document dit 7443 ; l'adresse VRM
    donnée par Nicolas utilise 8443, vraisemblablement le port console) ;
  - la forme exacte du hachage du mot de passe : le document dit seulement
    « haché SHA-256 par défaut » sans préciser encodage/casse/sel — on
    utilise ici l'hypothèse la plus directe (SHA-256 hexadécimal minuscule
    du mot de passe UTF-8), NON confirmée sur ce lab ;
  - la clé exacte de la réponse de listage des port groups : le tableau de
    description du document dit `portGroups`, mais son propre exemple de
    réponse JSON dit `portgroups` (incohérence documentaire, comme celle
    déjà relevée en §7.4 sur la limite SCSI) — ce module accepte les deux.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import requests
import urllib3

log = logging.getLogger("v2fc.fusioncompute")

# §7.5 : « le document précise explicitement que le port des API est
# 7443 » — à vérifier en pratique contre ce lab avant de s'y fier (cf.
# note de module ci-dessus).
DEFAULT_API_PORT = 7443

# §7.5 ① : validité du jeton documentée, non paramétrable côté client.
TOKEN_VALIDITY_MS = 600_000


class FusionComputeError(RuntimeError):
    pass


class FusionComputeTaskFailed(FusionComputeError):
    """Une tâche asynchrone VRM a fini en `failed` ou `cancelling` (§7.5)."""


def _urn_tail(urn: str) -> str:
    """« urn:sites:05C9108A:dvswitchs:1 » -> « 1 » ; « urn:sites:05C9108A » -> « 05C9108A »."""
    return urn.rsplit(":", 1)[-1]


def _full_urn(site_id: str, kind: str, urn: str) -> str:
    """« 5 » -> « urn:sites:0A7D0FDA:datastores:5 » ; une URN complète passe telle quelle.

    Constaté le 26/10/2026 sur le VRM de Nicolas : `GET <host_uri>/<id>/datastores`
    renvoie `"urn": "5"` (identifiant nu), alors que `GET <site>/datastores/5`
    renvoie l'URN complète — et `createattachvol` refuse l'identifiant nu
    (`10000001`, « format de requête invalide »).
    """
    return urn if urn.startswith("urn:") else f"urn:sites:{site_id}:{kind}:{urn}"


def guest_disk_path(volume_uuid: str) -> str:
    """
    Chemin stable, dans la VM relais, d'un volume attaché en VIRTIO (bus par
    défaut de `createattachvol`). D5 tranché le 26/10/2026 sur le VRM de
    Nicolas : FusionCompute passe l'UUID du volume comme numéro de série
    virtio, tronqué à 20 caractères — volume 61172379-913f-4377-a541-...
    vu comme /dev/disk/by-id/virtio-61172379-913f-4377-a -> /dev/vdb.
    Aucune déduction par ordre d'attachement (vdb, vdc...) : ce lien
    désigne toujours le bon volume, même attachés en parallèle.
    """
    return f"/dev/disk/by-id/virtio-{volume_uuid[:20]}"


def _error_detail(resp: "requests.Response") -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    return str(body)


@dataclass
class FcSite:
    urn: str
    uri: str
    id: str
    name: str


class FusionComputeClient:
    """
    Connexion à un VRM FusionCompute. À utiliser comme gestionnaire de
    contexte — connexion (§7.5 ①, `POST <basic_uri>/session`) à l'entrée,
    déconnexion (`DELETE <basic_uri>/session`) à la sortie.

    Le site est résolu une fois à la connexion (`GET <basic_uri>/sites`) :
    la plupart des déploiements FusionCompute n'ont qu'un seul site, donc on
    prend le premier par défaut si `site_id` n'est pas fourni. Le support
    multi-site (choix explicite, opérations cross-site) n'est pas implémenté
    — inutile tant qu'aucun cas d'usage réel ne l'exige.
    """

    def __init__(self, host: str, user: str, password: str,
                port: int = DEFAULT_API_PORT, verify: bool = True,
                user_type: int = 2, auth_type: int = 0,
                site_id: str | None = None, timeout: float = 60.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.base_url = f"https://{host}:{port}/service"
        self.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._session = requests.Session()
        self._token: str | None = None
        self._login(user, password, user_type, auth_type)
        self.site: FcSite = self._resolve_site(site_id)
        log.info("connecté au VRM %s:%s — site %s (%s)", host, port,
                 self.site.name, self.site.id)

    # -- session --------------------------------------------------------
    def _login(self, user: str, password: str, user_type: int, auth_type: int) -> None:
        hashed = hashlib.sha256(password.encode("utf-8")).hexdigest()
        headers = {
            "X-Auth-User": user,
            "X-Auth-Key": hashed,
            "X-Auth-UserType": str(user_type),
            "X-Auth-AuthType": str(auth_type),
            "Accept": "application/json",
        }
        try:
            resp = self._session.post(f"{self.base_url}/session", headers=headers,
                                      verify=self.verify, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FusionComputeError(
                f"connexion à {self.host}:{self.port} impossible : {exc}") from exc
        if resp.status_code != 200:
            raise FusionComputeError(
                f"connexion VRM refusée ({resp.status_code}) : {_error_detail(resp)}")
        token = resp.headers.get("X-Auth-Token")
        if not token:
            raise FusionComputeError(
                "réponse de connexion sans en-tête X-Auth-Token (§7.5 ①) — "
                "réponse inattendue du VRM")
        self._token = token

    def close(self) -> None:
        if self._token is None:
            return
        try:
            self._session.delete(f"{self.base_url}/session",
                                 headers=self._auth_headers(), verify=self.verify,
                                 timeout=15)
        except requests.RequestException as exc:
            log.warning("déconnexion VRM %s échouée (ignorée) : %s", self.host, exc)
        finally:
            self._token = None

    def __enter__(self) -> "FusionComputeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _auth_headers(self) -> dict:
        return {"X-Auth-Token": self._token or "", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, headers=self._auth_headers(), params=params,
                                     verify=self.verify, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FusionComputeError(f"GET {path} impossible : {exc}") from exc
        if resp.status_code != 200:
            raise FusionComputeError(
                f"GET {path} -> {resp.status_code} : {_error_detail(resp)}")
        return resp.json() if resp.content else {}

    def _post(self, path: str, body: dict | None = None) -> dict:
        """
        POST générique pour les actions §7.5 ②③④⑤⑦, toutes documentées comme
        asynchrones : la réponse ne contient que `{taskUrn, taskUri}` (ou,
        pour la création de volume/VM, en plus `{urn, uri}`) — c'est
        `wait_task()` qui confirme le résultat réel, jamais ce 200 OK seul
        (§7.5 « Suivi de tâche »).
        """
        url = f"{self.base_url}{path}"
        headers = {**self._auth_headers(), "Content-Type": "application/json; charset=UTF-8"}
        try:
            resp = self._session.post(url, headers=headers, json=body or {},
                                      verify=self.verify, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FusionComputeError(f"POST {path} impossible : {exc}") from exc
        if resp.status_code not in (200, 202):
            raise FusionComputeError(
                f"POST {path} -> {resp.status_code} : {_error_detail(resp)}")
        return resp.json() if resp.content else {}

    # -- sites ------------------------------------------------------------
    def list_sites(self) -> list[FcSite]:
        data = self._get("/sites")
        out = []
        for s in data.get("sites", []):
            urn = s["urn"]
            out.append(FcSite(urn=urn, uri=s.get("uri", ""), id=_urn_tail(urn),
                              name=s.get("name", "")))
        return out

    def _resolve_site(self, site_id: str | None) -> FcSite:
        sites = self.list_sites()
        if not sites:
            raise FusionComputeError("aucun site renvoyé par GET /service/sites")
        if site_id is None:
            if len(sites) > 1:
                log.warning(
                    "plusieurs sites VRM (%d) — on prend le premier (%s, %s) ; "
                    "le support multi-site n'est pas implémenté",
                    len(sites), sites[0].name, sites[0].id)
            return sites[0]
        for s in sites:
            if s.id == site_id:
                return s
        raise FusionComputeError(
            f"site {site_id!r} introuvable parmi {[s.id for s in sites]}")

    # -- clusters / hôtes ---------------------------------------------------
    def list_clusters(self) -> list[dict]:
        """`GET <cluster_uri>` (table maîtresse « Object ID Description », §7.5)."""
        data = self._get(f"/sites/{self.site.id}/clusters")
        return data.get("clusters", [])

    def list_hosts(self, scope: str | None = None) -> list[dict]:
        """
        `GET <host_uri>` — sans `scope`, tous les hôtes du site (documenté
        explicitement : « If the request URI does not contain this
        attribute, all hosts at the site will be queried »).
        """
        params = {"scope": scope} if scope else None
        data = self._get(f"/sites/{self.site.id}/hosts", params=params)
        return data.get("hosts", [])

    # -- datastores -----------------------------------------------------------
    def list_datastores(self) -> list[dict]:
        """
        Pas de « lister tous les datastores du site » en un seul appel dans
        la documentation (seulement par hôte ou par dossier parent) : on
        énumère donc tous les hôtes du site puis leurs datastores associés
        (`GET <host_uri>/<host_id>/datastores`), dédupliqués par `urn`
        puisqu'un datastore partagé est associé à plusieurs hôtes.

        Acceptable pour un inventaire à la demande (quelques hôtes dans un
        lab/petit site) ; à revoir si un site avec beaucoup d'hôtes rend ça
        trop lent — la piste alternative documentée est la requête par
        dossier (`querystoragepool?parentFolderUrn=...`), qui impose de
        connaître un URN de dossier au préalable, pas plus simple ici.
        """
        seen: dict[str, dict] = {}
        for host in self.list_hosts():
            host_id = _urn_tail(host["urn"])
            data = self._get(f"/sites/{self.site.id}/hosts/{host_id}/datastores")
            for ds in data.get("datastores", []):
                ds = {**ds, "urn": _full_urn(self.site.id, "datastores", ds["urn"])}
                seen.setdefault(ds["urn"], ds)
        return list(seen.values())

    # -- réseaux (DVS + port groups) -------------------------------------------
    def list_dvswitches(self) -> list[dict]:
        """`GET <dvswitch_uri>` — « Query information about all DVSs »."""
        data = self._get(f"/sites/{self.site.id}/dvswitchs")
        return data.get("dvSwitchs", [])

    def list_portgroups(self, dvswitch_id: str) -> list[dict]:
        """
        `GET <dvswitch_uri>/<dvswitch_id>/portgroups` — « Query information
        about all port groups on a DVS ». Voir la note de module sur
        l'incohérence `portGroups`/`portgroups` entre la description et
        l'exemple du document : on accepte les deux clés.
        """
        data = self._get(f"/sites/{self.site.id}/dvswitchs/{dvswitch_id}/portgroups")
        return data.get("portgroups", data.get("portGroups", []))

    def list_networks(self) -> list[dict]:
        """
        Réseaux virtuels = port groups de tous les DVS du site (un « réseau »
        FusionCompute au sens où l'entend Nicolas, §9 étape 5), chacun
        annoté du DVS auquel il appartient pour l'affichage.
        """
        out = []
        for dvs in self.list_dvswitches():
            dvs_id = _urn_tail(dvs["urn"])
            for pg in self.list_portgroups(dvs_id):
                out.append({**pg, "dvswitch_urn": dvs["urn"],
                           "dvswitch_name": dvs.get("name", "")})
        return out

    # -- tâches asynchrones (§7.5 — pas encore appelé par ce module, mais
    #    nécessaire dès que ②③④⑤ seront utilisées par L2) ------------------
    def wait_task(self, task_uri: str, timeout: float = 600.0,
                 poll_interval: float = 2.0) -> dict:
        """
        `GET <task_uri>/<task_id>` jusqu'à `status` = success/failed
        (§7.5 « Suivi de tâche ») — ne jamais supposer la réussite au seul
        200 OK initial d'une création/attachement/détachement.
        """
        path = task_uri
        if path.startswith("http"):
            path = path.split("/service", 1)[-1]
        elif path.startswith("/service"):
            path = path[len("/service"):]
        elif not path.startswith("/"):
            path = f"/{path}"
        deadline = time.monotonic() + timeout
        while True:
            data = self._get(path)
            status_ = data.get("status")
            if status_ == "success":
                return data
            if status_ in ("failed", "cancelling"):
                raise FusionComputeTaskFailed(
                    f"tâche {task_uri} en échec ({status_}) : "
                    f"{data.get('reasonDes') or data.get('reason')}")
            if time.monotonic() > deadline:
                raise FusionComputeError(
                    f"tâche {task_uri} : délai dépassé ({timeout:.0f} s)")
            time.sleep(poll_interval)

    # -- lecture d'une VM (nécessaire pour situer la VM relais avant de créer
    #    la VM cible au même endroit, §7.5 « Querying information about a
    #    specified VM ») -----------------------------------------------------
    def get_vm(self, vm_id: str) -> dict:
        """`GET <vm_uri>/<vm_id>` — renvoie notamment `location`/`locationName`
        (cluster ou hôte de la VM), utilisé par le job runner pour créer la
        VM cible dans le même emplacement que la VM relais (aucun autre
        moyen documenté de choisir un emplacement pour l'instant — voir
        api/routers/jobs.py, `_finalize`)."""
        return self._get(f"/sites/{self.site.id}/vms/{vm_id}")

    # -- écriture cible (§7.5 ②③④⑤⑦) — ajouté le 17/09/2026, cf. note de
    #    module : chaque champ vient de « FusionCompute 8.10.0 VRM APIs »
    #    (docs-fusioncompute/), rien n'est deviné. Toutes ces opérations
    #    sont asynchrones : l'appelant doit passer la réponse à `wait_task`
    #    avant de considérer l'effet acquis. ------------------------------

    def create_and_attach_volume(self, vm_id: str, *, name: str, quantity_gb: int,
                                 datastore_urn: str, is_thin: bool = True,
                                 vol_type: str = "normal", indep_disk: bool = True,
                                 persistent_disk: bool = True,
                                 sequence_num: int | None = None) -> dict:
        """
        `POST <vm_uri>/<vm_id>/action/createattachvol` (§7.5 ②, section
        « Creating and Attaching a Disk » du document constructeur) : crée
        un volume et l'attache à `vm_id` en un seul appel — le cas d'usage
        de ce projet (un volume par disque source, jamais réutilisé), pas
        la variante en deux temps (`POST <vol_uri>` puis `attachvol`).

        `indep_disk=True` par défaut : le document précise que le stockage
        bloc n'accepte que des disques indépendants (« Block storage only
        supports independent disks »), et un volume de migration ne doit
        de toute façon jamais être affecté par un snapshot du relais.

        Retourne la réponse brute (`urn`, `uri`, `taskUrn`, `taskUri`) —
        c'est à l'appelant de faire `wait_task(taskUri)` avant de considérer
        le volume prêt (§7.5 « Suivi de tâche »).
        """
        body = {
            "name": name,
            "quantityGB": quantity_gb,
            # Les plans enregistrés avant le 26/10/2026 ont un identifiant nu.
            "datastoreUrn": _full_urn(self.site.id, "datastores", datastore_urn),
            "isThin": is_thin,
            "type": vol_type,
            "indepDisk": indep_disk,
            "persistentDisk": persistent_disk,
        }
        if sequence_num is not None:
            body["sequenceNum"] = sequence_num
        return self._post(f"/sites/{self.site.id}/vms/{vm_id}/action/createattachvol", body)

    def get_volume(self, vol_id: str) -> dict:
        """`GET <vol_uri>/<vol_id>` — notamment `uuid`, voir `guest_disk_path`."""
        return self._get(f"/sites/{self.site.id}/volumes/{vol_id}")

    def attach_volume(self, vm_id: str, *, vol_urn: str, pci_type: str = "VIRTIO",
                      sequence_num: int | None = None, io_mode: str | None = None,
                      access_mode: int | None = None, cache_mode: str | None = None) -> dict:
        """
        `POST <vm_uri>/<vm_id>/action/attachvol` (§7.5 ③) : attache un
        volume déjà existant (créé séparément, ou détaché d'une autre VM —
        c'est le cas à la bascule : détacher du relais puis rattacher à la
        VM cible, §7 « Finalizing »). `pci_type` par défaut VIRTIO comme
        documenté ; RDM/eVol n'acceptent que VIRTIO/SCSI (§7.5 ③, note déjà
        reprise dans ARCHITECTURE.md).
        """
        body: dict = {"volUrn": vol_urn, "pciType": pci_type}
        if sequence_num is not None:
            body["sequenceNum"] = sequence_num
        if io_mode is not None:
            body["ioMode"] = io_mode
        if access_mode is not None:
            body["accessMode"] = access_mode
        if cache_mode is not None:
            body["cacheMode"] = cache_mode
        return self._post(f"/sites/{self.site.id}/vms/{vm_id}/action/attachvol", body)

    def detach_volume(self, vm_id: str, *, vol_urn: str) -> dict:
        """
        `POST <vm_uri>/<vm_id>/action/detachvol` (§7.5 ④). `isFormat` fixé
        à `False` sans option pour l'appeler à `True` : notre contrat est de
        ne jamais écrire ce qui n'est pas explicitement demandé (même
        principe que I1/I2 côté source) — formater au détachement n'a
        aucun cas d'usage dans ce projet.
        """
        return self._post(f"/sites/{self.site.id}/vms/{vm_id}/action/detachvol",
                          {"volUrn": vol_urn, "isFormat": False})

    def create_vm(self, *, name: str, location_urn: str, cpu_count: int,
                  memory_mb: int, boot_firmware: str = "BIOS",
                  nics: list[dict] | None = None, auto_boot: bool = False) -> dict:
        """
        `POST <vm_uri>` (§7.5 ⑤, section « Creating a VM » — Table 3-1 pour
        `vmConfig`) : crée l'enveloppe VM cible, sans disque ni OS — le
        disque migré (déjà porteur de l'OS) est attaché séparément après
        coup via `attach_volume`, pas fourni ici en `disks[]` (cohérent avec
        la note déjà présente dans ARCHITECTURE.md §7.5 ⑤).

        `location_urn` : `clusterUrn` ou `hostUrn` (le document ne
        distingue pas les deux champs, un seul attribut `location` accepte
        les deux formes). `auto_boot=False` par défaut : la VM créée est
        vide, la démarrer avant l'attachement du disque migré n'a aucun
        sens et risquerait un boot sans disque système.
        """
        vm_config: dict = {
            "cpu": {"quantity": cpu_count},
            "memory": {"quantityMB": memory_mb},
            "disks": [],
            "nics": nics or [],
            "properties": {"bootFirmware": boot_firmware},
        }
        body = {
            "name": name,
            "location": location_urn,
            "vmConfig": vm_config,
            "autoBoot": auto_boot,
        }
        return self._post(f"/sites/{self.site.id}/vms", body)

    def power_on(self, vm_id: str) -> dict:
        """`POST <vm_uri>/<vm_id>/action/start` (§7.5 ⑦) — pas de corps."""
        return self._post(f"/sites/{self.site.id}/vms/{vm_id}/action/start")

    def power_off(self, vm_id: str, *, mode: str = "safe") -> dict:
        """
        `POST <vm_uri>/<vm_id>/action/stop` (§7.5 ⑦). `mode="safe"` par
        défaut, comme documenté (« The default value is safe ») — un arrêt
        `force` reste possible en le demandant explicitement, jamais choisi
        silencieusement par ce driver.
        """
        return self._post(f"/sites/{self.site.id}/vms/{vm_id}/action/stop", {"mode": mode})
