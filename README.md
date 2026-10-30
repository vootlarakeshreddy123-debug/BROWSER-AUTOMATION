# NEXA v4 — natural-language browser automation agent

NEXA turns a plain-English request into verified actions in a **real Chrome tab** (Playwright over CDP) and
leaves the tab open so you can take over. v4 upgrades the existing NEXA project (same repo, same API, same UI shell):

* **Understanding layer** – a structured, schema-validated reading of the request (objective, ordered subgoals,
  entities, missing information, expected result, confirmation flags). Deterministic fast paths first; one bounded
  AI call only when the wording is unclear. It never splits blindly at "and/then" ("search for cats and dogs" is one query).
* **Six AI providers** behind one registry/interface: **Groq (first in AUTO), Gemini, Ollama, OpenAI, Anthropic Claude, xAI Grok** (Groq = groq.com, not Grok/xAI).
* **Task contract + verified completion, optional Supabase sign-in/history** — see `README_V4_CONTRACT_SUPABASE.md`.
* **Observe → decide → resolve from the live DOM → validate → execute → verify → recover.** "Done" is never trusted;
  the outcome is checked against the page.
* **Forms / email composer** strategy that finds fields from the live DOM (no hard-coded Gmail selectors), selects
  autocomplete recipients from on-page evidence, and asks you when a recipient is ambiguous.
  "Compose" means a **verified draft**; it is never sent without your explicit confirmation.
* **Safety gate** – explicit confirmation before send / publish / purchase / delete / sensitive submit.
  Login walls and CAPTCHAs are reported as blockers; NEXA never tries to get around them.
* **Frontend** – natural-language input, Run/Stop/Clear, provider selector + status, live progress, active page,
  completed / failed / blocked / awaiting-confirmation / needs-input states, timeline, history,
  and which provider actually handled each task.

