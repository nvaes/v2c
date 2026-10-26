import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, JobOut, PlanOut, PlanVmOut, RelayOut,
} from "../api";

// États où un round s'exécute côté Data Mover (job-run, dans le terminal
// de Nicolas — cette UI ne peut pas le lancer elle-même, cf. HANDOFF.md :
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
      `${new Date(last.started_at + "Z").toLocaleTimeString()})`;
  }
  if (job.state === "SEEDING") return "seed en attente du runner (démarrage automatique)";
  if (job.state === "FINAL_DELTA") return "delta final en attente du runner (démarrage automatique)";
  if (job.state === "SYNCED") return "synchronisé — delta automatique périodique";
  return "";
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
  const [autoRestart, setAutoRestart] = useState<Record<string, boolean>>({});
  // Relais choisi par VM de plan avant création du job ("" = aucun relais,
  // écriture locale). Absent = premier relais de la cible du plan.
  const [relayChoice, setRelayChoice] = useState<Record<string, string>>({});

  function refresh() {
    setLoading(true);
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
  // de Nicolas (job-run) ou depuis des actions serveur (provision, cutover),
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

  function chosenRelayId(plan: PlanOut, planVmId: string): string {
    return relayChoice[planVmId] ?? relaysFor(plan)[0]?.id ?? "";
  }

  function relayLabel(relayId: string | null): string {
    if (!relayId) return "aucun (écriture locale)";
    const r = relays.find((x) => x.id === relayId);
    return r ? r.fc_vm_uri : relayId;
  }

  async function createJob(plan: PlanOut, planVmId: string) {
    setBusyKey(planVmId);
    setError(null);
    try {
      const relayId = chosenRelayId(plan, planVmId) || null;
      await api.createJob({ plan_vm_id: planVmId, relay_id: relayId });
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  }

  async function provision(jobId: string) {
    setBusyKey(jobId);
    setError(null);
    try {
      await api.provisionJob(jobId);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  }

  async function cutover(jobId: string) {
    if (!window.confirm("Démarrer la bascule ? La VM source va être éteinte proprement " +
      "pour le dernier delta à froid.")) return;
    setBusyKey(jobId);
    setError(null);
    try {
      await api.cutoverJob(jobId, autoRestart[jobId] ?? false);
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
          Aucune VM relais enregistrée — le provisioning FusionCompute réel (création/attachement
          de volume) sera sauté pour les nouveaux jobs, qui écriront en local (mode validation,
          identique au POC du 17/09/2026). Voir ARCHITECTURE.md §7.1 pour en enregistrer une une
          fois déployée dans FusionCompute.
        </p>
      )}

      {error && <p className="error">{error}</p>}
      {loading && <p className="muted">Chargement…</p>}

      {!loading && plans.filter((p) => p.target_id).length === 0 && (
        <p className="muted">
          Aucun plan avec une cible FusionCompute choisie — commence par la page Plans.
        </p>
      )}

      {plans.filter((p) => p.target_id).map((plan) => (
        <div className="card" key={plan.id} style={{ marginBottom: 16 }}>
          <h2 style={{ marginTop: 0 }}>{plan.name}</h2>
          {plan.vms.map((vm) => {
            const job = jobFor(vm);
            const canCreate = !!vm.tgt_datastore_urn && !!vm.tgt_network_urn;
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

                {!job && (
                  <p>
                    {!canCreate && (
                      <span className="muted">
                        datastore/réseau cible pas encore choisis (page « Cible FusionCompute » du
                        plan) —{" "}
                      </span>
                    )}
                    <label style={{ marginRight: 8 }}>
                      Relais :{" "}
                      <select
                        value={chosenRelayId(plan, vm.id)}
                        onChange={(e) =>
                          setRelayChoice((prev) => ({ ...prev, [vm.id]: e.target.value }))
                        }
                      >
                        {relaysFor(plan).map((r) => (
                          <option key={r.id} value={r.id}>
                            {r.fc_vm_uri} ({r.status})
                          </option>
                        ))}
                        <option value="">aucun (écriture locale)</option>
                      </select>
                    </label>
                    <button disabled={!canCreate || busyKey === vm.id} onClick={() => createJob(plan, vm.id)}>
                      {busyKey === vm.id ? "…" : "Créer un job"}
                    </button>
                  </p>
                )}

                {job && (job.state === "CREATED" || job.state === "VALIDATING") && (
                  <p>
                    <button disabled={busyKey === job.id} onClick={() => provision(job.id)}>
                      {busyKey === job.id ? "…" : "Provisionner"}
                    </button>
                  </p>
                )}

                {job && job.relay_id && autoRoundStatus(job) && (
                  <p className="muted">{autoRoundStatus(job)}</p>
                )}

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

                {job && ["CUTOVER_PENDING", "QUIESCING", "FINAL_DELTA", "FINALIZING", "CUTOVER_DONE"]
                  .includes(job.state) && (
                  <p>
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
