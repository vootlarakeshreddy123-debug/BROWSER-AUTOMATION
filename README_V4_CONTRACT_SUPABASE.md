# NEXA v4 — Task Contract, Groq-first planning, Supabase history

This is the existing NEXA v4 project (not a new app). This document matches the code; where something could not be run
here it says so.

## 1. Root cause of the proven defect
"Open Amazon login page and fill the phone number as 9010635950" became **navigate + click Sign in** and was reported
verified-complete without typing the number.

1. `login_assist` replaced the *whole* instruction with "open the login page", discarding every other clause.
2. Nothing compared the produced plan with the original instruction, so a plan that covered only part of it was treated as complete.
3. The "done" signals were a click, a URL change, or an empty action list — none of which prove the phone was entered.

## 2. What changed
| Area | Change |
|---|---|
| Understanding (`agent/understanding.py`, `agent/goals.py`) | `_login_goals` replaces only the "open login page" clause and keeps the rest. New `FILL` goal (phone/email ordinary identifiers). Passwords/OTPs become **blocked** secret goals; their value is never carried. AI interpretation is validated and `merge()` rejects an AI plan that drops a requirement. |
| Task contract (`agent/contract.py`) | Ordered requirements (id, kind, value, state, evidence, blocker). States PENDING, RUNNING, ACTION_SUCCEEDED, VERIFIED_COMPLETE, BLOCKED, FAILED, CANCELLED. `is_verified_complete()` is true only if every requirement is verified. `coverage_gaps` finds explicit values (phone/email/quoted) not carried by any goal and asks instead of dropping them. |
| FastPath (`agent/router.py`, `orchestrator.py`) | Bootstrap only. `fast_path_covers()` is the one shared completeness policy at the router, the orchestrator and the legacy loop (`unmet_typed_values`). An incomplete fast plan is marked `BOOTSTRAP_ONLY` and the goal runner takes over. |
| Runner (`agent/goal_runner.py`) | `_g_fill`: find the real field (role/name ranking, no weak fallback), ambiguity → blocker/clarify, type, read the value back from that field, **never submit**. `_final_verification` requires the contract. |
| Final status | `_terminal_payload` downgrades SUCCESS unless `contract_state == VERIFIED_COMPLETE`; REST, WebSocket, history and DB carry the same `verification_status`. |
| Schema / executor | Empty TYPE text rejected (`action_schema.py`, `executor.py`). Inspector form-control names = aria-label, label, placeholder (the previous `name` attribute broke role+name locators). |
| Gmail compose (`compose_goal.py`) | A recipient counts only when committed (a chip/resolved address), not when a typed fragment or an un-selected suggestion is present. Draft never sends; send needs explicit confirmation. |
| Providers | Groq (groq.com, OpenAI-compatible, `GROQ_*` config — never xAI config). Auto order `groq, gemini, ollama, openai, claude`. `retry_after` honoured for cooldowns; "configured order" and "provider actually used" are separate facts; a semantically insufficient answer is recorded as a planning-quality failure (`record_false_done`). |
| Supabase | Auth token validation, persisted tasks/events, RLS. See §4. |
| Frontend | Sign-in/out, token on every call, WS first-message auth, saved History with expandable timeline, verification + persistence chips, Groq labels. |

Before → after for the critical case: `navigate → click Sign in → "complete"` ⟶ `navigate → click Sign in → inspect page → find the phone field → type → read the value back → VERIFIED_COMPLETE (no submit)`.
If the page has no phone field the task ends **BLOCKED** with the reason; if two fields tie it asks; a password request is blocked.

## 3. Install, configure, run
```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then edit .env (never commit it)
#   minimum: GROQ_API_KEY=<your key>   (or leave empty and use Gemini/Ollama)
uvicorn main:app --host 127.0.0.1 --port 8000

# frontend
cd ../frontend
npm install
cp .env.example .env.local                             # leave the Supabase values empty for local mode
npm run dev                                            # http://localhost:5173
```
Key hygiene: keys live only in `backend/.env`. If a key was ever pasted into chat, a log or a commit, **rotate it**.

