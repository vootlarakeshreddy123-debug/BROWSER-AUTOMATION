"""Validate NEXA dataset records and optionally smoke-test tasks against live NEXA.

Default mode is local-only and never opens a browser. Use --execute to opt into
real browser work against a running backend. Results are written separately;
the synthetic source records are never silently promoted to verified workflows.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
DATASET_FILE = BACKEND_DIR / "data" / "task_dataset" / "tasks.jsonl"
REPORT_DIR = BACKEND_DIR / "data" / "task_dataset" / "verification_runs"
SAFE_READ_ONLY_TYPES = {
    "search_open", "extract_title", "extract_heading", "extract_url",
    "summarize", "verify_relevance", "metadata",
}
TERMINAL_STATUSES = {"success", "failed", "cancelled", "timeout"}


def _domain_allowed(url: str, domains: list[str]) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return bool(host) and any(
        host == domain.lower().strip().lstrip(".")
        or host.endswith("." + domain.lower().strip().lstrip("."))
        for domain in domains
    )


def load_and_validate(path: Path = DATASET_FILE) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return valid records and actionable errors; check duplicates across the file."""
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not path.is_file():
        return [], [{"line": 0, "error": f"Dataset file not found: {path}"}]

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("record must be a JSON object")
            for key in ("task_id", "instruction", "category", "task_type", "target", "planned_steps", "success_criteria"):
                if key not in record:
                    raise ValueError(f"missing required field: {key}")
            task_id = record["task_id"]
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("task_id must be a non-empty string")
            if task_id in seen:
                raise ValueError(f"duplicate task_id: {task_id}")
            seen.add(task_id)
            if not isinstance(record["instruction"], str) or not record["instruction"].strip():
                raise ValueError("instruction must be a non-empty string")
            target = record["target"]
            if not isinstance(target, dict):
                raise ValueError("target must be an object")
            start_url = target.get("start_url")
            parsed = urlparse(start_url) if isinstance(start_url, str) else None
            if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("target.start_url must be an absolute HTTP(S) URL")
            domains = target.get("allowed_domains")
            if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d.strip() for d in domains):
                raise ValueError("target.allowed_domains must be a non-empty list of strings")
            if not _domain_allowed(start_url, domains):
                raise ValueError("target.start_url host is not covered by allowed_domains")
            steps = record["planned_steps"]
            if not isinstance(steps, list) or not steps:
                raise ValueError("planned_steps must be a non-empty list")
            if not all(isinstance(step, dict) and isinstance(step.get("action"), str) and step["action"].strip() for step in steps):
                raise ValueError("every planned step must be an object with a non-empty action")
            if not isinstance(record["success_criteria"], list):
                raise ValueError("success_criteria must be a list")
            valid.append(record)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append({"line": line_no, "error": str(exc)})
    return valid, errors


def _request_json(base_url: str, path: str, *, method: str = "GET", body: dict | None = None, timeout: float = 30) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not reach NEXA at {url}: {exc}") from exc


def _save_screenshot(encoded: str | None, output_path: Path) -> str | None:
    if not encoded:
        return None
    # The backend currently returns a base64-encoded PNG/JPEG data payload.
    raw = encoded.split(",", 1)[1] if encoded.startswith("data:") and "," in encoded else encoded
    try:
        content = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if not content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return str(output_path)


