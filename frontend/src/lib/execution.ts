import type { GoalInfo, GoalStatus, Task, TaskEvent, TaskStats } from "../types";

/** Everything here is derived from recorded backend events. Nothing is estimated. */

export type Phase =
  | "WAITING"
  | "PLANNING"
  | "BROWSER_ACTION"
  | "OBSERVING"
  | "VERIFYING"
  | "RECOVERY"
  | "PROVIDER_UNAVAILABLE"
  | "UNDERSTANDING"
  | "AWAITING_USER"
  | "BLOCKED"
  | "COMPLETED"
  | "FAILED"
  | "TIMEOUT";

export const PHASE_LABEL: Record<Phase, string> = {
  WAITING: "WAITING FOR BACKEND EVENTS",
  PLANNING: "AI PLANNING",
  BROWSER_ACTION: "BROWSER ACTION",
  OBSERVING: "OBSERVING PAGE",
  VERIFYING: "VERIFYING",
  RECOVERY: "RECOVERY MODE",
  PROVIDER_UNAVAILABLE: "AI PROVIDER UNAVAILABLE",
  UNDERSTANDING: "UNDERSTANDING REQUEST",
  AWAITING_USER: "WAITING FOR YOU",
  BLOCKED: "BLOCKED (YOU CAN TAKE OVER)",
  COMPLETED: "TASK COMPLETED",
  FAILED: "TASK FAILED",
  TIMEOUT: "TASK TIMED OUT",
};

export function derivePhase(events: TaskEvent[]): { phase: Phase; detail: string } {
  if (!events.length) return { phase: "WAITING", detail: "No events received yet." };
  const last = events[events.length - 1];
  switch (last.type) {
    case "TASK_SUCCESS":
      return { phase: "COMPLETED", detail: last.message ?? "" };
    case "TASK_FAILURE":
      return { phase: "FAILED", detail: last.error ?? last.message ?? "" };
    case "TASK_TIMEOUT":
      return { phase: "TIMEOUT", detail: last.error ?? last.message ?? "" };
    case "ACTION_START":
      return { phase: "BROWSER_ACTION", detail: `${last.action ?? "action"} ${last.target ?? ""}`.trim() };
    case "ACTION_RESULT":
    case "OBSERVE":
    case "BROWSER_READY":
      return { phase: "OBSERVING", detail: last.url ?? "" };
    case "VERIFICATION":
    case "PROGRESS":
      return { phase: "VERIFYING", detail: last.reason ?? last.classification ?? "" };
    case "RECOVERY":
      return { phase: "RECOVERY", detail: `${last.strategy ?? ""} ${last.reason ?? ""}`.trim() };
    case "GOAL_START":
      return { phase: "OBSERVING", detail: `goal ${last.index ?? ""}/${last.total ?? ""}: ${last.label ?? ""}` };
    case "GOAL_COMPLETE":
      return { phase: "VERIFYING", detail: `goal verified: ${last.label ?? ""}` };
    case "UNDERSTANDING":
      return { phase: "UNDERSTANDING", detail: String(last.objective ?? last.intent ?? "") };
    case "CONFIRMATION_REQUESTED":
    case "INPUT_REQUESTED":
      return { phase: "AWAITING_USER", detail: String(last.question ?? last.description ?? last.reason ?? "") };
    case "GOAL_BLOCKED":
      return { phase: "BLOCKED", detail: `${last.label ?? ""}: ${last.reason ?? ""}` };
    case "GOAL_FAILED":
      return { phase: "FAILED", detail: `${last.label ?? ""}: ${last.reason ?? ""}` };
    case "NEW_PAGE_DETECTED":
    case "PAGE_ADOPTED":
    case "ACTIVE_PAGE_CHANGED":
      return { phase: "OBSERVING", detail: `active tab: ${last.url ?? ""}` };
    case "PROVIDER_STATUS":
      if (last.state === "calling") return { phase: "PLANNING", detail: `${last.provider ?? "provider"} (timeout ${last.timeout_s ?? "?"}s)` };
      if (last.state === "unavailable" || last.state === "skipped")
        return { phase: "PROVIDER_UNAVAILABLE", detail: `${last.provider ?? "provider"}: ${last.reason ?? last.failure ?? ""}` };
      return { phase: "PLANNING", detail: last.reason ?? "" };
    case "AI_PLAN":
      return last.result === "ok"
        ? { phase: "PLANNING", detail: `${last.provider} planned in ${last.duration_ms ?? "?"} ms` }
        : { phase: "PROVIDER_UNAVAILABLE", detail: `${last.provider}: ${last.result} ${last.error ?? ""}`.trim() };
    default:
      return { phase: "OBSERVING", detail: last.url ?? "" };
  }
}

