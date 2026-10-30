"""Groq-first provider order, Groq-vs-xAI isolation, attribution, rate-limit fallback.  Mocked transports only:
no network, no API key, no spend.  Run: python -m unittest tests.test_groq -v   (from backend/)"""
import asyncio, json, sys, types, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent import events as ev
from agent.adaptive_planner import PlannerUnavailable, plan_next_action
from agent.budget import TaskBudget
from agent.provider_health import FailureKind, ProviderHealth, clear_global_cooldowns, global_cooldown_remaining
from providers import registry
from providers.http import ProviderHTTPError, redact, parse_retry_after
from providers.openai_compat import GroqAdapter, GrokAdapter

GKEY, XKEY = "gsk_TESTKEY1234567890abcdef", "xai-TESTKEY1234567890abcdef"
PLAN = {"actions": [{"type": "click", "target": {"role": "button", "name": "Sign in"}, "reason": "open"}], "source": "ai", "done": False}
CTX = {"current_url": "https://x.test/", "current_title": "A", "body_text": "hello", "elements": []}


class Cfg:
    """A complete settings stand-in; every field can be overridden per test."""
    GROQ_API_KEY, GROQ_MODEL, GROQ_BASE_URL, GROQ_ENABLED, GROQ_TIMEOUT_SECONDS = GKEY, "groq-model-1", "https://api.groq.test/openai/v1", True, 0.0
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_ENABLED = "g-key", "gem-1", True
    OLLAMA_MODEL, OLLAMA_ENABLED = "q:4b", True
    OPENAI_API_KEY = OPENAI_MODEL = None; OPENAI_BASE_URL = "https://o.test/v1"; OPENAI_ENABLED = False
    CLAUDE_API_KEY = CLAUDE_MODEL = None; CLAUDE_ENABLED = False; ANTHROPIC_BASE_URL = "https://c.test"
    XAI_API_KEY, XAI_MODEL, XAI_BASE_URL, XAI_ENABLED = XKEY, "xai-model", "https://api.xai.test/v1", False
    AI_PROVIDER = "auto"
    AUTO_PROVIDER_ORDER = ["groq", "gemini", "ollama", "openai", "claude"]
    HOSTED_PROVIDER_TIMEOUT_SECONDS, HOSTED_PROVIDER_MAX_OUTPUT_TOKENS = 20.0, 500


def cfg(**kw):
    return type("C", (Cfg,), kw)


class Clock:
    t = 1000.0
    def __call__(self): return self.t


class Rec:
    def __init__(self, body=None, exc=None): self.body, self.exc, self.calls = body, exc, []
    async def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        if self.exc: raise self.exc
        return self.body


def body(obj=PLAN): return {"choices": [{"message": {"content": json.dumps(obj)}}]}


class Fake:
    """Planner-protocol provider that records calls and replays scripted outcomes."""
    def __init__(self, outcomes, model="m"): self.o, self.calls, self.model, self.timeout_s = list(outcomes), 0, model, 0
    async def generate_plan(self, instruction, page_context=None, **kw):
        self.calls += 1
        r = self.o.pop(0) if self.o else self.o[-1]
        if isinstance(r, Exception): raise r
        return r


def okplan(): return types.SimpleNamespace(actions=[{"type": "click"}], done=False)


def plan(providers, order, health=None, **kw):
    clock = Clock()
    health = health or ProviderHealth(clock=clock)
    kw.setdefault("transient_retries", 0)
    rec = ev.ExecutionRecorder("t", clock=clock)
    out = asyncio.run(plan_next_action(instruction="x", context=CTX, providers=providers, health=health,
                                       budget=TaskBudget(kw.pop("budget", 300), clock=clock), recorder=rec, step=1, order=order, **kw))
    return out, health, rec


