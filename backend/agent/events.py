"""Structured execution events + real statistics for one task.

Every event is a plain dict (JSON-serialisable) with at least:
    seq, type, ts (wall clock), elapsed_ms
plus whatever fields were actually recorded (provider, action, url, duration_ms, ...).
Statistics are computed only from recorded events - nothing is estimated.

``record`` is synchronous (append + log) and optionally fans out to an async
sink (the WebSocket) with a best-effort fire-and-forget task.
Dependency-free (stdlib only).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("agent.events")

TASK_START = "TASK_START"
BROWSER_READY = "BROWSER_READY"
ROUTER = "ROUTER"
OBSERVE = "OBSERVE"
AI_PLAN = "AI_PLAN"
PROVIDER_STATUS = "PROVIDER_STATUS"
ACTION_START = "ACTION_START"
ACTION_RESULT = "ACTION_RESULT"
PROGRESS = "PROGRESS"
VERIFICATION = "VERIFICATION"
RECOVERY = "RECOVERY"
GOAL_PLAN = "GOAL_PLAN"
GOAL_START = "GOAL_START"
GOAL_COMPLETE = "GOAL_COMPLETE"
GOAL_FAILED = "GOAL_FAILED"
GOAL_BLOCKED = "GOAL_BLOCKED"
NEW_PAGE_DETECTED = "NEW_PAGE_DETECTED"
PAGE_ADOPTED = "PAGE_ADOPTED"
ACTIVE_PAGE_CHANGED = "ACTIVE_PAGE_CHANGED"
UNDERSTANDING = "UNDERSTANDING"
CONFIRMATION_REQUESTED = "CONFIRMATION_REQUESTED"
INPUT_REQUESTED = "INPUT_REQUESTED"
TASK_SUCCESS = "TASK_SUCCESS"
TASK_FAILURE = "TASK_FAILURE"
TASK_TIMEOUT = "TASK_TIMEOUT"

TERMINAL = {TASK_SUCCESS, TASK_FAILURE, TASK_TIMEOUT}
MAX_EVENTS = 500


def _clip(value: Any, n: int = 240) -> Any:
    if isinstance(value, str):
        # nothing that looks like an API key / bearer token may reach events, the WebSocket or task history
        from providers.http import redact
        value = redact(value)
        if len(value) > n:
            return value[: n - 1] + "…"
    return value


class ExecutionRecorder:
    def __init__(self, task_id: str, sink: Callable[[dict], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.task_id = task_id
        self.sink = sink
        self.clock = clock
        self.started = clock()
        self.events: list[dict] = []
        self._seq = 0
        self.finalized = False
        self.phase = "starting"
        self.task: Any = None   # optional: object whose .stats attribute mirrors live statistics

    # ---- recording -------------------------------------------------------
    def elapsed_ms(self) -> int:
        return int((self.clock() - self.started) * 1000)

    def record(self, type_: str, **fields: Any) -> dict:
        self._seq += 1
        ev: dict[str, Any] = {"seq": self._seq, "type": type_, "ts": time.time(),
                              "elapsed_ms": self.elapsed_ms()}
        for k, v in fields.items():
            if v is not None:
                ev[k] = _clip(v)
        if len(self.events) >= MAX_EVENTS:      # keep first 50 + most recent
            del self.events[50:100]
        self.events.append(ev)
        self.phase = type_
        logger.info("[%6.1fs] %s %s", ev["elapsed_ms"] / 1000, type_,
                    " ".join(f"{k}={v}" for k, v in ev.items()
                             if k not in {"seq", "type", "ts", "elapsed_ms"}))
        stats = self.stats()
        if self.task is not None:
            try:
                self.task.stats = stats
            except Exception:
                pass
        if self.sink is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.sink({"type": "event", "event": ev, "stats": stats}))
            except RuntimeError:
                pass
        return ev

    # ---- statistics (derived only from recorded events) -----------------
    def stats(self) -> dict:
        ai = [e for e in self.events if e["type"] == AI_PLAN and "duration_ms" in e]
        by_provider: dict[str, dict] = {}
        for e in ai:
            p = e.get("provider", "unknown")
            d = by_provider.setdefault(p, {"calls": 0, "ok": 0, "failed": 0, "total_ms": 0})
            d["calls"] += 1
            d["total_ms"] += int(e["duration_ms"])
            d["ok" if e.get("result") == "ok" else "failed"] += 1
        acts = [e for e in self.events if e["type"] == ACTION_RESULT and "duration_ms" in e]
        prog = [e for e in self.events if e["type"] == PROGRESS]
        counts: dict[str, int] = {}
        for e in prog:
            c = e.get("classification")
            if c:
                counts[c] = counts.get(c, 0) + 1
        ai_total = sum(int(e["duration_ms"]) for e in ai)
        act_total = sum(int(e["duration_ms"]) for e in acts)
        return {
            "elapsed_ms": self.elapsed_ms(),
            "ai_calls": len(ai),
            "gemini_calls": by_provider.get("gemini", {}).get("calls", 0),
            "ollama_calls": by_provider.get("ollama", {}).get("calls", 0),
            "providers": by_provider,
            "ai_total_ms": ai_total,
            "avg_ai_latency_ms": int(ai_total / len(ai)) if ai else None,
            "browser_actions": sum(int(x.get("count", 1)) for x in acts),
            "browser_actions_ok": sum(int(x.get("count", 1)) for x in acts if x.get("result") == "ok"),
            "browser_total_ms": act_total,
            "avg_browser_latency_ms": int(act_total / max(1, sum(int(x.get("count", 1)) for x in acts))) if acts else None,
            "recoveries": sum(1 for e in self.events if e["type"] == RECOVERY),
            "goals_completed": sum(1 for e in self.events if e["type"] == GOAL_COMPLETE),
            "goals_total": next((int(e.get("total", 0)) for e in self.events if e["type"] == GOAL_PLAN), 0),
            "pages_adopted": sum(1 for e in self.events if e["type"] == PAGE_ADOPTED),
            "verifications": sum(1 for e in self.events if e["type"] == VERIFICATION),
            "observations": sum(1 for e in self.events if e["type"] == OBSERVE),
            "progress": counts,
            "false_done_claims": sum(1 for e in self.events
                                     if e["type"] == VERIFICATION and e.get("claimed_done") and e.get("result") == "incomplete"),
            "final_status": next((e["type"] for e in reversed(self.events) if e["type"] in TERMINAL), None),
        }


def derive_outcome(status: str, events: list[dict]) -> str:
    """Honest outcome label from RECORDED evidence only:
    success | partial | blocked | failed | cancelled | awaiting_user.
    ``partial`` = the task ended unsuccessfully but at least one goal was verified complete;
    ``blocked`` = an external condition (login wall, CAPTCHA, ...) stopped it."""
    if status == "success":
        return "success"
    if status in ("awaiting_confirmation", "needs_input"):
        return "awaiting_user"
    if status == "cancelled":
        return "cancelled"
    if any(e.get("type") == GOAL_BLOCKED for e in events):
        return "blocked"
    if any(e.get("type") == GOAL_COMPLETE for e in events):
        return "partial"
    return "failed"


def derive_verification(status: str, events: list[dict]) -> str:
    """THE verification verdict shown by REST, WebSocket, history and the dashboard (one function, one answer).

    VERIFIED_COMPLETE only when the task succeeded AND its last FINAL verification event says VERIFIED_COMPLETE.
    A success without that evidence is reported as ACTION_SUCCEEDED, never silently upgraded."""
    if status == "created":
        return "PENDING"
    if status == "running":
        return "RUNNING"
    if status in ("awaiting_confirmation", "needs_input"):
        return "BLOCKED"
    if status == "cancelled":
        return "CANCELLED"
    if status == "success":
        finals = [e for e in events if e.get("type") == VERIFICATION and e.get("scope") == "final"]
        if finals and finals[-1].get("verdict") == "VERIFIED_COMPLETE":
            return "VERIFIED_COMPLETE"
        return "ACTION_SUCCEEDED"
    if any(e.get("type") == GOAL_BLOCKED for e in events):
        return "BLOCKED"
    return "FAILED"
