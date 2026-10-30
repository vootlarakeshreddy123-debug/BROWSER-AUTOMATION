"""Per-task provider health + failure classification.

Two layers protect the task deadline from a failing/slow provider:

1. GLOBAL cooldown (module level, survives across tasks): after quota/503/
   timeout a provider is not called again until the cooldown expires.
2. PER-TASK state (``ProviderHealth``): a provider that failed in a way that
   makes retrying pointless for *this task* is marked unavailable for the rest
   of the task, and Ollama (emergency fallback) has a hard call/time budget.

Dependency-free (stdlib only).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class FailureKind(str, Enum):
    QUOTA = "quota"                # 429 / RESOURCE_EXHAUSTED / rate limit
    UNAVAILABLE = "unavailable"    # 503 / UNAVAILABLE / connection refused
    TIMEOUT = "timeout"
    MALFORMED = "malformed"        # invalid/empty/unusable JSON plan
    FALSE_DONE = "false_done"      # done=true rejected by the verifier
    MODEL_UNAVAILABLE = "model_unavailable"  # 404 / model retired / no access: permanent until config changes
    COOLDOWN = "cooldown"          # provider is in cooldown, not called
    BUDGET = "budget"              # task-level budget forbids the call
    REPEAT = "repeat"              # identical input already failed
    AUTH = "auth"                  # 401/403: key invalid / no access - retrying cannot help until config changes
    ERROR = "error"


def classify_failure(exc: BaseException | str) -> FailureKind:
    text = str(exc).lower()
    if any(k in text for k in ("401", "403", "unauthorized", "invalid api key", "invalid x-api-key",
                               "incorrect api key", "permission denied", "authentication")):
        return FailureKind.AUTH
    if any(k in text for k in ("404", "not_found", "not found", "no longer available",
                               "is not supported for", "model is unavailable", "unknown model")):
        return FailureKind.MODEL_UNAVAILABLE
    if any(k in text for k in ("quota", "rate limit", "rate-limit", "429",
                               "resource_exhausted", "too many requests")):
        return FailureKind.QUOTA
    if any(k in text for k in ("503", "unavailable", "overloaded", "connection refused",
                               "all connection attempts failed", "cooldown active")):
        return FailureKind.UNAVAILABLE
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return FailureKind.TIMEOUT
    if any(k in text for k in ("invalid json", "unusable action plan", "empty response",
                               "malformed", "validation error", "must contain at least one action")):
        return FailureKind.MALFORMED
    return FailureKind.ERROR


# ---- global cooldowns (survive across tasks) --------------------------
_LOCK = threading.Lock()
_GLOBAL_COOLDOWN_UNTIL: dict[str, float] = {}


def global_cooldown_remaining(provider: str, now: float | None = None) -> float:
    now = time.monotonic() if now is None else now
    with _LOCK:
        return max(0.0, _GLOBAL_COOLDOWN_UNTIL.get(provider, 0.0) - now)


def set_global_cooldown(provider: str, seconds: float, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _LOCK:
        _GLOBAL_COOLDOWN_UNTIL[provider] = max(_GLOBAL_COOLDOWN_UNTIL.get(provider, 0.0), now + seconds)


def clear_global_cooldowns() -> None:  # used by tests
    with _LOCK:
        _GLOBAL_COOLDOWN_UNTIL.clear()


@dataclass
class ProviderState:
    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    malformed: int = 0
    false_done: int = 0
    time_spent_s: float = 0.0
    unavailable_reason: str | None = None   # set => not callable for the rest of this task
    last_failure: str | None = None
    last_error: str | None = None
    latencies_ms: list[int] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "calls": self.calls, "successes": self.successes, "failures": self.failures,
            "malformed": self.malformed, "false_done": self.false_done,
            "time_spent_s": round(self.time_spent_s, 1),
            "last_failure": self.last_failure,
        }


def _cooldown_for(kind: "FailureKind", default_s: float, retry_after_s: float | None) -> float:
    """QUOTA cooldown = the provider's Retry-After when given (never shorter), else the configured default."""
    if kind == FailureKind.QUOTA and retry_after_s is not None and retry_after_s > 0:
        return max(retry_after_s, 1.0)
    return default_s


def input_digest(*parts) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


class _States(dict):
    """name -> ProviderState, created on first use so ANY registered provider can be tracked."""

    def __missing__(self, name: str) -> "ProviderState":
        st = self[name] = ProviderState(name)
        return st


