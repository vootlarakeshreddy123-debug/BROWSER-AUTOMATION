"""UNIT tests for the deadline / provider-health / progress / event modules.

Pure Python (no pydantic, no browser, no network).  Providers are scripted
fakes - these tests prove the *control logic*, not any real LLM or website.
Run:  python -m unittest tests.test_adaptive_units -v   (from backend/)
"""
import asyncio
import types
import unittest

from agent import events as ev
from agent.adaptive_planner import PlannerUnavailable, plan_next_action
from agent.budget import BudgetExhausted, CallLimits, TaskBudget
from agent.progress import Progress, ProgressTracker, observe
from agent.provider_health import (FailureKind, ProviderHealth, classify_failure,
                                   clear_global_cooldowns)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def plan(done=False, n=1):
    return types.SimpleNamespace(actions=[{"type": "click"}] * n, done=done)


class FakeProvider:
    def __init__(self, script, delay=0.0):
        self.script, self.delay, self.calls, self.last_kwargs = list(script), delay, 0, None

    async def generate_plan(self, instruction, page_context=None, **kw):
        self.calls += 1
        self.last_kwargs = kw
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if self.script else RuntimeError("script exhausted")
        if isinstance(item, BaseException):
            raise item
        return item


CTX = {"current_url": "https://x.test/a", "current_title": "A", "body_text": "hello", "elements": []}


def run(coro):
    return asyncio.run(coro)


def make(gem=None, oll=None, total=300.0, **hkw):
    clear_global_cooldowns()
    clock = Clock()
    budget = TaskBudget(total_s=total, clock=clock)
    health = ProviderHealth(clock=clock, **hkw)
    rec = ev.ExecutionRecorder("t1", clock=clock)
    providers = {"gemini": gem, "ollama": oll}
    return clock, budget, health, rec, providers


async def step(providers, health, budget, rec, ctx=CTX, **kw):
    kw.setdefault("transient_retries", 0)     # the bounded 503 retry has its own tests (test_providers.py)
    return await plan_next_action(instruction="do a thing", context=ctx, providers=providers,
                                  health=health, budget=budget, recorder=rec, step=1, **kw)


class BudgetTests(unittest.TestCase):
    def test_ai_cannot_eat_browser_reserve(self):
        c = Clock(); b = TaskBudget(total_s=100, ai_fraction=0.9, browser_reserve_s=30, clock=c)
        self.assertAlmostEqual(b.ai_remaining(), 70.0)
        c.advance(80)
        self.assertEqual(b.ai_remaining(), 0.0)          # only 20s left < reserve
        with self.assertRaises(BudgetExhausted):
            b.require_ai(4)

    def test_ai_share_cap(self):
        c = Clock(); b = TaskBudget(total_s=300, ai_fraction=0.35, clock=c)
        b.charge_ai(100)
        self.assertAlmostEqual(b.ai_remaining(), 5.0)     # 105 - 100
        self.assertFalse(b.can_start_ai(6))

    def test_expired(self):
        c = Clock(); b = TaskBudget(total_s=10, clock=c)
        c.advance(11)
        self.assertTrue(b.expired()); self.assertFalse(b.can_start_browser(1))

    def test_call_limits_repair_counts(self):
        cl = CallLimits(timeout_s=20, max_requests=2, total_time_s=25); cl.begin()
        self.assertTrue(cl.can_request()); cl.note_request()
        self.assertTrue(cl.can_request()); cl.note_request()
        self.assertFalse(cl.can_request())


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify_failure(RuntimeError("Gemini API quota/rate limit was reached.")), FailureKind.QUOTA)
        self.assertEqual(classify_failure(RuntimeError("503 UNAVAILABLE")), FailureKind.UNAVAILABLE)
        self.assertEqual(classify_failure(RuntimeError("Local Ollama planner timed out after 20.0s.")), FailureKind.TIMEOUT)
        self.assertEqual(classify_failure(RuntimeError("Ollama returned invalid JSON")), FailureKind.MALFORMED)
        self.assertEqual(classify_failure(RuntimeError("boom")), FailureKind.ERROR)


