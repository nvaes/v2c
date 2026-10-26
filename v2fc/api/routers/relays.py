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

from ..db import get_db
from ..models import Endpoint, Relay, User
from ..schemas import RelayCreate, RelayOut
from ..security import require_role

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


@router.delete("/{relay_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_relay(relay_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_role("operator"))) -> None:
    relay = await db.get(Relay, relay_id)
    if relay is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relais introuvable")
    await db.delete(relay)
    await db.commit()
