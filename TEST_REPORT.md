# NEXA v4 — test report

Labels: **PASS** = ran and passed here. **FAIL** = ran and failed. **ENVIRONMENT BLOCKED** = could not be run in the build
environment (no package registry, no keys, no live-web access); *not* evidence that it works or fails.

## What the tests are (and are not)

Integration scenarios use the **real** orchestrator, router, planner, inspector, resolver, executor, verifier, budgets,
provider-health manager, event recorder and a **real headless Chrome** over CDP — against **local fixture websites** with
**scripted fake AI providers**. They prove NEXA's logic, safety gates and recovery. They do **not** prove the behaviour of real
Gemini/Ollama/OpenAI/Claude/Grok models or of real websites.

## Backend unit tests — PASS (174 tests; 151 before the audit round)

| Module | Tests | Result |
|---|---|---|
| `test_providers` (adapters, config, Auto routing, fallback, cooldown, malformed, timeout, redaction, status safety) | 29 | PASS |
| `test_understanding` (understanding, safety gate, AI-output validation) | 25 | PASS |
| `test_adaptive_units` (planner loop, budgets, health) | 27 | PASS |
| `test_goals` | 22 | PASS |
| `test_regressions` | 26 | PASS |
| `test_recovery_round4` | 22 | PASS |
| `test_nl_routing` (audit round: fast-path domains, say/tell/ask, politeness, speech variants, login, outcome labels, paraphrase dataset) | 17 | PASS |
| `test_dataset_catalog` | 6 | PASS |
| `test_dataset_api` | 1 | **ENVIRONMENT BLOCKED** (needs `fastapi`, not installable here) |
| `test_dataset_verifier` | 0 collected | n/a (script-style module) |

## Integration scenarios (real Chrome, fixtures) — PASS (68/68)

Full run of 68 scenarios (64 earlier + `m14` + audit-round `p01`-`p03`): all pass. One existing check (`f10`) was updated on purpose: with v4's bounded
transient retry, Gemini 503 now means *one call + exactly one retry* (2 calls, never more) instead of 1.
Quota (429), auth (401/403), malformed output and timeouts are never retried.

## The 14 required natural-language regression tests

| # | Case | Coverage | Result |
|---|---|---|---|
| 1 | YouTube search "Python tutorials" | `test_understanding::01`, `test_goals` (parsing + goal plan). Real YouTube **not** run | PASS (parse) / ENVIRONMENT BLOCKED (live) |
| 2 | First relevant Python tutorial open | `test_understanding::02`; fixture `n01`/`g09` (first-result open + dead-link recovery) | PASS (fixture) |
| 3 | Gmail compose to Rahul saying hello | `test_understanding::03`; fixture `m01` (draft verified in composer). Real Gmail **not** run | PASS (fixture) / ENVIRONMENT BLOCKED (live) |
| 4 | Wikipedia AI first heading | fixture `n02` (h1 text, not paragraph) | PASS (fixture) |
| 5 | Go back + second result | `test_understanding::05`; fixture `m14` (real history, 0 AI calls) | PASS (fixture) |
| 6 | GitHub browser automation search | `test_understanding::06` (parse). Real GitHub **not** run | PASS (parse) / ENVIRONMENT BLOCKED (live) |
| 7 | Two matching contacts → clarify | fixture `m02` (pause → answer → resume), `m09` (unknown contact) | PASS |
| 8 | Compose but do not send → unsent | fixture `m03`, `test_understanding::08/08b` | PASS |
| 9 | "Send this email." → confirmation | fixture `m06`, `m04` (approve), `m05` (decline) | PASS |
| 10 | Quota failure → eligible fallback | `test_providers::quota_failure_falls_back…`; fixtures `f01`, `f13` | PASS |
| 11 | All providers unavailable | `test_providers::all_providers_unavailable…`; fixtures `f10`, `g11`, `g12` | PASS |
| 12 | Missing/disabled control | fixture `m13`, `g11`, `g10` | PASS |
| 13 | Login/CAPTCHA | fixture `m12` (login wall), `g14`/`g15` (CAPTCHA) — reported as blockers, never bypassed | PASS |
| 14 | Technical success without the outcome | fixtures `f05`, `f06` (false "done"), `m07` (Send clicked, nothing sent → failure), `g10` | PASS |

## Frontend

| Check | Result |
|---|---|
| `npm test` (15 logic tests: paused states, provider selector, "handled by", status chips, phases, no secrets/localStorage/URL keys in `src`) | PASS |
| Strict TypeScript check of all `.ts` files and the new/changed `.tsx` components against stubbed React types | PASS (stub limitations only) |
| `npm install`, `npm run build` (Vite), visual check in a browser | **ENVIRONMENT BLOCKED** (npm registry blocked/crashed) |

## Not run at all (ENVIRONMENT BLOCKED)

