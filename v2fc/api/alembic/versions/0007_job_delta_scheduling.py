"""delta manuel ou planifié au lieu du delta périodique automatique (26/10/2026).

Demande de Nicolas : pas de delta toutes les N minutes ; un delta se lance
à la main ou à une date choisie (planificateur, répétable chaque jour).

- `delta_requested` : delta demandé « maintenant », pris par le runner ;
- `delta_scheduled_at` : prochain delta planifié (UTC, sans fuseau comme les
  autres horodatages de `jobs`) ;
- `delta_repeat_daily` : replanifier 24 h plus tard après chaque delta.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("delta_requested", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
    op.add_column("jobs", sa.Column("delta_scheduled_at", sa.DateTime(), nullable=True))
    op.add_column("jobs", sa.Column("delta_repeat_daily", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("jobs", "delta_repeat_daily")
    op.drop_column("jobs", "delta_scheduled_at")
    op.drop_column("jobs", "delta_requested")
