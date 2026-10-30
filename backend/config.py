"""Central configuration for the local Browser Automation Agent."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    # AUTO = Gemini primary for adaptive tasks, Ollama emergency fallback.
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "auto")

    CLAUDE_API_KEY: str | None = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or None
    # No model default for hosted providers other than Gemini: model names and availability change, so you choose one.
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", os.getenv("ANTHROPIC_MODEL", ""))

    GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or None
    # ONE authoritative source for the Gemini model: GEMINI_MODEL in backend/.env.
    # The default below must match backend/.env.example. NEXA never rewrites it.
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    GEMINI_MODEL_ERROR_COOLDOWN_SECONDS: float = _float("GEMINI_MODEL_ERROR_COOLDOWN_SECONDS", 3600.0)

    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    OLLAMA_TIMEOUT_SECONDS: float = _float("OLLAMA_TIMEOUT_SECONDS", 30.0)

    # ---- additional hosted providers (see providers/registry.py) ----------------
    # A hosted provider is USED only when its key is set AND <NAME>_ENABLED=true. Keys live only in backend/.env.
    # Pricing, free-tier eligibility, quotas and model availability differ per provider and change over time:
    # check each provider's own console. NEXA never assumes any hosted model is free.
    OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY") or None
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_ENABLED: bool = _bool("OPENAI_ENABLED", False)
    ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    ANTHROPIC_VERSION: str = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    CLAUDE_ENABLED: bool = _bool("CLAUDE_ENABLED", False)
    XAI_API_KEY: str | None = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or None
    XAI_MODEL: str = os.getenv("XAI_MODEL", os.getenv("GROK_MODEL", ""))
    XAI_BASE_URL: str = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
    XAI_ENABLED: bool = _bool("XAI_ENABLED", _bool("GROK_ENABLED", False))
    # ---- Supabase (auth + persistent history). OFF by default: with it off NEXA is the local single-user tool it always was.
    # Supabase NEVER generates actions and NEVER decides success: it stores what the backend reports.
    # The publishable key is safe in a browser; the secret key must stay backend-only and is optional (see README).
    SUPABASE_ENABLED: bool = _bool("SUPABASE_ENABLED", False)
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_PUBLISHABLE_KEY: str | None = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or None
    SUPABASE_SECRET_KEY: str | None = os.getenv("SUPABASE_SECRET_KEY") or None
    SUPABASE_TIMEOUT_SECONDS: float = _float("SUPABASE_TIMEOUT_SECONDS", 8.0)
    SUPABASE_TOKEN_CACHE_SECONDS: float = _float("SUPABASE_TOKEN_CACHE_SECONDS", 30.0)
    # Browsers allowed to call the API (comma separated). Never "*" together with credentials.
    ALLOWED_ORIGINS: list[str] = _list("ALLOWED_ORIGINS", ["http://localhost:5173", "http://127.0.0.1:5173"])
    # Groq (groq.com - NOT xAI Grok). OpenAI-compatible endpoint; free-tier limits and model names change: check console.groq.com.
    GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY") or None
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    GROQ_ENABLED: bool = _bool("GROQ_ENABLED", True)
    GROQ_TIMEOUT_SECONDS: float = _float("GROQ_TIMEOUT_SECONDS", 0.0)     # 0 = use HOSTED_PROVIDER_TIMEOUT_SECONDS
    GEMINI_ENABLED: bool = _bool("GEMINI_ENABLED", True)
    OLLAMA_ENABLED: bool = _bool("OLLAMA_ENABLED", True)
    # Order AUTO mode tries providers in (comma separated, first = primary). Providers that are not
    # configured/enabled/healthy are skipped. Paid providers appear here only if YOU list them.
    AUTO_PROVIDER_ORDER: list[str] = _list("AUTO_PROVIDER_ORDER", ["groq", "gemini", "ollama", "openai", "claude"])
    # Hard cap on calls to ONE hosted (potentially billed) provider inside a single task.
    HOSTED_PROVIDER_MAX_CALLS_PER_TASK: int = _int("HOSTED_PROVIDER_MAX_CALLS_PER_TASK", 12)
    HOSTED_PROVIDER_TIMEOUT_SECONDS: float = _float("HOSTED_PROVIDER_TIMEOUT_SECONDS", 20.0)
    HOSTED_PROVIDER_MAX_OUTPUT_TOKENS: int = _int("HOSTED_PROVIDER_MAX_OUTPUT_TOKENS", 1500)
    HOSTED_PROVIDER_COOLDOWN_SECONDS: float = _float("HOSTED_PROVIDER_COOLDOWN_SECONDS", 300.0)
    # Optional second-opinion AI task understanding (one bounded call per task, only when the deterministic parse is unsure)
    AI_UNDERSTANDING_ENABLED: bool = _bool("AI_UNDERSTANDING_ENABLED", True)
    # Browser execution: "local" (default, real visible Chrome via CDP). "browser_use_cloud" is reserved, see README.
    BROWSER_EXECUTION_MODE: str = os.getenv("BROWSER_EXECUTION_MODE", "local")
    BROWSER_USE_API_KEY: str | None = os.getenv("BROWSER_USE_API_KEY") or None

    # Gemini is the primary adaptive planner in AUTO mode; Ollama is the
    # bounded emergency fallback. AUTO_PREFER_LOCAL is legacy and only affects
    # the old non-adaptive AutoProvider path (initial planning); the adaptive
    # loop always tries Gemini first.
    AUTO_PREFER_LOCAL: bool = _bool("AUTO_PREFER_LOCAL", False)
    # Retained for compatibility; AUTO mode may use Ollama after Gemini fails.
    AUTO_ALLOW_GEMINI_FALLBACK: bool = _bool("AUTO_ALLOW_GEMINI_FALLBACK", True)

    AI_MAX_RETRIES: int = _int("AI_MAX_RETRIES", 1)
    AI_REQUEST_TIMEOUT_SECONDS: float = _float("AI_REQUEST_TIMEOUT_SECONDS", 20.0)

    BROWSER_HEADLESS: bool = _bool("BROWSER_HEADLESS", False)
    BROWSER_PREFER_SYSTEM_CHROME: bool = _bool("BROWSER_PREFER_SYSTEM_CHROME", True)
    BROWSER_CHROME_PATH: str | None = os.getenv("BROWSER_CHROME_PATH") or None
    BROWSER_ACTION_TIMEOUT_MS: int = _int("BROWSER_ACTION_TIMEOUT_MS", 7000)
    # After a click, how long to wait for a new tab/popup before treating it as a same-tab click.
    BROWSER_NEW_PAGE_WAIT_MS: int = _int("BROWSER_NEW_PAGE_WAIT_MS", 1000)

    # ---- goal runner (agent/goal_runner.py) ----
    GOAL_MAX_STEPS: int = _int("GOAL_MAX_STEPS", 6)
    # How long to wait for the USER to clear a CAPTCHA / login / human-verification page in the live window.
    MANUAL_INTERVENTION_WAIT_SECONDS: float = _float("MANUAL_INTERVENTION_WAIT_SECONDS", 45.0)
    BROWSER_NAV_TIMEOUT_MS: int = _int("BROWSER_NAV_TIMEOUT_MS", 25000)
    ALLOW_PRIVATE_NETWORK: bool = _bool("ALLOW_PRIVATE_NETWORK", False)
    BROWSER_MAX_RETRIES: int = _int("BROWSER_MAX_RETRIES", 2)
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "./downloads")

    TASK_TIMEOUT_SECONDS: float = _float("TASK_TIMEOUT_SECONDS", 360.0)

    # ---- adaptive-loop budgets (see ARCHITECTURE.md) --------------------
    # Internal deadline = TASK_TIMEOUT_SECONDS - margin, so the agent can fail
    # gracefully (with diagnostics) before the outer hard timeout cancels it.
    TASK_DEADLINE_MARGIN_SECONDS: float = _float("TASK_DEADLINE_MARGIN_SECONDS", 8.0)
    AI_PLANNING_BUDGET_FRACTION: float = _float("AI_PLANNING_BUDGET_FRACTION", 0.35)
    RECOVERY_BUDGET_FRACTION: float = _float("RECOVERY_BUDGET_FRACTION", 0.15)
    BROWSER_RESERVE_SECONDS: float = _float("BROWSER_RESERVE_SECONDS", 20.0)
    GEMINI_CALL_TIMEOUT_SECONDS: float = _float("GEMINI_CALL_TIMEOUT_SECONDS", 15.0)
    GEMINI_COOLDOWN_SECONDS: float = _float("GEMINI_COOLDOWN_SECONDS", 300.0)
    GEMINI_TIMEOUT_COOLDOWN_SECONDS: float = _float("GEMINI_TIMEOUT_COOLDOWN_SECONDS", 60.0)
    OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK: int = _int("OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK", 2)
    OLLAMA_EMERGENCY_TIMEOUT_SECONDS: float = _float("OLLAMA_EMERGENCY_TIMEOUT_SECONDS", 20.0)
    OLLAMA_EMERGENCY_BUDGET_SECONDS: float = _float("OLLAMA_EMERGENCY_BUDGET_SECONDS", 30.0)
    OLLAMA_COOLDOWN_SECONDS: float = _float("OLLAMA_COOLDOWN_SECONDS", 120.0)
    MAX_FALSE_DONE_CLAIMS: int = _int("MAX_FALSE_DONE_CLAIMS", 3)
    NO_PROGRESS_RECOVER_AFTER: int = _int("NO_PROGRESS_RECOVER_AFTER", 3)
    NO_PROGRESS_FAIL_AFTER: int = _int("NO_PROGRESS_FAIL_AFTER", 6)

    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = _int("PORT", 8000)
    CORS_ORIGINS: list[str] = _list(
        "CORS_ORIGINS",
        ["http://127.0.0.1:5173", "http://localhost:5173"],
    )

    @property
    def ai_configured(self) -> bool:
        try:
            from providers.registry import configured_names
            return bool(configured_names(self))
        except Exception:  # registry import problems must never break startup
            return bool(self.GEMINI_API_KEY or self.OLLAMA_BASE_URL)


settings = Settings()