class EffectiveOrder(unittest.TestCase):
    def setUp(self):
        clear_global_cooldowns(); registry.reset_registry()

    def test_default_setting_is_groq_first(self):
        from config import settings
        self.assertEqual(list(settings.AUTO_PROVIDER_ORDER), ["groq", "gemini", "ollama", "openai", "claude"])

    def test_auto_order_groq_gemini_ollama_openai_claude(self):
        self.assertEqual(registry.order_for("auto", cfg()), ["groq", "gemini", "ollama", "openai", "claude"])

    def test_unconfigured_groq_is_skipped_not_fatal(self):
        c = cfg(GROQ_API_KEY=None)
        self.assertIn("API key not set in backend/.env", registry.config_problems("groq", c)[0])
        self.assertNotIn("groq", registry.configured_names(c))
        self.assertIn("gemini", registry.configured_names(c))

    def test_disabled_groq_is_skipped(self):
        c = cfg(GROQ_ENABLED=False)
        self.assertIn("groq", registry.config_problems("groq", c)[0].lower())
        self.assertNotIn("groq", registry.configured_names(c))

    def test_paid_providers_need_enablement_and_key(self):
        names = registry.configured_names(cfg())
        self.assertNotIn("openai", names); self.assertNotIn("claude", names); self.assertNotIn("grok", names)

    def test_explicit_selection_has_no_hidden_fallback(self):
        self.assertEqual(registry.order_for("groq", cfg()), ["groq"])
        self.assertEqual(registry.order_for("xai", cfg()), ["grok"])
        self.assertNotEqual(registry.normalise_selection("groq"), registry.normalise_selection("grok"))

    def test_status_reports_groq_and_no_keys(self):
        st = registry.status(cfg())
        self.assertTrue(st["groq"]["in_auto_order"]); self.assertEqual(st["groq"]["label"], "Groq"); self.assertEqual(st["groq"]["model"], "groq-model-1")
        self.assertNotIn(GKEY, json.dumps(st)); self.assertNotIn(XKEY, json.dumps(st))

    def test_ui_labels_distinguish_groq_and_grok(self):
        self.assertEqual(registry.LABELS["groq"], "Groq"); self.assertEqual(registry.LABELS["grok"], "xAI Grok")


class GroqUsesGroqConfig(unittest.TestCase):
    def setUp(self):
        clear_global_cooldowns(); registry.reset_registry(); registry.set_poster_override(Rec(body()))

    def tearDown(self):
        registry.set_poster_override(None); registry.reset_registry()

    def test_adapter_built_from_groq_not_xai_settings(self):
        a = registry.get_adapters(cfg())["groq"]
        self.assertIsInstance(a, GroqAdapter); self.assertNotIsInstance(a, GrokAdapter)
        self.assertEqual((a.model, a.base_url, a._api_key), ("groq-model-1", "https://api.groq.test/openai/v1", GKEY))
        self.assertNotIn("xai", a.base_url); self.assertNotEqual(a._api_key, XKEY)

    def test_request_goes_to_groq_endpoint_with_groq_key(self):
        rec = Rec(body()); a = GroqAdapter(api_key=GKEY, model="m", base_url="https://api.groq.test/openai/v1", enabled=True, poster=rec)
        asyncio.run(a.generate_plan("open sign in", CTX))
        url, headers, payload, _ = rec.calls[0]
        self.assertEqual(url, "https://api.groq.test/openai/v1/chat/completions")
        self.assertEqual(headers["Authorization"], f"Bearer {GKEY}")
        self.assertEqual(payload["model"], "m"); self.assertIn("max_tokens", payload)

    def test_changing_xai_settings_does_not_change_groq(self):
        a = registry.get_adapters(cfg(XAI_MODEL="other", XAI_API_KEY="zzz", XAI_BASE_URL="https://elsewhere.test"))["groq"]
        self.assertEqual((a.model, a._api_key), ("groq-model-1", GKEY))

    def test_groq_specific_timeout_precedence(self):
        self.assertEqual(registry.get_adapters(cfg())["groq"].timeout_s, 20.0)
        registry.reset_registry()
        self.assertEqual(registry.get_adapters(cfg(GROQ_TIMEOUT_SECONDS=7.0))["groq"].timeout_s, 7.0)

    def test_key_never_appears_in_redacted_errors(self):
        self.assertNotIn(GKEY, redact(f"bad key {GKEY}", [GKEY]))
        self.assertNotIn(GKEY, redact(f"bad key {GKEY}"))      # pattern-based, even when the secret list is unknown


