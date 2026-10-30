"""Common interface for the hosted adapters.

Every adapter exposes the same surface the adaptive loop already uses for Gemini/Ollama:

    name, label, model, paid
    configured() / config_problems()        -> startup / selection-time validation, never contains secrets
    await generate_plan(instruction, page_context=None, limits=None) -> agent.action_schema.ActionPlan

Provider-specific request building and response parsing stay in the subclass; the NEXA prompt and the strict
plan parsing are shared with the Gemini path (one validated ActionPlan representation).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent.action_schema import ActionPlan
from providers.http import Poster, ProviderHTTPError, post_json, redact


class _PlanCodec:
    """Reuses the existing NEXA prompt builder and the strict plan parser (no client / SDK needed)."""

    def _codec(self):
        from services.ai_provider import GeminiProvider as G
        return G

    def build_prompt(self, instruction: str, page_context: Any) -> str:
        G = self._codec()
        return G._build_prompt(self, instruction, page_context)

    def parse_plan(self, text: str, instruction: str | None = None) -> ActionPlan:
        G = self._codec()
        return G._parse_plan(self, text, instruction)

    # the two helpers _build_prompt/_parse_plan call on ``self``
    def _normalize_page_context(self, page_context):
        return self._codec()._normalize_page_context(page_context)

    def _extract_json_object(self, text):
        return self._codec()._extract_json_object(text)


class HostedAdapter(_PlanCodec, ABC):
    name: str = ""
    label: str = ""
    paid: bool = True            # hosted providers may bill: never used unless explicitly enabled

    def __init__(self, *, api_key: str | None, model: str, base_url: str, enabled: bool,
                 timeout_s: float = 20.0, max_tokens: int = 1500, poster: Poster | None = None) -> None:
        self._api_key = api_key
        self.model = (model or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._poster: Poster = poster or post_json

    # ---- configuration ------------------------------------------------------------
    def config_problems(self) -> list[str]:
        problems = []
        if not self.enabled:
            problems.append(f"{self.label} is disabled (set {self.name.upper()}_ENABLED=true to allow it; hosted calls may be billed)")
        if not self._api_key:
            problems.append(f"{self.label} API key is not set in backend/.env")
        if not self.model:
            problems.append(f"{self.label} model is not set (choose a model your account can use)")
        if not self.base_url.startswith(("https://", "http://")):
            problems.append(f"{self.label} base URL is invalid")
        return problems

    def configured(self) -> bool:
        return not self.config_problems()

    def secrets(self) -> list[str | None]:
        return [self._api_key]

    def safe(self, text: Any) -> str:
        return redact(text, self.secrets())

    # ---- planning -------------------------------------------------------------------
    @abstractmethod
    def _request(self, prompt: str) -> tuple[str, dict, dict]:
        """(url, headers, payload) for one planning request."""

    @abstractmethod
    def _extract_text(self, body: dict) -> str:
        """The model's text from the provider's response body."""

    async def complete_json(self, prompt: str, timeout_s: float | None = None) -> dict:
        """One bounded JSON completion (task understanding, message authoring).  Returns the parsed object."""
        problems = self.config_problems()
        if problems:
            raise RuntimeError(f"{self.label} is not usable: {problems[0]}")
        url, headers, payload = self._request(prompt)
        try:
            body = await self._poster(url, headers, payload, timeout_s or self.timeout_s)
        except ProviderHTTPError as exc:
            raise ProviderHTTPError(exc.status, self.safe(exc.detail), getattr(exc, 'retry_after', None)) from None
        except Exception as exc:
            raise RuntimeError(self.safe(f"{type(exc).__name__}: {exc}")) from None
        text = self._extract_text(body)
        if not text or not text.strip():
            raise RuntimeError(f"{self.label} returned an empty response")
        return self._extract_json_object(text)

    async def generate_plan(self, instruction: str, page_context: Any = None, limits: Any = None) -> ActionPlan:
        instruction = (instruction or "").strip()
        if not instruction:
            raise ValueError("Instruction must not be empty.")
        problems = self.config_problems()
        if problems:
            raise RuntimeError(f"{self.label} is not usable: {problems[0]}")
        prompt = self.build_prompt(instruction, page_context)
        url, headers, payload = self._request(prompt)
        try:
            body = await self._poster(url, headers, payload, self.timeout_s)
        except ProviderHTTPError as exc:
            # never let a key echoed by a proxy/provider reach logs, events or history
            raise ProviderHTTPError(exc.status, self.safe(exc.detail), getattr(exc, 'retry_after', None)) from None
        except Exception as exc:
            raise RuntimeError(self.safe(f"{type(exc).__name__}: {exc}")) from None
        text = self._extract_text(body)
        if not text or not text.strip():
            raise RuntimeError(f"{self.label} returned an empty response")
        return self.parse_plan(text, instruction)

