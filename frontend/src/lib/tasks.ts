import type { ProviderHealthInfo, ProviderSelection, ProvidersResponse, Task, TaskStatus } from "../types";

/** A paused task is NOT finished: it is waiting for the user and keeps its live tab. */
export const PAUSED: ReadonlySet<string> = new Set(["awaiting_confirmation", "needs_input"]);
export const ACTIVE: ReadonlySet<string> = new Set(["created", "running"]);

export const isPaused = (s: TaskStatus | "idle"): boolean => PAUSED.has(s);
export const isActive = (s: TaskStatus | "idle"): boolean => ACTIVE.has(s);
/** Terminal = nothing more will happen without a new task. */
export const isTerminal = (s: TaskStatus | "idle"): boolean => s !== "idle" && !ACTIVE.has(s) && !PAUSED.has(s);
/** The input must stay locked while a task is running OR paused. */
export const isBusy = (s: TaskStatus | "idle"): boolean => ACTIVE.has(s) || PAUSED.has(s);

export const PROVIDER_LABEL: Record<string, string> = {
  auto: "Auto", groq: "Groq", gemini: "Gemini", ollama: "Ollama (local)", openai: "OpenAI", claude: "Claude", grok: "Grok",
};

export interface ProviderChoice {
  value: ProviderSelection;
  label: string;
  disabled: boolean;
  hint: string;
}

/**
 * Options for the selector. A provider is only selectable when the backend reports it configured;
 * a cooling-down provider stays selectable (the backend will say so honestly) but is labelled.
 * Hosted providers are flagged as possibly billed - NEXA never calls any provider "free".
 */
export function providerChoices(resp: ProvidersResponse | null): ProviderChoice[] {
  const order = ["groq", "gemini", "ollama", "openai", "claude", "grok"];
  const out: ProviderChoice[] = [{
    value: "auto", label: "Auto", disabled: false,
    hint: resp ? `Tries: ${resp.auto_order.map((n) => PROVIDER_LABEL[n] ?? n).join(" → ") || "none configured"}` : "Server default order",
  }];
  for (const n of order) {
    const p: ProviderHealthInfo | undefined = resp?.providers?.[n];
    const selectable = !!p && p.selectable !== false && p.configured;
    let hint = !p ? "status unknown" : !p.configured ? p.reason || "not configured" : p.state === "cooldown" ? p.reason : p.model ? `model ${p.model}` : "";
    if (p?.may_bill && p.configured) hint += `${hint ? " · " : ""}hosted - may be billed`;
    out.push({ value: n as ProviderSelection, label: PROVIDER_LABEL[n] ?? n, disabled: !selectable, hint });
  }
  return out;
}

const KNOWN = ["groq", "gemini", "ollama", "openai", "claude", "grok"];

/** Which provider actually answered: the recorded final value, else the latest successful AI call event. */
export function providerUsed(task: Pick<Task, "provider_used" | "events">): string | null {
  if (task.provider_used) return task.provider_used;
  const ev = [...(task.events ?? [])].reverse().find((e) => e.type === "AI_PLAN" && e.result === "ok" && KNOWN.includes(String(e.provider)));
  return ev ? String(ev.provider) : null;
}

/** The provider line shown on a task: what actually answered, never what was merely selected. */
export function handledBy(task: Pick<Task, "provider" | "provider_used" | "events" | "status">): string {
  const used = providerUsed(task);
  const model = [...(task.events ?? [])].reverse().find((e) => e.type === "AI_PLAN" && e.result === "ok" && e.provider === used)?.model;
  if (used) return `Handled by ${PROVIDER_LABEL[used] ?? used}${model ? ` · ${String(model)}` : ""}`;
  const sel = task.provider && task.provider !== "auto" ? ` (selected: ${PROVIDER_LABEL[task.provider] ?? task.provider})` : "";
  if (isActive(task.status) || isPaused(task.status)) return `No AI provider used so far${sel}`;
  return `No AI provider was needed${sel}`;
}

export const OUTCOME_LABEL: Record<string, string> = {
  success: "Verified complete",
  partial: "Partly done - the full goal was not reached",
  blocked: "Blocked - needs you (sign-in, CAPTCHA or similar)",
  failed: "Not achieved",
  cancelled: "Cancelled",
  awaiting_user: "Waiting for you",
};

/** Honest wording for the backend verdict. "Completed" is only ever used for VERIFIED_COMPLETE. */
export const VERIFICATION_LABEL: Record<string, string> = {
  PENDING: "Pending", RUNNING: "Running", ACTION_SUCCEEDED: "Finished - not verified", VERIFIED_COMPLETE: "Completed - verified",
  BLOCKED: "Blocked", FAILED: "Failed", CANCELLED: "Cancelled",
};
export const PERSISTENCE_LABEL: Record<string, string> = {
  disabled: "History: this session only", pending: "Saving history…", saved: "History saved", failed: "History NOT saved",
};
