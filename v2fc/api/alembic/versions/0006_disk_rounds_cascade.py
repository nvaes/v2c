"""disk_rounds.job_disk_id en ON DELETE CASCADE (26/10/2026).

Trouvé en supprimant un plan réel (500) : supprimer un job cascade sur
job_disks et sync_rounds, mais disk_rounds référençait job_disks sans
cascade — dès qu'un round avait été rapporté, la suppression échouait
(ForeignKeyViolation disk_rounds_job_disk_id_fkey). C'était la seule clé
de la chaîne jobs → job_disks/sync_rounds → disk_rounds → extents sans
cascade.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26
"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("disk_rounds_job_disk_id_fkey", "disk_rounds", type_="foreignkey")
    op.create_foreign_key("disk_rounds_job_disk_id_fkey", "disk_rounds", "job_disks",
                          ["job_disk_id"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    op.drop_constraint("disk_rounds_job_disk_id_fkey", "disk_rounds", type_="foreignkey")
    op.create_foreign_key("disk_rounds_job_disk_id_fkey", "disk_rounds", "job_disks",
                          ["job_disk_id"], ["id"])
