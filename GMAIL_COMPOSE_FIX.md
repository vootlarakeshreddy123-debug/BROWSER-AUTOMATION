# NEXA v4 — Gmail compose fix (targeted)

Scope: composing an email (recipient + body, optional subject) and leaving it as a draft. Providers, API config, UI design,
database, datasets, auth and unrelated browser tasks were not touched.

Note: the reference PDF mentioned in the request was not attached (only the prompt and the project ZIP), so the ZIP source is the sole authority.

## Phase 1 — Diagnosis (proven by running the code, not assumed)

**Execution path.** `orchestrator._run_understood` → `understand()` → `goals.decompose()` → `needs_goal_runner()` (COMPOSE is a runner kind, so the AI planning loop is never used) → `GoalRunner._g_compose` (`agent/compose_goal.py`): `_open_composer` → `_resolve_recipient` → subject → body → verify.

| # | Question | Answer (evidence) |
|---|---|---|
| 1 | Parsed into recipient + body? | **No.** `goals._BODY` / `_BODY_LITERAL` only recognise "saying / that says / with the message". For "compose an email to X, and write 'B'. Do not send it." `understand()` returned `NAVIGATE, COMPOSE{recipient}, TYPE{text:B}` plus `missing=[body]`; `no_send` was `False`. Reproduced with the instruction text itself. |
| 2 | Routed to the compose workflow? | Yes (runner), but with the body missing the task **paused immediately** (`needs_input`, 0.01 s) asking "What should the email say?" — before Gmail was even opened. After answering, the stray `TYPE` goal typed the body a second time into whichever element had focus (`_g_type` may call `_ai_fallback`, which is where "AI takes over" came from). |
| 3 | After clicking Compose | `_open_composer` waited ≤ 6 s for the composer. |
| 4–6 | Field detection / inspector / ranker | Gmail-structured DOM (`input[role=combobox][aria-label="To recipients"]`, `div[role=textbox][contenteditable][aria-label="Message Body"]`, `input[name=subjectbox]`) is recognised by the existing ranker and inspector — no change needed (unit test + Chrome fixture). |
| 7 | Typing | Works; the failure was upstream of typing. |
| 8 | Timing / autocomplete / verification | **Three real defects** reproduced on a Gmail-structured fixture (below). |
| 9 | Can "done" be claimed after Compose alone? | Not by the compose goal (its verdict needs the fields), but defect D3 let an uncommitted address pass. |
| 10 | Why existing tests missed it | Every existing mail scenario used "… saying hello" and a composer that appears synchronously with a plain `<button>`. None used "write '…'", a late inbox, an async composer, a sign-in page, or a combobox that keeps typed text. |

**Defects**
- **D0 (primary)** body phrase "write '…'" and "Do not send it" not understood (`goals.py`).
- **D1** `_open_composer` looked for Compose once; a webmail inbox hydrates after page load → "no way to start a new message was found" after ~2 s (scenario `gm03`).
- **D2** a sign-in page (e.g. Google's "Email or phone / Next") was reported as that same misleading error instead of "sign in" (`gm05`).
- **D3** a typed-but-not-committed address was accepted: the check counted the text still sitting in the To box (`gm08` returned *success* with nothing committed). Body verification also searched the whole page text rather than the body editor.

## Phase 2 — What changed (smallest fix, compose-scoped)

| File | Change |
|---|---|
| `backend/agent/goals.py` | Clauses right after a compose clause complete that message: "write/type '…'" → body (quoted text kept exactly, including its full stop), "Do not send it" → `no_send`, "subject X" → subject. "click Compose, enter X in To, type 'B' in the body" folds into one COMPOSE goal. Anything else (other targets, other sites) is untouched. |
| `backend/agent/compose_goal.py` | Bounded wait (8 s) for the Compose control; an existing composer is reused (Compose not clicked again); bounded wait (8 s) for the composer after clicking; sign-in page → *blocked* with a clear message; recipient counts only when committed (chip / hidden `to` value / email attribute / field consumed; a combobox still holding the text or an open suggestion list is **not** committed); one extra commit key (Enter) for typed addresses; final verification reads the recipient, subject and body back from their own fields (body must occur exactly once); stage-named errors and VERIFICATION events. |
| `backend/agent/field_ranker.py` | `verify_in_field` also returns how many times the text occurs (additive). |
| `backend/tests/test_gmail_compose.py` (new) | 24 unit tests. |
| `backend/tests/fixture_site_extra.py`, `fixture_site.py` | New `gmaillike` fixture: Gmail-*structured* page (late hydration, `role=button` Compose, async composer, combobox To + chips, hidden `to`, contenteditable body). **Modelled, not Gmail and not live.** |
| `backend/tests/integration_adaptive_runs.py` | Scenarios `gm01`–`gm11`. |
| `GMAIL_COMPOSE_FIX.md` | this file. |

## Corrected flow
1. Parse: NAVIGATE Gmail + COMPOSE{recipient, body, subject?, no_send}. Missing recipient/body → ask first; nothing is invented.
2. Open Gmail in the live tab; wait (bounded) for the inbox. Signed-out → blocked.
3. If a composer is already open, use it; otherwise click Compose and wait until the To field **and** the body editor are visible.
4. Type the recipient; pick the matching suggestion (or confirm a typed address with Tab/Enter); ambiguous contacts → ask; verify it is committed.
5. Subject only if requested; body typed into the body editor.
6. Read back recipient / subject / body from the live composer → VERIFIED_COMPLETE. The draft stays open; Send is a separate goal that always needs your confirmation.

## Tests actually run (this environment)
| Command | Result |
|---|---|
| `cd backend && python -m unittest tests.test_gmail_compose` | PASS 24/24 |
| `cd backend && python -m unittest discover -s tests -p "test_*.py"` | PASS except `test_dataset_api` (needs fastapi test client — environment) |
| `cd backend && xvfb-run -a python tests/integration_adaptive_runs.py --all` | PASS 85/85 (74 existing + 11 new gm01-gm11; real headless Chrome, fixture sites, scripted fake providers) |

**Not tested:** the live Gmail site (no network/sign-in here). Real Gmail's DOM may differ from the model in details I could not verify; the commit check therefore accepts several independent forms of evidence and otherwise fails loudly instead of claiming success.

## Apply on Windows
1. Close NEXA (backend and frontend windows).
2. Back up your project folder (copy it).
3. Unzip `NEXA-v4-gmail-fix.zip` somewhere temporary.
4. Copy the unzipped `NEXA-v4\backend` and `NEXA-v4\GMAIL_COMPOSE_FIX.md` over your project (choose *Replace*). Your `backend\.env` is not in the ZIP and is not touched.
5. Start the backend (`START_BACKEND.bat`) and frontend (`START_FRONTEND.bat`) as usual. No new packages are needed.

## Test it in NEXA
Sign in to Gmail in the Chrome window NEXA controls, then run:

`Open Gmail, compose an email to YOUR_OWN_ADDRESS@gmail.com, and write 'Hello, I hope you are doing well.' Do not send it.`

Expect: status *Completed – verified*, a Compose window open with your address as a chip and the body typed once, nothing sent. If it stops, the message names the stage ("sign-in page", "composer did not appear", "recipient is not committed", …) — send me that text and the timeline.
