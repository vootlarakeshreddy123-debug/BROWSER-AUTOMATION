import { Loader2 } from "lucide-react";
import type { Task } from "../types";
import { isActive } from "../lib/tasks";
import { PHASE_LABEL, derivePhase, providerViews, recordedActionCount } from "../lib/execution";

const STATE_TEXT: Record<string, string> = {
  idle: "not used yet",
  calling: "calling…",
  ok: "responding",
  unavailable: "unavailable",
  degraded: "degraded",
  skipped: "skipped",
  cooldown: "cooling down",
};

/** Live agent state + provider status. Replaces the old fixed "Opening your live Chrome tab" banner. */
export default function AgentStatus({ task }: { task: Task }) {
  const running = isActive(task.status);
  const events = task.events ?? [];
  const { phase, detail } = derivePhase(events);
  const providers = providerViews(events);
  const actions = recordedActionCount(task);

  return (
    <div className="agent-progress" role="status" aria-live="polite">
      <div className="flex items-center gap-3">
        <div className="progress-orb">{running && <Loader2 className="h-4 w-4 animate-spin" />}</div>
        <div className="min-w-0">
          <p className="text-sm font-medium text-text">{PHASE_LABEL[phase]}</p>
          <p className="truncate text-xs text-muted">{detail || (running ? "Waiting for the first backend event…" : "")}</p>
        </div>
      </div>
      <div className="provider-row">
        {providers.map((p) => (
          <span key={p.name} className={`provider-pill provider-${p.state}`} title={p.reason || undefined}>
            <b>{p.name}</b> <i>({p.role})</i> · {STATE_TEXT[p.state]}
            {p.calls > 0 && ` · ${p.calls} call${p.calls === 1 ? "" : "s"}`}
            {p.lastLatencyMs !== null && ` · last ${p.lastLatencyMs}ms`}
          </span>
        ))}
        <span className="provider-pill">browser actions: {actions === null ? "—" : actions}</span>
      </div>
    </div>
  );
}
