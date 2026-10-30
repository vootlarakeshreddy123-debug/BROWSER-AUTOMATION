from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote_plus, unquote_plus

from agent.action_schema import (
    Action,
    ActionPlan,
    ActionType,
    Target,
    looks_sensitive,
)
from agent.recovery import RetryBudget
from agent.router import build_plan
from agent.workflow_memory import get_workflow, mark_replayed, mark_failed, save_workflow
from agent.planner import try_fast_path, try_live_path
from browser import inspector
from browser.executor import ExecutionError, execute_plan
from browser.manager import browser_manager
from config import settings
from database.models import TaskRecord, TaskStatus
from database.repository import task_repository
from services.ai_provider import AIProvider, get_adaptive_providers, get_ai_provider
from agent import events as ev
from agent.adaptive_planner import PlannerUnavailable, plan_next_action
from agent.budget import BudgetExhausted, TaskBudget
from agent.recovery_ladder import LadderState, alternate_click_target, scroll_action, strategy_name
from agent.typing_goal import next_typing_step, parse_typing_request, verify_typed_in_dom
from agent.extraction import extraction_spec, validate_record, verify_in_dom
from agent.page_identity import (
    is_search_url,
    navigation_regression_reason as _navigation_regression_reason,
    page_is_entity,
    title_core,
    urls_equivalent as _urls_equivalent,
)
from agent.placeholders import find_placeholders, placeholder_message
from agent.goal_runner import GoalRunner
from agent.goals import GoalKind, decompose, needs_goal_runner
from agent.site_resolver import resolve_site_url
from agent.goal_clauses import navigation_clauses, split_goal_clauses
from agent.progress import Progress, ProgressTracker, observe
from agent.provider_health import ProviderHealth
from agent.router import route_live_step
import time
from services.ws_manager import ws_manager


logger = logging.getLogger("agent.orchestrator")


_OPEN_CHROME_ONLY = re.compile(
    r"^(?:open|start)\s+(?:google\s+)?chrome\s*$",
    re.IGNORECASE,
)

_NEW_TAB_ONLY = re.compile(
    r"^(?:create|open)\s+(?:a\s+)?new\s+tab\s*$",
    re.IGNORECASE,
)

_MAX_LIVE_ACTION_STEPS = 24
# Adaptive loop contract: ONE AI response -> ONE executed action -> re-observe.
# (Was 2. The extra action was planned against a DOM that the first action
# could already have changed.)
_MAX_ACTIONS_PER_CHUNK = 1


_GITHUB_FIRST_ITEM_VARIANT = re.compile(
    r"^\s*(?:open|go\s+to|visit)\s+github\s*(?:,\s*)?(?:and\s+)?"
    r"(?:search|look\s+for|find)\s+(?P<query>.+?)\s+and\s+"
    r"(?:open|click)\s+(?:the\s+)?(?:first|top)\s+"
    r"(?P<item>.+?)\s+(?:repository|repo)\s*$",
    re.IGNORECASE,
)


def _direct_github_repository_plan(instruction: str) -> ActionPlan | None:
    """No site-specific task recipes; use the generic adaptive planner."""
    return None



def _normalize_deterministic_instruction(instruction: str) -> str:
    """
    Normalize common deterministic task wording before fast-path routing.
    """
    match = _GITHUB_FIRST_ITEM_VARIANT.match(instruction.strip())

    if not match:
        return instruction

    raw_query = match.group("query").strip()
    item = match.group("item").strip()

    query = re.sub(
        r"\s+(?:repository|repo)\s*$",
        "",
        raw_query,
        flags=re.IGNORECASE,
    ).strip()

    if query and item.casefold() == query.casefold():
        return (
            f"open github and search {query} "
            f"and open first repository"
        )

    return instruction


class TaskBlocked(Exception):
    """The task cannot proceed for a reason outside NEXA (e.g. a login wall)."""


async def _fresh_snapshot(page) -> dict:
    ctx = await inspector.snapshot(page)
    ctx["current_url"] = page.url
    ctx["current_title"] = await _safe_title(page)
    return ctx


class _TaskRuntime:
    """Per-task deadline, provider health and event recorder."""

    def __init__(self, task: TaskRecord) -> None:
        self.reset_budget()
        self._init_rest(task)

    def reset_budget(self) -> None:
        """Fresh deadline (also used when a paused task resumes: the user's thinking time must not count)."""
        total = max(20.0, settings.TASK_TIMEOUT_SECONDS - settings.TASK_DEADLINE_MARGIN_SECONDS)
        self.budget = TaskBudget(
            total_s=total,
            ai_fraction=settings.AI_PLANNING_BUDGET_FRACTION,
            recovery_fraction=settings.RECOVERY_BUDGET_FRACTION,
            browser_reserve_s=min(settings.BROWSER_RESERVE_SECONDS, total * 0.25),
        )

    def _init_rest(self, task: TaskRecord) -> None:
        from providers import registry as _reg
        try:
            self.selection = _reg.normalise_selection(getattr(task, "provider", None) or settings.AI_PROVIDER)
            self.order = _reg.order_for(self.selection)
        except ValueError:
            self.selection, self.order = "auto", ["groq", "gemini", "ollama"]
        self.health = ProviderHealth(
            hosted_cooldown_s=settings.HOSTED_PROVIDER_COOLDOWN_SECONDS,
            hosted_max_calls=settings.HOSTED_PROVIDER_MAX_CALLS_PER_TASK,
            gemini_cooldown_s=settings.GEMINI_COOLDOWN_SECONDS,
            gemini_timeout_cooldown_s=settings.GEMINI_TIMEOUT_COOLDOWN_SECONDS,
            gemini_model_error_cooldown_s=settings.GEMINI_MODEL_ERROR_COOLDOWN_SECONDS,
            ollama_cooldown_s=settings.OLLAMA_COOLDOWN_SECONDS,
            ollama_max_calls=settings.OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK,
            ollama_time_budget_s=settings.OLLAMA_EMERGENCY_BUDGET_SECONDS,
        )
        self.recorder = ev.ExecutionRecorder(
            task.id, sink=lambda payload, _id=task.id: ws_manager.send(_id, payload)
        )
        self.recorder.task = task
        task.events = self.recorder.events
        self.false_done_total = 0


_RUNTIME: dict[str, _TaskRuntime] = {}
from providers.registry import ALL as _PROVIDER_NAMES, LABELS as _PROVIDER_LABELS


def _runtime(task: TaskRecord) -> _TaskRuntime:
    rt = _RUNTIME.get(task.id)
    if rt is None:
        rt = _RUNTIME[task.id] = _TaskRuntime(task)
    return rt


def _target_label(action: Action) -> str | None:
    t = getattr(action, "target", None)
    if t is None:
        return action.url or action.key or None
    bits = [b for b in (t.role, t.name or t.label or t.placeholder or t.css) if b]
    return " ".join(bits) or None


async def _bounded_go_back(page, timeout_ms: int = 5000) -> bool:
    before = page.url
    try:
        await page.go_back(wait_until="commit", timeout=timeout_ms)
    except Exception:
        if page.url == before:
            return False
    await page.wait_for_timeout(300)
    return page.url != before


def _finish_deadline(task: TaskRecord, rt: _TaskRuntime, url: str, title: str, where: str,
                     retries: int) -> None:
    b = rt.budget.snapshot()
    task.mark_finished(
        TaskStatus.TIMEOUT,
        error=(
            f"Task deadline reached ({b['elapsed_s']}s of {b['total_s']}s) {where}. "
            f"Spent {b['ai_spent_s']}s on AI planning and {b['browser_spent_s']}s on browser actions. "
            "The live Chrome tab remains open for manual continuation."
        ),
        final_url=url, final_title=title, retries=retries,
        message="The task deadline was reached before the goal could be verified.",
    )
    task_repository.update(task)


def _url_navigation_is_only_bootstrap(instruction: str, plan: ActionPlan) -> bool:
    """True when a single explicit-URL navigate plan does NOT cover the whole goal."""
    from agent.progress import goal_tokens

    if len(plan.actions) != 1 or plan.actions[0].type != ActionType.NAVIGATE:
        return False
    if (plan.actions[0].reason or "").strip() != "Navigate to the explicitly requested URL.":
        return False
    rest = re.sub(r"https?://\S+", " ", instruction)
    rest_tokens = [t for t in goal_tokens(rest) if t not in {"navigate", "browse", "load", "launch", "website", "address"}]
    return bool(rest_tokens)


async def _sync_active_page(task: TaskRecord, page, step: int | None = None):
    """ONE source of truth for the active Page.

    Returns the Page NEXA must operate on, drains the browser manager's page
    adoptions into NEW_PAGE_DETECTED / PAGE_ADOPTED / ACTIVE_PAGE_CHANGED events,
    and re-targets a closed/stale local reference to the live page instead of
    re-bootstrapping (which used to navigate a successful page back to the
    site's homepage).
    """
    rt = _runtime(task)
    for item in browser_manager.drain_adoptions():
        rt.recorder.record(ev.NEW_PAGE_DETECTED, step=step, action=item.get("action"),
                           from_url=item.get("from_url"), url=item.get("to_url"),
                           page_count=item.get("page_count"))
        rt.recorder.record(ev.PAGE_ADOPTED, step=step, url=item.get("to_url"), title=item.get("title"))
    try:
        await browser_manager.adopt_late_popups(browser_manager.current_page or page)
    except Exception as exc:
        logger.debug("late popup check failed: %s", exc)
    for item in browser_manager.drain_adoptions():
        rt.recorder.record(ev.NEW_PAGE_DETECTED, step=step, action=item.get("action"),
                           from_url=item.get("from_url"), url=item.get("to_url"),
                           page_count=item.get("page_count"))
        rt.recorder.record(ev.PAGE_ADOPTED, step=step, url=item.get("to_url"), title=item.get("title"))
    current = browser_manager.current_page
    if current is None and page is not None:
        try:
            stale = page.is_closed()
        except Exception:
            stale = True
        if stale:
            open_pages = browser_manager.pages_snapshot()
            if open_pages and await browser_manager.adopt_page(open_pages[-1]):
                current = browser_manager.current_page
    if current is not None and current is not page:
        try:
            old_url = page.url if page is not None else None
        except Exception:
            old_url = None
        rt.recorder.record(ev.ACTIVE_PAGE_CHANGED, step=step, from_url=old_url, url=current.url)
        return current
    return page


def _page_identity(page) -> tuple[int, int]:
    return id(page) if page is not None else 0, len(browser_manager.pages_snapshot()) or 1


async def _execute_recorded(task: TaskRecord, page, actions, retry_budget, phase: str):
    """execute_plan() for the non-adaptive paths, with structured events.

    Records ROUTER (first use), ACTION_START and ACTION_RESULT so the
    deterministic fast path is as observable as the adaptive loop. Behaviour
    (retries, timeouts, exceptions) is exactly execute_plan's.
    """
    rt = _runtime(task)
    types = [a.type.value for a in actions]
    if not any(e["type"] == ev.BROWSER_READY for e in rt.recorder.events):
        rt.recorder.record(ev.BROWSER_READY, url=getattr(page, "url", None))
    if not any(e["type"] == ev.ROUTER for e in rt.recorder.events):
        rt.recorder.record(ev.ROUTER, route="deterministic", phase=phase, actions=len(actions))
    rt.recorder.record(ev.ACTION_START, phase=phase, action="+".join(types)[:80], count=len(actions),
                       target=_target_label(actions[0]) if actions else None, url=getattr(page, "url", None))
    t0 = time.perf_counter()
    try:
        result = await execute_plan(page, actions, retry_budget)
    except Exception as exc:
        dt = time.perf_counter() - t0
        rt.budget.charge_browser(dt)
        rt.recorder.record(ev.ACTION_RESULT, phase=phase, action="+".join(types)[:80], count=len(actions),
                           result="failed", duration_ms=int(dt * 1000), error=str(getattr(exc, "message", exc))[:200],
                           url=getattr(page, "url", None))
        raise
    dt = time.perf_counter() - t0
    rt.budget.charge_browser(dt)
    rt.recorder.record(ev.ACTION_RESULT, phase=phase, action="+".join(types)[:80], count=len(actions),
                       result="ok", duration_ms=int(dt * 1000), url=getattr(page, "url", None),
                       title=await _safe_title(page))
    return result


def _final_verified(task: "TaskRecord", reason: str) -> None:
    """Record the FINAL verification verdict for paths that are not run by the goal runner (which records its own)."""
    try:
        _runtime(task).recorder.record(ev.VERIFICATION, scope="final", result="complete", verdict="VERIFIED_COMPLETE", reason=reason)
    except Exception:
        pass


def _redact(text: str) -> str:
    from agent.contract import redact_instruction
    return redact_instruction(text)


async def _pause_before_start(task: TaskRecord, instruction: str, goals, clar) -> None:
    rt = _runtime(task)
    rt.recorder.record(ev.INPUT_REQUESTED, field=clar.field, question=clar.question)
    pend = dict(clar.to_dict(), goal_id=next((g.id for g in goals if g.kind.value in ("compose",)), None))
    _RESUME[task.id] = {"goals": goals, "index": 0, "approved": set(), "instruction": instruction,
                        "started": False, "ready": False}
    task.message = clar.question + " No browser action has been taken yet."
    task.mark_paused(TaskStatus.NEEDS_INPUT, pend)
    task_repository.update(task)
    await ws_manager.send(task.id, _terminal_payload(task))


async def _run_understood(task: TaskRecord, instruction: str) -> bool:
    """Returns True when the task was fully handled here (paused, or run through the goal runner)."""
    from agent.understanding import (UnderstandingError, build_understanding_prompt, merge, understand,
                                     validate_ai_understanding)
    from agent.goals import GoalKind
    rt = _runtime(task)
    und = understand(instruction, resolve_site_url)
    task.understanding = und.to_dict()
    rt.recorder.record(ev.UNDERSTANDING, intent=und.intent, risk=und.risk, confidence=round(und.confidence, 2),
                       subgoals=len(und.subgoals), missing=[m.field for m in und.missing] or None,
                       requires_confirmation=und.requires_confirmation or None, source=und.source,
                       objective=und.objective[:160])
    has_message = any(g.kind in (GoalKind.COMPOSE, GoalKind.SEND) for g in und.goals)

    # unsure about the wording of the request -> ONE bounded AI call, strictly validated, never authoritative
    if (not has_message and all(m.field == "requirement" for m in und.missing) and und.confidence < 0.7 and settings.AI_UNDERSTANDING_ENABLED
            and any(g.kind == GoalKind.ADAPTIVE for g in und.goals)):
        try:
            from agent.ai_assist import ask_json
            providers = get_adaptive_providers()
            data, provider = await ask_json(
                prompt=build_understanding_prompt(instruction, und), purpose="understanding", providers=providers,
                order=rt.order, health=rt.health, budget=rt.budget, recorder=rt.recorder)
            ai = merge(und, validate_ai_understanding(data, instruction))
            rt.recorder.record(ev.UNDERSTANDING, source="ai", provider=provider, subgoals=len(ai.subgoals),
                               objective=ai.objective[:160], missing=[m.field for m in ai.missing] or None)
            und, task.understanding = ai, ai.to_dict()
            has_message = any(g.kind in (GoalKind.COMPOSE, GoalKind.SEND) for g in und.goals)
            if not und.missing:
                await _run_goal_task(task, instruction, und.goals)
                return True
        except (PlannerUnavailable, UnderstandingError) as exc:
            rt.recorder.record(ev.RECOVERY, level=6, strategy="understanding_fallback",
                               reason=f"AI understanding unavailable or invalid, using the deterministic parse: {str(exc)[:160]}")

    if und.missing:
        # an essential detail is missing or a requirement could not be represented: ask, never run a partial task
        await _pause_before_start(task, instruction, und.goals, und.missing[0])
        return True
    if und.intent == "login_assist":
        await _run_goal_task(task, instruction, und.goals)
        return True
    if has_message:
        await _run_goal_task(task, instruction, und.goals)
        return True
    return False


