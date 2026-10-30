"""Gmail-style compose: parsing of the user's phrase, composer readiness / sign-in detection, recipient commit rules.
No browser: the DOM-reading pieces are driven through a stub page.  (Real-Chrome coverage: scenarios gm01-gm11 in
integration_adaptive_runs.py, against a fixture that models Gmail's DOM structure - NOT the live service.)"""
import asyncio, sys, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent import compose_goal as cg
from agent.compose_goal import ComposeMixin
from agent.field_ranker import BODY, RECIPIENT, SUBJECT, Candidate, rank_fields
from agent.goals import GoalKind, decompose
from agent.site_resolver import resolve_site_url
from agent.understanding import understand

TO, BODY_TEXT = "recipient@example.com", "Hello, I hope you are doing well."
EXACT = f"Open Gmail, compose an email to {TO}, and write '{BODY_TEXT}' Do not send it."


def compose_goal(text):
    u = understand(text, resolve_site_url)
    comp = [g for g in u.goals if g.kind == GoalKind.COMPOSE]
    return u, (comp[0] if comp else None)


class ParseTheUsersPhrase(unittest.TestCase):
    def test_exact_phrase_gives_recipient_exact_body_and_draft_only(self):
        u, c = compose_goal(EXACT)
        self.assertEqual([g.kind for g in u.goals], [GoalKind.NAVIGATE, GoalKind.COMPOSE])    # no stray TYPE goal
        self.assertEqual(c.params["recipient"], TO)
        self.assertEqual(c.params["body"], BODY_TEXT)                                         # incl. its full stop
        self.assertTrue(c.params["no_send"])
        self.assertEqual(u.missing, [])
        self.assertEqual(u.intent, "compose_draft")

    def test_phrase_variants(self):
        for t in (f"Open Gmail, compose an email to {TO} and write \"Hello there\"",
                  f"Open Gmail and compose an email to {TO} and write Hello there",
                  f"open gmail, compose an email to {TO} saying Hello there"):
            u, c = compose_goal(t)
            self.assertEqual(c.params["body"], "Hello there", t)
            self.assertEqual(u.missing, [], t)
            self.assertNotIn(GoalKind.TYPE, [g.kind for g in u.goals], t)

    def test_subject_only_when_requested(self):
        _, c = compose_goal(f"Open Gmail, compose an email to {TO}, subject Hi, and write 'Hello'. Don't send it.")
        self.assertEqual((c.params["subject"], c.params["body"], c.params["no_send"]), ("Hi", "Hello", True))
        _, c = compose_goal(EXACT)
        self.assertNotIn("subject", c.params)

    def test_missing_recipient_or_body_asks_instead_of_inventing(self):
        u, _ = compose_goal(f"Open Gmail, compose an email to {TO}")
        self.assertEqual([m.field for m in u.missing], ["body"])
        u, c = compose_goal("open gmail, compose an email, write 'Hello'")
        self.assertEqual([m.field for m in u.missing], ["recipient"])
        self.assertNotIn("recipient", c.params)

    def test_send_stays_a_separate_confirmed_goal(self):
        u, c = compose_goal(f"Open Gmail, compose an email to {TO}, and write 'Hello'. Then send it.")
        self.assertEqual([g.kind for g in u.goals], [GoalKind.NAVIGATE, GoalKind.COMPOSE, GoalKind.SEND])
        self.assertFalse(c.params["no_send"])

    def test_quoted_body_mentioning_sending_is_not_altered(self):
        _, c = compose_goal(f"compose an email to {TO} and write 'Please do not send this to anyone.'")
        self.assertEqual(c.params["body"], "Please do not send this to anyone.")
        self.assertFalse(c.params["no_send"])

    def test_typing_elsewhere_is_not_swallowed_by_the_message(self):
        goals = decompose(f"compose an email to {TO} saying hello, then type 'cats' in the search box", resolve_site_url)
        kinds = [g.kind for g in goals]
        self.assertEqual(kinds[0], GoalKind.COMPOSE)
        self.assertIn(GoalKind.TYPE, kinds)                       # body already known: the later TYPE stays its own goal
        goals = decompose(f"compose an email to {TO}, then type 'cats' in the search box", resolve_site_url)
        t = [g for g in goals if g.kind == GoalKind.TYPE]
        self.assertTrue(t and t[0].params.get("target"))          # a targeted TYPE is never taken for the body

    def test_stepwise_phrasing_becomes_one_compose_goal(self):
        u, c = compose_goal(f"Open Gmail, click Compose, enter {TO} in To and type 'Hello' in the body")
        self.assertEqual([g.kind for g in u.goals], [GoalKind.NAVIGATE, GoalKind.COMPOSE])
        self.assertEqual((c.params["recipient"], c.params["body"]), (TO, "Hello"))
        self.assertEqual([g.id for g in u.goals], [1, 2])
        # a plain click on a button called Compose elsewhere is not rewritten
        self.assertNotIn(GoalKind.COMPOSE, [g.kind for g in decompose("open docs.example.com and click compose", resolve_site_url)])

    def test_non_mail_tasks_are_unchanged(self):
        for t in ("open youtube and search for python tutorials", "open wikipedia, search for cats, then write 'hello' in the box",
                  "go to github and click sign in"):
            self.assertNotIn(GoalKind.COMPOSE, [g.kind for g in decompose(t, resolve_site_url)], t)
        goals = decompose("open youtube, then say hello", resolve_site_url)
        self.assertIn(GoalKind.TYPE, [g.kind for g in goals])


