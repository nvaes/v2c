"""
Schéma de la base au démarrage de l'API (remplace `alembic upgrade head`).

- Base vide (installation neuve) : schéma créé directement depuis les
  modèles (`api/models.py`), puis marqué à jour (`alembic stamp head`).
- Base existante : migrations Alembic jusqu'à la dernière (`upgrade head`).

Pourquoi : la migration 0001 crée le schéma depuis les modèles *actuels*,
qui contiennent déjà ce qu'ajoutent 0002 et suivantes. Enchaînées sur une
base vide, ces migrations échouaient (colonne `endpoints.port` déjà
existante) : constaté le 26/10/2026 à la première réinstallation complète.

À lancer depuis le dossier de `alembic.ini` : `cd /work/api && python3 -m api.migrate`.
"""
from __future__ import annotations

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from api import models  # noqa: F401  (enregistre les tables)
from api.db import Base


def main() -> None:
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://v2fc:v2fc@localhost:5432/v2fc")
    engine = create_engine(url.replace("+asyncpg", "+psycopg2"))
    tables = set(inspect(engine).get_table_names())
    cfg = Config("alembic.ini")
    if "alembic_version" not in tables and not tables & set(Base.metadata.tables):
        print("base vide : création du schéma depuis les modèles, puis stamp head")
        Base.metadata.create_all(engine)
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
    engine.dispose()


if __name__ == "__main__":
    main()
