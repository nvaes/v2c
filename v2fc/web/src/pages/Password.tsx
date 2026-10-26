import { FormEvent, useState } from "react";
import { api } from "../api";

/** Changement de son propre mot de passe (l'ancien est exigé). */
export default function PasswordPage() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setDone(false);
    if (next !== confirm) {
      setError("les deux nouveaux mots de passe ne correspondent pas");
      return;
    }
    setBusy(true);
    try {
      await api.changeOwnPassword(current, next);
      setDone(true);
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 420 }}>
      <h1 style={{ marginTop: 0 }}>Mon mot de passe</h1>
      {error && <p className="error">{error}</p>}
      {done && <p className="muted">Mot de passe modifié.</p>}
      <form onSubmit={submit}>
        <div className="form-row">
          <label>Mot de passe actuel</label>
          <input type="password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="form-row">
          <label>Nouveau mot de passe (10 caractères minimum)</label>
          <input type="password" required minLength={10} value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <div className="form-row">
          <label>Confirmer le nouveau mot de passe</label>
          <input type="password" required minLength={10} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        <button type="submit" disabled={busy}>Changer le mot de passe</button>
      </form>
    </div>
  );
}
