"""Task contract / completeness invariant / login+fill regression tests (no browser)."""
import sys, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent.action_schema import Action
from agent.contract import (ReqState, TaskContract, coverage_gaps, explicit_values, fast_path_covers,
                            redact_instruction, unmet_typed_values)
from agent.field_ranker import EMAIL, PHONE, rank_fields, value_matches
from agent.goals import Goal, GoalKind, GoalStatus, decompose
from agent.planner import try_fast_path
from agent.site_resolver import resolve_site_url
from agent.understanding import UnderstandingError, merge, understand, validate_ai_understanding

AMAZON = "Open Amazon login page and fill the phone number as 9010635950."
PHRASES = [AMAZON, "Open Amazon login page and enter the phone number 9010635950",
           "open amazon, click sign in and type 9010635950 in the phone number field"]


def kinds(u):
    return [g.kind for g in u.goals]


class LoginKeepsGoals(unittest.TestCase):
    def test_phone_goal_is_preserved_in_every_phrasing(self):
        for t in PHRASES:
            u = understand(t, resolve_site_url)
            fills = [g for g in u.goals if g.kind == GoalKind.FILL]
            self.assertEqual(len(fills), 1, t)
            self.assertEqual(fills[0].params["field"], "phone")
            self.assertEqual(fills[0].params["value"], "9010635950")
            self.assertEqual(kinds(u)[0], GoalKind.NAVIGATE, t)
            self.assertFalse(u.missing, t)

    def test_order_is_navigate_sign_in_fill_and_nothing_submits(self):
        u = understand(AMAZON, resolve_site_url)
        self.assertEqual(kinds(u), [GoalKind.NAVIGATE, GoalKind.CLICK, GoalKind.FILL])
        self.assertFalse(any(g.kind in (GoalKind.SEND, GoalKind.PRESS) for g in u.goals))

    def test_email_goal_preserved(self):
        u = understand("Open amazon login page and enter my email as a.b@example.com", resolve_site_url)
        f = [g for g in u.goals if g.kind == GoalKind.FILL][0]
        self.assertEqual((f.params["field"], f.params["value"]), ("email", "a.b@example.com"))

    def test_original_instruction_survives_understanding_and_contract(self):
        u = understand(AMAZON, resolve_site_url)
        self.assertEqual(u.instruction, " ".join(AMAZON.split()))
        c = TaskContract.from_goals(u.instruction, u.goals, task_id="t1", owner_id="u1")
        self.assertEqual(c.instruction, u.instruction)
        self.assertEqual(len(c.requirements), len(u.goals))
        self.assertEqual([r.id for r in c.requirements], [1, 2, 3])
        self.assertEqual(c.requirements[2].depends_on, [2])

    def test_no_explicit_value_is_dropped_or_there_is_a_question(self):
        for t in PHRASES:
            u = understand(t, resolve_site_url)
            self.assertEqual(coverage_gaps(t, u.goals), [], t)
        u = understand("Open Amazon and send 9010635950 somewhere weird", resolve_site_url)
        self.assertTrue(any(m.field == "requirement" for m in u.missing))

    def test_missing_value_asks(self):
        u = understand("open amazon login page and fill the phone number", resolve_site_url)
        self.assertEqual([m.field for m in u.missing], ["phone"])

    def test_password_goal_is_kept_blocked_and_never_carries_the_secret(self):
        u = understand("Open amazon login page and fill the password as hunter2", resolve_site_url)
        f = [g for g in u.goals if g.kind == GoalKind.FILL][0]
        self.assertTrue(f.params.get("secret"))
        self.assertNotIn("hunter2", repr(f.params) + f.text + repr(u.to_dict()))
        c = TaskContract.from_goals(u.instruction, u.goals)
        self.assertEqual(c.requirements[-1].state, ReqState.BLOCKED)
        self.assertNotIn("hunter2", repr(c.to_dict()))

    def test_secret_redaction_in_instruction(self):
        self.assertNotIn("hunter2", redact_instruction("fill the password as hunter2"))
        self.assertNotIn("123456", redact_instruction("my otp is 123456"))

    def test_phone_is_masked_in_display_copies(self):
        u = understand(AMAZON, resolve_site_url)
        self.assertNotIn("9010635950", repr(u.to_dict()["subgoals"]))
        self.assertNotIn("9010635950", repr(TaskContract.from_goals(u.instruction, u.goals).to_dict()["requirements"]))


