from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from agent.orchestrator import forget_paused_task, respond_to_task, run_task
from browser.manager import browser_manager
from database.models import TaskRecord, TaskStatus
from database.repository import task_repository
from models.schemas import CreateTaskRequest, CreateTaskResponse, RespondRequest, TaskResponse
from services.ws_manager import ws_manager
from dataset.catalog import get_catalog
from api.deps import AuthContext, current_auth
from services.auth import authenticate, owns
from services.persistence import persistence
from services.supabase_service import AuthError, SupabaseError

logger = logging.getLogger("api.tasks")
router = APIRouter(prefix="/api", tags=["tasks"])
_RUNNING: dict[str, asyncio.Task] = {}


def _to_response(task: TaskRecord, include_events: bool = True) -> TaskResponse:
    data = task.model_dump()
    if not include_events:
        data["events"] = []          # history list stays small; fetch /tasks/{id} for events
    return TaskResponse(**data)


def _owned(task_id: str, ctx: AuthContext) -> TaskRecord:
    """The task, or 404.  A task that belongs to someone else is indistinguishable from one that does not exist."""
    task = task_repository.get(task_id)
    if task is None or not owns(task, ctx.user):
        raise HTTPException(404, "task not found")
    if ctx.token:
        persistence.refresh_token(task_id, ctx.token)          # a refreshed session keeps later background writes valid
    return task


def _cleanup(task_id: str) -> None:
    _RUNNING.pop(task_id, None)


@router.post("/tasks", response_model=CreateTaskResponse)
async def create_task(body: CreateTaskRequest, ctx: AuthContext = Depends(current_auth)) -> CreateTaskResponse:
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(400, "instruction must not be empty")
    selection = None
    if body.provider:
        from providers import registry
        selection = registry.normalise_selection(body.provider)
        if selection not in registry.SELECTABLE:
            raise HTTPException(400, f"unknown provider '{body.provider}'; choose one of {', '.join(registry.SELECTABLE)}")
        if selection != "auto":
            problems = registry.config_problems(selection)
            if problems:
                raise HTTPException(400, f"{registry.LABELS[selection]} cannot be used: {'; '.join(problems)}")
    # owner = the VALIDATED token's user. Nothing in the request body can set it.
    task = task_repository.create(TaskRecord(instruction=instruction, provider=selection,
                                             owner_id=None if ctx.user.local else ctx.user.id))
    persistence.bind(task.id, ctx.user, ctx.token)
    persistence.on_created(task)
    return CreateTaskResponse(task_id=task.id, status=task.status)


@router.post("/tasks/{task_id}/run", response_model=TaskResponse)
async def run_task_endpoint(task_id: str, ctx: AuthContext = Depends(current_auth)) -> TaskResponse:
    task = _owned(task_id, ctx)
    if task.status != TaskStatus.CREATED:
        raise HTTPException(409, f"task already {task.status.value}")

    # Start immediately. Waiting for a WebSocket here made the UI appear
    # stuck before the browser could even create its new tab. The client has
    # HTTP polling as a fallback if the socket arrives a moment later.
    runner = asyncio.create_task(run_task(task), name=f"browser-task-{task_id}")
    _RUNNING[task_id] = runner
    runner.add_done_callback(lambda _: _cleanup(task_id))
    return _to_response(task)


@router.post("/tasks/{task_id}/respond", response_model=TaskResponse)
async def respond_endpoint(task_id: str, body: RespondRequest, ctx: AuthContext = Depends(current_auth)) -> TaskResponse:
    """Answer a paused task: ``approve`` true/false for a confirmation, ``answer`` (or ``choice``) for a question."""
    task = _owned(task_id, ctx)
    if task.status not in {TaskStatus.AWAITING_CONFIRMATION, TaskStatus.NEEDS_INPUT}:
        raise HTTPException(409, f"task is {task.status.value}, it is not waiting for an answer")
    try:
        outcome = respond_to_task(task, approve=body.approve, answer=body.answer or body.choice)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    task_repository.update(task)
    if outcome == "resume":
        runner = asyncio.create_task(run_task(task), name=f"resume-task-{task_id}")
        _RUNNING[task_id] = runner
        runner.add_done_callback(lambda _: _cleanup(task_id))
    else:
        await ws_manager.send(task_id, {"type": "result", "status": task.status.value, "error": task.error, "message": task.message})
    return _to_response(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: str, ctx: AuthContext = Depends(current_auth)) -> TaskResponse:
    task = _owned(task_id, ctx)

    runner = _RUNNING.get(task_id)
    if task.status == TaskStatus.RUNNING and runner is not None:
        task.mark_finished(TaskStatus.CANCELLED, error="Cancelled by user.")
        task_repository.update(task)
        runner.cancel()
        await ws_manager.send(task_id, {"type": "result", "status": task.status.value, "error": task.error})
    elif task.status in {TaskStatus.CREATED, TaskStatus.AWAITING_CONFIRMATION, TaskStatus.NEEDS_INPUT}:
        task.pending = None
        forget_paused_task(task_id)
        task.mark_finished(TaskStatus.CANCELLED, error="Cancelled by user.")
        task_repository.update(task)
        await ws_manager.send(task_id, {"type": "result", "status": task.status.value, "error": task.error})

    return _to_response(task)


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
async def resume_task_endpoint(task_id: str, ctx: AuthContext = Depends(current_auth)) -> TaskResponse:
    """Resume a timed-out/failed task from the currently visible NEXA Chrome tab."""
    task = _owned(task_id, ctx)
    if task.status not in {TaskStatus.TIMEOUT, TaskStatus.FAILED}:
        raise HTTPException(409, f"task cannot be resumed from {task.status.value}")
    if browser_manager.current_page is None:
        raise HTTPException(409, "no live Chrome tab is available to resume")
    task.status = TaskStatus.CREATED
    task.finished_at = None
    task.duration_ms = None
    task.error = None
    task.message = None
    task.result = None
    task.resume_existing_browser = True
    task_repository.update(task)
    runner = asyncio.create_task(run_task(task), name=f"resume-browser-task-{task_id}")
    _RUNNING[task_id] = runner
    runner.add_done_callback(lambda _: _cleanup(task_id))
    return _to_response(task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, ctx: AuthContext = Depends(current_auth)) -> TaskResponse:
    task = _owned(task_id, ctx)
    return _to_response(task)


