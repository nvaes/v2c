"""ajout de job_log_entries — journal technique des jobs (onglet Logs,
17/09/2026, socle L2). ARCHITECTURE.md §8.1 (jobs/sync_rounds/...) était
déjà entièrement modélisé mais jamais exposé par l'API ; ce journal est le
seul ajout de table nécessaire pour L2, le reste existe depuis 0001.

Migration additive (même discipline que 0002) : on ajoute une table, on ne
retouche à aucune donnée existante.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_log_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                 sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("level", sa.String(), nullable=False, server_default="info"),
        sa.Column("message", sa.String(), nullable=False),
        sa.CheckConstraint("level IN ('debug','info','warning','error')",
                           name="ck_job_log_entries_level"),
    )
    op.create_index("ix_job_log_entries_job_id", "job_log_entries", ["job_id"])
    op.create_index("ix_job_log_entries_at", "job_log_entries", ["at"])


def downgrade() -> None:
    op.drop_index("ix_job_log_entries_at", table_name="job_log_entries")
    op.drop_index("ix_job_log_entries_job_id", table_name="job_log_entries")
    op.drop_table("job_log_entries")
