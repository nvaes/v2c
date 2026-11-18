import { useEffect, useState } from "react";
import { api, EndpointOut, PlanAutoGroup, PlanAutoResponse } from "../api";

const GROUPS: { key: PlanAutoGroup; label: string; hint: string }[] = [
  { key: "dc", label: "Datacenter", hint: "une vague ne mélange pas les datacenters" },
  { key: "os", label: "Famille d'OS", hint: "Linux d'abord, Windows à part (pilotes virtio)" },
  { key: "power", label: "Allumée / éteinte", hint: "VM éteintes : passe à froid, sans fenêtre" },
  { key: "prefix", label: "Préfixe de nom", hint: "application/site selon la convention de nommage" },
];

function Counts({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) return <span className="muted">—</span>;
  return <>{entries.map(([k, n]) => `${k} : ${n}`).join(" · ")}</>;
}

// Migration complète d'une source (18/11/2026) : analyse son inventaire et
// propose un découpage de toutes ses VM migrables en plans (critères réglables,
// détail dans api/plan_auto.py), puis les crée sur confirmation. Aucune valeur
// propre à un client : la limite de disques vient du relais réel.
export default function AutoPlansModal({ endpoints, onClose, onCreated }: {
  endpoints: EndpointOut[]; onClose: () => void; onCreated: () => void;
}) {
  const sources = endpoints.filter((e) => e.kind === "vcenter");
  const [sourceId, setSourceId] = useState(sources[0]?.id ?? "");
  const [prefix, setPrefix] = useState("");
  const [maxDisks, setMaxDisks] = useState(24);
  const [relayHint, setRelayHint] = useState<string | null>(null);
  const [maxGb, setMaxGb] = useState(4096);
  const [maxVms, setMaxVms] = useState(0);
  const [pilotSize, setPilotSize] = useState(3);
  const [groupBy, setGroupBy] = useState<PlanAutoGroup[]>(["dc", "os"]);
  const [preview, setPreview] = useState<PlanAutoResponse | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Disques max par plan : ce qu'il reste sur le relais le plus contraint une
  // fois ses propres disques comptés (25 - système), pas une constante.
  useEffect(() => {
    api.relaysCapacity().then((caps) => {
      const known = caps.filter((c) => c.system !== null);
      if (known.length === 0) return;
      const usable = Math.min(...known.map((c) => c.total - (c.system ?? 0)));
      if (usable > 0) setMaxDisks(usable);
      setRelayHint(`relais : ${known[0].total} emplacements, ${known[0].system} pris par le relais lui-même`
        + (known.length > 1 ? ` (${known.length} relais)` : ""));
    }).catch(() => {});
  }, []);

  async function run(dryRun: boolean) {
    setBusy(dryRun ? "preview" : "create");
    setError(null);
    try {
      const out = await api.autoPlans({
        source_id: sourceId, prefix, max_disks: maxDisks, max_capacity_gb: maxGb,
        max_vms: maxVms, pilot_size: pilotSize, group_by: groupBy, dry_run: dryRun,
      });
      if (dryRun) setPreview(out);
      else {
        onCreated();
        onClose();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function rescan() {
    setBusy("scan");
    setError(null);
    try {
      await api.getInventory(sourceId, true);
      setBusy(null);
      await run(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(null);
    }
  }

  // Un paramètre modifié invalide l'aperçu : on ne crée que ce qu'on a vu.
  function changed<T>(set: (v: T) => void) {
    return (v: T) => { set(v); setPreview(null); };
  }
  function toggleGroup(key: PlanAutoGroup) {
    changed(setGroupBy)(groupBy.includes(key) ? groupBy.filter((k) => k !== key) : [...groupBy, key]);
  }

  const a = preview?.analysis;
  const vmCount = preview?.plans.reduce((n, p) => n + p.vms.length, 0) ?? 0;

  return (
    <div className="modal-backdrop">
      <div className="modal" style={{ width: "min(960px, calc(100vw - 32px))", overflowY: "auto" }}>
        <h2 style={{ margin: 0 }}>Migration complète : génération automatique des plans</h2>
        <p className="muted" style={{ margin: "4px 0 8px" }}>
          Analyse le dernier inventaire de la source et découpe en plans ses VM migrables pas
          encore planifiées : vague pilote de petites VM (éteintes et Linux de préférence), puis
          vagues regroupées selon les critères choisis, petites VM d'abord. Un plan ne dépasse
          jamais les places du relais, pour pouvoir être provisionné d'un seul coup.
        </p>
        {error && <p className="error">{error}</p>}
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-row">
            <label>Source</label>
            <select value={sourceId} onChange={(e) => changed(setSourceId)(e.target.value)}>
              {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <div className="form-row">
            <label>Préfixe des noms</label>
            <input value={prefix} placeholder="nom de la source"
                   onChange={(e) => changed(setPrefix)(e.target.value)} />
          </div>
          <div className="form-row">
            <label>Disques max / plan</label>
            <input type="number" min={1} max={25} value={maxDisks} style={{ width: 70 }}
                   title={relayHint ?? undefined}
                   onChange={(e) => changed(setMaxDisks)(Number(e.target.value))} />
          </div>
          <div className="form-row">
            <label>Go max / plan</label>
            <input type="number" min={1} value={maxGb} style={{ width: 90 }}
                   onChange={(e) => changed(setMaxGb)(Number(e.target.value))} />
          </div>
          <div className="form-row">
            <label>VM max / plan</label>
            <input type="number" min={0} value={maxVms} style={{ width: 70 }} title="0 : pas de limite"
                   onChange={(e) => changed(setMaxVms)(Number(e.target.value))} />
          </div>
          <div className="form-row">
            <label>VM pilotes</label>
            <input type="number" min={0} max={20} value={pilotSize} style={{ width: 70 }}
                   title="0 : pas de vague pilote"
                   onChange={(e) => changed(setPilotSize)(Number(e.target.value))} />
          </div>
        </div>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", margin: "4px 0" }}>
          <span className="muted">Regrouper par :</span>
          {GROUPS.map((g) => (
            <label key={g.key} title={g.hint}>
              <input type="checkbox" checked={groupBy.includes(g.key)}
                     onChange={() => toggleGroup(g.key)} /> {g.label}
            </label>
          ))}
        </div>
        {relayHint && <p className="muted" style={{ margin: 0 }}>Limite de disques proposée d'après le {relayHint}.</p>}
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button className="secondary" disabled={!!busy || !sourceId} onClick={() => run(true)}>
            {busy === "preview" ? "Analyse…" : "Analyser et prévisualiser"}
          </button>
          <button className="secondary" disabled={!!busy || !sourceId} onClick={rescan}
                  title="Relit toutes les VM sur le vCenter (long) avant l'analyse">
            {busy === "scan" ? "Inventaire en cours…" : "Rescanner l'inventaire puis analyser"}
          </button>
        </div>

        {a && preview && (
          <>
            <div className="card" style={{ marginTop: 8 }}>
              <strong>Analyse de l'inventaire</strong>
              <div>
                {a.vm_total} VM, {a.vm_migratable} migrables, {a.vm_already_planned} déjà dans un
                plan → <strong>{a.vm_to_plan} à planifier</strong> : {a.disks_to_plan} disques,{" "}
                {a.capacity_gb_to_plan.toLocaleString("fr-FR")} Go.
              </div>
              <div>OS : <Counts data={a.by_os} /></div>
              {Object.keys(a.by_dc).length > 1 && <div>Datacenters : <Counts data={a.by_dc} /></div>}
              <div>État : <Counts data={a.by_power} /></div>
              <div>Disques par VM : <Counts data={a.by_disks} /></div>
              {Object.keys(a.prefixes).length > 0 && (
                <div>Préfixes de nom fréquents : <Counts data={a.prefixes} /></div>
              )}
              <div>
                Plus grosses VM :{" "}
                {a.largest.map((v) => `${v.name} (${v.disk_capacity_gb.toLocaleString("fr-FR")} Go, ${v.num_disks} disques)`).join(" · ")}
              </div>
              <div>
                Le relais devra être rempli puis vidé au moins <strong>{a.min_relay_rounds} fois</strong>{" "}
                ({preview.max_disks} disques par passage).
                {a.over_max_disks.length > 0 &&
                  ` ${a.over_max_disks.length} VM ont plus de ${preview.max_disks} disques : ${a.over_max_disks.join(", ")}.`}
              </div>
            </div>
            <p style={{ margin: "8px 0" }}>
              <strong>{preview.plans.length} plan(s)</strong>, {vmCount} VM ;{" "}
              {preview.excluded.length} VM écartée(s).
            </p>
            <table>
              <thead>
                <tr><th>Plan</th><th>VM</th><th>Disques</th><th>Taille</th></tr>
              </thead>
              <tbody>
                {preview.plans.map((p) => (
                  <tr key={p.name} title={p.vms.map((v) => v.name).join("\n")}>
                    <td>{p.name}</td>
                    <td>
                      {p.vms.length}{" "}
                      <span className="muted">
                        ({p.vms.slice(0, 4).map((v) => v.name).join(", ")}
                        {p.vms.length > 4 ? ", …" : ""})
                      </span>
                    </td>
                    <td>{p.disks} / {preview.max_disks}</td>
                    <td>{p.capacity_gb.toLocaleString("fr-FR")} Go</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {preview.excluded.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary>VM écartées ({preview.excluded.length})</summary>
                <ul className="muted">
                  {preview.excluded.map((e) => <li key={e.name}><strong>{e.name}</strong> : {e.reason}</li>)}
                </ul>
              </details>
            )}
          </>
        )}

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <button className="secondary" onClick={onClose}>Fermer</button>
          <button disabled={!!busy || !preview || preview.plans.length === 0} onClick={() => run(false)}>
            {busy === "create" ? "Création…" : `Créer les ${preview?.plans.length ?? 0} plans`}
          </button>
        </div>
      </div>
    </div>
  );
}
