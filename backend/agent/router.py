"""Route browser instructions to deterministic or adaptive AI planning."""
from __future__ import annotations

from typing import Any

from agent.action_schema import ActionPlan
from agent.planner import try_fast_path, try_live_path
from services.ai_provider import AIProvider, FallbackProvider


async def build_plan(
    instruction: str,
    ai_provider: AIProvider,
    page_context: str | dict[str, Any] | None = None,
) -> ActionPlan:
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("Instruction must not be empty.")

    # Initial page: use complete deterministic workflows first.
    if page_context is None:
        fast = try_fast_path(instruction)
        if fast is not None:
            from agent.contract import fast_path_covers
            from agent.site_resolver import resolve_site_url
            cov = fast_path_covers(instruction, [a.type.value for a in fast.actions], resolve_site_url)
            if not cov["complete"]:
                # bootstrap only: never presented as a finished plan; the caller must continue with the remaining goals
                fast.done = False
                fast.expected_state = "BOOTSTRAP_ONLY: " + cov["reason"]
            return fast
    else:
        # On a live page, use only the conservative generic live-path
        # optimization for obvious search actions. Complex/ambiguous tasks
        # continue to the configured AI provider, whose normal order is
        # Gemini primary -> Ollama emergency fallback.
        live = try_live_path(instruction, page_context)
        if live is not None:
            return live

    try:
        return await ai_provider.generate_plan(
            instruction,
            page_context=page_context,
        )
    except Exception as ai_error:
        # A deterministic fallback is allowed only for initial simple
        # requests. Complex live reasoning must surface the provider failure
        # so the orchestrator can use its emergency DOM recovery layer.
        if page_context is None:
            try:
                return await FallbackProvider().generate_plan(
                    instruction,
                    page_context=page_context,
                )
            except Exception:
                pass
        raise ai_error


def route_live_step(
    instruction: str,
    page_context: dict[str, Any],
) -> tuple[str, ActionPlan | None]:
    """Route ONE live (already-open page) step.

    Returns ``("deterministic", plan)`` when the conservative generic
    live-path can pick the obvious next action from the live DOM without an
    AI call, otherwise ``("adaptive", None)`` - the caller then asks the AI
    planner (Gemini primary, bounded Ollama emergency).  Deterministic paths
    are an optimisation only; nothing requires them for an unknown task.
    """
    plan = try_live_path(instruction, page_context)
    if plan is not None:
        return "deterministic", plan
    return "adaptive", None