async def _run_goal_task(task: TaskRecord, instruction: str, goals, resume: dict | None = None) -> None:
    """Goal-runner path (agent/goal_runner.py): ordered goals, per-goal verification, AI only when needed."""
    rt = _runtime(task)
    if resume and resume.get("started") and browser_manager.current_page is not None:
        page = browser_manager.current_page             # continue in the SAME live tab the user can see
    else:
        _, page = await browser_manager.new_task_page()
    await browser_manager.focus_current_page()
    rt.recorder.record(ev.BROWSER_READY, url=page.url)
    rt.recorder.record(ev.ROUTER, route="goal_runner",
                       reason=f"{len(goals)} ordered goals: " + " -> ".join(g.label() for g in goals)[:200])
    try:
        providers = get_adaptive_providers()
    except Exception as exc:
        logger.warning("Adaptive providers unavailable: %s", exc)
        providers = {}
    runner = GoalRunner(task=task, page=page, goals=goals, instruction=instruction, runtime=rt,
                        providers=providers, sync_page=_sync_active_page, safe_title=_safe_title,
                        start_index=(resume or {}).get("index", 0), approved=(resume or {}).get("approved"))
    result = await runner.run()
    runner.contract.sync(runner.state.goals)
    task.contract, task.contract_state = runner.contract.to_dict(), runner.contract.state.value
    page = await _sync_active_page(task, result.page or page)
    task.actions_count = (task.actions_count or 0) + result.actions if resume else result.actions
    used_ai = any(e["type"] == ev.AI_PLAN and e.get("provider") in _PROVIDER_NAMES for e in rt.recorder.events)
    await browser_manager.focus_current_page()
    common = dict(final_url=page.url, final_title=await _safe_title(page), retries=runner.retry_budget.used,
                  plan_source="ai" if used_ai else "fast_path")
    if result.status in ("awaiting_confirmation", "needs_input"):
        _RESUME[task.id] = {"goals": goals, "index": result.resume_index or 0, "approved": set(runner.approved),
                            "instruction": instruction, "started": True, "ready": False}
        for k, v in common.items():
            setattr(task, k, v)
        task.message = result.message + " The live Chrome tab is left as it is."
        task.mark_paused(TaskStatus.AWAITING_CONFIRMATION if result.status == "awaiting_confirmation" else TaskStatus.NEEDS_INPUT,
                         result.pending or {})
        task_repository.update(task)
        await ws_manager.send(task.id, _terminal_payload(task))
        return
    if result.status == "success":
        note = ((task.understanding or {}).get("entities") or {}).get("note")
        task.mark_finished(TaskStatus.SUCCESS, result=result.result_text,
                           message=(result.message + " " + (note + " " if note else "") + "The live Chrome tab is ready for you.").replace("  ", " "),
                           pending=None, **common)
    elif result.status == "timeout":
        task.mark_finished(TaskStatus.TIMEOUT, error=result.error, message="The task deadline was reached.", pending=None, **common)
    else:
        task.mark_finished(TaskStatus.FAILED, error=result.error,
                           message="The live Chrome tab remains open for manual continuation.", pending=None, **common)
    task_repository.update(task)
    await ws_manager.send(task.id, _terminal_payload(task))


_PAUSED = {TaskStatus.AWAITING_CONFIRMATION, TaskStatus.NEEDS_INPUT}
_RESUME: dict[str, dict] = {}          # task id -> {"goals","index","approved","instruction","ready"} while a task is paused


async def run_task(task: TaskRecord) -> None:
    entry = _RESUME.get(task.id)
    if entry and entry.get("ready") and task.id in _RUNTIME:
        rt = _RUNTIME[task.id]
        rt.reset_budget()
        rt.recorder.finalized = False
        rt.recorder.record(ev.TASK_START, resumed=True, instruction=_redact(task.instruction)[:200],
                           deadline_s=round(rt.budget.total_s, 1), ai_budget_s=round(rt.budget.ai_budget_s, 1))
    else:
        rt = _RUNTIME[task.id] = _TaskRuntime(task)
        rt.recorder.record(ev.TASK_START, instruction=_redact(task.instruction)[:200],
                           deadline_s=round(rt.budget.total_s, 1),
                           ai_budget_s=round(rt.budget.ai_budget_s, 1))
    try:
        await _run_task_inner(task)
    finally:
        if task.status not in _PAUSED:
            _RUNTIME.pop(task.id, None)
            _RESUME.pop(task.id, None)


def forget_paused_task(task_id: str) -> None:
    """Drop the in-memory state of a paused task that was cancelled (the live tab is left as it is)."""
    _RESUME.pop(task_id, None)
    _RUNTIME.pop(task_id, None)


def respond_to_task(task: TaskRecord, *, approve: bool | None = None, answer: str | None = None) -> str:
    """Apply the user's reply to a paused task.  Returns "resume" (call run_task again) or "ended".

    approve=True  : the confirmed goal may execute.      approve=False: the task ends, nothing consequential happened.
    answer=<text> : fills the missing detail (recipient / body / ...) and re-runs the paused goal.
    """
    entry = _RESUME.get(task.id)
    if entry is None or task.status not in _PAUSED:
        raise ValueError("this task is not waiting for an answer")
    pend = task.pending or {}
    if task.status == TaskStatus.AWAITING_CONFIRMATION:
        if approve is not True:
            rt = _RUNTIME.get(task.id)
            if rt:
                rt.recorder.record(ev.VERIFICATION, result="declined", scope="confirmation",
                                   reason="the user declined; the action was not performed")
            task.mark_finished(TaskStatus.CANCELLED, error="You declined the action, so it was not performed.",
                               message="Nothing was sent or submitted. The browser tab was left as it is.", pending=None)
            task_repository.update(task)
            _RESUME.pop(task.id, None)
            return "ended"
        entry["approved"].add(int(pend.get("goal_id") or 0))
    else:
        text = (answer or "").strip()
        if not text:
            raise ValueError("an answer is required")
        goal = next((g for g in entry["goals"] if g.id == pend.get("goal_id")), None)
        field = pend.get("field") or "detail"
        if goal is None or goal.kind.value not in ("compose",):
            # the answer completes the REQUEST itself (e.g. an event title): restart with it appended
            task.instruction = f"{task.instruction.rstrip('. ')} ({field}: {text})"
            _RESUME.pop(task.id, None)
            task.status, task.pending, task.finished_at, task.duration_ms = TaskStatus.CREATED, None, None, None
            return "resume"
        if goal is not None:
            from agent.understanding import _EMAIL_ADDR as _addr
            if field == "recipient":
                m = _addr.search(text)
                goal.params["recipient"] = m.group(0) if m else text
            elif field in ("body", "subject", "title"):
                goal.params[field] = text
                goal.params.pop("needs_authoring", None)
            else:
                goal.params[field] = text
    entry["ready"] = True
    task.status, task.pending, task.finished_at, task.duration_ms = TaskStatus.CREATED, None, None, None
    task.resume_existing_browser = entry.get("started", False)
    return "resume"


async def _run_task_inner(task: TaskRecord) -> None:
    task.mark_running()
    task_repository.update(task)

    await ws_manager.send(
        task.id,
        {
            "type": "status",
            "status": TaskStatus.RUNNING.value,
        },
    )

    try:
        await asyncio.wait_for(
            _run(task),
            timeout=settings.TASK_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError:
        logger.exception(
            "Task %s timed out",
            task.id,
        )

        task.mark_finished(
            TaskStatus.TIMEOUT,
            error=(
                f"Automation exceeded "
                f"{settings.TASK_TIMEOUT_SECONDS:.0f}s (hard timeout) "
                f"while in phase {_RUNTIME[task.id].recorder.phase if task.id in _RUNTIME else 'unknown'}. "
                "The live Chrome tab remains open for manual continuation."
            ),
        )

        task_repository.update(task)

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

    except asyncio.CancelledError:
        logger.info(
            "Task %s cancelled",
            task.id,
        )

        if task.status != TaskStatus.CANCELLED:
            task.mark_finished(
                TaskStatus.CANCELLED,
                error="Cancelled by user.",
            )

        task_repository.update(task)

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

    except Exception as exc:
        logger.exception(
            "Unhandled error running task %s",
            task.id,
        )

        task.mark_finished(
            TaskStatus.FAILED,
            error=_friendly_error(exc),
        )

        task_repository.update(task)

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )


