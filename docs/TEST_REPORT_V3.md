> **DETERMINISTIC LOCAL FIXTURE TEST** — not evidence of arbitrary live-web success. See `TEST_REPORT_ADAPTIVE.md`.

# NEXA validation report — uploaded project working copy

Date: 2026-09-29

## What was tested

The working copy was created from the exact user-uploaded `NEXA-project-patched-from-your-upload.zip`.

The following fixes were made before the validation run:

1. Browser lifecycle commands (`new tab` / `open Chrome`) no longer create invalid `about:blank` navigation actions.
2. Generic live search submission now targets the inspected search control instead of sending an unbound keyboard action.
3. Semantic extraction phrase matching no longer contains hard-coded `machine learning` / `artificial intelligence` topic rules.
4. Unknown-site routing no longer turns browser lifecycle phrases such as `new tab` or `chrome` into guessed web domains.
5. The planner and browser modules were Python-compiled after the changes.

## Complex browser validation

25 distinct multi-step natural-language tasks were executed against a real Playwright Chromium browser with a live DOM fixture. Each task required:

- discovering the search box from the DOM;
- entering a different query;
- submitting the search;
- selecting the first relevant result from the live result DOM;
- opening the detail/article state;
- finding a requested section;
- extracting the first sentence from that section;
- verifying the extracted value.

The 25 tasks were executed in two independent batches because the test environment became unstable when keeping one Chromium process alive for the entire 25-task run.

- Tasks 1–14: **14/14 passed**
- Tasks 15–25: **11/11 passed**
- Combined: **25/25 passed**

## Additional validation

- Backend Python compilation: passed.
- Planner self-test: completed without the previous `about:blank` Pydantic crash.
- External web navigation was attempted and is blocked by this execution environment with Chromium `ERR_BLOCKED_BY_ADMINISTRATOR`.
- Ollama was not running in this environment.
- The Google Gemini package could not be downloaded because outbound package-network access is unavailable here.
- Frontend production build could not be completed because the uploaded `node_modules` tree is missing the Linux Rollup optional binary; this is an environment/package-tree issue, not a source TypeScript error.

## Important scope

The 25/25 result proves the browser-control, live-DOM inspection, generic search/result interaction, and semantic extraction path against the deterministic fixture. It does **not** prove that arbitrary third-party websites will always work, because live external websites were blocked from this execution environment and the real Ollama/Gemini services were unavailable here.

The project is therefore packaged only with the claims above; no claim of 25/25 arbitrary-live-web success is made.
