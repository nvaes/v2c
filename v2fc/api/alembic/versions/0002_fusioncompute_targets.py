"""ajout du port d'endpoint et du mapping cible par VM (datastore/réseau
FusionCompute) — ARCHITECTURE.md §7.5 (D2 résolu le 17/09/2026), api/models.py.

Migration additive explicite (contrairement à 0001, qui laissait
create_all faire le schéma initial) : la base existe déjà avec des données
réelles depuis le test matériel du 16/09/2026, donc on ajoute des colonnes,
on ne recrée rien.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "endpoints",
        sa.Column("port", sa.Integer(), nullable=False, server_default="443"),
    )
    # server_default a servi à remplir les lignes existantes (tous les
    # endpoints déjà créés étaient forcément en 443, seul port jamais
    # utilisé jusqu'ici) ; on le retire pour que la valeur par défaut ne
    # vive que côté modèle Python, pas en double dans le schéma.
    op.alter_column("endpoints", "port", server_default=None)

    op.add_column("plan_vms", sa.Column("tgt_datastore_urn", sa.String(), nullable=True))
    op.add_column("plan_vms", sa.Column("tgt_datastore_name", sa.String(), nullable=True))
    op.add_column("plan_vms", sa.Column("tgt_network_urn", sa.String(), nullable=True))
    op.add_column("plan_vms", sa.Column("tgt_network_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_vms", "tgt_network_name")
    op.drop_column("plan_vms", "tgt_network_urn")
    op.drop_column("plan_vms", "tgt_datastore_name")
    op.drop_column("plan_vms", "tgt_datastore_urn")
    op.drop_column("endpoints", "port")
