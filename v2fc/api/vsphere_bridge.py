"""
Pont synchrone vers `datamover.vsphere` (pyVmomi, donc bloquant). Les
routeurs FastAPI l'appellent via `starlette.concurrency.run_in_threadpool`
plutôt que d'en faire un faux-async — pyVmomi ne sait de toute façon pas
attendre autre chose qu'un thread bloqué.

Rien ici n'invente de comportement vSphere : c'est une fine couche au-dessus
du driver source déjà éprouvé sur ESXi réel (docs/dev/JOURNAL.md §5 quinquies).
"""
from __future__ import annotations

import hashlib
import ssl

from datamover.preflight import run_preflight
from datamover.vsphere import VSphereClient, VSphereError, fetch_thumbprint

from .cache import set_inventory_progress_sync


def probe(host: str, user: str, password: str, port: int, verify_tls: bool) -> dict:
    """Connexion + repli immédiat. Ne lève pas : renvoie ok=False avec le message."""
    try:
        with VSphereClient(host, user, password, port=port, verify=verify_tls) as client:
            about = client.content.about
            return {
                "ok": True,
                "message": f"{about.fullName} (build {about.build})",
                "platform": f"{about.fullName}",
                "tls_fingerprint_sha1": client.thumbprint,
            }
    except VSphereError as exc:
        return {"ok": False, "message": str(exc), "platform": None,
                "tls_fingerprint_sha1": None}
    except Exception as exc:                            # noqa: BLE001
        # une erreur de connexion pyVmomi brute (auth, réseau...) n'est pas
        # une VSphereError : on la rapporte quand même comme un échec de
        # sonde plutôt que de laisser remonter une 500 nue.
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "platform": None, "tls_fingerprint_sha1": None}


def inventory(host: str, user: str, password: str, port: int, verify_tls: bool,
             endpoint_id=None) -> dict:
    """
    Arbre datacenters/clusters/VM, chaque VM enrichie d'un statut de
    pré-vérification (datamover.preflight.run_preflight), pour que la
    sélection multiple dans l'UI sache déjà quelles VM sont migrables.

    `endpoint_id`, si fourni, publie une progression dans Redis au fil du
    scan (api/cache.py, `set_inventory_progress_sync`) — lue par la route de
    poll `GET /endpoints/{id}/inventory/progress` pendant que cet appel,
    potentiellement long, tourne dans son thread bloquant (demande de
    l'utilisateur du 17/09/2026 : un statut d'avancement pendant l'inventaire).
    Optionnel (`None` par défaut) pour ne pas exiger un contexte d'endpoint
    d'un appel direct sans route HTTP (tests, CLI future) : dans ce cas,
    aucune publication n'a lieu.
    """
    def _progress(phase: str, done: int, total: int) -> None:
        if endpoint_id is not None:
            set_inventory_progress_sync(endpoint_id, phase, done, total)

    _progress("connecting", 0, 0)
    with VSphereClient(host, user, password, port=port, verify=verify_tls) as client:
        raw = client.inventory()
        total = sum(len(dc["vms"]) for dc in raw)
        _progress("preflight", 0, total)

        vm_count = 0
        datacenters = []
        for dc in raw:
            clusters = [{"name": c["name"], "moref": c["moref"], "hosts": c["hosts"]}
                       for c in dc["clusters"]]
            vms = []
            for v in dc["vms"]:
                vm_count += 1
                migratable, findings, firmware = True, [], ""
                try:
                    source_vm = client.find_vm(instance_uuid=v["instance_uuid"])
                    firmware = source_vm.firmware
                    report = run_preflight(source_vm)
                    migratable = report.ok
                    findings = [str(f) for f in report.findings]
                except VSphereError as exc:
                    migratable = False
                    findings = [f"pré-vérification impossible : {exc}"]
                vms.append({
                    "moref": v["moref"], "instance_uuid": v["instance_uuid"],
                    "name": v["name"], "power_state": v["power_state"],
                    "guest_id": v["guest_id"], "firmware": firmware,
                    "num_disks": v["num_disks"],
                    # Go, pour homogénéité avec les tailles FusionCompute
                    # (FcDatastore.actual_free_gb) affichées ailleurs dans l'UI.
                    "disk_capacity_gb": round(v["disk_capacity_bytes"] / (1024 ** 3), 1),
                    "cbt_enabled": v["cbt"],
                    "migratable": migratable, "preflight_findings": findings,
                })
                _progress("preflight", vm_count, total)
            datacenters.append({"name": dc["name"], "moref": dc["moref"],
                                "clusters": clusters, "vms": vms})
        _progress("done", vm_count, total)
        return {"datacenters": datacenters, "vm_count": vm_count}


