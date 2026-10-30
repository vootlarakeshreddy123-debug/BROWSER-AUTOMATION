"""OpenAI Chat Completions adapter - also used for xAI Grok, whose public API is OpenAI-compatible.

Endpoint:  POST {base_url}/chat/completions      Auth: ``Authorization: Bearer <key>``
Response:  choices[0].message.content
JSON mode: ``response_format={"type": "json_object"}`` (the NEXA prompt already asks for JSON).
No temperature is sent: some current models only accept their default.
"""
from __future__ import annotations

from providers.base import HostedAdapter


class OpenAICompatAdapter(HostedAdapter):
    name = "openai"
    label = "OpenAI"

    def _request(self, prompt: str):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are the planner of a browser-automation agent. Reply with ONE JSON object only."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        if self.max_tokens:
            payload["max_completion_tokens" if self.name == "openai" else "max_tokens"] = self.max_tokens
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        return f"{self.base_url}/chat/completions", headers, payload

    def _extract_text(self, body: dict) -> str:
        try:
            choice = (body.get("choices") or [])[0]
        except Exception:
            choice = None
        if not choice:
            raise RuntimeError(f"{self.label} response had no choices")
        msg = choice.get("message") or {}
        if msg.get("refusal"):
            raise RuntimeError(f"{self.label} refused the request")
        content = msg.get("content")
        if isinstance(content, list):          # some compatible servers return content parts
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return str(content or "")


class GrokAdapter(OpenAICompatAdapter):
    name = "grok"
    label = "xAI Grok"


class GroqAdapter(OpenAICompatAdapter):
    """Groq (groq.com) through its OpenAI-compatible Chat Completions endpoint.  Uses GROQ_* configuration only."""
    name = "groq"
    label = "Groq"
