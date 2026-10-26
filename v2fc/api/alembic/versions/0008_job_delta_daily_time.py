"""planificateur de delta : une heure par jour, en heure locale (26/10/2026).

Nicolas : « met juste à quelle heure chaque jour ». Remplace la date +
case « chaque jour » de la 0007 :
- `delta_daily_time` « HH:MM » et `delta_timezone` (IANA, celui du
  navigateur) : l'heure suit l'heure locale, changements d'heure compris ;
- `delta_scheduled_at` (0007) garde la prochaine occurrence en UTC, que le
  runner compare à l'heure courante ;
- `delta_repeat_daily` disparaît (toujours quotidien).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("delta_daily_time", sa.String(5), nullable=True))
    op.add_column("jobs", sa.Column("delta_timezone", sa.String(64), nullable=True))
    op.drop_column("jobs", "delta_repeat_daily")
    # Une planification ponctuelle de la 0007 n'a plus de sens sans heure quotidienne.
    op.execute("UPDATE jobs SET delta_scheduled_at = NULL")


def downgrade() -> None:
    op.add_column("jobs", sa.Column("delta_repeat_daily", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
    op.drop_column("jobs", "delta_timezone")
    op.drop_column("jobs", "delta_daily_time")