def list_disks(host: str, user: str, password: str, port: int, verify_tls: bool,
              instance_uuid: str) -> list[dict]:
    """
    Disques réels d'une VM (`datamover.vsphere.SourceVm.disks()`), pour
    peupler `job_disks` au provisioning d'un job (ARCHITECTURE.md §8.1) —
    aucun champ inventé, reflet direct de `DiskSpec`.
    """
    with VSphereClient(host, user, password, port=port, verify=verify_tls) as client:
        vm = client.find_vm(instance_uuid=instance_uuid)
        return [{
            "device_key": d.device_key, "unit_number": d.unit_number,
            "src_path": d.file_name, "capacity_bytes": d.capacity_bytes,
            "disk_mode": d.disk_mode,
        } for d in vm.disks()]


def preflight_many(host: str, user: str, password: str, port: int, verify_tls: bool,
                   instance_uuids: list[str]) -> list[dict]:
    out = []
    with VSphereClient(host, user, password, port=port, verify=verify_tls) as client:
        for iu in instance_uuids:
            try:
                vm = client.find_vm(instance_uuid=iu)
                report = run_preflight(vm)
                out.append({"instance_uuid": iu, "name": vm.name, "ok": report.ok,
                           "findings": [f.__dict__ for f in report.findings]})
            except VSphereError as exc:
                out.append({"instance_uuid": iu, "name": "?", "ok": False,
                           "findings": [{"severity": "BLOCK", "code": "INTROUVABLE",
                                        "message": str(exc), "remedy": "", "subject": ""}]})
    return out


def nics_many(host: str, user: str, password: str, port: int, verify_tls: bool,
              instance_uuids: list[str]) -> dict[str, list[dict]]:
    """Cartes réseau de chaque VM (SourceVm.nics), une seule connexion. VM
    introuvable : liste vide plutôt qu'une erreur pour tout le plan."""
    out: dict[str, list[dict]] = {}
    with VSphereClient(host, user, password, port=port, verify=verify_tls) as client:
        for iu in instance_uuids:
            try:
                out[iu] = client.find_vm(instance_uuid=iu).nics()
            except VSphereError:
                out[iu] = []
    return out


def is_windows_guest(guest_id: str, guest_name: str) -> bool:
    """Invité Windows d'après vSphere : guestId « windows9Server64Guest »,
    mais aussi « winNetStandardGuest » (2003), « winLonghorn64Guest » (2008)."""
    return guest_id.lower().startswith("win") or "windows" in guest_name.lower()


def guests_many(host: str, user: str, password: str, port: int, verify_tls: bool,
                instance_uuids: list[str]) -> dict[str, dict]:
    """OS invité de chaque VM, une seule connexion : nom remonté par VMware
    Tools s'il existe, sinon celui de la configuration (comme
    `jobs._source_hardware`). VM introuvable : absente du résultat."""
    out: dict[str, dict] = {}
    with VSphereClient(host, user, password, port=port, verify=verify_tls) as client:
        for iu in instance_uuids:
            try:
                raw = client.find_vm(instance_uuid=iu).raw
            except VSphereError:
                continue
            guest_id = raw.config.guestId or ""
            guest_name = ((raw.guest.guestFullName if raw.guest else None)
                          or raw.config.guestFullName or "")
            out[iu] = {"guest_id": guest_id, "guest_name": guest_name,
                       "windows": is_windows_guest(guest_id, guest_name)}
    return out
