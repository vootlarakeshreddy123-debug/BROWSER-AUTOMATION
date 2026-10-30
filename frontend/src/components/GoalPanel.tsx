import { CheckCircle2, CircleDashed, Loader2, ShieldAlert, XCircle } from "lucide-react";
import type { GoalStatus, Task } from "../types";
import { activePageSummary, goalViews } from "../lib/execution";

const ICON: Record<GoalStatus, JSX.Element> = {
  PENDING: <CircleDashed className="h-3.5 w-3.5 opacity-50" />,
  IN_PROGRESS: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
  COMPLETED: <CheckCircle2 className="h-3.5 w-3.5 text-success" />,
  BLOCKED: <ShieldAlert className="h-3.5 w-3.5 text-danger" />,
  FAILED: <XCircle className="h-3.5 w-3.5 text-danger" />,
};

/** Ordered goals with per-goal status, current action, active tab and last verification. Backend events only. */
export default function GoalPanel({ task }: { task: Task }) {
  const events = task.events ?? [];
  const { goals, current, lastVerification } = goalViews(events);
  if (!goals.length) return null;
  const page = activePageSummary(events);
  const lastAction = [...events].reverse().find((e) => e.type === "ACTION_START");
  return (
    <section className="panel" aria-label="Goals">
      <h3 className="panel-title">Goals</h3>
      <ol className="goal-list">
        {goals.map((g) => (
          <li key={g.id} className={`goal-row goal-${g.status.toLowerCase()}`}>
            <span className="goal-icon">{ICON[g.status]}</span>
            <span className="goal-text">{g.label}</span>
            <span className="goal-status">{g.status.replace("_", " ").toLowerCase()}</span>
          </li>
        ))}
      </ol>
      <dl className="stat-grid">
        <div><dt>Current goal</dt><dd>{current ? current.label : "—"}</dd></div>
        <div><dt>Current action</dt><dd>{lastAction ? `${lastAction.action ?? ""} ${lastAction.target ?? ""}`.trim() : "—"}</dd></div>
        <div className="stat-wide"><dt>Active page{page?.pageCount ? ` (${page.pageCount} tab${page.pageCount === 1 ? "" : "s"})` : ""}</dt><dd>{page?.url ?? "—"}</dd></div>
        <div className="stat-wide"><dt>Last verification</dt><dd>{lastVerification || "—"}</dd></div>
      </dl>
    </section>
  );
}