### Tests
```bash
cd backend && python -m unittest discover -s tests -p "test_*.py"      # unit (pytest also works if installed)
cd backend && xvfb-run -a python tests/integration_adaptive_runs.py --all --out /tmp/nexa_runs   # real headless Chrome, fixture sites, scripted fake providers
cd frontend && npm test                                                  # node --test
```
No test uses a real provider key or a real website; providers are mocked at the HTTP transport.

## 4. Supabase (optional; off by default)
`SUPABASE_ENABLED=false` keeps local single-user mode (no sign-in, nothing stored remotely).

1. Create a project. Run `supabase/migrations/0001_nexa_tasks.sql` (SQL editor, or `supabase db push` with the CLI linked to your project).
2. Backend `.env`: `SUPABASE_ENABLED=true`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` (optional `SUPABASE_SECRET_KEY`, backend only).
3. Frontend `.env.local`: `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`. Create a user in Supabase Auth (Dashboard → Authentication → Users).

Design
* **Role of Supabase:** identity + history only. It never generates actions or decides success; the backend writes the status it decided.
* **Auth:** every task/browser endpoint validates the bearer token against GoTrue; the owner is the token's user, never a request field. The WebSocket sends `{"type":"auth","token":…}` as its first message (not in the URL). If the auth service is unreachable the request is **denied (503)**, not allowed.
* **Isolation:** tables `tasks`, `task_events`; RLS forced; owner-only policies; `user_id` immutable; events append-only; column-level grants. Reads/writes use the user's JWT + publishable key so RLS is the last line of defence. Other users' task ids return 404.
* **Idempotency:** task id is the task UUID; event ids are `uuid5(task, seq)`; writes use ignore-duplicates, so retries never duplicate.
* **Outage behaviour:** the browser loop never waits on a DB write. Writes retry (3 attempts, backoff) then queue; the task shows `persistence: failed` (never a false "saved"). `POST /api/persisted/retry` re-sends queued writes (History → "Retry unsaved").
* **What is stored:** instruction, status, provider used, verification status, result summary, events. Keys, tokens, passwords/OTPs are removed; phone numbers and e-mail addresses are masked; screenshots, typed text and email bodies are never stored.
* **Retention / deletion:** a signed-in user may delete their own `tasks` rows (RLS delete policy); the events go with them by `ON DELETE CASCADE` (events are append-only for clients, so they cannot be edited or deleted separately). `nexa_purge_old_tasks(days default 90)` deletes tasks older than N days; it is executable only by `service_role`/the SQL editor (not by app users) — run it by hand or schedule it with pg_cron. Deleting a Supabase auth user cascades to all of their tasks and events.
* **CORS / network:** origins limited to `ALLOWED_ORIGINS`; backend binds `127.0.0.1`.

Honest limits: events are persisted when a task reaches a terminal or paused state, not streamed mid-run.

## 5. Manual test checklist
- [ ] **Amazon phone, no submit:** "Open Amazon login page and enter the phone number 9010635950" → Sign-in page opens, phone field holds the number, nothing submitted, status *Completed – verified*. (Live site: not run in the build environment — try it yourself.)
- [ ] **Password blocked:** "log in to Amazon with password hunter2" → blocked, password never typed.
- [ ] **Gmail draft:** "open gmail and write an email to <contact> saying hello" → draft verified, not sent; sending asks for confirmation.
- [ ] **Groq first:** with `GROQ_API_KEY` set, a task that needs the planner shows *Handled by Groq*; with a bad key the next provider is used and the timeline shows the Groq failure.
- [ ] **Sign in / out:** header shows Sign in; after sign-in your email shows; after sign-out API calls get 401 and History asks to sign in.
- [ ] **Persistence after restart:** run a task, restart the backend, open History → the task and its timeline are still there.
- [ ] **Two-user isolation:** sign in as user A, run a task; sign in as user B → B's History does not show it and `/api/persisted/tasks/<A's id>` gives 404. Also run `supabase/tests/rls_isolation.sql` in the SQL editor.
- [ ] **Supabase outage:** block the Supabase URL (or set a wrong one) → the browser task still runs; the task shows *History NOT saved*; fix the URL and press *Retry unsaved*.
