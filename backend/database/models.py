"""
Task record + lifecycle.

Persistence is deliberately a thin, swappable layer (see repository.py).
Per the performance requirement ("avoid unnecessary database writes"), a
task is written twice at most: once on creation, once on terminal state.
No per-step writes.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    AWAITING_CONFIRMATION = "awaiting_confirmation"   # paused before a consequential action (send / publish / ...)
    NEEDS_INPUT = "needs_input"                       # paused to ask the user a clarification question


class TaskRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))      # a real UUID: it is also the primary key of the persisted row
    instruction: str
    status: TaskStatus = TaskStatus.CREATED
    created_at: float = Field(default_factory=time.time)
    finished_at: Optional[float] = None
    duration_ms: Optional[int] = None

    plan_source: Optional[str] = None          # "fast_path" | "ai" | "fallback"
    actions_count: int = 0
    retries: int = 0

    final_url: Optional[str] = None
    final_title: Optional[str] = None
    screenshot_base64: Optional[str] = None
    message: Optional[str] = None               # short, human-facing result message
    result: Optional[str] = None                 # extracted/user-requested answer
    error: Optional[str] = None
    resume_existing_browser: bool = False
    # AI provider selection for this task ("auto" | gemini | ollama | openai | claude | grok) and the provider that
    # actually produced plans (None when no AI was needed).
    provider: Optional[str] = None
    provider_used: Optional[str] = None
    outcome: Optional[str] = None               # success | partial | blocked | failed | cancelled | awaiting_user
    # Set while the task is paused (AWAITING_CONFIRMATION / NEEDS_INPUT): what is being asked of the user.
    pending: Optional[dict[str, Any]] = None
    understanding: Optional[dict[str, Any]] = None
    # Authoritative requirement contract + the single verification verdict (agent/contract.py, agent/events.py)
    contract: Optional[dict[str, Any]] = None
    contract_state: Optional[str] = None        # PENDING|RUNNING|ACTION_SUCCEEDED|VERIFIED_COMPLETE|BLOCKED|FAILED|CANCELLED
    verification_status: Optional[str] = None
    # Owner = the Supabase user id taken from a VALIDATED access token (never from the request body); None in local mode.
    owner_id: Optional[str] = None
    # Honest persistence state: {"status": disabled|pending|saved|failed, "detail": str|None}
    persistence: dict[str, Any] = Field(default_factory=lambda: {"status": "disabled", "detail": None})
    # Structured execution events + statistics recorded by the orchestrator
    # (see agent/events.py). Held in memory only; polled by the UI as a
    # fallback for the WebSocket event stream.
    events: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING

    def mark_paused(self, status: "TaskStatus", pending: dict) -> None:
        self.status = status
        self.pending = pending
        self.finished_at = None
        self.duration_ms = None

    def mark_finished(self, status: TaskStatus, **fields: Any) -> None:
        self.status = status
        self.finished_at = time.time()
        self.duration_ms = int((self.finished_at - self.created_at) * 1000)
        for k, v in fields.items():
            setattr(self, k, v)
