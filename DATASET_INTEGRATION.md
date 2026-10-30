# NEXA Dataset Integration — Phase 1

## What was added

- `backend/data/task_dataset/tasks.jsonl`: 300 synthetic browser-automation test specifications.
- `backend/data/task_dataset/manifest.json`: version, record count, and provenance.
- `backend/data/task_dataset/task.schema.json`: JSON Schema for records.
- `backend/dataset/catalog.py`: validates, loads, filters, and ranks examples.
- Four read-only dataset endpoints:
  - `GET /api/dataset/stats`
  - `GET /api/dataset/tasks?category=recovery&site=Wikipedia&q=heading&limit=20&offset=0`
  - `GET /api/dataset/tasks/{task_id}`
  - `GET /api/dataset/retrieve?q=search%20for%20Python&limit=5`
- Unit and API smoke tests.

## Important architecture boundary

These dataset records are **test specifications**, not trusted executable plans. The abstract `planned_steps` are not sent to Playwright as commands. Execution must continue through NEXA's existing planner, DOM resolver, Playwright executor, and verifier. This prevents synthetic examples or imported task text from becoming arbitrary browser instructions.

The existing workflow memory remains a separate system. It can replay only its already-approved workflow format and applies its existing verification-version and failure-count checks. Dataset entries are not automatically promoted into workflow memory.

## Run locally

From `backend/` with the project's Python dependencies installed:

```bash
pytest -q tests/test_dataset_catalog.py tests/test_dataset_api.py tests/test_adaptive_units.py tests/test_goals.py tests/test_regressions.py
uvicorn main:app --reload
```

Then open these endpoints in a browser or API client:

- `http://127.0.0.1:8000/api/dataset/stats`
- `http://127.0.0.1:8000/api/dataset/tasks?limit=10`
- `http://127.0.0.1:8000/api/dataset/tasks/NEXA-DATA-0001`
- `http://127.0.0.1:8000/api/dataset/retrieve?q=Python%20tutorials`

## Current validation status

- Dataset catalog, existing non-browser unit/regression tests, and dataset API smoke tests passed in this workspace.
- The unfiltered project test collection currently fails before running tests because `backend/tests/test_recovery_round4.py` imports `split_type_clause` from `agent.goals`, but that symbol is not present in the supplied project. This is an existing test/code mismatch and was not silently changed as part of the dataset work.
- The 300 task records are synthetic and marked `live_validated: false`. The API being healthy does not mean the real websites have been tested. Next, execute a small representative subset against local Playwright fixtures and capture real traces/screenshots before scaling the dataset.

## Next phase

1. Resolve the existing test collection mismatch or confirm whether that test is stale.
2. Build a fixture-backed dataset runner that feeds task instructions into the existing task API, records observed outcomes, and keeps expected criteria separate from observed results.
3. Add screenshot capture during those runs; do not create fake screenshots that pretend to represent live pages.
4. Promote only reviewed, successfully verified execution traces into workflow memory.
5. Add a frontend Dataset panel after the API/data contract stabilizes.
6. Expand beyond 300 cases based on coverage gaps and measured failure modes.

## Phase 2 — Automatic dataset verification

A standard-library runner is now included at `backend/scripts/verify_dataset.py`.

### Validate all records without opening a browser

```bash
cd backend
python scripts/verify_dataset.py
```

This checks all records for required fields, unique IDs, absolute HTTP(S) start URLs, allowlist coverage, and valid planned-step structure. It is local-only and does not consume AI credits.

### Run a small real-browser smoke test

Start the NEXA backend first, then run from `backend/`:

```bash
python scripts/verify_dataset.py --execute --limit 3 --site Wikipedia
```

`--execute` is required to launch actual tasks. The initial runner caps live batches at 10 tasks and only selects read-oriented task types. It reports actual backend status, final URL/title, action counts, errors, and screenshots when available. It conservatively marks a task live-verified only when status is `success`, a title and final URL are present, and the URL is on that record's allowed domain. All outcomes remain in the report, including failures and blocked pages.

Report path: `backend/data/task_dataset/verification_runs/latest_report.json`.

See `backend/data/task_dataset/VERIFICATION_GUIDE.md` for commands and output details. Source records remain synthetic and are never silently promoted to workflow memory.
