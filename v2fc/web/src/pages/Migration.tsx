import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, JobLogEntryOut, JobOut, PlanOut, PlanVmOut, RelayMetrics, RelayOut, SyncRoundOut,
} from "../api";
import { useRunnerStatus } from "../RunnerBadge";

// États où un round s'exécute côté Data Mover (job-run, lancé par le runner ;
// à la main seulement pour un job sans relais —
// le VDDK tourne dans un conteneur Docker séparé, pas dans l'API).
const ROUND_KIND_FOR_STATE: Record<string, "seed" | "delta" | "final" | undefined> = {
  SEEDING: "seed",
  SYNCED: "delta",
  FINAL_DELTA: "final",
};

const STATE_LABELS: Record<string, string> = {
  CREATED: "créé", VALIDATING: "validation", PROVISIONING: "provisioning en cours",
  SEEDING: "prêt pour le seed", SYNCED: "synchronisé", SYNCING: "resynchronisation en cours",
  PAUSED: "en pause", CUTOVER_PENDING: "bascule demandée", QUIESCING: "extinction en cours",
  FINAL_DELTA: "prêt pour le delta final", FINALIZING: "finalisation en cours",
  CUTOVER_DONE: "bascule terminée", ROLLBACK: "rollback en cours",
  ROLLED_BACK: "revenu en arrière", FAILED: "échec",
};

// Jobs avec relais : rounds exécutés par le service runner (seed après le
// provisioning, delta périodique, delta final après la bascule).
function autoRoundStatus(job: JobOut): string {
  const last = job.rounds[job.rounds.length - 1];
  if (last && last.status === "running") {
    return `round ${last.kind} n°${last.round_no} en cours (automatique, démarré à ` +
      `${new Date(last.started_at + "Z").toLocaleTimeString("fr-FR")})`;
  }
  if (job.state === "SEEDING") return "seed en attente du runner (démarrage automatique)";
  if (job.state === "FINAL_DELTA") return "delta final en attente du runner (démarrage automatique)";
  if (job.state === "SYNCED" && job.delta_requested) return "delta demandé — démarrage sous 15 s";
  if (job.state === "SYNCED") return "synchronisé — delta à lancer à la main ou à planifier";
  return "";
}

function formatDuration(s: number): string {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
}

// Round en cours : avancement rapporté par job-run toutes les 5 s.
function RoundProgress({ job }: { job: JobOut }) {
  const round = job.rounds[job.rounds.length - 1];
  if (!round || round.status !== "running") return null;
  const planned = round.bytes_planned ?? 0;
  const done = round.bytes_transferred ?? 0;
  if (planned <= 0) {
    return <p className="muted">round {round.kind} : analyse des disques…</p>;
  }
  const pct = Math.min(100, (100 * done) / planned);
  const elapsed = (Date.now() - new Date(round.started_at + "Z").getTime()) / 1000;
  const rate = elapsed > 0 ? done / elapsed : 0;
  const eta = rate > 0 ? (planned - done) / rate : 0;
  const gib = (b: number) => (b / 2 ** 30).toFixed(1);
  return (
    <div>
      <div className="progress"><div style={{ width: `${pct}%` }} /></div>
      <p className="muted" style={{ margin: 0 }}>
        round {round.kind} : {pct.toFixed(1)} % — {gib(done)} / {gib(planned)} Gio
        {rate > 0 && <> — {(rate / 2 ** 20).toFixed(0)} Mio/s — reste ~{formatDuration(eta)}</>}
      </p>
    </div>
  );
}

// Horodatages de l'API : UTC sans fuseau.
function utc(s: string): Date {
  return new Date(s + "Z");
}

const ROUND_KIND_LABELS: Record<string, string> = {
  SEED: "seed", DELTA: "delta", FINAL: "delta final",
};
const ROUND_STATUS_LABELS: Record<string, string> = {
  success: "réussi", failed: "échec", cbt_invalidated: "CBT invalidé — nouveau seed",
  running: "en cours",
};

