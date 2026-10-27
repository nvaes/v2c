"""Schémas Pydantic — entrée/sortie de l'API. Aucun secret en sortie (§13)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime | None = None


# Gestion des utilisateurs (admin) — 26/10/2026. Mots de passe : 10 caractères
# au moins, jamais renvoyés.
_ROLE = "^(viewer|operator|admin)$"


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)
    role: str = Field(default="operator", pattern=_ROLE)


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=_ROLE)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


# --------------------------------------------------------------------------
# Endpoints (vCenter ou ESXi — même « kind » : datamover.vsphere.VSphereClient
# gère les deux de façon transparente, voir docs/dev/JOURNAL.md)
# --------------------------------------------------------------------------
class EndpointCreate(BaseModel):
    kind: str = Field(default="vcenter", pattern="^(vcenter|fusioncompute)$")
    name: str
    url: str = Field(description="hôte ou adresse IP de l'ESXi/vCenter")
    port: int = 443
    username: str
    password: str = Field(repr=False)
    verify_tls: bool = False


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    name: str
    url: str
    port: int
    verify_tls: bool
    last_probe_at: datetime | None
    last_probe_ok: bool | None


class ProbeResult(BaseModel):
    ok: bool
    message: str
    platform: str | None = None
    tls_fingerprint_sha1: str | None = None


# --------------------------------------------------------------------------
# Ressources FusionCompute (cible) — ARCHITECTURE.md §7.5, D2 résolu.
# Reflète tel quel ce que datamover.fusioncompute renvoie (urn/name/etc.),
# pas de champ inventé au-delà de ce que la documentation VRM décrit.
# --------------------------------------------------------------------------
class FcDatastore(BaseModel):
    urn: str
    name: str
    status: str | None = None
    storage_type: str | None = None
    actual_capacity_gb: int | None = None
    actual_free_gb: int | None = None


class FcDatastoresResponse(BaseModel):
    datastores: list[FcDatastore]


class FcNetwork(BaseModel):
    urn: str
    name: str
    dvswitch_urn: str
    dvswitch_name: str
    port_type: int | None = None
    vlan_id: int | None = None
    vlan_range: str | None = None


class FcNetworksResponse(BaseModel):
    networks: list[FcNetwork]


# --------------------------------------------------------------------------
# Inventaire — reflète datamover.vsphere (SourceVm, préflight)
# --------------------------------------------------------------------------
class InventoryVm(BaseModel):
    moref: str
    instance_uuid: str
    name: str
    power_state: str
    guest_id: str
    firmware: str
    num_disks: int
    disk_capacity_gb: float
    cbt_enabled: bool
    migratable: bool
    preflight_findings: list[str] = Field(default_factory=list)


class InventoryCluster(BaseModel):
    name: str
    moref: str
    hosts: list[str]
    # Pas de VM ici : vsphere_bridge.inventory() ne les rattache pas au
    # cluster (elles peuvent être hors cluster, en pool de ressources
    # autonome, etc.) mais au datacenter — voir InventoryDatacenter.vms.
    # Un cluster ne sert qu'à afficher ses hôtes ESXi.


class InventoryDatacenter(BaseModel):
    name: str
    moref: str
    clusters: list[InventoryCluster]
    vms: list[InventoryVm]


class InventoryResponse(BaseModel):
    datacenters: list[InventoryDatacenter]
    vm_count: int


class InventoryProgress(BaseModel):
    """
    État d'avancement d'un inventaire vCenter en cours — voir `api/cache.py`
    (`set_inventory_progress_sync` / `get_inventory_progress`) et
    `GET /endpoints/{id}/inventory/progress`. `phase` vaut « idle » (aucun
    scan en cours ou jamais lancé), « connecting » (connexion vCenter en
    cours, avant même de connaître le nombre de VM), « preflight » (VM en
    cours de pré-vérification, `done`/`total` significatifs) ou « done »
    (scan terminé, la réponse principale de /inventory est déjà disponible).
    """
    phase: str
    done: int
    total: int


# --------------------------------------------------------------------------
# Plans — §9 : « un plan = un lot de VM + mappings communs »
# --------------------------------------------------------------------------
class PlanVmIn(BaseModel):
    moref: str
    instance_uuid: str
    name: str


class PlanCreate(BaseModel):
    name: str
    source_id: uuid.UUID
    target_id: uuid.UUID | None = None
    vms: list[PlanVmIn] = Field(min_length=1)


class PlanNicMapping(BaseModel):
    """Une carte réseau de la VM source et son réseau FusionCompute cible
    (`tgt_network_urn` absent : carte non recréée à la bascule)."""
    device_key: int
    label: str
    adapter: str = ""
    mac: str = ""
    src_network: str = ""
    tgt_network_urn: str | None = None
    tgt_network_name: str | None = None
    # Recréer la carte avec la MAC source (réservation DHCP, licence liée à
    # la MAC...) plutôt qu'une MAC attribuée par FusionCompute.
    keep_mac: bool = False


class SourceNic(BaseModel):
    device_key: int
    label: str
    adapter: str
    mac: str
    network: str


class PlanVmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    src_vm_moref: str
    src_instance_uuid: str
    src_vm_name: str
    tgt_datastore_urn: str | None = None
    tgt_datastore_name: str | None = None
    tgt_network_urn: str | None = None
    tgt_network_name: str | None = None
    nic_mappings: list[PlanNicMapping] = Field(default_factory=list)


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    source_id: uuid.UUID
    target_id: uuid.UUID | None
    created_at: datetime
    vms: list[PlanVmOut]


class PlanPreflightVm(BaseModel):
    instance_uuid: str
    name: str
    ok: bool
    findings: list[dict]


class PlanPreflightResponse(BaseModel):
    plan_id: uuid.UUID
    vms: list[PlanPreflightVm]


# --------------------------------------------------------------------------
# Cible d'un plan et mapping par VM (étapes 3-5 du flux, 17/09/2026) —
# volontairement séparés de PlanCreate : le plan existe déjà (créé depuis
# l'inventaire source, sans connaître encore la cible) et la cible se
# choisit ensuite, une fois qu'on est connecté à un environnement
# FusionCompute et qu'on a vu ses datastores/réseaux.
# --------------------------------------------------------------------------
class PlanTargetUpdate(BaseModel):
    target_id: uuid.UUID


class PlanVmMappingUpdate(BaseModel):
    datastore_urn: str | None = None
    datastore_name: str | None = None
    network_urn: str | None = None
    network_name: str | None = None
    # Mapping par carte (remplace network_* si fourni).
    nics: list[PlanNicMapping] | None = None

# --------------------------------------------------------------------------
# Relais (VM agent dans le cluster FusionCompute, ARCHITECTURE.md §7.1/§13)
# — simple registre pour l'instant : ce schéma ne prétend pas que l'agent
# réseau existe déjà (voir datamover/fusioncompute.py, note de module).
# --------------------------------------------------------------------------
class RelayCreate(BaseModel):
    target_id: uuid.UUID = Field(description="endpoint FusionCompute où vit ce relais")
    fc_vm_uri: str = Field(description="URI VRM de la VM relais, ex. /service/sites/1/vms/12")
    agent_url: str = Field(description="URL de l'agent réseau tournant sur cette VM "
                                       "(pas encore implémenté — réservé pour plus tard)")
    max_slots: int = 12


class RelayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    target_id: uuid.UUID
    fc_vm_uri: str
    agent_url: str
    max_slots: int
    used_slots: int
    status: str


# --------------------------------------------------------------------------
# Jobs — ARCHITECTURE.md §5 (machine à états) et §8.1. Ajouté le 17/09/2026
# à la demande explicite de l'utilisateur (onglets Migration/Logs), une fois L1
# validé deux fois sur plateforme réelle (docs/dev/JOURNAL.md §5 sexies).
# --------------------------------------------------------------------------
class JobCreate(BaseModel):
    plan_vm_id: uuid.UUID
    relay_id: uuid.UUID | None = Field(
        default=None,
        description="VM relais à utiliser pour le provisioning FusionCompute réel. "
                    "Laisser vide si aucun relais n'est encore déployé : le job reste "
                    "utilisable pour valider seed/delta en écriture locale (§7 non "
                    "encore exercé), voir POST /jobs/{id}/provision.")


class JobDiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    device_key: int
    unit_number: int
    src_path: str
    capacity_bytes: int
    disk_mode: str
    tgt_volume_uri: str | None
    tgt_dev_path: str | None


class SyncRoundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    round_no: int
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    bytes_planned: int | None
    bytes_transferred: int | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    plan_id: uuid.UUID | None
    source_id: uuid.UUID
    target_id: uuid.UUID
    relay_id: uuid.UUID | None
    src_vm_moref: str
    src_vm_name: str
    src_instance_uuid: str
    tgt_vm_uri: str | None
    state: str
    state_since: datetime
    spec: dict
    cbt_reseed_count: int
    delta_requested: bool = False
    delta_scheduled_at: datetime | None = None
    delta_daily_time: str | None = None
    delta_timezone: str | None = None
    created_at: datetime
    disks: list[JobDiskOut] = Field(default_factory=list)
    rounds: list[SyncRoundOut] = Field(default_factory=list)


class JobRunContext(BaseModel):
    """
    Ce qu'il faut à `tools/v2fc_source.py job-run` pour exécuter un round —
    volontairement dépourvu de tout secret (§13). Les identifiants source
    passent par V2FC_VC_USER/V2FC_VC_PASSWORD : fournis par le runner
    (route qui lui est réservée), ou par l'opérateur en lancement manuel.
    """
    job_id: uuid.UUID
    round_id: uuid.UUID
    round_kind: str
    round_no: int
    src_host: str
    src_port: int
    src_verify_tls: bool
    vm_moref: str
    vm_instance_uuid: str
    vm_name: str
    # Volumes FusionCompute attachés au relais (vide sans relais : écriture
    # dans le --target-dir local, mode POC).
    disks: list["RunContextDisk"] = Field(default_factory=list)


class RunContextDisk(BaseModel):
    device_key: int
    tgt_dev_path: str | None
    volume_id: str | None


class RunnerWorkItem(BaseModel):
    job_id: uuid.UUID
    round: str


class RunnerRound(BaseModel):
    job_id: uuid.UUID
    round: str


class RelayMetrics(BaseModel):
    """Charge de la VM relais mesurée par le runner (tools/relay_metrics.py) ;
    débits moyennés entre deux pouls, None au premier pouls."""
    cpu_count: int
    cpu_percent: float | None = None
    mem_total_mb: int
    mem_used_mb: int
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    disk_read_iops: float | None = None
    disk_write_iops: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None


class RunnerHeartbeat(BaseModel):
    running: list[RunnerRound] = Field(default_factory=list)
    metrics: RelayMetrics | None = None


class RunnerStatus(BaseModel):
    online: bool
    last_seen: datetime | None
    running: list[RunnerRound]
    metrics: RelayMetrics | None = None
    relays: list["RelayOut"]


class RunnerRegister(BaseModel):
    # /sys/class/dmi/id/product_uuid de la machine du runner : FusionCompute
    # le présente comme `uuid` de la VM (constaté le 26/10/2026).
    system_uuid: str = Field(min_length=36, max_length=36)


class RunnerSourceCredentials(BaseModel):
    user: str
    password: str


class JobRoundStart(BaseModel):
    kind: str = Field(pattern="^(SEED|DELTA|FINAL)$")


class DiskRoundReport(BaseModel):
    device_key: int
    next_change_id: str | None = None
    capacity_bytes: int | None = None
    bytes_transferred: int | None = None


class JobDeltaSchedule(BaseModel):
    # Heure du delta quotidien « HH:MM » dans `timezone` (fuseau IANA du
    # navigateur) ; null annule la planification.
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Europe/Brussels"


class JobRoundProgress(BaseModel):
    bytes_planned: int = Field(ge=0)
    bytes_done: int = Field(ge=0)


class JobRoundReport(BaseModel):
    status: str = Field(pattern="^(success|failed|cbt_invalidated)$")
    duration_s: float | None = None
    bytes_written: int | None = None
    bytes_skipped: int | None = None
    error: str | None = None
    disks: list[DiskRoundReport] = Field(default_factory=list)


class JobCutoverRequest(BaseModel):
    auto_restart: bool = Field(
        default=False,
        description="redémarrer automatiquement la VM cible sur FusionCompute "
                    "une fois la bascule terminée (étape 8 du flux, §7.5 ⑦)")


class JobLogEntryIn(BaseModel):
    level: str = Field(default="info", pattern="^(debug|info|warning|error)$")
    message: str


class JobLogsIn(BaseModel):
    entries: list[JobLogEntryIn] = Field(min_length=1)


class JobLogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    level: str
    message: str


class JobLogsOut(BaseModel):
    entries: list[JobLogEntryOut]