def execute_batch(records: list[dict[str, Any]], base_url: str, timeout_seconds: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    screenshots_dir = REPORT_DIR / f"screenshots_{run_stamp}"
    for index, record in enumerate(records, 1):
        item: dict[str, Any] = {
            "dataset_task_id": record["task_id"],
            "instruction": record["instruction"],
            "target_site": record["target"].get("site_name"),
            "status": "not_started",
            "live_verified": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        print(f"[{index}/{len(records)}] {record['task_id']} — {record['instruction']}")
        try:
            created = _request_json(base_url, "/api/tasks", method="POST", body={"instruction": record["instruction"]})
            task_id = created.get("task_id")
            if not task_id:
                raise RuntimeError(f"Backend did not return task_id: {created}")
            item["nexa_task_id"] = task_id
            _request_json(base_url, f"/api/tasks/{task_id}/run", method="POST", timeout=30)
            deadline = time.monotonic() + timeout_seconds
            current: dict[str, Any] = {}
            while time.monotonic() < deadline:
                current = _request_json(base_url, f"/api/tasks/{task_id}", timeout=30)
                status = str(current.get("status", "unknown")).lower()
                if status in TERMINAL_STATUSES:
                    break
                time.sleep(2)
            else:
                item["status"] = "runner_timeout"
                item["error"] = f"Task did not reach a terminal status within {timeout_seconds} seconds."
                results.append(item)
                continue

            status = str(current.get("status", "unknown")).lower()
            final_url = current.get("final_url")
            final_title = current.get("final_title")
            allowed_domains = record["target"].get("allowed_domains", [])
            on_allowed_domain = bool(final_url and _domain_allowed(final_url, allowed_domains))
            item.update({
                "status": status,
                "final_url": final_url,
                "final_title": final_title,
                "result": current.get("result"),
                "message": current.get("message"),
                "error": current.get("error"),
                "plan_source": current.get("plan_source"),
                "actions_count": current.get("actions_count", 0),
                "retries": current.get("retries", 0),
                "final_url_matches_allowed_domain": on_allowed_domain,
            })
            screenshot_path = screenshots_dir / f"{record['task_id']}.png"
            item["screenshot_path"] = _save_screenshot(current.get("screenshot_base64"), screenshot_path)
            # Conservative verification: success status + observed title and URL + URL is in the record's allowlist.
            item["live_verified"] = status == "success" and bool(final_title) and on_allowed_domain
        except Exception as exc:  # record one failure without abandoning the batch
            item["status"] = "runner_error"
            item["error"] = str(exc)
        item["finished_at"] = datetime.now(timezone.utc).isoformat()
        results.append(item)
        print(f"    -> {item['status']} | verified={item['live_verified']} | url={item.get('final_url') or '-'}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET_FILE, help="Path to tasks.jsonl")
    parser.add_argument("--execute", action="store_true", help="Opt in to real browser tasks against a running NEXA backend")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="NEXA backend URL (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=5, help="Maximum live tasks to run (1-10; default: 5)")
    parser.add_argument("--site", help="Limit live tests to a site name, e.g. Wikipedia")
    parser.add_argument("--category", help="Limit live tests to a dataset category")
    parser.add_argument("--timeout", type=int, default=180, help="Per-task timeout in seconds")
    parser.add_argument("--report", type=Path, help="Report output path (defaults to verification_runs/latest_report.json)")
    args = parser.parse_args()

    if args.limit < 1 or args.limit > 10:
        parser.error("--limit must be between 1 and 10 for this initial smoke-test runner")
    if args.timeout < 10 or args.timeout > 900:
        parser.error("--timeout must be between 10 and 900 seconds")

    valid, errors = load_and_validate(args.dataset)
    print(f"Dataset: {args.dataset}")
    print(f"Valid records: {len(valid)} | validation errors: {len(errors)}")
    if errors:
        for error in errors[:30]:
            print(f"  line {error['line']}: {error['error']}")
        if len(errors) > 30:
            print(f"  ... and {len(errors) - 30} more")
    if not args.execute:
        print("LOCAL-ONLY CHECK: no browser tasks were run. Add --execute to opt into live tests.")
        return 1 if errors else 0

    candidates = [r for r in valid if r.get("task_type") in SAFE_READ_ONLY_TYPES]
    if args.site:
        candidates = [r for r in candidates if str(r.get("target", {}).get("site_name", "")).casefold() == args.site.casefold()]
    if args.category:
        candidates = [r for r in candidates if str(r.get("category", "")).casefold() == args.category.casefold()]
    candidates = candidates[:args.limit]
    if not candidates:
        print("No safe read-only tasks match these filters.")
        return 2

    try:
        health = _request_json(args.base_url, "/api/health", timeout=10)
        print(f"Backend health: {health.get('status', 'unknown')} | browser_ready={health.get('browser_ready', 'unknown')} | ai_configured={health.get('ai_configured', 'unknown')}")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print("Start the NEXA backend first, then retry.", file=sys.stderr)
        return 2

    print(f"LIVE MODE: running {len(candidates)} read-only task(s). Each task may use your configured AI provider and browser.")
    results = execute_batch(candidates, args.base_url, args.timeout)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_file": str(args.dataset),
        "dataset_records_valid": len(valid),
        "validation_errors": errors,
        "live_mode": True,
        "backend_url": args.base_url,
        "attempted": len(results),
        "live_verified": sum(1 for item in results if item.get("live_verified")),
        "status_counts": {},
        "results": results,
        "note": "Only successful tasks with a visible title and final URL on the task's allowed domain are marked live_verified. Source templates remain synthetic until separately reviewed.",
    }
    for result in results:
        status = result.get("status", "unknown")
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
    report_path = args.report or (REPORT_DIR / "latest_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport saved: {report_path}")
    print(f"Live verified: {summary['live_verified']}/{summary['attempted']}")
    print("A failed/blocked task is still useful test evidence; it is not counted as verified.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