class Invariant(unittest.TestCase):
    def goals(self):
        return [Goal(1, GoalKind.NAVIGATE, "open", {"url": "https://a.test", "site": "a"}),
                Goal(2, GoalKind.FILL, "fill", {"field": "phone", "value": "9010635950"})]

    def contract(self, gs):
        return TaskContract.from_goals("x", gs)

    def test_empty_contract_is_never_complete(self):
        self.assertFalse(TaskContract("t", "x").is_verified_complete())

    def test_one_unverified_requirement_blocks_success(self):
        gs = self.goals(); gs[0].status, gs[0].evidence = GoalStatus.COMPLETED, {"host": "a.test"}
        c = self.contract(gs); c.sync(gs)
        self.assertFalse(c.is_verified_complete())
        self.assertEqual(c.unmet()[0].id, 2)

    def test_completed_without_evidence_is_only_action_succeeded(self):
        gs = self.goals()
        for g in gs: g.status = GoalStatus.COMPLETED
        c = self.contract(gs); c.sync(gs)
        self.assertEqual({r.state for r in c.requirements}, {ReqState.ACTION_SUCCEEDED})
        self.assertFalse(c.is_verified_complete())

    def test_skipped_goal_is_never_verified_without_state_proof(self):
        gs = self.goals(); gs[0].params["_skipped"] = True
        for g in gs: g.status, g.evidence, g.note = GoalStatus.COMPLETED, {"skipped": True}, "skipped"
        c = self.contract(gs); c.sync(gs)
        self.assertEqual(c.requirements[0].state, ReqState.ACTION_SUCCEEDED)
        self.assertFalse(c.is_verified_complete())
        gs[0].evidence = {"skipped": True, "state_verified": True}; c.sync(gs)
        self.assertEqual(c.requirements[0].state, ReqState.VERIFIED_COMPLETE)

    def test_all_verified_is_complete(self):
        gs = self.goals()
        for g in gs: g.status, g.evidence = GoalStatus.COMPLETED, {"ok": 1}
        c = self.contract(gs); c.sync(gs)
        self.assertTrue(c.is_verified_complete())
        self.assertEqual(c.state, ReqState.VERIFIED_COMPLETE)

    def test_blocked_and_failed_states(self):
        gs = self.goals(); gs[0].status, gs[0].evidence = GoalStatus.COMPLETED, {"a": 1}; gs[1].status, gs[1].note = GoalStatus.BLOCKED, "wall"
        c = self.contract(gs); c.sync(gs)
        self.assertEqual(c.state, ReqState.BLOCKED)
        gs[1].status = GoalStatus.FAILED; c.sync(gs)
        self.assertEqual(c.state, ReqState.FAILED)

    def test_planner_view_splits_verified_and_remaining(self):
        gs = self.goals(); gs[0].status, gs[0].evidence = GoalStatus.COMPLETED, {"a": 1}
        c = self.contract(gs); c.sync(gs)
        v = c.planner_view()
        self.assertEqual([r["id"] for r in v["verified"]], [1]); self.assertEqual([r["id"] for r in v["remaining"]], [2])


class FastPathIsBootstrapOnly(unittest.TestCase):
    def test_navigation_prefix_cannot_complete_a_multi_step_task(self):
        for t in PHRASES + ["open example.com and say hello"]:
            plan = try_fast_path(t)
            if plan is None:
                continue
            cov = fast_path_covers(t, [a.type.value for a in plan.actions], resolve_site_url)
            self.assertFalse(cov["complete"], t)
            self.assertTrue(cov["remaining"], t)

    def test_plain_open_is_complete(self):
        self.assertTrue(fast_path_covers("open amazon", ["navigate"], resolve_site_url)["complete"])

    def test_router_marks_prefix_as_bootstrap(self):
        import asyncio
        from agent.router import build_plan
        plan = asyncio.run(build_plan(AMAZON, None))
        self.assertFalse(plan.done)
        self.assertTrue((plan.expected_state or "").startswith("BOOTSTRAP_ONLY"))

    def test_legacy_loop_cannot_finish_without_typing_the_value(self):
        self.assertEqual(unmet_typed_values(AMAZON, [{"type": "navigate", "url": "https://x.test"}]), ["phone"])
        self.assertEqual(unmet_typed_values(AMAZON, [{"type": "type", "text": "9010635950"}]), [])


