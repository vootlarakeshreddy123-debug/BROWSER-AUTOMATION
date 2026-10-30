from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from database.models import TaskStatus


class CreateTaskRequest(BaseModel):
    instruction: str
    provider: Optional[str] = None      # auto | groq | gemini | ollama | openai | claude | grok (None = server default)


class CreateTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus


class TaskResponse(BaseModel):
    id: str
    instruction: str
    status: TaskStatus
    created_at: float
    finished_at: Optional[float] = None
    duration_ms: Optional[int] = None
    plan_source: Optional[str] = None
    actions_count: int = 0
    retries: int = 0
    final_url: Optional[str] = None
    final_title: Optional[str] = None
    screenshot_base64: Optional[str] = None
    message: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    events: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    provider: Optional[str] = None
    provider_used: Optional[str] = None
    outcome: Optional[str] = None
    pending: Optional[dict[str, Any]] = None
    understanding: Optional[dict[str, Any]] = None
    contract: Optional[dict[str, Any]] = None
    contract_state: Optional[str] = None
    verification_status: Optional[str] = None
    persistence: dict[str, Any] = {"status": "disabled", "detail": None}


class RespondRequest(BaseModel):
    """Answer to a paused task: ``approve`` for a confirmation, ``answer`` for a clarification question."""
    approve: Optional[bool] = None
    answer: Optional[str] = None
    choice: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    ai_configured: bool
    browser_ready: bool
