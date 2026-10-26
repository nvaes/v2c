from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from .. import vsphere_bridge
from ..db import get_db
from ..models import Credential, Endpoint, Job, JobDisk, Plan, PlanVm, Relay, SyncRound, User
from ..schemas import (PlanCreate, PlanOut, PlanPreflightResponse, PlanTargetUpdate,
                       PlanVmMappingUpdate, SourceNic)
from ..security import decrypt_credential_password, require_role
from .jobs import _connection_args
from datamover import fusioncompute as fc

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


@router.post("", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanCreate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_role("operator"))) -> Plan:
    source = await db.get(Endpoint, body.source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="endpoint source introuvable")
    if body.target_id is not None and await db.get(Endpoint, body.target_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="endpoint cible introuvable")

    plan = Plan(name=body.name, source_id=body.source_id, target_id=body.target_id,
               created_by=user.id)
    plan.vms = [PlanVm(src_vm_moref=v.moref, src_instance_uuid=v.instance_uuid,
                       src_vm_name=v.name) for v in body.vms]
    db.add(plan)
    await db.commit()
    result = await db.execute(
        select(Plan).where(Plan.id == plan.id).options(selectinload(Plan.vms)))
    return result.scalar_one()


@router.get("", response_model=list[PlanOut])
async def list_plans(db: AsyncSession = Depends(get_db),
                     _user: User = Depends(require_role("viewer"))) -> list[Plan]:
    result = await db.execute(select(Plan).options(selectinload(Plan.vms)).order_by(Plan.created_at.desc()))
    return list(result.scalars())


@router.get("/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                   _user: User = Depends(require_role("viewer"))) -> Plan:
    result = await db.execute(
        select(Plan).where(Plan.id == plan_id).options(selectinload(Plan.vms)))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")
    return plan


@router.get("/{plan_id}/preflight", response_model=PlanPreflightResponse)
async def plan_preflight(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         _user: User = Depends(require_role("viewer"))) -> dict:
    """
    Repasse chaque VM du plan par datamover.preflight.run_preflight, à la
    demande — les VM peuvent avoir changé d'état depuis la création du plan
    (CBT désactivé entretemps, VM éteinte, etc.), donc pas de simple lecture
    de ce qui a été mis en cache à la sélection.
    """
    result = await db.execute(
        select(Plan).where(Plan.id == plan_id).options(selectinload(Plan.vms)))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")

    endpoint = await db.get(Endpoint, plan.source_id)
    cred = await db.get(Credential, endpoint.credential_id)
    args = {"host": endpoint.url, "user": cred.username,
           "password": decrypt_credential_password(cred),
           "port": 443, "verify_tls": endpoint.verify_tls,
           "instance_uuids": [v.src_instance_uuid for v in plan.vms]}
    try:
        vms = await run_in_threadpool(vsphere_bridge.preflight_many, **args)
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"connexion à {endpoint.name} impossible : {exc}") from exc
    return {"plan_id": plan_id, "vms": vms}


@router.get("/{plan_id}/source-nics", response_model=dict[str, list[SourceNic]])
async def plan_source_nics(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                           _user: User = Depends(require_role("viewer"))) -> dict:
    """Cartes réseau actuelles de chaque VM du plan, lues sur le vCenter/ESXi
    source, par id de VM de plan — base du mapping réseau carte par carte."""
    result = await db.execute(
        select(Plan).where(Plan.id == plan_id).options(selectinload(Plan.vms)))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")

    endpoint = await db.get(Endpoint, plan.source_id)
    cred = await db.get(Credential, endpoint.credential_id)
    try:
        by_uuid = await run_in_threadpool(
            vsphere_bridge.nics_many, host=endpoint.url, user=cred.username,
            password=decrypt_credential_password(cred), port=endpoint.port or 443,
            verify_tls=endpoint.verify_tls,
            instance_uuids=[v.src_instance_uuid for v in plan.vms])
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"connexion à {endpoint.name} impossible : {exc}") from exc
    return {str(v.id): by_uuid.get(v.src_instance_uuid, []) for v in plan.vms}


