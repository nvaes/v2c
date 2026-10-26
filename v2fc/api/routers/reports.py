"""
Rapport de migration par plan (26/10/2026) — à remettre au client : pour
chaque VM, cible choisie, disques, chaque synchronisation (début, fin,
volume écrit, résultat), bascule et VM créée sur FusionCompute.

Lecture seule, assemblé depuis la base ; l'interface le met en page
(page imprimable, enregistrable en PDF par le navigateur).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_db
from ..models import Endpoint, Job, JobDisk, JobLogEntry, Plan, Relay, SyncRound, User
from ..security import require_role

router = APIRouter(prefix="/api/v1/plans", tags=["reports"])

# Messages de journal retenus comme événements clés (en plus des
# avertissements et erreurs).
_KEY_EVENTS = ("job créé", "provisioning", "volume", "bascule", "VM cible", "VM source",
               "rollback", "CBT", "delta demandé", "delta quotidien")


@router.get("/{plan_id}/report")
async def plan_report(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_role("viewer"))) -> dict:
    plan = (await db.execute(select(Plan).where(Plan.id == plan_id)
                             .options(selectinload(Plan.vms)))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")
    source = await db.get(Endpoint, plan.source_id)
    target = await db.get(Endpoint, plan.target_id) if plan.target_id else None
    creator = await db.get(User, plan.created_by) if plan.created_by else None
    jobs = {j.src_instance_uuid: j for j in (await db.execute(
        select(Job).where(Job.plan_id == plan_id))).scalars()}

    vms = []
    for vm in sorted(plan.vms, key=lambda v: v.src_vm_name.lower()):
        job = jobs.get(vm.src_instance_uuid)
        entry = {
            "name": vm.src_vm_name, "instance_uuid": vm.src_instance_uuid,
            "datastore": vm.tgt_datastore_name,
            "nics": vm.nic_mappings or ([{"label": "carte réseau", "src_network": "",
                                          "tgt_network_name": vm.tgt_network_name,
                                          "mac": "", "keep_mac": False}]
                                        if vm.tgt_network_name else []),
            "job": None,
        }
        if job is not None:
            relay = await db.get(Relay, job.relay_id) if job.relay_id else None
            disks = (await db.execute(select(JobDisk).where(JobDisk.job_id == job.id)
                                      .order_by(JobDisk.device_key))).scalars().all()
            rounds = (await db.execute(select(SyncRound).where(SyncRound.job_id == job.id)
                                       .order_by(SyncRound.started_at))).scalars().all()
            logs = (await db.execute(select(JobLogEntry).where(JobLogEntry.job_id == job.id)
                                     .order_by(JobLogEntry.at))).scalars().all()
            events = [{"at": e.at, "level": e.level, "message": e.message} for e in logs
                      if e.level in ("warning", "error")
                      or any(k.lower() in e.message.lower() for k in _KEY_EVENTS)]
            entry["job"] = {
                "state": job.state, "state_since": job.state_since, "created_at": job.created_at,
                "relay": relay.fc_vm_uri if relay else None, "tgt_vm_uri": job.tgt_vm_uri,
                "cbt_reseed_count": job.cbt_reseed_count,
                "disks": [{"device_key": d.device_key, "capacity_bytes": d.capacity_bytes,
                           "volume": d.tgt_volume_uri} for d in disks],
                "rounds": [{"no": r.round_no, "kind": r.kind, "status": r.status,
                            "started_at": r.started_at, "ended_at": r.ended_at,
                            "bytes": r.bytes_transferred} for r in rounds],
                "events": events[-60:],
                "errors": sum(1 for e in logs if e.level == "error"),
            }
        vms.append(entry)

    done = sum(1 for v in vms if v["job"] and v["job"]["state"] == "CUTOVER_DONE")
    return {
        "plan": {"id": plan.id, "name": plan.name, "created_at": plan.created_at,
                 "created_by": creator.email if creator else None},
        "source": {"name": source.name, "url": source.url} if source else None,
        "target": {"name": target.name, "url": target.url} if target else None,
        "generated_at": datetime.now(timezone.utc), "generated_by": user.email,
        "summary": {"vms": len(vms), "migrated": done,
                    "in_progress": sum(1 for v in vms if v["job"]) - done,
                    "not_started": sum(1 for v in vms if not v["job"])},
        "vms": vms,
    }
