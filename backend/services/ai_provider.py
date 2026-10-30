from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from urllib.parse import quote_plus
from abc import ABC, abstractmethod
from typing import Any

import httpx
# Optional hosted-provider dependency.  NEXA must still start in local-only
# Ollama mode when the Gemini SDK is not installed.  Import it lazily in the
# Gemini provider instead of making the whole backend unimportable.
genai = None
types = None

from agent.action_schema import Action, ActionPlan, looks_sensitive
from agent.budget import CallLimits
from config import settings

logger = logging.getLogger(__name__)


# ============================================================
# OLLAMA STRUCTURED OUTPUT SCHEMA
# ============================================================

OLLAMA_ACTION_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "navigate",
                            "click",
                            "type",
                            "press",
                            "wait_for",
                            "extract",
                            "download",
                            "scroll",
                            "hover",
                            "select_option",
                            "check",
                            "uncheck",
                            "upload",
                        ],
                    },
                    "url": {
                        "type": ["string", "null"],
                    },
                    "target": {
                        "type": ["object", "null"],
                        "properties": {
                            "role": {
                                "type": ["string", "null"],
                            },
                            "name": {
                                "type": ["string", "null"],
                            },
                            "css": {
                                "type": ["string", "null"],
                            },
                            "exact": {
                                "type": ["boolean", "null"],
                            },
                            "placeholder": {
                                "type": ["string", "null"],
                            },
                            "label": {
                                "type": ["string", "null"],
                            },
                        },
                        "additionalProperties": False,
                    },
                    "text": {
                        "type": ["string", "null"],
                    },
                    "key": {
                        "type": ["string", "null"],
                    },
                    "direction": {
                        "type": ["string", "null"],
                        "enum": ["up", "down", None],
                    },
                    "amount": {
                        "type": ["integer", "null"],
                        "minimum": 100,
                        "maximum": 5000,
                    },
                    "reason": {
                        "type": ["string", "null"],
                    },
                    "value": {"type": ["string", "null"]},
                    "file_path": {"type": ["string", "null"]},
                },
                "required": ["type"],
            },
        },
        "source": {
            "type": "string",
            "enum": ["ai", "fast_path", "fallback"],
        },
        "done": {
            "type": "boolean",
        },
        "goal": {"type": ["string", "null"]},
        "expected_state": {"type": ["string", "null"]},
    },
    "required": ["actions", "source", "done"],
}


# ============================================================
# BASE PROVIDER
# ============================================================

class AIProvider(ABC):
    """Base interface for NEXA AI planning providers."""

    @abstractmethod
    async def generate_plan(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None = None,
    ) -> ActionPlan:
        raise NotImplementedError


# ============================================================
# GEMINI PROVIDER
# ============================================================

