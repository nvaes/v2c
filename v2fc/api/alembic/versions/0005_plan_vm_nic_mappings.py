"""mapping réseau par carte pour chaque VM d'un plan (26/10/2026).

Demande utilisateur : une VM source peut avoir plusieurs cartes réseau
(VM-WIN-01 en a deux) ; chacune reçoit son propre réseau FusionCompute de
destination. Liste JSON dans `plan_vms.nic_mappings` :
`{device_key, label, adapter, mac, src_network, tgt_network_urn, tgt_network_name}`.
`tgt_network_urn`/`tgt_network_name` restent (première carte mappée) pour
les plans existants et la validation de création de job.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_vms", sa.Column("nic_mappings", sa.JSON(), nullable=False,
                                        server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column("plan_vms", "nic_mappings")
