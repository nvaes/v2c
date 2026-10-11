"""
Jobs de migration — machine à états (ARCHITECTURE.md §5) et exécution.
Ajouté le 17/09/2026 à la demande explicite de Nicolas (onglets Migration
et Logs), une fois L1 validé deux fois sur plateforme réelle (HANDOFF.md
§5 sexies) : ESXi direct (15/09) et vCenter réel (17/09).

Portée assumée de cette première version, documentée pour ne pas laisser
croire à plus que ce qui est réellement câblé (voir HANDOFF.md pour le
détail) :

- Le round de synchronisation lui-même (lecture VDDK + écriture) tourne
  toujours dans le Data Mover (conteneur Docker séparé, VDDK x86-64 —
  l'API ne le charge pas, cf. api/Dockerfile) : `tools/v2fc_source.py
  job-run` l'exécute et rapporte le résultat à cette API par callback HTTP
  authentifié (même jeton JWT que l'UI web). Les identifiants source
  restent fournis par Nicolas lui-même via V2FC_VC_PASSWORD, jamais par
  cette API (§13 : jamais de secret en sortie) — exactement comme
  aujourd'hui en CLI manuelle.
- Le provisioning FusionCompute (`POST /jobs/{id}/provision`) appelle pour
  de vrai `datamover.fusioncompute` (§7.5 ②) si un relais est assigné au
  job. Sans relais assigné (aucun n'existe encore chez Nicolas au
  17/09/2026), le provisioning passe directement en SEEDING sans créer de
  volume réel — le round écrit alors dans le `--target-dir` local que
  Nicolas fournit à `job-run`, exactement comme le POC L1 validé ce jour.
  Voir `api/routers/relays.py` pour enregistrer un relais une fois déployé.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .. import vsphere_bridge
from ..db import get_db
from ..models import (Credential, DiskRound, Endpoint, Job, JobDisk, JobLogEntry,
                      Plan, PlanVm, Relay, SyncRound, User)
from ..schemas import (JobCreate, JobCutoverRequest, JobLogsIn, JobLogsOut,
                       JobOut, JobRoundReport, JobRoundStart, JobRunContext)
from ..security import decrypt_credential_password, require_role
from datamover import fusioncompute as fc
from datamover.vsphere import VSphereClient, VSphereError
from relay.writer import VolumeWriter

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

# ---------------------------------------------------------------------------
# Machine à états (ARCHITECTURE.md §5) — quels états acceptent quelle action.
# ---------------------------------------------------------------------------
_PROVISIONABLE = {"CREATED", "VALIDATING"}
_SEED_STATES = {"SEEDING"}
_DELTA_STATES = {"SYNCED"}
_FINAL_STATES = {"FINAL_DELTA"}
_CUTOVER_FROM = {"SYNCED"}
_ROLLBACK_FROM = {"CUTOVER_PENDING", "QUIESCING", "FINAL_DELTA", "FINALIZING", "CUTOVER_DONE"}


async def _load_job_full(db: AsyncSession, job_id: uuid.UUID) -> dict:
    """Renvoie le job avec ses disques et rounds pour JobOut (pas de relationship ORM déclarée sur Job -> disks/rounds dans models.py : jointure manuelle)."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    disks = (await db.execute(select(JobDisk).where(JobDisk.job_id == job_id))).scalars().all()
    rounds = (await db.execute(
        select(SyncRound).where(SyncRound.job_id == job_id).order_by(SyncRound.round_no))
    ).scalars().all()
    out = {c.name: getattr(job, c.name) for c in Job.__table__.columns}
    out["disks"] = disks
    out["rounds"] = rounds
    return out


async def _log(db: AsyncSession, job_id: uuid.UUID, message: str, level: str = "info") -> None:
    db.add(JobLogEntry(job_id=job_id, level=level, message=message))


