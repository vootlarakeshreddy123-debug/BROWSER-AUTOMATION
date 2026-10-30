"""Natural-language understanding, safety classification and AI-output validation (no browser, no network).
Run: python -m unittest tests.test_understanding -v   (from backend/)"""
import sys, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent.goals import GoalKind, decompose, needs_goal_runner, parse_compose, split_clauses
from agent.safety import classify_label, is_sensitive_field, says_do_not_send, scrub_text
from agent.site_resolver import resolve_site_url
from agent.understanding import (RISK_CONSEQUENTIAL, RISK_DRAFT, UnderstandingError, classify_recipient, merge,
                                 understand, validate_ai_understanding, validate_body)


def U(t): return understand(t, resolve_site_url)
def kinds(t): return [g.kind.value for g in decompose(t, resolve_site_url)]


class NaturalLanguage(unittest.TestCase):
    def test_01_open_youtube_and_search(self):
        u = U("Open YouTube and search for Python tutorials.")
        self.assertEqual(kinds("Open YouTube and search for Python tutorials."), ["navigate", "search"])
        self.assertEqual(u.entities["query"], "Python tutorials"); self.assertEqual(u.intent, "search_and_navigate")
        self.assertIn("youtube", (u.site or "").lower())

    def test_02_find_first_tutorial_and_open_it(self):
        g = decompose("Find the first relevant Python tutorial and open it.", resolve_site_url)
        self.assertTrue(g)      # understood as ordered goals, not one blob typed into a field
        self.assertNotIn(GoalKind.TYPE, [x.kind for x in g])

    def test_03_gmail_compose_hello(self):
        u = U("Open Gmail and compose an email to Rahul saying hello.")
        self.assertEqual([s.kind for s in u.subgoals], ["navigate", "compose"])
        self.assertEqual((u.entities["recipient"], u.entities["body"]), ("Rahul", "hello"))
        self.assertEqual(u.risk, RISK_DRAFT); self.assertFalse(u.requires_confirmation); self.assertEqual(u.missing, [])
        self.assertEqual(u.intent, "compose_draft"); self.assertIn("mail.google.com", u.subgoals[0].params["url"])
        self.assertTrue(needs_goal_runner(u.goals))

    def test_03b_variants_are_understood(self):
        for t in ["Open Gmail and draft an email to Rahul saying hello.", "Please write an email to Rahul saying hello",
                  "Can you compose a quick email to Rahul saying hello?"]:
            u = U(t); c = [g for g in u.goals if g.kind == GoalKind.COMPOSE][0]
            self.assertEqual((c.params["recipient"], c.params["body"]), ("Rahul", "hello"), t)

    def test_04_search_wikipedia_and_extract(self):
        k = kinds("Search Wikipedia for artificial intelligence and tell me the first heading")
        self.assertEqual(k[0], "search"); self.assertIn("extract", k)

    def test_05_back_then_second_result(self):
        g = decompose("Go back to the previous page and open the second result.", resolve_site_url)
        self.assertEqual([x.kind for x in g], [GoalKind.BACK, GoalKind.CLICK]); self.assertEqual(g[1].params["ordinal"], "second")

    def test_06_github_search(self):
        self.assertEqual(kinds("Open GitHub and search for browser automation repositories."), ["navigate", "search"])
        self.assertEqual(U("Open GitHub and search for browser automation repositories.").entities["query"], "browser automation repositories")

    def test_search_for_cats_and_dogs_is_one_query_but_then_is_two_goals(self):
        self.assertEqual(kinds("search for cats and dogs"), ["search"])
        self.assertEqual(U("search for cats and dogs").entities["query"], "cats and dogs")
        self.assertEqual(kinds("search for cats and then open YouTube"), ["search", "navigate"])

    def test_08_compose_but_do_not_send(self):
        u = U("Compose an email to Rahul saying hello but do not send it")
        c = [g for g in u.goals if g.kind == GoalKind.COMPOSE][0]
        self.assertTrue(c.params["no_send"]); self.assertFalse(u.requires_confirmation)
        self.assertNotIn("send", [s.kind for s in u.subgoals])

    def test_08b_no_send_wins_over_a_later_send(self):
        u = U("Compose an email to Rahul saying hello, don't send it, then send it")
        self.assertNotIn(GoalKind.SEND, [g.kind for g in u.goals]); self.assertFalse(u.requires_confirmation)

    def test_09_send_this_email_requires_confirmation(self):
        u = U("Send this email.")
        self.assertEqual(u.intent, "send_message"); self.assertTrue(u.requires_confirmation); self.assertEqual(u.risk, RISK_CONSEQUENTIAL)
        self.assertTrue(u.subgoals[0].requires_confirmation)

    def test_compose_then_send_is_confirmation_gated(self):
        u = U("Open Gmail and compose an email to rahul@example.com saying hi, then send it")
        self.assertEqual([s.kind for s in u.subgoals], ["navigate", "compose", "send"])
        self.assertTrue(u.requires_confirmation); self.assertEqual(u.intent, "compose_and_send")

    def test_missing_recipient_and_body_ask_not_guess(self):
        u = U("Compose a message")
        self.assertEqual([m.field for m in u.missing], ["recipient", "body"])

    def test_relationship_recipient_asks(self):
        u = U("Can you write a quick email to my professor explaining that I will be absent tomorrow?")
        self.assertEqual([m.field for m in u.missing], ["recipient"])
        c = [g for g in u.goals if g.kind == GoalKind.COMPOSE][0]
        self.assertTrue(c.params.get("needs_authoring")); self.assertIn("absent tomorrow", c.params["purpose"])

    def test_calendar_event_missing_title_asks(self):
        u = U("Open my calendar and create an event tomorrow at 10 AM.")
        self.assertEqual(u.intent, "create_event"); self.assertEqual(u.entities["date"], "tomorrow"); self.assertEqual(u.entities["time"].lower(), "10 am")
        self.assertIn("title", [m.field for m in u.missing])
        self.assertEqual(U("Create an event called Dentist tomorrow at 10 AM").missing, [])

    def test_recipient_classification(self):
        self.assertEqual(classify_recipient("rahul@example.com"), "address")
        self.assertEqual(classify_recipient("Rahul"), "name"); self.assertEqual(classify_recipient("my professor"), "relationship")
        self.assertEqual(classify_recipient(""), "none")

    def test_body_with_commas_and_and_is_kept_whole(self):
        c = parse_compose("compose an email to Rahul saying hi, how are you and what's up")
        self.assertEqual(c["body"], "hi, how are you and what's up")
        self.assertEqual(len(split_clauses("compose an email to Rahul saying hi, how are you and what's up, then go back")), 2)

    def test_subject_extraction(self):
        c = parse_compose("draft an email to rahul@example.com with subject Weekly update saying all good")
        self.assertEqual((c["subject"], c["body"]), ("Weekly update", "all good"))


