import { FormEvent, useState } from "react";
import { api, setToken } from "../api";

export default function Login({ onLoggedIn }: { onLoggedIn: (email: string) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const token = await api.login(email, password);
      setToken(token);
      onLoggedIn(email);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 360, margin: "80px auto" }}>
      <h1>V2FC</h1>
      <p className="muted">
        Pas encore de compte ? Un administrateur en crée un avec :{" "}
        <code>docker compose exec api python3 -m api.seed_admin --email vous@exemple.com</code>
      </p>
      <form className="card" onSubmit={submit}>
        <div className="form-row">
          <label>Adresse e-mail</label>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="form-row">
          <label>Mot de passe</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "Connexion…" : "Se connecter"}
        </button>
      </form>
    </main>
  );
}