class PlannerTests(unittest.TestCase):
    def test_gemini_primary_success_no_ollama_call(self):
        _, b, h, r, p = make(FakeProvider([plan()]), FakeProvider([plan()]))
        out = run(step(p, h, b, r))
        self.assertEqual(out.provider, "gemini"); self.assertEqual(p["ollama"].calls, 0)

    def test_gemini_quota_cooldown_then_ollama_and_no_gemini_retry(self):
        g = FakeProvider([RuntimeError("Gemini API quota/rate limit was reached.")])
        o = FakeProvider([plan(), plan()])
        _, b, h, r, p = make(g, o)
        out = run(step(p, h, b, r)); self.assertEqual(out.provider, "ollama")
        ctx2 = dict(CTX, body_text="changed")
        out = run(step(p, h, b, r, ctx=ctx2))
        self.assertEqual(g.calls, 1, "Gemini must not be called again during cooldown")
        self.assertTrue(any(e["type"] == ev.PROVIDER_STATUS and e.get("provider") == "gemini"
                            and e.get("state") == "skipped" for e in r.events))

    def test_global_cooldown_survives_new_task(self):
        g1 = FakeProvider([RuntimeError("429 RESOURCE_EXHAUSTED")])
        _, b, h, r, p = make(g1, FakeProvider([plan()]))
        run(step(p, h, b, r))
        # new task (new health/budget) but NOT clearing global cooldowns
        clock = Clock(); h2 = ProviderHealth(clock=clock)
        g2 = FakeProvider([plan()]); o2 = FakeProvider([plan()])
        out = run(plan_next_action(instruction="x", context=CTX, providers={"gemini": g2, "ollama": o2},
                                   health=h2, budget=TaskBudget(300, clock=clock),
                                   recorder=ev.ExecutionRecorder("t2", clock=clock), step=1))
        self.assertEqual(g2.calls, 0); self.assertEqual(out.provider, "ollama")

    def test_ollama_timeout_retires_it_and_raises_fast(self):
        g = FakeProvider([RuntimeError("503 UNAVAILABLE")])
        o = FakeProvider([RuntimeError("Local Ollama planner timed out after 20.0s.")])
        _, b, h, r, p = make(g, o)
        with self.assertRaises(PlannerUnavailable):
            run(step(p, h, b, r))
        # next step: nothing is called at all
        with self.assertRaises(PlannerUnavailable):
            run(step(p, h, b, r, ctx=dict(CTX, body_text="other")))
        self.assertEqual((g.calls, o.calls), (1, 1))
        self.assertFalse(h.states["ollama"].available)

    def test_ollama_call_budget_enforced(self):
        g = FakeProvider([RuntimeError("503 UNAVAILABLE")])
        o = FakeProvider([plan(), plan(), plan(), plan()])
        _, b, h, r, p = make(g, o, ollama_max_calls=2)
        for i in range(4):
            try:
                run(step(p, h, b, r, ctx=dict(CTX, body_text=f"s{i}")))
            except PlannerUnavailable:
                pass
        self.assertEqual(o.calls, 2, "Ollama must be capped at ollama_max_calls per task")

    def test_ollama_limits_passed_and_bounded(self):
        g = FakeProvider([RuntimeError("503")]); o = FakeProvider([plan()])
        _, b, h, r, p = make(g, o, ollama_max_calls=2, ollama_time_budget_s=30)
        run(step(p, h, b, r))
        lim = o.last_kwargs["limits"]
        self.assertEqual(lim.max_requests, 2); self.assertLessEqual(lim.timeout_s, 20.0)
        self.assertLessEqual(lim.total_time_s, 30.0)

    def test_slow_provider_is_cut_off_by_call_budget(self):
        g = FakeProvider([plan()], delay=5.0)
        _, b, h, r, p = make(g, None)
        t0 = asyncio.get_event_loop_policy()  # noqa
        import time; s = time.perf_counter()
        with self.assertRaises(PlannerUnavailable):
            run(step(p, h, b, r, gemini_timeout_s=0.2))
        self.assertLess(time.perf_counter() - s, 2.0)
        self.assertEqual(h.states["gemini"].last_failure, "timeout")

    def test_no_call_when_ai_budget_nearly_exhausted(self):
        g = FakeProvider([plan()]); o = FakeProvider([plan()])
        c, b, h, r, p = make(g, o, total=100)
        c.advance(85)          # 15s left, reserve 20s -> AI budget 0
        with self.assertRaises(PlannerUnavailable):
            run(step(p, h, b, r))
        self.assertEqual((g.calls, o.calls), (0, 0))

    def test_identical_input_not_resent_after_failure(self):
        g = FakeProvider([RuntimeError("Ollama returned invalid JSON"), plan()])
        _, b, h, r, p = make(g, None, gemini_max_malformed=5)
        with self.assertRaises(PlannerUnavailable):
            run(step(p, h, b, r))
        # malformed does not retire gemini (limit 5) but the SAME input must not be resent
        with self.assertRaises(PlannerUnavailable):
            run(step(p, h, b, r))
        self.assertEqual(g.calls, 1)

    def test_malformed_gemini_retired_after_limit(self):
        g = FakeProvider([RuntimeError("invalid json")] * 3)
        _, b, h, r, p = make(g, None, gemini_max_malformed=2)
        for i in range(3):
            try: run(step(p, h, b, r, ctx=dict(CTX, body_text=f"b{i}")))
            except PlannerUnavailable: pass
        self.assertEqual(g.calls, 2)

    def test_false_done_distrust(self):
        _, b, h, r, p = make(FakeProvider([]), FakeProvider([]))
        self.assertFalse(h.record_false_done("gemini"))
        self.assertTrue(h.record_false_done("gemini"))       # 2nd claim -> distrusted
        self.assertTrue(h.record_false_done("ollama"))       # emergency provider: 1st claim


