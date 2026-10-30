# TEST-ONLY dependency shims

Minimal stand-ins for `pydantic`, `httpx` and `fastapi`, used ONLY so the
integration harness (`tests/integration_adaptive_runs.py`) can import the real
orchestrator in a sandbox without network/pip. They are NOT used in production:
the harness puts this directory on `sys.path` only when the real packages are
not installed. On a normal dev machine install the real requirements and the
shims are ignored.
