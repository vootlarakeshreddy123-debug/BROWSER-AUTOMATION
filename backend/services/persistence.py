"""Persistence manager: records what the BACKEND decided; never decides anything itself.

Rules
  * The browser loop never waits for, nor depends on, a database write: every write is a background task whose failure
    only changes ``task.persistence`` (disabled | pending | saved | failed).
  * "saved" is set ONLY after every outstanding write of that task returned success.  An outage yields "failed" with a
    short reason - never a false "saved".
  * Idempotent: the task row id is the task UUID and every event id is uuid5(task, seq), writes use
    ``ignore-duplicates``, so a retry (or a repeated terminal hook) can never duplicate a row.
  * Bounded retry (``MAX_ATTEMPTS`` with exponential backoff); failed writes stay queued (bounded) and are retried on
    the next hook or by ``retry_failed``.
  * Nothing sensitive is stored: instruction / messages / event details are redacted (keys, tokens, passwords, OTPs,
    masked phone numbers and e-mail addresses) and clipped.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from services.supabase_service import AuthUser, SupabaseError, SupabaseService

logger = logging.getLogger("services.persistence")

MAX_ATTEMPTS = 3
BACKOFF_S = (0.5, 1.5)
MAX_EVENTS = 300
MAX_FAILED_QUEUE = 200
_NS = uuid.UUID("6d6f2f0a-5d1c-4c1e-9d0e-0a7e5a1c2b3d")      # fixed namespace -> deterministic event ids

_DROP_KEYS = {"typed_text", "text", "value", "password", "token", "authorization", "screenshot_base64", "body", "key"}
_STATUS_OK = {"created", "running", "success", "failed", "cancelled", "timeout", "awaiting_confirmation", "needs_input"}


def scrub(text: Any, n: int = 300) -> str:
    """Everything that may reach the database goes through here: secrets/keys/JWTs, passwords/OTPs, masked identifiers."""
    from agent.contract import redact_instruction
    from providers.http import redact
    return redact(redact_instruction(str(text if text is not None else "")))[:n]


def event_id(task_id: str, seq: int) -> str:
    return str(uuid.uuid5(_NS, f"{task_id}:{seq}"))


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(float(ts) if ts else datetime.now(timezone.utc).timestamp(), tz=timezone.utc).isoformat()


def _clean(value: Any, n: int = 300) -> Any:
    if isinstance(value, str):
        return scrub(value, n)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(v, 120) for v in list(value)[:12]]
    if isinstance(value, dict):
        return {str(k)[:40]: _clean(v, 160) for k, v in list(value.items())[:20] if str(k).lower() not in _DROP_KEYS}
    return str(value)[:120]


def event_row(task_id: str, e: dict) -> dict:
    """task_events row for one recorded execution event (redacted, bounded)."""
    seq = int(e.get("seq") or 0)
    details = {k: _clean(v) for k, v in e.items()
               if k not in ("seq", "type", "ts", "elapsed_ms") and str(k).lower() not in _DROP_KEYS}
    details["elapsed_ms"] = e.get("elapsed_ms")
    msg = e.get("label") or e.get("reason") or e.get("error") or e.get("message") or e.get("action") or e.get("question") or ""
    return {"id": event_id(task_id, seq), "task_id": task_id, "event_type": str(e.get("type", "event")).lower()[:60],
            "message": _clean(str(msg), 400), "details": details, "seq": seq, "created_at": _iso(e.get("ts"))}


class PersistenceManager:
    def __init__(self) -> None:
        self.service: SupabaseService | None = None
        self.enabled = False
        self._tokens: dict[str, str] = {}              # task id -> user's access token (memory only, never persisted)
        self._owners: dict[str, str] = {}              # task id -> validated owner id
        self._outstanding: dict[str, int] = {}
        self._failed: dict[str, str] = {}              # task id -> reason of the last failure
        self._sent_seq: dict[str, int] = {}
        self._failed_ops: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._tasks: set[asyncio.Task] = set()

    # ---- configuration --------------------------------------------------------------------
    def configure(self, service: SupabaseService | None, enabled: bool, sleep=None) -> None:
        self.service, self.enabled = service, bool(enabled and service is not None)
        if sleep is not None:
            self._sleep = sleep

    def bind(self, task_id: str, user: AuthUser, token: str | None) -> None:
        if user.local:
            return
        self._owners[task_id] = user.id
        if token:
            self._tokens[task_id] = token

    def refresh_token(self, task_id: str, token: str) -> None:
        if task_id in self._owners:
            self._tokens[task_id] = token

    # ---- scheduling -----------------------------------------------------------------------
    def _spawn(self, task, name: str, op: Callable[[], Awaitable[None]]) -> None:
        tid = task.id
        self._outstanding[tid] = self._outstanding.get(tid, 0) + 1
        self._set(task, "pending", None)

        async def runner() -> None:
            ok, reason = await self._with_retry(op)
            self._outstanding[tid] = max(0, self._outstanding.get(tid, 1) - 1)
            if ok:
                if name == "final":                   # the final write is a superset of every earlier one
                    self._failed.pop(tid, None)
                    self._failed_ops = [(t, o) for t, o in self._failed_ops if t != tid]
            else:
                self._failed[tid] = reason
                if len(self._failed_ops) < MAX_FAILED_QUEUE:
                    self._failed_ops.append((tid, op))
            if self._outstanding[tid] == 0:
                if tid in self._failed:
                    self._set(task, "failed", self._failed[tid])
                else:
                    self._set(task, "saved", None)

        try:
            t = asyncio.get_running_loop().create_task(runner(), name=f"persist-{name}-{tid[:8]}")
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except RuntimeError:                              # no running loop (sync caller): cannot persist now, say so
            self._outstanding[tid] -= 1
            self._set(task, "failed", "no event loop available to persist")

    @staticmethod
    def _set(task, status: str, detail: str | None) -> None:
        task.persistence = {"status": status, "detail": detail}

    async def _with_retry(self, op: Callable[[], Awaitable[None]]) -> tuple[bool, str]:
        last = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            try:
                await op()
                return True, ""
            except SupabaseError as exc:
                last = str(exc)[:200]
                if not exc.transient or attempt == MAX_ATTEMPTS - 1:
                    break
                await self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            except Exception as exc:      # noqa: BLE001 - persistence never raises into the task
                last = f"{type(exc).__name__}"
                break
        return False, last

    # ---- hooks (called by the API / orchestrator; both are non-blocking) -------------------
    def _ready(self, task) -> tuple[bool, str | None, str | None]:
        if not self.enabled or self.service is None:
            return False, None, None
        owner = self._owners.get(task.id) or getattr(task, "owner_id", None)
        if not owner:
            return False, None, None
        return True, owner, self._tokens.get(task.id)

    def on_created(self, task) -> None:
        ok, owner, token = self._ready(task)
        if not ok:
            return
        row = self._task_row(task)

        async def op() -> None:
            await self.service.insert_task(owner, self._tokens.get(task.id, token), row)   # type: ignore[union-attr]
        self._spawn(task, "create", op)

    def on_terminal(self, task) -> None:
        """Called whenever a task reaches a terminal OR paused state.  Records the backend's final decision."""
        ok, owner, _ = self._ready(task)
        if not ok:
            return
        events = list(getattr(task, "events", None) or [])[-MAX_EVENTS:]
        sent = self._sent_seq.get(task.id, 0)
        fresh = [e for e in events if int(e.get("seq") or 0) > sent]
        rows = [event_row(task.id, e) for e in fresh]
        patch = self._task_patch(task)
        top = max([int(e.get("seq") or 0) for e in fresh], default=sent)
        ins_row = self._task_row(task)

        async def op() -> None:
            tok = self._tokens.get(task.id)
            await self.service.insert_task(owner, tok, ins_row)           # no-op if on_created already succeeded
            await self.service.update_task(owner, tok, task.id, patch)
            await self.service.insert_events(owner, tok, rows)
            self._sent_seq[task.id] = max(self._sent_seq.get(task.id, 0), top)
        self._spawn(task, "final", op)

    async def retry_failed(self) -> int:
        """Re-run queued failed writes (idempotent).  Returns how many succeeded."""
        pending, self._failed_ops = self._failed_ops, []
        done = 0
        for tid, op in pending:
            ok, reason = await self._with_retry(op)
            if ok:
                done += 1
                self._failed.pop(tid, None)
            else:
                self._failed_ops.append((tid, op))
        return done

    async def drain(self) -> None:
        """Wait for in-flight writes (tests / shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---- row builders ---------------------------------------------------------------------
    @staticmethod
    def _task_row(task) -> dict:
        st = task.status.value if hasattr(task.status, "value") else str(task.status)
        return {"id": task.id, "instruction": scrub(task.instruction, 4000) or "(empty)",
                "status": st if st in _STATUS_OK else "failed", "provider": task.provider_used,
                "verification_status": task.verification_status, "started_at": _iso(task.created_at),
                "created_at": _iso(task.created_at)}

    @staticmethod
    def _task_patch(task) -> dict:
        st = task.status.value if hasattr(task.status, "value") else str(task.status)
        summary = task.result or task.message or task.error or ""
        patch = {"status": st if st in _STATUS_OK else "failed", "provider": task.provider_used,
                 "result_summary": scrub(summary, 500) or None,
                 "verification_status": task.verification_status}
        if task.finished_at:
            patch["finished_at"] = _iso(task.finished_at)
        return patch


persistence = PersistenceManager()


def configure_from_settings(settings: Any = None, transport=None) -> PersistenceManager:
    """Create the process-wide service from settings.  Disabled (a no-op) unless SUPABASE_ENABLED and configured."""
    if settings is None:
        from config import settings as _s
        settings = _s
    if not settings.SUPABASE_ENABLED:
        persistence.configure(None, False)
        return persistence
    svc = SupabaseService(settings.SUPABASE_URL, settings.SUPABASE_PUBLISHABLE_KEY, settings.SUPABASE_SECRET_KEY, transport=transport,
                          timeout_s=settings.SUPABASE_TIMEOUT_SECONDS, cache_ttl_s=settings.SUPABASE_TOKEN_CACHE_SECONDS)
    problems = svc.problems()
    if problems:
        logger.error("SUPABASE_ENABLED=true but Supabase is not usable: %s", "; ".join(problems))
    persistence.configure(svc, True)
    return persistence
