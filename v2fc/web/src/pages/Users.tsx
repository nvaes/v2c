import { FormEvent, useEffect, useState } from "react";
import { api, Role, UserOut } from "../api";

const ROLE_LABELS: Record<Role, string> = {
  viewer: "lecture seule",
  operator: "opérateur (plans, migrations)",
  admin: "administrateur (+ utilisateurs)",
};

/**
 * Gestion des comptes (rôle admin). Pas de suppression : un compte est
 * désactivé (plans et jobs gardent leur auteur). L'API refuse de retirer le
 * dernier admin actif ou ses propres droits.
 */
export default function UsersPage({ currentEmail }: { currentEmail: string }) {
  const [users, setUsers] = useState<UserOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("operator");

  function refresh() {
    api.listUsers().then(setUsers).catch((e) => setError(String(e)));
  }
  useEffect(refresh, []);

  async function run(key: string, action: () => Promise<unknown>, done?: string) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (done) setNotice(done);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  function create(e: FormEvent) {
    e.preventDefault();
    run("create", async () => {
      await api.createUser({ email, password, role });
      setEmail("");
      setPassword("");
    }, `compte ${email} créé`);
  }

  function resetPassword(user: UserOut) {
    const pwd = window.prompt(`Nouveau mot de passe pour ${user.email} (10 caractères minimum) :`);
    if (!pwd) return;
    run(user.id, () => api.updateUser(user.id, { password: pwd }),
      `mot de passe de ${user.email} réinitialisé`);
  }

  return (
    <>
      <h1>Utilisateurs</h1>
      {error && <p className="error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}

      <table>
        <thead>
          <tr><th>E-mail</th><th>Rôle</th><th>Statut</th><th></th></tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.email}{u.email === currentEmail && <span className="muted"> (vous)</span>}</td>
              <td>
                <select
                  value={u.role}
                  disabled={busy === u.id}
                  onChange={(e) => run(u.id, () => api.updateUser(u.id, { role: e.target.value as Role }))}
                >
                  {(Object.keys(ROLE_LABELS) as Role[]).map((r) => (
                    <option key={r} value={r}>{ROLE_LABELS[r]}</option>
                  ))}
                </select>
              </td>
              <td>
                <span className={`badge ${u.is_active ? "ok" : "blocked"}`}>
                  {u.is_active ? "actif" : "désactivé"}
                </span>
              </td>
              <td>
                <button className="secondary" disabled={busy === u.id} onClick={() => resetPassword(u)}>
                  Réinitialiser le mot de passe
                </button>{" "}
                <button
                  className="secondary"
                  disabled={busy === u.id}
                  onClick={() => run(u.id, () => api.updateUser(u.id, { is_active: !u.is_active }))}
                >
                  {u.is_active ? "Désactiver" : "Réactiver"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="card" style={{ maxWidth: 420, marginTop: 24 }}>
        <h2 style={{ marginTop: 0 }}>Nouveau compte</h2>
        <form onSubmit={create}>
          <div className="form-row">
            <label>E-mail</label>
            <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="form-row">
            <label>Mot de passe initial (10 caractères minimum)</label>
            <input type="password" required minLength={10} value={password}
              onChange={(e) => setPassword(e.target.value)} />
          </div>
          <div className="form-row">
            <label>Rôle</label>
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {(Object.keys(ROLE_LABELS) as Role[]).map((r) => (
                <option key={r} value={r}>{ROLE_LABELS[r]}</option>
              ))}
            </select>
          </div>
          <button type="submit" disabled={busy === "create"}>Créer le compte</button>
        </form>
      </div>
    </>
  );
}
