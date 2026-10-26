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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .. import vsphere_bridge
from ..db import get_db
from ..models import (Credential, DiskRound, Endpoint, Job, JobDisk, JobLogEntry,
                      Plan, PlanVm, Relay, SyncRound, User)
from ..schemas import (JobCreate, JobCutoverRequest, JobDeltaSchedule, JobLogsIn,
                       JobLogsOut, JobOut, JobRoundProgress, JobRoundReport,
                       JobRoundStart, JobRunContext)
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
# SYNCING : reprise d'un delta interrompu (le round « running » est réutilisé).
_DELTA_STATES = {"SYNCED", "SYNCING"}
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
    if body.relay_id is not None:
        relay = await db.get(Relay, body.relay_id)
        if relay is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relais introuvable")
        # Le relais reçoit les volumes cibles en SCSI (§7.1) : il doit vivre
        # sur le même endpoint FusionCompute que la cible du plan.
        if relay.target_id != plan.target_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="ce relais n'appartient pas à l'endpoint FusionCompute cible du plan")

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

    volume_errors: list[str] = []
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
                    volume = await run_in_threadpool(
                        fc_client.get_volume, resp["uri"].rstrip("/").rsplit("/", 1)[-1])
                    job_disk.tgt_dev_path = fc.guest_disk_path(volume["uuid"])
                    await _log(db, job.id,
                              f"volume {resp['urn']} créé et attaché au relais pour le disque {d['device_key']}")
                except fc.FusionComputeError as exc:
                    volume_errors.append(f"disque {d['device_key']} : {exc}")
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

    if volume_errors:
        # Sans volume cible, un seed écrirait silencieusement en local : le job
        # échoue. Les volumes déjà créés restent attachés au relais (leur URI
        # est dans job_disks) — à détacher/supprimer avant de recréer le job.
        _set_state(job, "FAILED")
        await _log(db, job.id, "provisioning échoué — aucun seed possible sans tous les "
                               "volumes cibles ; supprimer ce job et le recréer", "error")
        await db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="création de volume FusionCompute échouée — "
                                   + " ; ".join(volume_errors))

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
            _consume_delta_trigger(job)
        else:
            _set_state(job, "FINAL_DELTA")
        await db.flush()
    else:
        round_row = existing

    source = await db.get(Endpoint, job.source_id)
    job_disks = (await db.execute(select(JobDisk).where(JobDisk.job_id == job_id))).scalars().all()
    await _log(db, job.id, f"round {kind} (#{round_row.round_no}) démarré (job-run)")
    await db.commit()
    return {
        "disks": [{"device_key": d.device_key, "tgt_dev_path": d.tgt_dev_path,
                   "volume_id": d.tgt_volume_uri} for d in job_disks],
        "job_id": job.id, "round_id": round_row.id, "round_kind": kind,
        "round_no": round_row.round_no, "src_host": source.url, "src_port": source.port,
        "src_verify_tls": source.verify_tls, "vm_moref": job.src_vm_moref,
        "vm_instance_uuid": job.src_instance_uuid, "vm_name": job.src_vm_name,
    }


# ---------------------------------------------------------------------------
# Delta manuel ou planifié (26/10/2026) — remplace le delta périodique : le
# runner ne lance un delta que si `delta_requested` ou `delta_scheduled_at`
# échu (api/routers/runner.py).
# ---------------------------------------------------------------------------
def _consume_delta_trigger(job: Job) -> None:
    """Au démarrage d'un delta : la demande est servie ; une planification
    quotidienne passe à la prochaine occurrence future, une ponctuelle
    s'efface (si elle était échue — sinon elle reste pour plus tard)."""
    job.delta_requested = False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if job.delta_scheduled_at is not None and job.delta_scheduled_at <= now:
        if job.delta_repeat_daily:
            while job.delta_scheduled_at <= now:
                job.delta_scheduled_at += timedelta(days=1)
        else:
            job.delta_scheduled_at = None


@router.post("/{job_id}/delta", response_model=JobOut)
async def request_delta(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        _user: User = Depends(require_role("operator"))) -> dict:
    """« Lancer un delta maintenant » : le runner le démarre sous 15 s."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if job.state != "SYNCED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"delta impossible depuis l'état {job.state} "
                                   f"(il faut un job synchronisé, seed terminé)")
    if job.relay_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="job sans relais : lancer le delta avec la commande affichée")
    job.delta_requested = True
    await _log(db, job.id, "delta demandé manuellement")
    await db.commit()
    return await _load_job_full(db, job.id)


@router.put("/{job_id}/delta-schedule", response_model=JobOut)
async def set_delta_schedule(job_id: uuid.UUID, body: JobDeltaSchedule,
                             db: AsyncSession = Depends(get_db),
                             _user: User = Depends(require_role("operator"))) -> dict:
    """Planifie le prochain delta (`at`, répétable chaque jour) ou l'annule (`at` null)."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if body.at is None:
        job.delta_scheduled_at, job.delta_repeat_daily = None, False
        await _log(db, job.id, "planification des deltas annulée")
    else:
        at = body.at if body.at.tzinfo else body.at.replace(tzinfo=timezone.utc)
        job.delta_scheduled_at = at.astimezone(timezone.utc).replace(tzinfo=None)
        job.delta_repeat_daily = body.repeat_daily
        await _log(db, job.id, f"delta planifié le {job.delta_scheduled_at:%d/%m/%Y %H:%M} UTC"
                               + (", puis chaque jour à la même heure" if body.repeat_daily else ""))
    await db.commit()
    return await _load_job_full(db, job.id)