async def _run(task: TaskRecord) -> None:
    """
    Main NEXA browser-agent execution flow (see ARCHITECTURE.md).

        USER TASK -> placeholder guard
             -> workflow memory (verified replay only)
             -> router: deterministic fast path (0 AI calls)
             -> otherwise deterministic bootstrap + adaptive loop
                  observe -> plan ONE action (Gemini primary, Ollama bounded
                  emergency) -> execute -> sync active page -> verify progress
             -> independent goal verification -> success / honest failure
    """

    instruction = task.instruction.strip()

    _entry = _RESUME.get(task.id)
    if _entry and _entry.get("ready"):
        await _run_goal_task(task, _entry["instruction"], _entry["goals"], resume=_entry)
        return

    _placeholders = find_placeholders(instruction)
    if _placeholders:
        _runtime(task).recorder.record(ev.ROUTER, route="blocked", reason="unfilled placeholder: " + ", ".join(_placeholders[:3]))
        task.mark_finished(
            TaskStatus.FAILED,
            error=placeholder_message(_placeholders),
            plan_source="fallback",
            message="No browser action was taken.",
        )
        task_repository.update(task)
        await ws_manager.send(task.id, _terminal_payload(task))
        return

    # ===============================================================
    # 1. OPEN CHROME / NEW TAB
    # ===============================================================

    if (
        _OPEN_CHROME_ONLY.match(instruction)
        or _NEW_TAB_ONLY.match(instruction)
    ):
        _, page = await browser_manager.new_task_page(fresh_tab=True)

        await browser_manager.focus_current_page()

        title = await _safe_title(page)

        _final_verified(task, "opened a fresh tab as requested")
        task.mark_finished(
            TaskStatus.SUCCESS,
            final_url=page.url,
            final_title=title,
            message="New Chrome tab opened and handed to you.",
            actions_count=1,
            plan_source="fast_path",
        )

        task_repository.update(task)

        await browser_manager.focus_current_page()

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

        return

    # ===============================================================
    # 1b. TASK UNDERSTANDING (structured, validated; AI only when unsure)
    #     message composition / sending are handled here with draft-vs-send
    #     separation, clarification questions and the confirmation gate.
    # ===============================================================

    if await _run_understood(task, instruction):
        return

    # ===============================================================
    # 2. SENSITIVE ACTION PROTECTION
    # ===============================================================

    if looks_sensitive(instruction):
        task.mark_finished(
            TaskStatus.FAILED,
            error=(
                "This task looks like a sensitive action "
                "(purchase/delete/send) and requires explicit approval. "
                "It was not executed."
            ),
            plan_source="fallback",
        )

        task_repository.update(task)

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

        return

    # ===============================================================
    # 2b. MULTI-GOAL TASKS (back/forward/new tab/scroll/press/several searches)
    #     are decomposed into ordered goals and run one verified goal at a time.
    # ===============================================================

    _goals = decompose(instruction, resolve_site_url)
    if needs_goal_runner(_goals):
        await _run_goal_task(task, instruction, _goals)
        return

    # ===============================================================
    # 3. RESUME FROM THE LIVE CHROME TAB
    # ===============================================================

    if getattr(task, "resume_existing_browser", False):
        page = browser_manager.current_page
        if page is None:
            task.mark_finished(TaskStatus.FAILED, error="There is no live Chrome tab available to resume.", plan_source="fallback")
            task_repository.update(task)
            await ws_manager.send(task.id, _terminal_payload(task))
            return
        task.resume_existing_browser = False
        try:
            ai_provider = get_ai_provider()
            retry_budget = RetryBudget(max_retries=settings.BROWSER_MAX_RETRIES)
            result = await _run_live_interaction_loop(
                task=task, page=page, instruction=instruction,
                ai_provider=ai_provider, retry_budget=retry_budget,
                workflow_seed_actions=[],
            )
            if result is None:
                return
            page = await _sync_active_page(task, page)
            await browser_manager.focus_current_page()
            _final_verified(task, "the final-goal verifier agreed with the resumed live interaction")
            task.mark_finished(
                TaskStatus.SUCCESS, final_url=result.get("final_url", page.url),
                final_title=result.get("final_title", await _safe_title(page)),
                result=result.get("result"), retries=retry_budget.used,
                message=(result.get("message") or "Task resumed and completed.") + " The live Chrome tab is ready for you.",
            )
            task_repository.update(task)
            await ws_manager.send(task.id, _terminal_payload(task))
            return
        except Exception as exc:
            task.mark_finished(TaskStatus.FAILED, error=_friendly_error(exc), final_url=page.url, final_title=await _safe_title(page), message="Resume failed; the live Chrome tab remains open for manual continuation.")
            task_repository.update(task)
            await ws_manager.send(task.id, _terminal_payload(task))
            return

    # ===============================================================
    # 4. WORKFLOW MEMORY
    #
    # Saved workflows are learned artifacts, not hard-coded task recipes.
    # They are replayed only after the live browser independently verifies
    # that the current instruction was actually satisfied.
    # ===============================================================

    carry_page = None
    saved_workflow = get_workflow(instruction)

    if saved_workflow:
        logger.info(
            "Workflow memory candidate found for task %s: %s",
            task.id,
            instruction,
        )

        _, page = await browser_manager.new_task_page()

        await browser_manager.focus_current_page()
        _runtime(task).recorder.record(ev.BROWSER_READY, url=page.url)

        replay_budget = RetryBudget(
            max_retries=settings.BROWSER_MAX_RETRIES,
        )

        try:
            replay_actions = [
                Action.model_validate(item)
                for item in saved_workflow
            ]

            replay_result = await _execute_recorded(task, page, replay_actions, replay_budget, "workflow_replay")
            page = await _sync_active_page(task, page)

            # -------------------------------------------------------
            # IMPORTANT:
            # Inspect the actual browser after replay.
            # -------------------------------------------------------

            replay_context = await inspector.snapshot(page)

            replay_context["current_url"] = page.url
            replay_context["current_title"] = await _safe_title(page)

            replay_context["completed_actions"] = [
                {
                    "action": action.model_dump(mode="json"),
                    "step": index + 1,
                    "url_after": page.url,
                }
                for index, action in enumerate(replay_actions)
            ]

            replay_verified = await _page_proves_final_goal(
                instruction,
                replay_context,
                replay_context["completed_actions"],
                page,
            )

            # -------------------------------------------------------
            # ONLY VERIFIED REPLAYS MAY RETURN SUCCESS.
            # -------------------------------------------------------

            if replay_verified:
                logger.info(
                    "Saved workflow passed final-goal verification "
                    "for task %s.",
                    task.id,
                )

                mark_replayed(instruction)

                _final_verified(task, "the saved workflow passed independent final-goal verification")
                task.mark_finished(
                    TaskStatus.SUCCESS,
                    final_url=replay_result.get(
                        "final_url",
                        page.url,
                    ),
                    final_title=replay_result.get(
                        "final_title",
                        await _safe_title(page),
                    ),
                    retries=replay_budget.used,
                    actions_count=len(replay_actions),
                    plan_source="workflow",
                    message=(
                        "Replayed a previously successful workflow and "
                        "verified the requested goal from the live browser. "
                        "The live Chrome tab is ready for you."
                    ),
                )

                task_repository.update(task)

                await ws_manager.send(
                    task.id,
                    _terminal_payload(task),
                )

                return

            # -------------------------------------------------------
            # STALE / BAD WORKFLOW
            # -------------------------------------------------------

            logger.warning(
                "Saved workflow reached a page but failed final-goal "
                "verification for task %s. Rejecting replay and "
                "continuing with normal planning.",
                task.id,
            )
            mark_failed(instruction)
            # Keep the tab open: it may hold real progress (e.g. a result page that opened in a
            # new tab). The bootstrap below only reuses it when navigating would not regress.
            carry_page = page
            page = None

        except ExecutionError as exc:
            logger.warning(
                "Saved workflow replay failed for task %s; "
                "continuing with normal planning: %s",
                task.id,
                exc.message,
            )
            mark_failed(instruction)
            # Keep the tab open: it may hold real progress (e.g. a result page that opened in a
            # new tab). The bootstrap below only reuses it when navigating would not regress.
            carry_page = page
            page = None

        except Exception as exc:
            logger.warning(
                "Saved workflow replay produced an unexpected error "
                "for task %s; continuing with normal planning: %s",
                task.id,
                exc,
            )
            mark_failed(instruction)
            # Keep the tab open: it may hold real progress (e.g. a result page that opened in a
            # new tab). The bootstrap below only reuses it when navigating would not regress.
            carry_page = page
            page = None

    # ===============================================================
    # 5. DETERMINISTIC FAST PATH
    # ===============================================================

    deterministic_instruction = _normalize_deterministic_instruction(
        instruction
    )

    fast_plan = try_fast_path(
        deterministic_instruction
    )

    page = None

    if fast_plan is not None:

        # -----------------------------------------------------------
        # A one-action navigation can be a bootstrap.
        #
        # It opens the requested site but does NOT mean the complete
        # user task is finished.
        # -----------------------------------------------------------

        is_adaptive_bootstrap = (
            len(fast_plan.actions) == 1
            and fast_plan.actions[0].type == ActionType.NAVIGATE
            and fast_plan.actions[0].reason.strip()
            == "Open the requested website before adaptive browser reasoning."
        )

        # DEFECT FIX: an instruction that merely CONTAINS a URL ("Open http://x, go to
        # Pricing and open the Team plan") used to be treated as a complete
        # one-navigation workflow and was reported as SUCCESS right after the
        # page loaded. If the user's words say more than "open this URL", the
        # navigation is only a bootstrap and the adaptive loop must continue.
        if not is_adaptive_bootstrap and _url_navigation_is_only_bootstrap(instruction, fast_plan):
            is_adaptive_bootstrap = True
            _runtime(task).recorder.record(
                ev.ROUTER, route="adaptive",
                reason="instruction contains a URL plus further goals; navigation is only the bootstrap",
            )
        # ROUTING POLICY (defence in depth): a single navigation is a COMPLETE workflow only when the whole
        # instruction is just "open <destination>".  Any further clause (say/fill/search/click/...) means the
        # navigation is only the start and the rest must be planned and verified.
        if not is_adaptive_bootstrap:
            from agent.contract import fast_path_covers
            _cov = fast_path_covers(instruction, [a.type.value for a in fast_plan.actions], resolve_site_url)
            if not _cov["complete"]:
                is_adaptive_bootstrap = True
                _runtime(task).recorder.record(
                    ev.ROUTER, route="adaptive",
                    reason="fast path is only the bootstrap: " + _cov["reason"])
        if is_adaptive_bootstrap:
            logger.info(
                "Bootstrap navigation matched for task %s; "
                "continuing with adaptive AI: %s",
                task.id,
                instruction,
            )

            bootstrap_retry = RetryBudget(
                max_retries=settings.BROWSER_MAX_RETRIES,
            )
            _target = fast_plan.actions[0].url or ""
            _reuse_why = None
            if carry_page is not None:
                try:
                    _carry_ok = not carry_page.is_closed()
                except Exception:
                    _carry_ok = False
                if _carry_ok:
                    _reuse_why = _navigation_regression_reason(carry_page.url, _target, [])
            if _reuse_why and await browser_manager.adopt_page(carry_page):
                page = carry_page
                _runtime(task).recorder.record(
                    ev.ROUTER, route="adaptive",
                    reason=f"bootstrap navigation skipped: {_reuse_why}", url=page.url,
                )
                _runtime(task).recorder.record(ev.BROWSER_READY, url=page.url)
            else:
                _reuse_why = None
                _, page = await browser_manager.new_task_page()
                await browser_manager.focus_current_page()

            try:
                if not _reuse_why:
                    await _execute_recorded(task, page, fast_plan.actions, bootstrap_retry, "deterministic")
                page = await _sync_active_page(task, page)

            except ExecutionError as exc:
                logger.exception(
                    "Bootstrap navigation failed for task %s",
                    task.id,
                )

                await browser_manager.focus_current_page()

                task.mark_finished(
                    TaskStatus.FAILED,
                    error=exc.message,
                    final_url=page.url,
                    final_title=await _safe_title(page),
                    retries=bootstrap_retry.used,
                    message=(
                        "The requested website could not be opened. "
                        "The live Chrome tab remains open."
                    ),
                )

                task_repository.update(task)

                await ws_manager.send(
                    task.id,
                    _terminal_payload(task),
                )

                return

            # IMPORTANT:
            # Do NOT return success here.
            #
            # The browser is now on the starting page.
            # The adaptive loop must continue.
            fast_plan = None

        else:
            plan: ActionPlan = fast_plan

            logger.info(
                "Fast-path task %s matched deterministic workflow: %s",
                task.id,
                instruction,
            )

            # -------------------------------------------------------
            # Create Chrome only after deterministic decision.
            # -------------------------------------------------------

            _, page = await browser_manager.new_task_page()

            await browser_manager.focus_current_page()

            task.plan_source = plan.source
            task.actions_count = len(plan.actions)

            task_repository.update(task)

            retry_budget = RetryBudget(
                max_retries=settings.BROWSER_MAX_RETRIES,
            )

            try:
                result = await _execute_recorded(task, page, plan.actions, retry_budget, "deterministic")
                page = await _sync_active_page(task, page)

            except ExecutionError as exc:
                logger.exception(
                    "Fast-path execution failed for task %s",
                    task.id,
                )

                await browser_manager.focus_current_page()

                task.mark_finished(
                    TaskStatus.FAILED,
                    error=exc.message,
                    final_url=page.url,
                    final_title=await _safe_title(page),
                    retries=retry_budget.used,
                    message=(
                        "The predefined browser workflow failed. "
                        "The live Chrome tab remains open for manual recovery."
                    ),
                )

                task_repository.update(task)

                await ws_manager.send(
                    task.id,
                    _terminal_payload(task),
                )

                return

            except Exception as exc:
                logger.exception(
                    "Unexpected fast-path execution error for task %s",
                    task.id,
                )

                await browser_manager.focus_current_page()

                task.mark_finished(
                    TaskStatus.FAILED,
                    error=_friendly_error(exc),
                    final_url=page.url,
                    final_title=await _safe_title(page),
                    retries=retry_budget.used,
                    message=(
                        "The predefined browser workflow failed unexpectedly. "
                        "The live Chrome tab remains open."
                    ),
                )

                task_repository.update(task)

                await ws_manager.send(
                    task.id,
                    _terminal_payload(task),
                )

                return

            # -------------------------------------------------------
            # Complete deterministic workflow succeeded.
            # -------------------------------------------------------

            await browser_manager.focus_current_page()

            _final_verified(task, "the deterministic plan covered every requirement and all of its actions executed")
            task.mark_finished(
                TaskStatus.SUCCESS,
                final_url=result.get(
                    "final_url",
                    page.url,
                ),
                final_title=result.get(
                    "final_title",
                    await _safe_title(page),
                ),
                retries=retry_budget.used,
                message=(
                    result.get(
                        "message",
                        "Task completed.",
                    )
                    or "Task completed."
                )
                + " The live Chrome tab is ready for you.",
            )

            task_repository.update(task)

            await ws_manager.send(
                task.id,
                _terminal_payload(task),
            )

            return

    # ===============================================================
    # 6. NO COMPLETE FAST PATH
    #
    # We now use the AI architecture.
    # ===============================================================

    logger.info(
        "No deterministic workflow found for task %s. "
        "Using AI fallback: %s",
        task.id,
        instruction,
    )

    if page is None:
        _, page = await browser_manager.new_task_page()

    await browser_manager.focus_current_page()

    _runtime(task).recorder.record(ev.BROWSER_READY, url=page.url)
    ai_provider: AIProvider | None = None

    # ===============================================================
    # 7. INITIALIZE AI PROVIDER
    #
    # The provider itself decides:
    #
    #   Ollama first
    #        ↓
    #   genuine local failure
    #        ↓
    #   Gemini fallback
    #
    # No separate Gemini-first call is made here.
    # ===============================================================

    try:
        ai_provider = get_ai_provider()

    except Exception as exc:
        task.mark_finished(
            TaskStatus.FAILED,
            error=_friendly_error(exc),
            plan_source="fallback",
            final_url=page.url,
            final_title=await _safe_title(page),
            message=(
                "The Chrome tab was opened, but the AI provider "
                "could not be initialized."
            ),
        )

        task_repository.update(task)

        await browser_manager.focus_current_page()

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

        return

    # ===============================================================
    # 8. INITIAL AI PLAN
    # ===============================================================

    _rt0 = _runtime(task)
    _rt0.recorder.record(ev.ROUTER, route="initial", reason="deterministic fast path first, then AI bootstrap plan")
    _t_plan = time.perf_counter()
    try:
        # The bootstrap plan is bounded by the AI planning budget; it used to
        # be limited only by the outer task timeout.
        _plan_timeout = max(1.0, min(_rt0.budget.ai_remaining(), settings.GEMINI_CALL_TIMEOUT_SECONDS + 30.0))
        plan = await asyncio.wait_for(
            build_plan(
                instruction,
                ai_provider,
            ),
            timeout=_plan_timeout,
        )
        _plan_s = time.perf_counter() - _t_plan
        if plan.source != "fast_path":
            _rt0.budget.charge_ai(_plan_s)
            _rt0.recorder.record(ev.AI_PLAN, step=0, provider="auto", result="ok",
                                 duration_ms=int(_plan_s * 1000), actions=len(plan.actions),
                                 claimed_done=bool(plan.done), reason=f"bootstrap plan source={plan.source}")
        else:
            _rt0.recorder.record(ev.ROUTER, route="deterministic", reason="known task: fast path plan",
                                 actions=len(plan.actions))

    except Exception as exc:
        if isinstance(exc, asyncio.TimeoutError):
            exc = RuntimeError(f"Bootstrap AI planning exceeded its {_plan_timeout:.0f}s budget.")
        _rt0.budget.charge_ai(time.perf_counter() - _t_plan)
        _rt0.recorder.record(ev.AI_PLAN, step=0, provider="auto", result="failed",
                             duration_ms=int((time.perf_counter() - _t_plan) * 1000), error=str(exc)[:200])
        logger.exception(
            "Planning failed for task %s",
            task.id,
        )

        task.mark_finished(
            TaskStatus.FAILED,
            error=_friendly_error(exc),
            plan_source="fallback",
            final_url=page.url,
            final_title=await _safe_title(page),
            message=(
                "Chrome was opened, but the task could not be planned."
            ),
        )

        task_repository.update(task)

        await browser_manager.focus_current_page()

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

        return

    task.plan_source = plan.source
    task.actions_count = len(plan.actions)

    task_repository.update(task)

    # ===============================================================
    # 9. DETERMINISTIC FALLBACK PLAN
    # ===============================================================

    if plan.source == "fallback":
        retry_budget = RetryBudget(
            max_retries=settings.BROWSER_MAX_RETRIES,
        )

        try:
            result = await _execute_recorded(task, page, plan.actions, retry_budget, "fallback")
            page = await _sync_active_page(task, page)

        except ExecutionError as exc:
            logger.exception(
                "Fallback plan execution failed for task %s",
                task.id,
            )

            await browser_manager.focus_current_page()

            task.mark_finished(
                TaskStatus.FAILED,
                error=exc.message,
                final_url=page.url,
                final_title=await _safe_title(page),
                retries=retry_budget.used,
                message=(
                    "The deterministic fallback workflow failed. "
                    "The live Chrome tab remains open."
                ),
            )

            task_repository.update(task)

            await ws_manager.send(
                task.id,
                _terminal_payload(task),
            )

            return

        await browser_manager.focus_current_page()

        _final_verified(task, "the deterministic plan covered every requirement and all of its actions executed")
        task.mark_finished(
            TaskStatus.SUCCESS,
            final_url=result.get(
                "final_url",
                page.url,
            ),
            final_title=result.get(
                "final_title",
                await _safe_title(page),
            ),
            retries=retry_budget.used,
            message=result.get(
                "message",
                "Task completed.",
            ),
        )

        task_repository.update(task)

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

        return

    # ===============================================================
    # 10. INITIAL NAVIGATION
    #
    # For AI-generated plans, navigation is executed first.
    # Remaining interactions are handled by the adaptive loop.
    # ===============================================================

    navigation_actions = [
        action
        for action in plan.actions
        if action.type == ActionType.NAVIGATE
    ]

    retry_budget = RetryBudget(
        max_retries=settings.BROWSER_MAX_RETRIES,
    )

    if navigation_actions:
        try:
            navigation_result = await _execute_recorded(task, page, navigation_actions, retry_budget, "initial_navigation")
            page = await _sync_active_page(task, page)

        except ExecutionError as exc:
            logger.exception(
                "Navigation failed for task %s",
                task.id,
            )

            await browser_manager.focus_current_page()

            task.mark_finished(
                TaskStatus.FAILED,
                error=exc.message,
                final_url=page.url,
                final_title=await _safe_title(page),
                retries=retry_budget.used,
                message=(
                    "The live Chrome tab remains open "
                    "for manual recovery."
                ),
            )

            task_repository.update(task)

            await ws_manager.send(
                task.id,
                _terminal_payload(task),
            )

            return

    else:
        navigation_result = {
            "final_url": page.url,
            "final_title": await _safe_title(page),
            "message": "",
        }

    # ===============================================================
    # 11. ADAPTIVE INTERACTION
    # ===============================================================

    needs_interaction = _needs_live_interaction(
        instruction,
        plan,
    )

    if needs_interaction:
        if ai_provider is None:
            try:
                ai_provider = get_ai_provider()

            except Exception as exc:
                logger.exception(
                    "AI provider initialization failed during "
                    "live interaction for task %s",
                    task.id,
                )

                await browser_manager.focus_current_page()

                task.mark_finished(
                    TaskStatus.FAILED,
                    error=_friendly_error(exc),
                    final_url=page.url,
                    final_title=await _safe_title(page),
                    retries=retry_budget.used,
                    message=(
                        "Chrome remains open, but the next browser "
                        "interaction could not be planned because the "
                        "AI provider could not be initialized."
                    ),
                )

                task_repository.update(task)

                await ws_manager.send(
                    task.id,
                    _terminal_payload(task),
                )

                return

        result = await _run_live_interaction_loop(
            task=task,
            page=page,
            instruction=instruction,
            ai_provider=ai_provider,
            retry_budget=retry_budget,
            workflow_seed_actions=navigation_actions,
        )

        if result is None:
            return

        page = await _sync_active_page(task, page)
        final_result = result

    else:
        final_result = navigation_result

    # ===============================================================
    # 12. FINAL CHROME HANDOFF
    # ===============================================================

    await browser_manager.focus_current_page()

    # ===============================================================
    # 13. AI PATH SUCCESS
    # ===============================================================

    _final_verified(task, "the AI path's final-goal verifier agreed")
    task.mark_finished(
        TaskStatus.SUCCESS,
        final_url=final_result.get(
            "final_url",
            page.url,
        ),
        final_title=final_result.get(
            "final_title",
            await _safe_title(page),
        ),
        result=final_result.get("result"),
        retries=retry_budget.used,
        message=(
            final_result.get("message")
            or "Task completed."
        )
        + " The live Chrome tab is ready for you.",
    )

    task_repository.update(task)

    await ws_manager.send(
        task.id,
        _terminal_payload(task),
    )


