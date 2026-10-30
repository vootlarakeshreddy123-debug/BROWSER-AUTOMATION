"""Task deadline and time-budget model.

The old design had one outer ``asyncio.wait_for`` around the whole task, so no
inner operation knew how much time was left and one slow AI provider could eat
the entire deadline.  ``TaskBudget`` gives every major operation (AI call,
browser action, DOM snapshot, recovery) a way to ask "can I still afford this?"
and records where the time actually went.

Dependency-free on purpose (stdlib only) so it can be unit tested anywhere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExhausted(Exception):
    """Raised when an operation cannot be started within the remaining budget."""

    def __init__(self, kind: str, remaining_s: float, needed_s: float) -> None:
        super().__init__(
            f"{kind} budget exhausted: {remaining_s:.1f}s remaining, {needed_s:.1f}s needed"
        )
        self.kind = kind
        self.remaining_s = remaining_s
        self.needed_s = needed_s


@dataclass
class TaskBudget:
    """Deadline plus per-category budgets for one task.

    total_s            internal deadline (shorter than the outer hard timeout)
    ai_fraction        max share of total_s that may be spent waiting for AI
    recovery_fraction  max share of total_s for recovery work
    browser_reserve_s  time always kept free for browser work + final verification;
                       AI calls may not start if they would eat into it
    """

    total_s: float
    ai_fraction: float = 0.35
    recovery_fraction: float = 0.15
    browser_reserve_s: float = 20.0
    clock: callable = time.monotonic
    started: float = field(init=False)
    ai_spent_s: float = 0.0
    browser_spent_s: float = 0.0
    recovery_spent_s: float = 0.0

    def __post_init__(self) -> None:
        self.started = self.clock()

    # ---- time queries -------------------------------------------------
    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return max(0.0, self.total_s - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    @property
    def ai_budget_s(self) -> float:
        return self.total_s * self.ai_fraction

    @property
    def recovery_budget_s(self) -> float:
        return self.total_s * self.recovery_fraction

    def ai_remaining(self) -> float:
        """Seconds an AI call may still use: bounded by the AI share AND by the
        time that must stay free for the browser."""
        by_share = self.ai_budget_s - self.ai_spent_s
        by_reserve = self.remaining() - self.browser_reserve_s
        return max(0.0, min(by_share, by_reserve))

    def recovery_remaining(self) -> float:
        return max(0.0, min(self.recovery_budget_s - self.recovery_spent_s, self.remaining()))

    # ---- admission checks --------------------------------------------
    def can_start_ai(self, needed_s: float) -> bool:
        return self.ai_remaining() >= needed_s

    def can_start_browser(self, needed_s: float = 2.0) -> bool:
        return self.remaining() >= needed_s

    def can_start_recovery(self, needed_s: float = 2.0) -> bool:
        return self.recovery_remaining() >= needed_s

    def require_ai(self, needed_s: float) -> float:
        """Return the timeout an AI call may use (>= needed_s) or raise."""
        avail = self.ai_remaining()
        if avail < needed_s:
            raise BudgetExhausted("ai", avail, needed_s)
        return avail

    def require_browser(self, needed_s: float = 2.0) -> float:
        rem = self.remaining()
        if rem < needed_s:
            raise BudgetExhausted("browser", rem, needed_s)
        return rem

    # ---- accounting ---------------------------------------------------
    def charge_ai(self, seconds: float) -> None:
        self.ai_spent_s += max(0.0, seconds)

    def charge_browser(self, seconds: float) -> None:
        self.browser_spent_s += max(0.0, seconds)

    def charge_recovery(self, seconds: float) -> None:
        self.recovery_spent_s += max(0.0, seconds)

    def snapshot(self) -> dict:
        return {
            "total_s": round(self.total_s, 1),
            "elapsed_s": round(self.elapsed(), 1),
            "remaining_s": round(self.remaining(), 1),
            "ai_spent_s": round(self.ai_spent_s, 1),
            "ai_budget_s": round(self.ai_budget_s, 1),
            "browser_spent_s": round(self.browser_spent_s, 1),
            "recovery_spent_s": round(self.recovery_spent_s, 1),
        }


@dataclass
class CallLimits:
    """Hard limits for ONE provider call (which may internally issue a bounded
    number of HTTP requests, e.g. initial + one JSON repair).  Every request,
    including repair, is charged against these limits, so repair can never
    bypass the emergency budget."""

    timeout_s: float                 # cap for each individual request
    max_requests: int = 1            # initial + repair requests allowed
    total_time_s: float | None = None  # cap for the whole call incl. repair
    requests_used: int = 0
    elapsed_s: float = 0.0
    _clock: callable = time.monotonic
    _started: float | None = None

    def begin(self) -> None:
        self._started = self._clock()

    def time_left(self) -> float:
        if self.total_time_s is None:
            return float("inf")
        used = 0.0 if self._started is None else self._clock() - self._started
        return max(0.0, self.total_time_s - used)

    def can_request(self, min_needed_s: float = 5.0) -> bool:
        return self.requests_used < self.max_requests and self.time_left() >= min_needed_s

    def next_timeout(self) -> float:
        return max(1.0, min(self.timeout_s, self.time_left()))

    def note_request(self) -> None:
        self.requests_used += 1
