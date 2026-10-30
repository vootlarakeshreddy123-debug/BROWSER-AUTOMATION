"""Unit tests: task decomposition, goal verification, task state, blocked-page detection.

Pure Python (no browser/pydantic/network).   python -m unittest tests.test_goals -v   (from backend/)
"""
import re
import unittest
from pathlib import Path

from agent.blocked_page import detect_block, looks_like_login_wall
from agent.goal_verify import (host_matches_site, verify_click, verify_close, verify_history, verify_navigate,
                               verify_new_tab, verify_press, verify_scroll, verify_search, verify_switch)
from agent.goals import Goal, GoalKind, GoalStatus, clean_search_queries, decompose, needs_goal_runner, split_clauses
from agent.progress import observe
from agent.task_state import TaskState
from agent.typing_goal import pick_composer

SITES = {"google": "https://www.google.com", "youtube": "https://www.youtube.com", "wikipedia": "https://www.wikipedia.org",
         "chatgpt": "https://chatgpt.com", "coursera": "https://www.coursera.org", "github": "https://github.com"}
RES = lambda n: SITES.get(n)


def kinds(t):
    return [g.kind.value for g in decompose(t, RES)]


class Decomposition(unittest.TestCase):
    def test_google_two_searches_with_back(self):
        t = "Open Google, search for Python multithreading tutorials, then go back and search for Python multiprocessing tutorials."
        gs = decompose(t, RES)
        self.assertEqual([g.kind.value for g in gs], ["navigate", "search", "back", "search"])
        self.assertEqual(gs[1].params["query"], "Python multithreading tutorials")
        self.assertEqual(gs[3].params["query"], "Python multiprocessing tutorials")

    def test_google_then_youtube_new_tab(self):
        gs = decompose("Open Google, search for Python tutorials, then open YouTube in a new tab and search for Python tutorials.", RES)
        self.assertEqual([g.kind.value for g in gs], ["navigate", "search", "new_tab", "search"])
        self.assertEqual(gs[2].params["url"], "https://www.youtube.com")
        self.assertEqual([g.params.get("query") for g in gs if g.kind == GoalKind.SEARCH], ["Python tutorials", "Python tutorials"])

    def test_typed_text_never_contains_control_language(self):
        for t in ["Open Google and search for Python tutorials.",
                  "Open Google, search for Python tutorials and go back",
                  "Open Google, search for Python tutorials, then open the first result",
                  "Open Google; search for Python tutorials; scroll down"]:
            q = clean_search_queries(t, RES)
            self.assertEqual(q, ["Python tutorials"], t)
            self.assertFalse(re.search(r"\b(?:then|and|go back|scroll|open)\b", q[0], re.I), q)

    def test_and_inside_a_query_is_kept(self):
        self.assertEqual(clean_search_queries("search for cats and dogs", RES), ["cats and dogs"])
        self.assertEqual(clean_search_queries("Open Google, search for salt, pepper and oil, then go back", RES), ["salt, pepper and oil"])

    def test_existing_task_shapes(self):
        self.assertEqual(kinds("Open Wikipedia, search for artificial intelligence, and open the first relevant article."),
                         ["navigate", "search", "click"])
        self.assertEqual(kinds("open chatgpt and type hello"), ["navigate", "type"])
        self.assertEqual(kinds("Open Coursera, search for machine learning, and open the first course."), ["navigate", "search", "click"])
        self.assertEqual(kinds("Open Wikipedia, search for artificial intelligence, open the first relevant article, and tell me the first heading"),
                         ["navigate", "search", "click", "extract"])

    def test_primitives(self):
        self.assertEqual(kinds("go back"), ["back"]); self.assertEqual(kinds("go forward"), ["forward"])
        self.assertEqual(kinds("scroll down"), ["scroll"]); self.assertEqual(decompose("scroll up", RES)[0].params["direction"], "up")
        self.assertEqual(decompose("scroll to the bottom", RES)[0].params["to"], "bottom")
        self.assertEqual(kinds("press Enter"), ["press"]); self.assertEqual(decompose("press escape", RES)[0].params["key"], "Escape")
        self.assertEqual(kinds("switch to the first tab"), ["switch_tab"]); self.assertEqual(kinds("close the tab"), ["close_tab"])
        self.assertEqual(kinds("open a new tab"), ["new_tab"]); self.assertEqual(kinds("wait 3 seconds"), ["wait"])
        self.assertEqual(kinds("return to the previous page"), ["back"])

    def test_ordinals_and_new_tab_clicks(self):
        g = decompose("open the second result in a new tab", RES)[0]
        self.assertEqual((g.kind, g.params["ordinal"], g.params["new_tab"]), (GoalKind.CLICK, "second", True))
        g = decompose("click the Pricing link", RES)[0]
        self.assertEqual((g.kind, g.params["target"]), (GoalKind.CLICK, "Pricing"))

    def test_url_followed_by_comma_is_a_boundary_and_unknown_clauses_are_kept(self):
        gs = decompose("Open http://127.0.0.1:9/ , frobnicate the widget, scroll down", RES)
        self.assertEqual([g.kind.value for g in gs], ["navigate", "adaptive", "scroll"])
        self.assertEqual(gs[0].params["url"], "http://127.0.0.1:9/")

    def test_unknown_site_is_not_guessed(self):
        g = decompose("open Nonexistentsite in a new tab", RES)[0]
        self.assertEqual(g.kind, GoalKind.NEW_TAB); self.assertIsNone(g.params.get("url"))

    def test_runner_selection(self):
        self.assertTrue(needs_goal_runner(decompose("Open Google, search for a, then go back and search for b", RES)))
        self.assertTrue(needs_goal_runner(decompose("Open Google, then open YouTube in a new tab", RES)))
        self.assertTrue(needs_goal_runner(decompose("Open the page, scroll down", RES)))
        self.assertFalse(needs_goal_runner(decompose("Open Wikipedia, search for artificial intelligence, and open the first relevant article.", RES)))
        self.assertFalse(needs_goal_runner(decompose("open chatgpt and type hello", RES)))

    def test_labels_and_statuses(self):
        g = decompose("search for abc", RES)[0]
        self.assertEqual(g.status, GoalStatus.PENDING); self.assertIn("abc", g.label()); self.assertEqual(g.to_dict()["kind"], "search")

    def test_clause_split_basics(self):
        self.assertEqual(split_clauses("a, then b; c and then d"), ["a", "b", "c", "d"])


