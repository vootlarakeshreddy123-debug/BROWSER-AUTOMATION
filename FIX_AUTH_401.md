# NEXA v4 — Fix for HTTP 401 authentication-required

## What was wrong

The supplied source code has Supabase authentication disabled by default (`SUPABASE_ENABLED=false` in
`backend/.env.example`). Your runtime log, however, explicitly said:

`Supabase auth + history ENABLED (every task endpoint requires a valid access token)`

At the same time, the frontend displayed `Sign-in not configured`. The frontend therefore sent no
`Authorization: Bearer ...` header, and the backend correctly rejected `POST /api/tasks` with HTTP 401.

The uploaded ZIP does not contain your real `backend/.env` or a frontend `.env.local`; those files are
machine-specific and may contain private keys. The project cannot safely overwrite or invent them.

## Safe fix for your current local setup

This project is being run as a local, single-user browser automation app. To use that mode, run this
PowerShell script from the extracted project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\\FIX_LOCAL_AUTH_401.ps1
```

The script changes only `SUPABASE_ENABLED` to `false` in `backend/.env`. It preserves all AI provider
keys and all other settings. If `backend/.env` does not exist, it creates it from `.env.example`.

Then stop BOTH NEXA processes (backend and frontend), and restart them.

Backend, from the `backend` folder:

```powershell
py -3.12 -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Frontend, from the `frontend` folder:

```powershell
npm run dev
```

Open `http://127.0.0.1:5173`.

## Code improvements in this repair

- `/api/health` now reports the backend authentication mode without exposing secrets.
- The frontend status now distinguishes local mode from the exact frontend/backend Supabase mismatch.
- Supabase authentication is NOT bypassed when enabled. If you want Supabase sign-in/history, configure
  both backend and frontend with the same project URL and browser-safe publishable key instead.

## Validation performed

- Python `compileall`: passed for the backend source.
- Backend unit suite: 279 tests passed.
- Gmail compose safety tests are included in that suite. The draft flow remains separate from Send;
  sending still requires explicit user confirmation.
- Live Chrome, real Gmail, and external provider API calls were not exercised in this offline validation.
