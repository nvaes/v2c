import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  EndpointOut,
  FcDatastore,
  FcNetwork,
  PlanOut,
} from "../api";

/**
 * Étapes 3-5 du flux voulu par Nicolas (17/09/2026) : choisir un
 * environnement FusionCompute cible pour le plan, puis, pour chaque VM du
 * plan, choisir le datastore et le réseau de destination.
 *
 * Volontairement séparée de Plans.tsx : la sélection de VM (Inventory.tsx)
 * et le mapping cible sont deux moments différents du flux, avec des
 * données différentes (l'un lit un vCenter/ESXi, l'autre un VRM
 * FusionCompute) — les mélanger dans un seul écran aurait rendu les deux
 * plus confus.
 */
export default function PlanTargetPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [endpoints, setEndpoints] = useState<EndpointOut[]>([]);
  const [datastores, setDatastores] = useState<FcDatastore[]>([]);
  const [networks, setNetworks] = useState<FcNetwork[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [resourcesLoading, setResourcesLoading] = useState(false);
  const [selectedTarget, setSelectedTarget] = useState("");
  const [settingTarget, setSettingTarget] = useState(false);
  const [savingVm, setSavingVm] = useState<string | null>(null);
  const [savedVm, setSavedVm] = useState<string | null>(null);
  const [pendingMapping, setPendingMapping] = useState<
    Record<string, { datastoreUrn: string; networkUrn: string }>
  >({});

  const fcEndpoints = useMemo(
    () => endpoints.filter((e) => e.kind === "fusioncompute"),
    [endpoints],
  );

  function loadPlanAndEndpoints() {
    if (!id) return;
    setLoading(true);
    setError(null);
    Promise.all([api.getPlan(id), api.listEndpoints()])
      .then(([p, eps]) => {
        setPlan(p);
        setEndpoints(eps);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(loadPlanAndEndpoints, [id]);

  function loadResources(targetId: string, refresh = false) {
    setResourcesLoading(true);
    setError(null);
    Promise.all([
      api.getFcDatastores(targetId, refresh),
      api.getFcNetworks(targetId, refresh),
    ])
      .then(([ds, nets]) => {
        setDatastores(ds.datastores);
        setNetworks(nets.networks);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setResourcesLoading(false));
  }

  useEffect(() => {
    if (plan?.target_id) loadResources(plan.target_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan?.target_id]);

  async function submitTarget() {
    if (!id || !selectedTarget) return;
    setSettingTarget(true);
    setError(null);
    try {
      const updated = await api.setPlanTarget(id, selectedTarget);
      setPlan(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSettingTarget(false);
    }
  }

  function pendingFor(vmId: string) {
    return pendingMapping[vmId] ?? { datastoreUrn: "", networkUrn: "" };
  }

  function setPending(vmId: string, patch: Partial<{ datastoreUrn: string; networkUrn: string }>) {
    setPendingMapping((prev) => ({ ...prev, [vmId]: { ...pendingFor(vmId), ...patch } }));
  }

  async function saveMapping(vmId: string) {
    if (!id) return;
    const vm = plan?.vms.find((v) => v.id === vmId);
    if (!vm) return;
    const pending = pendingFor(vmId);
    // Bug corrigé le 17/09/2026 : si l'utilisateur n'a pas retouché les
    // <select> depuis le chargement de la page (cas courant : la page
    // s'ouvre déjà avec le mapping précédemment enregistré affiché),
    // pending.datastoreUrn/networkUrn valent "" — l'ancien code envoyait
    // alors `null`, ce que la route PUT /mapping ignore (`if ... is not
    // None`), donc "Enregistrer" ne faisait strictement rien de visible.
    // On retombe maintenant sur la valeur déjà affichée (vm.tgt_*), comme
    // le fait déjà le rendu JSX (currentDs/currentNet), pour que le clic
    // envoie toujours ce qui est réellement sélectionné à l'écran.
    const dsUrn = pending.datastoreUrn || vm.tgt_datastore_urn || "";
    const netUrn = pending.networkUrn || vm.tgt_network_urn || "";
    const ds = datastores.find((d) => d.urn === dsUrn);
    const net = networks.find((n) => n.urn === netUrn);
    setSavingVm(vmId);
    setError(null);
    setSavedVm(null);
    try {
      const updated = await api.setPlanVmMapping(id, vmId, {
        datastore_urn: dsUrn || null,
        datastore_name: ds?.name ?? (dsUrn ? vm.tgt_datastore_name : null),
        network_urn: netUrn || null,
        network_name: net?.name ?? (netUrn ? vm.tgt_network_name : null),
      });
      setPlan(updated);
      // On efface l'override "pending" : l'état vient d'être confirmé par
      // le serveur, l'affichage doit repartir de vm.tgt_* (source de
      // vérité) et donc pouvoir réafficher le libellé "actuel : ...".
      setPendingMapping((prev) => {
        const next = { ...prev };
        delete next[vmId];
        return next;
      });
      setSavedVm(vmId);
      window.setTimeout(() => setSavedVm((cur) => (cur === vmId ? null : cur)), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingVm(null);
    }
  }

  return (
    <>
      <button className="secondary" onClick={() => navigate("/plans")}>
        ← Plans
      </button>
      <h1>Cible FusionCompute — {plan?.name ?? "…"}</h1>
      <p className="muted">
        Étapes 3 à 5 : connexion à un environnement FusionCompute, puis choix du datastore et du
        réseau de destination pour chaque VM du plan. La copie elle-même (étapes 6-8) n'est pas
        encore implémentée — voir HANDOFF.md.
      </p>

      {error && <p className="error">{error}</p>}
      {loading && <p className="muted">Chargement…</p>}

      {!loading && plan && !plan.target_id && (
        <div className="card" style={{ maxWidth: 480 }}>
          <h2>1. Choisir l'environnement FusionCompute cible</h2>
          {fcEndpoints.length === 0 ? (
            <p className="muted">
              Aucun endpoint FusionCompute déclaré — ajoutez-en un depuis la page{" "}
              <a href="/endpoints" onClick={(e) => { e.preventDefault(); navigate("/endpoints"); }}>
                Environnements
              </a>{" "}
              (type « FusionCompute — VRM »).
            </p>
          ) : (
            <div className="form-row">
              <label>Environnement</label>
              <select value={selectedTarget} onChange={(e) => setSelectedTarget(e.target.value)}>
                <option value="">— choisir —</option>
                {fcEndpoints.map((ep) => (
                  <option key={ep.id} value={ep.id}>
                    {ep.name} ({ep.url}:{ep.port})
                  </option>
                ))}
              </select>
              <button disabled={!selectedTarget || settingTarget} onClick={submitTarget}>
                {settingTarget ? "Connexion…" : "Définir la cible"}
              </button>
            </div>
          )}
        </div>
      )}

      {!loading && plan && plan.target_id && (
        <>
          <p className="muted">
            Cible : {endpoints.find((e) => e.id === plan.target_id)?.name ?? plan.target_id}{" "}
            <button className="secondary" onClick={() => loadResources(plan.target_id!, true)}>
              rafraîchir datastores/réseaux
            </button>
          </p>

          {resourcesLoading && (
            <p className="muted">Connexion au VRM, listage des datastores et des réseaux…</p>
          )}

          {!resourcesLoading && (
            <table>
              <thead>
                <tr>
                  <th>VM</th>
                  <th>Datastore cible</th>
                  <th>Réseau cible</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {plan.vms.map((vm) => {
                  const pending = pendingFor(vm.id);
                  const currentDs = pending.datastoreUrn || vm.tgt_datastore_urn || "";
                  const currentNet = pending.networkUrn || vm.tgt_network_urn || "";
                  return (
                    <tr key={vm.id}>
                      <td>{vm.src_vm_name}</td>
                      <td>
                        <select
                          value={currentDs}
                          onChange={(e) => setPending(vm.id, { datastoreUrn: e.target.value })}
                        >
                          <option value="">— aucun —</option>
                          {datastores.map((ds) => (
                            <option key={ds.urn} value={ds.urn}>
                              {ds.name}
                              {ds.actual_free_gb != null ? ` (${ds.actual_free_gb} Go libres)` : ""}
                            </option>
                          ))}
                        </select>
                        {vm.tgt_datastore_name && !pending.datastoreUrn && (
                          <div className="muted">actuel : {vm.tgt_datastore_name}</div>
                        )}
                      </td>
                      <td>
                        <select
                          value={currentNet}
                          onChange={(e) => setPending(vm.id, { networkUrn: e.target.value })}
                        >
                          <option value="">— aucun —</option>
                          {networks.map((net) => (
                            <option key={net.urn} value={net.urn}>
                              {net.dvswitch_name} / {net.name}
                              {net.vlan_id != null ? ` (VLAN ${net.vlan_id})` : ""}
                            </option>
                          ))}
                        </select>
                        {vm.tgt_network_name && !pending.networkUrn && (
                          <div className="muted">actuel : {vm.tgt_network_name}</div>
                        )}
                      </td>
                      <td>
                        <button
                          className="secondary"
                          disabled={savingVm === vm.id || (!currentDs && !currentNet)}
                          onClick={() => saveMapping(vm.id)}
                        >
                          {savingVm === vm.id ? "…" : "Enregistrer"}
                        </button>
                        {savedVm === vm.id && <span className="muted"> enregistré ✓</span>}
                      </td>
                    </tr>
                  );
                })}
                {plan.vms.length === 0 && (
                  <tr>
                    <td colSpan={4} className="muted">
                      Ce plan ne contient aucune VM.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  );
}
