import type { HealthStatus } from "../types";

/**
 * Backend / browser / AI-provider / task status are INDEPENDENT.
 * - "Backend offline" requires several consecutive failed health checks, never a single
 *   slow poll, and never a provider failure.
 * - A provider being unavailable only changes the AI row.
 */
export type BackendState = "checking" | "connected" | "offline";
export type BrowserStateLabel = "unknown" | "starting" | "ready";
export type ProviderState = "available" | "unavailable" | "cooldown" | "unconfigured" | "unknown";

export interface SystemStatus {
  backend: BackendState;
  browser: BrowserStateLabel;
  gemini: { state: ProviderState; reason: string; model?: string | null };
  ollama: { state: ProviderState; reason: string; model?: string | null };
  /** every OTHER provider the backend reports as configured (openai / claude / grok) */
  extra: Record<string, { state: ProviderState; reason: string; model?: string | null }>;
  consecutiveFailures: number;
}

export const OFFLINE_AFTER_FAILURES = 3;

export const initialStatus: SystemStatus = {
  backend: "checking",
  browser: "unknown",
  gemini: { state: "unknown", reason: "" },
  ollama: { state: "unknown", reason: "" },
  extra: {},
  consecutiveFailures: 0,
};

export function applyHealthResult(prev: SystemStatus, result: HealthStatus | Error): SystemStatus {
  if (result instanceof Error) {
    const failures = prev.consecutiveFailures + 1;
    return {
      ...prev,
      consecutiveFailures: failures,
      // keep the last known state until the failure is clearly persistent
      backend: failures >= OFFLINE_AFTER_FAILURES ? "offline" : prev.backend === "checking" ? "checking" : prev.backend,
      browser: failures >= OFFLINE_AFTER_FAILURES ? "unknown" : prev.browser,
    };
  }
  const p = result.providers;
  const extra: SystemStatus["extra"] = {};
  if (p) for (const [n, v] of Object.entries(p)) {
    if (n === "gemini" || n === "ollama" || !v.configured) continue;
    extra[n] = { state: v.state, reason: v.reason, model: v.model };
  }
  return {
    extra,
    backend: "connected",
    consecutiveFailures: 0,
    browser: result.browser_ready ? "ready" : "starting",
    gemini: p?.gemini ? { state: p.gemini.state, reason: p.gemini.reason, model: p.gemini.model } : { state: "unknown", reason: "" },
    ollama: p?.ollama ? { state: p.ollama.state, reason: p.ollama.reason, model: p.ollama.model } : { state: "unknown", reason: "" },
  };
}

const STATE_WORD: Record<ProviderState, string> = {
  available: "available", unavailable: "unavailable", cooldown: "cooling down", unconfigured: "not configured", unknown: "unknown",
};

export function describeStatus(s: SystemStatus): { label: string; ok: boolean }[] {
  const prov = (name: string, v: SystemStatus["gemini"]) => ({
    label: `${name}: ${STATE_WORD[v.state]}`,
    ok: v.state === "available",
  });
  const names: Record<string, string> = { groq: "Groq", openai: "OpenAI", claude: "Claude", grok: "Grok" };
  return [
    { label: s.backend === "checking" ? "Backend: checking" : s.backend === "connected" ? "Backend: connected" : "Backend: offline", ok: s.backend === "connected" },
    { label: s.browser === "ready" ? "Browser: ready" : s.browser === "starting" ? "Browser: starting" : "Browser: unknown", ok: s.browser === "ready" },
    prov("Gemini", s.gemini),
    prov("Ollama", s.ollama),
    ...Object.entries(s.extra).map(([n, v]) => prov(names[n] ?? n, v)),
  ];
}
