# Changes — adaptive loop fix

Each entry: **OLD → NEW → WHY → RESULT**. "Verified" statements refer to `TEST_REPORT_ADAPTIVE.md`
(local fixtures + scripted providers; no live-site or real-LLM verification was possible in the build environment).

### 1. Repeated Ollama calls
* **OLD** When Gemini was unavailable, Ollama became the planner for every adaptive step (up to 24 steps) with only a 30 s per-provider cooldown.
* **NEW** Ollama is an emergency provider: ≤ `OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK` requests (default 2) and ≤ `OLLAMA_EMERGENCY_BUDGET_SECONDS` (default 30 s) per task; any timeout/malformed/error/false-done retires it for the task.
* **WHY** Qwen3:4B took ~46–56 s per planning cycle; ~6 cycles consumed the whole 360 s deadline.
* **RESULT** A slow local model cannot consume the task deadline; the agent falls back to generic DOM recovery or fails fast with a diagnostic.

### 2. Ollama repair path bypassed limits
* **OLD** Malformed output triggered a second request with the large prompt and a 30 s timeout (20 s + 30 s ≈ 50 s).
* **NEW** `OllamaProvider._generate_bounded`: initial + at most one repair, both using the compact live prompt, both charged to `CallLimits` (request count, per-request timeout, total time).
* **WHY** Structural: repair must not bypass the emergency budget.
* **RESULT** Worst-case emergency call time is bounded by `OLLAMA_EMERGENCY_BUDGET_SECONDS`.

### 3. False `done=true` loop
* **OLD** Verifier rejection set `force_ai_replan=True` and called the same provider again; `repeated_done_claims` was never enforced and reset after any successful action.
* **NEW** Per-task and per-provider false-done tracking, identical-input protection, strategy switch (semantic extraction → generic DOM recovery), provider distrust, and honest failure after `MAX_FALSE_DONE_CLAIMS`. Never reset by unrelated successes.
* **RESULT** A planner that always claims "done" ends in a bounded, explained failure (verified in scenario f06).

### 4. No deadline awareness
* **OLD** One outer `asyncio.wait_for`; nothing inside knew the remaining time.
* **NEW** `TaskBudget` with AI / browser / recovery budgets checked before every AI call, action and recovery; internal deadline before the outer hard timeout; timeout message states where time went.
* **RESULT** No AI call starts without budget; the task ends gracefully with diagnostics (scenario f08).

### 5. Multiple adaptive actions per AI response
* **OLD** `_MAX_ACTIONS_PER_CHUNK = 2` (TYPE/SCROLL/EXTRACT could be followed by a second action planned on a stale DOM).
* **NEW** Exactly one action per response; extras discarded (scenario f11 proves the decoy second action never runs).

### 6. Weak progress detection
* **OLD** Identical-state counter (recovery after 5 identical states); any successful action reset all failure counters.
* **NEW** Before/after classifier with six verdicts; goal relevance from the user's own words; counters reset only on real progress; `go_back` recovery for wrong-state pages.

### 7. Verification assumed search → result → extraction
* **OLD** Contract fields from fixed keywords; "any action + any page" completed navigation-style goals.
* **NEW** Added generic checks: ordered goal-clause evidence (`_unsatisfied_goal_clauses`) and goal-vocabulary coverage (`_goal_evidence`). The old contract logic is kept.
* **Defect found by the new tests:** an instruction containing a URL plus further goals ("Open http://x, go to Pricing…") was reported **SUCCESS right after the first page load**. Now the navigation is only a bootstrap and the adaptive loop continues.
* **Limitation:** clause matching is lexical; goals that cannot be proven are reported as not proven rather than succeeded.

### 8. Missing frontend observability / fake progress
* **OLD** The UI showed a fixed "Opening your live Chrome tab…" banner for the whole run and `actions_count || 1` ("1 browser action" with zero actions).
* **NEW** Structured events over WebSocket/polling; Agent status, provider status, Automation chart, Execution timeline; action count from recorded events only (hidden when unknown).

### 9. Provider configuration contradictions
* **OLD** Docs said Ollama primary, code comments said Gemini, defaults made Ollama first; `GEMINI_MODEL` was silently rewritten.
* **NEW** Gemini primary everywhere (`AUTO_PREFER_LOCAL` default false); `GEMINI_MODEL` used exactly as configured.

### 10. Unbounded browser housekeeping
* **OLD** `page.title()` and `focus_current_page()` had no timeout; after a hung navigation the failure was reported ~70 s late.
* **NEW** `_safe_title` ≤ 2 s, `focus_current_page` ≤ 8 s.

---
## Second repair round (runtime-log driven)

Note: the execution log itself was never received in this environment; fixes below follow the defects described in the request and were each reproduced on local fixtures. Where the real log could disagree, it is flagged in TEST_REPORT_ADAPTIVE.md.