class ProviderHealth:
    """Health/budget state for the providers used by ONE task."""

    def __init__(
        self,
        *,
        gemini_cooldown_s: float = 300.0,
        gemini_timeout_cooldown_s: float = 60.0,
        ollama_cooldown_s: float = 120.0,
        ollama_max_calls: int = 2,
        ollama_time_budget_s: float = 30.0,
        gemini_max_malformed: int = 2,
        gemini_model_error_cooldown_s: float = 3600.0,
        max_false_done_per_provider: int = 2,
        hosted_cooldown_s: float = 300.0,
        hosted_max_calls: int = 12,
        clock=time.monotonic,
    ) -> None:
        self.clock = clock
        self.gemini_cooldown_s = gemini_cooldown_s
        self.gemini_timeout_cooldown_s = gemini_timeout_cooldown_s
        self.ollama_cooldown_s = ollama_cooldown_s
        self.ollama_max_calls = ollama_max_calls
        self.ollama_time_budget_s = ollama_time_budget_s
        self.gemini_max_malformed = gemini_max_malformed
        self.gemini_model_error_cooldown_s = gemini_model_error_cooldown_s
        self.max_false_done_per_provider = max_false_done_per_provider
        self.hosted_cooldown_s = hosted_cooldown_s
        self.hosted_max_calls = hosted_max_calls
        self.states = _States()
        self.states["gemini"], self.states["ollama"]   # created up front (keeps snapshot()/any_available() shape)
        self._failed_inputs: dict[tuple[str, str], int] = {}

    # ---- admission -----------------------------------------------------
    def check(self, provider: str) -> tuple[bool, FailureKind | None, str]:
        """(allowed, blocking_kind, reason)."""
        st = self.states[provider]
        if not st.available:
            return False, FailureKind(st.last_failure) if st.last_failure in FailureKind._value2member_map_ else FailureKind.ERROR, \
                f"{provider} unavailable for this task: {st.unavailable_reason}"
        cd = global_cooldown_remaining(provider, self.clock())
        if cd > 0:
            return False, FailureKind.COOLDOWN, f"{provider} cooldown active ({cd:.0f}s remaining)"
        if provider not in ("gemini", "ollama") and st.calls >= self.hosted_max_calls:
            return False, FailureKind.BUDGET, (
                f"{provider} per-task call limit reached ({st.calls}/{self.hosted_max_calls})")
        if provider == "ollama":
            if st.calls >= self.ollama_max_calls:
                return False, FailureKind.BUDGET, (
                    f"ollama emergency call budget exhausted ({st.calls}/{self.ollama_max_calls})")
            if st.time_spent_s >= self.ollama_time_budget_s:
                return False, FailureKind.BUDGET, (
                    f"ollama emergency time budget exhausted ({st.time_spent_s:.0f}s/{self.ollama_time_budget_s:.0f}s)")
        return True, None, "ok"

    def ollama_time_left(self) -> float:
        st = self.states["ollama"]
        return max(0.0, self.ollama_time_budget_s - st.time_spent_s)

    def ollama_calls_left(self) -> int:
        return max(0, self.ollama_max_calls - self.states["ollama"].calls)

    def mark_input_failed(self, provider: str, digest: str) -> None:
        """A plan produced from this exact input was rejected downstream (false
        done, repeated action, no progress). Asking again with identical input
        would just reproduce it."""
        key = (provider, digest)
        self._failed_inputs[key] = self._failed_inputs.get(key, 0) + 1

    def is_repeat_failure(self, provider: str, digest: str) -> bool:
        return self._failed_inputs.get((provider, digest), 0) >= 1

    # ---- recording -----------------------------------------------------
    def begin_call(self, provider: str) -> None:
        self.states[provider].calls += 1

    def record_success(self, provider: str, latency_s: float) -> None:
        st = self.states[provider]
        st.successes += 1
        st.time_spent_s += latency_s
        st.latencies_ms.append(int(latency_s * 1000))

    def record_failure(self, provider: str, kind: FailureKind, latency_s: float,
                       message: str = "", digest: str | None = None, retry_after_s: float | None = None) -> None:
        st = self.states[provider]
        st.failures += 1
        st.time_spent_s += latency_s
        st.latencies_ms.append(int(latency_s * 1000))
        st.last_failure = kind.value
        st.last_error = (message or "")[:300]
        if digest:
            key = (provider, digest)
            self._failed_inputs[key] = self._failed_inputs.get(key, 0) + 1
        now = self.clock()
        if provider not in ("gemini", "ollama"):
            self._record_hosted_failure(st, provider, kind, now, retry_after_s)
            return
        if provider == "gemini":
            if kind in (FailureKind.QUOTA, FailureKind.UNAVAILABLE):
                cd = _cooldown_for(kind, self.gemini_cooldown_s, retry_after_s)
                set_global_cooldown("gemini", cd, now)
                st.unavailable_reason = f"{kind.value} (cooldown {cd:.0f}s)"
            elif kind == FailureKind.AUTH:
                set_global_cooldown("gemini", self.gemini_model_error_cooldown_s, now)
                st.unavailable_reason = "GEMINI_API_KEY was rejected (401/403) - fix the key in backend/.env"
            elif kind == FailureKind.MODEL_UNAVAILABLE:
                # A 404 on the configured model will not fix itself: do not call it again
                # this task, nor in later tasks until the cooldown (or a restart with a
                # corrected GEMINI_MODEL) clears it.
                set_global_cooldown("gemini", self.gemini_model_error_cooldown_s, now)
                st.unavailable_reason = "configured GEMINI_MODEL is not available to this API key (404) - fix GEMINI_MODEL in .env"
            elif kind == FailureKind.TIMEOUT:
                set_global_cooldown("gemini", self.gemini_timeout_cooldown_s, now)
                st.unavailable_reason = f"timeout (cooldown {self.gemini_timeout_cooldown_s:.0f}s)"
            elif kind == FailureKind.MALFORMED:
                st.malformed += 1
                if st.malformed >= self.gemini_max_malformed:
                    st.unavailable_reason = f"malformed output x{st.malformed}"
            elif kind == FailureKind.ERROR:
                st.unavailable_reason = "provider error"
        else:  # ollama = emergency provider: any failure that costs time retires it for the task
            if kind in (FailureKind.TIMEOUT, FailureKind.MALFORMED, FailureKind.ERROR):
                st.unavailable_reason = f"{kind.value} (emergency provider retired for this task)"
                if kind == FailureKind.TIMEOUT:
                    set_global_cooldown("ollama", self.ollama_cooldown_s, now)
            elif kind == FailureKind.UNAVAILABLE:
                st.unavailable_reason = "unavailable"
                set_global_cooldown("ollama", self.ollama_cooldown_s, now)

    def _record_hosted_failure(self, st: "ProviderState", provider: str, kind: FailureKind, now: float,
                               retry_after_s: float | None = None) -> None:
        """Groq / OpenAI / Claude / Grok: circuit-breaker style, never retried on a failure retrying cannot fix.
        A provider-supplied Retry-After is honoured (a rate limit is not cured by calling again sooner)."""
        if kind in (FailureKind.QUOTA, FailureKind.UNAVAILABLE, FailureKind.TIMEOUT):
            cd = _cooldown_for(kind, self.hosted_cooldown_s, retry_after_s)
            set_global_cooldown(provider, cd, now)
            st.unavailable_reason = f"{kind.value} (cooldown {cd:.0f}s)"
        elif kind in (FailureKind.AUTH, FailureKind.MODEL_UNAVAILABLE):
            set_global_cooldown(provider, self.gemini_model_error_cooldown_s, now)
            st.unavailable_reason = (f"{provider}: API key rejected or no access (401/403) - fix the key in backend/.env"
                                     if kind == FailureKind.AUTH else
                                     f"{provider}: configured model is not available to this key (404) - fix the model in backend/.env")
        elif kind == FailureKind.MALFORMED:
            st.malformed += 1
            if st.malformed >= self.gemini_max_malformed:
                st.unavailable_reason = f"malformed output x{st.malformed}"
        else:
            st.unavailable_reason = "provider error"

    def record_false_done(self, provider: str) -> bool:
        """Returns True if the provider is now distrusted for this task."""
        st = self.states[provider]
        st.false_done += 1
        limit = 1 if provider == "ollama" else self.max_false_done_per_provider
        if st.false_done >= limit and st.available:
            st.unavailable_reason = f"false done=true x{st.false_done}"
            st.last_failure = FailureKind.FALSE_DONE.value
            return True
        return False

    def any_available(self) -> bool:
        return any(self.check(p)[0] for p in self.states)

    def snapshot(self) -> dict:
        return {name: st.to_dict() for name, st in self.states.items()}
