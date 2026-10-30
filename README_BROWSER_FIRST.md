> **History.** Provider-order statements here are superseded by `ARCHITECTURE.md`.

# Browser-First behavior

This version changes the core flow to:

1. User types or speaks a task.
2. Backend immediately creates a fresh Chrome tab.
3. The exact dedicated Chrome window is activated and maximized.
4. The task is planned/executed in that tab.
5. The tab stays open and interactive for the user.
6. The UI receives a terminal result instead of spinning indefinitely.

Examples:
- `Open GitHub` -> new tab -> GitHub -> Chrome foreground.
- `Open ChatGPT` -> new tab -> ChatGPT -> Chrome foreground.
- `Open YouTube and search Python tutorials` -> new tab -> YouTube results.
- `Open Chrome and search Python interview questions` -> new tab -> web results.
- `Open Chrome` -> new tab only.

No screenshot is used as the browser interaction surface. The screenshot field is kept only for API compatibility.

## Existing installation

If `frontend/node_modules` already exists, do not run `npm install` again. Start the frontend with `npm run dev`.

Start backend from `backend` with:

`py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000`

Google Chrome must be installed. The backend creates a dedicated Chrome profile so it does not interfere with the user's normal Chrome profile.
