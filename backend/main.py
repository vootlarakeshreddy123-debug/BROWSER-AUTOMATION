import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsProactorEventLoopPolicy()
    )

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.tasks import router as tasks_router
from browser.manager import browser_manager
from services.ai_provider import get_ai_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def log_runtime_identity() -> None:
    """Make it obvious WHICH code is running (stale copies / wrong folder / old bytecode)."""
    import inspect
    import os
    from pathlib import Path

    import agent.orchestrator as orchestrator_module
    import services.ai_provider as ai_provider_module
    from config import settings

    log = logging.getLogger("nexa.runtime")
    log.info(
        "NEXA_RUNTIME project_root=%s cwd=%s python=%s (%s) orchestrator=%s ai_provider=%s "
        "gemini_model=%s ollama_model=%s ai_provider_mode=%s",
        Path(__file__).resolve().parent.parent, os.getcwd(), sys.executable, sys.version.split()[0],
        Path(inspect.getfile(orchestrator_module)).resolve(),
        Path(inspect.getfile(ai_provider_module)).resolve(),
        settings.GEMINI_MODEL, settings.OLLAMA_MODEL, settings.AI_PROVIDER,
    )
    # Fail LOUDLY at startup (not mid-task) if the orchestrator is not this project's code.
    for name in ("split_goal_clauses", "navigation_clauses", "extraction_spec", "verify_in_dom"):
        if not hasattr(orchestrator_module, name):
            raise RuntimeError(
                f"orchestrator.py at {inspect.getfile(orchestrator_module)} is missing {name!r}: "
                "you are running a different/older copy than this project (check the folder you "
                "start uvicorn from, stale __pycache__, or a second NEXA copy on PYTHONPATH)."
            )
    if settings.GEMINI_MODEL.strip().lower() == "gemini-2.5-flash":
        log.warning("GEMINI_MODEL is gemini-2.5-flash, which new API keys get a 404 for. Set GEMINI_MODEL in backend/.env.")
    # V4: validate provider configuration at startup (names only - never a key) and be honest about the browser mode
    try:
        from providers.registry import ALL, config_problems, order_for
        for n in ALL:
            problems = config_problems(n, settings)
            if problems and (n in order_for("auto", settings) or n == settings.AI_PROVIDER.lower().strip()):
                log.warning("provider %s is not usable: %s", n, "; ".join(problems))
        log.info("provider auto order: %s", " -> ".join(order_for("auto", settings)) or "(none configured)")
    except Exception as exc:  # never block startup on a status log
        log.warning("could not evaluate provider configuration: %s", exc)
    note = _browser_mode_note()
    if note:
        log.warning(note)

app = FastAPI(
    title="Browser Automation Agent",
    version="4.0.0",
)

from config import settings as _settings          # noqa: E402

# Only the local frontend may call the API from a browser (override with ALLOWED_ORIGINS). Never "*" with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in _settings.ALLOWED_ORIGINS if o != "*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(tasks_router)


@app.on_event("startup")
async def _startup_diagnostics() -> None:
    log_runtime_identity()
    from services.persistence import configure_from_settings
    configure_from_settings()
    log = logging.getLogger("nexa.runtime")
    if _settings.SUPABASE_ENABLED:
        log.info("Supabase auth + history ENABLED (every task endpoint requires a valid access token)")
    else:
        log.info("Supabase DISABLED: local single-user mode, history is in memory only")


@app.get("/")
async def root():
    return {
        "name": "Browser Automation Agent",
        "status": "running",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


def _provider_status() -> dict:
    """AI provider availability - deliberately independent of backend/browser health. Never contains secrets."""
    from providers.registry import status
    return status()


def _browser_mode_note() -> str | None:
    """Browser Use Cloud is NOT implemented: say so instead of silently ignoring the setting."""
    from config import settings
    mode = (settings.BROWSER_EXECUTION_MODE or "local").strip().lower()
    if mode in ("", "local"):
        return None
    return (f"BROWSER_EXECUTION_MODE={mode!r} is not supported in this build (Browser Use Cloud is not implemented); "
            "NEXA is using the local Chrome (CDP) browser.")


@app.get("/api/providers")
async def api_providers():
    """Safe provider configuration/status for the frontend (no keys, ever)."""
    from config import settings
    from providers.registry import SELECTABLE, order_for
    return {
        "default_selection": settings.AI_PROVIDER.lower().strip(),
        "selectable": list(SELECTABLE),
        "auto_order": order_for("auto"),
        "providers": _provider_status(),
        "browser_execution_mode": "local",
        "browser_mode_note": _browser_mode_note(),
        "note": ("Pricing, free-tier eligibility, quotas and model availability vary by provider and change over "
                 "time. NEXA never assumes a hosted provider is free; hosted providers are used only when enabled."),
    }


@app.get("/api/health")
async def api_health():
    ai_configured = False

    try:
        ai_configured = get_ai_provider() is not None
    except Exception:
        ai_configured = False

    return {
        "status": "ok",
        "ai_configured": ai_configured,
        "browser_ready": browser_manager.is_ready,
        # Expose the backend's auth mode so the frontend can diagnose a
        # Supabase/backend configuration mismatch instead of showing a vague 401.
        "auth_enabled": bool(_settings.SUPABASE_ENABLED),
        "auth_configured": bool(_settings.SUPABASE_URL and _settings.SUPABASE_PUBLISHABLE_KEY),
        "providers": _provider_status(),
    }