async def _connection_args(db: AsyncSession, endpoint: Endpoint) -> dict:
    cred = await db.get(Credential, endpoint.credential_id)
    if cred is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="identifiants de l'endpoint introuvables")
    return {"host": endpoint.url, "user": cred.username,
           "password": decrypt_credential_password(cred),
           "port": endpoint.port, "verify_tls": endpoint.verify_tls}


def _set_state(job: Job, new_state: str) -> None:
    job.state = new_state
    # jobs.state_since est un TIMESTAMP WITHOUT TIME ZONE (Job.state_since
    # ne déclare pas timezone=True) — même piège déjà rencontré et corrigé
    # sur endpoints.last_probe_at (api/routers/endpoints.py) : asyncpg
    # refuse un datetime tz-aware sur une colonne naive ("can't subtract
    # offset-naive and offset-aware datetimes"), 500 sur toute transition
    # d'état (confirmé le 17/09/2026 par Nicolas sur POST .../provision).
    job.state_since = datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreate, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_role("operator"))) -> dict:
    plan_vm = await db.get(PlanVm, body.plan_vm_id)
    if plan_vm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="VM de plan introuvable")
    plan = await db.get(Plan, plan_vm.plan_id)
    if plan.target_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="le plan n'a pas encore de cible FusionCompute (PUT /plans/{id}/target)")
    if not plan_vm.tgt_datastore_urn or not plan_vm.tgt_network_urn:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="cette VM du plan n'a pas encore de datastore/réseau cible "
                                   "(PUT /plans/{id}/vms/{vm_id}/mapping)")
    if body.relay_id is not None and await db.get(Relay, body.relay_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relais introuvable")

    job = Job(plan_id=plan.id, source_id=plan.source_id, target_id=plan.target_id,
             relay_id=body.relay_id, src_vm_moref=plan_vm.src_vm_moref,
             src_vm_name=plan_vm.src_vm_name, src_instance_uuid=plan_vm.src_instance_uuid,
             state="CREATED", created_by=user.id)
    db.add(job)
    await db.flush()
    await _log(db, job.id, f"job créé pour {job.src_vm_name} depuis le plan « {plan.name} »")
    await db.commit()
    return await _load_job_full(db, job.id)


@router.get("", response_model=list[JobOut])
async def list_jobs(db: AsyncSession = Depends(get_db),
                    _user: User = Depends(require_role("viewer"))) -> list[dict]:
    result = await db.execute(select(Job.id).order_by(Job.created_at.desc()))
    return [await _load_job_full(db, jid) for (jid,) in result.all()]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                  _user: User = Depends(require_role("viewer"))) -> dict:
    return await _load_job_full(db, job_id)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                     _user: User = Depends(require_role("operator"))) -> None:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if job.state not in ("CREATED", "VALIDATING", "FAILED", "ROLLED_BACK"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"job en état {job.state} — l'arrêter avant de le supprimer")
    await db.delete(job)
    await db.commit()


