import { useEffect, useState } from "react";
import { api, RunnerStatus } from "./api";

/**
 * État du service runner (exécution des copies), rafraîchi toutes les 15 s.
 * Hors ligne : aucun seed, delta ni delta final ne peut démarrer.
 */
export function useRunnerStatus(): RunnerStatus | null {
  const [status, setStatus] = useState<RunnerStatus | null>(null);
  useEffect(() => {
    const load = () => api.runnerStatus().then(setStatus).catch(() => setStatus(null));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);
  return status;
}

export default function RunnerBadge() {
  const status = useRunnerStatus();
  if (!status) return null;
  const color = status.online ? "#4ade80" : "#f87171";
  const title = status.last_seen
    ? `dernier signal : ${new Date(status.last_seen).toLocaleString()}`
    : "aucun signal reçu";
  return (
    <span title={title} style={{ color: "#cbd5e1", fontSize: "0.85rem" }}>
      <span style={{ color }}>●</span>{" "}
      {status.online
        ? `runner en ligne${status.running.length ? ` — ${status.running.length} copie(s) en cours` : ""}`
        : "runner hors ligne"}
    </span>
  );
}
