# Adaptive-loop test report

## What was and was NOT tested

| Layer | Status |
|---|---|
| Real orchestrator, router, planner fast paths, inspector, resolver, executor, verifier, progress tracker, budgets, provider-health manager, event recorder | **REAL code** |
| Chrome | **REAL Chrome/Chromium** (headed under Xvfb, driven over CDP by NEXA's BrowserManager) |
| LLM planners | **SCRIPTED FAKES** (rule-based, read the live element list, emit actions). Not Gemini, not Qwen3:4B |
| Websites | **LOCAL FIXTURES** (`tests/fixture_site.py`: "LearnHub" and "DocsWorld", dynamic content, decoy search box, hidden menu, duplicate links, sponsored trap, hanging URL) |
| Python deps | `pydantic`/`httpx`/`fastapi` unavailable offline → **test-only shims** in `tests/_shims` (the shim pydantic is minimal; real pydantic was not exercised) |
| OS window focus | Win32-only in the project; stubbed for Linux in the harness (CDP tab activation still real) |
| Live websites (Coursera, GitHub, …) | **ENVIRONMENT BLOCKED** (no network, `host_not_allowed`) |
| Real Gemini / real Ollama | **ENVIRONMENT BLOCKED** (no key, no network, Ollama not installed). Real model latency/quality is unmeasured |
| FastAPI server, `/api/health`, real WebSocket transport | **ENVIRONMENT BLOCKED** (`fastapi`/`uvicorn` not installable). The WS payload shapes were exercised through the orchestrator's `ws_manager.send` calls only |
| Frontend `npm install` / `vite build` / UI in a browser | **ENVIRONMENT BLOCKED** (no npm registry). TypeScript was type-checked against hand-written ambient stubs for react/lucide/framer-motion; errors in untouched files caused by the stubs were ignored |

**Nothing here is evidence that Coursera or any live website works.**

## Results

* `python -m unittest tests.test_adaptive_units tests.test_regressions` — **75/75 PASS** (27 control-logic + 26 regression + 22 goal tests (`tests.test_goals`: decomposition, per-goal verification, task state, block detection, no-website-names / no stale-model hygiene); the first two groups cover: `_CLAUSE_SPLIT`/undefined names, placeholder, extraction provenance, navigation preservation, page identity, typing parser/composer choice, recovery ladder, Gemini 404, config consistency).
* `node --experimental-strip-types --test tests/status.test.mjs` (frontend/) — **8/8 PASS** (`status.test.mjs` backend/browser/provider independence + `goals.test.mjs` goal panel derived only from recorded events).
* `tests/test_local_25_tasks.py` — **25/25 PASS**. **DETERMINISTIC LOCAL FIXTURE TEST**: still exercises only the deterministic planner + executor on one page; not the adaptive loop and not proof of arbitrary-web success.
* `tests/integration_adaptive_runs.py --all` — **48/48 scenarios PASS** (16 + 13 earlier + 19 goal-runner scenarios g01–g17) (each in its own process with fresh state). Raw JSON in `tests/results/`.

### Three independent runs

| Run | Task (fixture) | Route | Provider | Browser actions | Duration | Result |
|---|---|---|---|---|---|---|
| 1 | Open `<learn-site>/` | deterministic fast path | none (0 AI calls) | 1 | 1.8 s | success |
| 2 | Open site, go to Pricing, open Team plan details | initial URL bootstrap → adaptive | scripted Gemini (2 calls) | 4 (incl. bootstrap) | 5.4 s | success, ended on `/pricing/team` |
| 3 | Different site: open menu → Guides → Getting Started (hidden menu, duplicate Guides links, sponsored trap) | bootstrap → adaptive | scripted Gemini (3 calls) | 5 | 6.7 s | success, ended on `/guides/getting-started`, not on the trap |

### Failure injection (all with real Chrome, scripted providers)

| ID | Injected failure | Observed |
|---|---|---|
| f01 | Gemini quota | Gemini called once; Ollama (scripted) completed; task success |
| f02 | Gemini down, Ollama budget 2 calls, task needs 3 decisions | Ollama called exactly 2×; finished 6.9 s; recovery used; no retry storm |
| f03 | Ollama slower than its emergency budget (30 s sleep, 8 s budget) | called once, cut off at its budget, classified `timeout`; task failed in 10.5 s (not ~30 s+) |
| f04 | Ollama malformed JSON | called once, classified `malformed`, retired; failed in 2.3 s |
| f05 | Planner claims `done=true` once | verifier rejected (1 false-done); loop continued; success |
| f06 | Planner always claims done | Gemini 2 calls, Ollama 1; failed honestly in 2.8 s with explanation (3 false-done claims) |
| f07 | Planner repeats a no-op click | 2×`NO_PROGRESS` recorded; stopped by the pre-existing repeated-action guard after 4 actions; failed in 5.7 s |
| f08 | 25 s task timeout (4 s margin), planner 8 s/call | internal deadline; no Ollama call started with insufficient AI budget; ended 9.8 s |
| f09 | Navigation to a never-answering URL | failed in ~12.7 s (bounded by executor timeout + 8 s focus cap + 2 s title cap) |
| f10 | Gemini 503, Ollama not configured | called once; failed in 2.3 s naming providers |
| f11 | Planner returns 2 actions | decoy second action never executed; success |
| f12 | Search task with decoy footer search box, delayed results | success through the existing deterministic search/result path (0 AI planning of the search itself) |
| f13 | Second task in same process after Gemini quota | Gemini not called again (cooldown across tasks); Ollama planned |

Caveats: scripted providers answer in ~0 ms, so the AI-latency statistics are meaningful only in the scenarios that inject delay (f03, f08). Gemini-quota/timeout/malformed *error strings* are those the project's own provider code raises; the real SDK's error shapes are unverified.

## Defects the new tests found in the original code (fixed)

1. An instruction containing a URL plus further goals was reported **success** immediately after the first page load (fast path treated it as a complete one-step workflow).
2. Navigation-style goals were considered complete after "any action + any page" (`_page_proves_final_goal`).
3. `page.title()` / `focus_current_page()` were unbounded and delayed failure reports by ~70 s after a hung navigation.

## Not done / open

* No real Gemini/Ollama/live-site run (see above). Whether Qwen3:4B or your Gemini model can plan Coursera is untested here.
* Recovery level 4 (screenshot/visual inspection) is not implemented.
* Windows foreground-window logic (`window_focus.py`) is untested here.


## Second-round scenarios (all LOCAL FIXTURES that imitate behaviours; real browser + real orchestrator, scripted providers)

| Requested test | Scenario | Result |
|---|---|---|
| 1 Wikipedia search + article | n01 (search submit redirects straight to the article) | PASS - success, **0 AI calls**, did not click the already-open article |
| 2 GitHub search + repo | no GitHub fixture; generic search/result path covered by f12 and the 25-task test | **ENVIRONMENT BLOCKED** for real GitHub (generic path PASS on fixtures) |
| 3 YouTube search + video | same as 2 | **ENVIRONMENT BLOCKED** for real YouTube |
| 4 Wikipedia first heading | n02 | PASS - result exactly "Artificial intelligence" (h1), not the paragraph; 0 AI calls |
| 5 Coursera search + first course | n03 (results open in a NEW TAB) | PASS on fixture - new tab adopted, ended on the first course, no navigation after adoption, link clicked once, 0 AI calls. **Real Coursera: ENVIRONMENT BLOCKED** |
| 6 ChatGPT type hello | n04a/b/c (3 composer variants) + n04d (login wall) | PASS - text verified in the composer, nothing sent, 0 AI calls; login wall reported as blocked. **Real chatgpt.com: ENVIRONMENT BLOCKED** |
| 7 Generic new tab | n05 (fast), n05b (opens 1.5 s after click) | PASS - adopted and continued from the new tab |
| 8 `_CLAUSE_SPLIT` | tests.test_regressions.ClauseSplitRegression | PASS |
| 9 Provider unavailable | f01, f10, n09 | PASS |
| 10 Ollama timeout | f03, f04, f02 | PASS - bounded |
| 11 Backend health UI | frontend/tests/status.test.mjs | PASS (mapping logic). **`/api/health` endpoint and the rendered header: ENVIRONMENT BLOCKED** (no fastapi/vite) |
| 12 Repeated recovery | n07, f07 | PASS - ladder: other-pages check → alternate target → scroll → honest failure; no action repeated more than twice |
| 13 Bootstrap preservation | n08 + NavigationPreservation unit tests | PASS - root navigation from a deeper page skipped |
| 14 `[a site]` | n06 + PlaceholderRegression | PASS - fails immediately, no browser action, no Google search |

### What is NOT proven
* Anything on the real Wikipedia, GitHub, YouTube, Coursera or ChatGPT sites, and real Gemini/Ollama behaviour.
* That the real `/api/health` endpoint and rendered UI behave as the unit-tested mapping does (FastAPI/Vite could not run).
* Why the user's real log showed `NameError: _CLAUSE_SPLIT`: the log was not available here. The delivered ZIP defined it; stale `__pycache__`, a second project copy, or a mixed-file copy are the candidates. The startup diagnostic added in this round will show the running paths.
* In n08 the planner's repeated "go to homepage" requests end in an honest failure after the navigation is skipped; the page itself is preserved.


## Third round: goal-state architecture (LOCAL FIXTURES; real browser + real orchestrator + scripted providers)

| Spec test group | Scenario(s) | Result |
|---|---|---|
| Multi-step: two searches with back | g01 (typed texts asserted to be exactly the two queries; 4 goals verified; real history) | PASS, 0 AI calls |
| search → click → back → forward | g02 | PASS (history verified against predicted URLs) |
| search → new tab on another named site → search | g03 (Enter does not submit there; button fallback used) | PASS; 2 tabs, first tab untouched |
| Scroll down/up, find-after-scroll, click | g04 | PASS, bounded actions |
| Type into contenteditable / textarea / bare contenteditable, press Escape (no Enter) | g05a/b/c + n04a-d (now strict: composer, not the search box) | PASS |
| Type then Enter | g06 | PASS |
| Delayed new tab (opens 1.5 s after click) / unexpected new tab then continue | g07, g08 | PASS (decoy never clicked; follows active page) |
| Wrong first target (dead link) | g09 | PASS via alternate target; no target clicked twice |
| Dead click, no AI | g10 | PASS: clicked once, honest failure, provider calls bounded |
| Missing target + AI unavailable | g11 | PASS honest failure |
| Unknown clause: AI unavailable / AI helps | g12 / g13 | PASS; AI done-claim judged from evidence, limitation reported |
| CAPTCHA that clears / never clears | g14 / g15 | PASS: waits for the user, then continues / reports BLOCKED |
| Explicit later navigation honoured | g16 | PASS |
| Switch / close tab | g17 | PASS (1 tab left, active = first site) |

### Not tested / not done
* **Real Google, YouTube, Wikipedia, Coursera, ChatGPT, GitHub: ENVIRONMENT BLOCKED** (no network). Fixtures imitate behaviours (Enter-submit, JS-submit, results opening in new tabs, composer variants, CAPTCHA text), not the sites.
* Spec items not implemented: a separate FOCUS action and a dedicated SELECT goal (the schema's select_option exists but no goal parses it); screenshot-based recovery.
* Simple tasks (open / one search / open first result / typing / extraction) still use the earlier single-flow path, not the goal runner (documented in ARCHITECTURE §15).
* ADAPTIVE (free-form) goals are verified weakly; the run's message says so.
* Observed once during this round: a leftover Chrome from a killed run could hang the test driver; the harness now isolates each scenario in its own process group.

---
## Recovery round (after sandbox reset) — what was re-verified

Source of truth: the v3 tree. Round-4 files lost in the reset were NOT assumed to exist; the work below was rebuilt and re-run.

| Suite | Result | Label |
|---|---|---|
| Backend unit tests (`test_adaptive_units`, `test_regressions`, `test_goals`, new `test_recovery_round4`) | 97 run, 97 OK | PASS |
| Browser scenarios on local fixtures (real Chrome, scripted providers) | 51 / 51 | PASS (fixtures only) |
| New scenarios r01 (search skips newsletter e-mail), r02 (exact e-mail into the email field), r03 (quoted text with comma + "then" intact) | 3 / 3 | PASS (fixtures only) |
| Live Google / Wikipedia / GitHub / YouTube / Coursera / ChatGPT | not run | ENVIRONMENT BLOCKED |
| Real Gemini / Ollama behaviour | not run (scripted providers) | ENVIRONMENT BLOCKED |

Not recovered: scenarios s01–s16, g18, g19 and the frontend tests from the lost round; screenshot-based recovery was never implemented.
