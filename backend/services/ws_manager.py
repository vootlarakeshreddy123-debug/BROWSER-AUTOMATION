"""
WebSocket manager.

Deliberately emits a *small* number of coarse-grained events per task
(connected -> running -> result), not a verbose internal log. That matches
the product requirement: the UI shows the operation and the result, not a
stream of "validation started" / "retrying" noise.

Also solves the connect-before-start race: `wait_for_connection` lets the
task runner block briefly until the frontend's socket is attached before
any events are emitted, so nothing is missed.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class WSManager:
    def __init__(self) -> None:
        self._sockets: dict[str, WebSocket] = {}
        self._ready: dict[str, asyncio.Event] = {}

    def _event_for(self, task_id: str) -> asyncio.Event:
        return self._ready.setdefault(task_id, asyncio.Event())

    async def connect(self, task_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets[task_id] = websocket
        self._event_for(task_id).set()

    def attach(self, task_id: str, websocket: WebSocket) -> None:
        """Register an ALREADY accepted (and authenticated) socket."""
        self._sockets[task_id] = websocket
        self._event_for(task_id).set()

    def disconnect(self, task_id: str) -> None:
        self._sockets.pop(task_id, None)
        self._ready.pop(task_id, None)

    async def wait_for_connection(self, task_id: str, timeout: float = 3.0) -> bool:
        """Give the frontend a brief window to attach its socket before we
        start emitting events, without blocking the task indefinitely if the
        frontend never connects (e.g. someone calling the HTTP API directly)."""
        try:
            await asyncio.wait_for(self._event_for(task_id).wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def send(self, task_id: str, payload: dict[str, Any]) -> None:
        ws = self._sockets.get(task_id)
        if ws is None:
            return
        try:
            await ws.send_json(payload)
        except Exception:
            self.disconnect(task_id)


ws_manager = WSManager()