class Safety(unittest.TestCase):
    def test_consequential_labels(self):
        for label, kind in [("Send", "send"), ("Send now", "send"), ("Post", "publish"), ("Publish", "publish"),
                            ("Place order", "purchase"), ("Buy now", "purchase"), ("Delete", "delete"), ("Submit", "submit")]:
            self.assertEqual(classify_label(label).kind, kind, label)
        for label in ["Compose", "Search", "Save draft", "Discard", "Reply", "Next", "Send feedback to the team about our new pricing model page layout and more words"]:
            self.assertIsNone(classify_label(label), label)

    def test_sensitive_fields_and_redaction(self):
        self.assertTrue(is_sensitive_field("Password")); self.assertTrue(is_sensitive_field("Card number"))
        self.assertFalse(is_sensitive_field("Search"))
        self.assertEqual(scrub_text("hunter2", "password"), "[hidden]")
        self.assertNotIn("sk-abcdefghijklmnop", scrub_text("key sk-abcdefghijklmnop here"))
        self.assertTrue(says_do_not_send("compose it but don't send")); self.assertFalse(says_do_not_send("compose and send"))


class AIValidation(unittest.TestCase):
    GOOD = {"objective": "Open a site and search", "intent": "search_and_navigate", "site": "github",
            "entities": {"query": "python"},
            "subgoals": [{"kind": "navigate", "text": "open github", "params": {"url": "https://github.com"}},
                         {"kind": "search", "text": "search python", "params": {"query": "python"}, "expected": "results"}],
            "missing": []}

    def test_valid_output_is_accepted(self):
        u = validate_ai_understanding(self.GOOD, "x")
        self.assertEqual([g.kind.value for g in u.goals], ["navigate", "search"]); self.assertEqual(u.source, "ai")

    def test_rejections(self):
        import copy
        bad = []
        for mut in (lambda d: d.update(subgoals=[]),
                    lambda d: d["subgoals"].append({"kind": "send", "text": "send", "params": {}}),
                    lambda d: d["subgoals"].append({"kind": "rm -rf", "text": "x", "params": {}}),
                    lambda d: d["subgoals"][0]["params"].update(url="javascript:alert(1)"),
                    lambda d: d["subgoals"][0]["params"].update(url="file:///etc/passwd"),
                    lambda d: d["subgoals"][1]["params"].update(query=123),
                    lambda d: d.update(subgoals=d["subgoals"] * 8),
                    lambda d: d["subgoals"][1]["params"].update(query="x" * 5000)):
            d = copy.deepcopy(self.GOOD); mut(d); bad.append(d)
        bad += ["text", None, [], {"subgoals": "no"}]
        for d in bad:
            with self.assertRaises(UnderstandingError, msg=str(d)[:80]):
                validate_ai_understanding(d, "x")

    def test_ai_can_never_make_compose_send_or_inject_params(self):
        d = {"subgoals": [{"kind": "compose", "text": "c", "params": {"recipient": "a@b.co", "body": "hi", "send": True, "evil": "x"}}]}
        u = validate_ai_understanding(d, "x")
        self.assertFalse(u.goals[0].params["send"]); self.assertNotIn("evil", u.goals[0].params)

    def test_merge_keeps_deterministic_safety(self):
        det = U("Compose an email to a@b.co saying hi but do not send it")
        ai = validate_ai_understanding({"subgoals": [{"kind": "compose", "text": "c", "params": {"recipient": "a@b.co", "body": "hi"}}]}, "x")
        self.assertTrue(merge(det, ai).goals[0].params["no_send"])
        det2 = U("Send this email.")
        ai2 = validate_ai_understanding({"subgoals": [{"kind": "click", "text": "c", "params": {"target": "Send"}}]}, "x")
        self.assertTrue(merge(det2, ai2).requires_confirmation)

    def test_body_validation(self):
        self.assertEqual(validate_body({"body": "Hi there, see you"}, "p"), "Hi there, see you")
        for bad in ({"body": ""}, {"body": 5}, "x", {"body": "x" * 5000}):
            with self.assertRaises(UnderstandingError): validate_body(bad, "p")


if __name__ == "__main__":
    unittest.main()
