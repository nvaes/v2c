import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, PlanReport } from "../api";

const STATE_LABELS: Record<string, string> = {
  CREATED: "créé", VALIDATING: "validation", PROVISIONING: "provisioning",
  SEEDING: "copie initiale", SYNCED: "synchronisée", SYNCING: "synchronisation",
  CUTOVER_PENDING: "bascule", QUIESCING: "bascule", FINAL_DELTA: "bascule",
  FINALIZING: "finalisation", CUTOVER_DONE: "migrée", ROLLBACK: "retour arrière",
  ROLLED_BACK: "revenue sur VMware", FAILED: "échec",
};
const KIND_LABELS: Record<string, string> = { SEED: "Copie initiale", DELTA: "Delta", FINAL: "Delta final" };
const STATUS_LABELS: Record<string, string> = {
  success: "réussi", failed: "échec", cbt_invalidated: "CBT invalidé", running: "en cours",
};

// Horodatages de l'API : UTC sans fuseau, sauf generated_at (avec fuseau).
const when = (s: string | null) =>
  s ? new Date(/[Z+]/.test(s.slice(19)) ? s : s + "Z").toLocaleString("fr-FR") : "—";
const gib = (b: number | null) => (b == null ? "—" : `${(b / 2 ** 30).toFixed(1)} Gio`);
function duration(a: string, b: string | null): string {
  if (!b) return "—";
  const s = (new Date(b + "Z").getTime() - new Date(a + "Z").getTime()) / 1000;
  return s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min`
    : `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}`;
}

/** Rapport de migration d'un plan, mis en page pour l'impression / PDF. */
export default function PlanReportPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [report, setReport] = useState<PlanReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (id) api.planReport(id).then(setReport).catch((e) => setError(String(e)));
  }, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!report) return <p className="muted">Chargement…</p>;
  const { plan, summary } = report;

  return (
    <div className="report">
      <div className="no-print" style={{ marginBottom: 16 }}>
        <button className="secondary" onClick={() => navigate("/plans")}>← Plans</button>{" "}
        <button onClick={() => window.print()}>Imprimer / enregistrer en PDF</button>
      </div>

      <h1>Rapport de migration — {plan.name}</h1>
      <table className="kv">
        <tbody>
          <tr><th>Source (VMware)</th><td>{report.source ? `${report.source.name} (${report.source.url})` : "—"}</td></tr>
          <tr><th>Cible (FusionCompute)</th><td>{report.target ? `${report.target.name} (${report.target.url})` : "—"}</td></tr>
          <tr><th>Plan créé</th><td>{when(plan.created_at)}{plan.created_by ? ` par ${plan.created_by}` : ""}</td></tr>
          <tr><th>Rapport généré</th><td>{when(report.generated_at)} par {report.generated_by}</td></tr>
          <tr>
            <th>Synthèse</th>
            <td>
              {summary.vms} VM : {summary.migrated} migrée(s), {summary.in_progress} en cours,{" "}
              {summary.not_started} non démarrée(s)
            </td>
          </tr>
        </tbody>
      </table>

      {report.vms.map((vm) => (
        <section key={vm.instance_uuid} className="card report-vm">
          <h2 style={{ marginTop: 0 }}>
            {vm.name}{" "}
            <span className="badge">{vm.job ? STATE_LABELS[vm.job.state] ?? vm.job.state : "non démarrée"}</span>
          </h2>
          <table className="kv">
            <tbody>
              <tr><th>Datastore cible</th><td>{vm.datastore ?? "—"}</td></tr>
              <tr>
                <th>Réseaux</th>
                <td>
                  {vm.nics.length === 0 ? "—" : vm.nics.map((n, i) => (
                    <div key={i}>
                      {n.label}{n.src_network ? ` (${n.src_network})` : ""} → {n.tgt_network_name ?? "non recréée"}
                      {n.keep_mac && n.mac ? `, MAC ${n.mac} conservée` : ""}
                    </div>
                  ))}
                </td>
              </tr>
              {vm.job && (
                <>
                  <tr><th>Job créé</th><td>{when(vm.job.created_at)}</td></tr>
                  <tr>
                    <th>Disques</th>
                    <td>{vm.job.disks.map((d) => gib(d.capacity_bytes)).join(", ") || "—"}</td>
                  </tr>
                  {vm.job.tgt_vm_uri && <tr><th>VM FusionCompute</th><td>{vm.job.tgt_vm_uri}</td></tr>}
                  {vm.job.cbt_reseed_count > 0 && (
                    <tr><th>Copies complètes refaites (CBT)</th><td>{vm.job.cbt_reseed_count}</td></tr>
                  )}
                </>
              )}
            </tbody>
          </table>

          {vm.job && vm.job.rounds.length > 0 && (
            <>
              <h3>Synchronisations</h3>
              <table>
                <thead>
                  <tr><th>Type</th><th>Début</th><th>Fin</th><th>Durée</th><th>Écrit</th><th>Résultat</th></tr>
                </thead>
                <tbody>
                  {vm.job.rounds.map((r) => (
                    <tr key={`${r.kind}-${r.no}-${r.started_at}`}>
                      <td>{KIND_LABELS[r.kind] ?? r.kind}</td>
                      <td>{when(r.started_at)}</td>
                      <td>{when(r.ended_at)}</td>
                      <td>{duration(r.started_at, r.ended_at)}</td>
                      <td>{gib(r.bytes)}</td>
                      <td>{STATUS_LABELS[r.status] ?? r.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {vm.job && vm.job.events.length > 0 && (
            <>
              <h3>Événements clés{vm.job.errors ? ` (${vm.job.errors} erreur(s) au total)` : ""}</h3>
              <ul className="report-events">
                {vm.job.events.map((e, i) => (
                  <li key={i} className={e.level === "error" ? "log-error" : e.level === "warning" ? "log-warning" : ""}>
                    {when(e.at)} — {e.message}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      ))}
    </div>
  );
}