# ---------------------------------------------------------------------------
# Provisioning (CREATED/VALIDATING -> PROVISIONING -> SEEDING)
# ---------------------------------------------------------------------------
@router.post("/{job_id}/provision", response_model=JobOut)
async def provision_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        _user: User = Depends(require_role("operator"))) -> dict:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if job.state not in _PROVISIONABLE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"provisioning impossible depuis l'état {job.state}")

    _set_state(job, "PROVISIONING")
    await _log(db, job.id, "provisioning démarré")
    await db.commit()

    source = await db.get(Endpoint, job.source_id)
    src_args = await _connection_args(db, source)
    try:
        disks = await run_in_threadpool(
            vsphere_bridge.list_disks, **src_args, instance_uuid=job.src_instance_uuid)
    except VSphereError as exc:
        _set_state(job, "VALIDATING")
        await _log(db, job.id, f"provisioning échoué (lecture des disques source) : {exc}", "error")
        await db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"connexion source impossible : {exc}") from exc

    plan_vm = (await db.execute(
        select(PlanVm).where(PlanVm.src_instance_uuid == job.src_instance_uuid,
                             PlanVm.plan_id == job.plan_id))).scalar_one_or_none()

    relay = await db.get(Relay, job.relay_id) if job.relay_id else None
    fc_client = None
    if relay is not None:
        target = await db.get(Endpoint, job.target_id)
        tgt_args = await _connection_args(db, target)
        try:
            fc_client = fc.FusionComputeClient(
                tgt_args["host"], tgt_args["user"], tgt_args["password"],
                port=target.port or fc.DEFAULT_API_PORT, verify=target.verify_tls)
        except fc.FusionComputeError as exc:
            _set_state(job, "VALIDATING")
            await _log(db, job.id, f"provisioning échoué (connexion FusionCompute) : {exc}", "error")
            await db.commit()
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail=f"connexion FusionCompute impossible : {exc}") from exc

    try:
        relay_vm_id = relay.fc_vm_uri.rstrip("/").rsplit("/", 1)[-1] if relay else None
        for d in disks:
            job_disk = JobDisk(job_id=job.id, device_key=d["device_key"],
                               unit_number=d["unit_number"], src_path=d["src_path"],
                               capacity_bytes=d["capacity_bytes"], disk_mode=d["disk_mode"])
            if fc_client is not None and plan_vm is not None:
                # Même règle de dimensionnement que le POC local validé le
                # 17/09/2026 (relay/writer.py, §7.3 : capacité + secteur de
                # signature, arrondi au granule) — pas une nouvelle formule.
                quantity_gb = -(-VolumeWriter.required_volume_size(d["capacity_bytes"]) // (1024 ** 3))
                try:
                    resp = await run_in_threadpool(
                        fc_client.create_and_attach_volume, relay_vm_id,
                        name=f"v2fc-{job.id}-{d['device_key']}", quantity_gb=quantity_gb,
                        datastore_urn=plan_vm.tgt_datastore_urn)
                    await run_in_threadpool(fc_client.wait_task, resp["taskUri"])
                    job_disk.tgt_volume_uri = resp["uri"]
                    await _log(db, job.id,
                              f"volume {resp['urn']} créé et attaché au relais pour le disque {d['device_key']}")
                except fc.FusionComputeError as exc:
                    await _log(db, job.id,
                              f"création du volume échouée pour le disque {d['device_key']} : {exc}", "error")
            db.add(job_disk)
        if fc_client is None:
            await _log(db, job.id,
                       "aucun relais assigné — provisioning FusionCompute sauté, "
                       "le round écrira dans le --target-dir local (mode POC)", "warning")
    finally:
        if fc_client is not None:
            fc_client.close()

    _set_state(job, "SEEDING")
    await _log(db, job.id, "provisioning terminé — prêt pour le seed")
    await db.commit()
    return await _load_job_full(db, job.id)


# ---------------------------------------------------------------------------
# Contexte d'exécution pour `tools/v2fc_source.py job-run`
# ---------------------------------------------------------------------------
@router.get("/{job_id}/run-context", response_model=JobRunContext)
async def get_run_context(job_id: uuid.UUID, round: str,
                          db: AsyncSession = Depends(get_db),
                          _user: User = Depends(require_role("operator"))) -> dict:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    kind = round.upper()
    legal = {"SEED": _SEED_STATES, "DELTA": _DELTA_STATES, "FINAL": _FINAL_STATES}.get(kind)
    if legal is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="round invalide")
    if job.state not in legal:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"round {kind} impossible depuis l'état {job.state}")

    existing = (await db.execute(
        select(SyncRound).where(SyncRound.job_id == job_id, SyncRound.kind == kind,
                                SyncRound.status == "running"))).scalar_one_or_none()
    if existing is None:
        round_no = (await db.execute(
            select(SyncRound).where(SyncRound.job_id == job_id))).scalars().all()
        round_row = SyncRound(job_id=job_id, round_no=len(round_no) + 1, kind=kind, status="running")
        db.add(round_row)
        if kind == "SEED":
            _set_state(job, "SEEDING")
        elif kind == "DELTA":
            _set_state(job, "SYNCING")
        else:
            _set_state(job, "FINAL_DELTA")
        await db.flush()
    else:
        round_row = existing

    source = await db.get(Endpoint, job.source_id)
    await _log(db, job.id, f"round {kind} (#{round_row.round_no}) démarré (job-run)")
    await db.commit()
    return {
        "job_id": job.id, "round_id": round_row.id, "round_kind": kind,
        "round_no": round_row.round_no, "src_host": source.url, "src_port": source.port,
        "src_verify_tls": source.verify_tls, "vm_moref": job.src_vm_moref,
        "vm_instance_uuid": job.src_instance_uuid, "vm_name": job.src_vm_name,
    }


