"""
Driver source vSphere — inventaire, CBT, snapshots de migration.

Fait respecter mécaniquement l'invariant I1 (ARCHITECTURE.md §4) : la VM
source n'est jamais détruite ni reconfigurée hors des trois mutations
tolérées. Ce n'est pas une convention de revue de code — toute méthode
vSphere non explicitement autorisée lève une exception AVANT l'appel réseau
(voir SourceGuard).

Les trois mutations tolérées, et rien d'autre :
  1. activation du CBT (une fois par VM) ;
  2. création et suppression de snapshots de migration ;
  3. extinction de l'invité à la bascule.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Iterator

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .extents import Extent, query_changed_areas_complete

log = logging.getLogger("v2fc.vsphere")

SNAPSHOT_PREFIX = "v2fc"
FULL_CHANGE_ID = "*"          # demande tous les blocs alloués

# Méthodes vSphere mutantes que ce driver a le droit d'appeler.
# Toute autre méthode mutante est refusée par SourceGuard.
ALLOWED_MUTATIONS = frozenset({
    "CreateSnapshot_Task",
    "RemoveSnapshot_Task",
    "ReconfigVM_Task",          # UNIQUEMENT pour changeTrackingEnabled, cf. enable_cbt
    "ShutdownGuest",
    "PowerOffVM_Task",
    "SetCustomValue",           # annotation « migrée le … »
})

# Méthodes qui ne doivent JAMAIS partir sur le réseau depuis ce driver.
FORBIDDEN = frozenset({
    "Destroy_Task", "UnregisterVM", "DeleteDatastoreFile_Task",
    "RelocateVM_Task", "MigrateVM_Task", "CloneVM_Task",
    "MarkAsTemplate", "ResetVM_Task", "RevertToSnapshot_Task",
    "RevertToCurrentSnapshot_Task", "RemoveAllSnapshots_Task",
    "PowerOnVM_Task",           # rallumer la source est une décision de rollback,
                                # prise par l'orchestrateur, pas par le driver
})


class SourceMutationRefused(PermissionError):
    """Tentative d'appel d'une méthode interdite sur la VM source."""


class VSphereError(RuntimeError):
    pass


class CbtInvalidated(VSphereError):
    """
    Le changeId de référence n'est plus exploitable.

    Ne JAMAIS rattraper cette exception en continuant le delta : le seul
    comportement acceptable est un re-seed complet avec alerte
    (ARCHITECTURE.md §6.3).
    """


# --------------------------------------------------------------------------
# Garde-fou
# --------------------------------------------------------------------------
class SourceGuard:
    """
    Enveloppe un objet managé vSphere et intercepte les accès aux méthodes.

    Le contrôle porte sur le NOM de la méthode, avant toute émission réseau.
    Les propriétés de lecture passent sans entrave.
    """

    __slots__ = ("_obj",)

    def __init__(self, obj) -> None:
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str):
        if name in FORBIDDEN:
            raise SourceMutationRefused(
                f"{name} est interdite sur une VM source (invariant I1). "
                f"L'outil ne détruit ni ne déplace jamais la source.")
        attr = getattr(self._obj, name)
        if callable(attr) and name.endswith(("_Task", "Guest")) \
                and name not in ALLOWED_MUTATIONS:
            raise SourceMutationRefused(
                f"{name} n'est pas dans la liste blanche des mutations "
                f"autorisées sur la source : {sorted(ALLOWED_MUTATIONS)}")
        return attr

    def __setattr__(self, name, value):
        raise SourceMutationRefused("modification directe d'une propriété refusée")

    @property
    def raw(self):
        """Accès non protégé — réservé aux lectures internes du driver."""
        return self._obj

    def __repr__(self) -> str:
        return f"SourceGuard({self._obj!r})"


# --------------------------------------------------------------------------
# Modèle de disque
# --------------------------------------------------------------------------
@dataclass
class DiskSpec:
    device_key: int
    unit_number: int
    controller_key: int
    label: str
    file_name: str                # « [ds] VM/VM.vmdk »
    capacity_bytes: int
    disk_mode: str
    sharing: str | None
    thin: bool | None
    is_rdm: bool
    rdm_compatibility: str | None
    change_id: str | None         # changeId courant porté par le backing

    @property
    def is_independent(self) -> bool:
        return "independent" in (self.disk_mode or "")

    @property
    def is_physical_rdm(self) -> bool:
        return self.is_rdm and self.rdm_compatibility == "physicalMode"

    @property
    def is_multi_writer(self) -> bool:
        return self.sharing == "sharingMultiWriter"


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------
def fetch_thumbprint(host: str, port: int = 443, digest: str = "sha1") -> str:
    """
    Empreinte du certificat, au format « AA:BB:… » attendu par le VDDK.

    VDDK 8 accepte historiquement SHA-1 ; les versions récentes acceptent
    aussi SHA-256. [À VALIDER en laboratoire contre le vCenter cible avant
    de changer le défaut.]
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    h = hashlib.new(digest, der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


class VSphereClient:
    """Connexion vCenter/ESXi. À utiliser comme gestionnaire de contexte."""

    def __init__(self, host: str, user: str, password: str, port: int = 443,
                 verify: bool = True, thumbprint: str | None = None) -> None:
        self.host, self.port, self.user = host, port, user
        self.thumbprint = thumbprint or fetch_thumbprint(host, port)
        if verify:
            ctx = None            # contexte système, vérification active
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            log.warning("vérification TLS désactivée pour %s — empreinte épinglée "
                        "%s", host, self.thumbprint)
        self.si = SmartConnect(host=host, user=user, pwd=password, port=port,
                               sslContext=ctx)
        self.content = self.si.RetrieveContent()
        about = self.content.about
        self.version = about.version
        self.build = about.build
        log.info("connecté à %s — %s %s build %s", host, about.fullName,
                 about.version, about.build)

    def close(self) -> None:
        if self.si:
            Disconnect(self.si)
            self.si = None

    def __enter__(self) -> "VSphereClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- inventaire ------------------------------------------------------
    def _view(self, kind, root=None):
        mgr = self.content.viewManager.CreateContainerView(
            root or self.content.rootFolder, [kind], True)
        try:
            return list(mgr.view)
        finally:
            mgr.Destroy()

    def datacenters(self) -> list:
        return self._view(vim.Datacenter)

    def inventory(self) -> list[dict]:
        """Arbre simplifié pour l'UI : datacenter → cluster → VM."""
        out = []
        for dc in self.datacenters():
            clusters = []
            for cr in self._view(vim.ComputeResource, dc.hostFolder):
                clusters.append({
                    "name": cr.name,
                    "moref": str(cr._moId),
                    "hosts": [h.name for h in cr.host],
                })
            vms = []
            for vm in self._view(vim.VirtualMachine, dc.vmFolder):
                if vm.config is None or vm.config.template:
                    continue
                disks = [d for d in vm.config.hardware.device
                         if isinstance(d, vim.vm.device.VirtualDisk)]
                vms.append({
                    "name": vm.name,
                    "moref": str(vm._moId),
                    "instance_uuid": vm.config.instanceUuid,
                    "power_state": str(vm.runtime.powerState),
                    "guest_id": vm.config.guestId,
                    "num_disks": len(disks),
                    # Même repli que DiskSpec.capacity_bytes (cf. SourceVm.disks()
                    # plus bas) : capacityInBytes si présent, sinon capacityInKB*1024.
                    "disk_capacity_bytes": sum(
                        int(d.capacityInBytes or (d.capacityInKB or 0) * 1024)
                        for d in disks),
                    "cbt": bool(vm.config.changeTrackingEnabled),
                })
            out.append({"name": dc.name, "moref": str(dc._moId),
                        "clusters": clusters, "vms": vms})
        return out

    def find_vm(self, *, instance_uuid: str | None = None,
                moref: str | None = None, name: str | None = None) -> "SourceVm":
        """
        Recherche par identité stable de préférence : instanceUuid survit au
        renommage et au déplacement, contrairement au nom.
        """
        if instance_uuid:
            vm = self.content.searchIndex.FindByUuid(
                None, instance_uuid, True, True)
            if vm is None:
                raise VSphereError(f"aucune VM d'instanceUuid {instance_uuid}")
            return SourceVm(self, vm)
        for vm in self._view(vim.VirtualMachine):
            if (moref and str(vm._moId) == moref) or (name and vm.name == name):
                return SourceVm(self, vm)
        raise VSphereError(f"VM introuvable (moref={moref}, name={name})")

    def wait_task(self, task, timeout: float = 3600.0, label: str = "tâche"):
        deadline = time.monotonic() + timeout
        while task.info.state in (vim.TaskInfo.State.running,
                                  vim.TaskInfo.State.queued):
            if time.monotonic() > deadline:
                raise VSphereError(f"{label} : délai dépassé ({timeout:.0f} s)")
            time.sleep(0.5)
        if task.info.state == vim.TaskInfo.State.error:
            raise VSphereError(f"{label} : {task.info.error.msg}")
        return task.info.result


# --------------------------------------------------------------------------
# VM source
# --------------------------------------------------------------------------
class SourceVm:
    """
    Vue d'une VM source. Toute mutation passe par SourceGuard.

    `.raw` donne l'objet pyVmomi brut pour les lectures ; il n'est jamais
    exposé aux couches supérieures.
    """

    def __init__(self, client: VSphereClient, vm) -> None:
        self.client = client
        self.raw = vm
        self.guarded = SourceGuard(vm)

    # -- identité --------------------------------------------------------
    @property
    def name(self) -> str:
        return self.raw.name

    @property
    def moref(self) -> str:
        return str(self.raw._moId)

    @property
    def instance_uuid(self) -> str:
        return self.raw.config.instanceUuid

    @property
    def power_state(self) -> str:
        return str(self.raw.runtime.powerState)

    @property
    def firmware(self) -> str:
        return self.raw.config.firmware or "bios"

    @property
    def guest_id(self) -> str:
        return self.raw.config.guestId or ""

    def nics(self) -> list[dict]:
        """
        Cartes réseau, dans l'ordre des clés de périphérique : clé, libellé,
        type d'adaptateur (E1000e, Vmxnet3...), MAC et réseau source. Le nom
        d'un port group distribué n'est pas dans le backing (seulement sa
        clé) : il est résolu via `vm.network`.
        """
        dvs_names = {net.key: net.name for net in (self.raw.network or [])
                     if isinstance(net, vim.dvs.DistributedVirtualPortgroup)}
        out = []
        for dev in self.raw.config.hardware.device:
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                continue
            backing = dev.backing
            if isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                network = dvs_names.get(backing.port.portgroupKey, backing.port.portgroupKey)
            elif isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                network = backing.deviceName
            else:
                network = type(backing).__name__
            out.append({
                "device_key": dev.key,
                "label": dev.deviceInfo.label if dev.deviceInfo else f"NIC {dev.key}",
                "adapter": type(dev).__name__.rsplit(".", 1)[-1].replace("Virtual", ""),
                "mac": dev.macAddress or "",
                "network": network or "",
            })
        return sorted(out, key=lambda n: n["device_key"])

    @property
    def is_encrypted(self) -> bool:
        """VM chiffrée : le VDDK ne peut pas lire ses disques."""
        cfg = self.raw.config
        return bool(getattr(cfg, "keyId", None))

    @property
    def managed_by(self) -> str | None:
        """
        Clé de la solution propriétaire, si la VM est une VM d'agent.

        vCLS, les agents NSX, vSAN ou EAM portent `config.managedBy`. Ce sont
        des VM déployées et recréées automatiquement par vCenter : les migrer
        n'a aucun sens et leur disparition côté source serait rattrapée
        d'elle-même, pas celle côté cible.
        """
        mb = getattr(self.raw.config, "managedBy", None)
        if not mb:
            return None
        key = getattr(mb, "extensionKey", None) or "inconnu"
        kind = getattr(mb, "type", None)
        return f"{key}/{kind}" if kind else key

    @property
    def agent_signal(self) -> str | None:
        """
        Second indice qu'il s'agit d'une VM d'agent, quand `managedBy` est
        absent.

        Relevé sur le laboratoire : la VM `vCLS-…` de l'ESXi de test ne porte
        AUCUN `managedBy` vu depuis une session ouverte directement sur
        l'hôte — la propriété est renseignée par vCenter, que l'on contourne.
        Le type d'invité reste, lui, parfaitement révélateur : `crxSys1Guest`
        désigne un conteneur système CRX, jamais une charge de travail.
        """
        guest = (self.guest_id or "").lower()
        if guest.startswith("crxsys"):
            return f"type d'invité système {self.guest_id}"
        name = (self.name or "").lower()
        for prefix in ("vcls-", "nsx-", "vmware-vcls", "vsan-"):
            if name.startswith(prefix):
                return f"nom réservé aux VM d'agent ({self.name})"
        return None

    @property
    def host_connection(self) -> bool:
        """
        True si la session est ouverte directement sur un ESXi plutôt que sur
        un vCenter. Conséquence opérationnelle : `PrepareForAccess` ne peut
        pas suspendre DRS et vMotion, puisque ces décisions appartiennent au
        vCenter que l'on contourne.
        """
        if self.client is None:
            return False
        return str(self.client.content.about.apiType) == "HostAgent"

    @property
    def cbt_enabled(self) -> bool:
        return bool(self.raw.config.changeTrackingEnabled)

    @property
    def tools_status(self) -> str:
        return str(self.raw.guest.toolsStatus) if self.raw.guest else "unknown"

    @property
    def snapshots(self) -> list:
        sn = self.raw.snapshot
        if not sn:
            return []
        out = []

        def walk(nodes):
            for n in nodes:
                out.append(n)
                walk(n.childSnapshotList or [])
        walk(sn.rootSnapshotList or [])
        return out

    # -- chemins ---------------------------------------------------------
    @property
    def datacenter_path(self) -> str:
        """
        Chemin d'inventaire du datacenter, requis par le vmxSpec du VDDK.

        Remonte jusqu'au Datacenter, puis reconstruit son chemin depuis la
        racine en excluant le dossier racine lui-même (celui dont le parent
        est None). Sur un ESXi autonome, il n'y a qu'un datacenter implicite
        et le résultat est « ha-datacenter ».
        """
        node = self.raw
        while node is not None and not isinstance(node, vim.Datacenter):
            node = node.parent
        if node is None:
            raise VSphereError(f"aucun datacenter au-dessus de {self.name}")

        parts = [node.name]
        parent = node.parent
        while parent is not None and parent.parent is not None:
            parts.append(parent.name)
            parent = parent.parent
        return "/".join(reversed(parts))

    @property
    def vmx_spec(self) -> str:
        """
        « MaVM/MaVM.vmx?dcPath=Chemin/Datacenter&dsName=datastore1 »

        Format imposé par VixDiskLibConnectParams.vmxSpec.

        HANDOFF §5 quater : sur le lab du 15/09/2026 (connexion directe à un
        hôte ESXi, sans vCenter), cette forme est rejetée par le VDDK à
        l'ouverture du disque (« Invalid search specifier »), alors que
        `moref=<self.moref>` fonctionne. Pas re-vérifié via vCenter — ne pas
        utiliser cette propriété seule pour se connecter en production, voir
        `vmx_spec_candidates()` ci-dessous et `vddk.Connection.vsphere_auto`.
        """
        path = self.raw.config.files.vmPathName          # « [ds] VM/VM.vmx »
        m = re.match(r"^\[(?P<ds>[^\]]+)\]\s*(?P<rel>.+)$", path)
        if not m:
            raise VSphereError(f"vmPathName inattendu : {path!r}")
        return (f"{m.group('rel')}?dcPath={self.datacenter_path}"
                f"&dsName={m.group('ds')}")

    @staticmethod
    def _parse_bracket_path(file_name: str) -> tuple[str, str]:
        """Sépare « [datastore] chemin/relatif » en (datastore, chemin)."""
        m = re.match(r"^\[(?P<ds>[^\]]+)\]\s*(?P<rel>.+)$", file_name)
        if not m:
            raise VSphereError(f"chemin de disque inattendu : {file_name!r}")
        return m.group("ds"), m.group("rel")

    def absolute_disk_path(self, file_name: str) -> str:
        """
        Résout « [datastore] chemin/relatif » vers le chemin VMFS absolu réel
        (/vmfs/volumes/<uuid>/chemin/relatif), en interrogeant l'URL du
        datastore (vim.Datastore.info.url) plutôt qu'en la devinant.

        Hypothèse de travail, PAS un fait documenté : Broadcom a fermé
        l'accès public à la documentation VDDK (§2.4), donc le format de
        chemin attendu par VixDiskLib_Open en connexion directe à un hôte
        (sans vCenter) n'a pas pu être confirmé par une source officielle.
        Elle répond au blocage du §5 du HANDOFF : le journal VDDK y montre
        VixDiskLib_Open échouant à résoudre lui-même la notation « [ds] »
        (VixError=0x1, « Invalid search specifier »). Fournir directement le
        chemin absolu contourne cette résolution interne — à vérifier en
        conditions réelles, pas à supposer acquis.
        """
        ds_name, rel = self._parse_bracket_path(file_name)
        for ds in self.raw.datastore:
            if ds.name == ds_name:
                url = ds.info.url or ""
                m = re.match(r"^(?:ds://)?(/vmfs/volumes/[^/]+)/?$", url)
                if not m:
                    raise VSphereError(
                        f"URL de datastore inattendue pour {ds_name!r} : {url!r} "
                        "— probablement pas un volume VMFS local, hypothèse "
                        "non applicable telle quelle")
                return f"{m.group(1)}/{rel}"
        raise VSphereError(
            f"datastore {ds_name!r} introuvable parmi ceux visibles par la VM")

    # -- disques ---------------------------------------------------------
    @staticmethod
    def _hardware_of(snapshot):
        """
        Accepte indifféremment un `vim.vm.Snapshot` ou son `config`
        (`vim.vm.ConfigInfo`).

        Les deux se ressemblent assez pour qu'on se trompe, et l'erreur ne se
        voit qu'à l'exécution sur une plateforme réelle — c'est exactement ce
        qui s'est produit au premier cycle de mesure. On tolère les deux
        plutôt que d'imposer à l'appelant de deviner.
        """
        if hasattr(snapshot, "hardware"):        # déjà un ConfigInfo
            return snapshot.hardware.device
        if hasattr(snapshot, "config"):          # un Snapshot managé
            return snapshot.config.hardware.device
        raise VSphereError(
            f"objet de snapshot inattendu : {type(snapshot).__name__} — "
            f"fournir un vim.vm.Snapshot ou son .config")

    def disks(self, snapshot=None) -> list[DiskSpec]:
        """
        Disques de la VM, ou ceux figés dans un snapshot.

        Lire depuis le matériel du snapshot est essentiel : après création
        d'un snapshot, le backing courant de la VM pointe sur le delta en
        écriture, alors que le point cohérent à copier est celui du snapshot,
        et c'est lui qui porte le `changeId` de référence du cycle.
        """
        devices = (self._hardware_of(snapshot) if snapshot is not None
                   else self.raw.config.hardware.device)
        out = []
        for d in devices:
            if not isinstance(d, vim.vm.device.VirtualDisk):
                continue
            b = d.backing
            is_rdm = isinstance(b, vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo)
            out.append(DiskSpec(
                device_key=d.key,
                unit_number=d.unitNumber,
                controller_key=d.controllerKey,
                label=d.deviceInfo.label if d.deviceInfo else f"disk-{d.key}",
                file_name=getattr(b, "fileName", ""),
                capacity_bytes=int(d.capacityInBytes or
                                   (d.capacityInKB or 0) * 1024),
                disk_mode=getattr(b, "diskMode", "") or "",
                sharing=getattr(b, "sharing", None),
                thin=getattr(b, "thinProvisioned", None),
                is_rdm=is_rdm,
                rdm_compatibility=getattr(b, "compatibilityMode", None) if is_rdm else None,
                change_id=getattr(b, "changeId", None),
            ))
        return sorted(out, key=lambda x: (x.controller_key, x.unit_number))

    # -- CBT -------------------------------------------------------------
    def enable_cbt(self, wait_stun: bool = True) -> bool:
        """
        Active le Changed Block Tracking. Renvoie True s'il a fallu l'activer.

        L'activation ne prend effet qu'après un cycle de « stun ». Un cycle
        d'alimentation en est un, mais il impose un arrêt de l'invité : on
        provoque donc le stun par une création puis suppression immédiate de
        snapshot, qui est non intrusive.
        """
        if self.cbt_enabled:
            return False
        log.info("activation du CBT sur %s", self.name)
        spec = vim.vm.ConfigSpec(changeTrackingEnabled=True)
        self.client.wait_task(self.guarded.ReconfigVM_Task(spec=spec),
                              label="ReconfigVM(CBT)")
        if wait_stun:
            snap = self.create_snapshot(f"{SNAPSHOT_PREFIX}-cbt-stun")
            self.remove_snapshot(snap)
        return True

    def query_changed_areas(self, snapshot, disk: DiskSpec,
                            change_id: str) -> list[Extent]:
        """
        Zones modifiées depuis `change_id`, avec la boucle de couverture
        complète (ARCHITECTURE.md §6.3).

        Lève CbtInvalidated si vSphere refuse le changeId ou n'avance pas :
        l'appelant DOIT alors repasser en seed complet, jamais poursuivre.
        """
        def call(offset: int):
            return self.raw.QueryChangedDiskAreas(
                snapshot=snapshot, deviceKey=disk.device_key,
                startOffset=offset, changeId=change_id)
        try:
            return query_changed_areas_complete(call, disk.capacity_bytes, change_id)
        except (vim.fault.FileFault, vim.fault.NotFound,
                vmodl.fault.InvalidArgument, vmodl.fault.NotSupported) as exc:
            raise CbtInvalidated(
                f"CBT inexploitable sur {disk.label} de {self.name} "
                f"(changeId={change_id!r}) : {exc.msg if hasattr(exc, 'msg') else exc}. "
                f"Re-seed complet obligatoire.") from exc
        except ValueError as exc:                # PlanError du planificateur
            raise CbtInvalidated(str(exc)) from exc

    def validate_cbt_continuity(self, disk: DiskSpec,
                                expected_capacity: int) -> None:
        """
        Contrôle préalable à chaque delta : une capacité qui a changé
        invalide le changeId de référence sans qu'aucune erreur ne remonte.
        """
        if disk.capacity_bytes != expected_capacity:
            raise CbtInvalidated(
                f"{disk.label} : capacité passée de {expected_capacity:,} à "
                f"{disk.capacity_bytes:,} octets — le changeId de référence "
                f"n'est plus valide. Re-seed complet obligatoire.")

    # -- snapshots -------------------------------------------------------
    def create_snapshot(self, name: str, quiesce: bool = False,
                        description: str = "snapshot de migration V2FC"):
        log.info("création du snapshot %s sur %s (quiesce=%s)",
                 name, self.name, quiesce)
        task = self.guarded.CreateSnapshot_Task(
            name=name, description=description, memory=False, quiesce=quiesce)
        self.client.wait_task(task, label=f"CreateSnapshot({name})")
        for node in self.snapshots:
            if node.name == name:
                return node.snapshot
        raise VSphereError(f"snapshot {name} créé mais introuvable")

    def remove_snapshot(self, snapshot, consolidate: bool = True) -> None:
        log.info("suppression du snapshot sur %s (consolidate=%s)",
                 self.name, consolidate)
        task = snapshot.RemoveSnapshot_Task(removeChildren=False,
                                            consolidate=consolidate)
        self.client.wait_task(task, timeout=7200, label="RemoveSnapshot")

    def cleanup_migration_snapshots(self) -> int:
        """
        Purge les snapshots laissés par un job interrompu.

        À appeler sur toute sortie en échec : un snapshot de migration oublié
        fait croître le delta VMDK jusqu'à saturer le datastore source.
        """
        removed = 0
        for node in self.snapshots:
            if node.name.startswith(SNAPSHOT_PREFIX):
                self.remove_snapshot(node.snapshot)
                removed += 1
        return removed

    # -- bascule ---------------------------------------------------------
    def shutdown_guest(self, timeout: float = 300.0, force: bool = True) -> str:
        """
        Arrêt propre par VMware Tools, extinction forcée au bout du délai.

        Renvoie « graceful » ou « forced ». Ne supprime rien : la VM reste
        présente et redémarrable, c'est la base du rollback.
        """
        if self.power_state == "poweredOff":
            return "already-off"
        if self.tools_status in ("toolsOk", "toolsOld"):
            self.guarded.ShutdownGuest()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.power_state == "poweredOff":
                    return "graceful"
                time.sleep(2)
            log.warning("%s n'a pas répondu à ShutdownGuest en %.0f s",
                        self.name, timeout)
        else:
            log.warning("%s : VMware Tools indisponible (%s), pas d'arrêt propre "
                        "possible", self.name, self.tools_status)
        if not force:
            raise VSphereError(f"{self.name} toujours allumée et force=False")
        self.client.wait_task(self.guarded.PowerOffVM_Task(), label="PowerOffVM")
        return "forced"

    def annotate(self, key: str, value: str) -> None:
        """Marque la source après bascule, sans rien modifier d'autre."""
        self.guarded.SetCustomValue(key=key, value=value)


def vmx_spec_candidates(vm: "SourceVm") -> list[tuple[str, str]]:
    """
    Formes de vmxSpec à essayer pour VixDiskLib_ConnectEx, dans l'ordre.

    HANDOFF §5 quater : sur ESXi 8.0.3 en connexion directe à l'hôte (sans
    vCenter), `vm.vmx_spec` (« <vmx>?dcPath=...&dsName=... ») est rejeté par
    le VDDK à l'ouverture du disque, alors que `moref=<vm.moref>` fonctionne.
    Non re-vérifié via vCenter ni sur une autre version d'ESXi/VDDK : on
    essaie donc toujours les deux, dans cet ordre (le format documenté
    d'abord, l'alternative éprouvée en second), plutôt que de figer un choix
    sur la seule preuve d'un lab. Utiliser avec
    `datamover.vddk.Connection.vsphere_auto`.
    """
    return [
        ("vmx + dcPath/dsName", vm.vmx_spec),
        ("moref", f"moref={vm.moref}"),
    ]