def O(url, title="t", body="b", heads=(), page_id=1, count=1, els=()):
    return observe({"current_url": url, "current_title": title, "body_text": body, "headings": list(heads),
                    "elements": [{"role": "link", "name": n} for n in els]}, page_id=page_id, page_count=count)


class Verification(unittest.TestCase):
    def goal(self, kind, **p):
        return Goal(1, kind, "x", p)

    def test_search_needs_typing_submission_and_effect(self):
        g = self.goal(GoalKind.SEARCH, query="python tutorials")
        a, b = O("https://s.test/"), O("https://s.test/search?q=python+tutorials", "python tutorials - s", "results")
        self.assertTrue(verify_search(g, a, b, True, True).ok)
        self.assertFalse(verify_search(g, a, b, False, True).ok)       # text not in field
        self.assertFalse(verify_search(g, a, b, True, False).ok)       # not submitted
        self.assertFalse(verify_search(g, a, a, True, True).ok)        # nothing changed

    def test_click_requires_effect_and_leaving_the_result_list(self):
        g = self.goal(GoalKind.CLICK, ordinal="first", what="result")
        res = O("https://s.test/search?q=a")
        self.assertTrue(verify_click(g, res, O("https://s.test/page/1?q=a", "p"), []).ok)
        self.assertFalse(verify_click(g, res, O("https://s.test/search?q=b"), []).ok)       # same list, new query string
        self.assertFalse(verify_click(g, res, res, []).ok)                                   # no change
        self.assertTrue(verify_click(g, res, O("https://s.test/search?q=a", page_id=2, count=2), [{"new_page_detected": True}]).ok)

    def test_history_checks_expected_url(self):
        g = self.goal(GoalKind.BACK)
        a, b = O("https://s.test/results"), O("https://s.test/")
        self.assertTrue(verify_history(g, a, b, "https://s.test/").ok)
        self.assertFalse(verify_history(g, a, b, "https://s.test/other").ok)
        self.assertFalse(verify_history(g, a, a, None).ok)

    def test_scroll_new_tab_switch_close_press(self):
        s = self.goal(GoalKind.SCROLL, direction="down")
        self.assertTrue(verify_scroll(s, {"scroll_y": 0}, {"scroll_y": 700, "inner_height": 600, "scroll_height": 3000}).ok)
        self.assertFalse(verify_scroll(s, {"scroll_y": 0}, {"scroll_y": 0, "inner_height": 600, "scroll_height": 3000}).ok)
        self.assertTrue(verify_scroll(s, {"scroll_y": 2400}, {"scroll_y": 2400, "inner_height": 600, "scroll_height": 3000}).ok)  # already at bottom
        nt = self.goal(GoalKind.NEW_TAB, url="https://www.youtube.com", site="youtube")
        self.assertTrue(verify_new_tab(nt, O("https://g.test/"), O("https://www.youtube.com/", page_id=2, count=2)).ok)
        self.assertFalse(verify_new_tab(nt, O("https://g.test/"), O("https://www.youtube.com/", page_id=1, count=2)).ok)   # not adopted
        self.assertFalse(verify_new_tab(nt, O("https://g.test/"), O("https://other.test/", page_id=2, count=2)).ok)        # wrong site
        self.assertFalse(verify_new_tab(nt, O("https://g.test/"), O("https://www.youtube.com/", page_id=2, count=1)).ok)   # count unchanged
        self.assertTrue(verify_switch(O("https://a/", page_id=1), O("https://b/", page_id=2)).ok)
        self.assertTrue(verify_close(O("https://a/", count=2), O("https://b/", count=1)).ok)
        p = self.goal(GoalKind.PRESS, key="Enter")
        self.assertFalse(verify_press(p, {"url": "a", "text_hash": 1}, {"url": "a", "text_hash": 1}).ok)
        self.assertTrue(verify_press(p, {"url": "a"}, {"url": "b"}).ok)
        self.assertTrue(verify_press(self.goal(GoalKind.PRESS, key="Escape"), {"url": "a"}, {"url": "a"}).ok)

    def test_navigate_checks_site(self):
        g = self.goal(GoalKind.NAVIGATE, url="https://www.google.com", site="google")
        self.assertTrue(verify_navigate(g, O("https://www.google.com/?hl=en")).ok)
        self.assertTrue(verify_navigate(g, O("https://www.google.co.in/")).ok)       # regional redirect shares the label
        self.assertFalse(verify_navigate(g, O("https://example.org/")).ok)
        self.assertFalse(verify_navigate(g, O("about:blank")).ok)
        self.assertTrue(host_matches_site("https://m.youtube.com/", "youtube", None))


