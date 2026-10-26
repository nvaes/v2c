import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  EndpointOut,
  FcDatastore,
  FcNetwork,
  PlanOut,
  PlanVmOut,
  SourceNic,
} from "../api";

/**
 * Étapes 3-5 du flux voulu (17/09/2026) : choisir un
 * environnement FusionCompute cible pour le plan, puis, pour chaque VM du
 * plan, choisir le datastore et le réseau de destination — un réseau par
 * carte réseau de la VM source (26/10/2026, cartes lues sur le vCenter).
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
  // Cartes réseau source par VM de plan (undefined : pas encore lues ou
  // vCenter injoignable -> repli sur un seul réseau par VM).
  const [sourceNics, setSourceNics] = useState<Record<string, SourceNic[]> | null>(null);
  const [nicsError, setNicsError] = useState<string | null>(null);
  // Choix en cours par VM puis par clé de carte ("" = ne pas recréer).
  const [pendingNics, setPendingNics] = useState<Record<string, Record<number, string>>>({});
  const [pendingKeepMac, setPendingKeepMac] = useState<Record<string, Record<number, boolean>>>({});

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

  useEffect(() => {
    if (!id || !plan?.target_id) return;
    setNicsError(null);
    api.getPlanSourceNics(id)
      .then(setSourceNics)
      .catch((e) => setNicsError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, plan?.target_id]);

  // Réseau cible affiché pour une carte : choix en cours, sinon mapping
  // enregistré, sinon (plan d'avant le mapping par carte) l'ancien réseau
  // unique pour la première carte.
  function nicTarget(vm: PlanVmOut, nic: SourceNic, index: number): string {
    const pending = pendingNics[vm.id]?.[nic.device_key];
    if (pending !== undefined) return pending;
    const saved = vm.nic_mappings.find((m) => m.device_key === nic.device_key);
    if (saved) return saved.tgt_network_urn ?? "";
    return index === 0 && vm.nic_mappings.length === 0 ? vm.tgt_network_urn ?? "" : "";
  }

  function nicKeepMac(vm: PlanVmOut, nic: SourceNic): boolean {
    const pending = pendingKeepMac[vm.id]?.[nic.device_key];
    if (pending !== undefined) return pending;
    return vm.nic_mappings.find((m) => m.device_key === nic.device_key)?.keep_mac ?? false;
  }

  function setPendingNic(vmId: string, deviceKey: number, urn: string) {
    setPendingNics((prev) => ({ ...prev, [vmId]: { ...prev[vmId], [deviceKey]: urn } }));
  }

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
    const nics = sourceNics?.[vmId];
    setSavingVm(vmId);
    setError(null);
    setSavedVm(null);
    try {
      const updated = await api.setPlanVmMapping(id, vmId, {
        datastore_urn: dsUrn || null,
        datastore_name: ds?.name ?? (dsUrn ? vm.tgt_datastore_name : null),
        ...(nics && nics.length > 0
          ? {
              nics: nics.map((nic, i) => {
                const urn = nicTarget(vm, nic, i);
                return {
                  device_key: nic.device_key, label: nic.label, adapter: nic.adapter,
                  mac: nic.mac, src_network: nic.network,
                  tgt_network_urn: urn || null,
                  tgt_network_name: urn ? networks.find((n) => n.urn === urn)?.name ?? null : null,
                  keep_mac: nicKeepMac(vm, nic),
                };
              }),
            }
          : {
              network_urn: netUrn || null,
              network_name: net?.name ?? (netUrn ? vm.tgt_network_name : null),
            }),
      });
      setPlan(updated);
      setPendingNics((prev) => {
        const next = { ...prev };
        delete next[vmId];
        return next;
      });
      setPendingKeepMac((prev) => {
        const next = { ...prev };
        delete next[vmId];
        return next;
      });
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
        Étapes 3 à 5 : connexion à un environnement FusionCompute, puis choix du datastore et,
        pour chaque carte réseau de la VM source, du réseau de destination. Une carte laissée sur
        « ne pas recréer » n'existera pas sur la VM migrée. « Conserver la MAC » recrée la carte
        avec l'adresse MAC source : ne rallume pas la VM source (rollback) en même temps que la VM
        migrée sur le même réseau, les deux auraient la même MAC.
      </p>

      {error && <p className="error">{error}</p>}
      {nicsError && (
        <p className="muted">
          Cartes réseau source illisibles ({nicsError}) — un seul réseau par VM en attendant.
        </p>
      )}
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
                  <th>Réseaux (une ligne par carte source)</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {plan.vms.map((vm) => {
                  const pending = pendingFor(vm.id);
                  const currentDs = pending.datastoreUrn || vm.tgt_datastore_urn || "";
                  const currentNet = pending.networkUrn || vm.tgt_network_urn || "";
                  const nics = sourceNics?.[vm.id];
                  const anyNet = nics && nics.length > 0
                    ? nics.some((nic, i) => nicTarget(vm, nic, i) !== "")
                    : !!currentNet;
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
                        {nics && nics.length > 0 ? (
                          nics.map((nic, i) => (
                            <div key={nic.device_key} style={{ marginBottom: 6 }}>
                              <div className="muted">
                                {nic.label} · {nic.adapter} · {nic.mac} — source : {nic.network || "?"}
                              </div>
                              <select
                                value={nicTarget(vm, nic, i)}
                                onChange={(e) => setPendingNic(vm.id, nic.device_key, e.target.value)}
                              >
                                <option value="">— ne pas recréer cette carte —</option>
                                {networks.map((net) => (
                                  <option key={net.urn} value={net.urn}>
                                    {net.dvswitch_name} / {net.name}
                                    {net.vlan_id != null ? ` (VLAN ${net.vlan_id})` : ""}
                                  </option>
                                ))}
                              </select>
                              <label className="muted" style={{ marginLeft: 8 }}>
                                <input
                                  type="checkbox"
                                  disabled={!nicTarget(vm, nic, i)}
                                  checked={nicKeepMac(vm, nic)}
                                  onChange={(e) => setPendingKeepMac((prev) => ({
                                    ...prev,
                                    [vm.id]: { ...prev[vm.id], [nic.device_key]: e.target.checked },
                                  }))}
                                />{" "}
                                conserver la MAC
                              </label>
                            </div>
                          ))
                        ) : (
                        <>
                        {sourceNics === null && !nicsError && (
                          <div className="muted">lecture des cartes réseau source…</div>
                        )}
                        {nics && nics.length === 0 && (
                          <div className="muted">aucune carte réseau sur la VM source</div>
                        )}
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
                        </>
                        )}
                      </td>
                      <td>
                        <button
                          className="secondary"
                          disabled={savingVm === vm.id || (!currentDs && !anyNet)}
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