class ProgressTests(unittest.TestCase):
    def obs(self, url, title="t", body="b", heads=(), els=()):
        return observe({"current_url": url, "current_title": title, "body_text": body,
                        "headings": list(heads), "elements": [{"role": "link", "name": n} for n in els]})

    def test_same_state_is_no_progress(self):
        t = ProgressTracker("open the pricing page")
        o = self.obs("https://a.test/")
        v = t.classify(o, o, {"type": "click"}, True)
        self.assertEqual(v.kind, Progress.NO_PROGRESS); self.assertEqual(t.no_progress_streak, 1)

    def test_url_change_toward_goal_is_progress(self):
        t = ProgressTracker("open the pricing page")
        v = t.classify(self.obs("https://a.test/"), self.obs("https://a.test/pricing", "Pricing", "pricing plans"),
                       {"type": "click"}, True)
        self.assertEqual(v.kind, Progress.PROGRESS); self.assertEqual(t.no_progress_streak, 0)

    def test_action_failed(self):
        t = ProgressTracker("x")
        o = self.obs("https://a.test/")
        self.assertEqual(t.classify(o, o, {"type": "click"}, False, "timeout").kind, Progress.ACTION_FAILED)

    def test_wrong_state_other_site_lower_relevance(self):
        t = ProgressTracker("open the pricing page")
        v = t.classify(self.obs("https://a.test/pricing", "Pricing", "pricing"),
                       self.obs("https://ads.other/x", "Buy stuff", "sale"), {"type": "click"}, True)
        self.assertEqual(v.kind, Progress.WRONG_STATE)

    def test_landing_on_page_named_after_clicked_element_is_progress(self):
        t = ProgressTracker("go to pricing and open the team plan details")
        before = self.obs("https://a.test/pricing", "Pricing", "pricing plans solo team")
        after = self.obs("https://a.test/pricing/team", "Team plan - A", "team plan included", heads=("Team plan",))
        v = t.classify(before, after, {"type": "click", "target": {"role": "link", "name": "View Team plan details"}}, True)
        self.assertEqual(v.kind, Progress.PROGRESS)

    def test_changed_but_not_toward_goal(self):
        t = ProgressTracker("open the pricing page")
        v = t.classify(self.obs("https://a.test/pricing", "Pricing", "pricing"),
                       self.obs("https://a.test/blog", "Blog", "posts"), {"type": "click"}, True)
        self.assertEqual(v.kind, Progress.PAGE_CHANGED_NO_GOAL_PROGRESS)

    def test_repeated_action(self):
        t = ProgressTracker("x"); o = self.obs("https://a.test/"); a = {"type": "click", "target": {"name": "Go"}}
        kinds = [t.classify(o, o, a, True).kind for _ in range(3)]
        self.assertEqual(kinds[2], Progress.REPEATED_ACTION)

    def test_new_type_action_is_progress_same_text_is_not(self):
        t = ProgressTracker("search machine learning"); o = self.obs("https://a.test/")
        a = {"type": "type", "text": "machine learning"}
        self.assertEqual(t.classify(o, o, a, True).kind, Progress.PROGRESS)
        self.assertNotEqual(t.classify(o, o, a, True).kind, Progress.PROGRESS)

    def test_streak_resets_only_on_real_progress(self):
        t = ProgressTracker("open pricing"); o = self.obs("https://a.test/")
        for _ in range(3): t.classify(o, o, {"type": "click"}, True)
        self.assertEqual(t.no_progress_streak, 3)
        t.classify(o, self.obs("https://a.test/pricing", "Pricing", "pricing"), {"type": "click", "n": 2}, True)
        self.assertEqual(t.no_progress_streak, 0)


class EventTests(unittest.TestCase):
    def test_stats_only_from_recorded(self):
        c = Clock(); r = ev.ExecutionRecorder("t", clock=c)
        self.assertIsNone(r.stats()["avg_ai_latency_ms"]); self.assertEqual(r.stats()["browser_actions"], 0)
        r.record(ev.AI_PLAN, provider="gemini", result="ok", duration_ms=800)
        r.record(ev.AI_PLAN, provider="ollama", result="timeout", duration_ms=20000)
        r.record(ev.ACTION_RESULT, result="ok", duration_ms=300)
        r.record(ev.PROGRESS, classification="PROGRESS")
        r.record(ev.VERIFICATION, claimed_done=True, result="incomplete")
        s = r.stats()
        self.assertEqual((s["ai_calls"], s["gemini_calls"], s["ollama_calls"]), (2, 1, 1))
        self.assertEqual(s["avg_ai_latency_ms"], 10400); self.assertEqual(s["browser_actions"], 1)
        self.assertEqual(s["false_done_claims"], 1); self.assertEqual(s["progress"], {"PROGRESS": 1})

    def test_bounded_event_list(self):
        r = ev.ExecutionRecorder("t")
        for i in range(700): r.record(ev.OBSERVE, step=i)
        self.assertLessEqual(len(r.events), ev.MAX_EVENTS)


if __name__ == "__main__":
    unittest.main()
