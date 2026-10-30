# NEXA Workflow Memory

NEXA now has a local workflow-memory layer for Gemini-free replay of tasks that have already succeeded.

## Flow

1. Check local memory for the exact normalized task.
2. Replay the saved semantic actions through the normal DOM resolver.
3. If replay succeeds, no Gemini request is made.
4. If replay fails, keep the memory and continue through normal planning.
5. New complex tasks use Gemini for initial reasoning and selective replanning.
6. A verified adaptive success is saved for future replay.

The memory stores semantic targets (role/name/CSS evidence), not mouse coordinates.

Workflows are stored at `backend/data/workflows.json` and the directory is ignored by Git.

Do not commit or share `backend/.env`; keep the Gemini API key only in the local `.env`.