# ---------------------------------------------------------------------------
# Rapport de fin de round (appelé par `job-run` après cmd_sync)
# ---------------------------------------------------------------------------
@router.patch("/{job_id}/rounds/{round_id}", response_model=JobOut)
async def report_round(job_id: uuid.UUID, round_id: uuid.UUID, body: JobRoundReport,
                       db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_role("operator"))) -> dict:
    job = await db.get(Job, job_id)
    round_row = await db.get(SyncRound, round_id)
    if job is None or round_row is None or round_row.job_id != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job ou round introuvable")

    round_row.status = body.status
    # Même piège que _set_state ci-dessus : sync_rounds.ended_at est
    # aussi un TIMESTAMP WITHOUT TIME ZONE.
    round_row.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    round_row.bytes_transferred = body.bytes_written
    if body.duration_s is not None:
        await _log(db, job.id,
                  f"round {round_row.kind} (#{round_row.round_no}) : {body.status} en "
                  f"{body.duration_s:.0f} s, {(body.bytes_written or 0) / 2**20:,.0f} Mio écrits",
                  "error" if body.status == "failed" else "info")
    if body.error:
        await _log(db, job.id, f"erreur : {body.error}", "error")

    for d in body.disks:
        job_disk = (await db.execute(
            select(JobDisk).where(JobDisk.job_id == job_id,
                                  JobDisk.device_key == d.device_key))).scalar_one_or_none()
        if job_disk is not None:
            prev = (await db.execute(
                select(DiskRound).join(SyncRound)
                .where(SyncRound.job_id == job_id, DiskRound.job_disk_id == job_disk.id)
                .order_by(SyncRound.round_no.desc()))).scalars().first()
            db.add(DiskRound(
                round_id=round_row.id, job_disk_id=job_disk.id,
                base_change_id=prev.next_change_id if prev else None,
                next_change_id=d.next_change_id,
                bytes_transferred=d.bytes_transferred))

    if body.status == "cbt_invalidated":
        job.cbt_reseed_count += 1
        _set_state(job, "SEEDING")
        await _log(db, job.id, "CBT invalidé — nouveau seed complet requis", "warning")
    elif body.status == "failed":
        # §4 I5 : un job interrompu doit rester reprenable, pas retomber en
        # FAILED terminal pour un incident réseau — l'état ne bouge pas,
        # Nicolas relance le même round (job-run est idempotent, cf. State).
        pass
    elif round_row.kind == "FINAL":
        await _finalize(db, job)
    else:
        _set_state(job, "SYNCED")

    await db.commit()
    return await _load_job_full(db, job.id)


