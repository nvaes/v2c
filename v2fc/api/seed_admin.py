#!/usr/bin/env python3
"""
Crée le premier compte administrateur, à l'installation. Les comptes
suivants se gèrent dans l'interface (page Utilisateurs, rôle admin).

Usage :
    docker compose exec api python3 -m api.seed_admin --email a@b.c
(mot de passe demandé de façon interactive, jamais en argument)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass

from sqlalchemy import select

from .db import get_session_factory
from .models import User
from .security import hash_password


async def _run(email: str, password: str, role: str) -> None:
    factory = get_session_factory()
    async with factory() as db:
        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none() is not None:
            raise SystemExit(f"{email} existe déjà")
        user = User(email=email, password_hash=hash_password(password), role=role)
        db.add(user)
        await db.commit()
        print(f"utilisateur {email} créé avec le rôle « {role} »")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    ap.add_argument("--role", default="admin", choices=["viewer", "operator", "admin"])
    args = ap.parse_args()
    password = getpass.getpass(f"Mot de passe pour {args.email} : ")
    confirm = getpass.getpass("Confirmer : ")
    if password != confirm:
        raise SystemExit("les deux mots de passe ne correspondent pas")
    asyncio.run(_run(args.email, password, args.role))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
