import { motion } from "framer-motion";
import { CheckCircle2, Clock3, ExternalLink, XCircle, Cpu } from "lucide-react";
import type { Task } from "../types";
import BrowserPreview from "./BrowserPreview";
import StatusPill from "./StatusPill";
import AgentStatus from "./AgentStatus";
import AutomationChart from "./AutomationChart";
import GoalPanel from "./GoalPanel";
import ExecutionTimeline from "./ExecutionTimeline";
import PendingPrompt from "./PendingPrompt";
import { recordedActionCount } from "../lib/execution";
import { handledBy, isActive, isPaused, OUTCOME_LABEL, PERSISTENCE_LABEL, VERIFICATION_LABEL } from "../lib/tasks";

const SOURCE_LABEL: Record<string, string> = {
  fast_path: "Fast path",
  workflow: "Saved workflow",
  ai: "AI-planned step",
  fallback: "Direct link",
};

interface Props {
  task: Task;
  onCancel: () => void;
  onRespond: (reply: { approve?: boolean; answer?: string }) => void;
}

export default function ExecutionView({ task, onCancel, onRespond }: Props) {
  const running = isActive(task.status);
  const paused = isPaused(task.status);
  const success = task.status === "success";

  return (
    <motion.div
      initial={{ opacity: 0, y: 18 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      className="mt-8 space-y-5"
    >
      <div className="execution-header">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill status={task.status} />
            {task.outcome && !running && <span className={`meta-pill outcome-${task.outcome}`} title="Derived from recorded browser evidence">{OUTCOME_LABEL[task.outcome] ?? task.outcome}</span>}
            {task.plan_source && <span className="meta-pill">{SOURCE_LABEL[task.plan_source] ?? task.plan_source}</span>}
          </div>
          <p className="mt-2 truncate text-sm text-muted">{task.instruction}</p>
          {task.understanding?.objective && task.understanding.objective !== task.instruction && (
            <p className="mt-1 truncate text-xs text-muted" title="How NEXA understood the request">Understood as: {task.understanding.objective}</p>
          )}
          <p className="mt-1 inline-flex items-center gap-1 text-xs text-muted"><Cpu className="h-3 w-3" />{handledBy(task)}</p>
          {(task.verification_status || task.persistence) && (
            <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
              {task.verification_status && <span className="meta-pill">{VERIFICATION_LABEL[task.verification_status] ?? task.verification_status}</span>}
              {task.persistence && <span className="meta-pill">{PERSISTENCE_LABEL[task.persistence.status] ?? task.persistence.status}</span>}
            </p>
          )}
        </div>
        {(running || paused) && (
          <button type="button" onClick={onCancel} className="cancel-button">Cancel</button>
        )}
      </div>

      {paused && task.pending && <PendingPrompt key={`${task.id}-${task.pending.question}`} pending={task.pending} onRespond={onRespond} />}

      {(running || paused || (task.events ?? []).length > 0) && <AgentStatus task={task} />}

      <BrowserPreview task={task} />

      {!running && !paused && (
        <div className={`result-card ${success ? "result-success" : "result-error"}`}>
          {success ? <CheckCircle2 className="h-5 w-5 shrink-0" /> : <XCircle className="h-5 w-5 shrink-0" />}
          <div className="min-w-0 flex-1">
            <p className="font-medium">{success ? task.message || "Task completed." : task.error || "Task did not complete."}</p>
            {success && task.result && <p className="mt-2 rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm font-semibold">Result: {task.result}</p>}
            {task.final_title && <p className="mt-1 truncate text-xs opacity-70">{task.final_title}</p>}
          </div>
          {task.final_url && (
            <button type="button" onClick={async () => { await navigator.clipboard?.writeText(task.final_url || ""); }} className="result-url" title="Copy final URL">
              <ExternalLink className="h-4 w-4" />
            </button>
          )}
        </div>
      )}

      <GoalPanel task={task} />
      <AutomationChart task={task} />
      <ExecutionTimeline task={task} />

      <div className="flex items-center justify-between text-xs text-muted">
        <div className="flex items-center gap-4">
          {typeof task.duration_ms === "number" && <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{(task.duration_ms / 1000).toFixed(1)}s</span>}
          {(() => {
            const n = recordedActionCount(task);
            return n === null ? null : <span>{n} browser action{n === 1 ? "" : "s"}</span>;
          })()}
          {task.retries > 0 && <span>{task.retries} retry</span>}
        </div>
      </div>
    </motion.div>
  );
}