class GeminiProvider(AIProvider):
    """
    Remote Gemini planner.

    Gemini is the PRIMARY adaptive planner when available.
    Temporary quota/rate-limit/service failures put Gemini into a
    short cooldown so NEXA does not burn time retrying the same
    unavailable remote service on every browser step.
    """

    def __init__(self) -> None:
        global genai, types
        if not settings.GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured."
            )

        try:
            if genai is None or types is None:
                from google import genai as _genai
                from google.genai import types as _types
                genai, types = _genai, _types
        except ImportError as exc:
            raise RuntimeError(
                "Gemini is configured, but the google-genai package is not installed. "
                "Install the backend requirements or use AI_PROVIDER=ollama for local-only operation."
            ) from exc

        self.client = genai.Client(
            api_key=settings.GEMINI_API_KEY
        )

        configured_model = str(
            getattr(
                settings,
                "GEMINI_MODEL",
                "",
            )
            or ""
        ).strip()

        # The configured model is used exactly as given. NEXA does not rewrite
        # it and does not assume any model is available: if the API rejects
        # the name, the failure is classified and reported like any other
        # provider failure.
        if not configured_model:
            raise RuntimeError(
                "GEMINI_MODEL is empty. Set GEMINI_MODEL in backend/.env to a "
                "model your API key can access."
            )
        self.model = configured_model

        self._cooldown_until = 0.0
        self._cooldown_seconds = float(getattr(settings, "GEMINI_COOLDOWN_SECONDS", 300.0))

        logger.info(
            "Gemini provider initialized with model %s",
            self.model,
        )

    async def generate_plan(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None = None,
    ) -> ActionPlan:
        instruction = instruction.strip()

        if not instruction:
            raise ValueError(
                "Instruction must not be empty."
            )

        if looks_sensitive(instruction):
            raise RuntimeError(
                "Sensitive actions require explicit confirmation "
                "and are not currently supported."
            )

        now = time.monotonic()
        if now < self._cooldown_until:
            remaining = max(1, int(self._cooldown_until - now))
            raise RuntimeError(
                "Gemini temporarily unavailable; "
                f"cooldown active for about {remaining}s."
            )

        prompt = self._build_prompt(
            instruction,
            page_context,
        )

        max_attempts = max(
            1,
            int(
                getattr(
                    settings,
                    "AI_MAX_RETRIES",
                    1,
                )
            ) + 1,
        )

        timeout = max(
            5.0,
            float(
                getattr(
                    settings,
                    "AI_REQUEST_TIMEOUT_SECONDS",
                    12.0,
                )
            ),
        )

        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Generating Gemini plan (attempt %d/%d)",
                    attempt,
                    max_attempts,
                )

                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._generate_sync,
                        prompt,
                    ),
                    timeout=timeout,
                )

                text = getattr(
                    response,
                    "text",
                    None,
                )

                if not text:
                    raise RuntimeError(
                        "Gemini returned an empty planning response."
                    )

                return self._parse_plan(text, instruction=instruction)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                last_error = exc

                error_text = str(exc)
                upper = error_text.upper()

                quota_error = (
                    "429" in error_text
                    or "RESOURCE_EXHAUSTED" in upper
                    or "QUOTA EXCEEDED" in upper
                    or "TOO MANY REQUESTS" in upper
                )

                if quota_error:
                    self._cooldown_until = (
                        time.monotonic() + self._cooldown_seconds
                    )
                    logger.warning(
                        "Gemini quota/rate-limit detected; cooldown set to %.0fs.",
                        self._cooldown_seconds,
                    )
                    raise RuntimeError(
                        "Gemini API quota/rate limit was reached."
                    ) from exc

                temporary_error = (
                    "503" in error_text
                    or "UNAVAILABLE" in upper
                )

                if temporary_error:
                    self._cooldown_until = (
                        time.monotonic() + self._cooldown_seconds
                    )
                    logger.warning(
                        "Gemini temporarily unavailable; cooldown set to %.0fs.",
                        self._cooldown_seconds,
                    )
                    raise RuntimeError(
                        "Gemini temporarily unavailable; "
                        "remote service returned 503/UNAVAILABLE."
                    ) from exc

                timeout_error = (
                    "TIMEOUT" in upper
                    or isinstance(exc, asyncio.TimeoutError)
                )

                if (
                    not timeout_error
                    or attempt >= max_attempts
                ):
                    logger.exception(
                        "Gemini planning failed."
                    )
                    break

                delay = min(
                    4.0,
                    0.8 * (2 ** (attempt - 1)),
                )

                await asyncio.sleep(delay)

        raise RuntimeError(
            "Gemini planning failed after "
            f"{max_attempts} attempts: {last_error}"
        ) from last_error

    async def complete_json(self, prompt: str, timeout_s: float | None = None) -> dict:
        """One bounded JSON completion (task understanding, message authoring)."""
        response = await asyncio.wait_for(asyncio.to_thread(self._generate_sync, prompt),
                                          timeout=timeout_s or float(getattr(settings, "GEMINI_CALL_TIMEOUT_SECONDS", 15.0)))
        text = getattr(response, "text", None)
        if not text:
            raise RuntimeError("Gemini returned an empty response.")
        return self._extract_json_object(text)

    def _generate_sync(
        self,
        prompt: str,
    ) -> Any:
        config = types.GenerateContentConfig(
            max_output_tokens=2500,
            response_mime_type="application/json",
        )

        return self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )

    def _build_prompt(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None,
    ) -> str:
        context = self._normalize_page_context(
            page_context
        )

        if page_context is not None:
            phase_rules = """
LIVE EXECUTION MODE:

The browser is already open. Use the supplied DOM/page context as truth.
Return a SHORT PLAN of up to 5 actions that are safe to execute from the
CURRENT page state. Only include actions whose targets are already visible
or directly supported by the current context. If an action will create a
new page state that must be inspected before the next action, stop the plan
at that boundary (for example after navigation or submitting a search).
If the task is complete, return actions=[] and done=true ONLY when the
current page plus completed-action evidence satisfies EVERY requirement in
task_contract and progress.remaining_requirements is empty. A URL or page
title alone is NEVER proof that a requested result was opened. If any required
search, submission, result selection, section lookup, scrolling, or extraction
is still missing, you MUST return the next browser action instead of done=true.
If the user asks you to tell/give/show/read/report/extract information (including
a sentence, paragraph, section, heading, title, price, author, count, text, or
value), you must execute an EXTRACT action against the relevant live DOM. For
semantic requests such as a section, sentence, paragraph, or heading, prefer a
targetless EXTRACT with a precise reason rather than clicking a visible label.
Never extract browser chrome, language selectors, menus, headers, footers, or
sidebars merely because their text is easy to resolve.
"""
        else:
            phase_rules = """
INITIAL PLANNING MODE:

The browser starts at about:blank. Build the shortest safe plan that can be
performed without seeing a future DOM. Usually this means one navigation
action. Do not invent future-page targets.
"""

        return f"""
You are the reasoning engine of NEXA,
a general-purpose browser automation agent.

USER TASK:

{instruction}

CURRENT PAGE CONTEXT:

{context[:12000]}

{phase_rules}

AVAILABLE ACTIONS:

navigate
click
type
press
wait_for
extract
download
scroll

RULES:

- Return at most FIVE actions.
- Use only targets supported by the current DOM.
- Prefer role + name or placeholder.
- Do not invent selectors.
- Do not output xpath.
- For first/top results, choose the first relevant result using the live DOM, not a hard-coded website rule. Once a result has already navigated to a detail/article page, do NOT click another result-like link inside that page just because the original task says first/top result.
- If the user requests an answer from the page, use extract on the best live content. For section/sentence/paragraph requests, prefer targetless semantic extraction with a precise reason.
- Do not click unrelated navigation/header/footer links.
- Do not declare completion without evidence.
- NEXA will verify the browser after each short action chunk and can stop/replan.
- If the task is incomplete, return one useful next action.
- Never return actions=[] with done=false.
- Do not perform purchases, payments, financial transfers,
  destructive operations or consequential submissions.

RETURN ONLY VALID JSON.

Example:

{{
  "actions": [
    {{
      "type": "click",
      "target": {{
        "role": "link",
        "name": "Python Tutorial"
      }},
      "reason": "Open the relevant result."
    }}
  ],
  "source": "ai",
  "done": false
}}

Completed:

{{
  "actions": [],
  "source": "ai",
  "done": true
}}
"""

    @staticmethod
    def _normalize_page_context(
        page_context: str | dict[str, Any] | None,
    ) -> str:
        if page_context is None:
            return ""

        if isinstance(
            page_context,
            str,
        ):
            return page_context

        try:
            return json.dumps(
                page_context,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception:
            return str(page_context)

    @staticmethod
    def _extract_json_object(
        text: str,
    ) -> dict[str, Any]:
        cleaned = text.strip()

        # Remove Qwen/Gemini thinking blocks if present.
        cleaned = re.sub(
            r"<think>.*?</think>",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

        # Remove Markdown JSON fences.
        if cleaned.startswith("```"):
            cleaned = re.sub(
                r"^```(?:json)?\s*",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )

            cleaned = re.sub(
                r"\s*```$",
                "",
                cleaned,
            ).strip()

        # First try the complete response.
        try:
            value = json.loads(cleaned)

            if isinstance(value, dict):
                return value

        except json.JSONDecodeError:
            pass

        # Then search for a JSON object embedded in text.
        decoder = json.JSONDecoder()

        for match in re.finditer(
            r"\{",
            cleaned,
        ):
            try:
                value, _ = decoder.raw_decode(
                    cleaned[match.start():]
                )
            except json.JSONDecodeError:
                continue

            if isinstance(value, dict) and (
                "actions" in value
                or "done" in value
            ):
                return value

        preview = re.sub(
            r"\s+",
            " ",
            cleaned,
        )[:500]

        raise RuntimeError(
            f"AI returned invalid JSON. Response preview: {preview}"
        )

    def _parse_plan(
        self,
        text: str,
        instruction: str | None = None,
    ) -> ActionPlan:
        data = self._extract_json_object(text)

        raw_actions = data.get(
            "actions",
            [],
        )

        if not isinstance(
            raw_actions,
            list,
        ):
            raise RuntimeError(
                "AI planning response contains an invalid actions array."
            )

        if len(raw_actions) > 5:
            logger.warning("AI returned %d actions; limiting plan to 5 safe actions.", len(raw_actions))
            raw_actions = raw_actions[:5]

        actions: list[Action] = []

        for index, raw in enumerate(
            raw_actions
        ):
            if not isinstance(
                raw,
                dict,
            ):
                raise RuntimeError(
                    f"AI action {index + 1} is not an object."
                )

            action_data = dict(raw)

            raw_target = action_data.get(
                "target"
            )

            target_data = (
                {}
                if raw_target is None
                else dict(raw_target)
                if isinstance(raw_target, dict)
                else None
            )

            if target_data is None:
                raise RuntimeError(
                    f"AI action {index + 1} has an invalid target."
                )

            # Some models may put action-level fields
            # inside target. Move them back.
            for field in (
                "url",
                "text",
                "key",
                "direction",
                "amount",
                "value",
                "file_path",
            ):
                if (
                    action_data.get(field) is None
                    and field in target_data
                ):
                    action_data[field] = target_data.pop(
                        field
                    )

            # A navigate action must carry an explicit URL. Never invent a
            # website-specific URL from a hard-coded site table. URLs may come
            # from the model, the user's explicit URL, or live DOM href data.
            if action_data.get("url") is None and action_data.get("type") == "navigate":
                href = target_data.get("href")
                if href and str(href).startswith(("http://", "https://")):
                    action_data["url"] = str(href)

            allowed_target_fields = {
                "role",
                "name",
                "css",
                "exact",
                "placeholder",
                "label",
                "xpath",
            }

            filtered_target = {
                key: value
                for key, value in target_data.items()
                if (
                    key in allowed_target_fields
                    and value is not None
                )
            }

            action_data["target"] = (
                filtered_target
                if filtered_target
                else None
            )

            try:
                action = Action.model_validate(
                    action_data
                )
            except Exception as exc:
                raise RuntimeError(
                    f"AI action {index + 1} is invalid: {exc}"
                ) from exc

            if (
                action.type.value == "navigate"
                and not action.url
            ):
                raise RuntimeError(
                    f"AI action {index + 1} is a navigate "
                    "action without a URL."
                )

            if (
                action.type.value
                in {
                    "click", "type", "wait_for", "download",
                    "hover", "select_option", "check", "uncheck", "upload",
                }
                and action.target is None
            ):
                raise RuntimeError(
                    f"AI action {index + 1} "
                    f"({action.type.value}) is missing a target."
                )

            actions.append(action)

        done = bool(
            data.get(
                "done",
                False,
            )
        )

        source = str(
            data.get(
                "source",
                "ai",
            )
        )

        if source not in {
            "ai",
            "fast_path",
            "fallback",
        }:
            source = "ai"

        # Critical NEXA rule:
        # an incomplete task must always contain an action.
        if not actions and not done:
            raise RuntimeError(
                "AI returned no action while the task was still marked incomplete."
            )

        return ActionPlan(
            actions=actions,
            source=source,
            done=done,
            goal=data.get("goal"),
            expected_state=data.get("expected_state"),
        )


# ============================================================
# OLLAMA / QWEN3 PROVIDER
# ============================================================

class OllamaProvider(AIProvider):
    """
    Local Qwen/Ollama planner.

    NEXA uses this provider for local reasoning.
    Gemini quota is not used here.
    """

    def __init__(self) -> None:
        self.base_url = (
            settings.OLLAMA_BASE_URL.rstrip("/")
        )

        self.model = settings.OLLAMA_MODEL
        # A slow local model must not be called again on every browser step.
        # Cooldown is short because Ollama can recover as soon as the model is
        # warm, but long enough to prevent 18-second timeout amplification.
        self._cooldown_until = 0.0
        self._cooldown_seconds = 30.0

        logger.info(
            "Ollama provider initialized: "
            "model=%s url=%s",
            self.model,
            self.base_url,
        )

    async def is_available(self) -> bool:
        try:
            timeout = min(
                max(
                    float(
                        getattr(
                            settings,
                            "OLLAMA_TIMEOUT_SECONDS",
                            45.0,
                        )
                    ),
                    2.0,
                ),
                10.0,
            )

            async with httpx.AsyncClient(
                timeout=timeout
            ) as client:
                response = await client.get(
                    f"{self.base_url}/api/tags"
                )

            return response.status_code == 200

        except Exception as exc:
            logger.debug(
                "Ollama availability check failed: %s",
                exc,
            )

            return False

    @staticmethod
    def _compact_context(
        page_context: str | dict[str, Any] | None,
    ) -> str:
        """Build a task-aware browser context instead of arbitrary DOM truncation."""
        if page_context is None:
            return "No page context yet. Browser is at about:blank."
        if not isinstance(page_context, dict):
            context = str(page_context)
            return context[:3500] + ("\n...[context truncated]..." if len(context) > 3500 else "")

        instruction = str(page_context.get("remaining_instruction") or "").lower()
        tokens = {t for t in re.findall(r"[a-z0-9]{3,}", instruction) if t not in {"the","and","for","open","with","from","first","then","that","this"}}
        elements = page_context.get("elements") if isinstance(page_context.get("elements"), list) else []

        ranked: list[tuple[float, dict[str, Any]]] = []
        for el in elements:
            if not isinstance(el, dict) or el.get("disabled"):
                continue
            hay = " ".join(str(el.get(k) or "") for k in ("role","name","text","placeholder","href")).lower()
            score = 0.0
            score += 8.0 * sum(1 for token in tokens if token in hay)
            if el.get("region") in {"main", "article", "content"}:
                score += 12
            if el.get("href"):
                score += 3
            if el.get("role") in {"button", "link", "textbox", "searchbox"}:
                score += 5
            if any(bad in hay for bad in ("privacy", "terms", "cookie", "advertisement", "sign in", "login")):
                score -= 15
            try:
                score += max(0.0, 5.0 - float(el.get("y", 9999)) / 300.0)
            except Exception:
                pass
            ranked.append((score, el))
        ranked.sort(key=lambda x: (-x[0], int(x[1].get("index", 0) or 0)))

        selected=[]
        for _, el in ranked[:36]:
            selected.append({
                "index": el.get("index"), "tag": el.get("tag"), "role": el.get("role"),
                "name": str(el.get("name") or "")[:220],
                "text": str(el.get("text") or "")[:260],
                "placeholder": str(el.get("placeholder") or "")[:180],
                "href": str(el.get("href") or "")[:300],
                "region": el.get("region"),
                "disabled": el.get("disabled"),
            })

        compact = {
            "current_url": str(page_context.get("current_url") or page_context.get("url") or "")[:700],
            "current_title": str(page_context.get("current_title") or page_context.get("title") or "")[:350],
            "body_text": str(page_context.get("body_text") or "")[:1200],
            "remaining_instruction": str(page_context.get("remaining_instruction") or "")[:1200],
            "interaction_step": page_context.get("interaction_step"),
            "previous_action_result": page_context.get("previous_action_result"),
            "previous_failure": page_context.get("previous_failure"),
            "verification_feedback": page_context.get("verification_feedback"),
            "planner_mode": page_context.get("planner_mode", "normal"),
            "blocked_actions": page_context.get("blocked_actions", [])[-6:],
            "unsatisfied_goal_steps": page_context.get("unsatisfied_goal_clauses", [])[:6],
            "progress_feedback": page_context.get("progress_feedback"),
            "progress": page_context.get("progress", {}),
            "completed_actions": page_context.get("completed_actions", [])[-8:],
            "failed_actions": page_context.get("failed_actions", [])[-5:],
            "task_contract": page_context.get("task_contract", {}),
            "headings": page_context.get("headings", [])[:20],
            "sections": page_context.get("sections", [])[:12],
            "relevant_elements": selected,
        }
        try:
            context = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            context = str(compact)
        return context[:4800] + ("\n...[context truncated]..." if len(context) > 4800 else "")

    def _build_live_micro_prompt(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None,
    ) -> str:
        """Small live-step prompt for local models with limited CPU latency.

        A live browser decision needs one action, not the full planner policy
        document. Keeping the prompt small materially reduces Qwen latency
        while retaining the generic DOM-driven architecture.
        """
        context = self._compact_context(page_context)
        return f"""
You are NEXA, a browser-action planner. Choose ONLY the next action from the
current live browser state. Do not invent page state or selectors.

TASK:
{instruction[:1800]}

LIVE CONTEXT:
{context[:3200]}

RULES:
- Return exactly one action unless every task requirement is already proven.
- If incomplete, never return actions=[] or done=true.
- Use only evidence in LIVE CONTEXT.
- Prefer role/name/placeholder/label targets from the DOM.
- For search results, choose the most relevant visible result, not navigation.
- For a requested section/sentence/paragraph, use targetless EXTRACT so the
  browser semantic extractor can locate the relevant content.
- Never extract headers, menus, language selectors, sidebars, or footer text.
- If the current detail page is already open, do not click another result link
  inside it merely because the task says "first result".
- If completion is requested, it must be independently verifiable from the
  current page and extracted evidence.

AVAILABLE ACTIONS:
navigate, click, type, press, wait_for, extract, download, scroll, hover,
select_option, check, uncheck, upload

Return exactly one JSON object matching the action-plan schema.
""".strip()

    def _build_ollama_prompt(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None,
        repair: bool = False,
    ) -> str:
        context = self._compact_context(
            page_context
        )

        if page_context is None:
            phase = """
INITIAL STEP:

The browser starts at about:blank.

Return ONLY the first action required.

If the task says to open/visit a website,
navigate to that website's homepage first.

Do not jump directly to a documentation page,
search result, article, tutorial, or other deeper URL
unless the user explicitly supplied that exact URL.

Do not perform any later click, typing, search,
or scroll action yet.
"""
        else:
            phase = """
LIVE STEP:

The browser is already on a real page.

Return ONLY the NEXT action.

Use the supplied DOM/page context.

Do not guess future page state.

If the task is complete, return actions=[] and done=true ONLY when the
current browser state satisfies every requirement in task_contract and the
progress object shows no remaining requirement. A matching URL/title is not
enough to prove that a search result was opened.

If the user asks for information to be returned, do not declare done until
you have performed an extract action and the execution history contains a
non-empty extracted value relevant to the requested information.

If verification_feedback says your previous done=true was rejected, treat
that as authoritative: do NOT return done=true again. Choose a new action
that makes measurable progress toward the missing requirement.

If the task is NOT complete, you MUST return
exactly one useful browser action.
"""

        repair_instruction = ""

        if repair:
            repair_instruction = """
IMPORTANT RECOVERY MODE:

Your previous response was unusable.

You MUST produce a valid actionable plan now.

If the task is incomplete:

- return exactly ONE action
- choose an action supported by the current DOM
- do not return an empty actions array
- do not set done=true unless the current page proves completion

If you are uncertain, choose the safest observable
next action supported by the current page.
"""

        return f"""
You are NEXA's local browser-action planner.

USER TASK:

{instruction}

CURRENT BROWSER CONTEXT:

{context}

{phase}

{repair_instruction}

AVAILABLE ACTION TYPES:

navigate
click
type
press
wait_for
extract
download
scroll
hover
select_option
check
uncheck
upload

IMPORTANT RULES:

1. Return exactly ONE action unless the task is complete.

2. Use the current browser context as truth.

3. Never invent a CSS selector.

4. Prefer role + name or placeholder.

5. NEVER output xpath.

6. For search boxes, use the actual textbox
   represented in the current DOM.

7. For result pages, choose the first RELEVANT result,
   not an arbitrary first link.

8. Ignore unrelated:
   - headers
   - footers
   - advertisements
   - menus
   - sidebars

9. Read task_contract, progress, completed_actions, verification_feedback,
   and planner_mode as execution state. Continue from what is actually
   completed; do not restart a step that has already succeeded.

10. If planner_mode is "repair", the previous completion decision was
    rejected. Do not repeat the same done=true response or the same browser
    state. Choose an action that directly addresses the first missing
    requirement in progress.remaining_requirements.

11. For a requested section/sentence/paragraph, first locate the relevant
    section in the live DOM. Scroll when necessary. Only extract after the
    relevant content is visible or otherwise directly targetable.

12. For information requests, an EXTRACT action may omit target when the
    reason precisely describes a semantic page-level extraction, for example
    the first sentence of a named section. NEXA resolves that request against
    the live DOM; do not invent a selector.

13. If planner_mode is "repair", never repeat any action listed in
    blocked_actions. Prefer a different action type or a semantic EXTRACT
    when the remaining requirement is information retrieval.

14. Do not perform future actions before seeing
   the resulting DOM.

15. Do not say the task is complete unless the current
   page proves it.

11. NEVER return:

    actions=[] with done=false.

15. No purchases.
16. No payments.
17. No financial transfers.
18. No destructive actions.
19. No consequential form submission.
20. Return JSON only.

OUTPUT FORMAT:

Return exactly one JSON object.

Never return Markdown,
commentary,
reasoning,
or multiple actions.

Action example:

{{"actions":[{{"type":"click","target":{{"role":"link","name":"Python Tutorial"}},"reason":"Open the relevant tutorial."}}],"source":"ai","done":false}}

Completion example:

{{"actions":[],"source":"ai","done":true}}
"""

    async def _request_plan(
        self,
        prompt: str,
        instruction: str,
        *,
        live_fast: bool = False,
        timeout_override: float | None = None,
        num_predict_override: int | None = None,
    ) -> tuple[ActionPlan, float]:
        configured_timeout = float(
            getattr(
                settings,
                "OLLAMA_TIMEOUT_SECONDS",
                180.0,
            )
        )
        # Live steps are intentionally compact and bounded. A 4B local model
        # should not block the whole browser task for the full 30s+ timeout
        # when one small DOM decision is all that is required.
        timeout = max(12.0, min(configured_timeout, 20.0)) if live_fast else max(12.0, min(configured_timeout, 30.0))
        if timeout_override is not None:
            # Emergency-budget mode: the caller's bound wins (no 12s floor).
            timeout = max(1.0, float(timeout_override))

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": OLLAMA_ACTION_PLAN_SCHEMA,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "num_predict": (num_predict_override or (64 if live_fast else 140)),
                "num_ctx": 1536 if live_fast else 3072,
                "top_k": 20,
                "top_p": 0.8,
            },
        }

        started = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeout,
                    connect=min(
                        timeout,
                        10.0,
                    ),
                )
            ) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                )

                response.raise_for_status()

                body = response.json()

        except httpx.TimeoutException as exc:
            elapsed = (
                time.perf_counter()
                - started
            )

            logger.error(
                "Ollama timed out after %.1fs: %s",
                elapsed,
                exc,
            )
            self._cooldown_until = time.monotonic() + self._cooldown_seconds
            logger.warning(
                "Ollama cooldown set to %.0fs after timeout.",
                self._cooldown_seconds,
            )

            raise RuntimeError(
                "Local Ollama planner timed out "
                f"after {elapsed:.1f}s."
            ) from exc

        except httpx.HTTPStatusError as exc:
            elapsed = (
                time.perf_counter()
                - started
            )

            logger.error(
                "Ollama HTTP error after %.1fs: "
                "status=%s body=%s",
                elapsed,
                exc.response.status_code,
                exc.response.text[:1000],
            )
            self._cooldown_until = time.monotonic() + self._cooldown_seconds
            logger.warning(
                "Ollama cooldown set to %.0fs after HTTP failure.",
                self._cooldown_seconds,
            )

            raise RuntimeError(
                "Local Ollama returned HTTP "
                f"{exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc

        except Exception as exc:
            elapsed = (
                time.perf_counter()
                - started
            )

            logger.error(
                "Ollama failed after %.1fs: %s",
                elapsed,
                exc,
            )
            self._cooldown_until = time.monotonic() + self._cooldown_seconds
            logger.warning(
                "Ollama cooldown set to %.0fs after provider failure.",
                self._cooldown_seconds,
            )

            raise RuntimeError(
                f"Local Ollama planner failed: {exc}"
            ) from exc

        elapsed = (
            time.perf_counter()
            - started
        )

        text = body.get(
            "response"
        )

        if not text:
            raise RuntimeError(
                "Ollama returned an empty response."
            )

        # Reuse GeminiProvider's parser utilities
        # without inheriting from GeminiProvider.
        parser = GeminiProvider.__new__(
            GeminiProvider
        )

        try:
            plan = parser._parse_plan(
                text,
                instruction=instruction,
            )

        except RuntimeError as exc:
            logger.warning(
                "Ollama returned an unusable plan "
                "after %.1fs: %s",
                elapsed,
                exc,
            )

            raise RuntimeError(
                f"Ollama returned an unusable action plan: {exc}"
            ) from exc

        logger.info(
            "Ollama plan completed in %.1fs: "
            "actions=%d done=%s",
            elapsed,
            len(plan.actions),
            plan.done,
        )

        return plan, elapsed

    async def generate_plan(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None = None,
        limits: "CallLimits | None" = None,
    ) -> ActionPlan:
        instruction = instruction.strip()

        if not instruction:
            raise ValueError("Instruction must not be empty.")

        if looks_sensitive(instruction):
            raise RuntimeError(
                "Sensitive actions require explicit confirmation."
            )

        if limits is not None:
            return await self._generate_bounded(instruction, page_context, limits)

        now = time.monotonic()
        if now < self._cooldown_until:
            remaining = max(1, int(self._cooldown_until - now))
            raise RuntimeError(
                "Ollama temporarily unavailable; cooldown active for about "
                f"{remaining}s."
            )

        live_fast = page_context is not None
        prompt = (
            self._build_live_micro_prompt(instruction, page_context)
            if live_fast
            else self._build_ollama_prompt(instruction, page_context, repair=False)
        )

        logger.info(
            "Ollama planning started: model=%s context=%s live_fast=%s prompt_chars=%d",
            self.model,
            page_context is not None,
            live_fast,
            len(prompt),
        )

        try:
            plan, _ = await self._request_plan(
                prompt,
                instruction,
                live_fast=live_fast,
            )
            return plan
        except RuntimeError as first_error:
            # A timeout means the local model is simply too slow for this
            # step; repeating the same expensive request wastes the task
            # budget. Only retry malformed/empty output, where a tighter
            # recovery prompt can genuinely help.
            message = str(first_error).lower()
            malformed = (
                "invalid json" in message
                or "unusable action plan" in message
                or "empty response" in message
            )

            if not malformed:
                raise

            repair_prompt = self._build_ollama_prompt(
                instruction,
                page_context,
                repair=True,
            )

            logger.info(
                "Retrying Ollama locally in structured-output recovery mode."
            )

            try:
                plan, _ = await self._request_plan(repair_prompt, instruction)
                logger.info("Ollama recovery attempt succeeded.")
                return plan
            except Exception as second_error:
                logger.warning(
                    "Ollama recovery attempt failed: %s",
                    second_error,
                )
                raise RuntimeError(
                    "Ollama failed both the primary and local recovery "
                    "planning attempts. "
                    f"First error: {first_error}; "
                    f"Recovery error: {second_error}"
                ) from second_error

    async def _generate_bounded(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None,
        limits: "CallLimits",
    ) -> ActionPlan:
        """Emergency-planner path used by the adaptive loop.

        Every HTTP request - the initial one AND a JSON repair - is charged to
        ``limits`` (max requests, per-request timeout, total wall time), so a
        malformed answer can no longer turn one 20s call into a 20s+30s one.
        Repair is attempted at most once, only for malformed output, only when
        the budget still allows a request, and it reuses the compact live
        prompt instead of the large full-policy prompt.
        """
        now = time.monotonic()
        if now < self._cooldown_until:
            remaining = max(1, int(self._cooldown_until - now))
            raise RuntimeError(
                f"Ollama temporarily unavailable; cooldown active for about {remaining}s."
            )
        limits.begin()
        prompt = self._build_live_micro_prompt(instruction, page_context)
        limits.note_request()
        try:
            plan, _ = await self._request_plan(
                prompt, instruction, live_fast=True,
                timeout_override=limits.next_timeout(),
            )
            return plan
        except RuntimeError as first_error:
            message = str(first_error).lower()
            malformed = (
                "invalid json" in message
                or "unusable action plan" in message
                or "empty response" in message
            )
            if not malformed or not limits.can_request(min_needed_s=5.0):
                raise
            repair_prompt = prompt + (
                "\n\nYour previous answer was not a valid action plan. Reply with ONE JSON "
                "object only: {\"actions\": [ONE action], \"done\": false}."
            )
            limits.note_request()
            logger.info("Ollama bounded repair request (%d/%d requests, %.1fs left).",
                        limits.requests_used, limits.max_requests, limits.time_left())
            try:
                plan, _ = await self._request_plan(
                    repair_prompt, instruction, live_fast=True,
                    timeout_override=limits.next_timeout(),
                )
                return plan
            except Exception as second_error:
                raise RuntimeError(
                    "Ollama failed the primary and bounded repair requests. "
                    f"First error: {first_error}; Repair error: {second_error}"
                ) from second_error


# ============================================================
# AUTO PROVIDER
# ============================================================

class AutoProvider(AIProvider):
    """
    Adaptive provider orchestration.

    Normal order for live browser reasoning when AUTO_PREFER_LOCAL=true:
      1. Ollama Qwen3:4B (local primary)
      2. Gemini 3.8 Flash (remote fallback)
      3. Safe deterministic fallback

    If Gemini is rate-limited/unavailable, GeminiProvider enters a cooldown
    so later browser steps do not repeatedly hit the same failing endpoint.
    """

    def __init__(self) -> None:
        self.local = OllamaProvider()
        self.remote = (
            GeminiProvider()
            if settings.GEMINI_API_KEY
            else None
        )
        self.fallback = FallbackProvider()

    def _simple_open_task(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None,
    ) -> ActionPlan | None:
        if page_context is not None:
            return None

        normalized = re.sub(r"\s+", " ", instruction.lower()).strip()

        # This is NOT a full predefined workflow. It is only a safe
        # bootstrap for the first navigation when the user explicitly names
        # a website. Small local models often know the site name but fail to
        # emit a valid URL; doing this tiny step deterministically lets the
        # adaptive planner take over once the real DOM is available.
        sites = {
            "youtube": "https://www.youtube.com",
            "github": "https://github.com",
            "google": "https://www.google.com",
            "wikipedia": "https://www.wikipedia.org",
            "chatgpt": "https://chatgpt.com",
            "facebook": "https://www.facebook.com",
            "instagram": "https://www.instagram.com",
            "linkedin": "https://www.linkedin.com",
            "reddit": "https://www.reddit.com",
            "x": "https://x.com",
            "mdn": "https://developer.mozilla.org",
            "mdn web docs": "https://developer.mozilla.org",
            "mozilla developer network": "https://developer.mozilla.org",
            "coursera": "https://www.coursera.org",
            "stack overflow": "https://stackoverflow.com",
            "stackoverflow": "https://stackoverflow.com",
            "python": "https://www.python.org",
            "python.org": "https://www.python.org",
            "npm": "https://www.npmjs.com",
            "pypi": "https://pypi.org",
            "kaggle": "https://www.kaggle.com",
            "hugging face": "https://huggingface.co",
            "huggingface": "https://huggingface.co",
            "w3schools": "https://www.w3schools.com",
            "geeksforgeeks": "https://www.geeksforgeeks.org",
            "geeks for geeks": "https://www.geeksforgeeks.org",
            "arxiv": "https://arxiv.org",
            "medium": "https://medium.com",
        }

        # Accept a named website followed by the rest of a multi-step task,
        # e.g. "Open MDN Web Docs, search for JavaScript promises...".
        match = re.match(
            r"^(?:open|go\s+to|visit)\s+(?:the\s+)?(?P<site>[^,.;]+?)"
            r"(?:\s+(?:website|site|page))?(?=\s*(?:,|;|\band\b|\bthen\b|$))",
            normalized,
        )
        if match:
            site_name = re.sub(
                r"\s+(website|site|page)$",
                "",
                match.group("site").strip(),
            ).strip()
            url = sites.get(site_name)
            if url:
                return ActionPlan(
                    actions=[
                        {
                            "type": "navigate",
                            "url": url,
                            "reason": (
                                "Bootstrap the explicitly named website; "
                                "adaptive planning will continue from its live DOM."
                            ),
                        }
                    ],
                    source="ai",
                    done=False,
                )

            # For an explicit but less-common website name, use a generic
            # search-engine bootstrap instead of asking the small local model
            # to invent a domain. The adaptive planner takes over after the
            # search results are visible. This is deliberately generic rather
            # than a site-specific workflow.
            generic_site_phrases = {
                "a website",
                "a site",
                "website",
                "site",
                "a new tab",
                "chrome",
            }
            if (
                site_name not in generic_site_phrases
                and 1 <= len(site_name.split()) <= 8
                and not re.search(r"[\[\]<>{}]", site_name)   # never search for a template placeholder
            ):
                return ActionPlan(
                    actions=[
                        {
                            "type": "navigate",
                            "url": (
                                "https://www.google.com/search?q="
                                + quote_plus(site_name)
                            ),
                            "reason": (
                                "Find the explicitly named website; "
                                "adaptive planning will choose the relevant result."
                            ),
                        }
                    ],
                    source="ai",
                    done=False,
                )

        # Also support an explicit URL at the beginning of a task.
        url_match = re.match(
            r"^(?:open|go\s+to|visit)\s+(?P<url>https?://[^\s,;]+)",
            normalized,
            flags=re.IGNORECASE,
        )
        if url_match:
            return ActionPlan(
                actions=[
                    {
                        "type": "navigate",
                        "url": url_match.group("url").rstrip(".)"),
                        "reason": "Bootstrap the explicitly supplied URL.",
                    }
                ],
                source="ai",
                done=False,
            )

        return None

    async def generate_plan(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None = None,
    ) -> ActionPlan:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("Instruction must not be empty.")

        simple_plan = self._simple_open_task(instruction, page_context)
        if simple_plan is not None:
            return simple_plan

        remote_error: Exception | None = None
        local_error: Exception | None = None

        # LOCAL-FIRST: the local model is the normal adaptive planner.
        # Gemini is deliberately reserved for a genuine local failure so a
        # slow/ambiguous task does not burn remote quota on every step.
        if settings.AUTO_PREFER_LOCAL:
            try:
                logger.info("Trying Ollama local adaptive planner (preferred)")
                return await self.local.generate_plan(instruction, page_context=page_context)
            except Exception as exc:
                local_error = exc
                logger.warning("Ollama local planner failed; considering Gemini fallback: %s", exc)

            if self.remote is not None and settings.AUTO_ALLOW_GEMINI_FALLBACK:
                try:
                    logger.info("Trying Gemini fallback adaptive planner")
                    return await self.remote.generate_plan(instruction, page_context=page_context)
                except Exception as exc:
                    remote_error = exc
                    logger.warning("Gemini fallback planner unavailable: %s", exc)
        else:
            if self.remote is not None:
                try:
                    logger.info("Trying Gemini adaptive planner")
                    return await self.remote.generate_plan(instruction, page_context=page_context)
                except Exception as exc:
                    remote_error = exc
                    logger.warning("Gemini planner unavailable; trying Ollama: %s", exc)
            try:
                logger.info("Trying Ollama adaptive planner")
                return await self.local.generate_plan(instruction, page_context=page_context)
            except Exception as exc:
                local_error = exc
                logger.warning("Ollama planner unavailable: %s", exc)

        try:
            return await self.fallback.generate_plan(instruction, page_context=page_context)
        except Exception as fallback_error:
            details = []
            if local_error is not None:
                details.append(f"Ollama: {local_error}")
            if remote_error is not None:
                details.append(f"Gemini: {remote_error}")
            if not details:
                details.append(f"Fallback: {fallback_error}")
            raise RuntimeError("No adaptive AI planner was available. " + " | ".join(details)) from fallback_error


# ============================================================
# FALLBACK PROVIDER
# ============================================================

class FallbackProvider(AIProvider):
    """
    Deterministic fallback.

    Handles only safe/simple tasks.

    Complex browser reasoning must use Ollama or Gemini.
    """

    async def generate_plan(
        self,
        instruction: str,
        page_context: str | dict[str, Any] | None = None,
    ) -> ActionPlan:
        instruction = instruction.strip()

        if not instruction:
            raise ValueError(
                "Instruction must not be empty."
            )

        if looks_sensitive(instruction):
            raise RuntimeError(
                "Sensitive actions are not supported "
                "by fallback planning."
            )

        # A live browser step cannot safely be guessed.
        if page_context is not None:
            raise RuntimeError(
                "No local general-purpose planner is available "
                "for this live browser step."
            )

        # ----------------------------------------------------
        # Explicit URL
        # ----------------------------------------------------

        match = re.search(
            r"https?://[^\s]+",
            instruction,
            flags=re.IGNORECASE,
        )

        if match:
            url = match.group(0).rstrip(
                ".,)"
            )

            return ActionPlan(
                actions=[
                    {
                        "type": "navigate",
                        "url": url,
                        "reason": (
                            "Open the explicitly "
                            "requested URL."
                        ),
                    }
                ],
                source="fallback",
                done=False,
            )

        # ----------------------------------------------------
        # Simple website names
        # ----------------------------------------------------

        sites = {
            "github": "https://github.com",
            "youtube": "https://www.youtube.com",
            "google": "https://www.google.com",
            "wikipedia": "https://www.wikipedia.org",
            "chatgpt": "https://chatgpt.com",
        }

        normalized = re.sub(
            r"\s+",
            " ",
            instruction.lower(),
        ).strip()

        match = re.fullmatch(
            r"(?:open|go to|visit)\s+"
            r"(?:the\s+)?(.+)",
            normalized,
        )

        if match:
            site = match.group(1).strip()

            site = re.sub(
                r"\s+(website|site|page)$",
                "",
                site,
            ).strip()

            if site in sites:
                return ActionPlan(
                    actions=[
                        {
                            "type": "navigate",
                            "url": sites[site],
                            "reason": (
                                "Open the requested "
                                "website."
                            ),
                        }
                    ],
                    source="fallback",
                    done=False,
                )

        raise RuntimeError(
            "No safe deterministic fallback is available "
            "for this task."
        )


# ============================================================
# SINGLETON PROVIDER
# ============================================================

_provider_instance: AIProvider | None = None
_provider_lock = threading.Lock()


def get_ai_provider() -> AIProvider:
    global _provider_instance

    if _provider_instance is not None:
        return _provider_instance

    with _provider_lock:
        if _provider_instance is not None:
            return _provider_instance

        provider_name = (
            settings.AI_PROVIDER
            .lower()
            .strip()
        )

        if provider_name == "auto":
            from providers.registry import order_for
            extra = [n for n in order_for("auto") if n not in ("gemini", "ollama")]
            _provider_instance = RegistryProvider("auto") if extra else AutoProvider()

        elif provider_name == "ollama":
            _provider_instance = OllamaProvider()

        elif provider_name == "gemini":
            if not settings.GEMINI_API_KEY:
                raise RuntimeError(
                    "AI_PROVIDER=gemini but "
                    "GEMINI_API_KEY is not configured."
                )

            _provider_instance = GeminiProvider()

        elif provider_name == "fallback":
            _provider_instance = FallbackProvider()

        elif provider_name in ("groq", "claude", "openai", "grok", "xai", "anthropic"):
            _provider_instance = RegistryProvider(provider_name)

        else:
            raise RuntimeError(
                "Unsupported AI_PROVIDER: "
                f"{settings.AI_PROVIDER}"
            )

        return _provider_instance


def reset_ai_provider() -> None:
    global _provider_instance, _adaptive_providers

    with _provider_lock:
        _provider_instance = None
        _adaptive_providers = None
    try:
        from providers.registry import reset_registry
        reset_registry()
    except Exception:
        pass

# ============================================================
# ADAPTIVE PLANNER PROVIDERS (Gemini primary, Ollama emergency)
# ============================================================

_adaptive_providers: dict[str, Any] | None = None


def get_adaptive_providers() -> dict[str, Any]:
    """Provider instances used directly by the adaptive loop: ``{name: adapter | None}`` for gemini, ollama,
    openai, claude and grok.  Built by the central registry (providers/registry.py); a provider that is not
    configured / enabled is None.  Which of them a task may use, and in what order, is decided per task by
    ``providers.registry.order_for``.
    """
    global _adaptive_providers
    if _adaptive_providers is not None:
        return _adaptive_providers
    from providers.registry import get_adapters
    with _provider_lock:
        _adaptive_providers = dict(get_adapters())
        return _adaptive_providers


class RegistryProvider(AIProvider):
    """Non-adaptive (initial plan) provider backed by the registry: tries the configured order, first success wins.

    Only used when AI_PROVIDER is a hosted provider, or AUTO_PROVIDER_ORDER lists a hosted provider.
    """

    def __init__(self, selection: str) -> None:
        from providers.registry import normalise_selection
        self.selection = normalise_selection(selection)

    async def generate_plan(self, instruction: str, page_context: str | dict[str, Any] | None = None) -> ActionPlan:
        from agent.provider_health import FailureKind, classify_failure, global_cooldown_remaining, set_global_cooldown
        from providers.registry import get_adapters, order_for
        errors: list[str] = []
        adapters = get_adapters()
        for name in order_for(self.selection):
            adapter = adapters.get(name)
            if adapter is None:
                errors.append(f"{name}: not configured")
                continue
            if global_cooldown_remaining(name) > 0:
                errors.append(f"{name}: cooling down")
                continue
            try:
                return await adapter.generate_plan(instruction, page_context=page_context)
            except Exception as exc:  # noqa: BLE001
                kind = classify_failure(exc)
                if kind in (FailureKind.QUOTA, FailureKind.UNAVAILABLE, FailureKind.AUTH, FailureKind.MODEL_UNAVAILABLE):
                    set_global_cooldown(name, float(getattr(settings, "HOSTED_PROVIDER_COOLDOWN_SECONDS", 300.0)))
                errors.append(f"{name}: {kind.value}")
        raise RuntimeError("No configured AI provider could plan this task (" + "; ".join(errors) + ")")
