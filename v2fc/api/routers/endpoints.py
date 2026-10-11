from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .. import fusioncompute_bridge, vsphere_bridge
from ..cache import (get_cached_fc_datastores, get_cached_fc_networks,
                     get_cached_inventory, get_inventory_progress,
                     set_cached_fc_datastores, set_cached_fc_networks,
                     set_cached_inventory)
from ..db import get_db
from ..models import Credential, Endpoint, Plan, User
from ..schemas import (EndpointCreate, EndpointOut, FcDatastoresResponse,
                       FcNetworksResponse, InventoryProgress, InventoryResponse,
                       ProbeResult)
from ..security import (decrypt_credential_password, encrypt_credential_password,
                        require_role)

router = APIRouter(prefix="/api/v1/endpoints", tags=["endpoints"])


async def _connection_args(db: AsyncSession, endpoint: Endpoint) -> dict:
    cred = await db.get(Credential, endpoint.credential_id)
    if cred is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="identifiants de l'endpoint introuvables")
    return {
        "host": endpoint.url, "user": cred.username,
        "password": decrypt_credential_password(cred),
        "port": endpoint.port, "verify_tls": endpoint.verify_tls,
    }


def _bridge_for(endpoint: Endpoint):
    """
    vCenter/ESXi et FusionCompute parlent des protocoles totalement
    différents (pyVmomi/SOAP vs REST+JSON, §7.5) : un seul point de
    branchement ici, plutôt que de semer des `if kind ==` dans chaque route.
    """
    return vsphere_bridge if endpoint.kind == "vcenter" else fusioncompute_bridge


@router.post("", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(body: EndpointCreate, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_role("operator"))) -> Endpoint:
    encrypted_password, encrypted_dek, nonce = encrypt_credential_password(body.password)
    cred = Credential(label=f"{body.name} ({body.username})", username=body.username,
                      encrypted_password=encrypted_password, encrypted_dek=encrypted_dek,
                      nonce=nonce, created_by=user.id)
    db.add(cred)
    await db.flush()

    endpoint = Endpoint(kind=body.kind, name=body.name, url=body.url, port=body.port,
                        credential_id=cred.id, verify_tls=body.verify_tls)
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


@router.get("", response_model=list[EndpointOut])
async def list_endpoints(db: AsyncSession = Depends(get_db),
                         _user: User = Depends(require_role("viewer"))) -> list[Endpoint]:
    result = await db.execute(select(Endpoint).order_by(Endpoint.name))
    return list(result.scalars())


@router.get("/{endpoint_id}", response_model=EndpointOut)
async def get_endpoint(endpoint_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_role("viewer"))) -> Endpoint:
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")
    return endpoint


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_endpoint(endpoint_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                          _user: User = Depends(require_role("operator"))) -> None:
    """
    Manquait depuis le début de L0b : il n'y avait aucun moyen de retirer un
    endpoint mal configuré (mauvais port, faute de frappe) sans passer par la
    base directement. Refuse si un plan référence encore cet endpoint (source
    ou cible) plutôt que de laisser la contrainte de clé étrangère remonter
    en 500 nu — Nicolas doit d'abord traiter ces plans, pas les perdre en
    silence.
    """
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")

    result = await db.execute(
        select(Plan.id, Plan.name)
        .where((Plan.source_id == endpoint_id) | (Plan.target_id == endpoint_id)))
    referencing = result.all()
    if referencing:
        names = ", ".join(p.name for p in referencing)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cet endpoint est encore utilisé par {len(referencing)} plan(s) "
                   f"({names}) — supprimez ou réassignez ces plans avant de le retirer")

    credential_id = endpoint.credential_id
    await db.delete(endpoint)
    await db.flush()

    # Chaque endpoint reçoit sa propre credential dédiée à sa création
    # (create_endpoint ci-dessus) : rien ne les partage en pratique, donc on
    # nettoie l'identifiant devenu orphelin plutôt que de laisser un mot de
    # passe chiffré sans endpoint qui le référence.
    still_used = await db.execute(
        select(Endpoint.id).where(Endpoint.credential_id == credential_id))
    if still_used.first() is None:
        cred = await db.get(Credential, credential_id)
        if cred is not None:
            await db.delete(cred)

    await db.commit()


