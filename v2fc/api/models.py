"""
Modèle de données — ARCHITECTURE.md §8.1, complété par les tables qui y sont
référencées mais jamais définies dans le document (`credentials`, `plans`) et
par la table `users` qu'exige l'authentification du §9 mais qui n'est
décrite nulle part. Ces ajouts sont documentés dans HANDOFF.md — ce ne sont
pas des décisions d'API vSphere/FusionCompute (où « ne rien inventer »
s'applique), juste la plomberie ordinaire qu'un schéma applicatif requiert.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, CheckConstraint, ForeignKey, LargeBinary,
                        UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------
# Authentification (absente du §9 littéral, nécessaire à son RBAC)
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('viewer','operator','admin')", name="ck_users_role"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(default="viewer")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# --------------------------------------------------------------------------
# Identifiants — chiffrement enveloppe, §13 : « Jamais journalisés, jamais
# renvoyés par l'API (écriture seule) ».
# --------------------------------------------------------------------------
class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    label: Mapped[str]
    username: Mapped[str]
    encrypted_password: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"))


# --------------------------------------------------------------------------
# Environnements — ARCHITECTURE.md §8.1
# --------------------------------------------------------------------------
class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        CheckConstraint("kind IN ('vcenter','fusioncompute')", name="ck_endpoints_kind"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str]
    name: Mapped[str]
    url: Mapped[str]
    # Port TCP de l'API. Historiquement toujours 443 (implicite, jamais
    # stocké) tant que seul vCenter/ESXi existait. FusionCompute casse cette
    # hypothèse : ARCHITECTURE.md §7.5 documente un port d'API (7443) distinct
    # du port de la console VRM (8443, celui donné par Nicolas) — les deux
    # restent à distinguer sur ce lab précis. D'où une colonne explicite,
    # plutôt que le 443 en dur qu'utilisait _connection_args jusqu'ici.
    port: Mapped[int] = mapped_column(default=443)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credentials.id"))
    tls_fingerprint: Mapped[str | None]
    verify_tls: Mapped[bool] = mapped_column(default=True)
    last_probe_at: Mapped[datetime | None]
    last_probe_ok: Mapped[bool | None]

    credential: Mapped["Credential"] = relationship()


class Relay(Base):
    __tablename__ = "relays"

    id: Mapped[uuid.UUID] = _uuid_pk()
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("endpoints.id"))
    fc_vm_uri: Mapped[str]
    agent_url: Mapped[str]
    max_slots: Mapped[int] = mapped_column(default=12)
    used_slots: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="OFFLINE")


# --------------------------------------------------------------------------
# Plans — référencé par `jobs.plan_id` au §8.1 mais jamais défini : c'est le
# regroupement « un lot de VM + mappings communs » du §9 (POST /plans).
# --------------------------------------------------------------------------
class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str]
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("endpoints.id"))
    # optionnel : un plan existe avant qu'une cible FusionCompute ne soit
    # choisie (étape 3 du flux, PUT /plans/{id}/target)
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    source: Mapped["Endpoint"] = relationship(foreign_keys=[source_id])
    vms: Mapped[list["PlanVm"]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class PlanVm(Base):
    """
    Une VM sélectionnée dans un plan. Volontairement minimal pour l'instant
    (identité seule) : les mappings par VM (réseau, CPU/RAM, firmware — §9)
    viendront quand le driver cible existera (D2), pas avant, pour ne pas
    deviner leur forme.
    """
    __tablename__ = "plan_vms"
    __table_args__ = (
        UniqueConstraint("plan_id", "src_instance_uuid", name="uq_plan_vm"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"))
    src_vm_moref: Mapped[str]
    src_instance_uuid: Mapped[str]
    src_vm_name: Mapped[str]
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Mapping cible par VM (étapes 4-5 du flux voulu par Nicolas, 17/09/2026:
    # choix du datastore puis du réseau FusionCompute pour chaque VM). On ne
    # stocke QUE ce que datamover.fusioncompute.list_datastores/list_networks
    # renvoie déjà (urn + name), pas un nouvel objet inventé : le nom est
    # dupliqué ici pour l'affichage même si le datastore/réseau disparaît
    # ensuite côté FusionCompute (l'urn seul ne raconte rien à l'écran).
    tgt_datastore_urn: Mapped[str | None]
    tgt_datastore_name: Mapped[str | None]
    tgt_network_urn: Mapped[str | None]
    tgt_network_name: Mapped[str | None]
    # Une entrée par carte réseau source (migration 0005) ; tgt_network_*
    # ci-dessus = première carte mappée.
    nic_mappings: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")

    plan: Mapped["Plan"] = relationship(back_populates="vms")


# --------------------------------------------------------------------------
# Jobs et cycles de synchronisation — ARCHITECTURE.md §8.1. Schéma complet
# dès maintenant ; la logique d'exécution viendra avec L2, une fois le
# driver cible débloqué (D2). Non utilisées par l'API pour l'instant.
# --------------------------------------------------------------------------
class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("endpoints.id"))
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("endpoints.id"))
    relay_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("relays.id"))
    src_vm_moref: Mapped[str]
    src_vm_name: Mapped[str]
    src_instance_uuid: Mapped[str]
    tgt_vm_uri: Mapped[str | None]
    state: Mapped[str]
    state_since: Mapped[datetime] = mapped_column(server_default=func.now())
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    cbt_reseed_count: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class JobDisk(Base):
    __tablename__ = "job_disks"
    __table_args__ = (UniqueConstraint("job_id", "device_key", name="uq_job_disk"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"))
    device_key: Mapped[int]
    unit_number: Mapped[int]
    src_path: Mapped[str]
    # BIGINT, pas INTEGER : une capacité de disque en octets dépasse
    # systématiquement 2^31 (int4) dès qu'un disque fait plus de ~2 Go —
    # confirmé le 17/09/2026 par Nicolas (asyncpg DataError "value out of
    # int32 range" sur un disque de 4 Gio, /jobs/{id}/provision).
    capacity_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_mode: Mapped[str]
    tgt_volume_uri: Mapped[str | None]
    tgt_dev_path: Mapped[str | None]
    tgt_signature: Mapped[bytes | None] = mapped_column(LargeBinary)


class SyncRound(Base):
    __tablename__ = "sync_rounds"
    __table_args__ = (
        CheckConstraint("kind IN ('SEED','DELTA','FINAL')", name="ck_sync_rounds_kind"),
        UniqueConstraint("job_id", "round_no", name="uq_job_round"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"))
    round_no: Mapped[int]
    kind: Mapped[str]
    snapshot_moref: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    status: Mapped[str]
    # Même raison que JobDisk.capacity_bytes ci-dessus : un total d'octets
    # transférés sur un round dépasse 2^31 dès quelques Gio (le seed
    # test_ubuntu du 17/09/2026, 2 142 Mio, dépasse déjà cette limite).
    bytes_planned: Mapped[int | None] = mapped_column(BigInteger)
    bytes_transferred: Mapped[int | None] = mapped_column(BigInteger)


class DiskRound(Base):
    __tablename__ = "disk_rounds"
    __table_args__ = (UniqueConstraint("round_id", "job_disk_id", name="uq_round_disk"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_rounds.id", ondelete="CASCADE"))
    job_disk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_disks.id", ondelete="CASCADE"))
    base_change_id: Mapped[str | None]
    next_change_id: Mapped[str | None]
    extent_count: Mapped[int | None]
    # Mêmes colonnes-octets que SyncRound/JobDisk ci-dessus (BigInteger).
    bytes_planned: Mapped[int | None] = mapped_column(BigInteger)
    bytes_transferred: Mapped[int | None] = mapped_column(BigInteger)
    cursor_index: Mapped[int] = mapped_column(default=0)
    # cursor_offset est un décalage en octets dans le disque (pas un index
    # de zone) : BigInteger pour la même raison que capacity_bytes.
    cursor_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str]


class Extent(Base):
    __tablename__ = "extents"

    disk_round_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("disk_rounds.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(primary_key=True)
    # Décalage/longueur en octets dans le disque : BigInteger pour la même
    # raison que capacity_bytes (un décalage vers la fin d'un disque de
    # plusieurs Gio dépasse 2^31 tout aussi facilement que la capacité
    # elle-même).
    start_offset: Mapped[int] = mapped_column(BigInteger)
    length: Mapped[int] = mapped_column(BigInteger)


class RollbackStep(Base):
    __tablename__ = "rollback_steps"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"))
    seq: Mapped[int]
    action: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    undone_at: Mapped[datetime | None]


class JobLogEntry(Base):
    """
    Journal d'un job — alimenté par `tools/v2fc_source.py job-run` (chaque
    ligne de log du cycle en cours, transmise par callback HTTP) et par
    l'API elle-même (changements d'état, erreurs de provisioning/bascule).
    Distinct de `audit_log` : ceci est le fil d'exécution technique affiché
    dans l'onglet Logs (17/09/2026), `audit_log` reste le journal d'actions
    signées par un acteur humain (§13).
    """
    __tablename__ = "job_log_entries"
    __table_args__ = (
        CheckConstraint("level IN ('debug','info','warning','error')",
                        name="ck_job_log_entries_level"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    level: Mapped[str] = mapped_column(default="info")
    message: Mapped[str]


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str]
    target: Mapped[str | None]
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[str | None]