class StubPage:
    def __init__(self, state): self.state, self.calls = state, 0
    async def evaluate(self, js, arg=None):
        self.calls += 1
        return self.state


def cand():
    return Candidate(element={"tag": "input", "role": "combobox", "aria_label": "To recipients"}, score=50, kind="text")


def per(**kw): return dict({"attr": False, "hidden": False, "outside": False, "inl": False}, **kw)


class RecipientCommitRules(unittest.TestCase):
    def check(self, state, token=TO):
        m = ComposeMixin(); m.page = StubPage(state)
        return asyncio.run(m._recipient_state(cand(), [token]))[0]

    def test_typed_text_still_in_a_combobox_is_not_committed(self):
        self.assertFalse(self.check({"found": True, "value": TO, "isCombo": True, "expanded": False, "per": [per()]}))

    def test_open_suggestion_list_is_not_committed(self):
        self.assertFalse(self.check({"found": True, "value": TO, "isCombo": True, "expanded": True, "per": [per(inl=True)]}))
        self.assertFalse(self.check({"found": True, "value": "rahul", "isCombo": True, "expanded": True, "per": [per(inl=True)]}))

    def test_chip_hidden_value_or_attribute_is_committed(self):
        for p in (per(outside=True), per(hidden=True), per(attr=True)):
            self.assertTrue(self.check({"found": True, "value": "", "isCombo": True, "expanded": False, "per": [p]}))

    def test_chip_that_is_itself_an_option_counts_once_the_field_was_consumed(self):
        self.assertTrue(self.check({"found": True, "value": "", "isCombo": True, "expanded": False, "per": [per(inl=True)]}))

    def test_plain_input_counts_by_its_value(self):
        self.assertTrue(self.check({"found": True, "value": TO, "isCombo": False, "expanded": False, "per": [per()]}))

    def test_clicking_a_suggestion_that_does_nothing_is_not_a_commit(self):
        # typed "rahul", list still open and the resolved address is only in the list: the field still holds what was typed
        st = {"found": True, "value": "rahul", "isCombo": True, "expanded": False, "per": [per(inl=True)]}
        m = ComposeMixin(); m.page = StubPage(st)
        self.assertEqual(asyncio.run(m._recipient_state(cand(), ["rahul.sharma@example.com"], typed="Rahul")), [False])
        st["value"] = ""                                         # the field cleared itself: the suggestion became a chip
        self.assertEqual(asyncio.run(m._recipient_state(cand(), ["rahul.sharma@example.com"], typed="Rahul")), [True])

    def test_unlocatable_field_or_page_error_is_not_committed(self):
        self.assertFalse(self.check({"found": False, "value": "", "isCombo": False, "expanded": False, "per": [per()]}))
        class Boom:
            async def evaluate(self, *a): raise RuntimeError("page closed")
        m = ComposeMixin(); m.page = Boom()
        self.assertEqual(asyncio.run(m._recipient_state(cand(), [TO])), [False])


class GmailLikeCtx:
    """Inspector-shaped context for a Gmail-structured composer."""
    @staticmethod
    def inbox(opener=True):
        els = [{"tag": "input", "role": "combobox", "aria_label": "Search mail", "name": "Search mail", "visible": True, "region": "header"}]
        if opener:
            els.append({"tag": "div", "role": "button", "name": "Compose", "aria_label": "Compose", "visible": True, "interactive": True})
        return {"current_url": "https://mail.google.com/mail/u/0/#inbox", "current_title": "Inbox", "elements": els, "body_text": "Inbox"}

    @staticmethod
    def composer():
        c = GmailLikeCtx.inbox(False)
        c["elements"] += [
            {"tag": "input", "role": "combobox", "name": "To recipients", "aria_label": "To recipients", "visible": True, "input_type": "text"},
            {"tag": "input", "name": "Subject", "aria_label": "Subject", "placeholder": "Subject", "name_attr": "subjectbox", "visible": True, "input_type": "text"},
            {"tag": "div", "role": "textbox", "name": "Message Body", "aria_label": "Message Body", "contenteditable": True, "visible": True}]
        return c