class StateAndBlocking(unittest.TestCase):
    def test_history_stack_back_forward(self):
        st = TaskState("x", [])
        for u in ("https://s/", "https://s/results", "https://s/page"):
            st.track_url(1, u)
        self.assertEqual(st.expected_back(1), "https://s/results")
        st.did_back(1)
        self.assertEqual(st.expected_forward(1), "https://s/page")
        self.assertEqual(st.expected_back(1), "https://s/")
        st.did_forward(1)
        self.assertEqual(st.url_history[1][-1], "https://s/page")

    def test_block_detection(self):
        self.assertEqual(detect_block({"current_title": "Just a moment...", "body_text": "unusual traffic from your network"})[0], "captcha")
        self.assertEqual(detect_block({"current_url": "https://x/sorry/index", "body_text": ""})[0], "captcha")
        self.assertEqual(detect_block({"current_title": "Access denied", "body_text": "forbidden"})[0], "blocked")
        self.assertIsNone(detect_block({"current_title": "How CAPTCHA works", "body_text": "x" * 800 + " captcha " + "y" * 800}))
        self.assertTrue(looks_like_login_wall({"elements": [{"role": "button", "name": "Log in", "visible": True}]}))

    def test_site_search_box_is_not_a_composer(self):
        only_search = {"elements": [{"tag": "input", "type": "search", "role": "searchbox", "name": "Search chats", "visible": True}]}
        self.assertIsNone(pick_composer(only_search))
        self.assertIsNotNone(pick_composer(only_search, allow_search_boxes=True))


class SourceHygiene(unittest.TestCase):
    def test_no_hardcoded_stale_gemini_model_in_python_source(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for f in root.rglob("*.py"):
            if "tests" in f.parts:
                continue
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if "gemini-2.5-flash" in line and not line.strip().startswith(("#", '"""')) and "warn" not in line.lower() \
                        and "404" not in line and "no longer available" not in line.lower() \
                        and not ("GEMINI_MODEL" in line and "==" in line):
                    offenders.append(f"{f.name}:{i}")
        self.assertEqual(offenders, [])

    def test_goal_modules_contain_no_website_names(self):
        root = Path(__file__).resolve().parent.parent / "agent"
        names = re.compile(r"google|youtube|coursera|wikipedia|github|chatgpt|amazon|linkedin", re.I)
        for mod in ("goals.py", "goal_runner.py", "goal_verify.py", "task_state.py", "blocked_page.py", "recovery_ladder.py", "typing_goal.py"):
            src = re.sub(r'"""[\s\S]*?"""', "", (root / mod).read_text())
            code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
            self.assertIsNone(names.search(code), f"{mod} mentions a website name")


if __name__ == "__main__":
    unittest.main()
