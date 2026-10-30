import { useEffect, useState } from "react";
import { Clock3 } from "lucide-react";
import { getHistory, getPersistedEvents, getPersistedTasks, retryPersistence } from "../services/api";
import { authEnabled, loadSession, onSession } from "../services/auth";
import type { PersistedEvent, PersistedTask, Task } from "../types";
import StatusPill from "../components/StatusPill";
import { PERSISTENCE_LABEL, PROVIDER_LABEL, VERIFICATION_LABEL } from "../lib/tasks";

function timeAgo(unixSeconds: number): string {
  const diff = Date.now() / 1000 - unixSeconds;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function Timeline({ id }: { id: string }) {
  const [events, setEvents] = useState<PersistedEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { getPersistedEvents(id).then(setEvents).catch((e) => setError(e.message)); }, [id]);
  if (error) return <p className="submit-error mt-2">{error}</p>;
  if (!events) return <p className="mt-2 text-xs text-muted">Loading timeline…</p>;
  if (!events.length) return <p className="mt-2 text-xs text-muted">No events were saved for this task.</p>;
  return (
    <ol className="mt-3 space-y-1 border-l border-line pl-3">
      {events.map((e) => (
        <li key={e.id} className="text-xs text-muted"><span className="font-mono text-text">{e.event_type}</span>{e.message ? ` — ${e.message}` : ""}</li>
      ))}
    </ol>
  );
}

/** Saved history (Supabase, own tasks only - Row Level Security) when sign-in is configured; otherwise this backend session's tasks. */
export default function History() {
  const [signedIn, setSignedIn] = useState(Boolean(loadSession()));
  const [saved, setSaved] = useState<PersistedTask[] | null>(null);
  const [live, setLive] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const persisted = authEnabled();

  useEffect(() => onSession((s) => setSignedIn(Boolean(s))), []);
  useEffect(() => {
    setError(null);
    if (persisted) {
      setLive(null);
      if (!signedIn) { setSaved(null); return; }
      getPersistedTasks().then(setSaved).catch((e) => setError(e.message));
    } else {
      getHistory().then(setLive).catch((e) => setError(e.message));
    }
  }, [persisted, signedIn]);

  const retry = () => retryPersistence()
    .then((r) => { setNote(`${r.retried_ok} pending write(s) saved.`); return getPersistedTasks().then(setSaved); })
    .catch((e) => setError(e.message));

  return (
    <main className="mx-auto max-w-6xl px-5 pb-16 pt-12 sm:px-8 lg:px-10">
      <div className="history-heading">
        <div><p className="section-kicker">{persisted ? "SAVED HISTORY" : "SESSION LOG"}</p><h1 className="text-3xl font-semibold tracking-tight text-text">Task history</h1>
          <p className="mt-2 text-sm text-muted">{persisted ? "Your saved browser runs. Only you can see them." : "Recent browser runs from this backend session."}</p></div>
        {persisted && signedIn && <button type="button" className="nav-button" onClick={retry}>Retry unsaved</button>}
      </div>
      {error && <p className="submit-error mt-6">{error}</p>}
      {note && <p className="mt-3 text-xs text-muted">{note}</p>}
      {persisted && !signedIn && <div className="empty-history">Sign in to see your saved history.</div>}
      {saved && saved.length === 0 && <div className="empty-history">No saved tasks yet.</div>}
      {live && live.length === 0 && <div className="empty-history">No tasks yet — run one from the console.</div>}
      <div className="mt-8 space-y-3">
        {saved?.map((t) => (
          <div key={t.id} className="history-card flex-col items-stretch">
            <button type="button" className="text-left" aria-expanded={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)}>
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill status={t.status} />
                <span className="meta-pill">{VERIFICATION_LABEL[t.verification_status ?? ""] ?? "Not verified"}</span>
                {t.provider && <span className="meta-pill">{PROVIDER_LABEL[t.provider] ?? t.provider}</span>}
                <span className="text-xs text-muted">{new Date(t.created_at).toLocaleString()}</span>
              </div>
              <p className="mt-2 truncate text-sm text-text">{t.instruction}</p>
              {t.result_summary && <p className="mt-1 truncate text-xs text-muted">{t.result_summary}</p>}
            </button>
            {open === t.id && <Timeline id={t.id} />}
          </div>
        ))}
        {live?.map((task) => (
          <div key={task.id} className="history-card">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2"><StatusPill status={task.status} />
                {task.verification_status && <span className="meta-pill">{VERIFICATION_LABEL[task.verification_status] ?? task.verification_status}</span>}
                <span className="text-xs text-muted">{timeAgo(task.created_at)}</span>
                {task.provider_used && <span className="meta-pill">{PROVIDER_LABEL[task.provider_used] ?? task.provider_used}</span>}
                {task.persistence && <span className="meta-pill">{PERSISTENCE_LABEL[task.persistence.status] ?? task.persistence.status}</span>}</div>
              <p className="mt-2 truncate text-sm text-text">{task.instruction}</p>
              <p className="mt-1 truncate font-mono text-xs text-muted">{task.final_url || task.error || "—"}</p>
            </div>
            <div className="hidden shrink-0 items-center gap-1.5 text-xs text-muted sm:flex"><Clock3 className="h-3.5 w-3.5" />{typeof task.duration_ms === "number" ? `${(task.duration_ms / 1000).toFixed(1)}s` : "—"}</div>
          </div>
        ))}
      </div>
    </main>
  );
}