export interface ProviderView {
  name: string;
  role: "primary" | "emergency" | "other";
  state: "idle" | "calling" | "ok" | "unavailable" | "degraded" | "skipped" | "cooldown";
  reason: string;
  calls: number;
  failures: number;
  lastLatencyMs: number | null;
}

export function providerViews(events: TaskEvent[]): ProviderView[] {
  const map = new Map<string, ProviderView>();
  const get = (name: string): ProviderView => {
    let v = map.get(name);
    if (!v) {
      v = { name, role: name === "gemini" ? "primary" : name === "ollama" ? "emergency" : "other", state: "idle", reason: "", calls: 0, failures: 0, lastLatencyMs: null };
      map.set(name, v);
    }
    return v;
  };
  for (const e of events) {
    if (!e.provider || e.provider === "auto") continue;
    if (e.type === "AI_PLAN" && typeof e.duration_ms === "number") {
      const v = get(e.provider);
      v.calls += 1;
      v.lastLatencyMs = e.duration_ms;
      if (e.result === "ok") {
        v.state = "ok";
        v.reason = "";
      } else {
        v.failures += 1;
        v.reason = `${e.result}${e.error ? `: ${e.error}` : ""}`;
      }
    } else if (e.type === "PROVIDER_STATUS" && e.state) {
      const v = get(e.provider);
      if (e.state === "calling") v.state = "calling";
      else if (e.state === "unavailable" || e.state === "degraded" || e.state === "skipped") {
        v.state = e.state;
        v.reason = e.reason ?? e.failure ?? v.reason;
      }
    }
  }
  return ["gemini", "ollama"].map((n) => get(n)).concat([...map.values()].filter((v) => v.role === "other"));
}

export interface StepRow {
  step: number;
  chips: { kind: "observe" | "plan" | "action" | "progress" | "verify" | "recovery" | "provider" | "route"; label: string; tone: "ok" | "warn" | "bad" | "info"; detail?: string }[];
}

const PROGRESS_TONE: Record<string, "ok" | "warn" | "bad"> = {
  PROGRESS: "ok",
  NO_PROGRESS: "warn",
  PAGE_CHANGED_NO_GOAL_PROGRESS: "warn",
  REPEATED_ACTION: "bad",
  WRONG_STATE: "bad",
  ACTION_FAILED: "bad",
};

