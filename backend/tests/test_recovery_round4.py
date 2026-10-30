"""Round-4 recovery tests: typing-goal parsing, field ranking, search-outcome classification (no browser)."""
import sys, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent.goals import GoalKind, decompose, split_clauses, split_type_clause
from agent.field_ranker import COMPOSER, EMAIL, SEARCH, TEXT, classify_field, rank_fields
from agent.progress import Observation
from agent.search_outcome import Outcome, classify_search_outcome


def el(**k):
    base = dict(tag="input", role="textbox", visible=True, disabled=False, input_type="text", region="main", in_main=True)
    base.update(k); return base


class TypingParse(unittest.TestCase):
    def types(self, s):
        return [g.params for g in decompose(s) if g.kind == GoalKind.TYPE]

    def test_email_literal_excludes_control_text(self):
        p = self.types("Open example.com, search for Python, then open the contact page and type hello@example.com into the email field.")
        self.assertEqual(p, [])        # an e-mail literal for an e-mail field is now a FILL requirement (strict read-back)
        fills = [g.params for g in decompose("Open example.com, search for Python, then open the contact page and type hello@example.com into the email field.")
                 if g.kind == GoalKind.FILL]
        self.assertEqual(fills, [{"field": "email", "value": "hello@example.com", "target": "email field"}])

    def test_quoted_text_with_comma_and_then_is_intact(self):
        p = self.types("Open a page and type the exact text 'cats and dogs, then birds' into the editor.")
        self.assertEqual(p[0]["text"], "cats and dogs, then birds")
        self.assertEqual(p[0]["target"], "editor")

    def test_double_quotes_and_following_goal(self):
        gs = decompose('type "a; b and then c" in the message box, press enter')
        self.assertEqual([g.kind for g in gs], [GoalKind.TYPE, GoalKind.PRESS])
        self.assertEqual(gs[0].params["text"], "a; b and then c")

    def test_apostrophe_is_not_a_quote(self):
        self.assertEqual(self.types("type it's fine into the chat box")[0]["text"], "it's fine")

    def test_following_goal_not_swallowed(self):
        gs = decompose("type hello world into the message box and then go back")
        self.assertEqual([g.kind for g in gs], [GoalKind.TYPE, GoalKind.BACK])

    def test_split_clauses_keeps_quotes(self):
        self.assertEqual(split_clauses("type 'x, then y' and click Save"), ["type 'x, then y'", "click Save"])

    def test_bare_text(self):
        self.assertEqual(split_type_clause("hello"), ("hello", ""))


class FieldRanking(unittest.TestCase):
    def ctx(self, *els): return {"elements": list(els)}

    def test_search_prefers_search_field_over_email_first_in_dom(self):
        c = self.ctx(el(name="Email", input_type="email", xpath="/html[1]/body[1]/input[1]"),
                     el(name="Search repositories", input_type="search", role="searchbox", xpath="/html[1]/body[1]/input[2]"))
        self.assertEqual(rank_fields(c, SEARCH, "search").best().element["name"], "Search repositories")

    def test_email_target_never_picks_search(self):
        c = self.ctx(el(name="Search", input_type="search", role="searchbox"), el(name="Your email", input_type="email"))
        self.assertEqual(rank_fields(c, TEXT, "the email field").best().kind, EMAIL)

    def test_hidden_disabled_readonly_rejected(self):
        c = self.ctx(el(name="Search", visible=False), el(name="Search", disabled=True), el(name="Search", readonly=True))
        r = rank_fields(c, SEARCH, "search")
        self.assertIsNone(r.best()); self.assertEqual(len(r.rejected), 3)

    def test_absent_target(self):
        self.assertIsNone(rank_fields({"elements": [{"tag": "a", "role": "link", "name": "Home"}]}, TEXT, "").best())

    def test_signup_form_field_rejected_for_search(self):
        c = self.ctx(el(name="Your name", form_hint="newsletter sign up form"), el(name="Search", input_type="search"))
        self.assertEqual(rank_fields(c, SEARCH, "search").best().element["name"], "Search")

    def test_composer_rejects_search_box(self):
        c = self.ctx(el(name="Search", input_type="search", region="header"),
                     el(tag="textarea", name="Message", role="textbox", input_type=""))
        self.assertEqual(rank_fields(c, COMPOSER, "message box").best().element["tag"], "textarea")

    def test_ambiguous_labels_stay_ranked_with_reasons(self):
        c = self.ctx(el(name="Query", name_attr="q"), el(name="Query", name_attr="q", region="footer"))
        r = rank_fields(c, SEARCH, "search")
        self.assertEqual(len(r.candidates), 2); self.assertTrue(r.best().reasons)
        self.assertNotEqual(r.best().element.get("region"), "footer")

    def test_classify(self):
        self.assertEqual(classify_field(el(input_type="password")), "password")
        self.assertEqual(classify_field(el(input_type="checkbox")), "not_text")


def obs(url="https://a.test/", title="Home", heads=(), digest="d0", page_id=1, head=""):
    return Observation(url=url, title=title, text_digest=digest, text_head=head, headings=tuple(heads),
                       interactive=frozenset(), scroll_y=0, page_id=page_id, page_count=1)


class SearchOutcome(unittest.TestCase):
    def link(self, name, href="https://a.test/x"):
        return {"tag": "a", "role": "link", "name": name, "href": href, "region": "main", "visible": True}

    def test_not_submitted(self):
        b = obs(); r = classify_search_outcome(b, obs(), {"elements": [], "body_text": "x" * 800}, "python")
        self.assertEqual(r.outcome, Outcome.NOT_SUBMITTED)

    def test_results_page_by_links_not_by_query_param(self):
        ctx = {"elements": [self.link("Python tutorial"), self.link("Python docs"), self.link("Learn Python")], "body_text": "x" * 800}
        r = classify_search_outcome(obs(), obs("https://a.test/s?q=python", "Results", digest="d1"), ctx, "python")
        self.assertEqual(r.outcome, Outcome.SEARCH_RESULTS_PAGE)

    def test_query_param_alone_is_not_results(self):
        r = classify_search_outcome(obs(), obs("https://a.test/?q=python", "Home", digest="d1"), {"elements": [], "body_text": "x" * 800}, "python")
        self.assertNotEqual(r.outcome, Outcome.SEARCH_RESULTS_PAGE)

    def test_direct_target(self):
        ctx = {"elements": [], "body_text": "Python is a language. " * 50, "headings": ["Python"]}
        r = classify_search_outcome(obs(), obs("https://a.test/wiki/Python", "Python - Wiki", heads=["Python"], digest="d1", head="Python language"), ctx, "python")
        self.assertEqual(r.outcome, Outcome.DIRECT_TARGET_PAGE)

    def test_empty_results(self):
        ctx = {"elements": [], "body_text": "No results found for zzzq"}
        r = classify_search_outcome(obs(), obs("https://a.test/s?q=zzzq", "Search", digest="d1"), ctx, "zzzq")
        self.assertEqual(r.outcome, Outcome.EMPTY_RESULTS)

    def test_unchanged(self):
        r = classify_search_outcome(obs(), obs(), {"elements": [], "body_text": "x" * 800}, "python", typed_ok=False)
        self.assertEqual(r.outcome, Outcome.UNCHANGED)

    def test_unrelated(self):
        r = classify_search_outcome(obs(), obs("https://b.test/", "Totally different", digest="d1", head="cooking"), {"elements": [], "body_text": "x" * 800}, "python")
        self.assertEqual(r.outcome, Outcome.UNRELATED_PAGE)


if __name__ == "__main__":
    unittest.main()
