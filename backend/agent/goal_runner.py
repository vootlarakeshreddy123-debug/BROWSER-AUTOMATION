"""Goal runner: executes the ORDERED GOALS of a task one verified step at a time.

    for each goal:
        observe live browser -> choose a deterministic action (AI only if it cannot)
        -> execute ONE action -> sync active page -> settle -> observe again
        -> verify THIS goal from evidence -> COMPLETED / recover / FAILED / BLOCKED

Properties (see ARCHITECTURE.md section 15):
  * the AI never decides completion: it may only propose an action for the CURRENT goal
  * every goal is verified from before/after evidence, not from the instruction's words
  * progress already made is never undone (state-aware navigation, per-page history)
  * an action that did not change the state is not repeated; a recovery ladder takes over
  * login / CAPTCHA / blocked pages are reported honestly (optionally waiting for the user)
  * no website names or selectors live here - targets come from the inspected DOM
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

from agent import events as ev
from agent.action_schema import Action, ActionPlan, ActionType, Target
from agent.adaptive_planner import PlannerUnavailable, plan_next_action
from agent.blocked_page import detect_block
from agent.compose_goal import ComposeMixin, GoalNeedsConfirmation, GoalNeedsInput
from agent.safety import ConfirmationRequest, classify_label, is_sensitive_field
from agent.field_ranker import (Candidate, EMAIL as F_EMAIL, PHONE as F_PHONE, SEARCH as F_SEARCH, TEXT as F_TEXT, rank_fields,
                                target_dict, value_matches, verify_in_field)
from agent.contract import ReqState, TaskContract
from agent.extraction import extraction_spec, validate_record, verify_in_dom
from agent.goal_verify import (
    Verdict, verify_click, verify_close, verify_history, verify_navigate, verify_new_tab,
    verify_press, verify_scroll, verify_search, verify_switch,
)
from agent.goals import Goal, GoalKind, GoalStatus
from agent.page_identity import is_search_url, navigation_regression_reason, urls_equivalent
from agent.planner import _interactive_target, _ranked_result_elements, _search_target
from agent.progress import Observation, Progress, ProgressTracker, goal_tokens, observe
from agent.recovery import RetryBudget
from agent.recovery_ladder import alternate_click_target
from agent.search_outcome import Outcome, classify_search_outcome
from agent.task_state import TaskState
from agent.typing_goal import pick_composer, target_for, verify_typed_in_dom
from browser import inspector
from browser.executor import ExecutionError, execute_plan
from browser.manager import browser_manager
from config import settings

_ORD_INDEX = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1}


class GoalBlocked(Exception):
    """The goal cannot proceed for a reason outside NEXA (login, CAPTCHA, access blocked)."""


class GoalFailed(Exception):
    pass


@dataclass
class GoalRunResult:
    status: str                       # success | failed | blocked | timeout
    error: str | None = None
    result_text: str | None = None
    message: str = ""
    page: Any = None
    goals: list[dict] = field(default_factory=list)
    actions: int = 0
    failed_goal: dict | None = None
    pending: dict | None = None        # awaiting_confirmation / needs_input: what is asked of the user
    resume_index: int | None = None


FIND_JS = r"""
(needle) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = n(needle);
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = Array.from(document.querySelectorAll('a,button,h1,h2,h3,h4,h5,h6,label,summary,[role],li,p,span,div,td,th'))
    .filter(vis);
  let best = null;
  for (const el of els) {
    const t = n(el.innerText || el.textContent || el.getAttribute('aria-label'));
    if (!t || !t.includes(want)) continue;
    if (!best || t.length < best.t.length) best = {el, t};
  }
  if (!best) return {found: false};
  best.el.scrollIntoView({block: 'center', inline: 'nearest'});
  const r = best.el.getBoundingClientRect();
  const inView = r.top >= 0 && r.bottom <= window.innerHeight;
  return {found: true, tag: best.el.tagName.toLowerCase(), text: best.t.slice(0, 120), in_view: inView};
}
"""


class GoalRunner(ComposeMixin):
    def __init__(
        self,
        *,
        task: Any,
        page: Any,
        goals: list[Goal],
        instruction: str,
        runtime: Any,                      # orchestrator._TaskRuntime (budget, health, recorder)
        providers: dict[str, Any],
        sync_page: Callable[[Any, Any, int | None], Awaitable[Any]],
        safe_title: Callable[[Any], Awaitable[str]],
        start_index: int = 0,
        approved: set[int] | None = None,
    ) -> None:
        self.start_index = start_index
        self.approved: set[int] = set(approved or ())      # goal ids the user explicitly confirmed
        self.task = task
        self.page = page
        self.instruction = instruction
        self.rt = runtime
        self.providers = providers or {}
        self.sync_page = sync_page
        self.safe_title = safe_title
        self.state = TaskState(instruction=instruction, goals=goals)
        self.contract = TaskContract.from_goals(instruction, goals, task_id=getattr(task, "id", None),
                                                owner_id=getattr(task, "owner_id", None))
        self.retry_budget = RetryBudget(max_retries=settings.BROWSER_MAX_RETRIES)
        self.actions = 0
        self.step = 0
        self.tracker = ProgressTracker(instruction)

    # ------------------------------------------------------------------ events
    def rec(self, type_: str, goal: Goal | None = None, **fields: Any) -> None:
        if goal is not None:
            fields.setdefault("goal", goal.id)
            fields.setdefault("goal_kind", goal.kind.value)
        self.rt.recorder.record(type_, step=self.step or None, **fields)

    # ------------------------------------------------------------------ main
    async def run(self) -> GoalRunResult:
        goals = self.state.goals
        self.rec(ev.GOAL_PLAN, total=len(goals), goals=self.state.summary())
        for i, goal in enumerate(goals):
            if i < self.start_index or goal.status == GoalStatus.COMPLETED:
                continue                                  # resumed task: finished goals are never re-run
            self.state.current_goal_index = i
            if self.rt.budget.expired():
                return self._finish("timeout", goal, "the task deadline was reached")
            goal.status = GoalStatus.IN_PROGRESS
            self.rec(ev.GOAL_START, goal, label=goal.label(), index=i + 1, total=len(goals), url=self._url())
            try:
                await self._run_goal(goal)
            except GoalNeedsConfirmation as exc:
                goal.status, goal.note = GoalStatus.PENDING, f"awaiting confirmation: {exc}"
                pend = exc.request.to_dict()
                self.rec(ev.CONFIRMATION_REQUESTED, goal, label=goal.label(), kind=exc.request.kind, description=exc.request.description)
                return GoalRunResult(status="awaiting_confirmation", page=self.page, goals=self.state.summary(), actions=self.actions,
                                     message=f"Paused before a consequential step: {exc.request.description}. Nothing has been done yet.",
                                     pending=dict(pend, goal_id=goal.id), resume_index=i)
            except GoalNeedsInput as exc:
                goal.status, goal.note = GoalStatus.PENDING, f"needs input: {exc}"
                pend = exc.clarification.to_dict()
                self.rec(ev.INPUT_REQUESTED, goal, label=goal.label(), field=exc.clarification.field, question=exc.clarification.question)
                return GoalRunResult(status="needs_input", page=self.page, goals=self.state.summary(), actions=self.actions,
                                     message=exc.clarification.question, pending=dict(pend, goal_id=goal.id), resume_index=i)
            except GoalBlocked as exc:
                goal.status, goal.note = GoalStatus.BLOCKED, str(exc)
                self.rec(ev.GOAL_BLOCKED, goal, label=goal.label(), reason=str(exc), url=self._url())
                return self._finish("blocked", goal, str(exc))
            except GoalFailed as exc:
                goal.status, goal.note = GoalStatus.FAILED, str(exc)
                self.rec(ev.GOAL_FAILED, goal, label=goal.label(), reason=str(exc), url=self._url())
                return self._finish("failed", goal, str(exc))
            except _Deadline:
                goal.status, goal.note = GoalStatus.FAILED, "deadline"
                self.rec(ev.GOAL_FAILED, goal, label=goal.label(), reason="task deadline reached", url=self._url())
                return self._finish("timeout", goal, "the task deadline was reached")
            goal.status = GoalStatus.COMPLETED
            self.rec(ev.GOAL_COMPLETE, goal, label=goal.label(), reason=goal.note, url=self._url(),
                     evidence=json.dumps(goal.evidence, default=str)[:300])
        return await self._final_verification()

    def _url(self) -> str | None:
        try:
            return self.page.url
        except Exception:
            return None

    def _finish(self, status: str, goal: Goal | None, reason: str) -> GoalRunResult:
        done = len(self.state.completed_goals)
        where = f"goal {goal.id}/{len(self.state.goals)} ({goal.label()})" if goal else "the task"
        msg = {
            "failed": f"Stopped at {where}: {reason}. {done} of {len(self.state.goals)} goals were completed and verified.",
            "blocked": f"Blocked at {where}: {reason} {done} of {len(self.state.goals)} goals were completed. "
                       "The live Chrome tab is left open so you can resolve it and run the task again.",
            "timeout": f"Deadline reached at {where}. {done} of {len(self.state.goals)} goals were completed.",
        }[status]
        return GoalRunResult(status=status, error=msg, page=self.page, goals=self.state.summary(),
                             actions=self.actions, failed_goal=goal.to_dict() if goal else None)

    async def _final_verification(self) -> GoalRunResult:
        """Independent of every planner: all goals verified, page alive, nothing blocked."""
        self.page = await self.sync_page(self.task, self.page, None)
        ctx, obs, _ = await self._observe()
        pending = [g for g in self.state.goals if g.status != GoalStatus.COMPLETED]
        self.contract.sync(self.state.goals)
        unproven = [r.id for r in self.contract.unmet() if r.id not in {g.id for g in pending}]
        block = detect_block(ctx)
        ok = (not pending and not unproven and self.contract.is_verified_complete()
              and block is None and not self.page.is_closed())
        self.rec(ev.VERIFICATION, result="complete" if ok else "incomplete", scope="final",
                 reason=("all requirements verified" if ok else f"pending={[g.id for g in pending]} unproven={unproven} block={block}"),
                 url=obs.url, verdict="VERIFIED_COMPLETE" if ok else "INCOMPLETE")
        if not ok:
            g = pending[0] if pending else None
            return self._finish("failed", g, "final verification failed")
        text = " | ".join(str(r.get("text")) for r in self.state.extracted if r.get("text")) or None
        weak = [g for g in self.state.goals if g.evidence.get("strength") == "weak"]
        note = (f" Limitation: {len(weak)} free-form step(s) could only be verified by an observed page-state change, "
                "not by their content.") if weak else ""
        return GoalRunResult(status="success", result_text=text, page=self.page, goals=self.state.summary(),
                             actions=self.actions,
                             message=f"All {len(self.state.goals)} goals were completed and verified.{note}")

    # ------------------------------------------------------------------ observation / execution
    async def _observe(self) -> tuple[dict, Observation, dict]:
        page = self.page
        ctx = await inspector.snapshot(page)
        ctx["current_url"] = page.url
        ctx["current_title"] = await self.safe_title(page)
        sig = await browser_manager.page_state_signature(page)
        ctx["viewport"] = dict(ctx.get("viewport") or {}, scrollY=sig.get("scroll_y", 0))
        obs = observe(ctx, page.url, sig.get("page_id", 0), sig.get("page_count", 1))
        self.state.track_url(obs.page_id, obs.url)
        self.rec(ev.OBSERVE, url=obs.url, title=ctx["current_title"], page_id=obs.page_id,
                 page_count=obs.page_count, elements=len(ctx.get("elements") or []))
        return ctx, obs, sig

    async def _settle(self) -> None:
        """Wait (bounded) for the page to stop changing."""
        try:
            await browser_manager.wait_until_ready(self.page, 3000)
        except Exception:
            pass
        last = None
        for _ in range(6):
            sig = await browser_manager.page_state_signature(self.page)
            cur = (sig.get("url"), sig.get("text_hash"), sig.get("page_count"))
            if cur == last:
                return
            last = cur
            await asyncio.sleep(0.25)

    def _check_deadline(self) -> None:
        if self.rt.budget.expired() or not self.rt.budget.can_start_browser(3.0):
            raise _Deadline()

    async def _exec(self, goal: Goal, action: Action) -> dict:
        """Execute ONE action; always returns {'ok','error','result','evidence'}; keeps self.page current."""
        self._check_deadline()
        label = _label(action)
        # SAFETY GATE: a click on a Send / Post / Publish / Pay / Delete / Submit control never runs without the user's
        # explicit confirmation - whoever proposed the click (deterministic rule, AI planner, or the instruction text).
        if action.type == ActionType.CLICK and goal.id not in self.approved:
            cons = classify_label(label)
            if cons is not None:
                self.rec(ev.CONFIRMATION_REQUESTED, goal, kind=cons.kind, description=cons.describe())
                raise GoalNeedsConfirmation(ConfirmationRequest(cons.kind, cons.describe(), cons.label, goal.id))
        self.step += 1
        shown_text = "[hidden]" if is_sensitive_field(label, action.reason) else action.text
        if goal.kind == GoalKind.FILL and shown_text:
            from agent.goals import mask_value
            shown_text = mask_value(shown_text)            # redacted trace: identifiers are masked in every event
        self.rec(ev.ACTION_START, goal, action=action.type.value, target=label, reason=action.reason, url=self._url(),
                 provider="deterministic", text=shown_text, key=action.key)
        t0 = time.perf_counter()
        ok, error, result = True, None, {}
        try:
            result = await asyncio.wait_for(
                execute_plan(self.page, [action], self.retry_budget),
                timeout=max(1.0, self.rt.budget.remaining()),
            )
        except asyncio.TimeoutError:
            ok, error = False, "browser action exceeded the remaining task budget"
        except ExecutionError as exc:
            ok, error = False, getattr(exc, "message", str(exc))
        except Exception as exc:       # noqa: BLE001
            ok, error = False, str(exc)[:240]
        dt = time.perf_counter() - t0
        self.rt.budget.charge_browser(dt)
        self.page = await self.sync_page(self.task, self.page, self.step)
        if ok:
            self.actions += 1
            self.state.action_history.append({"goal": goal.id, "action": action.model_dump(mode="json")})
        evidence = (result or {}).get("evidence") or []
        self.rec(ev.ACTION_RESULT, goal, action=action.type.value, target=label, result="ok" if ok else "failed",
                 duration_ms=int(dt * 1000), error=error, url=self._url(),
                 page_id=browser_manager.page_identity(self.page),
                 new_page=any(e.get("new_page_detected") for e in evidence) or None)
        if ok:
            await self._settle()
        return {"ok": ok, "error": error, "result": result, "evidence": evidence}

    def _progress(self, goal: Goal, before: Observation, after: Observation, action: Action, ok: bool, err: str | None) -> Progress:
        verdict = self.tracker.classify(before, after, action.model_dump(mode="json"), ok, err)
        self.rec(ev.PROGRESS, goal, classification=verdict.kind.value, reason="; ".join(verdict.reasons),
                 streak=self.tracker.no_progress_streak, url=after.url)
        if verdict.is_progress:
            self.state.progress_count += 1
            self.state.no_progress_count = 0
        else:
            self.state.no_progress_count += 1
        return verdict.kind

    def _verdict(self, goal: Goal, v: Verdict, **extra: Any) -> bool:
        self.rec(ev.VERIFICATION, goal, result="complete" if v.ok else "incomplete", reason=v.reason,
                 url=self._url(), verdict="VERIFIED_COMPLETE" if v.ok else "INCOMPLETE", **extra)
        if v.ok:
            goal.note = v.reason
            goal.evidence.update(v.evidence or {})
        return v.ok

    async def _guard_blocked(self, goal: Goal, ctx: dict) -> None:
        """Report CAPTCHA/login/blocked pages honestly; give the user a bounded chance to clear it."""
        found = detect_block(ctx)
        if not found:
            return
        kind, reason = found
        wait_s = min(settings.MANUAL_INTERVENTION_WAIT_SECONDS, max(0.0, self.rt.budget.remaining() - 10.0))
        self.rec(ev.RECOVERY, goal, level=5, strategy="await_manual_intervention",
                 reason=f"{reason} Waiting up to {int(wait_s)}s for you to resolve it in the Chrome window.", url=self._url())
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            c, _, _ = await self._observe()
            if not detect_block(c):
                self.rec(ev.RECOVERY, goal, level=5, strategy="manual_intervention_cleared",
                         reason="the page became usable again", url=self._url())
                return
        raise GoalBlocked(f"{reason} It was not resolved within {int(wait_s)}s.")

    # ------------------------------------------------------------------ goal dispatch
    async def _run_goal(self, goal: Goal) -> None:
        handler = {
            GoalKind.NAVIGATE: self._g_navigate, GoalKind.SEARCH: self._g_search, GoalKind.TYPE: self._g_type,
            GoalKind.CLICK: self._g_click, GoalKind.PRESS: self._g_press, GoalKind.SCROLL: self._g_scroll,
            GoalKind.BACK: self._g_history, GoalKind.FORWARD: self._g_history, GoalKind.NEW_TAB: self._g_new_tab,
            GoalKind.SWITCH_TAB: self._g_switch, GoalKind.CLOSE_TAB: self._g_close, GoalKind.FIND: self._g_find,
            GoalKind.COMPOSE: self._g_compose, GoalKind.SEND: self._g_send, GoalKind.FILL: self._g_fill,
            GoalKind.EXTRACT: self._g_extract, GoalKind.WAIT: self._g_wait, GoalKind.ADAPTIVE: self._g_adaptive,
        }[goal.kind]
        await handler(goal)

    # ---- NAVIGATE -----------------------------------------------------------
    async def _g_navigate(self, goal: Goal) -> None:
        url = goal.params.get("url")
        if not url:
            raise GoalFailed(f"I do not know which website '{goal.params.get('site') or goal.text}' is. Give a URL.")
        ctx, before, _ = await self._observe()
        # Only the opening navigation is a "bootstrap": a later, explicit "open <site>" is the user's
        # instruction and is honoured. (Never regress progress just because the task STARTED somewhere.)
        skip = navigation_regression_reason(self.page.url, url, self.state.action_history) if goal.id == 1 else None
        if skip and not self.page.url.startswith("about:blank"):
            from agent.goal_verify import host_matches_site
            if host_matches_site(self.page.url, goal.params.get("site"), url):
                # skipping is only legitimate when the browser demonstrably already is where the requirement says
                self._verdict(goal, Verdict(True, f"already on the requested site, state preserved: {skip}",
                                            {"skipped": True, "state_verified": True, "host": self.page.url.split('/')[2] if '//' in self.page.url else ""}))
                return
        r = await self._exec(goal, Action(type=ActionType.NAVIGATE, url=url, reason=f"Open {url}"))
        ctx, after, _ = await self._observe()
        if not r["ok"]:
            await self._guard_blocked(goal, ctx)       # blocked pages make navigation 'fail' by design
            ctx, after, _ = await self._observe()
            if detect_block(ctx) is None and verify_navigate(goal, after).ok:
                self._verdict(goal, verify_navigate(goal, after)); return
            raise GoalFailed(r["error"] or "navigation failed")
        await self._guard_blocked(goal, ctx)
        ctx, after, _ = await self._observe()
        if not self._verdict(goal, verify_navigate(goal, after)):
            raise GoalFailed(f"navigation did not reach the requested site (now on {after.url})")

    # ---- SEARCH -------------------------------------------------------------
    async def _type_ranked(self, goal: Goal, ctx: dict, text: str, intent: str, hint: str = "", reason: str = "") -> Candidate | None:
        """Type into the best-ranked field; read the value back from THAT field; on a mismatch try the next candidate.

        Returns the candidate that verifiably holds the text, or None when no ranked field did.
        """
        ranking = rank_fields(ctx, intent, hint)
        for el, why in ranking.rejected[:6]:
            self.rec(ev.OBSERVE, goal, rejected_field=str(el.get("name") or el.get("placeholder") or el.get("tag"))[:60], why=why)
        cands = ranking.candidates[:3]
        for i, cand in enumerate(cands):
            tgt = target_dict(cand, ranking.candidates)
            if not tgt:
                continue
            self.rec(ev.OBSERVE, goal, field_candidate=cand.label[:60], kind=cand.kind, score=round(cand.score, 1),
                     reasons=cand.reasons[:4], rank=i + 1)
            r = await self._exec(goal, Action(type=ActionType.TYPE, text=text, target=Target(**tgt),
                                              reason=reason or f"Type into the {cand.kind} field '{cand.label[:40]}'"))
            if not r["ok"]:
                self.rec(ev.RECOVERY, goal, level=3, strategy="next_field_candidate", reason=str(r["error"])[:140], url=self._url())
                continue
            chk = await verify_in_field(self.page, cand, text)
            if chk.get("ok"):
                return cand
            self.rec(ev.RECOVERY, goal, level=3, strategy="next_field_candidate",
                     reason=f"the chosen field did not hold the text (found={chk.get('found')})", url=self._url())
        return None

    async def _g_search(self, goal: Goal) -> None:
        query = goal.params["query"]
        site = goal.params.get("site")
        if site:
            from agent.goal_verify import host_matches_site
            from agent.site_resolver import resolve_site_url
            url = resolve_site_url(site)
            if url and not host_matches_site(self.page.url, site, url):
                nav = Goal(id=goal.id, kind=GoalKind.NAVIGATE, text=f"open {site}", params={"url": url, "site": site})
                await self._g_navigate(nav)
        ctx, start, _ = await self._observe()
        await self._guard_blocked(goal, ctx)
        q_n = re.sub(r"\s+", " ", query.lower())
        if is_search_url(self.page.url) and q_n.replace(" ", "+") in self.page.url.lower().replace("%20", "+").replace("%2b", "+"):
            self._verdict(goal, Verdict(True, "the results for this query are already shown (state preserved)", {"skipped": True}))
            self.state.last_search_query = query
            return

        cand = await self._type_ranked(goal, ctx, query, F_SEARCH, "search", reason=f"Type the search query: {query}")
        if cand is None:
            if await self._ai_fallback(goal, "no search field accepted the query"):
                self.state.last_search_query = query
                return
            raise GoalFailed("no search field was found that held the query text")
        box = Target(**target_dict(cand, [cand]))
        typed_ok = True
        self.rec(ev.VERIFICATION, goal, result="complete", scope="typed", reason=f"search field contains {query!r}")

        before_submit = (await self._observe())[1]
        r = await self._exec(goal, Action(type=ActionType.PRESS, key="Enter", target=box, reason="Submit the search"))
        ctx, after, _ = await self._observe()
        submitted = r["ok"] and (before_submit.url_key != after.url_key or before_submit.text_digest != after.text_digest
                                 or after.page_id != before_submit.page_id)
        if not submitted:
            # Recovery: Enter did nothing - try the submit button next to the field (alternate strategy)
            self.rec(ev.RECOVERY, goal, level=3, strategy="click_submit_button", reason="Enter produced no change", url=self._url())
            btn = _find_by_text(ctx, r"^(?:search|go|submit|find)$|search", roles=("button",))
            if btn is not None:
                r2 = await self._exec(goal, Action(type=ActionType.CLICK, target=_interactive_target(btn, fallback_role="button"),
                                                   reason="Click the search button"))
                ctx, after, _ = await self._observe()
                submitted = r2["ok"] and (before_submit.url_key != after.url_key or before_submit.text_digest != after.text_digest)
        await self._guard_blocked(goal, ctx)
        ctx, after, _ = await self._observe()
        oc = classify_search_outcome(start, after, ctx, query, typed_ok=typed_ok, submitted=submitted)
        self.state.last_search_query = query
        self.state.last_search_outcome = oc.outcome.value
        self.rec(ev.VERIFICATION, goal, scope="search_outcome", result=oc.outcome.value, reason=oc.reason, url=self._url(), **oc.evidence)
        if oc.outcome in (Outcome.BLOCKED_PAGE,):
            raise GoalBlocked(oc.reason)
        if oc.outcome in (Outcome.EMPTY_RESULTS, Outcome.ERROR_PAGE):
            self._verdict(goal, Verdict(False, oc.reason))
            raise GoalFailed(f"the search returned no usable page: {oc.reason}")
        if oc.outcome.search_executed:
            self._verdict(goal, Verdict(True, oc.reason, {"search_outcome": oc.outcome.value}))
            return
        if not self._verdict(goal, verify_search(goal, start, after, typed_ok, submitted)):
            raise GoalFailed(f"the search did not produce a results state ({oc.outcome.value})")

    # ---- TYPE ---------------------------------------------------------------
    async def _g_type(self, goal: Goal) -> None:
        text = goal.params["text"]
        hint = goal.params.get("target") or ""
        deadline = time.monotonic() + min(6.0, max(0.0, self.rt.budget.remaining() - 5))
        while True:
            ctx, _, _ = await self._observe()
            ranking = rank_fields(ctx, F_TEXT, hint, allow_weak_fallback=True)
            usable = [c for c in ranking.candidates if c.kind != F_SEARCH] or (ranking.candidates if time.monotonic() >= deadline else [])
            if usable or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.5)       # hydration: wait for a real field before falling back to a search box
        if not ranking.candidates:
            await self._guard_blocked(goal, ctx)
            from agent.blocked_page import looks_like_login_wall
            if looks_like_login_wall(ctx):
                raise GoalBlocked("A login/sign-up wall is shown and no text field is available, so the text cannot be typed.")
            if await self._ai_fallback(goal, "no text field found"):
                return
            raise GoalFailed("no suitable text field was found to type into" + (f" ({hint})" if hint else ""))
        cand = await self._type_ranked(goal, ctx, text, F_TEXT, hint)
        if cand is None:
            self._verdict(goal, Verdict(False, f"no ranked field held {text!r} afterwards"))
            raise GoalFailed("the typed text was not found in the intended field afterwards")
        self._verdict(goal, Verdict(True, f"the {cand.kind} field '{cand.label[:40]}' contains {text!r}"))

    # ---- FILL (ordinary identifiers only; verified on the live page; never submitted) ----------------------
    async def _g_fill(self, goal: Goal) -> None:
        p = goal.params
        fld = p.get("field")
        if p.get("secret"):
            raise GoalBlocked(f"NEXA never enters a {fld} from a request. Please type it yourself in the open tab.")
        value = str(p.get("value") or "").strip()
        if not value:
            from agent.understanding import Clarification
            raise GoalNeedsInput(Clarification(str(fld), f"Which {'phone number' if fld == 'phone' else 'email address'} should I enter?"))
        intent = F_PHONE if fld == "phone" else F_EMAIL
        hint = "phone number" if fld == "phone" else "email"
        deadline = time.monotonic() + min(8.0, max(0.0, self.rt.budget.remaining() - 5))
        while True:                                   # hydration: the field may appear a moment after the click that opened it
            ctx, _, _ = await self._observe()
            ranking = rank_fields(ctx, intent, hint, allow_weak_fallback=False)
            if ranking.candidates or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.5)
        if not ranking.candidates:
            await self._guard_blocked(goal, ctx)
            self.rec(ev.VERIFICATION, goal, result="incomplete", scope="fill",
                     reason=f"no visible {fld} field on {self._url()}", verdict="INCOMPLETE")
            raise GoalFailed(f"no {fld} field is visible on the current page ({self._url()}), so nothing was entered")
        cands = ranking.candidates
        if len(cands) >= 2 and cands[0].score - cands[1].score < 5 and cands[0].kind == cands[1].kind:
            names = ", ".join(repr(c.label[:40]) for c in cands[:3])
            raise GoalBlocked(f"more than one {fld} field matches ({names}); I will not guess which one you mean. "
                              "Tell me which field to use.")
        for i, cand in enumerate(cands[:2]):
            tgt = target_dict(cand, cands)
            if not tgt:
                continue
            self.rec(ev.OBSERVE, goal, field_candidate=cand.label[:60], kind=cand.kind, score=round(cand.score, 1),
                     reasons=cand.reasons[:4], rank=i + 1)
            r = await self._exec(goal, Action(type=ActionType.TYPE, text=value, target=Target(**tgt),
                                              reason=f"Enter the {fld} in the '{cand.label[:40]}' field"))
            if not r["ok"]:
                self.rec(ev.RECOVERY, goal, level=3, strategy="next_field_candidate", reason=str(r["error"])[:140], url=self._url())
                continue
            chk = await verify_in_field(self.page, cand, value)
            actual = str(chk.get("value") or "")
            if chk.get("found") and value_matches(intent, actual, value):
                self._verdict(goal, Verdict(True, f"the '{cand.label[:40]}' field holds the requested {fld} (read back from the page)",
                                            {"field_label": cand.label[:60], "field_kind": cand.kind, "read_back": True,
                                             "requirement": "fill", "submitted": False}))
                return
            self.rec(ev.RECOVERY, goal, level=3, strategy="next_field_candidate",
                     reason=f"read-back mismatch: the field holds {mask_len(actual)} characters, not the requested value", url=self._url())
        self._verdict(goal, Verdict(False, f"the {fld} value was not found in the field afterwards"))
        raise GoalFailed(f"the {fld} was typed but could not be verified in the page's field")

    # ---- CLICK --------------------------------------------------------------
    async def _g_click(self, goal: Goal) -> None:
        tried: set[str] = set()
        for attempt in range(1, settings.GOAL_MAX_STEPS + 1):
            self._check_deadline()
            ctx, before, sig_b = await self._observe()
            await self._guard_blocked(goal, ctx)
            el = self._pick_click_element(goal, ctx, tried)
            if el is None:
                # recovery ladder: bounded scroll, then scoped AI, then honest failure
                if attempt <= 2 and await self._bounded_scroll_for_more(goal, ctx):
                    continue
                if await self._ai_fallback(goal, "no matching element is visible"):
                    return
                raise GoalFailed(f"could not find an element to click for: {goal.label()}")
            name = str(el.get("name") or el.get("text") or "")[:70]
            tried.add(name.lower())
            href = str(el.get("href") or "")
            if goal.params.get("new_tab") and href and not href.lower().startswith(("javascript:", "#")):
                act = Action(type=ActionType.NEW_TAB, url=urljoin(self.page.url, href), reason=f"Open '{name}' in a new tab")
            else:
                act = Action(type=ActionType.CLICK, target=_interactive_target(el, fallback_role="link"), reason=f"Click '{name}'")
            r = await self._exec(goal, act)
            ctx_a, after, _ = await self._observe()
            kind = self._progress(goal, before, after, act, r["ok"], r["error"])
            if r["ok"]:
                await self._guard_blocked(goal, ctx_a)
                ctx_a, after, _ = await self._observe()
                v = verify_click(goal, before, after, r["evidence"])
                if not v.ok and "no observable change" in v.reason:
                    # a tab can open AFTER the click returned: wait (bounded) and adopt it before judging
                    if await self._await_late_page(before.page_count):
                        ctx_a, after, _ = await self._observe()
                        v = verify_click(goal, before, after, r["evidence"])
                if self._verdict(goal, v):
                    return
            else:
                self.rec(ev.RECOVERY, goal, level=3, strategy="alternate_target", reason=str(r["error"])[:160], url=self._url())
            # verification failed: late popups are checked by sync_page; next attempt picks a different element
            self.state.recovery_count += 1
            self.rec(ev.RECOVERY, goal, level=3, strategy="alternate_target",
                     reason=f"'{name}' did not produce the expected effect ({kind.value}); trying another candidate", url=self._url())
        raise GoalFailed(f"clicking did not reach the expected result for: {goal.label()}")

    async def _await_late_page(self, count_before: int, max_s: float = 2.6) -> bool:
        end = time.monotonic() + max_s
        while time.monotonic() < end:
            await asyncio.sleep(0.4)
            self.page = await self.sync_page(self.task, self.page, self.step)
            if len(browser_manager.pages_snapshot()) > count_before:
                self.rec(ev.RECOVERY, None, level=2, strategy="late_page_adopted", url=self._url(),
                         reason="a new tab opened after the click returned")
                await self._settle()
                return True
        return False

    def _pick_click_element(self, goal: Goal, ctx: dict, tried: set[str]) -> dict | None:
        p = goal.params
        if p.get("ordinal"):
            what = p.get("what") or "result"
            q = self.state.last_search_query or ""
            instr = (f"search for {q} and " if q else "") + f"open the first {what}"
            ranked = [e for e in _ranked_result_elements(ctx, instr)
                      if str(e.get("name") or e.get("text") or "").strip().lower()[:70] not in tried]
            if not ranked:
                return None
            idx = _ORD_INDEX.get(p["ordinal"], 0)
            if idx == -1:
                return ranked[-1]
            return ranked[idx] if idx < len(ranked) and not tried else ranked[0]
        return _find_by_text(ctx, p.get("target") or "", exclude=tried)

    async def _bounded_scroll_for_more(self, goal: Goal, ctx: dict) -> bool:
        before = await browser_manager.page_state_signature(self.page)
        r = await self._exec(goal, Action(type=ActionType.SCROLL, direction="down", amount=800, reason="Recovery: reveal more of the page"))
        after = await browser_manager.page_state_signature(self.page)
        self.rec(ev.RECOVERY, goal, level=3, strategy="scroll", result="moved" if before.get("scroll_y") != after.get("scroll_y") else "no_movement",
                 url=self._url())
        return bool(r["ok"]) and before.get("scroll_y") != after.get("scroll_y")

    # ---- PRESS / SCROLL / FIND / WAIT --------------------------------------
    async def _g_press(self, goal: Goal) -> None:
        _, _, before = await self._observe()
        r = await self._exec(goal, Action(type=ActionType.PRESS, key=goal.params["key"], reason=f"Press {goal.params['key']}"))
        if not r["ok"]:
            raise GoalFailed(f"key press failed: {r['error']}")
        _, _, after = await self._observe()
        if not self._verdict(goal, verify_press(goal, before, after)):
            raise GoalFailed(f"pressing {goal.params['key']} changed nothing observable")

    async def _g_scroll(self, goal: Goal) -> None:
        direction = goal.params.get("direction", "down")
        to_edge = bool(goal.params.get("to"))
        first = await browser_manager.page_state_signature(self.page)
        last = first
        for _ in range(8 if to_edge else 1):
            before = await browser_manager.page_state_signature(self.page)
            r = await self._exec(goal, Action(type=ActionType.SCROLL, direction=direction, amount=900 if to_edge else 700,
                                              reason=f"Scroll {direction}"))
            if not r["ok"]:
                raise GoalFailed(f"scroll failed: {r['error']}")
            last = await browser_manager.page_state_signature(self.page)
            if last.get("scroll_y") == before.get("scroll_y"):
                break
        v = verify_scroll(goal, first, last)
        self.rec(ev.OBSERVE, goal, scroll_before=first.get("scroll_y"), scroll_after=last.get("scroll_y"))
        if not self._verdict(goal, v):
            raise GoalFailed(v.reason)

    async def _g_find(self, goal: Goal) -> None:
        target = re.sub(r"^(?:the|a|an)\s+", "", goal.params["target"], flags=re.I)
        for attempt in range(6):
            self._check_deadline()
            try:
                res = await self.page.evaluate(FIND_JS, target)
            except Exception as exc:
                raise GoalFailed(f"could not search the page: {exc}")
            if res.get("found"):
                self._verdict(goal, Verdict(True, f"found {res.get('tag')} '{res.get('text')}' (scrolled into view)", res))
                return
            before = await browser_manager.page_state_signature(self.page)
            r = await self._exec(goal, Action(type=ActionType.SCROLL, direction="down", amount=800, reason="Scroll to look for the target"))
            after = await browser_manager.page_state_signature(self.page)
            self.rec(ev.RECOVERY, goal, level=3, strategy="scroll_until_visible", url=self._url(),
                     result="moved" if before.get("scroll_y") != after.get("scroll_y") else "no_movement")
            if before.get("scroll_y") == after.get("scroll_y"):
                break
        if await self._ai_fallback(goal, f"'{target}' is not visible on the page"):
            return
        raise GoalFailed(f"'{target}' was not found on the page")

    async def _g_wait(self, goal: Goal) -> None:
        secs = min(float(goal.params.get("seconds", 2)), max(0.0, self.rt.budget.remaining() - 5))
        await asyncio.sleep(secs)
        self._verdict(goal, Verdict(True, f"waited {secs:.1f}s"))

    # ---- history / tabs -----------------------------------------------------
    async def _g_history(self, goal: Goal) -> None:
        back = goal.kind == GoalKind.BACK
        _, before, _ = await self._observe()
        expected = self.state.expected_back(before.page_id) if back else self.state.expected_forward(before.page_id)
        r = await self._exec(goal, Action(type=ActionType.BACK if back else ActionType.FORWARD,
                                          reason="Go back in browser history" if back else "Go forward in browser history"))
        if not r["ok"]:
            raise GoalFailed(r["error"] or "history navigation failed")
        # Book-keeping BEFORE observing: observing records the visited URL, which would otherwise be
        # appended to the history as if it were a new forward navigation.
        if self.page.url != before.url:
            (self.state.did_back if back else self.state.did_forward)(before.page_id if self.page else 0)
        ctx, after, _ = await self._observe()
        await self._guard_blocked(goal, ctx)
        v = verify_history(goal, before, after, expected)
        if not self._verdict(goal, v):
            raise GoalFailed(v.reason)

    async def _g_new_tab(self, goal: Goal) -> None:
        if not goal.params.get("url") and goal.params.get("site"):
            raise GoalFailed(f"I do not know which website '{goal.params['site']}' is. Give a URL.")
        _, before, _ = await self._observe()
        r = await self._exec(goal, Action(type=ActionType.NEW_TAB, url=goal.params.get("url"), reason=f"Open {goal.params.get('url') or 'a blank page'} in a new tab"))
        if not r["ok"]:
            raise GoalFailed(r["error"] or "could not open a new tab")
        ctx, after, _ = await self._observe()
        await self._guard_blocked(goal, ctx)
        ctx, after, _ = await self._observe()
        if not self._verdict(goal, verify_new_tab(goal, before, after)):
            raise GoalFailed("the new tab was not opened/adopted as expected")

    async def _g_switch(self, goal: Goal) -> None:
        _, before, _ = await self._observe()
        r = await self._exec(goal, Action(type=ActionType.SWITCH_TAB, value=goal.params.get("which", "other"), reason="Switch tab"))
        if not r["ok"]:
            raise GoalFailed(r["error"] or "could not switch tab")
        _, after, _ = await self._observe()
        if not self._verdict(goal, verify_switch(before, after)):
            raise GoalFailed("the active page did not change")

    async def _g_close(self, goal: Goal) -> None:
        _, before, _ = await self._observe()
        r = await self._exec(goal, Action(type=ActionType.CLOSE_TAB, reason="Close the current tab"))
        if not r["ok"]:
            raise GoalFailed(r["error"] or "could not close the tab")
        _, after, _ = await self._observe()
        if not self._verdict(goal, verify_close(before, after)):
            raise GoalFailed("the tab count did not decrease")

    # ---- EXTRACT --------------------------------------------------------------
    async def _g_extract(self, goal: Goal) -> None:
        req = goal.params["request"]
        spec = extraction_spec(req)
        r = await self._exec(goal, Action(type=ActionType.EXTRACT, reason=req))
        if not r["ok"]:
            raise GoalFailed(f"extraction failed: {r['error']}")
        records = (r["result"] or {}).get("extracted") or []
        for rec in reversed(records):
            ok, why = validate_record(spec, rec)
            if not ok:
                self.rec(ev.VERIFICATION, goal, result="incomplete", reason=f"extraction rejected: {why}")
                continue
            dom_ok, dom_why = await verify_in_dom(self.page, rec)
            if dom_ok:
                self.state.extracted.append(rec)
                self._verdict(goal, Verdict(True, f"{spec['kind']} {rec.get('text')!r} ({why}; {dom_why})", {"value": rec.get("text")}))
                return
        raise GoalFailed("no extracted value matched the requested kind with DOM provenance")

    # ---- ADAPTIVE (AI scoped to ONE goal) -----------------------------------
    async def _g_adaptive(self, goal: Goal) -> None:
        if not await self._ai_fallback(goal, "no deterministic rule understands this step"):
            raise GoalFailed(f"I could not safely perform this step without AI, and AI is unavailable: {goal.text!r}")

    async def _ai_fallback(self, goal: Goal, why: str) -> bool:
        """Ask the AI planner for actions for THIS goal only. The AI may propose; only evidence completes."""
        order = list(getattr(self.rt, "order", None) or ["groq", "gemini", "ollama"])
        if not any(self.providers.get(n) for n in order):
            return False
        start_ctx, start_obs, _ = await self._observe()
        tracker = ProgressTracker(goal.text)
        executed = 0
        last_kind: Progress | None = None
        for _ in range(settings.GOAL_MAX_STEPS):
            self._check_deadline()
            ctx, before, _ = await self._observe()
            ctx["completed_actions"] = [{"action": h["action"]} for h in self.state.action_history[-12:]]
            ctx["current_goal"] = {"id": goal.id, "text": goal.text, "label": goal.label(),
                                   "already_completed": [g.label() for g in self.state.completed_goals],
                                   "why_ai": why}
            try:
                out = await plan_next_action(
                    instruction=goal.text, context=ctx, providers=self.providers, health=self.rt.health,
                    budget=self.rt.budget, recorder=self.rt.recorder, step=self.step + 1,
                    planner_mode="repair", gemini_timeout_s=settings.GEMINI_CALL_TIMEOUT_SECONDS,
                    ollama_request_timeout_s=settings.OLLAMA_EMERGENCY_TIMEOUT_SECONDS,
                    order=order, hosted_timeout_s=settings.HOSTED_PROVIDER_TIMEOUT_SECONDS,
                )
            except PlannerUnavailable as exc:
                self.rec(ev.RECOVERY, goal, level=6, strategy="ai_unavailable", reason=str(exc)[:200])
                return False
            plan: ActionPlan = out.plan
            if plan.done or not plan.actions:
                # AI_PROPOSED_DONE -> independent evidence decides
                changed = before.url_key != start_obs.url_key or before.page_id != start_obs.page_id \
                    or before.text_digest != start_obs.text_digest
                toks = goal_tokens(goal.text)
                hay = " ".join([before.url.lower(), before.title.lower(), " ".join(before.headings), before.text_head])
                cover = (sum(1 for t in toks if t in hay) / len(toks)) if toks else 1.0
                # Independent evidence only: the page must have changed because of an action in THIS goal.
                # Content coverage strengthens the verdict; without it the verdict is explicitly WEAK
                # (a free-form step cannot be proven from its wording) and is reported as such.
                strong = changed and executed > 0 and cover >= 0.5
                weak = changed and executed > 0 and last_kind == Progress.PROGRESS and not strong
                ok = strong or weak
                self.rec(ev.VERIFICATION, goal, result="complete" if ok else "incomplete", claimed_done=True, provider=out.provider,
                         reason=(f"AI proposed done; evidence: state changed={changed}, actions={executed}, "
                                 f"goal coverage={cover:.2f}" + ("; WEAK: verified only by an observed state change" if weak else "")),
                         verdict=("VERIFIED_COMPLETE" if strong else "VERIFIED_WEAK" if weak else "AI_PROPOSED_DONE_REJECTED"))
                if ok:
                    goal.note = "completed via scoped AI; " + ("verified from page content" if strong else "verified only by an observed page-state change")
                    goal.evidence["strength"] = "strong" if strong else "weak"
                    return True
                self.state.false_done_count += 1
                # a syntactically valid but semantically insufficient answer is a PLANNING-QUALITY failure of that
                # provider (not a connectivity success): it counts against the provider's health for this task
                if out.provider in self.providers and self.rt.health.record_false_done(out.provider):
                    self.rec(ev.PROVIDER_STATUS, goal, provider=out.provider, state="unavailable", failure="planning_quality",
                             reason=f"{out.provider} repeatedly returned done/no-action without satisfying the requirement")
                if self.state.false_done_count >= settings.MAX_FALSE_DONE_CLAIMS:
                    return False
                continue
            action = plan.actions[0]                       # exactly ONE action per planning response
            r = await self._exec(goal, action)
            _, after, _ = await self._observe()
            kind = self._progress(goal, before, after, action, r["ok"], r["error"])
            executed += 1 if r["ok"] else 0
            last_kind = kind
            if kind in (Progress.REPEATED_ACTION,):
                return False
        return False


def mask_len(v: str) -> str:
    return f"{len(v or '')}"


class _Deadline(Exception):
    pass


def _label(action: Action) -> str | None:
    t = action.target
    if t is None:
        return action.url or action.key or action.value
    return " ".join(b for b in (t.role, t.name or t.placeholder or t.label or t.css or t.xpath) if b) or None


def _find_by_text(ctx: dict, text: str, roles: tuple[str, ...] = ("link", "button", "tab", "menuitem"), exclude: set[str] | None = None) -> dict | None:
    """Best visible interactive element whose accessible name matches ``text`` (regex or words)."""
    exclude = exclude or set()
    text = (text or "").strip()
    if not text:
        return None
    try:
        rx = re.compile(text, re.I) if any(ch in text for ch in "^$|\\") else None
    except re.error:
        rx = None
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1]
    best, best_score = None, 0.0
    for e in ctx.get("elements") or []:
        if not isinstance(e, dict) or e.get("disabled") or e.get("visible") is False:
            continue
        role = str(e.get("role") or e.get("tag") or "").lower()
        if role in ("a",):
            role = "link"
        if roles and role not in roles and str(e.get("tag") or "").lower() not in {"a", "button", "summary"}:
            continue
        name = str(e.get("name") or e.get("text") or e.get("aria_label") or "").strip()
        if not name or name.lower()[:70] in exclude:
            continue
        nl = name.lower()
        score = 0.0
        if rx is not None:
            score = 100.0 if rx.search(name) else 0.0
        elif nl == text.lower():
            score = 120.0
        elif text.lower() in nl:
            score = 90.0 - min(len(nl) - len(text), 40)
        elif words:
            hit = sum(1 for w in words if w in nl)
            score = 60.0 * hit / len(words) if hit == len(words) else 0.0
        if score <= 0:
            continue
        region = str(e.get("region") or "")
        if region in {"main", "article", "content"}:
            score += 5
        elif region in {"footer"}:
            score -= 8
        if score > best_score:
            best, best_score = e, score
    return best