async def _finalize(db: AsyncSession, job: Job) -> None:
    """
    FINALIZING (§5) : détache le volume du relais, l'attache à une VM cible
    nouvellement créée, démarre si demandé. Ne peut aboutir à CUTOVER_DONE
    que si un relais est réellement assigné — sinon reste en FINALIZING
    avec une entrée de log expliquant pourquoi (pas de FC réel à cette
    étape du projet, voir note de module).
    """
    _set_state(job, "FINALIZING")
    relay = await db.get(Relay, job.relay_id) if job.relay_id else None
    if relay is None:
        await _log(db, job.id,
                   "bascule finale : aucun relais assigné, impossible de créer/attacher "
                   "la VM cible réelle — job laissé en FINALIZING pour revue manuelle",
                   "warning")
        return

    target = await db.get(Endpoint, job.target_id)
    tgt_args = await _connection_args(db, target)
    plan_vm = (await db.execute(
        select(PlanVm).where(PlanVm.src_instance_uuid == job.src_instance_uuid,
                             PlanVm.plan_id == job.plan_id))).scalar_one_or_none()
    disks = (await db.execute(select(JobDisk).where(JobDisk.job_id == job.id))).scalars().all()
    relay_vm_id = relay.fc_vm_uri.rstrip("/").rsplit("/", 1)[-1]

    try:
        with fc.FusionComputeClient(tgt_args["host"], tgt_args["user"], tgt_args["password"],
                                    port=target.port or fc.DEFAULT_API_PORT,
                                    verify=target.verify_tls) as fc_client:
            for jd in disks:
                if not jd.tgt_volume_uri:
                    continue
                vol_urn = f"urn:sites:{fc_client.site.id}:volumes:{jd.tgt_volume_uri.rstrip('/').rsplit('/', 1)[-1]}"
                resp = fc_client.detach_volume(relay_vm_id, vol_urn=vol_urn)
                fc_client.wait_task(resp["taskUri"])

            # Aucun autre moyen documenté de choisir l'emplacement de la VM
            # cible (cluster/hôte) : on réutilise celui de la VM relais
            # elle-même (§7.5 « Querying information about a specified VM »,
            # champ `location`) — c'est l'environnement que Nicolas a déjà
            # choisi en y déployant le relais.
            relay_vm_info = fc_client.get_vm(relay_vm_id)
            create_resp = fc_client.create_vm(
                name=job.src_vm_name, location_urn=relay_vm_info["location"],
                cpu_count=job.spec.get("cpu_count", 2), memory_mb=job.spec.get("memory_mb", 4096),
                boot_firmware=job.spec.get("boot_firmware", "BIOS"),
                nics=[{"portGroupUrn": plan_vm.tgt_network_urn}] if plan_vm.tgt_network_urn else [])
            fc_client.wait_task(create_resp["taskUri"])
            job.tgt_vm_uri = create_resp["uri"]
            tgt_vm_id = create_resp["urn"].rstrip("/").rsplit(":", 1)[-1]

            for jd in disks:
                if not jd.tgt_volume_uri:
                    continue
                vol_urn = f"urn:sites:{fc_client.site.id}:volumes:{jd.tgt_volume_uri.rstrip('/').rsplit('/', 1)[-1]}"
                resp = fc_client.attach_volume(tgt_vm_id, vol_urn=vol_urn)
                fc_client.wait_task(resp["taskUri"])

            if job.spec.get("auto_restart"):
                resp = fc_client.power_on(tgt_vm_id)
                fc_client.wait_task(resp["taskUri"])
                await _log(db, job.id, "VM cible démarrée automatiquement sur FusionCompute")
    except fc.FusionComputeError as exc:
        await _log(db, job.id, f"bascule finale échouée : {exc}", "error")
        return

    _set_state(job, "CUTOVER_DONE")
    await _log(db, job.id, "bascule terminée — VM cible créée sur FusionCompute")


