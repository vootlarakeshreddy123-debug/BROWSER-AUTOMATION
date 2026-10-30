# NEXA Dataset Verifier — Test Report

## Local validation

Command: `cd backend && python scripts/verify_dataset.py`

Result: **300 valid records, 0 validation errors**. No browser was launched in this mode.

## Automated tests

Command:

```bash
pytest -q tests/test_dataset_verifier.py tests/test_dataset_catalog.py tests/test_dataset_api.py tests/test_adaptive_units.py tests/test_goals.py tests/test_regressions.py
```

Result: **87 passed, 2 warnings**. The warnings are existing FastAPI `on_event` deprecation notices.

Compilation check: `python -m compileall -q scripts dataset api main.py` passed.

## Live execution status

No live browser tasks were run in this workspace. To run a small real test against your local NEXA backend, follow `backend/data/task_dataset/VERIFICATION_GUIDE.md` and explicitly pass `--execute`.
