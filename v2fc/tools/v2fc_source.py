#!/usr/bin/env python3
"""
v2fc_source — outil en ligne de commande du driver source vSphere.

Trois sous-commandes :

    inventory              arbre datacenters / clusters / VM
    preflight  <vm>        contrôles avant migration, sortie 1 si bloquant
    sync       <vm>        un cycle complet seed ou delta vers un volume cible

`sync` est l'intégration complète : vSphere pour le CBT et les snapshots,
VDDK pour la lecture, le planificateur pour les extents, l'agent relais pour
l'écriture. C'est ce qui achève le lot L1 sur une plateforme réelle.

Prérequis d'environnement (voir ARCHITECTURE.md §2.2) :
    export VDDK_DISTRIB=/chemin/vmware-vix-disklib-distrib
    export LD_LIBRARY_PATH="$VDDK_DISTRIB/lib64"

Identifiants : V2FC_VC_HOST, V2FC_VC_USER, V2FC_VC_PASSWORD
(le mot de passe n'est jamais accepté en argument de ligne de commande, il
apparaîtrait dans l'historique du shell et dans `ps`).

Exemples :
    v2fc_source.py inventory
    v2fc_source.py preflight --name SRV-APP-01
    v2fc_source.py sync --name SRV-APP-01 --round seed \\
        --target-dir /mnt/relay --state ./srv-app-01.json
    v2fc_source.py sync --name SRV-APP-01 --round delta \\
        --target-dir /mnt/relay --state ./srv-app-01.json
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from datamover import extents as ex
from datamover import vddk
from datamover.preflight import Severity, run_preflight
from datamover.vsphere import (SNAPSHOT_PREFIX, CbtInvalidated, VSphereClient,
                               fetch_thumbprint, vmx_spec_candidates)
from relay.writer import Mode, VolumeWriter, checksum

log = logging.getLogger("v2fc.cli")
FULL = "*"


# --------------------------------------------------------------------------
# État persisté — préfiguration de la table sync_rounds (ARCHITECTURE.md §8)
# --------------------------------------------------------------------------
class State:
    """
    Fichier JSON tenant lieu de base de données pour l'outil en ligne de
    commande. Porte l'invariant du §8.2 : le changeId n'est écrit qu'APRÈS
    confirmation que les octets sont sur disque.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {
            "instance_uuid": None, "round": 0, "disks": {}}

    def disk(self, key: int) -> dict:
        return self.data["disks"].setdefault(str(key), {
            "change_id": None, "capacity": None, "volume_uuid": None,
            "cursor": 0, "rounds": 0})

    def commit(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)          # remplacement atomique


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------
def credentials(args) -> tuple[str, str, str]:
    host = args.host or os.environ.get("V2FC_VC_HOST")
    user = args.user or os.environ.get("V2FC_VC_USER")
    pwd = os.environ.get("V2FC_VC_PASSWORD")
    if not host or not user:
        sys.exit("hôte et utilisateur requis (--host/--user ou V2FC_VC_HOST/"
                 "V2FC_VC_USER)")
    if not pwd:
        pwd = getpass.getpass(f"Mot de passe {user}@{host} : ")
    return host, user, pwd


def connect(args) -> VSphereClient:
    host, user, pwd = credentials(args)
    return VSphereClient(host, user, pwd, port=args.port, verify=not args.insecure)


def locate(client: VSphereClient, args):
    return client.find_vm(instance_uuid=args.uuid, moref=args.moref, name=args.name)


# --------------------------------------------------------------------------
# Sous-commandes
# --------------------------------------------------------------------------
def cmd_inventory(args) -> int:
    with connect(args) as client:
        for dc in client.inventory():
            print(f"\nDatacenter {dc['name']}  ({dc['moref']})")
            for c in dc["clusters"]:
                print(f"  cluster {c['name']:<28} {len(c['hosts'])} hôte(s)")
            print(f"  {len(dc['vms'])} VM")
            for vm in sorted(dc["vms"], key=lambda v: v["name"]):
                flag = "CBT" if vm["cbt"] else "---"
                print(f"    {vm['name']:<36} {vm['power_state']:<12} "
                      f"{flag}  {vm['num_disks']} disque(s)  {vm['guest_id']}")
    return 0


