"""
Registre des VM relais (ARCHITECTURE.md §7.1/§13) — enregistrement, pas
d'orchestration : ce routeur ne fait qu'inscrire une VM relais existante
(créée par l'opérateur dans FusionCompute) et l'URL où son agent réseau
écoutera plus tard. Voir `datamover/fusioncompute.py` (note de module) :
cet agent réseau lui-même n'est pas encore écrit — le champ `agent_url`
est donc réservé, sans caller côté job aujourd'hui.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from starlette.concurrency import run_in_threadpool

from ..db import get_db
from ..models import Endpoint, Relay, User
from ..schemas import RelayCapacity, RelayCreate, RelayOut
from ..security import require_role
from .jobs import _RELAY_VIRTIO_SLOTS, _connection_args
from datamover import fusioncompute as fc

router = APIRouter(prefix="/api/v1/relays", tags=["relays"])


@router.post("", response_model=RelayOut, status_code=status.HTTP_201_CREATED)
async def create_relay(body: RelayCreate, db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_role("operator"))) -> Relay:
    target = await db.get(Endpoint, body.target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="endpoint cible introuvable")
    if target.kind != "fusioncompute":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="un relais vit forcément sur un endpoint FusionCompute")

    relay = Relay(target_id=body.target_id, fc_vm_uri=body.fc_vm_uri,
                  agent_url=body.agent_url, max_slots=body.max_slots, status="OFFLINE")
    db.add(relay)
    await db.commit()
    await db.refresh(relay)
    return relay


@router.get("", response_model=list[RelayOut])
async def list_relays(db: AsyncSession = Depends(get_db),
                      _user: User = Depends(require_role("viewer"))) -> list[Relay]:
    result = await db.execute(select(Relay))
    return list(result.scalars())


@router.get("/capacity", response_model=list[RelayCapacity])
async def relays_capacity(db: AsyncSession = Depends(get_db),
                          _user: User = Depends(require_role("viewer"))) -> list[dict]:
    """
    Emplacements virtio occupés sur chaque relais (18/11/2026), lus en direct
    sur FusionCompute — même comptage que le refus au provisioning
    (`jobs._capacity_problem`), pour alerter dès la création du plan plutôt
    qu'au premier provisioning. Un relais injoignable est renvoyé avec
    `error` (used à null), sans faire échouer les autres.
    """
    relays = list((await db.execute(select(Relay))).scalars())
    out: list[dict] = []
    by_target: dict[uuid.UUID, list[Relay]] = {}
    for r in relays:
        by_target.setdefault(r.target_id, []).append(r)
    for target_id, group in by_target.items():
        target = await db.get(Endpoint, target_id)
        args = await _connection_args(db, target)

        def _count(target=target, args=args, group=group) -> list[dict]:
            rows = []
            with fc.FusionComputeClient(args["host"], args["user"], args["password"],
                                        port=target.port or fc.DEFAULT_API_PORT,
                                        verify=target.verify_tls) as client:
                for r in group:
                    vm_id = r.fc_vm_uri.rstrip("/").rsplit("/", 1)[-1]
                    try:
                        used = len(client.get_vm(vm_id)["vmConfig"]["disks"])
                        rows.append({"relay_id": r.id, "target_id": r.target_id,
                                     "used": used, "total": _RELAY_VIRTIO_SLOTS})
                    except fc.FusionComputeError as exc:
                        rows.append({"relay_id": r.id, "target_id": r.target_id, "used": None,
                                     "total": _RELAY_VIRTIO_SLOTS, "error": str(exc)})
            return rows

        try:
            out += await run_in_threadpool(_count)
        except fc.FusionComputeError as exc:
            out += [{"relay_id": r.id, "target_id": r.target_id, "used": None,
                     "total": _RELAY_VIRTIO_SLOTS, "error": f"{target.name} : {exc}"}
                    for r in group]
    return out


@router.delete("/{relay_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_relay(relay_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_role("operator"))) -> None:
    relay = await db.get(Relay, relay_id)
    if relay is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relais introuvable")
    await db.delete(relay)
    await db.commit()
