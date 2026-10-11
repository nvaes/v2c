"""
Migrations Alembic. Utilise un moteur SYNCHRONE (psycopg2) indépendant du
moteur async de l'application (asyncpg) — c'est le chemin le plus simple
pour Alembic, qui ne sait pas gérer un moteur asyncio nativement. La même
DATABASE_URL sert aux deux : le schéma « +asyncpg » est traduit ici en
« +psycopg2 ».
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # -> /work

from api.db import Base                # noqa: E402
from api import models                 # noqa: E402,F401  (enregistre les tables)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://v2fc:v2fc@localhost:5432/v2fc")
    return url.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