class ComposerReadiness(unittest.TestCase):
    def test_gmail_structure_fields_are_recognised(self):
        ctx = GmailLikeCtx.composer()
        self.assertEqual(rank_fields(ctx, RECIPIENT, "recipient", allow_weak_fallback=False).best().element["aria_label"], "To recipients")
        self.assertEqual(rank_fields(ctx, BODY, "message body", allow_weak_fallback=False).best().element["aria_label"], "Message Body")
        self.assertEqual(rank_fields(ctx, SUBJECT, "subject", allow_weak_fallback=False).best().element["aria_label"], "Subject")
        self.assertTrue(ComposeMixin()._composer_ready(ctx))

    def test_inbox_alone_is_not_a_ready_composer(self):
        self.assertFalse(ComposeMixin()._composer_ready(GmailLikeCtx.inbox()))      # clicking Compose is not "composed"

    def test_sign_in_detection(self):
        so = ComposeMixin._signed_out
        self.assertTrue(so({"current_url": "https://accounts.google.com/v3/signin/identifier?x=1", "elements": []}))
        self.assertTrue(so({"current_url": "https://x.test/", "current_title": "Sign in", "elements": []}))
        self.assertTrue(so({"current_url": "https://x.test/", "elements": [{"tag": "input", "input_type": "password", "visible": True}]}))
        self.assertFalse(so(GmailLikeCtx.inbox()))
        self.assertFalse(so(GmailLikeCtx.composer()))


class Harness(ComposeMixin):
    """Just enough of the runner for _open_composer: scripted observations, recorded clicks."""
    def __init__(self, seq):
        self.seq, self.clicks, self.events = list(seq), [], []
    page = type("P", (), {"url": "https://mail.google.com/"})()
    def rec(self, *a, **k): self.events.append(k)
    def _url(self): return "https://mail.google.com/"
    async def _observe(self):
        ctx = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        return ctx, None, {}
    async def _guard_blocked(self, goal, ctx): return None
    async def _exec(self, goal, action):
        self.clicks.append(action.reason); return {"ok": True, "error": None}


class OpenComposerBehaviour(unittest.TestCase):
    def setUp(self):
        self._o, self._c = cg.OPENER_WAIT_S, cg.COMPOSER_WAIT_S
        cg.OPENER_WAIT_S = cg.COMPOSER_WAIT_S = 1.2
    def tearDown(self):
        cg.OPENER_WAIT_S, cg.COMPOSER_WAIT_S = self._o, self._c

    def run_(self, seq):
        from agent.goals import Goal
        h = Harness(seq)
        return h, asyncio.run(h._open_composer(Goal(id=2, kind=GoalKind.COMPOSE, text="compose", params={})))

    def test_waits_for_a_late_compose_button_then_clicks_once_and_waits_for_the_composer(self):
        loading = {"current_url": "https://mail.google.com/", "current_title": "Gmail", "elements": [], "body_text": "Loading"}
        h, ctx = self.run_([loading, loading, GmailLikeCtx.inbox(), GmailLikeCtx.inbox(False), GmailLikeCtx.composer()])
        self.assertEqual(len(h.clicks), 1)
        self.assertTrue(cg.ComposeMixin()._composer_ready(ctx))

    def test_existing_composer_is_reused_without_clicking_compose(self):
        h, _ = self.run_([GmailLikeCtx.composer()])
        self.assertEqual(h.clicks, [])

    def test_composer_that_never_appears_fails_naming_the_stage(self):
        from agent.goal_runner import GoalFailed
        with self.assertRaises(GoalFailed) as cm:
            self.run_([GmailLikeCtx.inbox()])
        self.assertIn("composer did not appear", str(cm.exception))

    def test_sign_in_page_is_blocked_not_failed(self):
        from agent.goal_runner import GoalBlocked
        so = {"current_url": "https://accounts.google.com/v3/signin/identifier", "current_title": "Sign in", "elements": []}
        with self.assertRaises(GoalBlocked):
            self.run_([so])

    def test_page_without_any_composer_fails_after_a_bounded_wait(self):
        from agent.goal_runner import GoalFailed
        with self.assertRaises(GoalFailed) as cm:
            self.run_([{"current_url": "https://x.test/", "current_title": "Docs", "elements": [], "body_text": "hello"}])
        self.assertIn("no way to start a new message", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
