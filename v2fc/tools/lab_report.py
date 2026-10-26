#!/usr/bin/env python3
"""
lab_report — relevé de plateforme vSphere pour le projet V2FC.

Répond aux questions restées ouvertes dans ARCHITECTURE.md, en les mesurant
au lieu de les supposer :

    D3   version et build de l'hôte — le VDDK 8.0.3 les couvre-t-il ?
    §2.5 licence : une licence ESXi gratuite rend l'API vSphere en LECTURE
         SEULE, ce qui interdit snapshots et VDDK. Détecté explicitement.
    §6.2 format réel du changeId, et nombre d'appels que la boucle de
         couverture de QueryChangedDiskAreas exige vraiment
    §6.4 mode de transport RÉELLEMENT négocié, et son débit
    §2.1 empreinte de certificat acceptée par le VDDK : SHA-1 ou SHA-256

Deux phases :

    --probe   LECTURE SEULE. Ne crée rien, ne modifie rien. À lancer d'abord.
    --vm NOM  Cycle de mesure sur UNE VM : active le CBT si besoin, crée un
              snapshot de migration, lit quelques extents, supprime le
              snapshot. Ce sont les trois seules mutations tolérées par
              l'invariant I3, et le snapshot est supprimé dans un `finally`.

Aucune écriture n'est jamais émise vers un disque source (invariant I2).

Produit un rapport JSON à joindre à l'analyse, plus une sortie lisible.

    export V2FC_VC_HOST=192.168.x.x
    export V2FC_VC_USER=root
    export V2FC_VC_PASSWORD='...'
    export VDDK_DISTRIB=/opt/vddk
    export LD_LIBRARY_PATH="$VDDK_DISTRIB/lib64"

    python3 tools/lab_report.py --probe --insecure
    python3 tools/lab_report.py --vm MA-VM-DE-TEST --insecure
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyVmomi import vim, vmodl

from datamover import extents as ex
from datamover import vddk
from datamover.preflight import run_preflight
from datamover.vsphere import (SNAPSHOT_PREFIX, CbtInvalidated, VSphereClient,
                               fetch_thumbprint)

log = logging.getLogger("v2fc.lab")
FULL = "*"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Report:
    def __init__(self) -> None:
        self.data: dict = {"generated_at": now(), "tool": "v2fc lab_report/1.0",
                           "client": {"python": platform.python_version(),
                                      "machine": platform.machine()},
                           "sections": {}}

    def section(self, name: str) -> dict:
        return self.data["sections"].setdefault(name, {})

    def fail(self, name: str, exc: BaseException) -> None:
        self.section(name)["error"] = {
            "type": type(exc).__name__, "message": str(exc),
            "trace": traceback.format_exc(limit=3)}
        log.error("%s : %s — %s", name, type(exc).__name__, exc)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False,
                                   default=str))


# --------------------------------------------------------------------------
# Phase 1 — lecture seule
# --------------------------------------------------------------------------
def probe_platform(client: VSphereClient, rep: Report) -> None:
    about = client.content.about
    s = rep.section("plateforme")
    s.update({
        "full_name": about.fullName,
        "version": about.version,
        "build": about.build,
        "api_version": about.apiVersion,
        "api_type": about.apiType,               # HostAgent | VirtualCenter
        "product_line": about.productLineId,
        "os_type": about.osType,
        "instance_uuid": getattr(about, "instanceUuid", None),
    })
    major = int(about.version.split(".")[0]) if about.version else 0
    s["vddk_803_compatible"] = major <= 8
    s["verdict"] = ("VDDK 8.0.3 attendu compatible" if major <= 8 else
                    f"vSphere {about.version} : le VDDK 8.0.3 (mai 2024) ne "
                    f"couvre probablement pas cette version — décision D3")
    print(f"  {about.fullName}")
    print(f"  API {about.apiVersion} ({about.apiType}), build {about.build}")
    print(f"  → {s['verdict']}")


def probe_license(client: VSphereClient, rep: Report) -> None:
    """
    Une licence ESXi gratuite rend l'API vSphere en LECTURE SEULE : ni
    snapshot par API, ni accès VDDK. C'est la première cause d'échec d'un
    lab monté à la va-vite, et elle ne se voit pas avant le premier appel
    mutant. On la détecte ici, sans rien modifier.
    """
    s = rep.section("licence")
    try:
        lm = client.content.licenseManager
        entries = []
        for lic in (lm.licenses or []):
            feats = {f.key for f in (lic.properties or [])
                     if f.key in ("feature",)} if lic.properties else set()
            entries.append({"name": lic.name, "edition": lic.editionKey,
                            "used": lic.used, "total": lic.total})
        s["licenses"] = entries
        s["read_only_suspected"] = any(
            "free" in (e.get("edition") or "").lower()
            or "free" in (e.get("name") or "").lower() for e in entries)
        for e in entries:
            print(f"  licence : {e['name']} ({e['edition']})")
        if s["read_only_suspected"]:
            print("  ATTENTION licence gratuite détectée : l'API vSphere est "
                  "alors en lecture seule.")
            print("            Ni snapshot par API ni lecture VDDK ne "
                  "fonctionneront. Il faut une licence d'évaluation (60 j).")
    except vim.fault.NoPermission as exc:
        s["error"] = f"pas de droit sur le gestionnaire de licences : {exc.msg}"
    except Exception as exc:                       # noqa: BLE001
        rep.fail("licence", exc)


def probe_network(host: str, port: int, rep: Report) -> None:
    """
    Le transport NBD/NBDSSL du VDDK n'utilise PAS le port de l'API : il ouvre
    une session NFC sur le port 902 de l'ESXi. Un 443 joignable ne garantit
    donc rien, et un 902 filtré se manifeste par un VIX_E_FAIL générique à
    l'ouverture du disque, sans indication de cause.
    """
    import socket
    s = rep.section("reseau")
    for label, p in (("api", port), ("nfc", 902)):
        t0 = time.time()
        try:
            with socket.create_connection((host, p), timeout=6):
                s[f"port_{p}"] = {"reachable": True,
                                  "seconds": round(time.time() - t0, 2)}
                print(f"  port {p} ({label}) joignable")
        except OSError as exc:
            s[f"port_{p}"] = {"reachable": False, "error": str(exc)}
            print(f"  port {p} ({label}) INJOIGNABLE — {exc}")
            if p == 902:
                print("        NBD et NBDSSL passent par ce port : leur "
                      "ouverture de disque échouera.")


def probe_thumbprints(host: str, port: int, rep: Report) -> dict:
    s = rep.section("certificat")
    out = {}
    for algo in ("sha1", "sha256"):
        try:
            out[algo] = fetch_thumbprint(host, port, algo)
        except Exception as exc:                   # noqa: BLE001
            out[algo] = f"erreur : {exc}"
    s.update(out)
    print(f"  empreinte SHA-1   : {out.get('sha1')}")
    print(f"  empreinte SHA-256 : {str(out.get('sha256'))[:47]}…")
    return out


def probe_inventory(client: VSphereClient, rep: Report) -> list[dict]:
    s = rep.section("inventaire")
    tree = client.inventory()
    s["datacenters"] = tree
    vms = [vm for dc in tree for vm in dc["vms"]]
    s["vm_count"] = len(vms)
    s["cbt_enabled_count"] = sum(1 for v in vms if v["cbt"])
    for dc in tree:
        print(f"  datacenter {dc['name']} — {len(dc['vms'])} VM, "
              f"{len(dc['clusters'])} cluster(s)")
    for vm in sorted(vms, key=lambda v: v["name"])[:40]:
        print(f"    {vm['name']:<34} {vm['power_state']:<11} "
              f"{'CBT' if vm['cbt'] else '---'}  {vm['num_disks']} disque(s)  "
              f"{vm['guest_id']}")
    if len(vms) > 40:
        print(f"    … et {len(vms) - 40} autres")
    return vms


def probe_preflight(client: VSphereClient, vms: list[dict], rep: Report) -> None:
    s = rep.section("prevols")
    for entry in vms:
        try:
            vm = client.find_vm(instance_uuid=entry["instance_uuid"])
            report = run_preflight(vm)
            s[entry["name"]] = {
                "ok": report.ok,
                "findings": [{"severity": f.severity.value, "code": f.code,
                              "message": f.message, "subject": f.subject}
                             for f in report.findings],
            }
            mark = "migrable" if report.ok else \
                   f"BLOQUÉ ({', '.join(f.code for f in report.blockers)})"
            print(f"  {entry['name']:<34} {mark}")
        except Exception as exc:                   # noqa: BLE001
            s[entry["name"]] = {"error": str(exc)}


# --------------------------------------------------------------------------
# Phase 2 — cycle de mesure sur une VM
# --------------------------------------------------------------------------
def measure_vm(client: VSphereClient, vm_name: str, thumbs: dict,
               args, rep: Report) -> None:
    s = rep.section("mesure")
    s["vm"] = vm_name
    vm = client.find_vm(name=vm_name)

    report = run_preflight(vm)
    s["preflight_ok"] = report.ok
    print(report.render())
    if not report.ok:
        s["abandon"] = "pré-vérification bloquante"
        return

    s["identity"] = {"instance_uuid": vm.instance_uuid, "moref": vm.moref,
                     "firmware": vm.firmware, "guest_id": vm.guest_id,
                     "power_state": vm.power_state}
    try:
        s["identity"]["datacenter_path"] = vm.datacenter_path
        s["identity"]["vmx_spec"] = vm.vmx_spec
        print(f"\n  vmxSpec : {vm.vmx_spec}")
    except Exception as exc:                       # noqa: BLE001
        s["identity"]["vmx_spec_error"] = str(exc)
        print(f"\n  ECHEC construction du vmxSpec : {exc}")
        return

    # --- CBT ---------------------------------------------------------
    try:
        s["cbt_was_enabled"] = vm.cbt_enabled
        t0 = time.time()
        activated = vm.enable_cbt()
        s["cbt_enable_seconds"] = round(time.time() - t0, 1)
        s["cbt_activated_now"] = activated
        print(f"  CBT : {'activé à l’instant' if activated else 'déjà actif'}"
              f" ({s['cbt_enable_seconds']} s)")
    except vim.fault.RestrictedVersion as exc:
        s["error"] = ("RestrictedVersion : l'API vSphere de cet hôte est en "
                      "LECTURE SEULE (licence ESXi gratuite). Snapshots et "
                      "VDDK sont impossibles. Installer une licence "
                      "d'évaluation 60 jours.")
        print(f"\n  BLOCAGE : {s['error']}")
        return
    except Exception as exc:                       # noqa: BLE001
        rep.fail("mesure", exc)
        return

    snapshot = None
    conn = None
    try:
        t0 = time.time()
        snapshot = vm.create_snapshot(f"{SNAPSHOT_PREFIX}-lab-report")
        s["snapshot_seconds"] = round(time.time() - t0, 1)
        print(f"  snapshot créé en {s['snapshot_seconds']} s")

        disks = vm.disks(snapshot=snapshot)
        s["disks"] = [{
            "label": d.label, "device_key": d.device_key,
            "unit": d.unit_number, "controller": d.controller_key,
            "file_name": d.file_name, "capacity_bytes": d.capacity_bytes,
            "disk_mode": d.disk_mode, "thin": d.thin,
            "change_id": d.change_id,
            "change_id_format": _describe_change_id(d.change_id),
        } for d in disks]
        for d in s["disks"]:
            print(f"  {d['label']:<14} {d['capacity_bytes'] / 2**30:6.1f} Gio  "
                  f"changeId={d['change_id']!r}")

        # --- QueryChangedDiskAreas : combien d'appels réellement ? ----
        s["cbt_query"] = {}
        for d in disks:
            calls = []

            def counting(offset, _d=d, _calls=calls):
                res = vm.raw.QueryChangedDiskAreas(
                    snapshot=snapshot, deviceKey=_d.device_key,
                    startOffset=offset, changeId=FULL)
                _calls.append({"start_offset": offset,
                               "returned_start": int(res.startOffset),
                               "returned_length": int(res.length),
                               "areas": len(res.changedArea or [])})
                return res
            try:
                t0 = time.time()
                areas = ex.query_changed_areas_complete(
                    counting, d.capacity_bytes, FULL)
                s["cbt_query"][d.label] = {
                    "calls": calls,
                    "call_count": len(calls),
                    "area_count": len(areas),
                    "allocated_bytes": sum(a.length for a in areas),
                    "allocated_pct": round(
                        100 * sum(a.length for a in areas) / d.capacity_bytes, 2),
                    "seconds": round(time.time() - t0, 2),
                }
                r = s["cbt_query"][d.label]
                print(f"    {d.label} : {r['call_count']} appel(s), "
                      f"{r['area_count']} zone(s), "
                      f"{r['allocated_bytes'] / 2**30:.1f} Gio alloués "
                      f"({r['allocated_pct']} %)")
            except (CbtInvalidated, ValueError) as exc:
                s["cbt_query"][d.label] = {"error": str(exc), "calls": calls}
                print(f"    {d.label} : ECHEC — {exc}")

        # --- VDDK : quelle empreinte, quel vmxSpec, quel débit ---------
        s["vddk"] = {}
        distrib = os.environ.get("VDDK_DISTRIB")
        if not distrib:
            s["vddk"]["skipped"] = "VDDK_DISTRIB non défini"
            print("  VDDK ignoré (VDDK_DISTRIB non défini)")
        else:
            lib = vddk.init(distrib)
            s["vddk"]["available_transports"] = lib.transport_modes
            print(f"  transports disponibles : {':'.join(lib.transport_modes)}")

            # journal §5 ter : le rejet VDDK observé (« Invalid search
            # specifier ») porte sur le vmxSpec, pas sur le diskPath. On
            # essaie donc plusieurs formes de vmxSpec, une connexion par
            # forme, jusqu'à ce qu'une permette d'ouvrir au moins un disque.
            s["vmx_spec_trials"] = {}
            for spec_label, vmx_spec in _vmx_spec_candidates(vm):
                print(f"  connexion VDDK, vmxSpec « {spec_label} » : {vmx_spec}")
                trial: dict = {"vmx_spec": vmx_spec}
                accepted, conn = _try_thumbprints(
                    vm, thumbs, snapshot, args, trial, vmx_spec)
                if conn is None:
                    trial["result"] = "aucune empreinte acceptée"
                    print("    ECHEC : aucune empreinte acceptée par le VDDK")
                    s["vmx_spec_trials"][spec_label] = trial
                    continue
                print(f"    empreinte acceptée : {accepted}")
                _measure_read(conn, disks, args, trial, vm=vm, snapshot=snapshot)
                try:
                    conn.close()
                except Exception:                  # noqa: BLE001
                    log.exception("fermeture de la connexion VDDK")
                conn = None
                any_ok = any("error" not in r for r in trial["read"].values())
                trial["au_moins_un_disque_ouvert"] = any_ok
                s["vmx_spec_trials"][spec_label] = trial
                if any_ok:
                    s["vmx_spec_accepted"] = spec_label
                    s["thumbprint_accepted"] = accepted
                    s["read"] = trial["read"]
                    s["open_attempts"] = trial.get("open_attempts", {})
                    break
            else:
                # aucune forme de vmxSpec n'a permis d'ouvrir un disque :
                # on garde la dernière tentative en évidence dans le rapport.
                if s["vmx_spec_trials"]:
                    last = list(s["vmx_spec_trials"].values())[-1]
                    s["read"] = last.get("read", {})
                    s["open_attempts"] = last.get("open_attempts", {})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                      # noqa: BLE001
                log.exception("fermeture de la connexion VDDK")
        if snapshot is not None:
            try:
                vm.remove_snapshot(snapshot)
                print("  snapshot de mesure supprimé")
                s["snapshot_removed"] = True
            except Exception as exc:               # noqa: BLE001
                s["snapshot_removed"] = False
                s["snapshot_removal_error"] = str(exc)
                print(f"  ATTENTION : snapshot NON supprimé — {exc}")
                print(f"  le supprimer à la main : v2fc_source.py cleanup "
                      f"--name {vm_name}")


def _describe_change_id(change_id: str | None) -> str:
    if not change_id:
        return "absent"
    if "/" in change_id:
        uid, seq = change_id.rsplit("/", 1)
        return f"uid={uid!r} sequence={seq!r}"
    return "format inattendu"


def _vmx_spec_candidates(vm):
    """
    Formes de vmxSpec à essayer pour VixDiskLib_ConnectEx (journal §5 ter) :
    la forme usuelle « <vmx>?dcPath=...&dsName=... » que construit
    `SourceVm.vmx_spec`, puis l'alternative « moref=<moRef> » documentée par
    un fil Broadcom Community pour un cas d'échec proche — non confirmée
    officiellement, à trancher par l'essai, pas à supposer acquise.
    """
    return [
        ("vmx + dcPath/dsName", vm.vmx_spec),
        ("moref", f"moref={vm.moref}"),
    ]


def _try_thumbprints(vm, thumbs: dict, snapshot, args, s: dict, vmx_spec: str):
    """
    Le VDDK accepte-t-il l'empreinte SHA-1, SHA-256, ou les deux, pour la
    forme de vmxSpec donnée par l'appelant ? Question laissée ouverte au
    §2.1 ; on la tranche par l'essai.
    """
    trials = {}
    host, user, pwd = args.host, args.user, args.password
    for algo in ("sha1", "sha256"):
        thumb = thumbs.get(algo)
        if not thumb or thumb.startswith("erreur"):
            continue
        try:
            conn = vddk.Connection.vsphere(
                host, user, pwd, thumb, vmx_spec,
                snapshot_ref=str(snapshot._moId),
                transport_modes=args.transport, port=args.port)
            trials[algo] = "accepté"
            s["thumbprint_trials"] = trials
            return algo, conn
        except Exception as exc:                   # noqa: BLE001
            trials[algo] = f"refusé : {exc}"
            log.warning("empreinte %s refusée (vmxSpec %r) : %s",
                       algo, vmx_spec, exc)
    s["thumbprint_trials"] = trials
    return None, None


def _open_strategies(vm, snapshot, disks_now, disk):
    """
    Combinaisons d'ouverture à essayer, de la plus orthodoxe à la plus
    permissive. VixDiskLib_Open renvoie un VIX_E_FAIL générique sans dire
    laquelle convient : on les essaie et on note celle qui passe.
    """
    current = next((x for x in disks_now if x.device_key == disk.device_key), None)
    out = [("disque du snapshot", disk.file_name)]
    if current and current.file_name != disk.file_name:
        out.append(("disque courant (delta)", current.file_name))
    if vm is not None:
        try:
            abs_path = vm.absolute_disk_path(disk.file_name)
        except Exception as exc:                     # noqa: BLE001
            log.warning("résolution du chemin VMFS absolu impossible pour %s : %s",
                        disk.file_name, exc)
        else:
            out.append(("chemin VMFS absolu (hypothèse §5)", abs_path))
    return out


def _measure_read(conn, disks, args, dest: dict, vm=None, snapshot=None) -> None:
    """`dest` reçoit `read` et `open_attempts` — un dict par essai de vmxSpec
    (journal §5 ter), pas nécessairement la section `mesure` du rapport."""
    s = dest
    s["read"] = {}
    sample = args.sample_mib * (1 << 20)
    disks_now = vm.disks() if vm is not None else []
    for d in disks:
        attempts = []
        opened = None
        for label, path in _open_strategies(vm, snapshot, disks_now, d):
            mark = vddk.journal_mark()
            try:
                opened = conn.open(path)
                attempts.append({"strategy": label, "path": path, "result": "ok"})
                print(f"    {d.label} : ouvert via {label} ({path})")
                break
            except Exception as exc:                   # noqa: BLE001
                tail = vddk.journal_since(mark, 12)
                attempts.append({"strategy": label, "path": path,
                                 "error": str(exc), "vddk_log": tail})
                print(f"    {d.label} : ECHEC {label} — {exc}")
                for line in tail[-6:]:
                    print(f"        {line[:150]}")
        s.setdefault("open_attempts", {})[d.label] = attempts
        if opened is None:
            s["read"][d.label] = {"error": "aucune stratégie d'ouverture n'a abouti",
                                  "attempts": attempts}
            continue
        try:
            with opened as src:
                mode = src.transport_mode
                info = src.info
                to_read = min(sample, src.capacity_bytes)
                t0 = time.time()
                got, pos = 0, 0
                while got < to_read:
                    n = min(4 << 20, to_read - got)
                    src.read(pos, n)
                    got += n
                    pos += n
                dt = max(time.time() - t0, 1e-6)
                s["read"][d.label] = {
                    "transport_negotiated": mode,
                    "bytes": got,
                    "seconds": round(dt, 2),
                    "mib_per_second": round(got / dt / 2**20, 1),
                    "logical_sector_size": info["logical_sector_size"],
                    "disk_uuid": info["uuid"],
                    "num_links": info["num_links"],
                }
                r = s["read"][d.label]
                print(f"    {d.label} : transport « {mode} », "
                      f"{r['mib_per_second']} Mio/s sur "
                      f"{got / 2**20:.0f} Mio")
        except Exception as exc:                   # noqa: BLE001
            s["read"][d.label] = {"error": str(exc),
                                  "vddk_log": vddk.journal_since(0, 12)}
            print(f"    {d.label} : ECHEC lecture — {exc}")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host"), ap.add_argument("--user")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--insecure", action="store_true",
                    help="certificat auto-signé (usuel sur un ESXi de lab)")
    ap.add_argument("--probe", action="store_true",
                    help="phase lecture seule uniquement")
    ap.add_argument("--vm", help="VM sur laquelle mesurer le cycle CBT + VDDK")
    ap.add_argument("--transport", default="nbdssl:nbd",
                    help="modes proposés au VDDK (hotadd exige un proxy "
                         "virtuel dans le cluster)")
    ap.add_argument("--sample-mib", type=int, default=256,
                    help="volume lu par disque pour la mesure de débit")
    ap.add_argument("-o", "--out", default="v2fc-lab-report.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s")

    args.host = args.host or os.environ.get("V2FC_VC_HOST")
    args.user = args.user or os.environ.get("V2FC_VC_USER")
    args.password = os.environ.get("V2FC_VC_PASSWORD")
    if not args.host or not args.user:
        sys.exit("V2FC_VC_HOST et V2FC_VC_USER requis")
    if not args.password:
        args.password = getpass.getpass(f"Mot de passe {args.user}@{args.host} : ")

    rep = Report()
    rep.data["target"] = {"host": args.host, "user": args.user,
                          "port": args.port, "verify_tls": not args.insecure}
    out = Path(args.out)

    print(f"\n=== Relevé de plateforme V2FC — {args.host} ===\n")
    try:
        print("1. Réseau")
        probe_network(args.host, args.port, rep)
        print("\n2. Certificat")
        thumbs = probe_thumbprints(args.host, args.port, rep)

        with VSphereClient(args.host, args.user, args.password, port=args.port,
                           verify=not args.insecure,
                           thumbprint=thumbs.get("sha1")) as client:
            print("\n3. Plateforme")
            probe_platform(client, rep)
            print("\n4. Licence")
            probe_license(client, rep)
            print("\n5. Inventaire")
            vms = probe_inventory(client, rep)
            print("\n6. Pré-vérifications")
            probe_preflight(client, vms, rep)

            if args.vm and not args.probe:
                print(f"\n7. Cycle de mesure sur « {args.vm} »")
                measure_vm(client, args.vm, thumbs, args, rep)
            elif not args.probe:
                print("\n6. Cycle de mesure ignoré (préciser --vm NOM)")
    except Exception as exc:                       # noqa: BLE001
        rep.fail("global", exc)
        print(f"\nECHEC : {type(exc).__name__} — {exc}")
    finally:
        try:
            from datamover.vddk import _Library
            if _Library._instance is not None:
                rep.section("journal_vddk")["lines"] = _Library._instance.journal[-250:]
        except Exception:                              # noqa: BLE001
            pass
        rep.save(out)
        print(f"\nRapport écrit : {out.resolve()}")
        print("Il ne contient ni mot de passe ni jeton — relisez-le avant envoi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