def cmd_preflight(args) -> int:
    with connect(args) as client:
        vm = locate(client, args)
        report = run_preflight(vm)
        print(report.render())
        if args.json:
            print(json.dumps([f.__dict__ for f in report.findings],
                             default=str, indent=2, ensure_ascii=False))
    return 0 if report.ok else 1


class _RoundProgress:
    """
    Avancement du round entier (tous disques), transmis à `callback(planned,
    done)` au plus toutes les `interval` secondes — barre de progression de
    la page Migration (job-run). Sans callback (CLI manuelle) : rien.
    """

    def __init__(self, planned: int, done: int, callback=None,
                interval: float = 5.0) -> None:
        self.planned, self.done = planned, done
        self.callback, self.interval = callback, interval
        self._last = 0.0
        self._emit()

    def update(self, n_bytes: int) -> None:
        self.done += n_bytes
        if time.time() - self._last >= self.interval or self.done >= self.planned:
            self._emit()

    def _emit(self) -> None:
        self._last = time.time()
        if self.callback is not None:
            self.callback(self.planned, self.done)


class _Progress:
    """
    Ligne de progression pendant le transfert d'un disque, réaffichée sur
    place (`\r`). Sans elle, un cycle de plusieurs minutes ne donne aucun
    signe de vie tant que le disque n'est pas terminé — ce qui a été
    signalé le 15/09/2026 lors du premier cycle réel.

    Rafraîchit au plus toutes les `interval` secondes pour ne pas ralentir
    le transfert par l'affichage lui-même.
    """

    def __init__(self, label: str, planned_bytes: int, chunk_count: int,
                interval: float = 2.0) -> None:
        self.label = label
        self.planned = max(planned_bytes, 1)
        self.chunk_count = max(chunk_count, 1)
        self.interval = interval
        self.done_bytes = 0
        self.done_chunks = 0
        self.t0 = time.time()
        self._last_print = 0.0

    def update(self, n_bytes: int) -> None:
        self.done_bytes += n_bytes
        self.done_chunks += 1
        now = time.time()
        if (now - self._last_print >= self.interval
                or self.done_chunks == self.chunk_count):
            self._render(now)
            self._last_print = now

    def _render(self, now: float) -> None:
        dt = max(now - self.t0, 1e-6)
        rate_mib_s = self.done_bytes / dt / 2**20
        pct = 100 * self.done_bytes / self.planned
        remaining = max(self.planned - self.done_bytes, 0)
        eta_s = remaining / (rate_mib_s * 2**20) if rate_mib_s > 0 else 0
        print(f"\r  {self.label} : {pct:5.1f} %  "
              f"{self.done_bytes / 2**20:,.0f}/{self.planned / 2**20:,.0f} Mio  "
              f"{rate_mib_s:6.1f} Mio/s  "
              f"tranche {self.done_chunks}/{self.chunk_count}  "
              f"ETA {eta_s:5.0f} s   ", end="", flush=True)

    def finish(self) -> None:
        self._render(time.time())
        print()   # passe à la ligne après la barre de progression


