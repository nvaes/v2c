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
    # Première migration : on laisse SQLAlchemy créer le schéma depuis les
    # modèles ORM plutôt que de le retranscrire à la main en op.create_table
    # — cohérence garantie avec api/models.py. Les migrations suivantes,
    # une fois le schéma stabilisé, devront passer par un vrai
    # `alembic revision --autogenerate`.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