# ---------------------------------------------------------------------------
# Bascule (Nicolas : étape 8, "migrate now")
# ---------------------------------------------------------------------------
@router.post("/{job_id}/cutover", response_model=JobOut)
async def start_cutover(job_id: uuid.UUID, body: JobCutoverRequest,
                        db: AsyncSession = Depends(get_db),
                        _user: User = Depends(require_role("operator"))) -> dict:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if job.state not in _CUTOVER_FROM:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"bascule impossible depuis l'état {job.state}")

    job.spec = {**job.spec, "auto_restart": body.auto_restart}
    _set_state(job, "CUTOVER_PENDING")
    await _log(db, job.id, f"bascule démarrée (redémarrage auto : {body.auto_restart})")
    await db.commit()

    source = await db.get(Endpoint, job.source_id)
    src_args = await _connection_args(db, source)
    _set_state(job, "QUIESCING")
    await db.commit()
    try:
        with VSphereClient(src_args["host"], src_args["user"], src_args["password"],
                           port=src_args["port"], verify=src_args["verify_tls"]) as client:
            vm = client.find_vm(instance_uuid=job.src_instance_uuid)
            mode = await run_in_threadpool(vm.shutdown_guest)
            await _log(db, job.id, f"VM source arrêtée ({mode}) — prêt pour le delta final")
    except VSphereError as exc:
        _set_state(job, "SYNCED")
        await _log(db, job.id, f"extinction de la VM source échouée : {exc}", "error")
        await db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"extinction impossible : {exc}") from exc

    _set_state(job, "FINAL_DELTA")
    await db.commit()
    return await _load_job_full(db, job.id)


@router.post("/{job_id}/rollback", response_model=JobOut)
async def rollback_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_role("operator"))) -> dict:
    """
    Rollback = rallumer la source (§4, invariant I1/promesse de rollback) —
    jamais de suppression de la cible : Nicolas la nettoie lui-même s'il le
    souhaite, ce driver ne détruit rien de sa propre initiative (I1).
    """
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if job.state not in _ROLLBACK_FROM:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"rollback impossible depuis l'état {job.state}")

    source = await db.get(Endpoint, job.source_id)
    src_args = await _connection_args(db, source)
    _set_state(job, "ROLLBACK")
    await db.commit()
    try:
        with VSphereClient(src_args["host"], src_args["user"], src_args["password"],
                           port=src_args["port"], verify=src_args["verify_tls"]) as client:
            vm = client.find_vm(instance_uuid=job.src_instance_uuid)
            if vm.power_state == "poweredOff":
                await run_in_threadpool(
                    lambda: client.wait_task(vm.guarded.PowerOnVM_Task(), label="PowerOnVM"))
                await _log(db, job.id, "VM source redémarrée — rollback effectué")
            else:
                await _log(db, job.id, "VM source déjà allumée — rien à faire")
    except VSphereError as exc:
        await _log(db, job.id, f"rollback échoué (VM source) : {exc}", "error")
        await db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"redémarrage de la source impossible : {exc}") from exc

    _set_state(job, "ROLLED_BACK")
    await db.commit()
    return await _load_job_full(db, job.id)


# ---------------------------------------------------------------------------
# Logs (onglet Logs, alimenté par job-run et par ce routeur lui-même)
# ---------------------------------------------------------------------------
@router.post("/{job_id}/logs", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def push_logs(job_id: uuid.UUID, body: JobLogsIn, db: AsyncSession = Depends(get_db),
                    _user: User = Depends(require_role("operator"))) -> None:
    if await db.get(Job, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    for entry in body.entries:
        db.add(JobLogEntry(job_id=job_id, level=entry.level, message=entry.message))
    await db.commit()


@router.get("/{job_id}/logs", response_model=JobLogsOut)
async def get_logs(job_id: uuid.UUID, since: datetime | None = None,
                   db: AsyncSession = Depends(get_db),
                   _user: User = Depends(require_role("viewer"))) -> dict:
    query = select(JobLogEntry).where(JobLogEntry.job_id == job_id)
    if since is not None:
        query = query.where(JobLogEntry.at > since)
    query = query.order_by(JobLogEntry.at).limit(500)
    result = await db.execute(query)
    return {"entries": list(result.scalars())}