def cmd_sync(args, stats: dict | None = None) -> int:
    """
    `stats`, si fourni, est rempli en place avec {"written", "skipped",
    "duration"} juste avant le retour — ajouté le 17/09/2026 pour
    `cmd_job_run` (qui doit rapporter ces chiffres à l'API), sans rien
    changer au comportement existant quand l'argument est omis (défaut
    None, aucune capture) : la CLI manuelle n'est pas affectée.
    """
    distrib = os.environ.get("VDDK_DISTRIB")
    if not distrib:
        sys.exit("VDDK_DISTRIB non défini")
    target_dir = Path(args.target_dir)
    if not target_dir.is_dir():
        sys.exit(f"{target_dir} n'est pas un répertoire")
    # {device_key: (chemin du volume, identifiant de signature)} : volumes
    # FusionCompute attachés au relais (job-run). Absent = fichiers
    # disk-<key>.raw dans target_dir (CLI manuelle, POC).
    volumes = getattr(args, "volumes", None)

    state = State(Path(args.state))
    seed = args.round == "seed"
    # Mode.SEED saute les zones nulles en supposant un volume neuf. Faux pour
    # un re-seed (CBT invalidé) ou la reprise d'un seed multi-disque : le
    # volume contient déjà des données, les zéros doivent être écrits.
    mode = Mode.SEED if seed and state.data["round"] == 0 else Mode.DELTA

    vddk.init(distrib, require=args.require.split(",") if args.require else None)

    host, user, pwd = credentials(args)
    thumb = fetch_thumbprint(host, args.port)
    log.info("empreinte %s : %s", host, thumb)

    with VSphereClient(host, user, pwd, port=args.port,
                       verify=not args.insecure, thumbprint=thumb) as client:
        vm = locate(client, args)

        report = run_preflight(vm)
        if not report.ok:
            print(report.render())
            sys.exit("pré-vérification bloquante — cycle refusé")
        for w in report.warnings:
            log.warning("%s", w)

        if state.data["instance_uuid"] and \
                state.data["instance_uuid"] != vm.instance_uuid:
            sys.exit(f"l'état {args.state} appartient à une autre VM "
                     f"({state.data['instance_uuid']})")
        state.data["instance_uuid"] = vm.instance_uuid

        vm.enable_cbt()

        # Un round interrompu peut avoir laissé son snapshot : il ferait
        # grossir le delta VMDK de la source jusqu'à saturer le datastore.
        if vm.cleanup_migration_snapshots():
            log.warning("snapshot(s) de migration oublié(s) supprimé(s) sur %s",
                        vm.name)

        round_no = state.data["round"] + 1
        snap_name = f"{SNAPSHOT_PREFIX}-r{round_no:04d}"
        snapshot = vm.create_snapshot(snap_name, quiesce=args.quiesce)
        started = time.time()
        totals = {"planned": 0, "written": 0, "skipped": 0}

        try:
            disks = vm.disks(snapshot=snapshot)

            def _probe_open(c, _path=disks[0].file_name):
                # ConnectEx réussit pour n'importe quelle forme de vmxSpec ;
                # seul VixDiskLib_Open révèle si elle convient vraiment
                # (journal §5 quater). On le vérifie ici plutôt que de
                # découvrir le rejet au milieu du cycle d'écriture.
                with c.open(_path):
                    pass

            spec_label, conn = vddk.Connection.vsphere_auto(
                host, user, pwd, thumb, vmx_spec_candidates(vm),
                snapshot_ref=str(snapshot._moId),
                transport_modes=args.transport, port=args.port,
                probe=_probe_open)
            log.info("connexion VDDK établie avec vmxSpec « %s »", spec_label)
            if "hotadd" in args.transport.split(":"):
                # PrepareForAccess/EndAccess ne concernent que le mode HotAdd
                # (désactivation temporaire de vMotion/DRS pendant que les
                # disques sont attachés au proxy, ARCHITECTURE.md §6.4).
                # Les appeler hors HotAdd échoue en connexion directe à un
                # hôte (« not supported on remote objects », VixError=0x14,
                # constaté au premier cycle réel du 15/09/2026) et n'a de
                # toute façon aucun sens pour NBD/NBDSSL/SAN.
                conn.prepare_access("v2fc")
            try:
                plans = []
                for disk in disks:
                    st = state.disk(disk.device_key)
                    # Reprise d'un seed : un disque déjà copié en entier n'a
                    # besoin que des changements depuis sa copie.
                    full = seed and not (st["change_id"] and st["rounds"])
                    if st["capacity"] and not full:
                        vm.validate_cbt_continuity(disk, st["capacity"])
                    if seed and not full:
                        print(f"{disk.label} : copie initiale déjà faite, "
                              f"rattrapage des changements seulement")
                    base = FULL if full else st["change_id"]
                    if base is None:
                        sys.exit(f"{disk.label} : aucun changeId de référence, "
                                 f"lancer d'abord un cycle seed")

                    changed = vm.query_changed_areas(snapshot, disk, base)
                    plan = ex.build_plan(changed, disk.capacity_bytes,
                                         args.chunk * (1 << 20))
                    planned = ex.total_bytes(plan)
                    totals["planned"] += planned
                    print(f"\n{disk.label}  {disk.capacity_bytes / 2**30:.1f} Gio")
                    print(f"  {len(changed)} zone(s) -> {len(plan)} tranche(s), "
                          f"{planned / 2**20:,.0f} Mio "
                          f"({100 * planned / disk.capacity_bytes:.2f} %)")
                    plans.append((disk, st, plan))

                # Tous les plans sont calculés avant la copie : le total du
                # round est connu dès le départ pour la barre de progression.
                remaining = sum(ex.total_bytes(ex.resume_from(p, s["cursor"] or 0))
                                for _, s, p in plans)
                round_progress = _RoundProgress(totals["planned"],
                                                totals["planned"] - remaining,
                                                getattr(args, "progress_cb", None))
                for disk, st, plan in plans:
                    if volumes:
                        if disk.device_key not in volumes:
                            sys.exit(f"{disk.label} : aucun volume cible provisionné "
                                     f"(disque {disk.device_key})")
                        volume_path, signature_id = volumes[disk.device_key]
                        volume = Path(volume_path)
                    else:
                        volume = target_dir / f"disk-{disk.device_key}.raw"
                        signature_id = volume.name
                    if not volume.exists():
                        sys.exit(f"volume cible absent : {volume}. Le provisionner "
                                 f"et y déposer la signature avant le cycle.")
                    uuid = st["volume_uuid"] or signature_id

                    with conn.open(disk.file_name) as src, \
                            VolumeWriter(str(volume), disk.capacity_bytes, mode,
                                         uuid) as dst:
                        log.info("transport négocié : %s", src.transport_mode)
                        cursor = st["cursor"] if st["cursor"] else 0
                        todo = ex.resume_from(plan, cursor)
                        if cursor:
                            print(f"  reprise au curseur {cursor}")
                        progress = _Progress(disk.label, ex.total_bytes(todo), len(todo))
                        for chunk in todo:
                            data = src.read(chunk.offset, chunk.length)
                            dst.write_chunk(chunk.offset, data,
                                            expected_crc=checksum(data))
                            st["cursor"] = chunk.idx + 1
                            progress.update(chunk.length)
                            round_progress.update(chunk.length)
                            client.keepalive()
                        progress.finish()
                        dst.flush()          # AVANT d'écrire le changeId (§8.2)
                        totals["written"] += dst.stats.bytes_written
                        totals["skipped"] += dst.stats.bytes_skipped

                    # le backing du snapshot porte le changeId du point figé :
                    # c'est LUI la référence du cycle suivant
                    st["change_id"] = disk.change_id
                    st["capacity"] = disk.capacity_bytes
                    st["volume_uuid"] = uuid
                    st["cursor"] = 0
                    st["rounds"] += 1
                    state.data["round"] = round_no
                    state.commit()
                    print(f"  changeId enregistré : {disk.change_id}")
            finally:
                conn.close()
        except CbtInvalidated as exc:
            state.data["disks"] = {}         # force un re-seed complet
            state.commit()
            print(f"\nCBT INVALIDÉ : {exc}")
            print("L'état a été réinitialisé : relancer avec --round seed.")
            return 2
        finally:
            vm.remove_snapshot(snapshot)

    dt = time.time() - started
    print(f"\ncycle {round_no} ({args.round}) terminé en {dt:.0f} s — "
          f"{totals['written'] / 2**20:,.0f} Mio écrits, "
          f"{totals['skipped'] / 2**20:,.0f} Mio de zéros ignorés, "
          f"{totals['written'] / max(dt, 0.1) / 2**20:.0f} Mio/s")
    if stats is not None:
        stats.update(written=totals["written"], skipped=totals["skipped"], duration=dt)
    return 0


