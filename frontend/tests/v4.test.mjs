// V4 frontend logic tests.  Run:  node --experimental-strip-types --test tests/*.test.mjs   (from frontend/)
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { OUTCOME_LABEL, handledBy, isActive, isBusy, isPaused, isTerminal, providerChoices, providerUsed } from "../src/lib/tasks.ts";
import { applyHealthResult, describeStatus, initialStatus } from "../src/lib/status.ts";
import { derivePhase } from "../src/lib/execution.ts";

const resp = {
  default_selection: "auto", selectable: ["auto", "gemini", "ollama"], auto_order: ["gemini", "ollama"],
  providers: {
    gemini: { configured: true, state: "available", reason: "", model: "gemini-x", may_bill: true, selectable: true },
    ollama: { configured: true, state: "available", reason: "", model: "qwen3:4b", may_bill: false, selectable: true },
    openai: { configured: false, state: "unconfigured", reason: "OPENAI_API_KEY is not set", selectable: false },
    claude: { configured: true, state: "cooldown", reason: "cooling down (120s left)", model: "m", may_bill: true, selectable: true },
    grok: { configured: false, state: "unconfigured", reason: "XAI_API_KEY is not set", selectable: false },
  },
};

test("paused states are not terminal and keep the input locked", () => {
  for (const s of ["awaiting_confirmation", "needs_input"]) {
    assert.ok(isPaused(s)); assert.ok(!isTerminal(s)); assert.ok(isBusy(s)); assert.ok(!isActive(s));
  }
  for (const s of ["success", "failed", "cancelled", "timeout"]) assert.ok(isTerminal(s) && !isBusy(s));
  assert.ok(isBusy("running") && isBusy("created") && !isBusy("idle") && !isTerminal("idle"));
});

test("selector: unconfigured providers are disabled, Auto always selectable, hosted flagged as billable", () => {
  const c = Object.fromEntries(providerChoices(resp).map((x) => [x.value, x]));
  assert.equal(c.auto.disabled, false);
  assert.match(c.auto.hint, /Gemini → Ollama/);
  assert.equal(c.openai.disabled, true); assert.equal(c.grok.disabled, true);
  assert.equal(c.gemini.disabled, false); assert.match(c.gemini.hint, /may be billed/);
  assert.doesNotMatch(c.ollama.hint, /billed/);
  assert.equal(c.claude.disabled, false); assert.match(c.claude.hint, /cooling down/);
  assert.ok(!/free/i.test(JSON.stringify(c)), "no provider is ever promised to be free");
});

test("selector before the status loads: only Auto is selectable", () => {
  const c = providerChoices(null);
  assert.equal(c.find((x) => x.value === "auto").disabled, false);
  assert.ok(c.filter((x) => x.value !== "auto").every((x) => x.disabled));
});

test("handled-by shows the provider that ANSWERED, not the one selected", () => {
  const events = [
    { seq: 1, type: "AI_PLAN", provider: "gemini", result: "quota", duration_ms: 5 },
    { seq: 2, type: "AI_PLAN", provider: "ollama", result: "ok", duration_ms: 900 },
  ];
  const t = { provider: "gemini", provider_used: null, events, status: "running" };
  assert.equal(providerUsed(t), "ollama");
  assert.match(handledBy(t), /Ollama/);
  assert.match(handledBy({ ...t, provider_used: "claude" }), /Claude/);
  assert.match(handledBy({ provider: "gemini", provider_used: null, events: [], status: "success" }), /No AI provider was needed \(selected: Gemini\)/);
  assert.doesNotMatch(handledBy({ provider: "gemini", provider_used: null, events: [{ seq: 1, type: "AI_PLAN", provider: "gemini", result: "error" }], status: "failed" }), /Handled by/);
});

