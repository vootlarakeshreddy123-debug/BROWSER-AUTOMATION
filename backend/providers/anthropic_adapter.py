"""Anthropic Claude adapter over the documented Messages API.

Endpoint:  POST {base_url}/v1/messages
Headers:   x-api-key, anthropic-version, content-type
Response:  content = [{"type": "text", "text": ...}, ...]
Claude has no JSON mode here, so the plan is parsed from the text with the same strict parser as other providers.
"""
from __future__ import annotations

from config import settings
from providers.base import HostedAdapter


class ClaudeAdapter(HostedAdapter):
    name = "claude"
    label = "Anthropic Claude"

    def _request(self, prompt: str):
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens or 1500,
            "system": "You are the planner of a browser-automation agent. Reply with ONE JSON object only, no prose.",
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"x-api-key": str(self._api_key), "anthropic-version": settings.ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        return f"{self.base_url}/v1/messages", headers, payload

    def _extract_text(self, body: dict) -> str:
        if body.get("type") == "error":
            raise RuntimeError(f"{self.label} error: {(body.get('error') or {}).get('message', 'unknown')}")
        parts = [b.get("text", "") for b in (body.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
        if body.get("stop_reason") == "max_tokens" and not "".join(parts).strip():
            raise RuntimeError(f"{self.label} output was cut off (max_tokens)")
        return "".join(parts)
