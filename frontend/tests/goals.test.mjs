// Goal list / active page derivation uses ONLY recorded events.
// Run:  node --experimental-strip-types --test tests/goals.test.mjs   (from frontend/)
import test from "node:test";
import assert from "node:assert/strict";
import { goalViews, activePageSummary, derivePhase } from "../src/lib/execution.ts";

const plan = { seq: 1, type: "GOAL_PLAN", ts: 0, elapsed_ms: 0, total: 3, goals: [
  { id: 1, kind: "navigate", label: "open https://a.test", text: "", status: "PENDING" },
  { id: 2, kind: "search", label: 'search "x"', text: "", status: "PENDING" },
  { id: 3, kind: "back", label: "back", text: "", status: "PENDING" } ] };
const ev = (seq, type, extra = {}) => ({ seq, type, ts: 0, elapsed_ms: seq * 10, ...extra });

test("statuses follow GOAL_* events and nothing else", () => {
  const events = [plan, ev(2, "GOAL_START", { goal: 1 }), ev(3, "GOAL_COMPLETE", { goal: 1 }), ev(4, "GOAL_START", { goal: 2 })];
  const v = goalViews(events);
  assert.deepEqual(v.goals.map((g) => g.status), ["COMPLETED", "IN_PROGRESS", "PENDING"]);
  assert.equal(v.current.id, 2);
});

test("failed and blocked goals are shown as such; no completion is invented", () => {
  const v = goalViews([plan, ev(2, "GOAL_START", { goal: 1 }), ev(3, "GOAL_BLOCKED", { goal: 1, reason: "captcha" })]);
  assert.equal(v.goals[0].status, "BLOCKED");
  assert.equal(v.goals.filter((g) => g.status === "COMPLETED").length, 0);
  assert.equal(derivePhase([ev(4, "GOAL_BLOCKED", { label: "open a", reason: "captcha" })]).phase, "BLOCKED");
});

test("no GOAL_PLAN means no goal panel data; active page comes from OBSERVE", () => {
  assert.equal(goalViews([ev(1, "OBSERVE", { url: "u" })]).goals.length, 0);
  assert.deepEqual(activePageSummary([ev(1, "OBSERVE", { url: "https://a", page_id: 2, page_count: 2 })]), { url: "https://a", pageId: 2, pageCount: 2 });
});
