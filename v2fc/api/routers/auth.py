from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import PasswordChange, TokenResponse, UserOut
from ..security import (RUNNER_USER_ID, create_access_token, get_current_user, hash_password,
                        verify_password)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends(),
                db: AsyncSession = Depends(get_db)) -> TokenResponse:
    result = await db.execute(select(User).where(User.email == form.username))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="identifiants invalides")
    return TokenResponse(access_token=create_access_token(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def change_own_password(body: PasswordChange, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)) -> None:
    """Changement de son propre mot de passe (l'ancien est exigé)."""
    if user.id == RUNNER_USER_ID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte de service")
    db_user = await db.get(User, user.id)
    if not verify_password(body.current_password, db_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="mot de passe actuel incorrect")
    db_user.password_hash = hash_password(body.new_password)
    await db.commit()