@router.put("/{plan_id}/target", response_model=PlanOut)
async def set_plan_target(plan_id: uuid.UUID, body: PlanTargetUpdate,
                          db: AsyncSession = Depends(get_db),
                          _user: User = Depends(require_role("operator"))) -> Plan:
    """
    Choix de l'environnement FusionCompute cible d'un plan (étape 3 du flux
    voulu par Nicolas, 17/09/2026) — séparé de la création du plan : le plan
    existe déjà (créé depuis l'inventaire source) avant qu'une cible ne soit
    choisie, donc c'est une mise à jour, pas un champ de PlanCreate.
    """
    result = await db.execute(
        select(Plan).where(Plan.id == plan_id).options(selectinload(Plan.vms)))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")

    target = await db.get(Endpoint, body.target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="endpoint cible introuvable")
    if target.kind != "fusioncompute":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="la cible d'un plan doit être un endpoint FusionCompute")

    plan.target_id = body.target_id
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put("/{plan_id}/vms/{plan_vm_id}/mapping", response_model=PlanOut)
async def set_plan_vm_mapping(plan_id: uuid.UUID, plan_vm_id: uuid.UUID,
                              body: PlanVmMappingUpdate,
                              db: AsyncSession = Depends(get_db),
                              _user: User = Depends(require_role("operator"))) -> Plan:
    """
    Mapping datastore/réseau cible d'une VM du plan (étapes 4-5) : ne fait
    que recopier ce que /endpoints/{target}/datastores et /networks ont
    renvoyé, l'API ne valide pas ici que l'urn existe encore côté
    FusionCompute (ça se refera à l'exécution du job, L2 — pas la peine de
    reconnecter au VRM juste pour enregistrer un choix).
    """
    plan_vm = await db.get(PlanVm, plan_vm_id)
    if plan_vm is None or plan_vm.plan_id != plan_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="VM de plan introuvable")

    if body.datastore_urn is not None:
        plan_vm.tgt_datastore_urn = body.datastore_urn
        plan_vm.tgt_datastore_name = body.datastore_name
    if body.network_urn is not None:
        plan_vm.tgt_network_urn = body.network_urn
        plan_vm.tgt_network_name = body.network_name
    if body.nics is not None:
        nics = sorted(body.nics, key=lambda n: n.device_key)
        plan_vm.nic_mappings = [n.model_dump() for n in nics]
        first = next((n for n in nics if n.tgt_network_urn), None)
        plan_vm.tgt_network_urn = first.tgt_network_urn if first else None
        plan_vm.tgt_network_name = first.tgt_network_name if first else None
    await db.commit()

    result = await db.execute(
        select(Plan).where(Plan.id == plan_id).options(selectinload(Plan.vms)))
    return result.scalar_one()


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_plan(plan_id: uuid.UUID, with_jobs: bool = False, delete_volumes: bool = True,
                      db: AsyncSession = Depends(get_db),
                      _user: User = Depends(require_role("operator"))) -> None:
    """
    Supprime un plan et ses VM (cascade `plan_vms.plan_id`).

    Sans `with_jobs`, refuse (409) si des jobs référencent le plan :
    `jobs.plan_id` n'a volontairement pas de `ON DELETE CASCADE` (§8.1), pour
    ne jamais perdre un historique d'exécution en silence.

    Avec `with_jobs=true` (bouton « Supprimer » de la page Plans, 26/10/2026) :
    supprime aussi les jobs et leur historique, sauf si l'un d'eux est en
    pleine copie ou bascule (409 : attendre la fin, ou faire un rollback).
    `delete_volumes` (défaut oui) détache du relais et supprime sur
    FusionCompute les volumes des migrations non terminées — copies
    partielles qui occuperaient sinon des emplacements disque du relais.
    Les volumes d'une migration terminée (CUTOVER_DONE) appartiennent à la
    VM migrée : jamais touchés.
    """
    # VM chargées d'avance : la cascade ORM Plan.vms ne peut pas les charger
    # paresseusement en asynchrone au moment du delete.
    plan = (await db.execute(select(Plan).where(Plan.id == plan_id)
                             .options(selectinload(Plan.vms)))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")

    jobs = (await db.execute(select(Job).where(Job.plan_id == plan_id))).scalars().all()
    if jobs and not with_jobs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"ce plan est encore référencé par {len(jobs)} job(s) d'exécution — "
                   f"les supprimer avec le plan (with_jobs=true) ou les supprimer d'abord")

    running = {r.job_id for r in (await db.execute(
        select(SyncRound).where(SyncRound.job_id.in_([j.id for j in jobs]),
                                SyncRound.status == "running"))).scalars()}
    busy = [j for j in jobs if j.id in running or j.state in _BUSY_STATES]
    if busy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="copie ou bascule en cours pour "
                   + ", ".join(f"{j.src_vm_name} ({j.state})" for j in busy)
                   + " — attendre la fin du round ou faire un rollback avant de supprimer")

    if delete_volumes:
        for job in jobs:
            if job.state == "CUTOVER_DONE":
                continue
            try:
                await _release_volumes(db, job)
            except fc.FusionComputeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"suppression des volumes de {job.src_vm_name} sur FusionCompute "
                           f"impossible : {exc} — rien n'a été supprimé côté application") from exc

    for job in jobs:
        await db.delete(job)
    await db.flush()
    await db.delete(plan)
    await db.commit()


# Copie ou bascule en cours : un job dans ces états ne se supprime pas.
_BUSY_STATES = {"PROVISIONING", "CUTOVER_PENDING", "QUIESCING", "FINAL_DELTA",
                "FINALIZING", "ROLLBACK"}


async def _release_volumes(db: AsyncSession, job: Job) -> None:
    """Détache du relais puis supprime les volumes FusionCompute d'un job."""
    disks = [d for d in (await db.execute(
        select(JobDisk).where(JobDisk.job_id == job.id))).scalars() if d.tgt_volume_uri]
    if not disks:
        return
    target = await db.get(Endpoint, job.target_id)
    args = await _connection_args(db, target)
    relay = await db.get(Relay, job.relay_id) if job.relay_id else None

    def _release() -> None:
        with fc.FusionComputeClient(args["host"], args["user"], args["password"],
                                    port=target.port or fc.DEFAULT_API_PORT,
                                    verify=target.verify_tls) as client:
            relay_vm_id = relay.fc_vm_uri.rstrip("/").rsplit("/", 1)[-1] if relay else None
            on_relay = ({d["volumeUrn"] for d in client.get_vm(relay_vm_id)["vmConfig"]["disks"]}
                        if relay_vm_id else set())
            for d in disks:
                vol_id = d.tgt_volume_uri.rstrip("/").rsplit("/", 1)[-1]
                urn = f"urn:sites:{client.site.id}:volumes:{vol_id}"
                try:
                    client.get_volume(vol_id)
                except fc.FusionComputeError:
                    continue                      # déjà supprimé
                if urn in on_relay:
                    client.wait_task(client.detach_volume(relay_vm_id, vol_urn=urn)["taskUri"])
                task = client.delete_volume(vol_id).get("taskUri")
                if task:
                    client.wait_task(task)

    await run_in_threadpool(_release)