@router.post("/{endpoint_id}/probe", response_model=ProbeResult)
async def probe_endpoint(endpoint_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         _user: User = Depends(require_role("operator"))) -> ProbeResult:
    from datetime import datetime, timezone

    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")
    args = await _connection_args(db, endpoint)
    result = await run_in_threadpool(_bridge_for(endpoint).probe, **args)

    # `endpoints.last_probe_at` est un TIMESTAMP WITHOUT TIME ZONE (le modèle
    # ne déclare pas timezone=True) — asyncpg refuse un datetime tz-aware sur
    # une colonne naive ("can't subtract offset-naive and offset-aware datetimes"),
    # ce qui faisait échouer CE probe (500), FusionCompute comme vCenter/ESXi,
    # avant ce correctif. On stocke un datetime naïf, UTC par construction.
    endpoint.last_probe_at = datetime.now(timezone.utc).replace(tzinfo=None)
    endpoint.last_probe_ok = result["ok"]
    if result.get("tls_fingerprint_sha1"):
        endpoint.tls_fingerprint = result["tls_fingerprint_sha1"]
    await db.commit()
    return ProbeResult(**result)


@router.get("/{endpoint_id}/inventory", response_model=InventoryResponse)
async def get_inventory(endpoint_id: uuid.UUID, refresh: bool = False,
                        db: AsyncSession = Depends(get_db),
                        _user: User = Depends(require_role("viewer"))) -> dict:
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")
    if endpoint.kind != "vcenter":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="l'inventaire de VM ne s'applique qu'aux endpoints vCenter/ESXi — "
                   "pour un endpoint FusionCompute, voir /datastores et /networks")

    if not refresh:
        cached = await get_cached_inventory(endpoint_id)
        if cached is not None:
            return cached

    args = await _connection_args(db, endpoint)
    try:
        data = await run_in_threadpool(vsphere_bridge.inventory, endpoint_id=endpoint_id, **args)
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"connexion à {endpoint.name} impossible : {exc}") from exc
    await set_cached_inventory(endpoint_id, data)
    return data


@router.get("/{endpoint_id}/inventory/progress", response_model=InventoryProgress)
async def get_inventory_progress_route(
        endpoint_id: uuid.UUID, db: AsyncSession = Depends(get_db),
        _user: User = Depends(require_role("viewer"))) -> dict:
    """
    Poll léger pour l'UI pendant qu'un inventaire tourne en tâche de fond
    (GET /{endpoint_id}/inventory, potentiellement long : une requête
    vSphere complète avec pré-vérification par VM). Ne relance rien côté
    vCenter — ne fait que lire ce que le thread bloquant de `get_inventory`
    a publié dans Redis au fil de son avancement (`api/cache.py`,
    `set_inventory_progress_sync`). Pas de garde « kind == vcenter » : un
    endpoint FusionCompute ou un endpoint sans scan en cours renvoie
    simplement l'état par défaut « idle », ce qui est la bonne réponse dans
    les deux cas plutôt qu'une erreur.
    """
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")
    progress = await get_inventory_progress(endpoint_id)
    if progress is None:
        return {"phase": "idle", "done": 0, "total": 0}
    return progress


def _require_fusioncompute(endpoint: Endpoint) -> None:
    if endpoint.kind != "fusioncompute":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="datastores/réseaux ne s'appliquent qu'aux endpoints FusionCompute "
                   "(étapes 3-5 du flux, ARCHITECTURE.md §7.5)")


@router.get("/{endpoint_id}/datastores", response_model=FcDatastoresResponse)
async def get_fc_datastores(endpoint_id: uuid.UUID, refresh: bool = False,
                            db: AsyncSession = Depends(get_db),
                            _user: User = Depends(require_role("viewer"))) -> dict:
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")
    _require_fusioncompute(endpoint)

    if not refresh:
        cached = await get_cached_fc_datastores(endpoint_id)
        if cached is not None:
            return {"datastores": cached}

    args = await _connection_args(db, endpoint)
    try:
        data = await run_in_threadpool(fusioncompute_bridge.list_datastores, **args)
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"connexion à {endpoint.name} impossible : {exc}") from exc
    await set_cached_fc_datastores(endpoint_id, data)
    return {"datastores": data}


@router.get("/{endpoint_id}/networks", response_model=FcNetworksResponse)
async def get_fc_networks(endpoint_id: uuid.UUID, refresh: bool = False,
                          db: AsyncSession = Depends(get_db),
                          _user: User = Depends(require_role("viewer"))) -> dict:
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint introuvable")
    _require_fusioncompute(endpoint)

    if not refresh:
        cached = await get_cached_fc_networks(endpoint_id)
        if cached is not None:
            return {"networks": cached}

    args = await _connection_args(db, endpoint)
    try:
        data = await run_in_threadpool(fusioncompute_bridge.list_networks, **args)
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"connexion à {endpoint.name} impossible : {exc}") from exc
    await set_cached_fc_networks(endpoint_id, data)
    return {"networks": data}