class AttributionAndFallback(unittest.TestCase):
    def setUp(self): clear_global_cooldowns()

    def test_groq_answers_first_and_is_credited(self):
        g, m = Fake([okplan()]), Fake([okplan()])
        out, _, rec = plan({"groq": g, "gemini": m}, ["groq", "gemini"])
        self.assertEqual(out.provider, "groq"); self.assertEqual((g.calls, m.calls), (1, 0))
        self.assertTrue(any(e["type"] == ev.AI_PLAN and e["provider"] == "groq" and e["result"] == "ok" for e in rec.events))

    def test_configured_order_and_provider_actually_used_are_separate_facts(self):
        g, m = Fake([ProviderHTTPError(429, "Rate limit reached", 30.0)]), Fake([okplan()])
        out, _, rec = plan({"groq": g, "gemini": m}, ["groq", "gemini"])
        self.assertEqual(out.provider, "gemini")                     # NOT the configured primary
        self.assertEqual([a["provider"] for a in out.attempts], ["groq", "gemini"])
        self.assertEqual(out.attempts[0]["result"], "quota")

    def test_rate_limit_honours_retry_after_and_is_not_retried(self):
        g, m = Fake([ProviderHTTPError(429, "Rate limit reached", 120.0)]), Fake([okplan()])
        out, h, _ = plan({"groq": g, "gemini": m}, ["groq", "gemini"], transient_retries=3)
        self.assertEqual(g.calls, 1)                                  # a rate limit is never retried in-line
        self.assertGreaterEqual(global_cooldown_remaining("groq", Clock.t), 119.0)
        # next planning step: Groq is in cooldown -> skipped without a call
        g2 = Fake([okplan()])
        out2, _, rec2 = plan({"groq": g2, "gemini": Fake([okplan()])}, ["groq", "gemini"], health=ProviderHealth(clock=Clock()))
        self.assertEqual(g2.calls, 0); self.assertEqual(out2.provider, "gemini")
        self.assertTrue(any(e.get("state") == "skipped" and e.get("failure") == "cooldown" for e in rec2.events))

    def test_default_cooldown_when_no_retry_after(self):
        g = Fake([ProviderHTTPError(429, "Rate limit reached")])
        plan({"groq": g, "gemini": Fake([okplan()])}, ["groq", "gemini"])
        self.assertGreaterEqual(global_cooldown_remaining("groq", Clock.t), 299.0)

    def test_auth_failure_is_not_a_quota_and_not_retried(self):
        g = Fake([ProviderHTTPError(401, "Invalid API Key")])
        out, h, _ = plan({"groq": g, "gemini": Fake([okplan()])}, ["groq", "gemini"], transient_retries=3)
        self.assertEqual((g.calls, out.provider), (1, "gemini")); self.assertEqual(h.states["groq"].last_failure, "auth")

    def test_fallback_skips_providers_that_are_not_configured(self):
        out, _, _ = plan({"groq": None, "gemini": Fake([okplan()])}, ["groq", "gemini"])
        self.assertEqual(out.provider, "gemini"); self.assertEqual(out.attempts[0]["result"], "not_configured")

    def test_deadline_prevents_a_call_and_nothing_is_attributed(self):
        g = Fake([okplan()])
        with self.assertRaises(PlannerUnavailable) as cm:
            plan({"groq": g}, ["groq"], budget=0.5)
        self.assertEqual(g.calls, 0); self.assertEqual(cm.exception.attempts[0]["result"], FailureKind.BUDGET.value)

    def test_all_providers_failing_raises_instead_of_inventing_success(self):
        with self.assertRaises(PlannerUnavailable):
            plan({"groq": Fake([ProviderHTTPError(429, "x")]), "gemini": Fake([ProviderHTTPError(503, "unavailable")])}, ["groq", "gemini"])

    def test_semantically_insufficient_answer_counts_as_planning_quality_not_connectivity(self):
        h = ProviderHealth(clock=Clock())
        self.assertFalse(h.record_false_done("groq"))
        self.assertTrue(h.record_false_done("groq"))                  # second empty "done" -> distrusted for this task
        self.assertEqual(h.states["groq"].last_failure, FailureKind.FALSE_DONE.value)
        self.assertFalse(h.check("groq")[0])

    def test_retry_after_parser(self):
        self.assertEqual(parse_retry_after("42"), 42.0); self.assertIsNone(parse_retry_after("soon")); self.assertIsNone(parse_retry_after(None))


if __name__ == "__main__":
    unittest.main()
