"""
In-memory task repository.

Kept deliberately simple and dependency-free so the project runs with zero
external services out of the box. Swap this class for a Postgres/Supabase
implementation later without touching callers - the public methods
(create/get/update/list) are the whole contract.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

from database.models import TaskRecord

_MAX_HISTORY = 200


class TaskRepository:
    def __init__(self) -> None:
        self._tasks: "OrderedDict[str, TaskRecord]" = OrderedDict()
        self._lock = threading.Lock()

    def create(self, record: TaskRecord) -> TaskRecord:
        with self._lock:
            self._tasks[record.id] = record
            while len(self._tasks) > _MAX_HISTORY:
                self._tasks.popitem(last=False)
        return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, record: TaskRecord) -> None:
        with self._lock:
            self._tasks[record.id] = record

    def list_recent(self, limit: int = 50, owner_id: Optional[str] = None, scoped: bool = False) -> list[TaskRecord]:
        """``scoped=True`` (auth enabled) returns ONLY the owner's tasks; unscoped (local single-user mode) returns all."""
        with self._lock:
            items = list(self._tasks.values())
        if scoped:
            items = [t for t in items if t.owner_id is not None and t.owner_id == owner_id]
        return list(reversed(items))[:limit]


# Single process-wide instance - fine for a local dev tool with one backend
# process. Replace with a proper DB session/connection pool for multi-worker
# deployments.
task_repository = TaskRepository()
