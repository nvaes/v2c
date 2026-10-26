"""élargit en BIGINT les colonnes qui comptent des octets (17/09/2026, soir).

Trouvé en conditions réelles : `job_disks.capacity_bytes` était
un INTEGER (int4, max ~2,147 Go) alors qu'une capacité de disque en octets
dépasse cette limite dès qu'un disque fait plus de ~2 Gio — le disque de
test_ubuntu (4 Gio = 4 294 967 296 octets) faisait déjà planter
POST /jobs/{id}/provision (asyncpg.DataError: "value out of int32 range").
Même défaut de conception sur toutes les autres colonnes qui comptent des
octets dans le même schéma (§8.1) : `sync_rounds.bytes_planned`/
`bytes_transferred` (le seed test_ubuntu du jour, 2 142 Mio, dépasse déjà
2^31 lui aussi), `disk_rounds.bytes_planned`/`bytes_transferred`/
`cursor_offset`, `extents.start_offset`/`length` — ces trois derniers ne
sont pas encore écrits par le code actuel (`api/routers/jobs.py`), mais
partagent exactement le même risque dès qu'ils le seront ; les corriger
maintenant évite de retomber sur le même bug plus tard sur un disque plus
gros. `job_disks.device_key`/`unit_number` et les compteurs/index
(`round_no`, `extent_count`, `cursor_index`, `idx`, `seq`, ...) restent en
INTEGER : ce ne sont pas des quantités d'octets.

Migration additive au sens large : élargir INTEGER -> BIGINT ne perd
aucune donnée existante (postgres caste silencieusement), pas de
`postgresql_using` nécessaire.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("job_disks", "capacity_bytes"),
    ("sync_rounds", "bytes_planned"),
    ("sync_rounds", "bytes_transferred"),
    ("disk_rounds", "bytes_planned"),
    ("disk_rounds", "bytes_transferred"),
    ("disk_rounds", "cursor_offset"),
    ("extents", "start_offset"),
    ("extents", "length"),
]


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, type_=sa.BigInteger(), existing_type=sa.Integer())


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, type_=sa.Integer(), existing_type=sa.BigInteger())
