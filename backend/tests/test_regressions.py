"""Regression tests for defects seen in the runtime log. Pure Python (no pydantic/browser/network).

    python -m unittest tests.test_regressions -v        (from backend/)
"""
import ast
import builtins
import re
import unittest
from pathlib import Path

from agent import events as ev
from agent.extraction import extraction_spec, validate_record
from agent.goal_clauses import navigation_clauses, split_goal_clauses
from agent.page_identity import navigation_regression_reason, page_is_entity, title_core, urls_equivalent
from agent.placeholders import find_placeholders
from agent.progress import Progress, ProgressTracker, observe
from agent.provider_health import FailureKind, ProviderHealth, classify_failure, clear_global_cooldowns
from agent.recovery_ladder import LadderState, alternate_click_target, strategy_name
from agent.typing_goal import parse_typing_request, pick_composer, login_wall, target_for

BACKEND = Path(__file__).resolve().parent.parent


def undefined_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    bound = set(dir(builtins)) | {"__file__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        if isinstance(n, ast.arg):
            bound.add(n.arg)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        if isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    return sorted({f"{n.id}@{n.lineno}" for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound})


class ClauseSplitRegression(unittest.TestCase):
    """TEST 8 - the exact NameError: name '_CLAUSE_SPLIT' is not defined."""

    def test_no_undefined_names_in_any_backend_module(self):
        bad = {}
        for f in list(BACKEND.glob("agent/*.py")) + list(BACKEND.glob("browser/*.py")) + list(BACKEND.glob("services/*.py")):
            u = undefined_names(f)
            if u:
                bad[f.name] = u
        self.assertEqual(bad, {})

    def test_orchestrator_does_not_define_or_reference_clause_split_locally(self):
        src = (BACKEND / "agent/orchestrator.py").read_text()
        self.assertNotIn("_CLAUSE_SPLIT", src)       # lives in agent.goal_clauses; imported at startup
        self.assertIn("from agent.goal_clauses import", src)

    def test_multistep_instructions_split(self):
        self.assertEqual(split_goal_clauses("Open Wikipedia, search for artificial intelligence, and open the first article."),
                         ["Open Wikipedia", "search for artificial intelligence", "open the first article."])
        nav = [c for c, _ in navigation_clauses("Open a website, click a link that opens in a new tab, and continue from that page")]
        self.assertEqual(nav[:2], ["Open a website", "click a link that opens in a new tab"])
        self.assertEqual(len(navigation_clauses("Open GitHub, search Python repositories, and open the first repository")), 2)


class PlaceholderRegression(unittest.TestCase):
    """TEST 14."""

    def test_bracket_placeholder_detected(self):
        self.assertTrue(find_placeholders("Open [a site], click a link that opens in a new tab, and continue from that page."))
        self.assertTrue(find_placeholders("go to <website> and search"))
        self.assertTrue(find_placeholders("Open a website, click a link"))

    def test_real_tasks_are_not_placeholders(self):
        for t in ("Open Wikipedia, search for artificial intelligence, and open the first relevant article.",
                  "open chatgpt and type hello", "Open https://example.com and click the first link"):
            self.assertEqual(find_placeholders(t), [], t)


class ExtractionProvenance(unittest.TestCase):
    """Wikipedia 'first heading' returned paragraph text and was marked SUCCESS."""

    H = {"kind": "heading", "tag": "h1", "text": "Artificial intelligence"}
    P = {"kind": "text", "tag": "p", "text": "AI developers argue that this is the only way to deliver valuable applications"}

    def test_spec(self):
        self.assertEqual(extraction_spec("tell me the first heading")["kind"], "heading")
        self.assertEqual(extraction_spec("tell me the title")["kind"], "title")
        self.assertEqual(extraction_spec("tell me the first sentence")["kind"], "first_sentence")
        self.assertEqual(extraction_spec("tell me the second heading")["position"], 1)

    def test_first_heading_accepts_heading_rejects_paragraph(self):
        spec = extraction_spec("tell me the first heading")
        self.assertTrue(validate_record(spec, self.H)[0])
        ok, why = validate_record(spec, self.P)
        self.assertFalse(ok); self.assertIn("not from an h1-h6", why)

    def test_title_and_sentence_and_section(self):
        self.assertTrue(validate_record(extraction_spec("tell me the title"), {"kind": "title", "text": "T"})[0])
        self.assertFalse(validate_record(extraction_spec("tell me the title"), self.P)[0])
        self.assertTrue(validate_record(extraction_spec("tell me the first sentence"), {"kind": "first_sentence", "text": "A."})[0])
        self.assertFalse(validate_record(extraction_spec("tell me the first sentence"), self.H)[0])
        spec = extraction_spec("extract the Applications section", {"section_requested": True})
        self.assertFalse(validate_record(spec, {"kind": "text", "text": "random visible text"})[0])
        self.assertTrue(validate_record(spec, {"kind": "section", "text": "x", "section_heading": "Applications"})[0])

    def test_value_patterns(self):
        self.assertTrue(validate_record({"kind": "price"}, {"kind": "text", "text": "Now only $19.99"})[0])
        self.assertFalse(validate_record({"kind": "price"}, {"kind": "text", "text": "Free shipping on orders"})[0])
        self.assertFalse(validate_record({"kind": "table_value"}, {"kind": "text", "tag": "p", "text": "x"})[0])
        self.assertFalse(validate_record({"kind": "heading"}, {"kind": "heading", "tag": "p", "text": "x"})[0])


class NavigationPreservation(unittest.TestCase):
    """TEST 13 - never navigate a successful page back to the site homepage."""

    def test_root_navigation_from_deeper_page_is_blocked(self):
        self.assertIsNotNone(navigation_regression_reason("https://www.coursera.org/learn/machine-learning-with-python", "https://www.coursera.org/", []))
        self.assertIsNotNone(navigation_regression_reason("https://a.test/search?q=x", "https://a.test", []))

    def test_same_url_and_work_done(self):
        self.assertIsNotNone(navigation_regression_reason("https://a.test/x", "https://a.test/x", []))
        done = [{"action": {"type": "click"}}]
        self.assertIsNotNone(navigation_regression_reason("https://a.test/", "https://a.test", done))

    def test_legitimate_navigations_allowed(self):
        self.assertIsNone(navigation_regression_reason("about:blank", "https://a.test/", []))
        self.assertIsNone(navigation_regression_reason("https://a.test/deep/page", "https://b.test/", []))
        self.assertIsNone(navigation_regression_reason("https://a.test/deep", "https://a.test/other", []))


class PageIdentity(unittest.TestCase):
    def test_brand_suffix_removed(self):
        ctx = {"current_url": "https://www.coursera.org/learn/x", "elements": []}
        self.assertEqual(title_core("Machine Learning with Python | Coursera", ctx), "machine learning with python")
        self.assertEqual(title_core("Artificial intelligence - Wikipedia", {"current_url": "https://en.wikipedia.org/wiki/A"}), "artificial intelligence")

    def test_entity_vs_search_page(self):
        ctx = {"current_url": "https://en.wikipedia.org/wiki/Artificial_intelligence", "current_title": "Artificial intelligence - Wikipedia", "headings": ["Artificial intelligence"]}
        self.assertTrue(page_is_entity(ctx, "artificial intelligence"))
        ctx2 = dict(ctx, current_url="https://en.wikipedia.org/w/index.php?search=artificial+intelligence", current_title="Search results")
        self.assertFalse(page_is_entity(ctx2, "artificial intelligence"))

    def test_urls_equivalent(self):
        self.assertTrue(urls_equivalent("https://A.test/x/", "https://a.test/x"))
        self.assertFalse(urls_equivalent("https://a.test/x?q=1", "https://a.test/x"))


class TypingWorkflow(unittest.TestCase):
    """BUG 1 - 'open chatgpt and type hello' must be deterministic (0 AI)."""

    def test_parse_variants(self):
        for t in ("open chatgpt and type hello", "open chatgpt and enter hello", "go to chatgpt and type hello",
                  'open chatgpt and type "hello"', "open chatgpt and enter 'hello'"):
            r = parse_typing_request(t)
            self.assertIsNotNone(r, t); self.assertEqual(r.text, "hello", t); self.assertFalse(r.wants_send, t)

    def test_send_and_search_are_not_claimed(self):
        self.assertTrue(parse_typing_request("open chatgpt and type hello and send it").wants_send)
        self.assertIsNone(parse_typing_request("Open YouTube, search for Python tutorials, and open the first video"))

    def test_composer_selection_prefers_message_box_over_site_search(self):
        ctx = {"elements": [
            {"index": 1, "tag": "input", "type": "search", "role": "searchbox", "name": "Search chats", "region": "header", "visible": True},
            {"index": 2, "tag": "div", "contenteditable": True, "role": "textbox", "name": "Message", "in_main": True, "visible": True},
        ]}
        self.assertEqual(pick_composer(ctx)["index"], 2)
        self.assertEqual(target_for(pick_composer(ctx)), {"role": "textbox", "name": "Message"})

    def test_target_fallbacks_and_login_wall(self):
        self.assertEqual(target_for({"tag": "textarea", "placeholder": "Type your message"}), {"placeholder": "Type your message"})
        self.assertEqual(target_for({"tag": "div", "contenteditable": True, "xpath": "//div[1]"}), {"xpath": "//div[1]"})
        wall = {"elements": [{"role": "button", "name": "Log in", "visible": True}]}
        self.assertTrue(login_wall(wall)); self.assertIsNone(pick_composer(wall))


class RecoveryLadder(unittest.TestCase):
    """TEST 12 - same action twice, identical state -> strategy change, bounded."""

    def obs(self, url="https://a.test/", body="b"):
        return observe({"current_url": url, "current_title": "t", "body_text": body, "elements": []}, page_id=1, page_count=1)

    def test_repeat_is_detected_and_ladder_is_bounded(self):
        t = ProgressTracker("open the first course"); o = self.obs(); a = {"type": "click", "target": {"name": "X"}}
        self.assertEqual(t.classify(o, o, a, True).kind, Progress.NO_PROGRESS)
        self.assertGreaterEqual(t.previously_tried(a, o), 1)
        ladder, seen = LadderState(), []
        for _ in range(8):
            s = strategy_name(ladder.next_index("k"))
            if s is None:
                break
            seen.append(s)
        self.assertEqual(seen, ["investigate_new_page", "alternate_target", "scroll", "generic_dom"])

    def test_alternate_target_skips_tried_and_page_chrome(self):
        ctx = {"current_url": "https://a.test/s", "elements": [
            {"index": 0, "tag": "a", "role": "link", "name": "Home", "region": "header", "href": "/", "visible": True},
            {"index": 1, "tag": "a", "role": "link", "name": "First result", "region": "main", "href": "/a", "visible": True},
            {"index": 2, "tag": "a", "role": "link", "name": "Second result", "region": "main", "href": "/b", "visible": True},
        ]}
        alt = alternate_click_target(ctx, [{"type": "click", "target": {"name": "First result"}}])
        self.assertEqual(alt["target"]["name"], "Second result")

    def test_new_tab_is_progress_even_with_same_url(self):
        t = ProgressTracker("open the first course")
        before = observe({"current_url": "https://a.test/s", "current_title": "s", "body_text": "x"}, page_id=1, page_count=1)
        after = observe({"current_url": "https://a.test/s", "current_title": "s", "body_text": "x"}, page_id=2, page_count=2)
        self.assertEqual(t.classify(before, after, {"type": "click", "target": {"name": "C"}}, True).kind, Progress.PROGRESS)


class ProviderFailure404(unittest.TestCase):
    """Gemini 404 'model no longer available' must not be retried."""

    MSG = "404 NOT_FOUND. This model models/gemini-2.5-flash is no longer available to new users."

    def test_classified_and_permanent(self):
        clear_global_cooldowns()
        self.assertEqual(classify_failure(RuntimeError(self.MSG)), FailureKind.MODEL_UNAVAILABLE)
        h = ProviderHealth()
        h.begin_call("gemini"); h.record_failure("gemini", FailureKind.MODEL_UNAVAILABLE, 0.2, self.MSG)
        ok, kind, why = h.check("gemini")
        self.assertFalse(ok); self.assertIn("GEMINI_MODEL", h.states["gemini"].unavailable_reason)
        clear_global_cooldowns()


class ConfigConsistency(unittest.TestCase):
    """ONE authoritative Gemini model: config.py default == .env.example."""

    def test_gemini_model_default_matches_env_example(self):
        cfg = re.search(r'os\.getenv\("GEMINI_MODEL",\s*"([^"]+)"\)', (BACKEND / "config.py").read_text()).group(1)
        env = re.search(r"^GEMINI_MODEL=(\S+)", (BACKEND / ".env.example").read_text(), re.M).group(1)
        self.assertEqual(cfg, env)
        self.assertNotEqual(cfg, "gemini-2.5-flash")

    def test_provider_does_not_rewrite_model_names(self):
        src = (BACKEND / "services/ai_provider.py").read_text()
        self.assertNotIn('"gemini-2.5-flash"', src.split("class GeminiProvider")[1].split("class ")[0])


class NewEvents(unittest.TestCase):
    def test_page_events_defined(self):
        for n in ("NEW_PAGE_DETECTED", "PAGE_ADOPTED", "ACTIVE_PAGE_CHANGED"):
            self.assertTrue(hasattr(ev, n))


if __name__ == "__main__":
    unittest.main()
