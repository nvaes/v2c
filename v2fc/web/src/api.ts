/**
 * Client HTTP minimal — jette une erreur lisible sur tout statut >= 400.
 * Le jeton est gardé en mémoire de module + sessionStorage (pour survivre
 * à un rechargement de page), jamais renvoyé nulle part ailleurs qu'ici.
 */
const TOKEN_KEY = "v2fc_token";

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !(options.body instanceof URLSearchParams)) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`/api/v1${path}`, { ...options, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* corps non JSON, on garde statusText */
    }
    throw new Error(`${res.status} : ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  async login(email: string, password: string): Promise<string> {
    const body = new URLSearchParams({ username: email, password });
    const data = await request<{ access_token: string }>("/auth/login", {
      method: "POST",
      body,
    });
    return data.access_token;
  },
  me: () => request<{ email: string; role: string }>("/auth/me"),

  listEndpoints: () => request<EndpointOut[]>("/endpoints"),
  createEndpoint: (body: EndpointCreate) =>
    request<EndpointOut>("/endpoints", { method: "POST", body: JSON.stringify(body) }),
  probeEndpoint: (id: string) =>
    request<ProbeResult>(`/endpoints/${id}/probe`, { method: "POST" }),
  deleteEndpoint: (id: string) =>
    request<void>(`/endpoints/${id}`, { method: "DELETE" }),
  getInventory: (id: string, refresh = false) =>
    request<InventoryResponse>(`/endpoints/${id}/inventory${refresh ? "?refresh=true" : ""}`),
  getInventoryProgress: (id: string) =>
    request<InventoryProgress>(`/endpoints/${id}/inventory/progress`),
  getFcDatastores: (id: string, refresh = false) =>
    request<FcDatastoresResponse>(`/endpoints/${id}/datastores${refresh ? "?refresh=true" : ""}`),
  getFcNetworks: (id: string, refresh = false) =>
    request<FcNetworksResponse>(`/endpoints/${id}/networks${refresh ? "?refresh=true" : ""}`),

  listPlans: () => request<PlanOut[]>("/plans"),
  getPlan: (id: string) => request<PlanOut>(`/plans/${id}`),
  // withJobs : supprime aussi les jobs (et les volumes FusionCompute des
  // migrations non terminées).
  deletePlan: (id: string, withJobs = false) =>
    request<void>(`/plans/${id}${withJobs ? "?with_jobs=true" : ""}`, { method: "DELETE" }),
  createPlan: (body: PlanCreate) =>
    request<PlanOut>("/plans", { method: "POST", body: JSON.stringify(body) }),
  planPreflight: (id: string) =>
    request<PlanPreflightResponse>(`/plans/${id}/preflight`),
  setPlanTarget: (planId: string, targetId: string) =>
    request<PlanOut>(`/plans/${planId}/target`, {
      method: "PUT",
      body: JSON.stringify({ target_id: targetId }),
    }),
  getPlanSourceNics: (planId: string) =>
    request<Record<string, SourceNic[]>>(`/plans/${planId}/source-nics`),
  setPlanVmMapping: (planId: string, planVmId: string, body: PlanVmMappingUpdate) =>
    request<PlanOut>(`/plans/${planId}/vms/${planVmId}/mapping`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // -- relais (VM agent FusionCompute, §7.1/§13) --------------------------
  listRelays: () => request<RelayOut[]>("/relays"),
  createRelay: (body: RelayCreate) =>
    request<RelayOut>("/relays", { method: "POST", body: JSON.stringify(body) }),
  deleteRelay: (id: string) => request<void>(`/relays/${id}`, { method: "DELETE" }),

  // -- jobs de migration (onglets Migration/Logs, §5, 17/09/2026) ---------
  listJobs: () => request<JobOut[]>("/jobs"),
  getJob: (id: string) => request<JobOut>(`/jobs/${id}`),
  createJob: (body: JobCreate) =>
    request<JobOut>("/jobs", { method: "POST", body: JSON.stringify(body) }),
  deleteJob: (id: string) => request<void>(`/jobs/${id}`, { method: "DELETE" }),
  provisionJob: (id: string) => request<JobOut>(`/jobs/${id}/provision`, { method: "POST" }),
  cutoverJob: (id: string, autoRestart: boolean) =>
    request<JobOut>(`/jobs/${id}/cutover`, {
      method: "POST",
      body: JSON.stringify({ auto_restart: autoRestart }),
    }),
  rollbackJob: (id: string) => request<JobOut>(`/jobs/${id}/rollback`, { method: "POST" }),
  finalizeJob: (id: string) => request<JobOut>(`/jobs/${id}/finalize`, { method: "POST" }),
  requestDelta: (id: string) => request<JobOut>(`/jobs/${id}/delta`, { method: "POST" }),
  // time « HH:MM » chaque jour, heure locale du navigateur (null = annuler)
  setDeltaSchedule: (id: string, time: string | null) =>
    request<JobOut>(`/jobs/${id}/delta-schedule`, {
      method: "PUT",
      body: JSON.stringify({ time, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }),
    }),
  getJobLogs: (id: string, since?: string) =>
    request<JobLogsOut>(`/jobs/${id}/logs${since ? `?since=${encodeURIComponent(since)}` : ""}`),
};

// -- types miroir de api/schemas.py ---------------------------------------
export interface EndpointOut {
  id: string; kind: string; name: string; url: string; port: number; verify_tls: boolean;
  last_probe_at: string | null; last_probe_ok: boolean | null;
}
export interface EndpointCreate {
  kind: "vcenter" | "fusioncompute"; name: string; url: string; port: number;
  username: string; password: string; verify_tls: boolean;
}
export interface ProbeResult {
  ok: boolean; message: string; platform: string | null; tls_fingerprint_sha1: string | null;
}
export interface InventoryVm {
  moref: string; instance_uuid: string; name: string; power_state: string;
  guest_id: string; firmware: string; num_disks: number; disk_capacity_gb: number;
  cbt_enabled: boolean;
  migratable: boolean; preflight_findings: string[];
}
export interface InventoryCluster { name: string; moref: string; hosts: string[]; }
export interface InventoryDatacenter { name: string; moref: string; clusters: InventoryCluster[]; vms: InventoryVm[]; }
export interface InventoryResponse { datacenters: InventoryDatacenter[]; vm_count: number; }
export interface InventoryProgress { phase: "idle" | "connecting" | "preflight" | "done"; done: number; total: number; }

export interface PlanVmIn { moref: string; instance_uuid: string; name: string; }
export interface PlanCreate { name: string; source_id: string; target_id?: string | null; vms: PlanVmIn[]; }
export interface PlanVmOut {
  id: string; src_vm_moref: string; src_instance_uuid: string; src_vm_name: string;
  tgt_datastore_urn: string | null; tgt_datastore_name: string | null;
  tgt_network_urn: string | null; tgt_network_name: string | null;
  nic_mappings: PlanNicMapping[];
}
// Une carte réseau source et son réseau FusionCompute (null : non recréée).
export interface PlanNicMapping {
  device_key: number; label: string; adapter: string; mac: string; src_network: string;
  tgt_network_urn: string | null; tgt_network_name: string | null;
  keep_mac: boolean;
}
export interface SourceNic {
  device_key: number; label: string; adapter: string; mac: string; network: string;
}
export interface PlanOut {
  id: string; name: string; source_id: string; target_id: string | null;
  created_at: string; vms: PlanVmOut[];
}
export interface PlanPreflightVm { instance_uuid: string; name: string; ok: boolean; findings: unknown[]; }
export interface PlanPreflightResponse { plan_id: string; vms: PlanPreflightVm[]; }

// -- ressources FusionCompute (cible — ARCHITECTURE.md §7.5) --------------
export interface FcDatastore {
  urn: string; name: string; status: string | null; storage_type: string | null;
  actual_capacity_gb: number | null; actual_free_gb: number | null;
}
export interface FcDatastoresResponse { datastores: FcDatastore[]; }

export interface FcNetwork {
  urn: string; name: string; dvswitch_urn: string; dvswitch_name: string;
  port_type: number | null; vlan_id: number | null; vlan_range: string | null;
}
export interface FcNetworksResponse { networks: FcNetwork[]; }

export interface PlanVmMappingUpdate {
  datastore_urn?: string | null; datastore_name?: string | null;
  network_urn?: string | null; network_name?: string | null;
  nics?: PlanNicMapping[];
}

// -- relais et jobs (ARCHITECTURE.md §5/§7.1, ajoutés le 17/09/2026) ------
export interface RelayCreate {
  target_id: string; fc_vm_uri: string; agent_url: string; max_slots: number;
}
export interface RelayOut {
  id: string; target_id: string; fc_vm_uri: string; agent_url: string;
  max_slots: number; used_slots: number; status: string;
}

export interface JobCreate { plan_vm_id: string; relay_id?: string | null; }

export interface JobDiskOut {
  id: string; device_key: number; unit_number: number; src_path: string;
  capacity_bytes: number; disk_mode: string;
  tgt_volume_uri: string | null; tgt_dev_path: string | null;
}
export interface SyncRoundOut {
  id: string; round_no: number; kind: "SEED" | "DELTA" | "FINAL";
  status: string; started_at: string; ended_at: string | null;
  bytes_planned: number | null; bytes_transferred: number | null;
}
export type JobState =
  | "CREATED" | "VALIDATING" | "PROVISIONING" | "SEEDING" | "SYNCED" | "SYNCING"
  | "PAUSED" | "CUTOVER_PENDING" | "QUIESCING" | "FINAL_DELTA" | "FINALIZING"
  | "CUTOVER_DONE" | "ROLLBACK" | "ROLLED_BACK" | "FAILED";

export interface JobOut {
  id: string; plan_id: string | null; source_id: string; target_id: string;
  relay_id: string | null; src_vm_moref: string; src_vm_name: string;
  src_instance_uuid: string; tgt_vm_uri: string | null; state: JobState;
  state_since: string; spec: Record<string, unknown>; cbt_reseed_count: number;
  delta_requested: boolean; delta_scheduled_at: string | null;
  delta_daily_time: string | null; delta_timezone: string | null;
  created_at: string; disks: JobDiskOut[]; rounds: SyncRoundOut[];
}

export interface JobLogEntryOut { id: string; at: string; level: string; message: string; }
export interface JobLogsOut { entries: JobLogEntryOut[]; }
