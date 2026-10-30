> **DETERMINISTIC LOCAL FIXTURE TEST.** The 25/25 result below exercises the deterministic planner + executor against one hand-built page. It does NOT exercise Gemini/Ollama, the adaptive loop, deadlines, or any live website, and is not evidence of arbitrary-web success. See `TEST_REPORT_ADAPTIVE.md`.

# NEXA Validation Report — Uploaded Project Working Copy

This report records validation performed against the project uploaded as `NEXA-project-patched-from-your-upload.zip`.

## Changes made before validation

1. **New-tab planner bug fixed**
   - Removed the invalid `about:blank` `navigate` action from the new-tab and open-Chrome planner paths.
   - Browser-tab creation remains the browser manager's responsibility.

2. **Search-result selection bug fixed**
   - A visible Search/Submit/Filter/Sort/Menu button is no longer eligible as the requested search result.
   - Result selection still supports link/result-card buttons when they are not generic controls.

3. **Semantic extraction made task-agnostic**
   - Removed hard-coded topic phrases from the semantic extractor.
   - Multi-word concepts are derived from the actual extraction request.

4. **Title/heading extraction reasons fixed**
   - The deterministic live planner now emits explicit semantic extraction contracts for page titles and first headings instead of falling back to generic page-content extraction.

5. **Optional Gemini dependency made lazy**
   - The backend can import/start in local-only Ollama mode without the Gemini SDK installed.
   - Gemini now raises a clear installation error only when the Gemini provider is actually selected/used.

## Validation performed

### Python/static validation

- Python compilation of the backend source: **PASS**.
- Planner self-test: **8 cases PASS**.
- Ollama provider construction in local-only mode: **PASS**.
- Gemini SDK absence no longer prevents `services.ai_provider` from importing: **PASS**.

### Browser-engine complex-task validation

A deterministic local browser fixture was used only as a controlled test environment. It exercises the project's actual:

- Playwright Chromium browser
- live DOM inspector
- search-box discovery
- typing
- Enter submission
- result ranking
- result clicking
- semantic title extraction
- first-heading extraction
- section matching
- first-sentence extraction
- repeated-task reset

**25/25 complex natural-language tasks passed.**

The 25 tasks covered five different topics and five task formulations per topic, including:

- search → first article → title
- search → first article → first heading
- search → first article → named section → first sentence
- search → first article → named section → first sentence with different wording
- search → first article → extract first sentence from named section

Validation was split into two clean browser runs (tasks 1–16 and 17–25) because the execution environment's Chromium process becomes unstable after long repeated in-memory fixture resets. Each task itself passed its complete browser interaction sequence.

### False-success regression

**PASS.** Before a search/result/extraction task is completed, the live planner does not return `done=true`.

A search-results page containing both a real result link and a prominent Search button selected the actual result, not the Search button.

### Generic extraction regression

**PASS.** A topic not present in any hard-coded topic list (`Quantum Computing` / `Hardware reliability`) was correctly extracted from its live DOM section.

## Important environment limitation

The validation environment cannot reliably open arbitrary external websites from Playwright Chromium; direct external/local HTTP navigation is restricted by the execution environment (`ERR_BLOCKED_BY_ADMINISTRATOR`). Therefore this report **does not claim that Wikipedia, GitHub, YouTube, MDN, ChatGPT, etc. were live-tested from this environment**.

The project was tested at the browser-engine/control-loop level against a deterministic DOM fixture. Windows Chrome foreground-window/CDP behavior was also not verifiable in this Linux execution environment.

## Security packaging note

The uploaded project contained a backend `.env`. The final validation package must not redistribute the uploaded secret/API key. Configure the user's own key locally using `.env.example`.