## Plain English is the interface
Type or speak normally: *"Can you open ChatGPT and say hello?"*, *"open youtube search python tutorials open the first video"*,
*"hey nexa open gmail and write an email to rahul@example.com saying hello"*. Politeness, missing punctuation and common
voice-transcript slips ("you tube", "g mail", "github dot com") are understood. Tasks that sound like a login ("fill in the Amazon
login form") open the site's sign-in page only; NEXA never types passwords or codes from a request. Not yet supported:
follow-ups that refer to an earlier task ("open the second result instead").

## Honest status of what is verified

| Area | Status |
|---|---|
| Backend unit tests (providers, understanding, planner, goals, regressions, recovery) | PASS (see `TEST_REPORT.md`) |
| Fixture-site integration scenarios (real orchestrator + real Chrome, scripted providers, local fixture sites) | PASS (see `TEST_REPORT.md`) |
| Frontend logic tests (`npm test`) | PASS |
| Live Gemini / Ollama / OpenAI / Claude / Grok calls | **NOT TESTED** – no keys or network access in the build environment. Adapters follow each vendor's documented REST API and are tested against fake transports. |
| Real websites (Gmail, YouTube, Wikipedia, GitHub …) | **NOT TESTED live.** Behaviour is proven only on local fixture sites. Real sites change; expect to tune. |
| `npm install` / `vite build` / FastAPI server start | **NOT RUN** in the build environment (package registry blocked). Run the commands below on your machine. |
| Browser Use Cloud | **Not implemented** (see below). Local Chrome only. |

## Install and start (Windows)

Requirements: Python 3.12, Node 20+, Google Chrome. Ollama is optional.

```bat
:: backend
cd backend
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
:: edit .env (see "Providers"), then:
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000

:: frontend (second terminal)
cd frontend
npm install
npm run dev
```

`START_BACKEND.bat` and `START_FRONTEND.bat` do the start step. Open the URL Vite prints (normally http://localhost:5173).

Other npm scripts: `npm run build` (typecheck + production build), `npm test` (frontend logic tests),
`npm run typecheck`. Backend tests: `cd backend` then `python -m unittest tests.test_providers tests.test_understanding tests.test_adaptive_units tests.test_goals tests.test_regressions tests.test_recovery_round4`.
Integration scenarios (need Chromium): `python tests/integration_adaptive_runs.py --all`.

## Providers

All keys live **only in `backend/.env`** (git-ignored). They are never sent to the frontend, written to logs,
events, task history or URLs; error text is passed through a redactor.

| Provider | Needs | Env vars |
|---|---|---|
| Gemini | API key | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_ENABLED` |
| Ollama (local) | Ollama running, model pulled | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_ENABLED` |
| OpenAI | API key + model | `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_ENABLED`, `OPENAI_BASE_URL` |
| Anthropic Claude | API key + model | `ANTHROPIC_API_KEY` (or `CLAUDE_API_KEY`), `CLAUDE_MODEL`, `CLAUDE_ENABLED` |
| xAI Grok | API key + model | `XAI_API_KEY`, `XAI_MODEL`, `XAI_ENABLED` |

* **Models are yours to choose.** Only Gemini ships a default name; model names and availability change, so set
  `*_MODEL` to one your key can use. A wrong name is reported in the task events, not hidden.
* **Selection.** `AI_PROVIDER=auto` (default) uses `AUTO_PROVIDER_ORDER` (default `gemini,ollama`). You can also pick a
  provider per task in the UI. A manually selected provider that is not configured is rejected up front; it never silently
  becomes another one.
* **Hosted providers are opt-in.** OpenAI/Claude/Grok are used only when `*_ENABLED=true` and a key is set, and Auto uses
  them only if you list them in `AUTO_PROVIDER_ORDER`. No silent failover to a paid provider.
* **Cost.** NEXA does not promise any hosted provider is free. Pricing, free tiers and quotas differ and change; check your
  own account. Ollama runs locally (hardware cost only). Limits you control: `HOSTED_PROVIDER_MAX_CALLS_PER_TASK`,
  `HOSTED_PROVIDER_TIMEOUT_SECONDS`, `HOSTED_PROVIDER_MAX_OUTPUT_TOKENS`, `TASK_TIMEOUT_SECONDS`, `AI_MAX_RETRIES`,
  `OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK`.
* **Status.** `GET /api/providers` (and `providers` inside `/api/health`) report configured / state / model / reason /
  `may_bill` – never a key. Ollama "available" reflects configuration, not a live ping.
* **Validated at startup** (names only are logged) and on selection.

### Ollama setup
```bat
ollama pull qwen3:4b
```
then set `OLLAMA_MODEL=qwen3:4b` (default) and keep Ollama running at `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`).
Ollama is used for planning steps only; it is **not** used for request understanding or for authoring email bodies.

### Fallback behaviour
Per task there is a shared deadline and AI-call budget. A provider that hits quota (429), auth failure (401/403),
timeout, or returns malformed output is recorded for that task and put on a cooldown (circuit breaker). The next eligible
provider in order is tried; the same failing request is not repeated, and not every provider is called every step. One bounded
retry is made only for transient 5xx/UNAVAILABLE. If every provider is unavailable the task says so honestly and continues
with deterministic DOM recovery where possible, or fails with a clear message.

## Confirmation, clarification and blockers

* A click on Send / Post / Publish / Pay / Delete / Submit-style controls pauses the task (**Awaiting confirmation**).
  *Yes* resumes in the same live tab; *No* ends it with nothing sent.
* "Compose an email to Rahul saying hello" fills a **draft** and stops, verifying the recipient/subject/body in the composer.
  "…but don't send it" is respected. "Send this email." asks for confirmation first.
* Ambiguous or missing details (two matching contacts, no address, missing event title) pause the task (**Needs your input**).
* Login walls and CAPTCHAs end the goal as **blocked**; the tab stays open for you. NEXA does not solve or bypass them.
* Text typed into password, card and OTP fields is masked in events, history and the UI (the value is shown as `[hidden]`).

API: `POST /api/tasks` (`instruction`, optional `provider`), `POST /api/tasks/{id}/run`, `POST /api/tasks/{id}/respond`
(`{"approve": true|false}` or `{"answer": "..."}`), `POST /api/tasks/{id}/cancel`, `GET /api/history`, `GET /api/providers`, `GET /api/health`.

## Browser modes

* **Local (implemented):** your Chrome via CDP, visible, persistent profile, tab left open.
* **Browser Use Cloud: not implemented.** `BROWSER_EXECUTION_MODE` and `BROWSER_USE_API_KEY` exist in `.env.example` only so
  the setting is explicit; any value other than `local` is reported in `/api/providers` (`browser_mode_note`), logged at
  startup, and local mode is used. Building it would need the vendor's API contract, which could not be verified offline.

## Troubleshooting

* *Provider shows "not configured"* – the key or model env var is empty, or `*_ENABLED` is false; the reason text names the variable.
* *Provider shows "cooling down"* – it recently hit quota/auth/timeout; it is retried after the cooldown (`*_COOLDOWN_SECONDS`).
* *Gemini 404 "no longer available"* – set `GEMINI_MODEL` to a model your key can use.
* *Task asks a question or confirmation* – answer it in the card; the run resumes with a fresh time budget.
* *Backend offline chip* – requires 3 failed health checks in a row; check the uvicorn window.
* *CAPTCHA / sign-in page* – complete it yourself in the open tab, then start the task again.
* *If a key was ever pasted into chat, a file, or committed* – rotate it at the provider.

## More docs
`ARCHITECTURE.md`, `CHANGES.md`, `TEST_REPORT.md`, `docs/README_V3_LIVE_BROWSER.md` (previous README), older notes in the `README_*.md` / `CHANGES_*.md` files.
