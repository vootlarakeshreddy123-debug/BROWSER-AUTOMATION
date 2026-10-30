import { AlertTriangle, CheckCircle2, Cpu, Eye, Globe, RefreshCw, ShieldCheck, XCircle } from "lucide-react";
import type { Task, TaskEvent } from "../types";
import { formatMs } from "../lib/execution";

function icon(e: TaskEvent) {
  switch (e.type) {
    case "AI_PLAN":
    case "PROVIDER_STATUS":
      return <Cpu className="h-3.5 w-3.5" />;
    case "ACTION_START":
    case "ACTION_RESULT":
    case "BROWSER_READY":
      return <Globe className="h-3.5 w-3.5" />;
    case "OBSERVE":
      return <Eye className="h-3.5 w-3.5" />;
    case "VERIFICATION":
    case "PROGRESS":
      return <ShieldCheck className="h-3.5 w-3.5" />;
    case "RECOVERY":
      return <RefreshCw className="h-3.5 w-3.5" />;
    case "CONFIRMATION_REQUESTED":
    case "INPUT_REQUESTED":
    case "GOAL_BLOCKED":
      return <ShieldCheck className="h-3.5 w-3.5" />;
    case "TASK_SUCCESS":
      return <CheckCircle2 className="h-3.5 w-3.5" />;
    case "TASK_FAILURE":
    case "TASK_TIMEOUT":
      return <XCircle className="h-3.5 w-3.5" />;
    default:
      return <AlertTriangle className="h-3.5 w-3.5" />;
  }
}

function describe(e: TaskEvent): string {
  const bits: string[] = [];
  if (e.provider) bits.push(e.provider);
  if (e.state) bits.push(e.state);
  if (e.route) bits.push(`route=${e.route}`);
  if (e.action) bits.push(String(e.action));
  if (e.target) bits.push(`→ ${e.target}`);
  if (e.classification) bits.push(e.classification);
  if (e.strategy) bits.push(`L${e.level ?? "?"} ${e.strategy}`);
  if (e.result) bits.push(`result=${e.result}`);
  if (typeof e.duration_ms === "number") bits.push(`${e.duration_ms}ms`);
  if (e.failure) bits.push(`failure=${e.failure}`);
  const extra = e.error || e.reason || e.message || (e.question as string | undefined) || (e.objective as string | undefined) || e.url;
  return `${bits.join(" ")}${extra ? ` — ${extra}` : ""}`;
}

/** Raw, ordered, timestamped event log - exactly what the backend recorded. */
export default function ExecutionTimeline({ task }: { task: Task }) {
  const events = task.events ?? [];
  if (!events.length) return null;
  return (
    <section className="panel" aria-label="Execution timeline">
      <h3 className="panel-title">Execution timeline</h3>
      <ol className="timeline">
        {events.map((e) => (
          <li key={e.seq} className={`tl-row tl-${e.type.toLowerCase()}`}>
            <span className="tl-time">{formatMs(e.elapsed_ms)}</span>
            <span className="tl-icon">{icon(e)}</span>
            <span className="tl-type">{e.type}</span>
            <span className="tl-text">{describe(e)}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