@router.get("/history", response_model=list[TaskResponse])
async def history(limit: int = 50, ctx: AuthContext = Depends(current_auth)) -> list[TaskResponse]:
    return [_to_response(t, include_events=False)
            for t in task_repository.list_recent(limit, owner_id=ctx.user.id, scoped=ctx.scoped)]


@router.get("/browser/state")
async def browser_state(ctx: AuthContext = Depends(current_auth)) -> dict:
    page = browser_manager.current_page
    title = None
    url = None
    if page is not None:
        try:
            url = page.url
            title = await page.title()
        except Exception:
            page = None
    return {
        "ready": browser_manager.is_ready,
        "live_tab_open": page is not None,
        "url": url,
        "title": title,
    }


@router.post("/browser/focus")
async def focus_browser(ctx: AuthContext = Depends(current_auth)) -> dict:
    focused = await browser_manager.focus_current_page()
    return {"focused": focused, "live_tab_open": focused}


@router.websocket("/ws/tasks/{task_id}")
async def task_ws(websocket: WebSocket, task_id: str) -> None:
    if persistence.enabled:
        # The token is sent as the FIRST MESSAGE ({"type":"auth","token":...}), never in the URL (URLs end up in logs).
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
            if not isinstance(first, dict) or first.get("type") != "auth":
                raise AuthError("the first message must be an auth message")
            user, _ = await authenticate(f"Bearer {first.get('token')}", persistence.service, True)
            task = task_repository.get(task_id)
            if task is None or not owns(task, user):
                raise AuthError("not your task", 403)
        except (AuthError, asyncio.TimeoutError, ValueError, WebSocketDisconnect, Exception) as exc:  # noqa: BLE001
            code = 4403 if isinstance(exc, AuthError) and exc.status == 403 else 4401
            try:
                await websocket.close(code=code)
            except Exception:
                pass
            return
        ws_manager.attach(task_id, websocket)
    else:
        await ws_manager.connect(task_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket for task %s closed.", task_id)
    finally:
        ws_manager.disconnect(task_id)


# ---------------------------------------------------------------- persisted history (Supabase, RLS-scoped to the caller)
def _need_persistence() -> None:
    if not persistence.enabled or persistence.service is None:
        raise HTTPException(503, "persistent history is not enabled (set SUPABASE_ENABLED=true and configure Supabase)")


async def _persisted(call):
    try:
        return await call
    except SupabaseError as exc:
        raise HTTPException(502 if exc.transient else 400, "history storage error: " + str(exc)[:160])


@router.get("/persisted/tasks")
async def persisted_tasks(limit: int = 50, ctx: AuthContext = Depends(current_auth)) -> list[dict]:
    _need_persistence()
    return await _persisted(persistence.service.list_tasks(ctx.token or "", limit))       # type: ignore[union-attr]


@router.get("/persisted/tasks/{task_id}")
async def persisted_task(task_id: str, ctx: AuthContext = Depends(current_auth)) -> dict:
    _need_persistence()
    row = await _persisted(persistence.service.get_task(ctx.token or "", task_id))        # type: ignore[union-attr]
    if row is None:
        raise HTTPException(404, "task not found")
    return row


@router.get("/persisted/tasks/{task_id}/events")
async def persisted_events(task_id: str, ctx: AuthContext = Depends(current_auth)) -> list[dict]:
    _need_persistence()
    return await _persisted(persistence.service.list_events(ctx.token or "", task_id))    # type: ignore[union-attr]


@router.post("/persisted/retry")
async def persisted_retry(ctx: AuthContext = Depends(current_auth)) -> dict:
    """Re-send history writes that failed during an outage (idempotent: nothing is duplicated)."""
    _need_persistence()
    return {"retried_ok": await persistence.retry_failed()}


@router.get("/dataset/stats")
async def dataset_stats() -> dict:
    """Return summary statistics for the bundled test-case dataset."""
    return get_catalog().stats()


@router.get("/dataset/tasks")
async def dataset_tasks(
    category: str | None = None,
    site: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List test cases from the dataset; this endpoint does not execute them."""
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(400, "offset must be non-negative")
    return get_catalog().list_tasks(category=category, site=site, query=q, limit=limit, offset=offset)


@router.get("/dataset/tasks/{task_id}")
async def dataset_task(task_id: str) -> dict:
    """Return one dataset test case by its stable ID."""
    item = get_catalog().get_task(task_id)
    if item is None:
        raise HTTPException(404, "dataset task not found")
    return item


@router.get("/dataset/retrieve")
async def dataset_retrieve(q: str, limit: int = 5) -> dict:
    """Return similar example cases for inspection; does not execute/replay them."""
    if not q.strip():
        raise HTTPException(400, "q must not be empty")
    if limit < 1 or limit > 20:
        raise HTTPException(400, "limit must be between 1 and 20")
    return {"query": q, "matches": get_catalog().retrieve(q, limit=limit),
            "note": "Matches are synthetic examples, not verified workflows. Validate before reuse."}
