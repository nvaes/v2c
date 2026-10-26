import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, EndpointCreate, EndpointOut, ProbeResult } from "../api";

const emptyForm: EndpointCreate = {
  kind: "vcenter",
  name: "",
  url: "",
  port: 443,
  username: "root",
  password: "",
  verify_tls: false,
};

export default function EndpointsPage() {
  const [endpoints, setEndpoints] = useState<EndpointOut[]>([]);
  const [form, setForm] = useState<EndpointCreate>(emptyForm);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [probeResults, setProbeResults] = useState<Record<string, ProbeResult>>({});
  const [deletingId, setDeletingId] = useState<string | null>(null);

  function refresh() {
    api.listEndpoints().then(setEndpoints).catch((e) => setError(String(e)));
  }

  useEffect(refresh, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createEndpoint(form);
      setForm(emptyForm);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function probe(id: string) {
    setError(null);
    try {
      const result = await api.probeEndpoint(id);
      setProbeResults((prev) => ({ ...prev, [id]: result }));
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function remove(ep: EndpointOut) {
    if (!window.confirm(`Supprimer l'environnement « ${ep.name} » (${ep.url}) ?`)) return;
    setDeletingId(ep.id);
    setError(null);
    try {
      await api.deleteEndpoint(ep.id);
      refresh();
    } catch (err) {
      // 409 attendu si un plan référence encore cet endpoint — le message
      // du backend liste déjà les plans concernés, pas la peine de le reformuler.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <>
      <h1>Environnements</h1>
      <p className="muted">
        Un vCenter ou un hôte ESXi autonome — <code>datamover.vsphere</code> traite les deux de la
        même façon, pas la peine de les distinguer ici.
      </p>

      {error && <p className="error">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Nom</th>
            <th>Hôte</th>
            <th>TLS vérifié</th>
            <th>Dernière sonde</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {endpoints.map((ep) => {
            const probeResult = probeResults[ep.id];
            return (
              <tr key={ep.id}>
                <td>{ep.name}</td>
                <td>{ep.url}</td>
                <td>{ep.verify_tls ? "oui" : "non (empreinte épinglée)"}</td>
                <td>
                  {ep.last_probe_at ? (
                    <span className={`badge ${ep.last_probe_ok ? "ok" : "blocked"}`}>
                      {ep.last_probe_ok ? "OK" : "échec"} — {new Date(ep.last_probe_at).toLocaleString()}
                    </span>
                  ) : (
                    <span className="muted">jamais sondé</span>
                  )}
                  {probeResult && !probeResult.ok && (
                    <div className="muted">{probeResult.message}</div>
                  )}
                </td>
                <td style={{ display: "flex", gap: 6 }}>
                  <button className="secondary" onClick={() => probe(ep.id)}>
                    Tester
                  </button>
                  {ep.kind === "vcenter" ? (
                    <Link to={`/endpoints/${ep.id}/inventory`}>
                      <button>Voir les VM</button>
                    </Link>
                  ) : (
                    <span className="muted">
                      cible — datastores/réseaux visibles depuis un plan
                    </span>
                  )}
                  <button
                    className="secondary"
                    disabled={deletingId === ep.id}
                    onClick={() => remove(ep)}
                  >
                    {deletingId === ep.id ? "…" : "Supprimer"}
                  </button>
                </td>
              </tr>
            );
          })}
          {endpoints.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                Aucun environnement déclaré pour l'instant.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h2>Ajouter un environnement</h2>
      <form className="card" onSubmit={submit} style={{ maxWidth: 420 }}>
        <div className="form-row">
          <label>Type</label>
          <select
            value={form.kind}
            onChange={(e) => {
              const kind = e.target.value as EndpointCreate["kind"];
              // Le port par défaut de vCenter/ESXi (443) n'a aucune raison
              // de convenir à FusionCompute — ARCHITECTURE.md §7.5 documente
              // 7443 pour l'API VRM (à vérifier sur ce lab, voir docs/dev/JOURNAL.md
              // §12 : 8443 est l'adresse console de la console).
              const port = kind === "fusioncompute" && form.kind !== "fusioncompute" ? 7443
                : kind === "vcenter" && form.kind !== "vcenter" ? 443
                : form.port;
              setForm({ ...form, kind, port });
            }}
          >
            <option value="vcenter">vCenter / ESXi (source)</option>
            <option value="fusioncompute">FusionCompute — VRM (cible)</option>
          </select>
        </div>
        <div className="form-row">
          <label>Nom</label>
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="ex. Lab ESXi"
            required
          />
        </div>
        <div className="form-row">
          <label>Hôte ou adresse IP</label>
          <input
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
            placeholder="192.0.2.30"
            required
          />
        </div>
        <div className="form-row">
          <label>Port</label>
          <input
            type="number"
            value={form.port}
            onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
            required
          />
          {form.kind === "fusioncompute" && (
            <span className="muted">
              7443 selon la documentation VRM (§7.5) — non vérifié sur ce lab, où l'adresse
              console donnée est en 8443. À confirmer avant la première connexion.
            </span>
          )}
        </div>
        <div className="form-row">
          <label>Utilisateur</label>
          <input
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
            required
          />
        </div>
        <div className="form-row">
          <label>Mot de passe</label>
          <input
            type="password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            required
          />
          <span className="muted">
            Chiffré en base par enveloppe (ARCHITECTURE.md §13), jamais renvoyé ensuite.
          </span>
        </div>
        <div className="form-row">
          <label>
            <input
              type="checkbox"
              checked={form.verify_tls}
              onChange={(e) => setForm({ ...form, verify_tls: e.target.checked })}
            />{" "}
            Vérifier le certificat TLS (à décocher pour un certificat auto-signé de lab)
          </label>
        </div>
        <button type="submit" disabled={busy}>
          {busy ? "Ajout…" : "Ajouter"}
        </button>
      </form>
    </>
  );
}