# ===================================================================
# GENERIC TASK CONTRACT
# ===================================================================


def _build_task_contract(instruction: str) -> dict:
    return _with_extraction_spec(_build_task_contract_base(instruction))


def _build_task_contract_base(instruction: str) -> dict:
    """Derive generic requirements from the user's language.

    This is deliberately about task semantics (search, open-result, extract),
    never about particular websites.  It gives the verifier and local model a
    shared contract so a model cannot finish merely because the current page
    looks plausible.
    """
    text = " ".join(str(instruction or "").lower().split())
    queries = _extract_search_queries(instruction)
    asks_for_answer = bool(re.search(
        r"\b(?:tell|give|show|return|extract|read|report|what(?:'s| is))\b", text
    ))
    extraction_kind = None
    for kind in (
        "title", "heading", "name", "price", "author", "rating",
        "count", "number", "date", "sentence", "paragraph", "section",
        "text", "value", "content", "summary",
    ):
        if re.search(rf"\b{re.escape(kind)}(?:s)?\b", text):
            extraction_kind = kind
            break

    # Information requests are broader than a fixed vocabulary such as
    # title/price.  Phrases like "tell me the first sentence" or
    # "what does the section say" are still extraction tasks.
    answer_requested = asks_for_answer or bool(re.search(
        r"\b(?:first|last|next|exact|specific)\s+(?:sentence|paragraph|line|heading|section|text|value)\b|"
        r"\b(?:what|which|who|where|when|how many|how much)\b.*\b(?:page|section|article|result|text|answer|value)\b",
        text,
    ))

    section_requested = bool(re.search(
        r"\b(?:section|subsection|part)\b", text,
    ))
    section_topic = None
    section_match = re.search(
        r"\b(?:section|subsection|part)\b(?:\s+that\s+(?:explains?|describes?|covers?)|\s+(?:about|on|for|concerning))?\s+(?P<topic>.+?)(?=\s*,?\s+and\s+(?:tell|give|show|provide|return|report|read)|$)",
        text,
        flags=re.IGNORECASE,
    )
    if section_match:
        section_topic = section_match.group("topic").strip(" ,.;")
    first_sentence = bool(re.search(r"\bfirst\s+sentence\b", text))
    result_open = bool(re.search(
        r"\b(?:open|click|select|choose|visit)\b.*\b(?:first|top|most relevant|relevant|best)\b", text
    ))
    scroll_needed = bool(re.search(r"\b(?:scroll|below|further down|more results|lower|down the page)\b", text))
    return {
        "search_queries": queries,
        "requires_search": bool(queries),
        "requires_search_submission": bool(queries),
        "requires_result_open": result_open,
        "requires_extraction": answer_requested,
        "extraction_kind": extraction_kind,
        "answer_requested": answer_requested,
        "section_requested": section_requested,
        "section_topic": section_topic,
        "first_sentence": first_sentence,
        "scroll_intent": scroll_needed,
        "instruction": instruction[:1600],
    }


def _with_extraction_spec(contract: dict) -> dict:
    contract["extraction_spec"] = extraction_spec(contract.get("instruction", ""), contract)
    return contract


def _ordinal_word(position: int) -> str:
    return {0: "first", 1: "second", 2: "third", 3: "fourth", 4: "fifth", -1: "last"}.get(position, "first")


def _extraction_records(completed_actions: list[dict]) -> list[dict]:
    records: list[dict] = []
    for item in completed_actions:
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            continue
        for extracted in result.get("extracted") or []:
            if isinstance(extracted, dict) and str(extracted.get("text") or "").strip():
                records.append(dict(extracted))
    return records


def _extracted_results(completed_actions: list[dict]) -> list[str]:
    return [str(item.get("text") or "").strip() for item in _extraction_records(completed_actions) if str(item.get("text") or "").strip()]


def _semantic_extraction_requested(contract: dict, action: Action) -> bool:
    """Return True when extraction should be resolved semantically from page content.

    For section/sentence/heading/title requests, a model-supplied UI target is
    unsafe: it can point at a language selector, navigation link, or other
    visible text that happens to be easy to resolve. These requests should use
    the page-level semantic extractor instead.
    """
    if action.type != ActionType.EXTRACT or not contract.get("requires_extraction"):
        return False
    kind = contract.get("extraction_kind")
    return bool(
        contract.get("section_requested")
        or contract.get("first_sentence")
        or kind in {"sentence", "paragraph", "section", "summary", "content", "text", "title", "heading"}
    )


def _current_page_matches_requested_result(contract: dict, context: dict) -> bool:
    """Recognize a requested result without accepting merely related pages.

    Exact title/entity evidence is preferred. A page such as "Unsupervised
    machine learning" must not be treated as the exact "machine learning"
    result merely because it contains the query phrase. If the browser history
    proves that a result was deliberately opened, that evidence can supplement
    title matching for sites whose result titles contain useful qualifiers.
    """
    queries = contract.get("search_queries") or []
    title = _normalize_goal_evidence(context.get("current_title") or context.get("title") or "")
    url = _normalize_goal_evidence(context.get("current_url") or context.get("url") or "")
    if not title and not url:
        return False
    if any(marker in url for marker in ("/search", "?q=", "&q=", "?query=", "&query=", "search_query=")):
        return False
    instruction = _normalize_goal_evidence(contract.get("instruction") or "")
    if "article" in instruction and any(word in title for word in ("journal", "magazine", "research report", "category", "disambiguation")):
        return False

    def _title_core(value: str) -> str:
        return re.sub(
            r"\s*[-|–—]\s*(?:wikipedia|youtube|github|mdn web docs|stackoverflow)\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()

    core = _title_core(title_core(title, context))
    for query in queries:
        query_norm = _normalize_goal_evidence(query)
        if not query_norm:
            continue
        if core == query_norm or page_is_entity(context, query_norm):
            return True

        # Preserve flexibility for ordinary result titles only when action
        # history shows a genuine post-search result navigation and the page
        # is not an obvious collection/metadata page. This prevents false
        # acceptance of related entities while still supporting titles such as
        # "Python Tutorial - W3Schools".
        opened = False
        for item in context.get("completed_actions") or []:
            action = item.get("action") if isinstance(item, dict) else None
            if not isinstance(action, dict):
                continue
            typ = str(action.get("type") or "").lower()
            if typ in {"click", "navigate"} and _is_result_open_action(action):
                opened = True
        if opened and query_norm in core and not any(marker in core for marker in (
            "timeline", "list of", "history of", "journal", "magazine",
            "category", "disambiguation", "archive", "index", "unsupervised"
        )):
            return True
    return False


def _extraction_satisfies_contract(contract: dict, records: list[dict], context: dict) -> bool:
    if not contract.get("requires_extraction"):
        return True
    if not records:
        return False

    topic = _normalize_goal_evidence(contract.get("section_topic") or "")
    topic_tokens = [t for t in re.findall(r"[a-z0-9]{3,}", topic) if t not in {"the", "and", "for", "from", "with", "that", "this", "section", "about", "relationship", "between", "explains", "explaining", "first", "sentence"}]
    kind = contract.get("extraction_kind")

    for record in reversed(records):
        text = _normalize_goal_evidence(record.get("text") or "")
        result_kind = str(record.get("kind") or "").lower()
        heading = _normalize_goal_evidence(record.get("section_heading") or "")
        if not text:
            continue

        # Provenance, not plausibility: the value must come from the kind of DOM
        # element the user asked for (a "heading" request is never satisfied by
        # paragraph text).
        spec = contract.get("extraction_spec") or extraction_spec(contract.get("instruction", ""), contract)
        prov_ok, prov_why = validate_record(spec, record)
        if not prov_ok:
            logger.warning("Extraction rejected: %s (value=%r)", prov_why, text[:80])
            continue

        # A section request must be semantically tied to the requested topic.
        # Heading evidence is preferred because the first sentence itself may
        # naturally omit one of the topic terms.
        if topic_tokens:
            evidence = f"{heading} {text}"
            hits = sum(1 for token in topic_tokens if token in evidence)
            heading_hits = sum(1 for token in topic_tokens if token in heading)
            required_hits = max(2, int((len(topic_tokens) * 0.75) + 0.999)) if len(topic_tokens) >= 2 else 1
            if hits < required_hits:
                continue

        # Reject obvious browser chrome/noise. This is generic UI filtering,
        # not a website-specific answer.
        if len(text) < 8 and contract.get("first_sentence"):
            continue
        if re.fullmatch(r"(?:\d+\s+)?languages?", text, flags=re.IGNORECASE):
            continue
        if any(noise in text for noise in ("create account", "log in", "sign in", "donate", "view history")):
            continue
        return True
    return False


def _generic_extraction_recovery(contract: dict, context: dict) -> ActionPlan | None:
    """Produce a generic extraction action when the model falsely says done.

    It only uses live DOM evidence.  No site names or task recipes appear here.
    """
    if not contract.get("requires_extraction"):
        return None

    # Never extract semantic information from an arbitrary page while the
    # task still requires opening the requested search result. If the current
    # page is a wrong result (for example a related article), navigation
    # recovery must get us back to the search results first. Returning an
    # EXTRACT action here would mask the navigation problem and can create an
    # extraction/retry loop.
    if contract.get("requires_result_open"):
        try:
            if not _current_page_matches_requested_result(contract, context):
                return None
        except Exception:
            return None

    elements = context.get("elements") or []
    headings = context.get("headings") or []
    kind = contract.get("extraction_kind") or "value"

    # When an information task is stalled or a completion claim is rejected,
    # use the browser's generic semantic extractor before spending another AI
    # turn. This is not a website recipe: it operates on headings and content
    # in the current live DOM.
    if contract.get("section_requested") or kind in {"sentence", "paragraph", "section", "summary", "content", "text", "value"}:
        topic = str(contract.get("section_topic") or "the relevant section").strip()
        if contract.get("first_sentence") or kind == "sentence":
            reason = f"Extract the first sentence from the section about {topic}."
        elif contract.get("section_requested"):
            reason = f"Extract the relevant section about {topic}."
        else:
            reason = f"Extract the requested {kind} from the relevant live page content."
        return ActionPlan(
            actions=[Action(type=ActionType.EXTRACT, target=None, reason=reason)],
            source="fallback",
            done=False,
        )

    if kind in {"heading", "title", "name"}:
        return ActionPlan(
            actions=[Action(type=ActionType.EXTRACT, target=None, reason=f"Extract the {_ordinal_word((contract.get('extraction_spec') or {}).get('position', 0))} {kind} from the live page.")],
            source="fallback",
            done=False,
        )

    # Otherwise choose a semantically relevant visible content element.
    keywords = {kind}
    if kind == "price": keywords |= {"price", "amount", "cost"}
    if kind == "author": keywords |= {"author", "by"}
    if kind == "rating": keywords |= {"rating", "stars", "score"}
    candidates = []
    for el in elements:
        if not isinstance(el, dict) or el.get("disabled"): continue
        hay = " ".join(str(el.get(k) or "") for k in ("name","text","aria_label","label","role")).lower()
        score = sum(4 for word in keywords if word in hay)
        if el.get("region") in {"main","article","content"}: score += 5
        if el.get("text"): score += 1
        if score: candidates.append((score, el))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        el = candidates[0][1]
        return ActionPlan(actions=[Action(
            type=ActionType.EXTRACT,
            target=Target(role=el.get("role") or None, name=el.get("name") or None, css=None, exact=bool(el.get("name"))),
            reason=f"Extract the requested {kind} from the relevant live element."
        )], source="fallback", done=False)
    return None


# ===================================================================
# GOAL PARSING
# ===================================================================


def _goal_phrases(instruction: str) -> list[str]:
    """
    Extract ordered command goals from natural language.
    """
    text = re.sub(
        r"\s+",
        " ",
        instruction.strip(),
    )

    command = (
        r"(?:open|go to|visit|find|locate|look for|"
        r"search for|search|click)"
    )

    matches = list(
        re.finditer(
            rf"\b{command}\b",
            text,
            flags=re.IGNORECASE,
        )
    )

    goals: list[str] = []

    for i, match in enumerate(matches):
        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(text)
        )

        chunk = text[
            match.start():end
        ].strip(" ,.;")

        chunk = re.sub(
            r"\s+and\s*$",
            "",
            chunk,
            flags=re.IGNORECASE,
        ).strip(" ,.;")

        if chunk:
            goals.append(chunk)

    return goals


def _goal_tokens(text: str) -> list[str]:
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "on",
        "in",
        "at",
        "for",
        "with",
        "about",
        "section",
        "page",
        "website",
        "documentation",
        "open",
        "find",
        "click",
        "go",
        "visit",
        "tutorial",
    }

    return [
        word
        for word in re.findall(
            r"[a-z0-9]+",
            text.lower(),
        )
        if len(word) > 2 and word not in stop
    ]


def _normalize_goal_evidence(value: str) -> str:
    """Normalize visible/action evidence for strict goal comparisons."""
    return re.sub(
        r"\s+",
        " ",
        str(value or "").lower().replace("\u00a0", " "),
    ).strip()


