"""Regression tests for the natural-language / routing defects found in the A-Z audit.

Every test here FAILED on the original v4 code (see TEST_REPORT.md "Audit round") and passes after the fix:
  * legacy fast path glued whole sentences into domains (chatgptandsayhello.com)
  * "say / tell it / ask it" clauses were not understood, so the task collapsed to ONE navigation/click
  * unpunctuated / polite speech ("hey nexa could you ...") broke clause splitting
  * a login request was reduced to a bogus click, URLs containing "login" were mistaken for a login request
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from agent.goals import GoalKind, decompose, needs_goal_runner, strip_politeness       # noqa: E402
from agent.planner import is_plain_site_name, looks_like_sentence, try_fast_path       # noqa: E402
from agent.site_resolver import resolve_site_url                                       # noqa: E402
from agent.understanding import LOGIN_NOTE, understand                                 # noqa: E402
import nl_dataset                                                                      # noqa: E402

BAD_PHRASES = [
    "open chatgpt and say hello",
    "Open ChatGPT and say hello.",
    "open flipkart and fill the login form",
    "open amazon and fill in the login form",
    "open wikipedia search databases open the first article and tell me the first heading",
    "open youtube then play some music for me",
    "go to github and star the first repository",
    "open my bank and pay my bills",
]


class FastPathNeverInventsDomains(unittest.TestCase):
    def test_no_sentence_becomes_a_domain(self):
        for t in BAD_PHRASES:
            plan = try_fast_path(t)
            if plan is None:
                continue
            for a in plan.actions:
                host = (a.url or "").split("//")[-1].split("/")[0]
                self.assertLess(len(host), 30, f"{t!r} -> {a.url}")
                for w in ("and", "say", "fill", "login", "form", "tell", "first", "pay", "bills", "star"):
                    self.assertNotIn(w + "the", host.replace(".", ""), t)
                self.assertNotRegex(host, r"andsay|andfill|andtell|searchdatabases|andstar|thenplay|andpay", f"{t!r} -> {a.url}")

    def test_known_site_is_only_a_bootstrap_for_a_longer_request(self):
        plan = try_fast_path("open chatgpt and say hello")
        self.assertEqual(plan.actions[0].url.rstrip("/"), "https://chatgpt.com")
        self.assertIn("before adaptive browser reasoning", plan.actions[0].reason)   # orchestrator keeps planning after it

    def test_simple_open_still_uses_fast_path(self):
        for t, host in (("open chatgpt", "chatgpt.com"), ("open youtube", "youtube.com"), ("open ecart", "ecart.com"),
                        ("open e cart", "ecart.com"), ("open stackoverflow", "stackoverflow.com")):
            plan = try_fast_path(t)
            self.assertIsNotNone(plan, t)
            self.assertIn(host, plan.actions[0].url)
            self.assertNotIn("before adaptive", plan.actions[0].reason)

    def test_plain_site_name_predicate(self):
        for ok in ("ecart", "e cart", "my-shop", "shopzone", "Example"):
            self.assertTrue(is_plain_site_name(ok), ok)
        for bad in ("chatgpt and say hello", "flipkart and fill the login form", "the best video", "a b c d", "say hello", "hello", "what is this?"):
            self.assertFalse(is_plain_site_name(bad), bad)
        self.assertTrue(looks_like_sentence("chatgpt and say hello"))
        self.assertFalse(looks_like_sentence("pricing page"))


class SayTellAsk(unittest.TestCase):
    def kinds(self, t):
        return [g.kind.value for g in decompose(t, resolve_site_url)]

    def test_say_hello_is_navigate_type_submit(self):
        for t in ("open chatgpt and say hello", "Open ChatGPT and say hello.", "Can you go to ChatGPT and tell it hello for me?",
                  "could you please open chat gpt and say hello", "go to chatgpt then say hello"):
            gs = decompose(t, resolve_site_url)
            self.assertEqual([g.kind for g in gs], [GoalKind.NAVIGATE, GoalKind.TYPE, GoalKind.PRESS], t)
            self.assertEqual(gs[0].params["url"].rstrip("/"), "https://chatgpt.com")
            self.assertEqual(gs[1].params["text"], "hello")
            self.assertEqual(gs[2].params["key"], "Enter")
            self.assertTrue(needs_goal_runner(gs), "multi-step: must not take the one-action legacy path")

    def test_type_does_not_submit(self):
        self.assertEqual(self.kinds("open chatgpt and type hello"), ["navigate", "type"])

    def test_ask_it_a_question(self):
        gs = decompose("hey nexa open chatgpt and ask it what is python", resolve_site_url)
        self.assertEqual([g.kind.value for g in gs], ["navigate", "type", "press"])
        self.assertEqual(gs[1].params["text"], "what is python")

    def test_message_body_is_not_cut_up(self):
        gs = decompose("open chatgpt and say hello there, how are you", resolve_site_url)
        self.assertEqual(gs[1].params["text"].lower().startswith("hello there"), True)

    def test_compose_body_saying_is_still_a_draft_not_a_say(self):
        gs = decompose("Open Gmail and compose an email to rahul@example.com saying hello", resolve_site_url)
        self.assertEqual([g.kind.value for g in gs], ["navigate", "compose"])


class Politeness(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(strip_politeness("Hey nexa, could you please open YouTube for me?"), "open YouTube")
        self.assertEqual(strip_politeness("I want you to open wikipedia"), "open wikipedia")
        self.assertEqual(strip_politeness("what is python?"), "what is python?")
        self.assertEqual(strip_politeness("hello"), "hello")

    def test_unpunctuated_speech(self):
        self.assertEqual([g.kind.value for g in decompose("open youtube search python tutorials open the first video", resolve_site_url)],
                         ["navigate", "search", "click"])
        self.assertEqual([g.kind.value for g in decompose("go on youtube search python", resolve_site_url)], ["navigate", "search"])


class SpeechTranscripts(unittest.TestCase):
    def test_recoverable_transcription_variants(self):
        def first(t):
            return decompose(t, resolve_site_url)[0].params.get("url", "")
        self.assertIn("youtube.com", first("open you tube and search python"))
        self.assertIn("chatgpt.com", first("open chat gpt and say hello"))
        self.assertIn("mail.google.com", first("open g mail"))
        self.assertEqual(first("open github dot com and search for python"), "https://github.com")


class LoginRequests(unittest.TestCase):
    def test_login_help_opens_the_sign_in_page_only(self):
        for t, site in (("open amazon and fill in the login form", "amazon"), ("open flipkart and fill the login form", "flipkart"),
                        ("I need to log in to Amazon. Open the login page and help me fill it in.", "amazon")):
            u = understand(t, resolve_site_url)
            self.assertEqual(u.intent, "login_assist", t)
            self.assertEqual([g.kind.value for g in u.goals], ["navigate", "click"], t)
            self.assertIn(site, u.goals[0].params["url"])
            self.assertEqual(u.goals[1].params["target"], "Sign in")
            self.assertNotIn("type", [g.kind.value for g in u.goals], "credentials are never typed from a request")
            self.assertEqual(u.entities["note"], LOGIN_NOTE)

    def test_a_url_containing_login_is_not_a_login_request(self):
        u = understand("Open http://127.0.0.1:9/?v=login and say hello", resolve_site_url)
        self.assertEqual(u.intent, "browse")
        self.assertEqual([g.kind.value for g in u.goals], ["navigate", "type", "press"])


class OutcomeLabels(unittest.TestCase):
    def test_outcome_comes_from_recorded_evidence(self):
        from agent import events as ev
        E = lambda t: {"type": t}                       # noqa: E731
        self.assertEqual(ev.derive_outcome("success", []), "success")
        self.assertEqual(ev.derive_outcome("failed", [E(ev.GOAL_COMPLETE), E(ev.GOAL_FAILED)]), "partial")
        self.assertEqual(ev.derive_outcome("failed", [E(ev.GOAL_COMPLETE), E(ev.GOAL_BLOCKED)]), "blocked")
        self.assertEqual(ev.derive_outcome("failed", [E(ev.GOAL_FAILED)]), "failed")
        self.assertEqual(ev.derive_outcome("timeout", []), "failed")
        self.assertEqual(ev.derive_outcome("awaiting_confirmation", []), "awaiting_user")
        self.assertEqual(ev.derive_outcome("cancelled", [E(ev.GOAL_COMPLETE)]), "cancelled")


class ParaphraseDataset(unittest.TestCase):
    """Measured language-layer accuracy on tests/nl_dataset.py (no AI, no browser).  Known gaps are listed, not hidden."""

    def test_supported_rows_are_all_understood(self):
        misses, total = [], 0
        for ins, kinds, dest, params, supported in nl_dataset.ROWS:
            if not supported:
                continue
            total += 1
            u = understand(ins, resolve_site_url)
            got = [g.kind.value for g in u.goals]
            nav = next((g.params.get("url") for g in u.goals if g.kind.value == "navigate"), None)
            pm = all(any(str(v).lower() == str(g.params.get(k, "")).lower() for g in u.goals) for k, v in params.items())
            if not (got == kinds and (dest is None or (nav and dest in nav)) and pm):
                misses.append((ins, got, nav))
        print(f"\n[nl-dataset] supported rows understood: {total - len(misses)}/{total}")
        self.assertEqual(misses, [])

    def test_known_gaps_are_not_claimed_as_understood(self):
        gaps = [r for r in nl_dataset.ROWS if not r[4]]
        self.assertTrue(gaps)
        for ins, *_ in gaps:
            u = understand(ins, resolve_site_url)
            self.assertTrue(any(g.kind.value == "adaptive" for g in u.goals), f"{ins!r} must be left to the AI planner")


if __name__ == "__main__":
    unittest.main()
