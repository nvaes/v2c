from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from .. import vsphere_bridge
from ..db import get_db
from ..models import Credential, Endpoint, Job, Plan, PlanVm, User
from ..schemas import (PlanCreate, PlanOut, PlanPreflightResponse, PlanTargetUpdate,
                       PlanVmMappingUpdate, SourceNic)
from ..security import decrypt_credential_password, require_role

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
async def delete_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                      _user: User = Depends(require_role("operator"))) -> None:
    """
    Symétrique à DELETE /endpoints/{id} (HANDOFF.md, 17/09/2026) : Nicolas a
    demandé un moyen de retirer un plan de migration (créé par erreur, ou
    plus utile) sans passer par la base. Les VM du plan disparaissent avec
    (cascade ORM `Plan.vms` + FK `plan_vms.plan_id` en `ON DELETE CASCADE`,
    voir `api/models.py`) — rien à faire de spécial pour elles. Refuse en
    revanche si un job d'exécution référence encore ce plan, plutôt que de
    laisser remonter une contrainte de clé étrangère en 500 nu : `jobs.plan_id`
    n'a pas de `ON DELETE CASCADE` (§8.1), volontairement, pour ne jamais
    perdre un historique d'exécution en silence. Aucun job n'est créé par
    l'API pour l'instant (L2 pas encore écrit) : ce garde-fou ne se
    déclenche donc pas en pratique aujourd'hui, mais protège dès que L2
    existera — même discipline que pour les endpoints.
    """
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan introuvable")

    result = await db.execute(select(Job.id).where(Job.plan_id == plan_id))
    referencing = result.scalars().all()
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"ce plan est encore référencé par {len(referencing)} job(s) "
                   f"d'exécution — impossible de le supprimer sans perdre leur historique")

    await db.delete(plan)
    await db.commit()