class PlannerOutputValidation(unittest.TestCase):
    def test_empty_typing_action_rejected(self):
        for t in ("", "   ", None):
            with self.assertRaises(Exception):
                Action(type="type", text=t)

    def test_zero_action_plan_cannot_be_done_without_evidence(self):
        from agent.action_schema import ActionPlan
        with self.assertRaises(Exception):
            ActionPlan(actions=[], done=False)

    def test_ai_may_not_invent_fill_values_or_secrets(self):
        bad = {"subgoals": [{"kind": "fill", "text": "x", "params": {"field": "phone", "value": "1112223333"}}]}
        with self.assertRaises(UnderstandingError):
            validate_ai_understanding(bad, AMAZON)
        with self.assertRaises(UnderstandingError):
            validate_ai_understanding({"subgoals": [{"kind": "fill", "text": "x", "params": {"field": "password", "value": "x"}}]}, AMAZON)

    def test_ai_plan_that_drops_the_fill_is_rejected_by_merge(self):
        det = understand(AMAZON, resolve_site_url)
        ai = validate_ai_understanding({"subgoals": [{"kind": "navigate", "text": "open", "params": {"url": "https://www.amazon.in"}},
                                                     {"kind": "click", "text": "sign in", "params": {"target": "Sign in"}}]}, AMAZON)
        out = merge(det, ai)
        self.assertIs(out, det)
        self.assertIn("ai_rejected", out.entities)

    def test_ai_plan_that_keeps_every_requirement_is_accepted(self):
        det = understand(AMAZON, resolve_site_url)
        ai = validate_ai_understanding({"subgoals": [
            {"kind": "navigate", "text": "open", "params": {"url": "https://www.amazon.in"}},
            {"kind": "click", "text": "sign in", "params": {"target": "Sign in"}},
            {"kind": "fill", "text": "phone", "params": {"field": "phone", "value": "9010635950"}}]}, AMAZON)
        self.assertIs(merge(det, ai), ai)


class FieldGrounding(unittest.TestCase):
    def el(self, **k):
        b = dict(tag="input", role="textbox", visible=True, input_type="text", in_main=True, region="main"); b.update(k); return b

    def test_phone_goes_to_the_phone_or_email_identifier_not_the_search_box(self):
        ctx = {"elements": [self.el(input_type="search", role="searchbox", name="Search", region="header"),
                            self.el(input_type="email", name="Mobile number or email", xpath="/body/form/input[1]")]}
        r = rank_fields(ctx, PHONE, "phone number")
        self.assertEqual([c.label for c in r.candidates], ["Mobile number or email"])

    def test_pure_phone_field_beats_combined(self):
        ctx = {"elements": [self.el(input_type="tel", name="Phone number", xpath="/a"), self.el(input_type="email", name="Mobile number or email", xpath="/b")]}
        self.assertEqual(rank_fields(ctx, PHONE, "phone").candidates[0].label, "Phone number")

    def test_password_field_is_never_a_phone_target(self):
        ctx = {"elements": [self.el(input_type="password", name="Password")]}
        self.assertEqual(rank_fields(ctx, PHONE, "phone").candidates, [])

    def test_value_matches_is_strict(self):
        self.assertTrue(value_matches(PHONE, "90106 35950", "9010635950"))
        self.assertTrue(value_matches(PHONE, "+919010635950", "9010635950"))
        self.assertFalse(value_matches(PHONE, "9010635951", "9010635950"))
        self.assertFalse(value_matches(PHONE, "", "9010635950"))
        self.assertTrue(value_matches(EMAIL, "A@B.co", "a@b.co"))
        self.assertFalse(value_matches(EMAIL, "a@b.com", "a@b.co"))


if __name__ == "__main__":
    unittest.main()
