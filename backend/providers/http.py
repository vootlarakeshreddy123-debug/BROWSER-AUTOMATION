"""Tiny HTTP layer shared by the hosted-provider adapters.

* ``post_json`` is the only place that touches the network (httpx, imported lazily). Adapters accept an injectable
  ``poster`` so tests can feed provider-format responses without a network or an API key.
* Errors carry the HTTP status in their text so ``agent.provider_health.classify_failure`` can classify them
  (429 quota/rate limit, 5xx unavailable, 401/403 auth, 404 model).
* ``redact`` removes API keys from any text that might reach logs, events, history or the UI.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

Poster = Callable[[str, dict, dict, float], Awaitable[dict]]


class ProviderHTTPError(RuntimeError):
    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.detail = message
        self.retry_after = retry_after          # seconds the provider asked us to wait (Retry-After), if any


_KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),          # OpenAI / Anthropic style
    re.compile(r"xai-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gsk_[A-Za-z0-9]{12,}"),             # Groq
    re.compile(r"sb_(?:secret|publishable)_[A-Za-z0-9_\-]{8,}"),     # Supabase API keys
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),   # any JWT (user access / service tokens)
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),          # Google
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|x-api-key|authorization)\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}"),
]


def redact(text: Any, secrets: list[str | None] | None = None) -> str:
    out = str(text if text is not None else "")
    for sec in secrets or []:
        if sec and len(sec) >= 6:
            out = out.replace(sec, "[redacted]")
    for pat in _KEY_PATTERNS:
        out = pat.sub("[redacted]", out)
    return out


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)[:300]
        if err:
            return str(err)[:300]
        return str(body.get("message") or body)[:300]
    return str(body)[:300]


def parse_retry_after(value: Any) -> float | None:
    """Retry-After as seconds (the numeric form providers use); None when absent or unusable."""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 86400 else None


async def post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    import httpx  # lazy: only needed when a hosted provider is actually called

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    if resp.status_code >= 400:
        raise ProviderHTTPError(resp.status_code, _error_text(body), parse_retry_after(resp.headers.get("retry-after")))
    if not isinstance(body, dict):
        raise RuntimeError("provider returned a non-JSON response")
    return body