/** Group events by adaptive step so the chart shows what actually happened, in order. */
export function buildSteps(events: TaskEvent[]): { pre: StepRow["chips"]; steps: StepRow[]; end: StepRow["chips"] } {
  const pre: StepRow["chips"] = [];
  const end: StepRow["chips"] = [];
  const byStep = new Map<number, StepRow>();
  const row = (n: number) => {
    let r = byStep.get(n);
    if (!r) byStep.set(n, (r = { step: n, chips: [] }));
    return r;
  };
  for (const e of events) {
    const n = typeof e.step === "number" && e.step > 0 ? e.step : 0;
    const target = n === 0 && e.type !== "OBSERVE" ? pre : row(n).chips;
    switch (e.type) {
      case "TASK_START":
        pre.push({ kind: "route", label: "TASK", tone: "info" });
        break;
      case "BROWSER_READY":
        pre.push({ kind: "route", label: "BROWSER", tone: "info", detail: e.url });
        break;
      case "ROUTER":
        target.push({ kind: "route", label: `ROUTER: ${e.route ?? "?"}`, tone: "info", detail: e.reason });
        break;
      case "OBSERVE":
        row(n).chips.push({ kind: "observe", label: "OBSERVE", tone: "info", detail: e.url });
        break;
      case "PROVIDER_STATUS":
        if (e.state === "unavailable" || e.state === "skipped" || e.state === "degraded")
          target.push({ kind: "provider", label: `${(e.provider ?? "?").toUpperCase()} ${(e.failure ?? e.state ?? "").toUpperCase()}`, tone: "bad", detail: e.reason });
        break;
      case "AI_PLAN":
        target.push({
          kind: "plan",
          label: `${(e.provider ?? "?").toUpperCase()} ${e.result === "ok" ? "" : (e.result ?? "").toUpperCase()} ${e.duration_ms ?? "?"}ms`.replace(/\s+/g, " ").trim(),
          tone: e.result === "ok" ? "ok" : "bad",
          detail: e.error,
        });
        break;
      case "ACTION_START":
        break;
      case "ACTION_RESULT":
        target.push({
          kind: "action",
          label: `${e.action ?? "action"}${e.target ? ` → ${e.target}` : ""} ${e.duration_ms ?? "?"}ms`,
          tone: e.result === "ok" ? "ok" : "bad",
          detail: e.error,
        });
        break;
      case "PROGRESS":
        target.push({ kind: "progress", label: e.classification ?? "PROGRESS", tone: PROGRESS_TONE[e.classification ?? ""] ?? "info", detail: e.reason });
        break;
      case "VERIFICATION":
        target.push({ kind: "verify", label: `VERIFY: ${e.result ?? "?"}${e.claimed_done ? " (AI claimed done)" : ""}`, tone: e.result === "complete" ? "ok" : "warn", detail: e.reason });
        break;
      case "RECOVERY":
        target.push({ kind: "recovery", label: `RECOVERY L${e.level ?? "?"}: ${e.strategy ?? ""}`, tone: "warn", detail: e.reason });
        break;
      case "TASK_SUCCESS":
        end.push({ kind: "verify", label: "SUCCESS", tone: "ok", detail: e.message });
        break;
      case "TASK_FAILURE":
        end.push({ kind: "verify", label: "FAILURE", tone: "bad", detail: e.error });
        break;
      case "TASK_TIMEOUT":
        end.push({ kind: "verify", label: "TIMEOUT", tone: "bad", detail: e.error });
        break;
    }
  }
  return { pre, steps: [...byStep.values()].filter((r) => r.step > 0 && r.chips.length).sort((a, b) => a.step - b.step), end };
}

/** Goal list with live statuses, rebuilt ONLY from GOAL_* events the backend recorded. */
export function goalViews(events: TaskEvent[]): { goals: GoalInfo[]; current: GoalInfo | null; lastVerification: string } {
  const plan = events.find((e) => e.type === "GOAL_PLAN");
  const goals: GoalInfo[] = (plan?.goals ?? []).map((g) => ({ ...g, status: "PENDING" as GoalStatus }));
  let lastVerification = "";
  for (const e of events) {
    const g = goals.find((x) => x.id === e.goal);
    if (e.type === "VERIFICATION" && e.reason) lastVerification = String(e.reason);
    if (!g) continue;
    if (e.type === "GOAL_START") g.status = "IN_PROGRESS";
    else if (e.type === "GOAL_COMPLETE") g.status = "COMPLETED";
    else if (e.type === "GOAL_FAILED") g.status = "FAILED";
    else if (e.type === "GOAL_BLOCKED") g.status = "BLOCKED";
  }
  const current = goals.find((g) => g.status === "IN_PROGRESS") ?? null;
  return { goals, current, lastVerification };
}

export function activePageSummary(events: TaskEvent[]): { url: string; pageId?: number; pageCount?: number } | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "OBSERVE" && e.url) return { url: e.url, pageId: e.page_id, pageCount: e.page_count };
  }
  return null;
}

export function statValue(v: number | null | undefined, suffix = ""): string {
  return typeof v === "number" ? `${v}${suffix}` : "—";
}

export function isStats(s: Task["stats"]): s is TaskStats {
  return !!s && typeof (s as TaskStats).ai_calls === "number";
}

/** Browser-action count: the recorded number, never a guess. Null when unknown. */
export function recordedActionCount(task: Task): number | null {
  if (isStats(task.stats)) return task.stats.browser_actions;
  return task.status === "created" || task.status === "running" ? null : task.actions_count;
}

export function formatMs(ms: number): string {
  return `${(ms / 1000).toFixed(1).padStart(5, "0")}s`;
}
