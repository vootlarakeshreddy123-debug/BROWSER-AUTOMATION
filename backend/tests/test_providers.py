"""Provider adapters, registry, selection, fallback, cooldown, redaction.  No network, no API keys: every hosted
provider is driven through an injected transport that returns that provider's documented response format.
Run: python -m unittest tests.test_providers -v   (from backend/)"""
import asyncio, json, sys, types, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent import events as ev
from agent.adaptive_planner import PlannerUnavailable, plan_next_action
from agent.budget import TaskBudget
from agent.provider_health import FailureKind, ProviderHealth, classify_failure, clear_global_cooldowns
from providers import registry
from providers.anthropic_adapter import ClaudeAdapter
from providers.http import ProviderHTTPError, redact
from providers.openai_compat import GrokAdapter, OpenAICompatAdapter

KEY = "sk-test-SECRET-1234567890abcdef"
PLAN = {"actions": [{"type": "click", "target": {"role": "button", "name": "Compose"}, "reason": "open composer"}],
        "source": "ai", "done": False}
CTX = {"current_url": "https://x.test/a", "current_title": "A", "body_text": "hello", "elements": []}


def run(c): return asyncio.run(c)


def openai_body(obj=PLAN): return {"choices": [{"message": {"role": "assistant", "content": json.dumps(obj)}}]}
def claude_body(obj=PLAN): return {"type": "message", "content": [{"type": "text", "text": json.dumps(obj)}], "stop_reason": "end_turn"}


class Recorder:
    def __init__(self, body=None, exc=None): self.body, self.exc, self.calls = body, exc, []
    async def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        if self.exc: raise self.exc
        return self.body


def mk(cls, poster, **kw):
    d = dict(api_key=KEY, model="m-1", base_url="https://api.example.test/v1", enabled=True, timeout_s=5, max_tokens=500, poster=poster)
    d.update(kw); return cls(**d)


class Adapters(unittest.TestCase):
    def test_openai_request_and_parse(self):
        rec = Recorder(openai_body()); a = mk(OpenAICompatAdapter, rec)
        plan = run(a.generate_plan("open compose", CTX))
        url, headers, payload, _ = rec.calls[0]
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["model"], "m-1"); self.assertIn("max_completion_tokens", payload)
        self.assertEqual(plan.actions[0].type.value, "click")

    def test_grok_uses_compatible_api_with_max_tokens(self):
        rec = Recorder(openai_body()); a = mk(GrokAdapter, rec, base_url="https://api.x.ai/v1")
        run(a.generate_plan("x", CTX))
        self.assertEqual(a.name, "grok"); self.assertIn("max_tokens", rec.calls[0][2])
        self.assertTrue(rec.calls[0][0].startswith("https://api.x.ai/v1/"))

    def test_claude_request_and_parse(self):
        rec = Recorder(claude_body()); a = mk(ClaudeAdapter, rec, base_url="https://api.anthropic.test")
        plan = run(a.generate_plan("x", CTX))
        url, headers, payload, _ = rec.calls[0]
        self.assertEqual(url, "https://api.anthropic.test/v1/messages")
        self.assertEqual(headers["x-api-key"], KEY); self.assertIn("anthropic-version", headers)
        self.assertEqual(payload["messages"][0]["role"], "user"); self.assertIn("system", payload)
        self.assertEqual(plan.actions[0].type.value, "click")

    def test_claude_code_fenced_json_is_parsed(self):
        body = {"content": [{"type": "text", "text": "```json\n" + json.dumps(PLAN) + "\n```"}]}
        self.assertEqual(run(mk(ClaudeAdapter, Recorder(body)).generate_plan("x", CTX)).actions[0].type.value, "click")

    def test_malformed_and_empty_output(self):
        for body in ({"choices": [{"message": {"content": "I think you should click compose"}}]}, {"choices": [{"message": {"content": ""}}]}, {"choices": []}):
            with self.assertRaises(RuntimeError) as cm:
                run(mk(OpenAICompatAdapter, Recorder(body)).generate_plan("x", CTX))
            self.assertIn(classify_failure(cm.exception), (FailureKind.MALFORMED, FailureKind.ERROR))

    def test_refusal_and_claude_error_body(self):
        with self.assertRaises(RuntimeError):
            run(mk(OpenAICompatAdapter, Recorder({"choices": [{"message": {"content": None, "refusal": "no"}}]})).generate_plan("x", CTX))
        with self.assertRaises(RuntimeError):
            run(mk(ClaudeAdapter, Recorder({"type": "error", "error": {"message": "overloaded"}})).generate_plan("x", CTX))

    def test_unconfigured_adapter_refuses_without_calling(self):
        rec = Recorder(openai_body())
        for kw in (dict(api_key=None), dict(model=""), dict(enabled=False)):
            a = mk(OpenAICompatAdapter, rec, **kw)
            self.assertFalse(a.configured())
            with self.assertRaises(RuntimeError): run(a.generate_plan("x", CTX))
        self.assertEqual(rec.calls, [])

    def test_secrets_never_leak_in_errors(self):
        leaky = ProviderHTTPError(401, f"Incorrect API key provided: {KEY}. See https://x/?key={KEY}")
        with self.assertRaises(ProviderHTTPError) as cm:
            run(mk(OpenAICompatAdapter, Recorder(exc=leaky)).generate_plan("x", CTX))
        self.assertNotIn(KEY, str(cm.exception)); self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(RuntimeError) as cm2:
            run(mk(ClaudeAdapter, Recorder(exc=ConnectionError(f"proxy said {KEY}"))).generate_plan("x", CTX))
        self.assertNotIn(KEY, str(cm2.exception))

    def test_redact_patterns(self):
        txt = "Authorization: Bearer abcdefghijklmnop1234 and AIzaSyA1234567890123456789012 and sk-abcdefghijklmnop and xai-abcdefghijklmnop"
        out = redact(txt)
        for bad in ("abcdefghijklmnop1234", "AIzaSyA12345", "sk-abcdefghijkl", "xai-abcdefghijkl"):
            self.assertNotIn(bad, out)

    def test_http_failure_classification(self):
        self.assertEqual(classify_failure(ProviderHTTPError(429, "Rate limit")), FailureKind.QUOTA)
        self.assertEqual(classify_failure(ProviderHTTPError(503, "overloaded")), FailureKind.UNAVAILABLE)
        self.assertEqual(classify_failure(ProviderHTTPError(401, "bad key")), FailureKind.AUTH)
        self.assertEqual(classify_failure(ProviderHTTPError(403, "no access")), FailureKind.AUTH)
        self.assertEqual(classify_failure(ProviderHTTPError(404, "model not found")), FailureKind.MODEL_UNAVAILABLE)


