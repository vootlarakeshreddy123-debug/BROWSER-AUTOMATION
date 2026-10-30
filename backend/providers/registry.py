"""Central provider registry: ONE place that knows every provider, how it is configured, and in what order
AUTO mode tries them.  No other module contains provider-specific conditionals.

Selection (``AI_PROVIDER`` in .env, or per task from the UI):
    auto      -> AUTO_PROVIDER_ORDER (default: groq, gemini, ollama, openai, claude), skipping providers that are not configured / enabled
    <name>    -> exactly that provider (groq | gemini | ollama | openai | claude | grok); no hidden fallback
Paid hosted providers (openai, claude, grok) are only ever used when their *_ENABLED flag is true, a key and model
are set, and - in AUTO - they are explicitly listed in AUTO_PROVIDER_ORDER.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from config import settings

logger = logging.getLogger("providers.registry")

ALL = ("groq", "gemini", "ollama", "openai", "claude", "grok")
HOSTED_NEW = ("groq", "openai", "claude", "grok")
LABELS = {"groq": "Groq", "gemini": "Google Gemini", "ollama": "Ollama (local)", "openai": "OpenAI", "claude": "Anthropic Claude", "grok": "xAI Grok"}
SELECTABLE = ("auto",) + ALL


@dataclass
class ProviderSpec:
    name: str
    label: str
    paid: bool
    key: Callable[[Any], str | None]
    model: Callable[[Any], str]
    enabled: Callable[[Any], bool]


SPECS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec("groq", LABELS["groq"], True, lambda s: getattr(s, "GROQ_API_KEY", None), lambda s: getattr(s, "GROQ_MODEL", ""), lambda s: bool(getattr(s, "GROQ_ENABLED", False))),
    "gemini": ProviderSpec("gemini", LABELS["gemini"], True, lambda s: s.GEMINI_API_KEY, lambda s: s.GEMINI_MODEL, lambda s: s.GEMINI_ENABLED),
    "ollama": ProviderSpec("ollama", LABELS["ollama"], False, lambda s: "local", lambda s: s.OLLAMA_MODEL, lambda s: s.OLLAMA_ENABLED),
    "openai": ProviderSpec("openai", LABELS["openai"], True, lambda s: s.OPENAI_API_KEY, lambda s: s.OPENAI_MODEL, lambda s: s.OPENAI_ENABLED),
    "claude": ProviderSpec("claude", LABELS["claude"], True, lambda s: s.CLAUDE_API_KEY, lambda s: s.CLAUDE_MODEL, lambda s: s.CLAUDE_ENABLED),
    "grok": ProviderSpec("grok", LABELS["grok"], True, lambda s: s.XAI_API_KEY, lambda s: s.XAI_MODEL, lambda s: s.XAI_ENABLED),
}


def normalise_selection(value: str | None) -> str:
    v = (value or "").strip().lower()
    v = {"anthropic": "claude", "xai": "grok", "groqcloud": "groq", "google": "gemini", "local": "ollama", "gpt": "openai"}.get(v, v)
    return v if v in SELECTABLE else ("auto" if not v else v)


def config_problems(name: str, cfg: Any = settings) -> list[str]:
    """Why ``name`` cannot be used right now (no secrets in the text)."""
    spec = SPECS[name]
    out: list[str] = []
    if not spec.enabled(cfg):
        out.append(f"disabled (set {'XAI' if name == 'grok' else name.upper()}_ENABLED=true)")
    if name != "ollama" and not spec.key(cfg):
        out.append("API key not set in backend/.env")
    if not spec.model(cfg):
        out.append(f"model not set (set {'XAI' if name == 'grok' else name.upper()}_MODEL)")
    return out


def configured_names(cfg: Any = settings) -> list[str]:
    return [n for n in ALL if not config_problems(n, cfg)]


def order_for(selection: str | None = None, cfg: Any = settings) -> list[str]:
    sel = normalise_selection(selection or cfg.AI_PROVIDER)
    if sel == "auto":
        seen: list[str] = []
        for n in cfg.AUTO_PROVIDER_ORDER:
            n = normalise_selection(n)
            if n in ALL and n not in seen:
                seen.append(n)
        return seen or ["groq", "gemini", "ollama"]
    if sel in ALL:
        return [sel]
    if sel in ("fallback",):          # legacy deterministic-only mode
        return []
    raise ValueError(f"Unsupported provider '{selection}'. Choose one of: {', '.join(SELECTABLE)}")


# ---- adapter instances (cached; reset by reset_registry) -------------------------------------
_LOCK = threading.Lock()
_ADAPTERS: dict[str, Any] | None = None
_POSTER_OVERRIDE = None          # tests inject a fake transport here


def set_poster_override(poster) -> None:
    """Tests only: route every hosted adapter through ``poster`` instead of the network."""
    global _POSTER_OVERRIDE, _ADAPTERS
    with _LOCK:
        _POSTER_OVERRIDE, _ADAPTERS = poster, None


def reset_registry() -> None:
    global _ADAPTERS
    with _LOCK:
        _ADAPTERS = None


def _build(name: str, cfg: Any) -> Any | None:
    if config_problems(name, cfg):
        return None
    try:
        if name == "gemini":
            from services.ai_provider import GeminiProvider
            return GeminiProvider()
        if name == "ollama":
            from services.ai_provider import OllamaProvider
            return OllamaProvider()
        kw = dict(timeout_s=cfg.HOSTED_PROVIDER_TIMEOUT_SECONDS, max_tokens=cfg.HOSTED_PROVIDER_MAX_OUTPUT_TOKENS,
                  poster=_POSTER_OVERRIDE)
        if name == "groq":
            from providers.openai_compat import GroqAdapter
            kw_g = dict(kw, timeout_s=(getattr(cfg, "GROQ_TIMEOUT_SECONDS", 0) or cfg.HOSTED_PROVIDER_TIMEOUT_SECONDS))
            return GroqAdapter(api_key=cfg.GROQ_API_KEY, model=cfg.GROQ_MODEL, base_url=cfg.GROQ_BASE_URL,
                               enabled=cfg.GROQ_ENABLED, **kw_g)
        if name == "openai":
            from providers.openai_compat import OpenAICompatAdapter
            return OpenAICompatAdapter(api_key=cfg.OPENAI_API_KEY, model=cfg.OPENAI_MODEL, base_url=cfg.OPENAI_BASE_URL,
                                       enabled=cfg.OPENAI_ENABLED, **kw)
        if name == "grok":
            from providers.openai_compat import GrokAdapter
            return GrokAdapter(api_key=cfg.XAI_API_KEY, model=cfg.XAI_MODEL, base_url=cfg.XAI_BASE_URL,
                               enabled=cfg.XAI_ENABLED, **kw)
        if name == "claude":
            from providers.anthropic_adapter import ClaudeAdapter
            return ClaudeAdapter(api_key=cfg.CLAUDE_API_KEY, model=cfg.CLAUDE_MODEL, base_url=cfg.ANTHROPIC_BASE_URL,
                                 enabled=cfg.CLAUDE_ENABLED, **kw)
    except Exception as exc:  # SDK missing, bad config...
        logger.warning("Provider %s could not be initialised: %s", name, exc)
    return None


def get_adapters(cfg: Any = settings) -> dict[str, Any]:
    """``{name: adapter | None}`` for every known provider (None = unconfigured / unusable)."""
    global _ADAPTERS
    with _LOCK:
        if _ADAPTERS is None:
            _ADAPTERS = {n: _build(n, cfg) for n in ALL}
        return _ADAPTERS


def providers_for_task(selection: str | None = None, cfg: Any = settings) -> tuple[dict[str, Any], list[str]]:
    """(adapters, order) for one task.  ``adapters`` holds only providers in ``order``."""
    order = order_for(selection, cfg)
    all_ = get_adapters(cfg)
    return {n: all_.get(n) for n in order}, order


def status(cfg: Any = settings) -> dict[str, Any]:
    """Safe for the frontend: never contains a key, only configured/available flags, model names and reasons."""
    from agent.provider_health import global_cooldown_remaining
    order = order_for("auto", cfg)
    adapters = get_adapters(cfg)
    out: dict[str, Any] = {}
    for n in ALL:
        spec = SPECS[n]
        problems = config_problems(n, cfg)
        cd = global_cooldown_remaining(n)
        if problems:
            state, reason = "unconfigured", "; ".join(problems)
        elif adapters.get(n) is None:
            state, reason = "unavailable", "the provider could not be initialised (see backend log)"
        elif cd > 0:
            state, reason = "cooldown", f"cooling down after a failure ({int(cd)}s left)"
        else:
            state, reason = "available", ""
        out[n] = {"label": spec.label, "configured": not problems, "enabled": bool(spec.enabled(cfg)),
                  "state": state, "reason": reason, "model": spec.model(cfg) or None,
                  "may_bill": spec.paid, "in_auto_order": n in order,
                  "selectable": not problems}
    return out
