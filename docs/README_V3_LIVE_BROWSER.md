# Browser Automation Agent — Live Browser Edition

A local natural-language browser agent that plans a task, controls a **real visible Chrome tab**, and then leaves that tab open for the user.

## Core behavior

```text
VOICE / TEXT TASK
      ↓
WORKFLOW MEMORY / FAST PATH
      ↓
LOCAL OLLAMA (preferred) / GEMINI FALLBACK
      ↓
PLAYWRIGHT
      ↓
NEW MAXIMIZED CHROME TAB
      ↓
AUTOMATION
      ↓
TAB STAYS OPEN + USER TAKES OVER
```

### What changed from the screenshot-preview design

The old version took one screenshot at the end and closed the browser context. That made the result look frozen and prevented manual interaction.

This version:

- launches a visible browser (`BROWSER_HEADLESS=false`)
- prefers the installed Google Chrome channel
- starts the browser maximized
- creates a new tab for every task
- keeps the browser context alive for the backend session
- keeps the completed/failed tab open
- preserves cookies/session state across task tabs
- exposes a **Focus live browser** control in the UI
- does not pretend that a screenshot is interactive
- keeps a screenshot only as optional task-history data

On Windows, the backend also activates and maximizes the Chrome OS window after the task reaches its destination, and explicitly selects the task tab. The user does not have to click the new tab manually. The user can then click, type, scroll, sign in, and continue the website task.

## Voice input

The frontend uses the browser's Web Speech API when available (Chrome is recommended).

Voice recognition is configured as continuous with interim results. If Chrome ends recognition after a short pause, the app restarts recognition automatically while listening and keeps the transcript in the task box. **Nothing is submitted until the user presses Run task.**

## Reliability changes

- Navigation timeout: 15 seconds by default.
- Action timeout: 7 seconds by default.
- One bounded element-resolution retry.
- Task deadline: 30 seconds by default for AI planning + browser work.
- A timeout does **not** close the live tab.
- WebSocket status updates have HTTP polling fallback.
- Cancel actually cancels the background asyncio task.
- Google bot/challenge pages are still reported as failures instead of being called success.
- A generic `Search ...` task uses DuckDuckGo rather than automatically sending the user to Google's automated-traffic challenge page. If the user explicitly asks for Google, the task still targets Google and reports Google's response honestly.

## Supported fast-path sites

Google, YouTube, GitHub, Wikipedia, Reddit, Amazon, LinkedIn, X/Twitter, Bing, DuckDuckGo, and Stack Overflow.

Examples:

- `Open GitHub`
- `Open YouTube and search Python tutorials`
- `Open Wikipedia and search artificial intelligence`
- `Search Python interview questions`
- `Open https://example.com`

For tasks outside the deterministic fast path, the default `auto` provider prefers a local Ollama model. This is intentional: local AI can perform repeated complex-task replanning without consuming Gemini API requests. Gemini remains an optional fallback.

### Zero-credit local AI setup

Install Ollama on the Windows machine, then pull the configured model:

```powershell
ollama pull qwen3:4b
```

Keep Ollama running and use:

```text
AI_PROVIDER=auto
OLLAMA_MODEL=qwen3:4b
AUTO_PREFER_LOCAL=true
```

To force local-only operation:

```text
AI_PROVIDER=ollama
```

To use Gemini only:

```text
AI_PROVIDER=gemini
GEMINI_API_KEY=your_key
```

A Gemini daily/request quota is a provider-side limit; NEXA cannot legitimately bypass it. The local provider is what allows many complex tasks without spending hosted AI credits.

## Safety boundary

The agent does not execute tasks containing sensitive action hints such as purchase, checkout, payment, delete, send, transfer, or place-order instructions without an approval flow. This is intentionally fail-closed.

## Project layout

```text
backend/
  main.py
  config.py
  api/tasks.py
  agent/
    action_schema.py
    orchestrator.py
    planner.py
    recovery.py
    router.py
    validator.py
  browser/
    manager.py
    executor.py
    inspector.py
    element_resolver.py
    download_manager.py
  services/
    ai_provider.py
    ws_manager.py
  database/
  models/

frontend/
  src/
    pages/Home.tsx
    pages/History.tsx
    components/
    hooks/useTaskExecution.ts
    services/api.ts
```

## Windows setup

### 1. Backend

Use Python 3.12:

```powershell
cd backend
py -3.12 -m pip install -r requirements.txt
py -3.12 -m playwright install chromium
copy .env.example .env
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The backend should show:

```text
Browser launched and ready.
Uvicorn running on http://127.0.0.1:8000
```

### 2. Frontend

Open a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173/
```

### 3. Claude (optional)

Put the key in `backend/.env`:

```text
CLAUDE_API_KEY=your_key_here
```

The fast path works without Claude for supported open/search tasks.

## Important usage note

The live browser is an operating-system Chrome window, not an iframe inside the React page. This is intentional: arbitrary external websites cannot reliably be made into an interactive iframe, and an image/screenshot can never accept real clicks or typing.

After a task finishes, use **Focus live browser** or switch to the maximized Chrome window. The React app remains the control console.

Stopping the backend intentionally closes its browser session.


## Architecture

See `ARCHITECTURE.md` for the complete component map and the quota-saving execution design.
