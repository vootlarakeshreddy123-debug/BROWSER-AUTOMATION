# NEXA Browser Automation Agent — Architecture (matches the code)

> This document describes the implementation as of the adaptive-loop fix.
> Older notes (`CHANGES_QUOTA_ARCHITECTURE.md`, `README_BROWSER_FIRST.md`, `README_FIX.md`)
> describe earlier designs and are kept only as history. Where they disagree with this file, this file wins.

## 1. Goal

Accept a natural-language browser task and operate the **real, visible Chrome** (Playwright over CDP):
observe the live page → decide the next action → execute it → observe again → verify progress → finish
only when the *live browser state* proves the goal. Nothing is hard-coded per website; known/simple tasks
may use deterministic fast paths purely as an optimisation.

## 2. Flow

```text
USER (React console)
  ↓  POST /api/tasks
TASK API (api/tasks.py) ── creates TaskRecord, starts agent.orchestrator.run_task
  ↓
ORCHESTRATOR (agent/orchestrator.py)        deadline + budgets + provider health + event recorder
  ↓
ROUTER (agent/router.py)
  ├── DETERMINISTIC FAST PATH  (agent/planner.py: explicit URL, known-site open, generic search/result paths)
  │        no AI call; same Playwright executor
  └── ADAPTIVE AGENT LOOP      (unknown / multi-step / anything the fast path cannot finish)
        ↓
     OBSERVE   browser/inspector.py   live DOM → compact semantic context (role, name, text, region, visibility…)
        ↓
     VERIFY ALREADY COMPLETE?  orchestrator._page_proves_final_goal (independent of the AI)
        ↓
     PLAN ONE ACTION  agent/adaptive_planner.py
        │   GEMINI  (primary)           ── per-call timeout, cooldown aware
        │   OLLAMA  (EMERGENCY ONLY)    ── hard per-task call + time budget, bounded repair
        │   neither available           ── PlannerUnavailable (instant) → generic DOM recovery
        ↓
     VALIDATE   agent/validator.py  +  first valid executable action only (ONE action per step)
        ↓
     EXECUTE   browser/element_resolver.py → browser/executor.py (Playwright, bounded timeouts)
        ↓
     OBSERVE AGAIN  →  PROGRESS CLASSIFIER (agent/progress.py, before vs after)
        ↓
     PROGRESS ─────────────► next step
     NO PROGRESS / WRONG STATE / REPEATED / FAILED
        ↓
     RECOVERY LADDER → success | honest failure (with diagnostics)
```

## 3. Components (and the file that implements each)

| Stage | File |
|---|---|
| Frontend console, WebSocket + polling | `frontend/src/hooks/useTaskExecution.ts`, `components/*`, `lib/execution.ts` |
| Task API | `backend/api/tasks.py`, `backend/main.py` |
| Orchestrator (state machine) | `backend/agent/orchestrator.py` |
| Deadline / budgets | `backend/agent/budget.py` |
| Provider health + failure classes | `backend/agent/provider_health.py` |
| Provider calls (Gemini → Ollama) | `backend/agent/adaptive_planner.py`, `backend/services/ai_provider.py` |
| Router | `backend/agent/router.py` |
| Deterministic planner (fast paths) | `backend/agent/planner.py` (unchanged) |
| Inspector / resolver / executor | `backend/browser/inspector.py`, `element_resolver.py`, `executor.py` (unchanged) |
| Progress classification | `backend/agent/progress.py` |
| Independent verification | `backend/agent/orchestrator.py` (`_page_proves_final_goal`, `_unsatisfied_goal_clauses`, `_goal_evidence`) |
| Structured events + statistics | `backend/agent/events.py` |
| Status transport | `backend/services/ws_manager.py` (events + terminal `result`), `GET /api/tasks/{id}` (events + stats) |

## 4. The adaptive step (state machine)

```
OBSERVE → (already complete? verify) → ROUTE → PLAN(one action) → VALIDATE → EXECUTE → OBSERVE → VERIFY_PROGRESS → …
```

Hard guarantees enforced in code:

