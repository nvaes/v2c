"""
Service runner (tools/v2fc_runner.py) — exécution automatique des rounds,
ajoutée le 26/10/2026 à la demande de Nicolas (« ça doit être en
automatique ») : plus de commande `job-run` à copier dans un terminal.

Le runner demande ici quoi faire, puis lance `job-run` lui-même. Seul un
job avec relais et volumes FusionCompute attachés est pris en charge : sans
relais, un round écrirait en local (mode POC) et reste manuel.

Exception assumée au §13 (« jamais de secret en sortie ») : le runner a
besoin du mot de passe source pour VDDK. Il ne le reçoit que sur une route
réservée à son secret V2FC_RUNNER_TOKEN, jamais avec un JWT d'utilisateur.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..models import Credential, Endpoint, Job, JobDisk, SyncRound, User
from ..schemas import RunnerSourceCredentials, RunnerWorkItem
from ..security import decrypt_credential_password, require_runner

router = APIRouter(prefix="/api/v1/runner", tags=["runner"])

# Après un round en échec, attendre avant de relancer (incident réseau,
# source injoignable...) plutôt que de boucler.
_RETRY_AFTER = timedelta(minutes=5)

_ROUND_FOR_STATE = {"SEEDING": "seed", "SYNCING": "delta", "FINAL_DELTA": "final"}


@router.get("/work", response_model=list[RunnerWorkItem])
async def get_work(db: AsyncSession = Depends(get_db),
                   _runner: User = Depends(require_runner)) -> list[dict]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delta_every = timedelta(minutes=get_settings().delta_interval_min)
    jobs = (await db.execute(
        select(Job).where(Job.relay_id.is_not(None),
                          Job.state.in_([*_ROUND_FOR_STATE, "SYNCED"])))).scalars().all()
    work = []
    for job in jobs:
        disks = (await db.execute(select(JobDisk).where(JobDisk.job_id == job.id))).scalars().all()
        if not disks or any(not d.tgt_dev_path for d in disks):
            continue
        last = (await db.execute(
            select(SyncRound).where(SyncRound.job_id == job.id)
            .order_by(SyncRound.round_no.desc()))).scalars().first()
        if last is not None and last.status == "failed" and last.ended_at \
                and now - last.ended_at < _RETRY_AFTER:
            continue
        if job.state == "SYNCED":
            if last is not None and last.ended_at and now - last.ended_at < delta_every:
                continue
            work.append({"job_id": job.id, "round": "delta"})
        else:
            work.append({"job_id": job.id, "round": _ROUND_FOR_STATE[job.state]})
    return work


@router.get("/jobs/{job_id}/source-credentials", response_model=RunnerSourceCredentials)
async def get_source_credentials(job_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                                 _runner: User = Depends(require_runner)) -> dict:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    source = await db.get(Endpoint, job.source_id)
    cred = await db.get(Credential, source.credential_id)
    return {"user": cred.username, "password": decrypt_credential_password(cred)}