* Live calls to Gemini, Ollama, OpenAI, Anthropic Claude, xAI Grok (adapters tested with fake transports built from each vendor's documented request/response shapes).
* Real websites (Gmail, YouTube, Wikipedia, GitHub, …).
* Starting the FastAPI server (`uvicorn main:app`) and the HTTP/WebSocket layer end to end (`fastapi` not installable here); the router code is syntax-checked only.
* Browser Use Cloud (not implemented).

## Known limitations

* Draft persistence in a real mailbox (autosave) is not independently verified; the composer contents are.
* `looks_sensitive` still refuses purchase/delete/transfer-type instructions outside the compose/send flow.
* Ollama is not used for request understanding or email-body authoring.
* `/api/providers` reports Ollama "available" from configuration, not a live ping.
* SELECT handles native `<select>` only.

## Audit round (A–Z audit prompts) — what was found, fixed and measured

**Reproduced on the delivered v4 ZIP (before any change):**

| Input | v4 behaviour (BUG) |
|---|---|
| `open chatgpt and say hello` | legacy `try_fast_path` -> navigate `https://chatgptandsayhello.com/`; goal parser -> ONE click goal `"chatgpt and say hello"` |
| `open flipkart and fill the login form` | `https://flipkartandfilltheloginform.com/` |
| `open wikipedia search databases open the first article and tell me the first heading` | a 90-character invented domain |

**Root causes and fixes**

1. `planner._extract_unknown_site_target` excluded only a few verbs and its `.com` guesser accepted any text up to 80 characters; the orchestrator then treated that navigation as a *complete* workflow. Fix: shared `is_plain_site_name` / `looks_like_sentence` (a destination is a short name; a sentence never is) and a router safety net: a single navigation is complete only when the whole instruction is just "open <destination>".
2. `goals.py` did not know `say / tell it / ask it / fill / report` as clause starters, so `and say hello` was not split off. Fix: new say/tell/ask clause (type the words, then Enter, verified separately), extra verbs, polite-wrapper stripping ("hey nexa, could you … for me, please?"), unpunctuated speech ("open youtube search python open the first video"), "go on <site>", "my gmail", spoken "dot com", "you tube", "g mail", "open a good result" as a result pick, and a sentence is never a click target (it goes to the planner).
3. Login requests were reduced to a bogus click. Fix: `login_assist` intent: open the known site, click its sign-in control, and say plainly that NEXA never types passwords/one-time codes and does not pass CAPTCHA/MFA. A URL containing "login" is not a login request (found by `p03`).
4. No distinction between partial and blocked runs. Fix: `outcome` (`success | partial | blocked | failed | cancelled | awaiting_user`) derived only from recorded events; shown in the UI.

**Evidence (all fixture-based; no real ChatGPT/Gmail/Amazon/Flipkart was contacted):**

| Check | Result |
|---|---|
| Original failing inputs no longer produce garbage domains; simple `open chatgpt` / `open ecart` still use the fast path | PASS (`test_nl_routing`) |
| `open <chat fixture> and say hello`: types `hello`, presses Enter, message appears once, 0 AI calls (`p01`) | PASS (real Chrome) |
| `Can you open <chat fixture> and tell it hello for me?` on a late-hydrating composer (`p02`) | PASS (real Chrome) |
| `say hello` on a login wall: reported as blocked, nothing sent (`p03`) | PASS (real Chrome) |
| Language-layer paraphrase dataset (`tests/nl_dataset.py`): supported rows understood | **27/27**; 2 documented gaps ("Find me a Python tutorial and play it", "I want to learn Python…") are deliberately handed to the AI planner |
| Frontend logic tests | PASS (16) |
| The new unit tests against the ORIGINAL v4 code | FAIL (import/behaviour), as expected: the reproduction table above is the pre-fix evidence |

**Not implemented / not verified in this round (be aware):**
* Real ChatGPT, Gmail, Amazon, Flipkart, YouTube, GitHub, Wikipedia: **NOT RUN** (no live-web access); real sites need authentication or hit CAPTCHAs that NEXA reports instead of bypassing.
* Amazon/Flipkart **form filling with supplied credentials: NOT implemented** — by design NEXA only opens the sign-in page (see `login_assist`).
* Conversation follow-ups across tasks ("Actually open the second result", "What happened?", "Change the message to…"): **NOT implemented** (a task is one instruction; `POST /tasks/{id}/resume` continues a failed/timed-out task from the live tab).
* Optional second-provider plan review/ensemble: **NOT implemented** (single primary provider with ordered fallback).
* Real provider API calls (Gemini/Ollama/OpenAI/Claude/Grok): **NOT RUN**.


## Contract / Groq / Supabase round — results actually run in the build environment
| Suite | Result |
|---|---|
| Backend unit (`python -m unittest discover -s tests -p "test_*.py"`) | **PASS** 255 / 256 — the one error is `test_dataset_api`, which cannot import `fastapi.testclient` here (ENVIRONMENT) |
| New: `test_contract` (29), `test_groq` (23), `test_supabase` (29) | **PASS** |
| Real headless Chrome, fixture sites, scripted fake providers (`integration_adaptive_runs.py --all`) | **PASS** 74 / 74, including s01 phone filled+verified+not submitted, s02 no field → blocked, s03 ambiguous → asks, s04 page drops value → not verified, s05 password never entered, m09 un-selected suggestion is not a recipient |
| Frontend (`node --test`) | **PASS** 10 / 10 |
| Frontend TypeScript (global `tsc`, React/lucide stubs, strict) | **PASS** for services, lib, types, hook, AuthMenu, History (stub-only implicit-any noise excluded) |
| FastAPI endpoint tests (auth 401/503, WS auth, REST/WS agreement, cross-user 404) | **NOT RUN** — fastapi test client unavailable; API files are syntax-checked only. The logic they call (`services/auth.py`, `persistence`, `supabase_service`) is unit-tested with mocked transports |
| `supabase/tests/rls_isolation.sql` | **NOT RUN** — no Postgres here; run it in your Supabase SQL editor |
| `npm run build` | **ENVIRONMENT BLOCKED** (npm install not possible here) |
| Live Groq / Gemini / other providers, live Amazon / Gmail, real Supabase project | **NOT RUN** — no keys, no network to those sites; all provider tests use mocked transports |