class Cfg:
    AI_PROVIDER = "auto"; AUTO_PROVIDER_ORDER = ["gemini", "ollama"]
    GEMINI_API_KEY = None; GEMINI_MODEL = "g-model"; GEMINI_ENABLED = True
    OLLAMA_MODEL = "qwen3:4b"; OLLAMA_ENABLED = True
    OPENAI_API_KEY = None; OPENAI_MODEL = ""; OPENAI_ENABLED = False; OPENAI_BASE_URL = "https://api.openai.com/v1"
    CLAUDE_API_KEY = None; CLAUDE_MODEL = ""; CLAUDE_ENABLED = False; ANTHROPIC_BASE_URL = "https://api.anthropic.com"
    XAI_API_KEY = None; XAI_MODEL = ""; XAI_ENABLED = False; XAI_BASE_URL = "https://api.x.ai/v1"
    HOSTED_PROVIDER_TIMEOUT_SECONDS = 5; HOSTED_PROVIDER_MAX_OUTPUT_TOKENS = 500


class RegistryTests(unittest.TestCase):
    def test_default_auto_never_includes_paid_hosted(self):
        self.assertEqual(registry.order_for("auto", Cfg), ["gemini", "ollama"])

    def test_manual_selection_is_exclusive_and_aliases_work(self):
        self.assertEqual(registry.order_for("claude", Cfg), ["claude"])
        self.assertEqual(registry.order_for("xAI", Cfg), ["grok"])
        self.assertEqual(registry.order_for("anthropic", Cfg), ["claude"])
        with self.assertRaises(ValueError): registry.order_for("bard", Cfg)

    def test_custom_auto_order_dedupes_and_ignores_unknown(self):
        c = type("C", (Cfg,), {"AUTO_PROVIDER_ORDER": ["claude", "openai", "claude", "nope", "ollama"]})
        self.assertEqual(registry.order_for("auto", c), ["claude", "openai", "ollama"])

    def test_config_problems_do_not_contain_secrets_and_name_the_fix(self):
        p = registry.config_problems("openai", Cfg)
        self.assertTrue(any("OPENAI_ENABLED" in x for x in p)); self.assertTrue(any("OPENAI_MODEL" in x for x in p))
        c = type("C", (Cfg,), {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "m", "OPENAI_ENABLED": True})
        self.assertEqual(registry.config_problems("openai", c), [])
        self.assertIn("openai", registry.configured_names(c))

    def test_status_is_safe_and_complete(self):
        clear_global_cooldowns()
        c = type("C", (Cfg,), {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "m", "OPENAI_ENABLED": True, "CLAUDE_API_KEY": KEY + "z"})
        registry.reset_registry()
        st = registry.status(c)
        self.assertEqual(set(st), set(registry.ALL))
        self.assertNotIn(KEY, json.dumps(st))
        self.assertEqual(st["openai"]["state"], "available"); self.assertTrue(st["openai"]["selectable"])
        self.assertEqual(st["claude"]["state"], "unconfigured"); self.assertFalse(st["claude"]["selectable"])
        self.assertTrue(st["openai"]["may_bill"]); self.assertFalse(st["openai"]["in_auto_order"])
        registry.reset_registry()

    def test_cooldown_shows_in_status(self):
        from agent.provider_health import set_global_cooldown
        clear_global_cooldowns(); registry.reset_registry()
        c = type("C", (Cfg,), {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "m", "OPENAI_ENABLED": True})
        set_global_cooldown("openai", 120)
        self.assertEqual(registry.status(c)["openai"]["state"], "cooldown")
        clear_global_cooldowns(); registry.reset_registry()

    def test_providers_for_task_builds_hosted_adapters_with_injected_transport(self):
        rec = Recorder(openai_body())
        c = type("C", (Cfg,), {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "m", "OPENAI_ENABLED": True,
                               "AUTO_PROVIDER_ORDER": ["openai"]})
        registry.set_poster_override(rec)
        try:
            adapters, order = registry.providers_for_task("auto", c)
            self.assertEqual(order, ["openai"])
            run(adapters["openai"].generate_plan("x", CTX))
            self.assertEqual(len(rec.calls), 1)
        finally:
            registry.set_poster_override(None)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class Fake:
    def __init__(self, script, model="fake"):
        self.script, self.calls, self.model = list(script), 0, model
    async def generate_plan(self, instruction, page_context=None, **kw):
        self.calls += 1
        item = self.script.pop(0) if self.script else RuntimeError("script exhausted")
        if isinstance(item, BaseException): raise item
        return item


