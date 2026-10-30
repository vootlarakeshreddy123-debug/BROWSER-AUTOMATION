import type { TaskStatus } from "../types";

const CONFIG: Record<TaskStatus | "idle", { label: string; dot: string; text: string }> = {
  idle: { label: "Idle", dot: "bg-muted", text: "text-muted" },
  created: { label: "Starting", dot: "bg-signal", text: "text-signal" },
  running: { label: "Running", dot: "bg-signal animate-pulse", text: "text-signal" },
  success: { label: "Completed", dot: "bg-success", text: "text-success" },
  failed: { label: "Failed", dot: "bg-danger", text: "text-danger" },
  cancelled: { label: "Cancelled", dot: "bg-warn", text: "text-warn" },
  timeout: { label: "Timed out", dot: "bg-danger", text: "text-danger" },
  awaiting_confirmation: { label: "Awaiting confirmation", dot: "bg-warn animate-pulse", text: "text-warn" },
  needs_input: { label: "Needs your input", dot: "bg-warn animate-pulse", text: "text-warn" },
};

export default function StatusPill({ status }: { status: TaskStatus | "idle" }) {
  const cfg = CONFIG[status] ?? CONFIG.idle;
  return (
    <span className="inline-flex items-center gap-2 text-sm font-medium">
      <span className={`h-2 w-2 rounded-full ${cfg.dot}`} />
      <span className={cfg.text}>{cfg.label}</span>
    </span>
  );
}
