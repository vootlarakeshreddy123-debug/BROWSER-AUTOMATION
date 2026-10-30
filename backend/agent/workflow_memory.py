"""
Local workflow memory for NEXA.

Stores successful semantic browser action sequences keyed by a normalized
copy of the user's instruction. Replays use the normal live DOM resolver, so
we do not record blind mouse coordinates.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent.workflow_memory")
_MEMORY_PATH = Path(__file__).resolve().parents[1] / "data" / "workflows.json"
_LOCK = threading.Lock()
_MAX_WORKFLOWS = 500
_MEMORY_VERSION = 3
_MAX_FAILURES_BEFORE_INVALID = 2


def normalize_instruction(instruction: str) -> str:
    return re.sub(r"\s+", " ", instruction.strip().lower())


def _is_information_task(instruction: str) -> bool:
    text = normalize_instruction(instruction)
    return bool(re.search(
        r"\b(?:tell|give|show|return|extract|read|report|what|which|who|when|where|how)\b.*\b(?:sentence|paragraph|section|heading|title|text|answer|value|price|author|count|number|content)\b|"
        r"\bfirst\s+(?:sentence|paragraph|heading|result|item)\b",
        text,
    ))


def _read() -> dict[str, dict[str, Any]]:
    if not _MEMORY_PATH.exists():
        return {}
    try:
        raw = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Workflow memory could not be read; starting empty.")
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _MEMORY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(_MEMORY_PATH)


def get_workflow(instruction: str) -> list[dict[str, Any]] | None:
    key = normalize_instruction(instruction)
    if not key:
        return None
    with _LOCK:
        record = _read().get(key)
    if not isinstance(record, dict):
        return None
    # Legacy workflows were produced by the old one-action/heuristic engine.
    # Do not replay them; only workflows created by the repaired engine are eligible.
    if int(record.get("memory_version", 0) or 0) < _MEMORY_VERSION:
        return None
    if bool(record.get("invalidated", False)):
        return None
    if int(record.get("failure_count", 0) or 0) >= _MAX_FAILURES_BEFORE_INVALID:
        return None
    # Information workflows from memory version 2 were saved before the
    # semantic extraction verifier was strict enough to reject arbitrary UI
    # labels. Do not replay those records; they will be relearned only after
    # the new verifier has independently proved the requested answer.
    if _is_information_task(instruction) and int(record.get("verification_version", 0) or 0) < 3:
        return None
    actions = record.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    return [dict(action) for action in actions if isinstance(action, dict)]


def save_workflow(instruction: str, actions: list[dict[str, Any]], *, verified_extraction: bool = False) -> None:
    key = normalize_instruction(instruction)
    cleaned = [dict(action) for action in actions if isinstance(action, dict)]
    if not key or not cleaned:
        return
    now = time.time()
    with _LOCK:
        data = _read()
        previous = data.get(key, {})
        if not isinstance(previous, dict):
            previous = {}
        data[key] = {
            "memory_version": _MEMORY_VERSION,
            "verification_version": 3 if verified_extraction else int(previous.get("verification_version", 0) or 0),
            "instruction": instruction.strip(),
            "normalized_instruction": key,
            "actions": cleaned,
            "created_at": previous.get("created_at", now),
            "updated_at": now,
            "success_count": int(previous.get("success_count", 0)) + 1,
            "failure_count": 0,
            "invalidated": False,
            "last_used_at": previous.get("last_used_at"),
        }
        if len(data) > _MAX_WORKFLOWS:
            ordered = sorted(
                data.items(),
                key=lambda item: float(item[1].get("updated_at", 0)) if isinstance(item[1], dict) else 0,
            )
            for old_key, _ in ordered[: len(data) - _MAX_WORKFLOWS]:
                data.pop(old_key, None)
        try:
            _write(data)
        except OSError:
            logger.exception("Could not persist workflow memory.")


def mark_replayed(instruction: str) -> None:
    key = normalize_instruction(instruction)
    if not key:
        return
    with _LOCK:
        data = _read()
        record = data.get(key)
        if not isinstance(record, dict):
            return
        record["last_used_at"] = time.time()
        record["replay_count"] = int(record.get("replay_count", 0)) + 1
        try:
            _write(data)
        except OSError:
            logger.exception("Could not update workflow replay statistics.")


def mark_failed(instruction: str, *, invalidate: bool = False) -> None:
    key = normalize_instruction(instruction)
    if not key:
        return
    with _LOCK:
        data = _read()
        record = data.get(key)
        if not isinstance(record, dict):
            return
        record["failure_count"] = int(record.get("failure_count", 0) or 0) + 1
        record["last_failed_at"] = time.time()
        if invalidate or record["failure_count"] >= _MAX_FAILURES_BEFORE_INVALID:
            record["invalidated"] = True
        try:
            _write(data)
        except OSError:
            logger.exception("Could not update workflow failure statistics.")


def clear_legacy_workflows() -> int:
    with _LOCK:
        data = _read()
        removed = 0
        for key in list(data):
            record = data.get(key)
            if not isinstance(record, dict) or int(record.get("memory_version", 0) or 0) < _MEMORY_VERSION:
                data.pop(key, None)
                removed += 1
        if removed:
            try:
                _write(data)
            except OSError:
                logger.exception("Could not clear legacy workflow memory.")
        return removed
