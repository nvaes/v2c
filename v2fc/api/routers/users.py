"""
Gestion des utilisateurs depuis l'interface (26/10/2026) — jusqu'ici, seul
`api.seed_admin` (ligne de commande) créait des comptes.

Réservé au rôle admin, sauf le changement de son propre mot de passe
(`POST /auth/password`, dans auth.py). Pas de suppression : un compte est
désactivé, car plans et jobs gardent leur auteur (`created_by`). Deux
garde-fous contre le verrouillage : on ne se désactive ni ne se rétrograde
soi-même, et il reste toujours au moins un admin actif.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import UserCreate, UserOut, UserUpdate
from ..security import hash_password, require_role

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db),
                     _admin: User = Depends(require_role("admin"))) -> list[User]:
    return list((await db.execute(select(User).order_by(User.email))).scalars())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db),
                      _admin: User = Depends(require_role("admin"))) -> User:
    email = body.email.strip().lower()
    if (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{email} existe déjà")
    user = User(email=email, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, body: UserUpdate, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_role("admin"))) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="utilisateur introuvable")

    loses_admin = user.role == "admin" and user.is_active and (
        (body.role is not None and body.role != "admin") or body.is_active is False)
    if loses_admin:
        if user.id == admin.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="impossible de retirer vos propres droits d'admin "
                                       "ou de désactiver votre propre compte")
        admins = (await db.execute(select(func.count()).select_from(User).where(
            User.role == "admin", User.is_active.is_(True)))).scalar()
        if admins <= 1:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="il doit rester au moins un administrateur actif")

    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    await db.commit()
    await db.refresh(user)
    return user