# ---------------------------------------------------------------------------
# Avancement d'un round en cours (job-run, toutes les 5 s) — barre de
# progression de la page Migration. Pendant le round, bytes_transferred
# porte les octets parcourus ; le rapport de fin le remplace par les octets
# réellement écrits.
# ---------------------------------------------------------------------------
@router.post("/{job_id}/rounds/{round_id}/progress", status_code=status.HTTP_204_NO_CONTENT,
             response_model=None)
async def report_round_progress(job_id: uuid.UUID, round_id: uuid.UUID, body: JobRoundProgress,
                                db: AsyncSession = Depends(get_db),
                                _user: User = Depends(require_role("operator"))) -> None:
    round_row = await db.get(SyncRound, round_id)
    if round_row is None or round_row.job_id != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="round introuvable")
    if round_row.status != "running":
        return
    round_row.bytes_planned = body.bytes_planned
    round_row.bytes_transferred = body.bytes_done
    await db.commit()


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
                bytes_transferred=d.bytes_transferred,
                status=body.status))

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
    FINALIZING (§5) : détache les volumes du relais, crée la VM cible avec le
    matériel de la VM source, y attache les volumes, démarre si demandé.
    Reprenable (`POST /jobs/{id}/finalize`) : chaque étape déjà faite est
    sautée (volume déjà détaché, VM déjà créée, volume déjà attaché). En
    cas d'erreur, le job reste en FINALIZING avec l'erreur dans les logs.

    Corrigé le 26/10/2026 au premier essai réel (R00vSkypeEd) : `osOptions`
    obligatoire, CPU/mémoire/firmware repris de la source au lieu de valeurs
    par défaut, disques attachés en IDE (voir `_TARGET_PCI_TYPE`).
    """
    _set_state(job, "FINALIZING")
    relay = await db.get(Relay, job.relay_id) if job.relay_id else None
    if relay is None:
        await _log(db, job.id,
                   "bascule finale : aucun relais assigné, impossible de créer/attacher "
                   "la VM cible réelle — job laissé en FINALIZING pour revue manuelle",
                   "warning")
        return

    source = await db.get(Endpoint, job.source_id)
    src_args = await _connection_args(db, source)
    try:
        hw = await run_in_threadpool(_source_hardware, src_args, job.src_instance_uuid)
    except VSphereError as exc:
        await _log(db, job.id, f"bascule finale : lecture de la VM source impossible : {exc}", "error")
        return

    target = await db.get(Endpoint, job.target_id)
    tgt_args = await _connection_args(db, target)
    plan_vm = (await db.execute(
        select(PlanVm).where(PlanVm.src_instance_uuid == job.src_instance_uuid,
                             PlanVm.plan_id == job.plan_id))).scalar_one_or_none()
    disks = sorted((await db.execute(select(JobDisk).where(JobDisk.job_id == job.id))).scalars().all(),
                   key=lambda d: d.device_key)
    relay_vm_id = relay.fc_vm_uri.rstrip("/").rsplit("/", 1)[-1]

    try:
        with fc.FusionComputeClient(tgt_args["host"], tgt_args["user"], tgt_args["password"],
                                    port=target.port or fc.DEFAULT_API_PORT,
                                    verify=target.verify_tls) as fc_client:
            def vol_urn(jd: JobDisk) -> str:
                return f"urn:sites:{fc_client.site.id}:volumes:{jd.tgt_volume_uri.rstrip('/').rsplit('/', 1)[-1]}"

            on_relay = {d["volumeUrn"] for d in fc_client.get_vm(relay_vm_id)["vmConfig"]["disks"]}
            for jd in disks:
                if jd.tgt_volume_uri and vol_urn(jd) in on_relay:
                    resp = fc_client.detach_volume(relay_vm_id, vol_urn=vol_urn(jd))
                    fc_client.wait_task(resp["taskUri"])
                    await _log(db, job.id, f"volume {vol_urn(jd)} détaché du relais")

            if job.tgt_vm_uri is None:
                os_type, os_version, os_label = fc.pick_os_version(
                    fc_client.list_os_versions(), hw["guest_name"], hw["guest_id"])
                # Aucun autre moyen documenté de choisir l'emplacement de la VM
                # cible : celui de la VM relais (§7.5, champ `location`).
                relay_vm_info = fc_client.get_vm(relay_vm_id)
                create_resp = fc_client.create_vm(
                    name=job.src_vm_name, location_urn=relay_vm_info["location"],
                    cpu_count=hw["cpu"], memory_mb=hw["memory_mb"],
                    os_type=os_type, os_version=os_version,
                    boot_firmware="UEFI" if hw["firmware"] == "efi" else "BIOS",
                    nics=_target_nics(plan_vm))
                fc_client.wait_task(create_resp["taskUri"])
                job.tgt_vm_uri = create_resp["uri"]
                await _log(db, job.id,
                           f"VM cible créée : {len(_target_nics(plan_vm))} carte(s) réseau, "
                           f"{hw['cpu']} vCPU, {hw['memory_mb']} Mo, "
                           f"{hw['firmware'].upper()}, OS « {os_label} » "
                           f"(source : {hw['guest_name']})")
                await db.commit()
            tgt_vm_id = job.tgt_vm_uri.rstrip("/").rsplit("/", 1)[-1]

            on_target = {d["volumeUrn"] for d in fc_client.get_vm(tgt_vm_id)["vmConfig"]["disks"]}
            for i, jd in enumerate(d for d in disks if d.tgt_volume_uri):
                if vol_urn(jd) in on_target:
                    continue
                pci = _TARGET_PCI_TYPE if i < _IDE_SLOTS else "VIRTIO"
                resp = fc_client.attach_volume(tgt_vm_id, vol_urn=vol_urn(jd), pci_type=pci)
                fc_client.wait_task(resp["taskUri"])
                await _log(db, job.id, f"volume {vol_urn(jd)} attaché à la VM cible ({pci})")
            if len(disks) > _IDE_SLOTS:
                await _log(db, job.id,
                           f"disques au-delà du {_IDE_SLOTS}e attachés en VIRTIO : visibles une "
                           f"fois les outils FusionCompute installés dans la VM", "warning")

            if job.spec.get("auto_restart"):
                resp = fc_client.power_on(tgt_vm_id)
                fc_client.wait_task(resp["taskUri"])
                await _log(db, job.id, "VM cible démarrée automatiquement sur FusionCompute")
    except fc.FusionComputeError as exc:
        await _log(db, job.id, f"bascule finale échouée : {exc}", "error")
        return

    _set_state(job, "CUTOVER_DONE")
    await _log(db, job.id, "bascule terminée — VM cible créée sur FusionCompute. Disques en "
                           "IDE : installer les outils FusionCompute dans la VM avant de les "
                           "passer en VIRTIO (performances)")


def _target_nics(plan_vm: PlanVm | None) -> list[dict]:
    """Une carte FusionCompute par carte source mappée, dans l'ordre source,
    avec la MAC source si demandé (champ `mac` accepté par le VRM, vérifié le
    26/10/2026) ; plan antérieur au mapping par carte : la seule carte de
    tgt_network_urn."""
    if plan_vm is None:
        return []
    mapped = [n for n in (plan_vm.nic_mappings or []) if n.get("tgt_network_urn")]
    if mapped:
        return [{"portGroupUrn": n["tgt_network_urn"],
                 **({"mac": n["mac"]} if n.get("keep_mac") and n.get("mac") else {})}
                for n in sorted(mapped, key=lambda n: n["device_key"])]
    return [{"portGroupUrn": plan_vm.tgt_network_urn}] if plan_vm.tgt_network_urn else []


# Une VM venue de VMware n'a pas de pilote virtio (Windows en particulier :
# écran bleu INACCESSIBLE_BOOT_DEVICE au démarrage). IDE fonctionne sans
# pilote, mais FusionCompute n'en propose que 3 emplacements.
_TARGET_PCI_TYPE = "IDE"
_IDE_SLOTS = 3


def _source_hardware(src_args: dict, instance_uuid: str) -> dict:
    with VSphereClient(src_args["host"], src_args["user"], src_args["password"],
                       port=src_args["port"], verify=src_args["verify_tls"]) as client:
        vm = client.find_vm(instance_uuid=instance_uuid).raw
        cfg = vm.config
        # Nom remonté par VMware Tools (plus précis) s'il existe, sinon celui
        # déclaré dans la configuration de la VM.
        guest_name = (vm.guest.guestFullName if vm.guest else None) or cfg.guestFullName
        return {"cpu": cfg.hardware.numCPU, "memory_mb": cfg.hardware.memoryMB,
                "firmware": cfg.firmware or "bios", "guest_name": guest_name,
                "guest_id": cfg.guestId or ""}


@router.post("/{job_id}/finalize", response_model=JobOut)
async def retry_finalize(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         _user: User = Depends(require_role("operator"))) -> dict:
    """Relance une finalisation restée en FINALIZING après une erreur."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if job.state != "FINALIZING":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"finalisation impossible depuis l'état {job.state}")
    await _log(db, job.id, "finalisation relancée")
    await _finalize(db, job)
    await db.commit()
    return await _load_job_full(db, job.id)


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
