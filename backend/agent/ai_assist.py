"""Bounded one-shot JSON calls (task understanding, message authoring) through the SAME provider health, budget and
event machinery as browser planning: a provider that is unconfigured, cooling down, out of budget, or already failed
is skipped, every call is charged to the task's AI budget, and failures are classified (never retried blindly).

Ollama is intentionally not used here: a small local model is reserved for emergency per-step planning, and
understanding must not burn its tiny call budget.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from agent import events as ev
from agent.adaptive_planner import PlannerUnavailable
from agent.provider_health import FailureKind, classify_failure, input_digest

MIN_START_S = 4.0


async def ask_json(*, prompt: str, purpose: str, providers: dict[str, Any], order: list[str], health, budget,
                   recorder, timeout_s: float = 15.0) -> tuple[dict, str]:
    digest = input_digest(purpose, prompt)
    attempts: list[dict] = []
    for name in order:
        provider = providers.get(name)
        if provider is None or name == "ollama" or not hasattr(provider, "complete_json"):
            attempts.append({"provider": name, "result": "not_available"})
            continue
        allowed, kind, reason = health.check(name)
        if not allowed:
            attempts.append({"provider": name, "result": kind.value if kind else "blocked", "reason": reason})
            continue
        if health.is_repeat_failure(name, digest):
            attempts.append({"provider": name, "result": FailureKind.REPEAT.value})
            continue
        if not budget.can_start_ai(MIN_START_S):
            attempts.append({"provider": name, "result": FailureKind.BUDGET.value, "reason": "AI budget exhausted"})
            continue
        timeout = min(timeout_s, budget.ai_remaining())
        health.begin_call(name)
        started = time.perf_counter()
        failure, data, err = None, None, ""
        try:
            data = await asyncio.wait_for(provider.complete_json(prompt, timeout), timeout=timeout)
            if not isinstance(data, dict):
                raise RuntimeError("invalid json: not an object")
        except asyncio.TimeoutError:
            failure, err = FailureKind.TIMEOUT, f"{name} exceeded {timeout:.1f}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            failure, err = classify_failure(exc), str(exc)[:240]
        latency = time.perf_counter() - started
        budget.charge_ai(latency)
        if failure is None:
            health.record_success(name, latency)
            recorder.record(ev.AI_PLAN, provider=name, result="ok", purpose=purpose, duration_ms=int(latency * 1000))
            return data, name
        health.record_failure(name, failure, latency, err, digest)
        recorder.record(ev.AI_PLAN, provider=name, result=failure.value, purpose=purpose,
                        duration_ms=int(latency * 1000), error=err)
        attempts.append({"provider": name, "result": failure.value, "error": err})
    raise PlannerUnavailable(f"No AI provider could answer ({purpose}): " +
                             "; ".join(f"{a['provider']}={a.get('reason') or a.get('error') or a['result']}" for a in attempts),
                             attempts)
