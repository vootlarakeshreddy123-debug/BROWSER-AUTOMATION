// TEST 11 - backend / browser / AI-provider status are independent.
// Run:  node --experimental-strip-types --test tests/status.test.mjs   (from frontend/)
import test from "node:test";
import assert from "node:assert/strict";
import { applyHealthResult, describeStatus, initialStatus, OFFLINE_AFTER_FAILURES } from "../src/lib/status.ts";

const health = (over = {}) => ({
  status: "ok",
  ai_configured: true,
  browser_ready: true,
  providers: {
    gemini: { configured: true, state: "unavailable", reason: "cooling down after a failure (3000s left)", model: "gemini-3.8-flash" },
    ollama: { configured: true, state: "available", reason: "", model: "qwen3:4b" },
  },
  ...over,
});

test("backend 200 + Gemini unavailable => Backend connected (never offline)", () => {
  const s = applyHealthResult(initialStatus, health());
  assert.equal(s.backend, "connected");
  assert.equal(s.gemini.state, "unavailable");
  assert.equal(s.ollama.state, "available");
  const labels = describeStatus(s).map((x) => x.label);
  assert.ok(labels.includes("Backend: connected"));
  assert.ok(labels.includes("Gemini: unavailable"));
  assert.ok(!labels.some((l) => /offline/i.test(l)));
});

test("both providers unavailable still leaves backend connected and browser ready", () => {
  const h = health();
  h.providers.ollama.state = "unavailable";
  const s = applyHealthResult(initialStatus, h);
  assert.equal(s.backend, "connected");
  assert.equal(s.browser, "ready");
});

test("browser not ready is 'starting', not a backend problem", () => {
  const s = applyHealthResult(initialStatus, health({ browser_ready: false }));
  assert.equal(s.backend, "connected");
  assert.equal(s.browser, "starting");
});

test("a single failed poll does not flip to offline; persistent failures do", () => {
  let s = applyHealthResult(initialStatus, health());
  for (let i = 1; i < OFFLINE_AFTER_FAILURES; i++) {
    s = applyHealthResult(s, new Error("timeout"));
    assert.equal(s.backend, "connected", `after ${i} failure(s)`);
  }
  s = applyHealthResult(s, new Error("timeout"));
  assert.equal(s.backend, "offline");
  s = applyHealthResult(s, health());
  assert.equal(s.backend, "connected");
  assert.equal(s.consecutiveFailures, 0);
});

test("older backend without provider info degrades to 'unknown', still connected", () => {
  const s = applyHealthResult(initialStatus, { status: "ok", ai_configured: true, browser_ready: true });
  assert.equal(s.backend, "connected");
  assert.equal(s.gemini.state, "unknown");
});