* **ONE action per AI response.** `_MAX_ACTIONS_PER_CHUNK = 1` and the candidate loop `break`s after the first valid action; the rest were planned against a DOM that may already be stale.
* **Progress is measured, not assumed.** After each action `ProgressTracker.classify(before, after, …)` returns `PROGRESS`, `NO_PROGRESS`, `WRONG_STATE`, `ACTION_FAILED`, `REPEATED_ACTION` or `PAGE_CHANGED_NO_GOAL_PROGRESS`. A successful Playwright call is not progress; a changed URL alone is not progress (goal relevance is computed from the user's own words). Trouble counters reset **only** on `PROGRESS`.
* **AI `done=true` is never trusted.** The orchestrator re-observes and runs the independent verifier. Rejected claims are logged (`PLANNER CLAIMED COMPLETE / VERIFIER RESULT: INCOMPLETE`), counted per provider and per task (`MAX_FALSE_DONE_CLAIMS`), never reset by an unrelated successful action, and a provider that claims done wrongly is distrusted for the rest of the task. The loop switches strategy (semantic extraction → generic DOM recovery) and finally fails honestly.
* **No identical-input retries.** A planner input that already failed or was rejected is not sent to the same provider again.

## 5. Provider architecture

```
            ┌─────────────┐  quota / 429 / 503 / timeout / malformed
 step ───►  │  GEMINI     │ ───────────────┐   → global cooldown + per-task state
            │  (primary)  │                ▼
            └─────────────┘        ┌────────────────────┐
                                   │ OLLAMA (emergency) │  ≤ OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK requests
                                   │ qwen3:4b           │  ≤ OLLAMA_EMERGENCY_BUDGET_SECONDS total
                                   └────────┬───────────┘  each request ≤ OLLAMA_EMERGENCY_TIMEOUT_SECONDS
                                            │ timeout / malformed / error / false done → retired for this task
                                            ▼
                               PlannerUnavailable  →  generic live-DOM recovery  →  honest failure
```

* Two layers: a process-wide **cooldown** (survives across tasks) and **per-task provider state**.
* Gemini quota/503/timeout → cooldown (`GEMINI_COOLDOWN_SECONDS`, `GEMINI_TIMEOUT_COOLDOWN_SECONDS`); Gemini is not called again during it.
* Ollama is **not** a replacement planner. Every request — including the single JSON-repair request — is charged to the emergency budget (`CallLimits`), uses the compact live prompt, and cannot start if the budget cannot support it. Any timeout/malformed/error retires Ollama for the task.
* `GEMINI_MODEL` is used exactly as configured. NEXA no longer rewrites model names and does not assume availability; a rejected model name surfaces as a classified provider failure in the event stream.

## 6. Deadline and budgets

`TaskBudget` (`agent/budget.py`): internal deadline = `TASK_TIMEOUT_SECONDS − TASK_DEADLINE_MARGIN_SECONDS`.
The outer `asyncio.wait_for(TASK_TIMEOUT_SECONDS)` remains only as a hard backstop.

| Budget | Rule |
|---|---|
| AI planning | ≤ `AI_PLANNING_BUDGET_FRACTION` of the deadline **and** never into the last `BROWSER_RESERVE_SECONDS` |
| Browser actions | the remainder; an action is not started with < 3 s left and is abandoned at the deadline |
| Recovery | ≤ `RECOVERY_BUDGET_FRACTION` |

Before every AI call, browser action and recovery step the budget is consulted; an AI call is never started when the remaining AI budget is below that provider's minimum.
On expiry the task ends as `timeout` with a message stating how much time went to AI vs browser and **where** it stopped.

## 7. Recovery ladder

1. Primary AI planner (Gemini)
2. Semantic extraction / semantic resolver recovery
3. Generic DOM recovery (`_dom_recovery_plan`, `go_back` after a wrong-state page)
4. Screenshot / visual inspection — **not implemented** (see limitations)
5. Emergency provider (Ollama, bounded)
6. Honest failure with diagnostics

## 8. Observability

`agent/events.py` records structured events — `TASK_START, BROWSER_READY, ROUTER, OBSERVE, AI_PLAN, PROVIDER_STATUS, ACTION_START, ACTION_RESULT, PROGRESS, VERIFICATION, RECOVERY, TASK_SUCCESS, TASK_FAILURE, TASK_TIMEOUT` — each with `elapsed_ms`, step, provider, action, target, URL, title, duration, result, error, reason where recorded.
Events stream over the existing WebSocket (`{"type":"event"}`), are included in the terminal `result` payload, and are available via `GET /api/tasks/{id}` (`events`, `stats`). `stats` is derived purely from recorded events. The UI (Agent status, Automation chart, Execution timeline) renders only these.

## 9. Known-site knowledge

`planner.py` still contains site adapters / a domain table for *opening* well-known sites and a few special-case planners. They are optional optimisations; the adaptive loop does not depend on them and no site-specific logic was added.

## 10. Active page (single source of truth) and new tabs

* `BrowserManager.current_page` is the truth. After **every** action the orchestrator calls `_sync_active_page()`, which re-reads it, drains `BrowserManager.adoption_log` into `NEW_PAGE_DETECTED` / `PAGE_ADOPTED` / `ACTIVE_PAGE_CHANGED` events, and returns the Page every later step (observe, plan, execute, verify) must use. A stale/closed local Page is re-targeted to a live page instead of re-bootstrapping.
* One mechanism for new tabs: the executor snapshots `context.pages`, clicks, then `adopt_new_pages()` diffs the pages (waits ≤ `BROWSER_NEW_PAGE_WAIT_MS`, returns early on same-tab navigation). A tab that opens **later** than that window is adopted by `adopt_late_popups()`, which only adopts pages whose `opener` is a page NEXA is controlling. The old `expect_popup` wrapper was removed.
* `execute_plan` follows an adopted page for the remaining actions of the same plan and reports the adopted page's URL/title.
* Progress classification includes page identity and page count, so "same URL but a new tab opened" is `PROGRESS`.

## 11. State-aware navigation and recovery

* `navigation_regression_reason()` (agent/page_identity.py): a `navigate` that would go to where we already are, or to a site root while the page is deeper / the task already did work, is skipped (event `RECOVERY strategy=skip_navigation`). The workflow-memory replay no longer closes a tab it could not verify; the bootstrap reuses it when navigating would regress.
* Recovery ladder (agent/recovery_ladder.py): an action already tried on the same page state without progress is never repeated. Order: investigate other open pages → alternate target in page content (never header/nav/footer) → scroll → generic DOM recovery → honest failure.

## 12. Deterministic typing workflow (0 AI calls)

`agent/typing_goal.py` handles "open <site> and type <text>": navigate (bootstrap) → pick the composer from the inspected DOM (textarea / input / contenteditable / role=textbox; role+name, then placeholder, aria-label, xpath last) → TYPE → verify the editable control really contains the text in the live DOM. Enter is never pressed unless asked (a "send" request is left to the adaptive path, which is not claimed here). No composer + login/sign-up controls ⇒ reported as **blocked**, not success; no composer and no login wall ⇒ the adaptive path (AI, bounded).

## 13. Extraction provenance

`agent/extraction.py`: the requested kind (heading / title / first sentence / section / price / date / phone / table value) defines acceptable DOM provenance. The executor returns `kind` + element `tag`; the verifier rejects a value of the wrong kind and re-checks the value against the live DOM of the *current* page (`verify_in_dom`). A "heading" request is answered only from an h1–h6 element.

## 14. Which code is running

At startup `main.py` logs `NEXA_RUNTIME project_root=… cwd=… python=… orchestrator=… ai_provider=… gemini_model=… ollama_model=…` and fails with a clear error if the orchestrator module is not this project's code. `agent/goal_clauses.py` holds the clause-splitting regex and is imported at module load, so a mixed/stale copy fails at startup instead of mid-task with `NameError`. `/api/health` returns `browser_ready` and per-provider status; the UI shows Backend / Browser / Gemini / Ollama as independent states.

## 15. Goal runner: decomposition, per-goal verification, generic primitives

```
USER TASK
  ↓ agent/goals.py        split into ordered clauses → one Goal each (kind + clean params)
  ↓ needs_goal_runner?    back/forward/new-tab/switch/close/scroll/press/find/wait, several searches, "open … in a new tab"
  ├── no  → the single-flow path of §2 (unchanged; its search query now also comes from the decomposer)
  └── yes → agent/goal_runner.py  (TaskState: goals, statuses, per-page URL history, counters)
              for each goal:  observe → deterministic action → execute ONE action → sync active page
                              → settle → observe → VERIFY THIS GOAL from evidence → COMPLETED | recover | FAILED | BLOCKED
              AI only when no deterministic rule can act, scoped to THAT clause; it can only propose actions
              final independent verification: every goal verified, page alive, no block page
```

**Decomposition.** Split on `;`, commas/`and`/`then`/`next`/`after that`/`followed by`/`afterwards`/`finally` *only when an action verb follows*, so "search for cats and dogs" stays one query and "search for A, then go back and search for B" becomes SEARCH(A), BACK, SEARCH(B). A comma right after a URL is always a boundary. Each clause maps to NAVIGATE, SEARCH, TYPE, CLICK (target text or ordinal + new-tab flag), PRESS, SCROLL, BACK, FORWARD, NEW_TAB, SWITCH_TAB, CLOSE_TAB, FIND, EXTRACT, WAIT, or ADAPTIVE (not understood → scoped AI, otherwise an honest failure). Control words can never reach a text field. Site *names* resolve only through the existing known-site table; unknown names are reported, never guessed.

**Primitives** (`browser/executor.py` + the page-lifecycle API in `browser/manager.py`): navigate, type, click, press, scroll up/down, **back/forward** (real `page.go_back/go_forward`, history-API fallback for same-document entries), **new_tab**, **switch_tab**, **close_tab** (never the last tab), wait. Every executed action returns evidence: before/after URL, before/after page id, page count before/after, new page detected, duration, typed text/key.

**Verification** (`agent/goal_verify.py`) — from observed evidence, not instruction wording:
| Goal | Verified by |
|---|---|
| NAVIGATE | browser on the requested site (not about:blank) |
| SEARCH | query text found in a field **and** submitted **and** results state (query in URL, or changed results page covering the query) |
| TYPE | an editable control's live value/text contains the text; Enter never pressed unless asked |
| CLICK | new page adopted, or navigation away from the result list (ordinal clicks must leave the list) |
| BACK / FORWARD | history moved **and** landed on the URL the per-page history predicts |
| SCROLL | scroll position changed (or already at the edge) |
| NEW_TAB | page count increased, new page adopted as active, host matches the site |
| SWITCH / CLOSE | active page id changed / tab count decreased |
| PRESS | page state changed (Escape/Tab accepted as "delivered") |
| FIND | the text is visible after scroll-into-view |
| EXTRACT | extraction provenance + live-DOM re-check (§13) |
| ADAPTIVE (AI) | AI "done" is only a *proposal*: accepted when state changed because of this goal's actions and goal words appear on the page; a state change alone is accepted as **WEAK** and reported as a limitation |

**Recovery** is state-aware and bounded: late-popup wait/adoption → alternate candidate (never the same target twice) → bounded scroll → scoped AI → honest failure. Search falls back to the submit button when Enter changes nothing. CAPTCHA / login / access-blocked pages (`agent/blocked_page.py`) are never bypassed: the runner waits up to `MANUAL_INTERVENTION_WAIT_SECONDS` for you to clear it in the live window, then continues or reports BLOCKED.

**Observability.** Events `GOAL_PLAN / GOAL_START / GOAL_COMPLETE / GOAL_FAILED / GOAL_BLOCKED` plus the existing OBSERVE/ACTION/PROGRESS/VERIFICATION/RECOVERY/PAGE_* events (each tagged with the goal id). The UI's Goals panel shows per-goal status, current action, active page + tab count, and the last verification — all rebuilt from recorded events.

**Which tasks use which path.** Tasks containing back/forward/new-tab/switch/close/scroll/press/find/wait, two or more searches, or an "in a new tab" click use the goal runner. Everything else keeps the earlier single-flow path (deterministic fast paths + adaptive loop), which already had its own verification and recovery; both share the executor, page lifecycle, budgets, provider health and events.

## 16. v4 additions (supersedes the two-provider description in section 5 where they differ)

**Request path:** router -> `agent/understanding.understand()` (deterministic, structured: intent, ordered subgoals, entities,
missing info, expected result, risk, confirmation flags) -> optional ONE bounded AI call (`agent/ai_assist.ask_json`, strictly
validated, merged so it can never lower risk or add compose/send) -> goal runner / adaptive loop.

**Providers:** `providers/registry.py` holds the five providers (`gemini, ollama, openai, claude, grok`), their specs, config
validation (`config_problems`), selection (`order_for`: manual = that provider only; auto = `AUTO_PROVIDER_ORDER`), and the safe
`status()`. Hosted adapters (`providers/openai_compat.py`, `anthropic_adapter.py`) share `providers/base.py` (prompt/plan codec reused
from the Gemini provider) and `providers/http.py` (`post_json`, `redact`). `agent/provider_health.py` keeps per-task state and a
global cooldown/circuit breaker per provider; `agent/adaptive_planner.plan_next_action` walks the eligible order once per step.

**Pause / resume:** `GoalRunner` raises `GoalNeedsConfirmation` / `GoalNeedsInput` (`agent/compose_goal.py`); the orchestrator
stores the paused state in `_RESUME`/`_RUNTIME`, sets `awaiting_confirmation` / `needs_input`, and keeps the live tab.
`POST /api/tasks/{id}/respond` -> `respond_to_task` -> `run_task` resumes at the paused goal with a fresh budget.

**Safety gate:** `agent/safety.classify_label` marks Send/Post/Publish/Pay/Delete/Submit-style controls; `GoalRunner._exec` refuses to
click them unless the goal id is in `approved`. Event text is passed through `redact` and `scrub_text`.

**Browser modes:** local Chrome/CDP only. `BROWSER_EXECUTION_MODE` other than `local` is reported and ignored.

## 17. Routing policy (audit round)

1. `understand()` -> `decompose()` splits the request into goals after normalising politeness / spoken punctuation.
2. A phrase is a **destination** only if it is a URL, a domain, a known site name, or a short plain name (`planner.is_plain_site_name`).
   Sentences are never destinations: they become goals (say/search/click/extract) or an ADAPTIVE goal for the AI planner.
3. `needs_goal_runner(goals)` sends multi-step tasks (type+submit, back/forward, several searches, ...) to the verified goal runner.
4. Legacy fast path: a single navigation completes the task only when the instruction is just "open <destination>"; otherwise it is a
   bootstrap and the adaptive loop continues (`orchestrator`, "ROUTING POLICY").
5. Outcomes come from recorded events (`events.derive_outcome`): success / partial / blocked / failed / cancelled / awaiting_user.
