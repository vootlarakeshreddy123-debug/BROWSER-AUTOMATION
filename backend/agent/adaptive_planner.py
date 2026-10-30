"""Provider orchestration for ONE adaptive planning step.

    Gemini (primary)  ->  Ollama (bounded emergency)  ->  PlannerUnavailable

The caller (orchestrator) reacts to ``PlannerUnavailable`` with generic
live-DOM recovery.  This module guarantees:

* a provider that is in cooldown / marked unavailable for this task is NOT called
* every call is bounded by ``min(provider timeout, budget.ai_remaining())`` and
  is never started when the remaining AI budget cannot support it
* Ollama has a hard per-task call + time budget (repair requests included)
* the identical input is never sent twice to a provider that already failed on it
* every attempt is recorded as structured events (AI_PLAN / PROVIDER_STATUS)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agent import events as ev
from agent.budget import CallLimits, TaskBudget
from agent.progress import observe
from agent.provider_health import FailureKind, ProviderHealth, classify_failure, input_digest

MIN_START_S = {"gemini": 4.0, "ollama": 6.0}      # any other (hosted) provider: DEFAULT_MIN_START_S
DEFAULT_MIN_START_S = 4.0


class PlannerUnavailable(RuntimeError):
    """No provider could produce a plan for this step (fast, never slow)."""

    def __init__(self, reason: str, attempts: list[dict]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


@dataclass
class PlanOutcome:
    plan: Any
    provider: str
    latency_s: float
    digest: str
    attempts: list[dict] = field(default_factory=list)


def step_digest(instruction: str, context: dict, planner_mode: str) -> str:
    obs = observe(context)
    failed = [
        (str(f.get("error"))[:80], str((f.get("action") or {}).get("type")))
        for f in (context.get("failed_actions") or [])[-4:] if isinstance(f, dict)
    ]
    return input_digest(instruction, obs.url_key, obs.text_digest, len(obs.interactive),
                        failed, planner_mode)


async def plan_next_action(
    *,
    instruction: str,
    context: dict,
    providers: dict[str, Any],
    health: ProviderHealth,
    budget: TaskBudget,
    recorder: ev.ExecutionRecorder,
    step: int,
    planner_mode: str = "normal",
    gemini_timeout_s: float = 15.0,
    ollama_request_timeout_s: float = 20.0,
    order: list[str] | None = None,
    hosted_timeout_s: float = 20.0,
    transient_retries: int = 1,
    backoff_base_s: float = 0.5,
) -> PlanOutcome:
    digest = step_digest(instruction, context, planner_mode)
    attempts: list[dict] = []
    url = context.get("current_url")

    for name in (order or ["groq", "gemini", "ollama"]):
        provider = providers.get(name)
        if provider is None:
            attempts.append({"provider": name, "result": "not_configured"})
            continue

        allowed, kind, reason = health.check(name)
        if not allowed:
            attempts.append({"provider": name, "result": kind.value if kind else "blocked", "reason": reason})
            recorder.record(ev.PROVIDER_STATUS, step=step, provider=name, state="skipped",
                            failure=kind.value if kind else None, reason=reason)
            continue
        if health.is_repeat_failure(name, digest):
            attempts.append({"provider": name, "result": FailureKind.REPEAT.value})
            recorder.record(ev.PROVIDER_STATUS, step=step, provider=name, state="skipped",
                            failure=FailureKind.REPEAT.value,
                            reason="identical planner input already failed for this provider")
            continue

        needed = MIN_START_S.get(name, DEFAULT_MIN_START_S)
        if not budget.can_start_ai(needed):
            reason = (f"AI planning budget cannot support a {name} call "
                      f"({budget.ai_remaining():.1f}s available, {needed:.0f}s needed)")
            attempts.append({"provider": name, "result": FailureKind.BUDGET.value, "reason": reason})
            recorder.record(ev.PROVIDER_STATUS, step=step, provider=name, state="skipped",
                            failure=FailureKind.BUDGET.value, reason=reason)
            continue

        limit_s = budget.ai_remaining()
        limits: CallLimits | None = None
        if name == "gemini":
            timeout = min(gemini_timeout_s, limit_s)
        elif name != "ollama":
            timeout = min(float(getattr(provider, "timeout_s", 0) or hosted_timeout_s), limit_s)   # provider-specific timeout wins
        else:
            total = min(health.ollama_time_left(), limit_s)
            limits = CallLimits(timeout_s=min(ollama_request_timeout_s, total),
                                max_requests=max(1, health.ollama_calls_left()),
                                total_time_s=total)
            timeout = total

        health.begin_call(name)
        recorder.record(ev.PROVIDER_STATUS, step=step, provider=name, state="calling",
                        timeout_s=round(timeout, 1), url=url, model=getattr(provider, "model", None))
        started = time.perf_counter()
        attempts_left = 1 + (max(0, transient_retries) if name not in ("ollama",) else 0)
        failure, plan, retry_after = None, None, None
        while True:
            try:
                kwargs = {"limits": limits} if limits is not None else {}
                plan = await asyncio.wait_for(
                    provider.generate_plan(instruction, page_context=context, **kwargs),
                    timeout=timeout,
                )
                failure = None
            except asyncio.TimeoutError as exc:
                failure, err = FailureKind.TIMEOUT, f"{name} exceeded {timeout:.1f}s call budget"
                exc_text = str(exc) or err
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                failure, err, exc_text = classify_failure(exc), str(exc)[:240], str(exc)
                retry_after = getattr(exc, "retry_after", None)
            attempts_left -= 1
            # ONE bounded retry, only for a transient server-side failure, never for quota/auth/model/malformed,
            # and only if the AI budget can still afford another call.
            if (failure == FailureKind.UNAVAILABLE and attempts_left > 0
                    and budget.can_start_ai(MIN_START_S.get(name, DEFAULT_MIN_START_S) + backoff_base_s)):
                recorder.record(ev.PROVIDER_STATUS, step=step, provider=name, state="retrying",
                                failure=failure.value, reason="transient server error; one bounded retry")
                await asyncio.sleep(backoff_base_s)
                continue
            break
        latency = time.perf_counter() - started
        budget.charge_ai(latency)
        if limits is not None:      # repair requests count against the emergency budget
            extra = max(0, limits.requests_used - 1)
            health.states[name].calls += extra

        if failure is None:
            health.record_success(name, latency)
            recorder.record(ev.AI_PLAN, step=step, provider=name, result="ok",
                            duration_ms=int(latency * 1000),
                            actions=len(getattr(plan, "actions", []) or []),
                            claimed_done=bool(getattr(plan, "done", False)),
                            url=url)
            attempts.append({"provider": name, "result": "ok"})
            return PlanOutcome(plan, name, latency, digest, attempts)

        health.record_failure(name, failure, latency, exc_text, digest, retry_after_s=retry_after)
        st = health.states[name]
        recorder.record(ev.AI_PLAN, step=step, provider=name, result=failure.value,
                        duration_ms=int(latency * 1000), error=err, url=url)
        recorder.record(ev.PROVIDER_STATUS, step=step, provider=name,
                        state="unavailable" if not st.available else "degraded",
                        failure=failure.value, reason=st.unavailable_reason or err,
                        emergency=(name == "ollama"))
        attempts.append({"provider": name, "result": failure.value, "error": err})

    reasons = "; ".join(f"{a['provider']}={a.get('reason') or a.get('error') or a['result']}" for a in attempts)
    raise PlannerUnavailable(f"No AI provider available for this step ({reasons})", attempts)
