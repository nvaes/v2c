import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, EndpointOut, PlanOut, PlanPreflightResponse } from "../api";

export default function PlansPage() {
  const navigate = useNavigate();
  const [plans, setPlans] = useState<PlanOut[]>([]);
  const [endpoints, setEndpoints] = useState<Record<string, EndpointOut>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [preflights, setPreflights] = useState<Record<string, PlanPreflightResponse>>({});
  const [preflightBusy, setPreflightBusy] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    setError(null);
    Promise.all([api.listPlans(), api.listEndpoints()])
      .then(([p, eps]) => {
        setPlans(p);
        setEndpoints(Object.fromEntries(eps.map((e) => [e.id, e])));
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function runPreflight(planId: string) {
    setPreflightBusy(planId);
    setError(null);
    try {
      const result = await api.planPreflight(planId);
      setPreflights((prev) => ({ ...prev, [planId]: result }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPreflightBusy(null);
    }
  }

  async function removePlan(plan: PlanOut) {
    if (!window.confirm(`Supprimer le plan « ${plan.name} » (${plan.vms.length} VM) ?`)) return;
    setDeletingId(plan.id);
    setError(null);
    try {
      await api.deletePlan(plan.id);
      setPlans((prev) => prev.filter((p) => p.id !== plan.id));
    } catch (err) {
      // 409 attendu si un job référence encore ce plan — le message du
      // backend l'explique déjà, pas la peine de le reformuler.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <>
      <h1>Plans de migration</h1>
      <p className="muted">
        Un plan fige une liste de VM sélectionnées depuis un environnement source. La
        pré-vérification (§ preflight) n'est pas rejouée automatiquement : les VM peuvent avoir
        changé d'état depuis la création du plan (CBT désactivé, VM éteinte, etc.), donc on la
        relance à la demande, par plan.
      </p>

      {error && <p className="error">{error}</p>}
      {loading && <p className="muted">Chargement…</p>}

      {!loading && plans.length === 0 && (
        <p className="muted">
          Aucun plan pour l'instant — créez-en un depuis la page « Voir les VM » d'un
          environnement, en sélectionnant les VM à migrer.
        </p>
      )}

      {plans.map((plan) => {
        const source = endpoints[plan.source_id];
        const preflight = preflights[plan.id];
        const findingsByVm = Object.fromEntries(
          (preflight?.vms ?? []).map((v) => [v.instance_uuid, v]),
        );
        return (
          <div className="card" key={plan.id} style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
              <h2 style={{ margin: 0 }}>{plan.name}</h2>
              <span className="muted">
                {source ? source.name : plan.source_id} — créé le{" "}
                {new Date(plan.created_at).toLocaleString()}
              </span>
              <button
                className="secondary"
                style={{ marginLeft: "auto" }}
                disabled={preflightBusy === plan.id}
                onClick={() => runPreflight(plan.id)}
              >
                {preflightBusy === plan.id ? "Vérification…" : "Rafraîchir la pré-vérification"}
              </button>
              <button onClick={() => navigate(`/plans/${plan.id}/target`)}>
                {plan.target_id ? "Cible FusionCompute" : "Choisir une cible FusionCompute"}
              </button>
              <button
                className="secondary"
                disabled={deletingId === plan.id}
                onClick={() => removePlan(plan)}
              >
                {deletingId === plan.id ? "…" : "Supprimer"}
              </button>
            </div>

            <table>
              <thead>
                <tr>
                  <th>VM</th>
                  <th>moRef</th>
                  <th>État</th>
                </tr>
              </thead>
              <tbody>
                {plan.vms.map((vm) => {
                  const found = findingsByVm[vm.src_instance_uuid];
                  return (
                    <tr key={vm.id}>
                      <td>{vm.src_vm_name}</td>
                      <td>
                        <code>{vm.src_vm_moref}</code>
                      </td>
                      <td>
                        {!preflight && <span className="muted">non vérifié depuis la création</span>}
                        {found && (
                          <>
                            <span className={`badge ${found.ok ? "ok" : "blocked"}`}>
                              {found.ok ? "migratable" : "bloqué"}
                            </span>
                            {!found.ok && found.findings.length > 0 && (
                              <div className="muted">
                                {found.findings.map((f, i) => (
                                  <div key={i}>{JSON.stringify(f)}</div>
                                ))}
                              </div>
                            )}
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {plan.vms.length === 0 && (
                  <tr>
                    <td colSpan={3} className="muted">
                      Ce plan ne contient aucune VM.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        );
      })}
    </>
  );
}