def _extract_search_queries(instruction: str) -> list[str]:
    """
    Extract explicit search/look-for queries from the user's instruction.

    Search queries are treated as exact task requirements. This is important
    for workflow replay: a page containing the generic words "search" and
    "course" is not proof that the requested query was actually searched.
    """
    from agent.goals import clean_search_queries
    from agent.site_resolver import resolve_site_url

    clean = clean_search_queries(instruction, resolve_site_url)
    if clean:
        return clean

    text = re.sub(r"\s+", " ", instruction.strip())

    pattern = re.compile(
        r"\b(?:search(?:\s+for)?|look\s+for)\s+"
        r"(?P<query>.+?)"
        r"(?=\s*(?:,\s*)?(?:and|then)\s+"
        r"(?:open|click|go\s+to|visit|find|locate|search|look\s+for)\b|\s*,\s*(?:open|click|go\s+to|visit|find|locate)\b|$)",
        re.IGNORECASE,
    )

    queries: list[str] = []

    for match in pattern.finditer(text):
        query = match.group("query").strip(" ,.;")
        query = re.sub(
            r"^(?:the|a|an)\s+",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip(" ,.;")

        if query:
            queries.append(query)

    return queries


def _completed_action_dicts(completed_actions: list[dict]) -> list[dict]:
    """Return normalized action dictionaries from loop/replay history."""
    result: list[dict] = []

    for item in completed_actions:
        if not isinstance(item, dict):
            continue

        action = item.get("action")
        if isinstance(action, dict):
            result.append(action)
        elif "type" in item:
            result.append(item)

    return result


def _search_query_was_executed(
    query: str,
    completed_actions: list[dict],
) -> bool:
    """
    Verify that the requested search text was actually executed.

    Only real action payloads count here. In particular, do NOT count an
    action's `reason`, because an AI can describe the desired search while
    typing a different value into the page.
    """
    wanted = _normalize_goal_evidence(query)
    if not wanted:
        return False

    for action in _completed_action_dicts(completed_actions):
        action_type = str(action.get("type") or "").lower()

        if action_type == "type":
            typed = _normalize_goal_evidence(action.get("text") or "")
            if typed == wanted:
                return True

        elif action_type == "navigate":
            url = _normalize_goal_evidence(action.get("url") or "")
            decoded_url = _normalize_goal_evidence(
                unquote_plus(action.get("url") or "")
            )
            if wanted in url or wanted in decoded_url:
                return True

    return False


def _is_search_submit_action(action: dict) -> bool:
    """Return True only for an action that plausibly submits a search.

    A generic verifier must not treat an unrelated click after typing the
    query as proof that the search was submitted.
    """
    typ = str(action.get("type") or "").lower()
    if typ == "press":
        return str(action.get("key") or "").lower() in {"enter", "return"}
    if typ != "click":
        return False
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    # Judge by WHAT WAS CLICKED, not by planner prose in ``reason`` (a result click whose
    # reason says "...from the live search-results DOM" is not a search submission).
    hay = " ".join(str(target.get(k) or "") for k in ("name", "label", "placeholder", "css"))
    if str(target.get("role") or "").lower() in {"searchbox", "button"}:
        hay += " " + str(target.get("role") or "")
    if not hay.strip():
        hay = str(action.get("reason") or "")
    return bool(re.search(r"\b(?:search|submit|go)\b", hay, re.IGNORECASE))


def _is_result_open_action(action: dict) -> bool:
    """Return True for a post-search action that opens a result, not search submission."""
    typ = str(action.get("type") or "").lower()
    if typ not in {"click", "navigate"}:
        return False
    if typ == "click" and _is_search_submit_action(action):
        return False
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    # Judge by WHAT WAS CLICKED. The free-text reason is planner prose ("...from the live
    # search-results DOM") and must not disqualify a result click; it is consulted only when
    # the action has no target at all.
    hay = " ".join(str(target.get(k) or "") for k in ("name", "label", "placeholder", "role", "css"))
    if not hay.strip():
        hay = str(action.get("reason") or "")
    if re.search(r"\b(?:search|submit|filter|sort|menu|login|sign in)\b", hay, re.IGNORECASE):
        return False
    return True


def _page_has_search_query(
    query: str,
    context: dict,
    page=None,
) -> bool:
    """Check current live-page evidence for an exact requested query."""
    wanted = _normalize_goal_evidence(query)
    if not wanted:
        return False

    visible_text = _normalize_goal_evidence(
        " ".join(
            str(context.get(key) or "")
            for key in (
                "body_text",
                "title",
                "current_title",
                "url",
                "current_url",
            )
        )
    )

    if wanted in visible_text:
        return True

    if page is None:
        return False

    try:
        value = page.locator(
            "input, textarea, [contenteditable='true']"
        ).evaluate_all(
            """(els) => els.map((el) => ({
                value: el.value || '',
                text: el.innerText || el.textContent || ''
            }))"""
        )

        for item in value or []:
            evidence = _normalize_goal_evidence(
                f"{item.get('value', '')} {item.get('text', '')}"
            )
            if wanted in evidence:
                return True
    except Exception:
        pass

    return False


def _goal_was_executed(
    goal: str,
    completed_actions: list[dict],
) -> bool:
    """Strictly verify a prior command goal from actual action payloads."""
    goal_lower = goal.lower().strip()

    search_match = re.search(
        r"\b(?:search(?:\s+for)?|look\s+for)\s+(.+)$",
        goal,
        flags=re.IGNORECASE,
    )
    if search_match:
        return _search_query_was_executed(
            search_match.group(1).strip(" ,.;"),
            completed_actions,
        )

    tokens = _goal_tokens(goal)
    if not tokens:
        return True

    for action in _completed_action_dicts(completed_actions):
        action_type = str(action.get("type") or "").lower()
        if action_type not in {
            "navigate",
            "click",
            "type",
            "press",
            "wait_for",
            "download",
            "scroll",
        }:
            continue

        evidence_parts = [
            str(action.get("url") or ""),
            str(action.get("text") or ""),
            str(action.get("key") or ""),
        ]

        target = action.get("target")
        if isinstance(target, dict):
            evidence_parts.extend(
                str(target.get(key) or "")
                for key in (
                    "name",
                    "placeholder",
                    "label",
                    "css",
                )
            )

        evidence = _normalize_goal_evidence(
            " ".join(evidence_parts)
        )

        if all(token in evidence for token in tokens):
            return True

    return False


# ===================================================================
# GENERIC DOM RECOVERY
# ===================================================================


def _dom_recovery_plan(
    instruction: str,
    context: dict,
    completed_actions: list[dict],
) -> ActionPlan | None:
    """
    Choose an obvious visible DOM action without consuming an AI call.

    This is generic and works from the actual inspected DOM. It is not
    a collection of hard-coded website workflows.
    """

    elements = context.get("elements") or []

    if not isinstance(elements, list):
        return None

    completed_text = " ".join(
        json.dumps(
            x,
            ensure_ascii=False,
        )
        for x in completed_actions[-8:]
    ).lower()

    goals = _goal_phrases(instruction)

    if not goals:
        return None

    goal = goals[-1]
    tokens = _goal_tokens(goal)

    # Prefer the first goal that has not already been satisfied.
    for candidate in goals:
        candidate_tokens = _goal_tokens(candidate)

        if (
            candidate_tokens
            and not all(
                token in completed_text
                for token in candidate_tokens
            )
        ):
            goal = candidate
            tokens = candidate_tokens
            break

    if not tokens:
        return None

    scored: list[tuple[int, dict]] = []

    for element in elements:
        if (
            not isinstance(element, dict)
            or element.get("disabled")
        ):
            continue

        # Page chrome is never "the thing the user asked to open": skip header/nav/footer/
        # sidebar controls and links that only lead home or back to the current page.
        if str(element.get("region") or "") in {"header", "nav", "footer", "sidebar"}:
            continue
        _href = str(element.get("href") or "")
        if _href and (_href in {"/", "#"} or _href.startswith("javascript:")
                      or _urls_equivalent(_href, str(context.get("current_url") or ""))):
            continue

        hay = " ".join(
            str(element.get(key) or "")
            for key in (
                "name",
                "text",
                "aria_label",
                "placeholder",
                "label",
                "href",
            )
        ).lower()

        if not hay.strip():
            continue

        score = 0

        for token in tokens:
            if token in hay:
                score += (
                    3
                    if len(token) >= 6
                    else 2
                )

        name = str(
            element.get("name") or ""
        ).strip()

        role = str(
            element.get("role") or ""
        ).strip()

        tag = str(
            element.get("tag") or ""
        ).strip().lower()

        if (
            name
            and tokens
            and all(
                token in name.lower()
                for token in tokens
            )
        ):
            score += 5

        if role in {
            "link",
            "button",
            "tab",
            "menuitem",
        }:
            score += 1

        if tag == "a":
            score += 1

        if score >= max(
            3,
            len(tokens) * 2,
        ):
            scored.append(
                (
                    score,
                    element,
                )
            )

    if not scored:
        return None

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    element = scored[0][1]

    target = Target(
        role=element.get("role") or None,
        name=element.get("name") or None,
        placeholder=element.get("placeholder") or None,
        label=element.get("label") or None,
        css=None,
        xpath=None,
        exact=bool(element.get("name")),
    )

    # If inspector supplied neither role nor name, use href.
    if not target.role and not target.name:
        href = element.get("href")

        if href:
            safe_href = str(href).replace(
                '"',
                '\\"',
            )

            target.css = (
                f'a[href="{safe_href}"]'
            )

        else:
            return None

    return ActionPlan(
        actions=[
            Action(
                type=ActionType.CLICK,
                target=target,
                reason=(
                    "Open the visible element relevant to: "
                    f"{goal}."
                ),
            )
        ],
        source="fallback",
        done=False,
    )


# ===================================================================
# FINAL GOAL VERIFICATION
# ===================================================================


async def _page_proves_final_goal(
    instruction: str,
    context: dict,
    completed_actions: list[dict],
    page=None,
) -> bool:
    """Verify the semantic task contract using browser evidence.

    The verifier is deliberately independent of website names.  It checks
    actions/results rather than trusting the model's ``done`` flag.
    """
    contract = context.get("task_contract") or _build_task_contract(instruction)
    actions = _completed_action_dicts(completed_actions)
    if not actions and not context.get("current_url"):
        return False

    # Every explicit value of the user (phone number, e-mail, quoted text) must have been typed by a real action.
    from agent.contract import unmet_typed_values
    _unmet = unmet_typed_values(instruction, actions)
    if _unmet:
        logger.warning("Goal not proven: explicit %s value(s) were never entered", _unmet)
        return False

    # Explicit searches must really have been executed.
    for query in contract.get("search_queries", []):
        if not _search_query_was_executed(query, completed_actions):
            logger.warning("Goal not proven: search for %r was not executed", query)
            return False

    # A search request is not complete merely because text was typed.  It
    # must be submitted by Enter, a search-button click, or a URL navigation.
    if contract.get("requires_search_submission"):
        submitted = False
        search_seen = False
        for action in actions:
            typ = str(action.get("type") or "").lower()
            if typ == "type" and any(_normalize_goal_evidence(q) == _normalize_goal_evidence(action.get("text") or "") for q in contract.get("search_queries", [])):
                search_seen = True
            elif search_seen and _is_search_submit_action(action):
                submitted = True
                break
        if not submitted:
            logger.warning("Goal not proven: the search was typed but never submitted")
            return False

    # If the user explicitly asked to open the first/top/relevant result, a
    # click or post-search navigation must have occurred after the search.
    if contract.get("requires_result_open"):
        search_seen = False
        opened = False
        for action in actions:
            typ = str(action.get("type") or "").lower()
            if typ == "type" and any(_normalize_goal_evidence(q) == _normalize_goal_evidence(action.get("text") or "") for q in contract.get("search_queries", [])):
                search_seen = True
            elif search_seen and _is_result_open_action(action):
                opened = True
                break
        if not opened and not _current_page_matches_requested_result(contract, context):
            logger.warning("Goal not proven: no result-opening action after the search and the page is not the requested result")
            return False
        # "Opened a result" needs EFFECT evidence, not just a click: the browser must have
        # left the page where the search began and must not be sitting on a search page.
        # (A click on a header logo that returns to the start page is not a result.)
        cur_url = str(context.get("current_url") or context.get("url") or "")
        if is_search_url(cur_url) and not _current_page_matches_requested_result(contract, context):
            logger.warning("Goal not proven: still on a search page (%s)", cur_url)
            return False
        start_urls = [
            str(i.get("url_before") or "")
            for i in (completed_actions or [])
            if isinstance(i, dict) and isinstance(i.get("action"), dict)
            and str(i["action"].get("type") or "").lower() == "type"
            and any(_normalize_goal_evidence(q) == _normalize_goal_evidence(i["action"].get("text") or "")
                    for q in contract.get("search_queries", []))
        ]
        if start_urls and _urls_equivalent(cur_url, start_urls[0]):
            logger.warning("Result-open not proven: browser is back on the page where the search started (%s)", cur_url)
            return False

    # Requested information must be a semantically relevant extraction, not
    # merely any non-empty visible string. This prevents UI labels such as a
    # language selector from being accepted as the requested answer.
    extracted_records = _extraction_records(completed_actions)
    if contract.get("requires_extraction") and not _extraction_satisfies_contract(contract, extracted_records, context):
        logger.warning(
            "Extraction evidence does not satisfy task contract: kind=%s first_sentence=%s section_topic=%r records=%s",
            contract.get("extraction_kind"),
            contract.get("first_sentence"),
            contract.get("section_topic"),
            extracted_records[-3:],
        )
        return False
    if contract.get("requires_extraction"):
        # Re-verify the extracted VALUE against the live DOM of the CURRENT page.
        dom_ok, dom_why = False, "no extraction record"
        for record in reversed(extracted_records):
            dom_ok, dom_why = await verify_in_dom(page, record)
            if dom_ok:
                break
        if not dom_ok:
            logger.warning("Extracted value is not present in the live DOM provenance: %s", dom_why)
            return False

    typing_req = parse_typing_request(instruction)
    if typing_req is not None and not typing_req.wants_send:
        # "type X" is only complete when the live page's editable control really contains X.
        if not await verify_typed_in_dom(page, typing_req.text):
            return False

    url = str(context.get("current_url") or context.get("url") or "")
    if not url or url.startswith("about:blank"):
        return False

    # For ordinary navigation/search tasks, require at least one real action
    # and some live page evidence.  Do not require brittle keyword equality
    # against the final page because arbitrary sites structure results differently.
    if not contract.get("requires_search") and not contract.get("requires_extraction"):
        if not actions:
            return False
        body = str(context.get("body_text") or "").strip()
        title = str(context.get("current_title") or context.get("title") or "").strip()
        if not body and not title:
            return False
        # Generic goal-evidence check (no site knowledge): the words of the
        # user's own goal must be substantially present in what the live page
        # shows (URL, title, headings, visible text). "Any action + any page"
        # is NOT enough to call a navigation-style goal complete.
        clauses = _unsatisfied_goal_clauses(instruction, completed_actions, context)
        context["unsatisfied_goal_clauses"] = clauses
        if clauses:
            logger.warning("Goal steps not yet evidenced by any completed action or page: %s", clauses)
            return False
        evidence = _goal_evidence(instruction, context)
        if evidence["checked"] and evidence["coverage"] < 0.5:
            logger.warning(
                "Goal evidence insufficient: coverage=%.2f missing=%s url=%s",
                evidence["coverage"], evidence["missing"], url,
            )
            return False

    return True


def _unsatisfied_goal_clauses(instruction: str, completed_actions: list[dict], context: dict) -> list[str]:
    """Ordered sub-goals of a multi-step navigation instruction that have no evidence.

    Generic (no site knowledge): split the user's own sentence into clauses,
    keep the ones that ask to open/click/go somewhere, and require each to be
    evidenced by a completed click/navigate whose target, URL or resulting
    title covers its distinctive words - or by the live page URL/title itself.
    Only applied when there are >= 2 such clauses; a single-clause goal keeps
    the simpler goal-evidence check. Wording mismatches can make this
    conservative: the task then fails honestly instead of being reported as done.
    """
    from agent.progress import goal_tokens

    generic = {"navigate", "browse", "load", "launch", "website", "address"}
    clauses: list[tuple[str, list[str]]] = []
    for part, rest in navigation_clauses(instruction):
        toks = [t for t in goal_tokens(rest) if t not in generic]
        if toks:
            clauses.append((part, toks))
    if len(clauses) < 2:
        return []

    haystacks: list[str] = []
    for item in completed_actions or []:
        if not isinstance(item, dict):
            continue
        a = item.get("action") if isinstance(item.get("action"), dict) else {}
        if str(a.get("type") or "").lower() not in {"click", "navigate", "press", "hover", "select_option"}:
            continue
        t = a.get("target") if isinstance(a.get("target"), dict) else {}
        haystacks.append(" ".join(str(x or "") for x in (
            t.get("name"), t.get("label"), t.get("placeholder"), t.get("css"), a.get("url"),
            item.get("url_after"), item.get("title_after"), a.get("reason"))).lower())
    page_hay = " ".join([str(context.get("current_url") or ""),
                         str(context.get("current_title") or context.get("title") or "")]).lower()

    missing: list[str] = []
    for clause, toks in clauses:
        def covered(hay: str) -> bool:
            return sum(1 for t in toks if t in hay) / len(toks) >= 0.5
        if not (any(covered(h) for h in haystacks) or covered(page_hay)):
            missing.append(clause)
    return missing


def _goal_evidence(instruction: str, context: dict) -> dict:
    """How much of the user's goal vocabulary is visible on the live page.

    Returns ``{"checked": bool, "coverage": float, "missing": [...]}``.
    ``checked`` is False when the instruction has no distinctive words, in
    which case completion cannot be independently proven beyond "an action
    ran on a real page" - that limitation is reported, not hidden.
    """
    from agent.progress import goal_tokens

    tokens = goal_tokens(instruction)
    if not tokens:
        return {"checked": False, "coverage": 1.0, "missing": []}
    heads = " ".join(str(h) for h in (context.get("headings") or [])[:20])
    hay = " ".join([
        str(context.get("current_url") or ""),
        str(context.get("current_title") or context.get("title") or ""),
        heads,
        str(context.get("body_text") or "")[:4000],
    ]).lower()
    missing = [t for t in tokens if t not in hay]
    return {"checked": True, "coverage": (len(tokens) - len(missing)) / len(tokens), "missing": missing}


# ===================================================================
# ADAPTIVE LIVE BROWSER LOOP
# ===================================================================


async def _run_live_interaction_loop(
    task: TaskRecord,
    page,
    instruction: str,
    ai_provider: AIProvider,
    retry_budget: RetryBudget,
    workflow_seed_actions: list[Action] | None = None,
) -> dict | None:
    """
    General adaptive browser loop.

    Every iteration:

        observe live DOM
              ↓
        verify completion
              ↓
        short local/remote plan
              ↓
        execute a small verified chunk
              ↓
        re-observe after state-changing actions
              ↓
        recover/replan when needed

    The browser remains authoritative: plans are bounded, state-changing
    actions force re-observation, and repeated actions are blocked.
    """

    completed_actions: list[dict] = []
    failures: list[dict] = []

    # Bounded recovery state. A browser/AI failure must change strategy or
    # terminate; it must never amplify into dozens of identical retries.
    browser_recoveries = 0
    planning_failures = 0
    max_browser_recoveries = 2
    max_planning_failures = 3

    last_result = {
        "final_url": page.url,
        "final_title": await _safe_title(page),
        "message": "",
    }
    forced_recovery_plan: ActionPlan | None = None
    force_ai_replan = False
    blocked_actions: list[dict] = []
    last_state_key: str | None = None
    stalled_steps = 0
    repeated_done_claims = 0

    # ---- adaptive-loop state (deadline, providers, progress) -------------
    rt = _runtime(task)
    try:
        providers = get_adaptive_providers()
    except Exception as exc:
        logger.warning("Adaptive providers unavailable: %s", exc)
        providers = {}
    rt.recorder.record(
        ev.PROVIDER_STATUS, state="configured",
        reason=(
            f"selection={rt.selection}; order=" + " -> ".join(
                f"{n}({getattr(providers.get(n), 'model', None) or 'not configured'})" for n in rt.order)
            + f"; ollama emergency max {settings.OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK} requests/task"
        ),
    )
    tracker = ProgressTracker(instruction)
    ladder = LadderState()
    pending_eval: dict | None = None
    last_verdict: dict | None = None
    wrong_state_backs = 0
    planned_by = "deterministic"
    last_plan_digest: str | None = None

    for step in range(
        _MAX_LIVE_ACTION_STEPS
    ):
        logger.info(
            "Adaptive interaction step %d/%d for task %s",
            step + 1,
            _MAX_LIVE_ACTION_STEPS,
            task.id,
        )

        await asyncio.sleep(0.25)

        if rt.budget.expired():
            await browser_manager.focus_current_page()
            _finish_deadline(task, rt, page.url, await _safe_title(page),
                             f"before step {step + 1}", retry_budget.used)
            await ws_manager.send(task.id, _terminal_payload(task))
            return None

        # The active Page may have changed (new tab/popup adopted by the
        # executor). Re-read it so planning/observation/verification all use it.
        page = await _sync_active_page(task, page, step + 1)

        # -----------------------------------------------------------
        # BROWSER LIFECYCLE GUARD
        # -----------------------------------------------------------
        # Playwright can lose the target page when Chrome closes, a tab is
        # discarded, or the CDP context is replaced. Never keep planning
        # against a dead Page object. Start a fresh controlled tab and
        # re-bootstrap the task instead.
        page_dead = page is None
        if not page_dead:
            try:
                page_dead = page.is_closed()
            except Exception:
                page_dead = True
        if page_dead or not browser_manager.is_ready:
            if browser_recoveries >= max_browser_recoveries:
                task.mark_finished(
                    TaskStatus.FAILED,
                    error="The controlled Chrome session could not be recovered safely.",
                    final_url=getattr(page, "url", "") if page is not None else "",
                    final_title="",
                    retries=retry_budget.used,
                    message="Browser recovery limit reached; no further browser actions were attempted.",
                )
                task_repository.update(task)
                await ws_manager.send(task.id, _terminal_payload(task))
                return None

            browser_recoveries += 1
            logger.warning(
                "Controlled browser page is unavailable; starting recovery %d/%d for task %s.",
                browser_recoveries, max_browser_recoveries, task.id,
            )
            try:
                _, page = await browser_manager.new_task_page(fresh_tab=True)
            except Exception as exc:
                logger.exception("Browser recovery failed for task %s", task.id)
                if browser_recoveries >= max_browser_recoveries:
                    task.mark_finished(
                        TaskStatus.FAILED,
                        error=_friendly_error(exc),
                        final_url="",
                        final_title="",
                        retries=retry_budget.used,
                        message="The controlled Chrome session could not be restarted.",
                    )
                    task_repository.update(task)
                    await ws_manager.send(task.id, _terminal_payload(task))
                    return None
                continue

            # Previous action history belongs to the dead page. Clear it so
            # verification does not falsely believe the new tab completed old
            # work, then let the normal initial bootstrap run again.
            completed_actions.clear()
            failures.append({"step": step + 1, "error": "Browser session recovered; task state reset and re-bootstrap requested."})
            force_ai_replan = False
            forced_recovery_plan = None

            bootstrap = try_fast_path(instruction)
            if bootstrap is not None and bootstrap.actions:
                try:
                    result = await execute_plan(page, bootstrap.actions[:1], retry_budget)
                    action = bootstrap.actions[0]
                    action_data = action.model_dump(mode="json")
                    fp = json.dumps(action_data, sort_keys=True, ensure_ascii=False)
                    completed_actions.append({
                        "step": step + 1,
                        "action": action_data,
                        "action_fingerprint": fp,
                        "url_before": "about:blank",
                        "url_after": page.url,
                        "title_after": await _safe_title(page),
                        "result": result,
                    })
                    task.actions_count += 1
                    continue
                except Exception as exc:
                    failures.append({"step": step + 1, "action": bootstrap.actions[0].model_dump(mode="json"), "error": _friendly_error(exc)})
            continue

        # -----------------------------------------------------------
        # OBSERVE
        # -----------------------------------------------------------

        context = await inspector.snapshot(
            page
        )

        context["current_url"] = page.url
        context["current_title"] = (
            await _safe_title(page)
        )

        context["completed_actions"] = (
            completed_actions[-12:]
        )

        context["failed_actions"] = (
            failures[-6:]
        )

        context["interaction_step"] = (
            step + 1
        )

        context["remaining_instruction"] = instruction
        context["task_contract"] = _build_task_contract(instruction)
        context["previous_action_result"] = last_result
        context["verification_feedback"] = failures[-1] if failures else None
        context["planner_mode"] = "repair" if force_ai_replan or repeated_done_claims > 0 else "normal"
        context["blocked_actions"] = blocked_actions[-6:]

        contract = context["task_contract"]
        extracted_now = _extracted_results(completed_actions)
        context["progress"] = {
            "search_queries_executed": [
                q for q in contract.get("search_queries", [])
                if _search_query_was_executed(q, completed_actions)
            ],
            "search_submitted": any(
                _is_search_submit_action(a.get("action", {}))
                for a in completed_actions
                if isinstance(a, dict) and isinstance(a.get("action"), dict)
            ),
            "result_opened": bool(
                contract.get("requires_result_open")
                and any(
                    _is_result_open_action(a.get("action", {}))
                    for a in completed_actions
                    if isinstance(a, dict) and isinstance(a.get("action"), dict)
                )
            ),
            "extracted_values": extracted_now[-3:],
            "remaining_requirements": {
                "search": [q for q in contract.get("search_queries", []) if not _search_query_was_executed(q, completed_actions)],
                "search_submission": bool(contract.get("requires_search_submission")) and not any(
                    _is_search_submit_action(a.get("action", {}))
                    for a in completed_actions
                    if isinstance(a, dict) and isinstance(a.get("action"), dict)
                ),
                "result_open": bool(contract.get("requires_result_open")) and not any(
                    _is_result_open_action(a.get("action", {}))
                    for a in completed_actions
                    if isinstance(a, dict) and isinstance(a.get("action"), dict)
                ),
                "information_extraction": bool(contract.get("requires_extraction")) and not extracted_now,
            },
        }

        # -----------------------------------------------------------
        # OBSERVE EVENT + PROGRESS EVALUATION OF THE PREVIOUS ACTION
        #
        # Progress is measured by comparing the page BEFORE the action with
        # the page NOW - never by "Playwright did not raise". Counters that
        # signal trouble are only reset by a real PROGRESS verdict.
        # -----------------------------------------------------------
        obs_now = observe(context, page.url, *_page_identity(page))
        rt.recorder.record(
            ev.OBSERVE, step=step + 1, url=page.url,
            title=context.get("current_title"),
            elements=len(context.get("elements") or []),
            headings=len(context.get("headings") or []),
        )
        if pending_eval is not None:
            verdict = tracker.classify(
                pending_eval["before"], obs_now, pending_eval["action"],
                pending_eval["ok"], pending_eval.get("error"),
            )
            last_verdict = verdict.to_dict()
            rt.recorder.record(
                ev.PROGRESS, step=step + 1, classification=verdict.kind.value,
                reason="; ".join(verdict.reasons), streak=tracker.no_progress_streak,
                url=page.url,
            )
            if verdict.is_progress:
                planning_failures = 0
            else:
                if verdict.kind != Progress.ACTION_FAILED:   # failed actions were already logged
                    failures.append({
                        "step": step + 1, "action": pending_eval["action"],
                        "error": f"{verdict.kind.value}: {'; '.join(verdict.reasons)}",
                        "url": page.url,
                    })
                if pending_eval["action"]:
                    blocked_actions.append({
                        "action": pending_eval["action"],
                        "url": pending_eval["before"].url,
                        "reason": verdict.kind.value,
                    })
                force_ai_replan = True     # do not let the deterministic path repeat it
            wrong_state = verdict.kind == Progress.WRONG_STATE
            pending_eval = None
            if wrong_state and wrong_state_backs < 2 and rt.budget.can_start_recovery(2.0):
                wrong_state_backs += 1
                t_rec = time.perf_counter()
                moved = await _bounded_go_back(page, min(settings.BROWSER_NAV_TIMEOUT_MS, 5000))
                rt.budget.charge_recovery(time.perf_counter() - t_rec)
                rt.recorder.record(
                    ev.RECOVERY, step=step + 1, level=3, strategy="go_back",
                    result="ok" if moved else "no_effect",
                    reason="last action led to a goal-irrelevant page", url=page.url,
                )
                continue

        context["planner_mode"] = "repair" if (force_ai_replan or rt.false_done_total > 0) else "normal"
        context["failed_actions"] = failures[-6:]
        context["blocked_actions"] = blocked_actions[-6:]
        context["verification_feedback"] = failures[-1] if failures else None
        if last_verdict:
            context["progress_feedback"] = last_verdict

        if tracker.no_progress_streak >= settings.NO_PROGRESS_FAIL_AFTER:
            rt.recorder.record(
                ev.RECOVERY, step=step + 1, level=6, strategy="honest_failure",
                reason=f"no measurable progress for {tracker.no_progress_streak} consecutive actions",
            )
            task.mark_finished(
                TaskStatus.FAILED,
                error=(
                    f"No measurable progress for {tracker.no_progress_streak} consecutive actions "
                    f"(verdicts: {tracker.counts}). Last: {(last_verdict or {}).get('classification')} - "
                    f"{'; '.join((last_verdict or {}).get('reasons', []))[:200]}"
                ),
                final_url=page.url,
                final_title=await _safe_title(page),
                retries=retry_budget.used,
                message="The live Chrome tab remains open for manual recovery.",
            )
            task_repository.update(task)
            await ws_manager.send(task.id, _terminal_payload(task))
            return None
        if (
            tracker.no_progress_streak >= settings.NO_PROGRESS_RECOVER_AFTER
            and tracker.no_progress_streak % settings.NO_PROGRESS_RECOVER_AFTER == 0
        ):
            strategy = "semantic_extraction"
            repair = _generic_extraction_recovery(context.get("task_contract") or {}, context)
            if repair is None:
                strategy = "generic_dom_recovery"
                repair = _dom_recovery_plan(instruction, context, completed_actions)
            rt.recorder.record(
                ev.RECOVERY, step=step + 1, level=3 if repair else 5,
                strategy=strategy if repair else "replan_with_failure_feedback",
                reason=f"{tracker.no_progress_streak} consecutive non-progress actions",
            )
            if repair is not None:
                forced_recovery_plan = repair
            force_ai_replan = True

        # -----------------------------------------------------------
        # FIRST: VERIFY WHETHER TASK IS ACTUALLY COMPLETE.
        # -----------------------------------------------------------

        planned_by = "deterministic"
        last_plan_digest = None
        if forced_recovery_plan is not None:
            interaction_plan = forced_recovery_plan
            forced_recovery_plan = None
            planned_by = "recovery"
        elif await _page_proves_final_goal(
            instruction,
            context,
            completed_actions,
            page,
        ):
            planned_by = "verifier"
            interaction_plan = ActionPlan(
                actions=[],
                source="fallback",
                done=True,
            )

        else:
            # -------------------------------------------------------
            # AI PLANNING IS PRIMARY FOR EVERY UNRESOLVED LIVE STEP.
            #
            # Do NOT let the generic DOM heuristic steal control before
            # Gemini/Qwen sees the current page. The DOM heuristic is only
            # an emergency recovery path when the AI providers genuinely
            # fail. This keeps the architecture general-purpose instead
            # of turning the inspector into a hidden site-specific router.
            # -------------------------------------------------------
            try:
                interaction_plan = None
                if not force_ai_replan:
                    route, det_plan = route_live_step(instruction, context)
                    if det_plan is None:
                        # Deterministic DOM workflow #2: "type <text>" into the page's message box.
                        tstep = await next_typing_step(
                            instruction, context, completed_actions,
                            snapshot=lambda: _fresh_snapshot(page),
                            wait_budget_s=min(6.0, max(0.0, rt.budget.remaining() - 5.0)),
                        )
                        if tstep.kind == "act":
                            route = "deterministic_dom"
                            det_plan = ActionPlan(actions=[Action.model_validate(tstep.action)], source="fast_path", done=False)
                            rt.recorder.record(ev.ROUTER, step=step + 1, route=route, url=page.url,
                                               reason=f"typing workflow: {tstep.action.get('reason')}",
                                               waited_s=tstep.waited_s)
                        elif tstep.kind == "blocked":
                            raise TaskBlocked(tstep.reason)
                    if det_plan is not None:
                        if route != "deterministic_dom":
                            rt.recorder.record(ev.ROUTER, step=step + 1, route=route, url=page.url)
                        interaction_plan = det_plan
                        planned_by = "deterministic"
                    else:
                        rt.recorder.record(ev.ROUTER, step=step + 1, route="adaptive", url=page.url)
                else:
                    rt.recorder.record(
                        ev.ROUTER, step=step + 1, route="adaptive", url=page.url,
                        reason="fresh AI reasoning required (rejected done / no progress / blocked action)",
                    )
                if interaction_plan is None:
                    outcome = await plan_next_action(
                        instruction=instruction, context=context, providers=providers,
                        health=rt.health, budget=rt.budget, recorder=rt.recorder,
                        step=step + 1, planner_mode=context.get("planner_mode", "normal"),
                        gemini_timeout_s=settings.GEMINI_CALL_TIMEOUT_SECONDS,
                        ollama_request_timeout_s=settings.OLLAMA_EMERGENCY_TIMEOUT_SECONDS,
                        order=rt.order, hosted_timeout_s=settings.HOSTED_PROVIDER_TIMEOUT_SECONDS,
                    )
                    interaction_plan = outcome.plan
                    planned_by = outcome.provider
                    last_plan_digest = outcome.digest
                force_ai_replan = False

            except TaskBlocked as blocked:
                rt.recorder.record(ev.ROUTER, step=step + 1, route="blocked", reason=str(blocked), url=page.url)
                await browser_manager.focus_current_page()
                task.mark_finished(
                    TaskStatus.FAILED,
                    error=f"Blocked: {blocked}",
                    final_url=page.url,
                    final_title=await _safe_title(page),
                    retries=retry_budget.used,
                    message="The live Chrome tab remains open for you to continue manually.",
                )
                task_repository.update(task)
                await ws_manager.send(task.id, _terminal_payload(task))
                return None
            except Exception as exc:
                planning_failures += 1
                contract = context.get("task_contract") or _build_task_contract(instruction)

                # First use deterministic DOM mechanics. These are based only
                # on the live page and task contract, so they remain generic.
                recovered = None
                if contract.get("requires_extraction"):
                    recovered = _generic_extraction_recovery(contract, context)

                if recovered is None:
                    recovered = try_live_path(instruction, context)

                # If the wrong result was opened and AI planning is currently
                # unavailable, use Playwright history directly. Do NOT encode
                # browser-history navigation as the invalid key combination
                # `Alt+Left`; page.go_back() is the browser-native abstraction.
                if recovered is None and contract.get("search_queries"):
                    current_url = str(context.get("current_url") or page.url or "").lower()
                    current_matches = _current_page_matches_requested_result(contract, context)
                    if current_matches and contract.get("requires_extraction"):
                        # The requested detail page is already open. Do not
                        # navigate backwards just because the AI is offline.
                        recovered = try_live_path(instruction, context)
                    elif not any(marker in current_url for marker in ("/search", "?q=", "&q=", "?query=", "&query=", "search_query=")):
                        before_url = page.url
                        try:
                            # Use a short commit-level wait. Wikipedia and other
                            # pages can complete the navigation while never
                            # satisfying a full DOMContentLoaded wait, which used
                            # to turn a successful back navigation into a false
                            # recovery failure.
                            try:
                                await page.go_back(wait_until="commit", timeout=min(settings.BROWSER_NAV_TIMEOUT_MS, 5000))
                            except Exception as back_exc:
                                if page.url == before_url:
                                    raise
                                logger.info("Back navigation changed the URL despite wait failure: %s", back_exc)
                            await page.wait_for_timeout(300)
                            back_context = await inspector.snapshot(page)
                            back_context["current_url"] = page.url
                            back_context["current_title"] = await _safe_title(page)
                            planning_history = []
                            search_seen = False
                            for history_item in completed_actions:
                                action_info = history_item.get("action") if isinstance(history_item, dict) else None
                                if not isinstance(action_info, dict):
                                    continue
                                typ = str(action_info.get("type") or "").lower()
                                if typ == "type" and any(
                                    _normalize_goal_evidence(action_info.get("text") or "") == _normalize_goal_evidence(q)
                                    for q in contract.get("search_queries") or []
                                ):
                                    search_seen = True
                                if search_seen and typ in {"click", "navigate"}:
                                    break
                                planning_history.append(history_item)
                            back_context["completed_actions"] = planning_history[-12:]
                            back_context["failed_actions"] = failures[-6:]
                            back_context["remaining_instruction"] = instruction
                            recovered = try_live_path(instruction, back_context)
                            if recovered is not None:
                                logger.info("Recovered to previous search state using page.go_back(); selecting from fresh DOM.")
                        except Exception as back_exc:
                            logger.warning("Generic page.go_back recovery failed: %s", back_exc)

                if recovered is None and not contract.get("requires_extraction"):
                    recovered = _dom_recovery_plan(
                        instruction,
                        context,
                        completed_actions,
                    )

                if planning_failures >= max_planning_failures and recovered is None:
                    logger.error("Adaptive planning failed too many times for task %s; stopping instead of retry amplification.", task.id)
                    task.mark_finished(
                        TaskStatus.FAILED,
                        error=_friendly_error(exc),
                        final_url=page.url,
                        final_title=await _safe_title(page),
                        retries=retry_budget.used,
                        message="The agent exhausted its bounded planning/recovery budget without making safe progress.",
                    )
                    task_repository.update(task)
                    await ws_manager.send(task.id, _terminal_payload(task))
                    return None

                if recovered is not None:
                    rt.recorder.record(
                        ev.RECOVERY, step=step + 1, level=3, strategy="generic_dom_recovery",
                        reason=str(exc)[:200], url=page.url,
                    )
                    logger.warning(
                        "Recovered adaptive step from live DOM "
                        "after all AI planners failed: %s",
                        exc,
                    )

                    interaction_plan = recovered
                    planned_by = "recovery"

                else:
                    rt.recorder.record(
                        ev.RECOVERY, step=step + 1, level=6, strategy="honest_failure",
                        reason=f"no provider available and no generic DOM recovery: {str(exc)[:220]}",
                        url=page.url,
                    )
                    logger.exception(
                        "Adaptive planning failed for task %s",
                        task.id,
                    )

                    await browser_manager.focus_current_page()

                    task.mark_finished(
                        TaskStatus.FAILED,
                        error=_friendly_error(exc),
                        final_url=page.url,
                        final_title=await _safe_title(page),
                        retries=retry_budget.used,
                        message=(
                            "The live Chrome tab remains open, "
                            "but the AI planner could not choose "
                            "the next action."
                        ),
                    )

                    task_repository.update(task)

                    await ws_manager.send(
                        task.id,
                        _terminal_payload(task),
                    )

                    return None

        task.plan_source = (
            interaction_plan.source
        )

        # -----------------------------------------------------------
        # COMPLETION
        # -----------------------------------------------------------

        if interaction_plan.done:
            # NEVER trust the model's `done=true` by itself. A local model
            # can hallucinate completion even when the browser is still on
            # the first page. Re-observe the live browser and require the
            # independent final-goal verifier to agree.
            fresh_context = await inspector.snapshot(page)
            fresh_context["current_url"] = page.url
            fresh_context["current_title"] = await _safe_title(page)
            fresh_context["completed_actions"] = completed_actions[-12:]
            fresh_context["failed_actions"] = failures[-6:]
            fresh_context["interaction_step"] = step + 1
            fresh_context["remaining_instruction"] = instruction

            verified_done = await _page_proves_final_goal(
                instruction,
                fresh_context,
                completed_actions,
                page,
            )

            if not verified_done:
                repeated_done_claims += 1
                rt.false_done_total += 1
                if planned_by in _PROVIDER_NAMES:
                    if rt.health.record_false_done(planned_by):
                        rt.recorder.record(
                            ev.PROVIDER_STATUS, step=step + 1, provider=planned_by,
                            state="unavailable", failure="false_done",
                            reason=f"{planned_by} distrusted for this task after repeated false done=true",
                        )
                    if last_plan_digest:
                        rt.health.mark_input_failed(planned_by, last_plan_digest)
                rt.recorder.record(
                    ev.VERIFICATION, step=step + 1, claimed_done=(planned_by != "verifier"),
                    result="incomplete", provider=planned_by, url=page.url,
                    reason="live browser state does not prove the final goal",
                )
                logger.warning(
                    "PLANNER CLAIMED COMPLETE (provider=%s) / VERIFIER RESULT: INCOMPLETE / "
                    "REASON: live state at %s does not prove the goal (false-done %d/%d)",
                    planned_by, page.url, rt.false_done_total, settings.MAX_FALSE_DONE_CLAIMS,
                )
                # Feed the false completion back into the next planning
                # iteration. This prevents the exact failure where Qwen
                # returns {done:true, actions:[]} on github.com before it
                # has searched, opened a repository, or found installation.
                contract = fresh_context.get("task_contract") or _build_task_contract(instruction)
                extracted = _extracted_results(completed_actions)
                failures.append({
                    "step": step + 1,
                    "action": None,
                    "error": (
                        "Planner claimed task completion, but the live browser "
                        "state does not prove the final goal. Missing requirements: "
                        f"search={contract.get('search_queries', [])}; "
                        f"requires_result_open={contract.get('requires_result_open')}; "
                        f"requires_extraction={contract.get('requires_extraction')}; "
                        f"extracted={extracted[-2:]}"
                    ),
                    "url": page.url,
                })
                force_ai_replan = True

                logger.warning(
                    "Rejected planner done=true for task %s: final-goal verification failed at %s",
                    task.id, page.url,
                )
                # First repair locally when the missing requirement is an
                # information extraction. This avoids burning another AI call
                # on the exact same false-completion state.
                repair = _generic_extraction_recovery(
                    fresh_context.get("task_contract") or _build_task_contract(instruction),
                    fresh_context,
                )
                if repair is not None:
                    forced_recovery_plan = repair
                    rt.recorder.record(
                        ev.RECOVERY, step=step + 1, level=2, strategy="semantic_extraction",
                        reason="planner claimed done; extracting the requested content instead",
                    )
                else:
                    # Switch strategy instead of asking the same planner again.
                    dom = (
                        _dom_recovery_plan(instruction, fresh_context, completed_actions)
                        if rt.false_done_total >= 2 else None
                    )
                    if dom is not None:
                        forced_recovery_plan = dom
                        rt.recorder.record(
                            ev.RECOVERY, step=step + 1, level=3, strategy="generic_dom_recovery",
                            reason=f"{rt.false_done_total} false done claims; switching strategy",
                        )
                    elif rt.false_done_total >= settings.MAX_FALSE_DONE_CLAIMS:
                        rt.recorder.record(
                            ev.RECOVERY, step=step + 1, level=6, strategy="honest_failure",
                            reason=f"{rt.false_done_total} false done claims and no recovery available",
                        )
                        task.mark_finished(
                            TaskStatus.FAILED,
                            error=(
                                f"The planner claimed the task was complete {rt.false_done_total} times, "
                                "but the live browser state never satisfied independent verification."
                            ),
                            final_url=page.url,
                            final_title=await _safe_title(page),
                            retries=retry_budget.used,
                            message="The live Chrome tab remains open for manual recovery.",
                        )
                        task_repository.update(task)
                        await ws_manager.send(task.id, _terminal_payload(task))
                        return None
                continue

            rt.recorder.record(
                ev.VERIFICATION, step=step + 1, claimed_done=(planned_by != "verifier"),
                result="complete", provider=planned_by, url=page.url,
            )
            workflow_actions: list[dict] = []

            for seed_action in (
                workflow_seed_actions or []
            ):
                workflow_actions.append(
                    seed_action.model_dump(
                        mode="json"
                    )
                )

            for completed in completed_actions:
                action_data = completed.get(
                    "action"
                )

                if isinstance(
                    action_data,
                    dict,
                ):
                    workflow_actions.append(
                        dict(action_data)
                    )

            if workflow_actions:
                save_workflow(
                    instruction,
                    workflow_actions,
                    verified_extraction=bool((context.get("task_contract") or {}).get("requires_extraction")),
                )

                logger.info(
                    "Saved verified successful workflow "
                    "for task %s with %d actions.",
                    task.id,
                    len(workflow_actions),
                )

            last_result = {
                "final_url": page.url,
                "final_title": await _safe_title(
                    page
                ),
                "message": (
                    "Task completed and verified "
                    "from the current browser state."
                ),
            }

            break

        # -----------------------------------------------------------
        # EMPTY PLAN
        # -----------------------------------------------------------

        if not interaction_plan.actions:
            task.mark_finished(
                TaskStatus.FAILED,
                error=(
                    "The planner returned no action and "
                    "did not mark the task complete."
                ),
                final_url=page.url,
                final_title=await _safe_title(page),
                retries=retry_budget.used,
                message=(
                    "The live Chrome tab remains open "
                    "for manual recovery."
                ),
            )

            task_repository.update(task)

            await ws_manager.send(
                task.id,
                _terminal_payload(task),
            )

            return None

        # -----------------------------------------------------------
        # EXECUTE A SHORT VERIFIED ACTION CHUNK
        # -----------------------------------------------------------

        planned_actions = list(interaction_plan.actions)   # trimmed to ONE executable action below
        current_url = page.url
        executable_actions: list[Action] = []

        # Reject exact repeats inside the same browser state.
        contract_for_actions = context.get("task_contract") or _build_task_contract(instruction)
        normalized_planned_actions: list[Action] = []
        for candidate in planned_actions:
            if _semantic_extraction_requested(contract_for_actions, candidate):
                logger.info(
                    "Normalizing semantic extraction to page-level mode for task %s: %s",
                    task.id,
                    candidate.reason,
                )
                candidate = candidate.model_copy(
                    update={
                        "target": None,
                        "reason": candidate.reason or "Extract the requested information from the relevant live page content.",
                    }
                )
            normalized_planned_actions.append(candidate)

        planned_actions = normalized_planned_actions
        ladder_restart = False
        for candidate in planned_actions:
            action_data = candidate.model_dump(mode="json")
            action_fingerprint = json.dumps(action_data, sort_keys=True, ensure_ascii=False)

            # ---- STATE-AWARE NAVIGATION: never destroy progress -------------------
            if candidate.type == ActionType.NAVIGATE:
                skip_why = _navigation_regression_reason(page.url, candidate.url or "", completed_actions)
                if skip_why:
                    rt.recorder.record(ev.RECOVERY, step=step + 1, level=2, strategy="skip_navigation",
                                       reason=skip_why, url=page.url)
                    blocked_actions.append({"action": action_data, "url": current_url, "reason": skip_why})
                    force_ai_replan = False
                    continue

            # ---- NEVER REPEAT AN ACTION THAT ALREADY FAILED TO CHANGE THIS STATE ----
            if candidate.type in {ActionType.CLICK, ActionType.PRESS, ActionType.NAVIGATE, ActionType.HOVER, ActionType.SELECT_OPTION} \
                    and tracker.previously_tried(action_data, obs_now) >= 1:
                state_key = f"{obs_now.url_key}|{obs_now.page_id}|{obs_now.text_digest}"
                idx = ladder.next_index(state_key)
                strat = strategy_name(idx)
                replacement: Action | None = None
                if strat == "investigate_new_page":
                    others = [p for p in browser_manager.pages_snapshot()
                              if p is not page and not str(getattr(p, "url", "")).startswith("about:blank")]
                    adopted_ok = bool(others) and await browser_manager.adopt_page(others[-1])
                    rt.recorder.record(ev.RECOVERY, step=step + 1, level=2, strategy=strat,
                                       result="adopted" if adopted_ok else "no_other_page",
                                       reason="same action already failed to change this page; checking for a new tab", url=page.url)
                    if adopted_ok:
                        browser_manager.adoption_log.append({"action": "recovery", "from_url": page.url,
                                                             "to_url": browser_manager.current_page.url, "title": "",
                                                             "page_count": len(browser_manager.pages_snapshot())})
                        ladder_restart = True
                        break
                    idx = ladder.next_index(state_key)
                    strat = strategy_name(idx)
                if strat == "alternate_target":
                    tried = [m["action"] for m in blocked_actions if isinstance(m, dict) and m.get("action")] + [action_data]
                    alt = alternate_click_target(context, tried)
                    if alt is not None:
                        try:
                            replacement = Action.model_validate(alt)
                        except Exception:
                            replacement = None
                    if replacement is None:
                        idx = ladder.next_index(state_key); strat = strategy_name(idx)
                if replacement is None and strat == "scroll":
                    replacement = Action.model_validate(scroll_action())
                if replacement is None and strat == "generic_dom":
                    dom_plan = _dom_recovery_plan(instruction, context, completed_actions)
                    if dom_plan is not None:
                        for cand in dom_plan.actions:
                            if tracker.previously_tried(cand.model_dump(mode="json"), obs_now) < 1:
                                replacement = cand
                                break
                if replacement is not None:
                    rt.recorder.record(ev.RECOVERY, step=step + 1, level=3, strategy=strat or "alternate",
                                       reason=f"{candidate.type.value} was already tried on this page without progress; trying a different strategy",
                                       target=_target_label(replacement), url=page.url)
                    blocked_actions.append({"action": action_data, "url": current_url, "reason": "already tried without progress"})
                    candidate = replacement
                    action_data = candidate.model_dump(mode="json")
                    action_fingerprint = json.dumps(action_data, sort_keys=True, ensure_ascii=False)
                else:
                    rt.recorder.record(ev.RECOVERY, step=step + 1, level=6, strategy="honest_failure",
                                       reason="recovery ladder exhausted: no untried strategy changes the browser state", url=page.url)
                    task.mark_finished(
                        TaskStatus.FAILED,
                        error=("Recovery exhausted: the same action did not change the page and no alternative "
                               "strategy (new-tab check, alternate target, scroll, DOM recovery) made progress. "
                               f"Verdicts: {tracker.counts}."),
                        final_url=page.url, final_title=await _safe_title(page), retries=retry_budget.used,
                        message="The live Chrome tab remains open for manual recovery.",
                    )
                    task_repository.update(task)
                    await ws_manager.send(task.id, _terminal_payload(task))
                    return None

            repeated = sum(
                1
                for item in completed_actions
                if item.get("action_fingerprint") == action_fingerprint
                and item.get("url_before") == current_url
            )
            if repeated >= 2:
                blocked_actions.append({
                    "action": action_data,
                    "url": current_url,
                    "reason": "The same action was already executed twice without sufficient progress.",
                })
                failures.append({
                    "step": step + 1,
                    "action": action_data,
                    "error": "Repeated the same action on the same page twice; action blocked and planner switched to repair mode.",
                    "url": current_url,
                })
                force_ai_replan = True
                contract_now = context.get("task_contract") or _build_task_contract(instruction)
                repair = _generic_extraction_recovery(contract_now, context)
                if repair is not None:
                    forced_recovery_plan = repair
                logger.warning(
                    "Blocked repeated action for task %s: %s; repair mode enabled.",
                    task.id, action_data,
                )
                continue
            executable_actions.append(candidate)
            break   # exactly ONE action per adaptive step; the rest were planned against a stale DOM

        if ladder_restart:
            continue   # a new tab was adopted: re-observe it before planning anything

        if not executable_actions:
            # Do not spin through the remaining interaction budget when the
            # same recovery action has already been blocked. Prefer a bounded
            # recovery path; if none is available, fail cleanly rather than
            # replaying an identical plan 20+ times.
            planning_failures += 1
            contract_now = context.get("task_contract") or _build_task_contract(instruction)
            recovered = None

            if contract_now.get("search_queries"):
                try:
                    if _current_page_matches_requested_result(contract_now, context) and contract_now.get("requires_extraction"):
                        recovered = _generic_extraction_recovery(contract_now, context)
                        if recovered is None:
                            recovered = _dom_recovery_plan(instruction, context, completed_actions)
                    else:
                        before_url = page.url
                        try:
                            await page.go_back(wait_until="commit", timeout=min(settings.BROWSER_NAV_TIMEOUT_MS, 5000))
                        except Exception as back_exc:
                            if page.url == before_url:
                                raise
                            logger.info("Bounded recovery navigation changed URL despite wait failure: %s", back_exc)
                        context = await inspector.snapshot(page)
                        context["current_url"] = page.url
                        context["current_title"] = await _safe_title(page)
                        context["completed_actions"] = completed_actions[-12:]
                        context["failed_actions"] = failures[-6:]
                        context["remaining_instruction"] = instruction
                        recovered = try_live_path(instruction, context)
                    if recovered is not None:
                        forced_recovery_plan = recovered
                        planning_failures = max(0, planning_failures - 1)
                        logger.info("Recovered blocked action using current or previous browser state.")
                except Exception as recovery_exc:
                    logger.warning("Bounded blocked-action recovery failed: %s", recovery_exc)

            if recovered is None and planning_failures >= max_planning_failures:
                task.mark_finished(
                    TaskStatus.FAILED,
                    error="The agent became stuck repeating an action without measurable browser progress.",
                    final_url=page.url,
                    final_title=await _safe_title(page),
                    retries=retry_budget.used,
                    message="The live Chrome tab remains open for manual recovery.",
                )
                task_repository.update(task)
                await ws_manager.send(task.id, _terminal_payload(task))
                return None

            force_ai_replan = True
            continue

        # Execute sequentially, but stop the chunk immediately if a browser
        # action fails. The next iteration gets a fresh DOM and replans.
        for offset, action in enumerate(executable_actions, start=1):
            action_data = action.model_dump(mode="json")
            action_fingerprint = json.dumps(action_data, sort_keys=True, ensure_ascii=False)
            url_before = page.url
            before_obs = observe(context, url_before, *_page_identity(page))
            pending_eval = None
            t_act = time.perf_counter()
            try:
                rt.budget.require_browser(3.0)
                rt.recorder.record(
                    ev.ACTION_START, step=step + 1, action=action.type.value,
                    target=_target_label(action), provider=planned_by,
                    reason=action.reason, url=url_before,
                )
                t_act = time.perf_counter()
                result = await asyncio.wait_for(
                    execute_plan(page, [action], retry_budget),
                    timeout=max(1.0, rt.budget.remaining()),
                )
                page = await _sync_active_page(task, page, step + 1)
                # A click may have opened/adopted a popup. Always refresh the
                # orchestrator's page reference from the browser manager.
                live_page = browser_manager.current_page
                if live_page is not None and live_page is not page:
                    page = live_page
                last_result = result
                completed_actions.append({
                    "step": step + offset,
                    "action": action_data,
                    "action_fingerprint": action_fingerprint,
                    "url_before": url_before,
                    "url_after": page.url,
                    "title_after": await _safe_title(page),
                    "result": result,
                })
                task.actions_count += 1
                _act_s = time.perf_counter() - t_act
                rt.budget.charge_browser(_act_s)
                rt.recorder.record(
                    ev.ACTION_RESULT, step=step + 1, action=action.type.value,
                    target=_target_label(action), result="ok",
                    duration_ms=int(_act_s * 1000), url=page.url,
                    title=await _safe_title(page),
                )
                pending_eval = {"before": before_obs, "action": action_data, "ok": True}

                # A navigation/click/submit may have changed the DOM. Do not
                # blindly execute the remaining planned actions against a
                # stale page. Only continue when the next action is a simple
                # same-page action; otherwise force a new observation.
                if action.type in {ActionType.NAVIGATE, ActionType.CLICK, ActionType.PRESS}:
                    if offset < len(executable_actions):
                        logger.info("Action changed page state; stopping current chunk and re-observing.")
                    break

            except BudgetExhausted as exc:
                rt.recorder.record(
                    ev.ACTION_RESULT, step=step + 1, action=action.type.value,
                    result="skipped_budget", error=str(exc), url=page.url,
                )
                await browser_manager.focus_current_page()
                _finish_deadline(task, rt, page.url, await _safe_title(page),
                                 f"before browser action at step {step + 1}", retry_budget.used)
                await ws_manager.send(task.id, _terminal_payload(task))
                return None
            except asyncio.TimeoutError:
                _msg = "Browser action exceeded the remaining task budget and was abandoned."
                _act_s = time.perf_counter() - t_act
                rt.budget.charge_browser(_act_s)
                failures.append({"step": step + offset, "action": action_data, "error": _msg, "url": page.url})
                rt.recorder.record(
                    ev.ACTION_RESULT, step=step + 1, action=action.type.value,
                    target=_target_label(action), result="timeout",
                    duration_ms=int(_act_s * 1000), error=_msg, url=page.url,
                )
                pending_eval = {"before": before_obs, "action": action_data, "ok": False, "error": _msg}
                break
            except ExecutionError as exc:
                failures.append({
                    "step": step + offset,

                    "action": action_data,
                    "error": exc.message,
                    "url": page.url,
                })
                logger.warning("Adaptive action failed; replanning from fresh DOM: %s", exc.message)
                _act_s = time.perf_counter() - t_act
                rt.budget.charge_browser(_act_s)
                rt.recorder.record(
                    ev.ACTION_RESULT, step=step + 1, action=action.type.value,
                    target=_target_label(action), result="failed",
                    duration_ms=int(_act_s * 1000), error=exc.message, url=page.url,
                )
                pending_eval = {"before": before_obs, "action": action_data, "ok": False, "error": exc.message}
                break
            except Exception as exc:
                failures.append({
                    "step": step + offset,
                    "action": action_data,
                    "error": _friendly_error(exc),
                    "url": page.url,
                })
                logger.exception("Unexpected adaptive action error for task %s", task.id)
                _act_s = time.perf_counter() - t_act
                rt.budget.charge_browser(_act_s)
                rt.recorder.record(
                    ev.ACTION_RESULT, step=step + 1, action=action.type.value,
                    target=_target_label(action), result="failed",
                    duration_ms=int(_act_s * 1000), error=_friendly_error(exc), url=page.url,
                )
                pending_eval = {"before": before_obs, "action": action_data, "ok": False, "error": _friendly_error(exc)}
                break

        # If nothing advanced, the next iteration still gets fresh context;
        # repeated-state protection prevents an infinite loop.
        continue

    else:
        # -----------------------------------------------------------
        # MAXIMUM ADAPTIVE STEPS
        # -----------------------------------------------------------

        await browser_manager.focus_current_page()

        task.mark_finished(
            TaskStatus.TIMEOUT,
            error=(
                "The adaptive agent reached its maximum "
                "interaction steps. The Chrome tab remains "
                "open for manual continuation."
            ),
            final_url=page.url,
            final_title=await _safe_title(page),
            retries=retry_budget.used,
            message=(
                "Maximum adaptive interaction steps reached."
            ),
        )

        task_repository.update(task)

        await ws_manager.send(
            task.id,
            _terminal_payload(task),
        )

        return None

    extracted = _extracted_results(completed_actions)
    return {
        "final_url": page.url,
        "final_title": await _safe_title(page),
        "result": extracted[-1] if extracted else None,
        "message": (
            ("Task completed. Result: " + extracted[-1])
            if extracted
            else (last_result.get("message") or "Task completed.")
        ),
    }


# ===================================================================
# LIVE INTERACTION DECISION
# ===================================================================


def _needs_live_interaction(
    instruction: str,
    initial_plan: ActionPlan,
) -> bool:
    """
    AI-path tasks are executed as:

        inspect -> act -> inspect -> act

    The initial AI plan is primarily used to reach the first page.
    """

    has_navigation = any(
        action.type == ActionType.NAVIGATE
        for action in initial_plan.actions
    )

    return (
        has_navigation
        and not initial_plan.done
    )


# ===================================================================
# HELPERS
# ===================================================================


async def _safe_title(page) -> str:
    # Bounded: page.title() has no timeout of its own and can block for as long as
    # a hung navigation keeps the page busy (observed: ~70s after a navigation timeout).
    try:
        return await asyncio.wait_for(page.title(), timeout=2.0)
    except Exception:
        return ""


def _friendly_error(
    exc: Exception,
) -> str:
    text = str(exc).strip()

    if (
        "Target page, context or browser has been closed"
        in text
    ):
        return (
            "The Chrome session closed unexpectedly. "
            "Run the task again to start a fresh Chrome session."
        )

    return (
        text
        or exc.__class__.__name__
    )


def _terminal_payload(
    task: TaskRecord,
) -> dict:
    rt = _RUNTIME.get(task.id)
    events_out: list[dict] = []
    stats_out: dict = {}
    paused = task.status in _PAUSED
    if rt is not None:
        if not rt.recorder.finalized and not paused:
            rt.recorder.finalized = True
            kind = {
                TaskStatus.SUCCESS: ev.TASK_SUCCESS,
                TaskStatus.TIMEOUT: ev.TASK_TIMEOUT,
            }.get(task.status, ev.TASK_FAILURE)
            rt.recorder.record(kind, status=task.status.value, url=task.final_url,
                               title=task.final_title, error=task.error, message=task.message)
        stats_out = rt.recorder.stats()
        stats_out["provider_selection"] = getattr(rt, "selection", None)
        ok_providers = [e.get("provider") for e in rt.recorder.events
                        if e["type"] == ev.AI_PLAN and e.get("result") == "ok" and e.get("provider") in _PROVIDER_NAMES]
        stats_out["provider_used"] = ok_providers[-1] if ok_providers else None
        stats_out["providers_used"] = sorted(set(ok_providers))
        task.provider_used = stats_out["provider_used"]
        events_out = list(rt.recorder.events)
        # ONE final-status policy for every entry point: success needs every requirement VERIFIED (when a contract exists)
        if task.status == TaskStatus.SUCCESS and task.contract_state and task.contract_state != "VERIFIED_COMPLETE":
            task.status = TaskStatus.FAILED
            task.error = task.error or ("Not every requirement was verified, so the task is not reported as complete "
                                        f"(contract state: {task.contract_state}).")
        task.outcome = ev.derive_outcome(task.status.value, events_out)
        task.stats = stats_out
        task.events = events_out
    task.verification_status = ev.derive_verification(task.status.value, events_out)
    task.contract_state = task.contract_state if task.contract else task.verification_status
    try:
        from services.persistence import persistence
        persistence.on_terminal(task)
    except Exception:           # persistence must never affect the browser loop or the result
        logger.debug("persistence hook failed", exc_info=True)
    return {
        "events": events_out,
        "stats": stats_out,
        "type": "result",
        "status": task.status.value,
        "final_url": task.final_url,
        "final_title": task.final_title,
        "screenshot_base64": None,
        "message": task.message,
        "result": task.result,
        "error": task.error,
        "duration_ms": task.duration_ms,
        "plan_source": task.plan_source,
        "retries": task.retries,
        "pending": task.pending,
        "understanding": task.understanding,
        "provider": task.provider,
        "provider_used": task.provider_used,
        "outcome": task.outcome or ev.derive_outcome(task.status.value, events_out),
        "verification_status": task.verification_status,
        "contract_state": task.contract_state,
        "contract": task.contract,
        "persistence": task.persistence,
    }