"""Read-only loader and lightweight retriever for NEXA task test cases.

Dataset records describe tests; they are not executable code and are not
replayed directly as browser actions. Actual execution must go through the
existing planner, browser executor, and goal verifier.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("dataset.catalog")
DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[1] / "data" / "task_dataset"


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _safe_start_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


class DatasetCatalog:
    """Validated in-memory catalog loaded from tasks.jsonl."""

    def __init__(self, dataset_dir: Path | str | None = None) -> None:
        self.dataset_dir = Path(dataset_dir) if dataset_dir else DEFAULT_DATASET_DIR
        self.records: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.load_errors: list[str] = []
        self._lock = threading.RLock()
        self.reload()

    def reload(self) -> dict[str, Any]:
        path = self.dataset_dir / "tasks.jsonl"
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        if not path.exists():
            errors.append(f"Dataset file not found: {path}")
        else:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                lines = []
                errors.append(f"Could not read dataset file: {exc}")
            for line_no, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    self._validate_record(item)
                    task_id = item["task_id"]
                    if task_id in seen:
                        raise ValueError(f"duplicate task_id: {task_id}")
                    seen.add(task_id)
                    records.append(item)
                except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
                    errors.append(f"line {line_no}: {exc}")
        with self._lock:
            self.records = records
            self.by_id = {item["task_id"]: item for item in records}
            self.load_errors = errors
        return self.stats()

    @staticmethod
    def _validate_record(item: Any) -> None:
        if not isinstance(item, dict):
            raise TypeError("record must be a JSON object")
        required = ("task_id", "instruction", "category", "task_type", "target", "planned_steps", "success_criteria")
        for key in required:
            if key not in item:
                raise ValueError(f"missing required field: {key}")
        if not isinstance(item["task_id"], str) or not item["task_id"].strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(item["instruction"], str) or not item["instruction"].strip():
            raise ValueError("instruction must be a non-empty string")
        if not isinstance(item["target"], dict) or not _safe_start_url(item["target"].get("start_url")):
            raise ValueError("target.start_url must be an absolute HTTP(S) URL")
        domains = item["target"].get("allowed_domains")
        if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d.strip() for d in domains):
            raise ValueError("target.allowed_domains must be a non-empty list of strings")
        if not isinstance(item["planned_steps"], list) or not item["planned_steps"]:
            raise ValueError("planned_steps must be a non-empty list")
        if not all(isinstance(step, dict) and isinstance(step.get("action"), str) for step in item["planned_steps"]):
            raise ValueError("every planned step must be an object with a string action")
        if not isinstance(item["success_criteria"], list):
            raise ValueError("success_criteria must be a list")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            categories = Counter(str(r.get("category", "uncategorized")) for r in self.records)
            types = Counter(str(r.get("task_type", "unknown")) for r in self.records)
            return {
                "dataset_version": "1.0.0",
                "total_tasks": len(self.records),
                "category_counts": dict(sorted(categories.items())),
                "task_type_counts": dict(sorted(types.items())),
                "load_error_count": len(self.load_errors),
                "provenance": "synthetic_test_templates_not_live_validated",
            }

    def list_tasks(self, *, category: str | None = None, site: str | None = None,
                   query: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        cat = _normalize(category or "")
        site_query = _normalize(site or "")
        text_query = _normalize(query or "")
        with self._lock:
            selected = []
            for record in self.records:
                target = record.get("target", {})
                if cat and _normalize(record.get("category", "")) != cat:
                    continue
                if site_query and site_query not in _normalize(target.get("site_name", "")):
                    continue
                searchable = _normalize(" ".join([
                    record.get("task_id", ""), record.get("instruction", ""),
                    record.get("category", ""), record.get("task_type", ""),
                    target.get("site_name", ""), target.get("query_or_topic", ""),
                ]))
                if text_query and not all(token in searchable for token in text_query.split()):
                    continue
                selected.append(record)
        return {"total": len(selected), "offset": offset, "limit": limit,
                "items": [dict(item) for item in selected[offset:offset + limit]]}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self.by_id.get(task_id)
            return dict(item) if item else None

    def retrieve(self, instruction: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return lexical candidate examples; callers must still validate applicability."""
        query_tokens = set(_normalize(instruction).split())
        if not query_tokens:
            return []
        ranked: list[tuple[float, dict[str, Any]]] = []
        with self._lock:
            records = list(self.records)
        for item in records:
            target = item.get("target", {})
            text = _normalize(" ".join([
                item.get("instruction", ""), item.get("category", ""), item.get("task_type", ""),
                target.get("site_name", ""), target.get("query_or_topic", ""),
            ]))
            tokens = set(text.split())
            if not tokens:
                continue
            overlap = len(query_tokens & tokens)
            score = overlap / max(1, len(query_tokens))
            if _normalize(target.get("site_name", "")) in _normalize(instruction):
                score += 0.2
            if score > 0:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [{"score": round(score, 4), "task": dict(item)} for score, item in ranked[:max(1, min(limit, 20))]]


_catalog: DatasetCatalog | None = None
_catalog_lock = threading.Lock()


def get_catalog() -> DatasetCatalog:
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = DatasetCatalog()
        return _catalog
