import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Link } from "react-router-dom";
import { api, JobLogEntryOut, JobOut } from "../api";

const LEVEL_CLASS: Record<string, string> = {
  error: "log-error", warning: "log-warning", info: "", debug: "muted",
};

export default function LogsPage() {
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<JobOut | null>(null);
  const [entries, setEntries] = useState<JobLogEntryOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const lastAt = useRef<string | undefined>(undefined);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    async function poll() {
      try {
        const [j, logs] = await Promise.all([api.getJob(id!), api.getJobLogs(id!, lastAt.current)]);
        if (cancelled) return;
        setJob(j);
        if (logs.entries.length > 0) {
          lastAt.current = logs.entries[logs.entries.length - 1].at;
          setEntries((prev) => [...prev, ...logs.entries]);
        }
        setError(null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }

    poll();
    const t = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "nearest" });
  }, [entries.length]);

  return (
    <>
      <h1>Logs {job ? `— ${job.src_vm_name}` : ""}</h1>
      {job && (
        <p className="muted">
          État actuel : <span className="badge">{job.state}</span> — round(s) :{" "}
          {job.rounds.map((r) => `${r.kind}#${r.round_no} (${r.status})`).join(", ") || "aucun"}
        </p>
      )}
      {error && <p className="error">{error}</p>}

      <div
        className="card"
        style={{
          fontFamily: "monospace", fontSize: 12, maxHeight: "70vh", overflowY: "auto",
        }}
      >
        {entries.length === 0 && <p className="muted">Aucune entrée pour l'instant.</p>}
        {entries.map((e) => (
          <div key={e.id} className={LEVEL_CLASS[e.level] ?? ""}>
            <span className="muted">{new Date(e.at).toLocaleTimeString()}</span>{" "}
            <strong>[{e.level}]</strong> {e.message}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </>
  );
}

/**
 * Onglet « Logs » de la barre de navigation (demande de Nicolas,
 * 17/09/2026 : « un onglet logs avec les logs en cours ») — liste les jobs
 * ayant une activité, plutôt qu'un flux global : le détail (page ci-dessus)
 * reste par job, plus lisible qu'un mélange de tous les journaux ensemble.
 */
export function LogsIndexPage() {
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    function refresh() {
      api.listJobs()
        .then((j) => setJobs(j.filter((job) => job.state !== "CREATED")
          .sort((a, b) => (a.state_since < b.state_since ? 1 : -1))))
        .catch((e) => setError(String(e)))
        .finally(() => setLoading(false));
    }
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <>
      <h1>Logs</h1>
      {error && <p className="error">{error}</p>}
      {loading && <p className="muted">Chargement…</p>}
      {!loading && jobs.length === 0 && (
        <p className="muted">Aucun job en cours — voir l'onglet Migration pour en démarrer un.</p>
      )}
      <table>
        <thead>
          <tr><th>VM</th><th>État</th><th>Depuis</th><th></th></tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td>{j.src_vm_name}</td>
              <td><span className="badge">{j.state}</span></td>
              <td className="muted">{new Date(j.state_since).toLocaleString()}</td>
              <td><Link to={`/jobs/${j.id}/logs`}>voir les logs</Link></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
