# NEXA v4 changes (upgrade of the existing project)

## Contract / Groq / Supabase round
Full description: `README_V4_CONTRACT_SUPABASE.md`.
* Fixed the dropped-goal defect (`login_assist` kept only "open login page"): requirements are now an immutable contract; new `FILL` goal; secrets blocked; AI plans cannot drop requirements.
* New `agent/contract.py`; one shared completeness policy for router, orchestrator and legacy loop; SUCCESS is downgraded unless `VERIFIED_COMPLETE`.
* Groq provider (first in AUTO), retry-after cooldowns, planning-quality failures; Gmail recipient must be committed.
* Optional Supabase: token-validated API, persisted tasks/events with RLS (`supabase/`), idempotent bounded-retry persistence, frontend sign-in and saved history.
* Backend binds 127.0.0.1; CORS limited to `ALLOWED_ORIGINS`.

## Audit round
* Fixed the root cause of `open chatgpt and say hello` -> `chatgptandsayhello.com` (shared destination check + router safety net); new say/tell/ask goals that type and submit with verification; politeness/speech normalisation; `login_assist` (opens the sign-in page only); evidence-based `outcome` (success/partial/blocked/failed); tests `test_nl_routing` (17), dataset `tests/nl_dataset.py`, scenarios `p01`-`p03`. Details: `TEST_REPORT.md` "Audit round".


* **Providers:** new `backend/providers/` package - registry + common interface for Gemini, Ollama, OpenAI, Anthropic Claude, xAI Grok
  (REST adapters with an injectable transport). Env-driven config, startup/selection validation, manual selection + Auto with
  `AUTO_PROVIDER_ORDER`, per-provider models, hosted providers opt-in (`*_ENABLED`), per-task hosted call cap, shared deadline/AI budget.
* **Provider health:** failure kinds incl. AUTH (401/403), quota detection, per-provider circuit breaker/cooldown, per-task failure
  tracking, one bounded retry for transient 5xx only, no repeated identical failures, honest all-providers-failed message.
* **Status:** `GET /api/providers` and `/api/health.providers` (no secrets); `provider` on task create; `provider_used` on tasks/stats/WebSocket.
* **Understanding:** `agent/understanding.py` (structured, schema-validated, deterministic first; strict validator for the optional AI call;
  AI can never weaken a safety confirmation or create compose/send goals) + `agent/ai_assist.py` (bounded one-shot JSON call).
* **Compose/send:** `agent/compose_goal.py`, field-ranker intents RECIPIENT/SUBJECT/BODY, autocomplete resolution from the live DOM,
  ambiguity/unknown recipient -> ask, compose = verified draft, send always needs confirmation.
* **Safety:** `agent/safety.py` confirmation gate on consequential controls, secret redaction in events/WebSocket/history,
  new task states `awaiting_confirmation` / `needs_input`, `POST /api/tasks/{id}/respond`, resume in the same live tab.
* **Frontend:** provider selector + status panel, Run/Stop/Clear, awaiting-confirmation / needs-input cards, "handled by" provider,
  understanding line, blocked phase, provider chips in the header, tests (`npm test`).
* **Browser Use Cloud:** not implemented; the setting is reported honestly and local mode is used.
* **Tests:** 54 new unit tests (providers, understanding), 14 new fixture scenarios (m01-m14), frontend tests.

---

# Live Browser Edition — implementation notes

## Requirements mapped to implementation

| Requirement | Implementation |
|---|---|
| Real browser, not frozen screenshot | Playwright browser remains visible and the task page stays alive after automation. |
| New tab for each task | `BrowserContext.new_page()` creates a fresh tab for each task. |
| Enlarge/maximize browser | Browser launches with `--start-maximized`; context uses `viewport=None`. |
| Manual interaction after task | Task page is not closed. User can interact with the real Chrome window. |
| Preserve session | One reusable browser context keeps cookies/session state across tabs. |
| Focus browser from UI | `POST /api/browser/focus` calls `page.bring_to_front()`. |
| No misleading preview | React UI describes the real OS browser as the live surface instead of rendering the final screenshot as if it were interactive. |
| Voice command | Web Speech API microphone control in `TaskInput.tsx`. |
| Pause without losing transcript | Continuous recognition + automatic restart on `onend`; submission happens only when Run task is pressed. |
| Some tasks failing | Generic search avoids Google bot challenges by using DuckDuckGo; explicit Google tasks remain explicit and report challenges honestly. |
| Longer real-world tasks | Task deadline raised from 10s to 30s; navigation/action timeouts increased. |
| Bounded retries | One element-resolution retry by default. |
| WebSocket reliability | HTTP polling fallback was added. |
| Cancel reliability | Background task handles are tracked and cancelled instead of only changing database/UI status. |
| Keep sensitive actions safe | Purchase/delete/send/transfer style actions still fail closed without approval. |

## Existing evidence inspected

The supplied project archive contains the FastAPI + Playwright backend and Vite + React frontend. The supplied desktop screenshot shows the old screenshot-preview design. The supplied reference video was reviewed as a visual/behavior target: dark, compact browser-agent presentation with visible web-task activity. The supplied terminal logs show the Windows Playwright startup issue is already fixed: the backend launched the browser successfully, `/api/health` returned 200, task creation/run returned 200, and task WebSockets were accepted.

## Important architecture decision

An arbitrary external website should not be embedded in an iframe and called "interactive." A screenshot also cannot accept clicks or typing. Therefore the live surface is the actual Playwright-controlled Chrome window. The React console controls and explains that handoff.

## Known environment note

The build environment used to prepare this archive did not have the frontend npm packages cached, so a full `npm run build` could not be executed here. All backend Python files were syntax-checked successfully, and all frontend `.ts/.tsx` source files were transpile/syntax checked with TypeScript. Install dependencies normally with `npm install` before `npm run build`.

## Recovery patch — 2026-09-17

- Hardened the Playwright browser manager against a manually closed/crashed Chrome window or context.
- `/api/health` now reflects the underlying Playwright connection instead of only checking whether Python objects still exist.
- Creating a task tab automatically repairs a stale browser/context once before returning an error.
- Kept the first visible tab task-driven: no blank Chrome tab is created merely by starting the backend.
- Added Chrome flags that reduce Windows background/occlusion behavior while keeping the browser maximized and visible.


## Final live-window handoff fix — 2026-09-17

- Added a Windows-native Chrome window activator using `ctypes`; `page.bring_to_front()` alone only selects a tab and may leave the React console as the foreground OS window.
- Added Chromium CDP window control to force the task window into the maximized state, preventing the real page from being clipped in a small window.
- Added `--new-window` to the browser launch arguments.
- The backend now re-focuses the task tab after navigation completes and also when a navigation/action fails, so the user sees the actual destination rather than having to click the Chrome tab.
- Hardened page snapshot handling if the user closes Chrome during an operation.
- Increased default navigation/task budgets to 25s/45s for slower real sites.

### Intended Windows behavior

1. User types or speaks a task in the agent console.
2. Agent creates a new Playwright tab.
3. Agent navigates that tab.
4. Agent selects that tab, maximizes Chrome, and activates the Chrome OS window.
5. The Chrome window becomes the foreground window automatically.
6. Automation finishes, but the tab remains open and fully interactive for the user.