def cmd_provision(args) -> int:
    """Crée les volumes cibles du banc d'essai et y dépose la signature."""
    with connect(args) as client:
        vm = locate(client, args)
        target_dir = Path(args.target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for disk in vm.disks():
            volume = target_dir / f"disk-{disk.device_key}.raw"
            size = VolumeWriter.required_volume_size(disk.capacity_bytes)
            with open(volume, "wb") as fh:
                fh.truncate(size)
            VolumeWriter.stamp_signature(str(volume), volume.name)
            print(f"{volume}  {size / 2**30:.1f} Gio  (source "
                  f"{disk.capacity_bytes / 2**30:.1f} Gio + réservoir)")
    return 0


def cmd_cleanup(args) -> int:
    with connect(args) as client:
        vm = locate(client, args)
        n = vm.cleanup_migration_snapshots()
        print(f"{n} snapshot(s) de migration supprimé(s) sur {vm.name}")
    return 0


# --------------------------------------------------------------------------
# job-run — exécute un round pour un job piloté par l'API (ajouté le
# 17/09/2026, socle L2, docs/dev/JOURNAL.md §5 sexies). Ne réimplémente rien : c'est
# un appelant supplémentaire de `cmd_sync`, exactement comme la CLI
# manuelle, avec deux différences seulement :
#   1. le contexte (VM, round à faire) vient de l'API plutôt que de
#      --uuid/--round tapés à la main ;
#   2. le résultat est rapporté à l'API (état du job, journal) en plus
#      d'être affiché dans ce terminal.
# Les identifiants source arrivent par V2FC_VC_USER/V2FC_VC_PASSWORD : posés
# par le runner (seul à les recevoir de l'API, §13) ou par l'opérateur.
# --------------------------------------------------------------------------
class _ApiLogHandler(logging.Handler):
    """
    Relaie chaque ligne de log du logger « v2fc » (et ses enfants :
    v2fc.cli, v2fc.vsphere, v2fc.vddk, v2fc.relay...) vers
    `POST /jobs/{id}/logs`, pour l'onglet Logs. Best-effort : un souci
    réseau ici ne doit jamais interrompre le round de migration en cours.
    """

    def __init__(self, api_url: str, job_id: str, headers: dict) -> None:
        super().__init__()
        self.api_url = api_url
        self.job_id = job_id
        self.headers = headers

    def emit(self, record: logging.LogRecord) -> None:
        level = {"DEBUG": "debug", "WARNING": "warning",
                "ERROR": "error", "CRITICAL": "error"}.get(record.levelname, "info")
        try:
            requests.post(
                f"{self.api_url}/api/v1/jobs/{self.job_id}/logs",
                json={"entries": [{"level": level, "message": self.format(record)}]},
                headers=self.headers, timeout=5)
        except requests.RequestException:
            pass


def _report_progress(api_url: str, job_id: str, round_id: str, headers: dict,
                     planned: int, done: int) -> None:
    """Best-effort, comme les logs : ne jamais interrompre le round pour ça."""
    try:
        requests.post(f"{api_url}/api/v1/jobs/{job_id}/rounds/{round_id}/progress",
                      json={"bytes_planned": planned, "bytes_done": done},
                      headers=headers, timeout=5)
    except requests.RequestException:
        pass


def _prepare_relay_volumes(volumes: dict, wait_s: float = 60.0) -> None:
    """
    Avant un seed : attend que chaque volume attaché au relais apparaisse
    (udev), puis y dépose la signature (§7.2) si elle manque. Le chemin
    vient de l'UUID du volume (fusioncompute.guest_disk_path) : c'est bien
    le volume créé pour ce disque, pas un disque du relais lui-même.
    """
    for path, signature_id in volumes.values():
        deadline = time.monotonic() + wait_s
        while not os.path.exists(path):
            if time.monotonic() > deadline:
                sys.exit(f"volume {signature_id} introuvable sur le relais ({path}) : "
                         f"est-il bien attaché à cette VM ?")
            time.sleep(2)
        expected = VolumeWriter._signature_bytes(signature_id)
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(fh.tell() - len(expected))
            present = fh.read(len(expected)) == expected
        if not present:
            VolumeWriter.stamp_signature(path, signature_id)
            log.info("signature déposée sur %s (volume %s)", path, signature_id)


def cmd_job_run(args) -> int:
    token = os.environ.get("V2FC_API_TOKEN")
    if not token:
        sys.exit("V2FC_API_TOKEN non défini — se connecter via POST /api/v1/auth/login "
                 "et exporter le jeton reçu (jamais en argument de ligne de commande, "
                 "même discipline que V2FC_VC_PASSWORD)")
    api_url = args.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(f"{api_url}/api/v1/jobs/{args.job_id}/run-context",
                        params={"round": args.round}, headers=headers, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"contexte de job refusé par l'API ({resp.status_code}) : {resp.text}")
    ctx = resp.json()

    handler = _ApiLogHandler(api_url, args.job_id, headers)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("v2fc").addHandler(handler)

    target_dir = Path(args.target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    state_path = target_dir / "state.json"

    volumes = {d["device_key"]: (d["tgt_dev_path"], d["volume_id"])
               for d in ctx.get("disks", []) if d.get("tgt_dev_path")}

    sync_args = argparse.Namespace(
        host=ctx["src_host"], user=args.user or os.environ.get("V2FC_VC_USER"),
        port=ctx["src_port"], insecure=not ctx["src_verify_tls"],
        uuid=ctx["vm_instance_uuid"], moref=None, name=None,
        round="seed" if ctx["round_kind"] == "SEED" else "delta",
        target_dir=str(target_dir), state=str(state_path),
        chunk=args.chunk, transport=args.transport, require=args.require,
        quiesce=args.quiesce or ctx["round_kind"] == "FINAL",
        volumes=volumes or None,
        progress_cb=lambda planned, done: _report_progress(
            api_url, args.job_id, ctx["round_id"], headers, planned, done))

    stats: dict = {}
    status_, error_msg, rc = "failed", None, 1
    try:
        if volumes and ctx["round_kind"] == "SEED":
            _prepare_relay_volumes(volumes)
        rc = cmd_sync(sync_args, stats=stats)
        status_ = {0: "success", 2: "cbt_invalidated"}.get(rc, "failed")
    except SystemExit as exc:
        error_msg = str(exc.code)
    except Exception as exc:                            # noqa: BLE001
        error_msg = f"{type(exc).__name__}: {exc}"
    finally:
        logging.getLogger("v2fc").removeHandler(handler)

    # Le fichier d'état (déjà écrit par cmd_sync/State.commit()) porte le
    # changeId à jour par disque — pas de répartition fiable des octets
    # par disque au niveau de ce script (`stats` est un total tous disques
    # confondus), donc `bytes_transferred` reste volontairement absent par
    # disque plutôt que d'inventer une répartition.
    disks_report = []
    if state_path.exists():
        state_data = json.loads(state_path.read_text())
        for device_key, d in state_data.get("disks", {}).items():
            disks_report.append({"device_key": int(device_key),
                                 "next_change_id": d.get("change_id"),
                                 "capacity_bytes": d.get("capacity")})

    report = {"status": status_, "duration_s": stats.get("duration"),
             "bytes_written": stats.get("written"), "bytes_skipped": stats.get("skipped"),
             "error": error_msg, "disks": disks_report}
    patch_resp = requests.patch(
        f"{api_url}/api/v1/jobs/{args.job_id}/rounds/{ctx['round_id']}",
        json=report, headers=headers, timeout=30)
    if patch_resp.status_code != 200:
        print(f"AVERTISSEMENT : rapport de round refusé par l'API "
              f"({patch_resp.status_code}) — le round a bien tourné localement, "
              f"seule la mise à jour du job a échoué : {patch_resp.text}", file=sys.stderr)
        return rc or 3

    return rc


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--host"), ap.add_argument("--user")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--insecure", action="store_true",
                    help="ne pas vérifier le certificat (l'empreinte reste épinglée)")

    sub = ap.add_subparsers(dest="cmd", required=True)

    def target(p):
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--uuid", help="instanceUuid — identité stable, à préférer")
        g.add_argument("--moref")
        g.add_argument("--name")

    sub.add_parser("inventory").set_defaults(func=cmd_inventory)

    p = sub.add_parser("preflight"); target(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("provision"); target(p)
    p.add_argument("--target-dir", required=True)
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("sync"); target(p)
    p.add_argument("--round", choices=["seed", "delta"], required=True)
    p.add_argument("--target-dir", required=True)
    p.add_argument("--state", required=True, help="fichier d'état JSON")
    p.add_argument("--chunk", type=int, default=4, help="taille de tranche en Mio")
    p.add_argument("--transport", default="hotadd:nbdssl:nbd")
    p.add_argument("--require", default="", help="modes exigés, séparés par des virgules")
    p.add_argument("--quiesce", action="store_true",
                   help="quiescence par VMware Tools (recommandé pour le delta final)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("cleanup"); target(p)
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("job-run",
                       help="exécute un round pour un job piloté par l'API "
                            "(onglets Migration/Logs, socle L2)")
    p.add_argument("--api-url", default=os.environ.get("V2FC_API_URL", "http://localhost:8000"))
    p.add_argument("--job-id", required=True)
    p.add_argument("--round", choices=["seed", "delta", "final"], required=True)
    p.add_argument("--target-dir", required=True)
    p.add_argument("--chunk", type=int, default=4, help="taille de tranche en Mio")
    p.add_argument("--transport", default="hotadd:nbdssl:nbd")
    p.add_argument("--require", default="", help="modes exigés, séparés par des virgules")
    p.add_argument("--quiesce", action="store_true")
    p.set_defaults(func=cmd_job_run)

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
