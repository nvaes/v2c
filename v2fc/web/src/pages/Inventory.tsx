import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, InventoryProgress, InventoryResponse, InventoryVm } from "../api";

type SortKey = "name" | "dc" | "power_state" | "num_disks" | "disk_capacity_gb"
  | "cbt_enabled" | "firmware" | "migratable";

function sortValue(row: { dc: string; vm: InventoryVm }, key: SortKey): string | number {
  switch (key) {
    case "dc": return row.dc;
    case "cbt_enabled": return row.vm.cbt_enabled ? 1 : 0;
    case "migratable": return row.vm.migratable ? 1 : 0;
    default: return row.vm[key];
  }
}

export default function InventoryPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [data, setData] = useState<InventoryResponse | null>(null);
  const [selected, setSelected] = useState<Record<string, InventoryVm>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [planName, setPlanName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createdPlanId, setCreatedPlanId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [progress, setProgress] = useState<InventoryProgress | null>(null);

  function load(refresh = false) {
    if (!id) return;
    setLoading(true);
    setError(null);
    setProgress(null);
    api
      .getInventory(id, refresh)
      .then(setData)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => load(), [id]);

  // Poll la progression pendant qu'un scan tourne (GET .../inventory/progress,
  // demande de Nicolas du 17/09/2026) — purement indicatif : la réponse de
  // getInventory ci-dessus reste la seule source de vérité sur la fin du scan,
  // ce poll s'arrête tout seul dès que `loading` repasse à false.
  useEffect(() => {
    if (!loading || !id) return;
    const interval = setInterval(() => {
      api.getInventoryProgress(id).then(setProgress).catch(() => {});
    }, 1000);
    return () => clearInterval(interval);
  }, [loading, id]);

  // Les VM apparaissent au niveau du datacenter dans la réponse de l'API
  // (voir vsphere_bridge.inventory) : on les récupère ici, pas au niveau
  // cluster, qui ne sert qu'à l'affichage des hôtes (un cluster n'a pas de
  // liste de VM propre côté backend).
  const vmsFlat = (data?.datacenters ?? []).flatMap((dc) => dc.vms.map((vm) => ({ dc: dc.name, vm })));

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const needle = search.trim().toLowerCase();
  const visibleVms = vmsFlat
    .filter(({ dc, vm }) => {
      if (!needle) return true;
      return (
        vm.name.toLowerCase().includes(needle) ||
        dc.toLowerCase().includes(needle) ||
        vm.guest_id.toLowerCase().includes(needle) ||
        vm.power_state.toLowerCase().includes(needle) ||
        vm.firmware.toLowerCase().includes(needle)
      );
    })
    .slice()
    .sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      const cmp = typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv), "fr", { sensitivity: "base" });
      return sortDir === "asc" ? cmp : -cmp;
    });

  function Th({ label, sortKeyName }: { label: string; sortKeyName: SortKey }) {
    const active = sortKey === sortKeyName;
    return (
      <th
        onClick={() => toggleSort(sortKeyName)}
        style={{ cursor: "pointer", userSelect: "none" }}
        title="Trier sur cette colonne"
      >
        {label}
        {active ? (sortDir === "asc" ? " \u25B2" : " \u25BC") : ""}
      </th>
    );
  }

  function toggle(vm: InventoryVm) {
    setSelected((prev) => {
      const next = { ...prev };
      if (next[vm.instance_uuid]) delete next[vm.instance_uuid];
      else next[vm.instance_uuid] = vm;
      return next;
    });
  }

  function selectAllMigratable() {
    const next: Record<string, InventoryVm> = {};
    for (const { vm } of vmsFlat) if (vm.migratable) next[vm.instance_uuid] = vm;
    setSelected(next);
  }

  async function createPlan(e: FormEvent) {
    e.preventDefault();
    if (!id) return;
    setCreating(true);
    setError(null);
    try {
      const plan = await api.createPlan({
        name: planName,
        source_id: id,
        vms: Object.values(selected).map((vm) => ({
          moref: vm.moref,
          instance_uuid: vm.instance_uuid,
          name: vm.name,
        })),
      });
      setCreatedPlanId(plan.id);
      setSelected({});
      setPlanName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  const selectedCount = Object.keys(selected).length;

  return (
    <>
      <button className="secondary" onClick={() => navigate("/endpoints")}>
        ← Environnements
      </button>
      <h1>Inventaire</h1>
      {error && <p className="error">{error}</p>}
      {loading && (
        <div style={{ marginBottom: 12 }}>
          <p className="muted" style={{ margin: "0 0 6px" }}>
            {progress?.phase === "preflight" && progress.total > 0
              ? `Pré-vérification des VM : ${progress.done} / ${progress.total}…`
              : progress?.phase === "connecting"
              ? "Connexion à l'environnement…"
              : "Connexion à l'environnement, inventaire et pré-vérification de chaque VM…"}
          </p>
          {progress?.phase === "preflight" && progress.total > 0 && (
            <div
              style={{ background: "#e5e7eb", borderRadius: 4, height: 6, maxWidth: 320, overflow: "hidden" }}
            >
              <div
                style={{
                  width: `${Math.round((progress.done / progress.total) * 100)}%`,
                  background: "#166534",
                  height: "100%",
                }}
              />
            </div>
          )}
        </div>
      )}

      {data && (
        <>
          <p className="muted">
            {data.vm_count} VM. Le dernier inventaire connu (VM et pré-vérification § preflight)
            est conservé et réaffiché tel quel à chaque ouverture de cette page — il n'est
            rescanné que sur demande explicite, avec{" "}
            <button className="secondary" onClick={() => load(true)}>
              rafraîchir
            </button>
            .
          </p>
          <div className="form-row" style={{ maxWidth: 320 }}>
            <label>Recherche</label>
            <input
              type="text"
              placeholder="nom, datacenter, OS, état…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <table>
            <thead>
              <tr>
                <th></th>
                <Th label="VM" sortKeyName="name" />
                <Th label="Datacenter" sortKeyName="dc" />
                <Th label="État" sortKeyName="power_state" />
                <Th label="Disques" sortKeyName="num_disks" />
                <Th label="Taille" sortKeyName="disk_capacity_gb" />
                <Th label="CBT" sortKeyName="cbt_enabled" />
                <Th label="Firmware" sortKeyName="firmware" />
                <Th label="Migrable" sortKeyName="migratable" />
              </tr>
            </thead>
            <tbody>
              {visibleVms.map(({ dc, vm }) => (
                <tr key={vm.instance_uuid} title={vm.preflight_findings.join("\n")}>
                  <td>
                    <input
                      type="checkbox"
                      disabled={!vm.migratable}
                      checked={!!selected[vm.instance_uuid]}
                      onChange={() => toggle(vm)}
                    />
                  </td>
                  <td>{vm.name}</td>
                  <td>{dc}</td>
                  <td>{vm.power_state}</td>
                  <td>{vm.num_disks}</td>
                  <td>{vm.disk_capacity_gb.toFixed(1)} Go</td>
                  <td>{vm.cbt_enabled ? "oui" : "non"}</td>
                  <td>{vm.firmware}</td>
                  <td>
                    <span className={`badge ${vm.migratable ? "ok" : "blocked"}`}>
                      {vm.migratable ? "oui" : "bloquée"}
                    </span>
                  </td>
                </tr>
              ))}
              {visibleVms.length === 0 && (
                <tr>
                  <td colSpan={9} className="muted">
                    {vmsFlat.length === 0 ? "Aucune VM." : "Aucune VM ne correspond à la recherche."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
            <button className="secondary" onClick={selectAllMigratable}>
              Tout sélectionner (migrables)
            </button>
            <span className="muted">{selectedCount} VM sélectionnée(s)</span>
          </div>

          <h2>Créer un plan de migration avec la sélection</h2>
          <form className="card" onSubmit={createPlan} style={{ maxWidth: 420 }}>
            <div className="form-row">
              <label>Nom du plan</label>
              <input value={planName} onChange={(e) => setPlanName(e.target.value)} required />
            </div>
            <button type="submit" disabled={creating || selectedCount === 0}>
              {creating ? "Création…" : `Créer le plan (${selectedCount} VM)`}
            </button>
            {createdPlanId && (
              <p className="muted">
                Plan créé.{" "}
                <a href="/plans" onClick={(e) => { e.preventDefault(); navigate("/plans"); }}>
                  Voir les plans
                </a>
              </p>
            )}
          </form>
        </>
      )}
    </>
  );
}
