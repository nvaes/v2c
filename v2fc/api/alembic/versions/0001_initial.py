"""schéma initial — ARCHITECTURE.md §8.1 + compléments (users, credentials,
plans, plan_vms) documentés dans docs/dev/JOURNAL.md et api/models.py.

Revision ID: 0001
Revises:
Create Date: 2026-09-16
"""
from __future__ import annotations

from alembic import op

from api.db import Base
from api import models  # noqa: F401  (enregistre les tables sur Base.metadata)

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Crée le schéma depuis les modèles ORM. N'est plus exécutée sur une
    # installation neuve : api/migrate.py crée alors le schéma complet et
    # marque la base à jour (sinon 0002+ échoueraient, leurs colonnes
    # existant déjà). Conservée pour l'historique des bases existantes.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