### 11. `_CLAUSE_SPLIT` NameError / running a different copy
* **OLD** The regex lived in `orchestrator.py`; a stale/partial copy failed mid-task with `NameError`.
* **NEW** `agent/goal_clauses.py` (imported at startup) + startup `NEXA_RUNTIME` diagnostic + fail-fast integrity check + unit test that scans backend modules for undefined names.
* **WHY** Import errors at startup are loud; NameErrors in a rarely-hit function are not. (The delivered ZIP did define the constant; the real cause of the log's NameError is unproven without the log - likely stale/mixed copy.)

### 12. Gemini model configuration
* **OLD** `config.py` default `gemini-3.8-flash`, `.env.example` `gemini-2.5-flash` (404 for new keys).
* **NEW** One value, equal in both (test enforces it); 404/NOT_FOUND/"no longer available" is classified `model_unavailable`: Gemini is retired for the task and cooled down for `GEMINI_MODEL_ERROR_COOLDOWN_SECONDS` (default 1 h).
* **Unverified:** that `gemini-3.8-flash` exists for your key. Confirm it.

### 13. New tabs and a stale local Page
* **OLD** Executor adopted a popup into the browser manager only; the orchestrator kept the old Page.
* **NEW** See ARCHITECTURE §10. Verified on fixtures: fast new tab, new tab opened 1.5 s late, tab-per-result-link.

### 14. Destroying a successful page / repeated recovery click
* **NEW** State-aware navigation + recovery ladder (ARCHITECTURE §11). Replay failure no longer closes the tab.

### 15. Result-open misdetection (found while testing)
* **OLD** `_is_search_submit_action` / `_is_result_open_action` used the free-text `reason`, so a result click whose reason said "…live search-results DOM" was classified as a search submit and never counted as opening a result (false "incomplete").
* **NEW** Classified by the clicked target; reason only when there is no target.

### 16. "First <thing>" picked the second item
* **OLD** The first-result ranking penalised longer titles, so for generic lists (courses/products) the second link beat the first.
* **NEW** For "open the first/top <generic thing>" DOM order wins; video/article/repository ranking is unchanged.

### 17. Wikipedia-style extraction false success
* **NEW** Provenance-checked extraction (ARCHITECTURE §13). A heading request can only be answered from h1–h6.

### 18. `[a site]` placeholder
* **NEW** `agent/placeholders.py`: unfilled placeholders fail immediately with an explanation, no browser action, no Google search.

### 19. Deterministic typing (ChatGPT-style)
* **NEW** ARCHITECTURE §12. 0 AI calls on fixtures for textarea / contenteditable-role / bare-contenteditable composers.

### 20. UI status
* **OLD** One `healthy`/`backendOnline` flag pair.
* **NEW** Backend / Browser / Gemini / Ollama as independent states; "offline" only after 3 consecutive failed health polls; `/api/health` now includes provider status.

---
## Third repair round (generic goal-state architecture)

### 21. Whole sentence treated as one blob (multi-step tasks)
* **OLD** `_query_from_instruction` / `_extract_search_queries` captured "Python multithreading tutorials, then go back and search for Python multiprocessing tutorials" as ONE query (reproduced before the fix).
* **NEW** `agent/goals.py` decomposes into ordered goals; both legacy extractors now take the clean query from it. Control words cannot reach a text field.
* **Principle missing before:** task decomposition / goal state.

### 22. No goal-level state, back/forward/tab/scroll primitives
* **NEW** `agent/goal_runner.py`, `agent/task_state.py`, `agent/goal_verify.py`; executor/manager primitives (back, forward, new/switch/close tab, scroll) with per-action evidence; one page-lifecycle API in `BrowserManager`.

### 23. Bugs found by the new tests (all fixed)
* **Composer selection**: a site's *search box* could win as "message composer" whenever the real composer had not rendered yet (text typed into the wrong field). The earlier ChatGPT-style tests only checked "some input contains the text" and could not see it; they now check the composer specifically and that the search box stayed empty. Search boxes are excluded outright.
* **Back/forward 4 s stall**: `wait_for_load_state` never re-fires after a back/forward-cache restore; replaced by bounded `readyState` polling (4.2 s → ~0.2 s per navigation).
* **Result-open detection**: a result whose URL kept the `?q=` query string was judged "still a search page"; now compares host+path.
* **CAPTCHA false positive**: an article *about* CAPTCHAs was flagged as a challenge page; weak keywords now count only on near-empty pages.
* **Website names in the generic parser** (`google` as a search verb): removed; a test now forbids website names in the goal modules.
* **Harness**: a Chrome left running could hang the test driver via an open pipe; children now run in their own process group with file output.

### Known limitations of this round
* Free-form (ADAPTIVE) goals can only be verified weakly; the final message says so.
* Per-goal verification of CLICK on a non-ordinal target (e.g. "click Pricing") accepts any observable page change.
* Live-site behaviour (Google/YouTube/Wikipedia/Coursera/ChatGPT) was not testable here.

## Recovery round
- `agent/goals.py`: quoted literals are protected from clause splitting; `split_type_clause` separates literal text from the target description ("into the email field"); TYPE goals carry `target`; `needs_goal_runner` routes targeted typing (or typing in 3+ goals) to the goal runner.
- `agent/goal_runner.py`: search and typing use `field_ranker.rank_fields` with read-back verification of the chosen field and fall-through to the next candidate; search results are classified by `search_outcome.classify_search_outcome` (event `scope=search_outcome`).
- `agent/field_ranker.py`, `agent/search_outcome.py` now wired in (previously unused); `agent/task_state.py` records `last_search_outcome`; `browser/inspector.py` exposes label/type/form-hint evidence.
- Tests: `tests/test_recovery_round4.py`, fixture `forms`, scenarios r01–r03.
