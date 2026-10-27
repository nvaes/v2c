"""
Configuration de l'API — variables d'environnement uniquement, jamais de
secret en dur. Voir ARCHITECTURE.md §13 (Sécurité).
"""
from __future__ import annotations

import base64
import os
from functools import lru_cache


class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://v2fc:v2fc@localhost:5432/v2fc")
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    jwt_secret: str = os.environ.get("JWT_SECRET", "")
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = int(os.environ.get("ACCESS_TOKEN_MINUTES", "30"))
    refresh_token_days: int = int(os.environ.get("REFRESH_TOKEN_DAYS", "7"))

    # Clé de chiffrement enveloppe (KEK) des identifiants stockés en base
    # (§13 : « Chiffrement enveloppe (DEK par secret, KEK depuis variable
    # d'environnement ou coffre) »). 32 octets, encodés en base64.
    kek_b64: str = os.environ.get("V2FC_KEK", "")

    # Secret partagé avec le service `runner` (tools/v2fc_runner.py), qui
    # exécute les rounds sans intervention. Vide = runner désactivé.
    runner_token: str = os.environ.get("V2FC_RUNNER_TOKEN", "")

    cors_origins: list[str] = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "https://localhost:5173").split(",")
        if o.strip()
    ]

    def __init__(self) -> None:
        if not self.jwt_secret:
            raise RuntimeError(
                "JWT_SECRET non défini — obligatoire, pas de valeur par défaut "
                "pour un secret de signature de jeton.")
        if not self.kek_b64:
            raise RuntimeError(
                "V2FC_KEK non défini — obligatoire pour chiffrer les identifiants "
                "en base (§13). Générer avec : "
                "python3 -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\"")
        try:
            key = base64.b64decode(self.kek_b64)
        except Exception as exc:
            raise RuntimeError(f"V2FC_KEK n'est pas du base64 valide : {exc}") from exc
        if len(key) != 32:
            raise RuntimeError(
                f"V2FC_KEK doit décoder en 32 octets (AES-256), reçu {len(key)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