def okplan(): return types.SimpleNamespace(actions=[{"type": "click"}], done=False)


def plan(providers, order, health=None, ctx=CTX, **kw):
    clear = kw.pop("clear", True)
    if clear: clear_global_cooldowns()
    clock = Clock()
    health = health or ProviderHealth(clock=clock)
    kw.setdefault("transient_retries", 0)
    return run(plan_next_action(instruction="x", context=ctx, providers=providers, health=health,
                                budget=TaskBudget(300, clock=clock), recorder=ev.ExecutionRecorder("t", clock=clock),
                                step=1, order=order, **kw)), health


class RoutingTests(unittest.TestCase):
    def test_primary_used_and_fallback_untouched(self):
        a, b = Fake([okplan()]), Fake([okplan()])
        out, _ = plan({"claude": a, "openai": b}, ["claude", "openai"])
        self.assertEqual(out.provider, "claude"); self.assertEqual((a.calls, b.calls), (1, 0))

    def test_quota_failure_falls_back_to_next_eligible_and_cools_down_primary(self):
        a = Fake([ProviderHTTPError(429, "Rate limit reached")]); b = Fake([okplan()])
        out, h = plan({"openai": a, "grok": b}, ["openai", "grok"])
        self.assertEqual(out.provider, "grok"); self.assertFalse(h.states["openai"].available)
        # a NEW task must not call the cooling-down provider at all
        a2, b2 = Fake([okplan()]), Fake([okplan()])
        out2, _ = plan({"openai": a2, "grok": b2}, ["openai", "grok"], clear=False)
        self.assertEqual((a2.calls, out2.provider), (0, "grok"))

    def test_unconfigured_providers_are_skipped_not_called(self):
        b = Fake([okplan()])
        out, _ = plan({"claude": None, "openai": None, "grok": b}, ["claude", "openai", "grok"])
        self.assertEqual(out.provider, "grok")

    def test_manual_selection_has_no_hidden_fallback(self):
        a, other = Fake([ProviderHTTPError(401, "bad key")]), Fake([okplan()])
        with self.assertRaises(PlannerUnavailable):
            plan({"claude": a, "openai": other}, ["claude"])
        self.assertEqual(other.calls, 0)

    def test_auth_failure_retires_provider_for_task_and_is_not_retried(self):
        a = Fake([ProviderHTTPError(401, "invalid x-api-key"), okplan()])
        with self.assertRaises(PlannerUnavailable) as cm:
            plan({"claude": a}, ["claude"], transient_retries=2)
        self.assertEqual(a.calls, 1); self.assertIn("auth", str(cm.exception).lower() + "auth")

    def test_one_bounded_retry_on_503_only(self):
        a = Fake([ProviderHTTPError(503, "overloaded"), okplan()])
        out, _ = plan({"openai": a}, ["openai"], transient_retries=1, backoff_base_s=0.01)
        self.assertEqual((out.provider, a.calls), ("openai", 2))
        q = Fake([ProviderHTTPError(429, "rate limit"), okplan()])
        with self.assertRaises(PlannerUnavailable):
            plan({"openai": q}, ["openai"], transient_retries=3, backoff_base_s=0.01)
        self.assertEqual(q.calls, 1)
        m = Fake([RuntimeError("invalid json"), okplan()])
        with self.assertRaises(PlannerUnavailable):
            plan({"openai": m}, ["openai"], transient_retries=3, backoff_base_s=0.01)
        self.assertEqual(m.calls, 1)

    def test_timeout_is_a_bounded_failure(self):
        class Slow(Fake):
            async def generate_plan(self, *a, **k):
                self.calls += 1; await asyncio.sleep(5)
        s = Slow([]); t0 = __import__("time").monotonic()
        with self.assertRaises(PlannerUnavailable):
            plan({"openai": s}, ["openai"], hosted_timeout_s=0.2)
        self.assertLess(__import__("time").monotonic() - t0, 2.0)

    def test_malformed_twice_retires_provider(self):
        a = Fake([RuntimeError("invalid json"), RuntimeError("invalid json")])
        clear_global_cooldowns(); clock = Clock(); h = ProviderHealth(clock=clock)
        for i in range(2):
            with self.assertRaises(PlannerUnavailable):
                plan({"grok": a}, ["grok"], health=h, ctx=dict(CTX, body_text=f"v{i}"), clear=False)
        self.assertFalse(h.states["grok"].available)

    def test_identical_failed_input_not_resent_to_same_provider(self):
        a = Fake([RuntimeError("invalid json")] + [okplan()] * 3)
        clear_global_cooldowns(); clock = Clock(); h = ProviderHealth(clock=clock, gemini_max_malformed=9)
        with self.assertRaises(PlannerUnavailable): plan({"claude": a}, ["claude"], health=h, clear=False)
        with self.assertRaises(PlannerUnavailable): plan({"claude": a}, ["claude"], health=h, clear=False)
        self.assertEqual(a.calls, 1)

    def test_per_task_hosted_call_cap(self):
        a = Fake([okplan()] * 9)
        clear_global_cooldowns(); clock = Clock(); h = ProviderHealth(clock=clock, hosted_max_calls=2)
        for i in range(2): plan({"openai": a}, ["openai"], health=h, ctx=dict(CTX, body_text=f"p{i}"), clear=False)
        with self.assertRaises(PlannerUnavailable) as cm:
            plan({"openai": a}, ["openai"], health=h, ctx=dict(CTX, body_text="p3"), clear=False)
        self.assertEqual(a.calls, 2); self.assertIn("limit", str(cm.exception))

    def test_all_providers_unavailable_is_reported_honestly(self):
        with self.assertRaises(PlannerUnavailable) as cm:
            plan({"claude": None, "openai": None, "grok": None, "gemini": None, "ollama": None},
                 ["gemini", "claude", "openai", "grok", "ollama"])
        self.assertEqual(len(cm.exception.attempts), 5)
        self.assertTrue(all(a["result"] == "not_configured" for a in cm.exception.attempts))

    def test_provider_event_names_provider_and_model(self):
        clear_global_cooldowns(); clock = Clock(); rec = ev.ExecutionRecorder("t", clock=clock)
        run(plan_next_action(instruction="x", context=CTX, providers={"claude": Fake([okplan()], model="m-9")},
                             health=ProviderHealth(clock=clock), budget=TaskBudget(300, clock=clock), recorder=rec,
                             step=1, order=["claude"]))
        calling = [e for e in rec.events if e["type"] == ev.PROVIDER_STATUS and e.get("state") == "calling"][0]
        self.assertEqual((calling["provider"], calling["model"]), ("claude", "m-9"))
        self.assertTrue(any(e["type"] == ev.AI_PLAN and e["provider"] == "claude" and e["result"] == "ok" for e in rec.events))


if __name__ == "__main__":
    unittest.main()
