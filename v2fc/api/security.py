"""
Authentification JWT, RBAC à trois rôles, hachage des mots de passe
utilisateurs, et chiffrement enveloppe des identifiants vCenter/ESXi
(ARCHITECTURE.md §9 et §13).
"""
from __future__ import annotations

import base64
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_db
from .models import Credential, User

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# RBAC à trois rôles (§9) : ordre croissant de privilège.
_ROLE_ORDER = {"viewer": 0, "operator": 1, "admin": 2}

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


# --------------------------------------------------------------------------
# Mots de passe utilisateurs
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd_context.verify(password, password_hash)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def create_access_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="jeton invalide ou expiré") from exc


# Identité du service runner : un utilisateur « operator » qui n'existe pas
# en base, reconnu au secret V2FC_RUNNER_TOKEN au lieu d'un JWT.
RUNNER_USER_ID = uuid.UUID(int=0)


def _runner_user() -> User:
    return User(id=RUNNER_USER_ID, email="runner@v2fc", password_hash="",
                role="operator", is_active=True)


async def get_current_user(token: str = Depends(_oauth2_scheme),
                           db: AsyncSession = Depends(get_db)) -> User:
    runner_token = get_settings().runner_token
    if runner_token and hmac.compare_digest(token.encode(), runner_token.encode()):
        return _runner_user()
    payload = _decode_token(token)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="jeton invalide") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="utilisateur introuvable ou désactivé")
    return user


def require_role(minimum: str):
    """
    Dépendance FastAPI : refuse (403) si le rôle de l'utilisateur courant est
    en dessous de `minimum` dans l'ordre viewer < operator < admin.
    """
    if minimum not in _ROLE_ORDER:
        raise ValueError(f"rôle inconnu : {minimum!r}")

    async def _dep(user: User = Depends(get_current_user)) -> User:
        if _ROLE_ORDER[user.role] < _ROLE_ORDER[minimum]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"rôle « {minimum} » ou supérieur requis")
        return user
    return _dep


async def require_runner(user: User = Depends(get_current_user)) -> User:
    """Réservé au service runner (seul à recevoir des identifiants source)."""
    if user.id != RUNNER_USER_ID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="réservé au service runner")
    return user


# --------------------------------------------------------------------------
# Chiffrement enveloppe des identifiants (§13)
#
# DEK (Data Encryption Key) aléatoire par secret, chiffrée par la KEK (Key
# Encryption Key, dans V2FC_KEK) via AES-256-GCM. Le mot de passe en clair
# n'est jamais journalisé ni renvoyé par l'API — voir schemas.py, où
# `encrypted_password` n'apparaît dans aucun schéma de sortie.
# --------------------------------------------------------------------------
def encrypt_credential_password(password: str) -> tuple[bytes, bytes, bytes]:
    """Renvoie (mot de passe chiffré, DEK chiffrée par la KEK, nonce)."""
    settings = get_settings()
    kek = base64.b64decode(settings.kek_b64)
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)

    aead_dek = AESGCM(dek)
    encrypted_password = aead_dek.encrypt(nonce, password.encode("utf-8"), None)

    aead_kek = AESGCM(kek)
    dek_nonce = os.urandom(12)
    encrypted_dek = dek_nonce + aead_kek.encrypt(dek_nonce, dek, None)

    return encrypted_password, encrypted_dek, nonce


def decrypt_credential_password(cred: Credential) -> str:
    settings = get_settings()
    kek = base64.b64decode(settings.kek_b64)
    aead_kek = AESGCM(kek)
    dek_nonce, dek_ciphertext = cred.encrypted_dek[:12], cred.encrypted_dek[12:]
    dek = aead_kek.decrypt(dek_nonce, dek_ciphertext, None)

    aead_dek = AESGCM(dek)
    password = aead_dek.decrypt(cred.nonce, cred.encrypted_password, None)
    return password.decode("utf-8")


async def load_credential(db: AsyncSession, credential_id) -> Credential:
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="identifiants introuvables")
    return cred