function roundSummary(r: SyncRoundOut): string {
  const end = r.ended_at ? utc(r.ended_at) : null;
  const duration = end ? (end.getTime() - utc(r.started_at).getTime()) / 1000 : 0;
  const size = r.bytes_transferred != null
    ? `${(r.bytes_transferred / 2 ** 20).toLocaleString("fr-FR", { maximumFractionDigits: 0 })} Mio écrits`
    : "";
  return [
    `${ROUND_KIND_LABELS[r.kind] ?? r.kind} n°${r.round_no}`,
    end ? `terminé le ${end.toLocaleString("fr-FR")}` : `démarré le ${utc(r.started_at).toLocaleString("fr-FR")}`,
    end ? (duration >= 60 ? formatDuration(duration) : `${Math.round(duration)} s`) : "",
    size,
    ROUND_STATUS_LABELS[r.status] ?? r.status,
  ].filter(Boolean).join(" — ");
}

// Dernière synchronisation terminée + historique complet des rounds du job.
function RoundHistory({ job }: { job: JobOut }) {
  const done = job.rounds.filter((r) => r.status !== "running");
  if (done.length === 0) return null;
  const last = done[done.length - 1];
  return (
    <div className="muted" style={{ margin: "4px 0" }}>
      Dernière synchronisation : {roundSummary(last)}
      {job.rounds.length > 1 && (
        <details>
          <summary>historique ({job.rounds.length} rounds)</summary>
          <ul style={{ margin: "4px 0", paddingLeft: 18 }}>
            {[...job.rounds].reverse().map((r) => <li key={r.id}>{roundSummary(r)}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}

function mibps(bps: number | null): string {
  return bps == null ? "…" : `${(bps / 2 ** 20).toFixed(1)} Mio/s`;
}

function iops(v: number | null): string {
  return v == null ? "…" : v.toLocaleString("fr-FR", { maximumFractionDigits: 0 });
}

// Cadre « VM relais » (27/10/2026) : charge de la VM où tournent le runner
// et les copies, mesurée par le runner et jointe à son pouls (15 s).
function RelayResources({ metrics }: { metrics: RelayMetrics }) {
  const memPct = metrics.mem_total_mb > 0 ? (100 * metrics.mem_used_mb) / metrics.mem_total_mb : 0;
  const gib = (mb: number) => (mb / 1024).toFixed(1);
  const fsTotal = metrics.fs_total_mb ?? 0;
  const fsPct = fsTotal > 0 ? (100 * (metrics.fs_used_mb ?? 0)) / fsTotal : 0;
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Ressources de la VM relais</h2>
      <div className="resource-grid">
        <div className="resource-tile">
          <div className="muted">CPU ({metrics.cpu_count} vCPU)</div>
          <strong>{metrics.cpu_percent == null ? "…" : `${metrics.cpu_percent.toFixed(0)} %`}</strong>
          <div className="progress"><div style={{ width: `${metrics.cpu_percent ?? 0}%` }} /></div>
        </div>
        <div className="resource-tile">
          <div className="muted">RAM</div>
          <strong>{gib(metrics.mem_used_mb)} / {gib(metrics.mem_total_mb)} Gio</strong>
          <div className="progress"><div style={{ width: `${memPct}%` }} /></div>
        </div>
        <div className="resource-tile">
          <div className="muted">Réseau (copies)</div>
          <strong>↓ {mibps(metrics.net_rx_bps)}</strong>
          <div>↑ {mibps(metrics.net_tx_bps)}</div>
        </div>
        <div className="resource-tile">
          <div className="muted">Disques — IOPS</div>
          <strong>
            {iops(metrics.disk_read_iops)} lect. / {iops(metrics.disk_write_iops)} écr.
          </strong>
          <div className="muted">
            {mibps(metrics.disk_read_bps)} lus — {mibps(metrics.disk_write_bps)} écrits
          </div>
        </div>
        {fsTotal > 0 && (
          <div className="resource-tile">
            {/* partition de /var/lib/docker : images, état du runner, logs */}
            <div className="muted">Espace disque (Docker)</div>
            <strong>
              {gib(metrics.fs_used_mb ?? 0)} / {gib(fsTotal)} Gio ({fsPct.toFixed(0)} %)
            </strong>
            <div className={`progress${fsPct >= 90 ? " progress-full" : ""}`}>
              <div style={{ width: `${fsPct}%` }} />
            </div>
          </div>
        )}
      </div>
      <p className="muted" style={{ margin: "8px 0 0" }}>
        Moyennes sur les 15 dernières secondes, mesurées par le runner.
      </p>
    </div>
  );
}

const VIRTIO_LABELS: Record<string, [string, string]> = {
  ok: ["présents", "ok"],
  inactive: ["présents, pas chargés au démarrage", "blocked"],
  absent: ["absents", "blocked"],
  unknown: ["non déterminés", ""],
};

// Pilotes virtio de l'OS copié, relus par job-run après chaque round réussi
// (datamover/guest_drivers.py) : sans pilote de stockage chargé au démarrage,
// la VM cible doit démarrer sur IDE.
function GuestDrivers({ job }: { job: JobOut }) {
  const gd = job.spec?.guest_drivers as
    | { os: string | null; product: string | null; storage: string; network: string;
        details: string[]; inspected_at: string; conversion?: string }
    | undefined;
  if (!gd) return null;
  const [storage, storageCls] = VIRTIO_LABELS[gd.storage] ?? [gd.storage, ""];
  // réseau aussi bloquant : la carte cible FusionCompute est virtio, sans
  // netkvm/virtio_net la VM démarre sans réseau
  const [network, networkCls] = VIRTIO_LABELS[gd.network] ?? [gd.network, ""];
  return (
    <p className="muted" style={{ margin: "4px 0" }} title={gd.details.join("\n")}>
      Pilotes virtio{gd.product ? ` (${gd.product})` : ""} : stockage{" "}
      <span className={`badge ${storageCls}`}>{storage}</span> — réseau{" "}
      <span className={`badge ${networkCls}`}>{network}</span>
      {" "}— vérifié le {new Date(gd.inspected_at).toLocaleString("fr-FR")}
      {gd.conversion && <><br />Conversion Linux : {gd.conversion}</>}
    </p>
  );
}

// Étapes de la bascule, dans l'ordre, pour le popup de suivi.
const CUTOVER_STEPS: [string, string][] = [
  ["CUTOVER_PENDING", "bascule demandée"],
  ["QUIESCING", "extinction de la VM source"],
  ["FINAL_DELTA", "delta final"],
  ["FINALIZING", "création de la VM cible"],
  ["CUTOVER_DONE", "terminé"],
];

const LOG_LEVEL_CLASS: Record<string, string | undefined> = {
  error: "log-error", warning: "log-warning",
};

// Popup de suivi de la bascule (27/10/2026) : état, avancement du delta
// final et journal du job, relus toutes les 2 s (même source que la page
// Logs). Fermer le popup n'interrompt rien.
function CutoverMonitor({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const [job, setJob] = useState<JobOut | null>(null);
  const [entries, setEntries] = useState<JobLogEntryOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const lastAt = useRef<string | undefined>(undefined);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const [j, logs] = await Promise.all([
          api.getJob(jobId), api.getJobLogs(jobId, lastAt.current),
        ]);
        if (cancelled) return;
        setJob(j);
        if (logs.entries.length > 0) {
          lastAt.current = logs.entries[logs.entries.length - 1].at;
          setEntries((prev) => [...prev, ...logs.entries]);
        }
        setError(null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }
    poll();
    const t = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [jobId]);

  useEffect(() => bottomRef.current?.scrollIntoView({ block: "end" }), [entries.length]);

  const stepIndex = job ? CUTOVER_STEPS.findIndex(([s]) => s === job.state) : -1;
  const finished = job?.state === "CUTOVER_DONE";
  const status = job && job.relay_id ? autoRoundStatus(job) : "";

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h2 style={{ margin: 0 }}>Bascule — {job?.src_vm_name ?? "…"}</h2>
        <ol className="cutover-steps">
          {CUTOVER_STEPS.map(([state, label], i) => (
            <li
              key={state}
              className={
                i < stepIndex || finished ? "done" : i === stepIndex ? "current" : undefined
              }
            >
              {label}
            </li>
          ))}
        </ol>
        {job && stepIndex < 0 && (
          <p className="muted" style={{ margin: "4px 0" }}>
            État : <span className="badge">{STATE_LABELS[job.state] ?? job.state}</span>
          </p>
        )}
        {job?.state === "FINALIZING" && !job.rounds.some((r) => r.status === "running") && (
          <p className="muted" style={{ margin: "4px 0" }}>
            Si rien n'avance, la finalisation a pu s'interrompre : voir le journal ci-dessous.
          </p>
        )}
        {status && <p className="muted" style={{ margin: "4px 0" }}>{status}</p>}
        {job && <RoundProgress job={job} />}
        {error && <p className="error">{error}</p>}
        <div className="modal-log">
          {entries.map((e) => (
            <div key={e.id} className={LOG_LEVEL_CLASS[e.level]}>
              {new Date(e.at).toLocaleTimeString("fr-FR")} [{e.level}] {e.message}
            </div>
          ))}
          {!finished && <div>…</div>}
          <div ref={bottomRef} />
        </div>
        <button style={{ alignSelf: "flex-end" }} onClick={onClose}>
          Fermer
        </button>
      </div>
    </div>
  );
}

function jobRunCommand(job: JobOut, kind: "seed" | "delta" | "final"): string {
  return (
    `V2FC_API_TOKEN=<jeton> docker run --rm --platform linux/amd64 \\\n` +
    `  -v "$PWD/../vmware-vix-disklib-distrib:/opt/vddk:ro" \\\n` +
    `  -v "$PWD/lab-vcenter-out/${job.src_vm_name}:/work/lab-vcenter-out/${job.src_vm_name}" \\\n` +
    `  -e V2FC_API_TOKEN -e V2FC_VC_USER -e V2FC_VC_PASSWORD \\\n` +
    `  v2fc-datamover \\\n` +
    `  python3 tools/v2fc_source.py job-run --api-url http://host.docker.internal:8000 ` +
    `--job-id ${job.id} --round ${kind} ` +
    `--target-dir lab-vcenter-out/${job.src_vm_name} --transport nbdssl --require nbdssl`
  );
}

export default function MigrationPage() {
  const [plans, setPlans] = useState<PlanOut[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [relays, setRelays] = useState<RelayOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const runner = useRunnerStatus();
  const [autoRestart, setAutoRestart] = useState<Record<string, boolean>>({});
  // Heure du delta quotidien en cours de saisie, par plan.
  const [scheduleTime, setScheduleTime] = useState<Record<string, string>>({});
  // Relais choisi par plan pour les jobs à créer ("" = aucun relais,
  // écriture locale). Absent = premier relais de la cible du plan.
  const [relayChoice, setRelayChoice] = useState<Record<string, string>>({});
  // Action globale en cours sur un plan (premier seed, planification).
  const [planProgress, setPlanProgress] = useState<Record<string, string>>({});
  // Job dont la bascule est suivie dans le popup.
  const [monitorJobId, setMonitorJobId] = useState<string | null>(null);

  // Rafraîchissement silencieux (27/10/2026) : `loading` ne vaut true qu'au
  // premier chargement — afficher « Chargement… » toutes les 5 s décalait
  // les blocs de plan vers le bas puis les remontait.
  function refresh() {
    setError(null);
    Promise.all([api.listPlans(), api.listJobs(), api.listRelays()])
      .then(([p, j, r]) => {
        setPlans(p);
        setJobs(j);
        setRelays(r);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);
  // Rafraîchit périodiquement : l'état d'un job avance depuis le terminal
  // de l'utilisateur (job-run) ou depuis des actions serveur (provision, cutover),
  // pas seulement depuis cette page.
  useEffect(() => {
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  function jobFor(vm: PlanVmOut): JobOut | undefined {
    return jobs.find((j) => j.src_instance_uuid === vm.src_instance_uuid);
  }

  function relaysFor(plan: PlanOut): RelayOut[] {
    return relays.filter((r) => r.target_id === plan.target_id);
  }

  function chosenRelayId(plan: PlanOut): string {
    return relayChoice[plan.id] ?? relaysFor(plan)[0]?.id ?? "";
  }

  function relayLabel(relayId: string | null): string {
    if (!relayId) return "aucun (écriture locale)";
    const r = relays.find((x) => x.id === relayId);
    return r ? r.fc_vm_uri : relayId;
  }

  function canCreate(vm: PlanVmOut): boolean {
    return !!vm.tgt_datastore_urn && !!vm.tgt_network_urn;
  }

  // VM du plan que « Lancer le premier seed » traite : sans job (et mapping
  // complet), ou job pas encore provisionné (création ou provisioning
  // précédent en échec).
  function seedCandidates(plan: PlanOut): PlanVmOut[] {
    return plan.vms.filter((vm) => {
      const job = jobFor(vm);
      return job ? job.state === "CREATED" || job.state === "VALIDATING" : canCreate(vm);
    });
  }

  function planRelayJobs(plan: PlanOut): JobOut[] {
    return plan.vms.map(jobFor).filter((j): j is JobOut => !!j && !!j.relay_id);
  }

  // Heure commune aux jobs du plan (ou "" si aucune ou différentes).
  function planDailyTime(plan: PlanOut): string {
    const times = new Set(planRelayJobs(plan).map((j) => j.delta_daily_time ?? ""));
    return times.size === 1 ? [...times][0] : "";
  }

  // Exécute `step` VM par VM (le provisioning parle au VRM : en série), en
  // continuant après un échec ; les erreurs sont regroupées par VM.
  async function runForPlan<T>(
    plan: PlanOut, items: T[], label: (t: T) => string, step: (t: T) => Promise<unknown>,
  ) {
    setBusyKey(plan.id);
    setError(null);
    const failures: string[] = [];
    for (const [i, item] of items.entries()) {
      setPlanProgress((p) => ({ ...p, [plan.id]: `${label(item)} (${i + 1}/${items.length})…` }));
      try {
        await step(item);
      } catch (err) {
        failures.push(`${label(item)} : ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    setPlanProgress((p) => {
      const next = { ...p };
      delete next[plan.id];
      return next;
    });
    if (failures.length > 0) setError(failures.join(" ; "));
    setBusyKey(null);
    refresh();
  }

  // Premier seed du plan : crée les jobs manquants puis provisionne ; le
  // runner démarre ensuite la copie initiale de chaque job avec relais.
  function startPlanSeed(plan: PlanOut) {
    const relayId = chosenRelayId(plan) || null;
    runForPlan(plan, seedCandidates(plan), (vm) => vm.src_vm_name, async (vm) => {
      const job = jobFor(vm) ?? await api.createJob({ plan_vm_id: vm.id, relay_id: relayId });
      await api.provisionJob(job.id);
    });
  }

  // Delta quotidien appliqué à tous les jobs avec relais du plan (le runner
  // ne le lance que sur les jobs synchronisés).
  function savePlanSchedule(plan: PlanOut, time: string | null) {
    runForPlan(plan, planRelayJobs(plan), (j) => j.src_vm_name, (j) => api.setDeltaSchedule(j.id, time))
      .then(() => setScheduleTime((p) => {
        const next = { ...p };
        delete next[plan.id];
        return next;
      }));
  }

  async function cutover(jobId: string) {
    if (!window.confirm("Démarrer la bascule ? La VM source va être éteinte proprement " +
      "pour le dernier delta à froid.")) return;
    setBusyKey(jobId);
    setError(null);
    try {
      await api.cutoverJob(jobId, autoRestart[jobId] ?? false);
      setMonitorJobId(jobId);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  }

  async function runAction(jobId: string, action: () => Promise<unknown>) {
    setBusyKey(jobId);
    setError(null);
    try {
      await action();
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  }

  async function retryFinalize(jobId: string) {
    setBusyKey(jobId);
    setError(null);
    try {
      await api.finalizeJob(jobId);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  }

  async function rollback(jobId: string) {
    if (!window.confirm("Annuler la bascule et rallumer la VM source ?")) return;
    setBusyKey(jobId);
    setError(null);
    try {
      await api.rollbackJob(jobId);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <>
      <h1>Migration</h1>
      <p className="muted">
        Pilote les étapes 6 à 8 du flux de migration : copie en ligne (seed), resynchronisations
        incrémentales (delta) et bascule finale (« migrate now »). Avec un relais, les rounds
        s'exécutent automatiquement (service runner) ; sans relais (écriture locale), la page
        affiche la commande à lancer à la main.
      </p>
      {relays.length === 0 && (
        <p className="muted">
          Aucune VM relais enregistrée. Le service runner enregistre automatiquement la VM sur
          laquelle il tourne dès qu'il la trouve dans un environnement FusionCompute (vérification
          toutes les 10 min). Sans relais, un job écrit en local (mode validation).
        </p>
      )}

      {runner && !runner.online && (
        <p className="error">
          Le service runner est hors ligne
          {runner.last_seen ? ` depuis ${new Date(runner.last_seen).toLocaleString("fr-FR")}` : ""} : aucune
          copie (seed, delta, delta final) ne peut démarrer. Sur le serveur :{" "}
          <code>docker compose up -d runner</code>, puis <code>docker compose logs runner</code>.
        </p>
      )}
      {runner?.online && runner.metrics && <RelayResources metrics={runner.metrics} />}
      {error && <p className="error">{error}</p>}
      {monitorJobId && (
        <CutoverMonitor jobId={monitorJobId} onClose={() => setMonitorJobId(null)} />
      )}

      {!loading && plans.filter((p) => p.target_id).length === 0 && (
        <p className="muted">
          Aucun plan avec une cible FusionCompute choisie — commence par la page Plans.
        </p>
      )}

      {plans.filter((p) => p.target_id).map((plan) => (
        <div className="card" key={plan.id} style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>{plan.name}</h2>
          {seedCandidates(plan).length > 0 && (
            <p>
              <label style={{ marginRight: 8 }}>
                Relais :{" "}
                <select
                  value={chosenRelayId(plan)}
                  onChange={(e) => setRelayChoice((prev) => ({ ...prev, [plan.id]: e.target.value }))}
                >
                  {relaysFor(plan).map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.fc_vm_uri} ({r.status})
                    </option>
                  ))}
                  <option value="">aucun (écriture locale)</option>
                </select>
              </label>
              <button disabled={busyKey === plan.id} onClick={() => startPlanSeed(plan)}>
                Lancer le premier seed ({seedCandidates(plan).length} VM)
              </button>
            </p>
          )}
          {planRelayJobs(plan).length > 0 && (
            <div style={{ margin: "8px 0" }}>
              Delta chaque jour à{" "}
              <input
                type="time"
                value={scheduleTime[plan.id] ?? planDailyTime(plan)}
                onChange={(e) => setScheduleTime((p) => ({ ...p, [plan.id]: e.target.value }))}
              />{" "}
              <button
                className="secondary"
                disabled={busyKey === plan.id || !(scheduleTime[plan.id] ?? planDailyTime(plan))}
                onClick={() => savePlanSchedule(plan, scheduleTime[plan.id] ?? planDailyTime(plan))}
              >
                Planifier pour le plan
              </button>
              {planRelayJobs(plan).some((j) => j.delta_daily_time) && (
                <button
                  className="secondary"
                  style={{ marginLeft: 4 }}
                  disabled={busyKey === plan.id}
                  onClick={() => savePlanSchedule(plan, null)}
                >
                  Annuler
                </button>
              )}
              {!planDailyTime(plan) && planRelayJobs(plan).some((j) => j.delta_daily_time) && (
                <span className="muted"> heures différentes selon les VM</span>
              )}
            </div>
          )}
          {planProgress[plan.id] && <p className="muted">{planProgress[plan.id]}</p>}
          {plan.vms.map((vm) => {
            const job = jobFor(vm);
            return (
              <div key={vm.id} style={{ borderTop: "1px solid #333", paddingTop: 12, marginTop: 12 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
                  <strong>{vm.src_vm_name}</strong>
                  {job ? (
                    <span className="badge">{STATE_LABELS[job.state] ?? job.state}</span>
                  ) : (
                    <span className="muted">aucun job</span>
                  )}
                  {job && (
                    <Link to={`/jobs/${job.id}/logs`} style={{ marginLeft: "auto" }}>
                      Logs
                    </Link>
                  )}
                </div>

                {job && (
                  <p className="muted" style={{ margin: "4px 0" }}>
                    Relais : {relayLabel(job.relay_id)}
                  </p>
                )}

                {!job && !canCreate(vm) && (
                  <p className="muted">
                    datastore/réseau cible pas encore choisis (page « Cible FusionCompute » du plan)
                  </p>
                )}

                {job && job.relay_id && autoRoundStatus(job) && (
                  <p className="muted">{autoRoundStatus(job)}</p>
                )}

                {job && <RoundProgress job={job} />}
                {job && <RoundHistory job={job} />}
                {job && <GuestDrivers job={job} />}

                {job && !job.relay_id && ROUND_KIND_FOR_STATE[job.state] && (
                  <div>
                    <p className="muted">
                      Lancer depuis le terminal (Docker + VDDK, comme pour la validation L1) :
                    </p>
                    <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>
                      {jobRunCommand(job, ROUND_KIND_FOR_STATE[job.state]!)}
                    </pre>
                  </div>
                )}

                {job && job.relay_id && job.state === "SYNCED" && (
                  <div style={{ margin: "8px 0" }}>
                    <button
                      className="secondary"
                      disabled={busyKey === job.id || job.delta_requested}
                      onClick={() => runAction(job.id, () => api.requestDelta(job.id))}
                    >
                      Lancer un delta maintenant
                    </button>
                    {job.delta_scheduled_at && (
                      <p className="muted" style={{ margin: "4px 0 0" }}>
                        Prochain delta : {utc(job.delta_scheduled_at).toLocaleString("fr-FR")}
                      </p>
                    )}
                  </div>
                )}

                {job && job.state === "SYNCED" && (
                  <p>
                    <label style={{ marginRight: 8 }}>
                      <input
                        type="checkbox"
                        checked={autoRestart[job.id] ?? false}
                        onChange={(e) =>
                          setAutoRestart((prev) => ({ ...prev, [job.id]: e.target.checked }))
                        }
                      />{" "}
                      redémarrer automatiquement la VM cible après bascule
                    </label>
                    <button disabled={busyKey === job.id} onClick={() => cutover(job.id)}>
                      {busyKey === job.id ? "…" : "Migrer maintenant"}
                    </button>
                  </p>
                )}

                {job && job.state === "FINALIZING" && (
                  <p>
                    <span className="muted">
                      Finalisation interrompue — voir l'erreur dans les Logs.{" "}
                    </span>
                    <button disabled={busyKey === job.id} onClick={() => retryFinalize(job.id)}>
                      {busyKey === job.id ? "…" : "Relancer la finalisation"}
                    </button>
                  </p>
                )}

                {job && ["CUTOVER_PENDING", "QUIESCING", "FINAL_DELTA", "FINALIZING", "CUTOVER_DONE"]
                  .includes(job.state) && (
                  <p>
                    <button
                      className="secondary"
                      style={{ marginRight: 8 }}
                      onClick={() => setMonitorJobId(job.id)}
                    >
                      {job.state === "CUTOVER_DONE" ? "Détail de la bascule" : "Suivre la bascule"}
                    </button>
                    <button className="secondary" disabled={busyKey === job.id} onClick={() => rollback(job.id)}>
                      {busyKey === job.id ? "…" : "Annuler / rollback"}
                    </button>
                  </p>
                )}

                {job && job.cbt_reseed_count > 0 && (
                  <p className="muted">
                    CBT invalidé {job.cbt_reseed_count} fois — un nouveau seed complet a été
                    nécessaire.
                  </p>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </>
  );
}
