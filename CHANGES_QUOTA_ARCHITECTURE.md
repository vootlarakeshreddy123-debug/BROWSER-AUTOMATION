> **SUPERSEDED.** This describes an earlier design in which Ollama was the primary planner. The current design is Gemini-primary with a bounded Ollama emergency fallback — see `ARCHITECTURE.md` and `CHANGES_ADAPTIVE_FIX.md`.

# NEXA — Quota Architecture Fix

## Problem

The previous architecture could call Gemini once for navigation and then once for each live browser action. Complex tasks therefore consumed the hosted-model quota very quickly.

## Changes

- Added `OllamaProvider` for local AI planning.
- Added `AutoProvider`.
- `AI_PROVIDER=auto` now prefers local Ollama.
- Gemini fallback is disabled by default in auto mode so the daily quota is protected.
- Added `AUTO_ALLOW_GEMINI_FALLBACK=true` as an explicit opt-in.
- Reused the existing NEXA prompt and structured action parser for Ollama.
- Added Ollama settings to `backend/config.py` and `.env.example`.
- Added `ARCHITECTURE.md` documenting the complete architecture and target.
- Updated README setup instructions.

## Result

With Ollama running, the existing adaptive loop can perform repeated DOM observations and AI replanning locally without consuming Gemini API requests.

Gemini's server-side quota cannot be bypassed. The way to get many complex tasks per day with no hosted-AI cost is to move the runtime planner to a local model.

## Setup

1. Install Ollama.
2. Run:

```powershell
ollama pull qwen3:4b
```

3. In `backend/.env`:

```text
AI_PROVIDER=auto
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:4b
AUTO_PREFER_LOCAL=true
AUTO_ALLOW_GEMINI_FALLBACK=false
```

4. Start the backend normally.

For a completely local AI path, use `AI_PROVIDER=ollama`.