test("header: configured hosted providers get chips, unconfigured ones do not, cooldown is not 'offline'", () => {
  const s = applyHealthResult(initialStatus, { status: "ok", ai_configured: true, browser_ready: true, providers: resp.providers });
  const labels = describeStatus(s).map((x) => x.label);
  assert.ok(labels.includes("Claude: cooling down"));
  assert.ok(!labels.some((l) => /OpenAI|Grok/.test(l)));
  assert.ok(labels.includes("Backend: connected"));
});

test("phases for the new events", () => {
  const p = (e) => derivePhase([{ seq: 1, ts: 0, elapsed_ms: 0, ...e }]).phase;
  assert.equal(p({ type: "CONFIRMATION_REQUESTED", question: "send?" }), "AWAITING_USER");
  assert.equal(p({ type: "INPUT_REQUESTED", question: "which Rahul?" }), "AWAITING_USER");
  assert.equal(p({ type: "UNDERSTANDING", objective: "x" }), "UNDERSTANDING");
  assert.equal(p({ type: "GOAL_BLOCKED", label: "x", reason: "login wall" }), "BLOCKED");
  assert.equal(p({ type: "GOAL_FAILED", label: "x", reason: "r" }), "FAILED");
});

function walk(dir) {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

test("no secrets in the frontend: no key patterns, no key env vars, no secret keys, no localStorage/URL key handling", () => {
  const files = walk("src").filter((f) => /\.(ts|tsx|css|html)$/.test(f)).concat(["index.html", "vite.config.ts"]);
  const bad = [];
  for (const f of files) {
    const t = readFileSync(f, "utf8");
    if (/\bAIza[0-9A-Za-z_-]{20,}|\bsk-[A-Za-z0-9_-]{16,}|\bxai-[A-Za-z0-9]{16,}/.test(t)) bad.push(`${f}: key-like string`);
    if (/VITE_[A-Z_]*(API_KEY|SECRET|TOKEN)/.test(t)) bad.push(`${f}: key in VITE_ env`);
    // only the auth module may keep the (short-lived) user session, and only in per-tab sessionStorage
    if (/localStorage/.test(t) || (/sessionStorage/.test(t) && !f.endsWith("auth.ts"))) bad.push(`${f}: browser storage used`);
    if (/(SERVICE_ROLE|SECRET_KEY|sb_secret_)/i.test(t)) bad.push(`${f}: secret key reference`);
    if (/[?&](api_?key|key|token)=/i.test(t)) bad.push(`${f}: key in URL`);
  }
  assert.deepEqual(bad, []);
});

test("outcome labels never call a partial or blocked run complete", () => {
  for (const k of ["success", "partial", "blocked", "failed", "cancelled", "awaiting_user"]) assert.ok(OUTCOME_LABEL[k]);
  assert.doesNotMatch(OUTCOME_LABEL.partial, /complete/i);
  assert.doesNotMatch(OUTCOME_LABEL.blocked, /complete|done/i);
  assert.match(OUTCOME_LABEL.success, /Verified/);
});

test("auth: token only in Authorization header / first WS message, never in a URL; publishable key only", () => {
  const api = readFileSync("src/services/api.ts", "utf8");
  const hook = readFileSync("src/hooks/useTaskExecution.ts", "utf8");
  const auth = readFileSync("src/services/auth.ts", "utf8");
  assert.match(api, /authHeaders\(\)/);
  assert.match(hook, /type: "auth"/);
  assert.doesNotMatch(hook, /ws\/tasks\/\$\{taskId\}\?/);
  assert.match(auth, /VITE_SUPABASE_PUBLISHABLE_KEY/);
});

test("groq is a known provider everywhere and verified wording never says Completed unless verified", () => {
  const tasks = readFileSync("src/lib/tasks.ts", "utf8");
  assert.match(tasks, /groq: "Groq"/);
  assert.match(tasks, /ACTION_SUCCEEDED: "Finished - not verified"/);
  assert.match(tasks, /VERIFIED_COMPLETE: "Completed - verified"/);
});
