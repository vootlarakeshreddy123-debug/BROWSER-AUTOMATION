"""State-aware recovery ladder.

When an action has ALREADY been tried on this exact page state without any
measurable progress, repeating it is not recovery.  The ladder moves through
different strategies instead:

    0  investigate_new_page   a click may have opened a tab that was not adopted yet
    1  alternate_target       a different visible link/button in the same region
    2  scroll                 reveal content below the fold
    3  generic_dom            the generic live-DOM recovery plan
    4+ (exhausted)            honest failure

Pure functions + a tiny state holder; stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LADDER = ["investigate_new_page", "alternate_target", "scroll", "generic_dom"]


@dataclass
class LadderState:
    by_state: dict[str, int] = field(default_factory=dict)

    def next_index(self, state_key: str) -> int:
        idx = self.by_state.get(state_key, 0)
        self.by_state[state_key] = idx + 1
        return idx


def strategy_name(idx: int) -> str | None:
    return LADDER[idx] if 0 <= idx < len(LADDER) else None


def _name(el: dict) -> str:
    return str(el.get("name") or el.get("text") or "").strip().lower()


def alternate_click_target(context: dict, tried_actions: list[dict]) -> dict | None:
    """Pick a different visible, enabled link/button in the same region as what was tried."""
    tried_names = set()
    for a in tried_actions:
        t = (a or {}).get("target") or {}
        if t.get("name"):
            tried_names.add(str(t["name"]).strip().lower())
    elements = [e for e in (context.get("elements") or []) if isinstance(e, dict)]
    ref_region = None
    for e in elements:
        if _name(e) in tried_names:
            ref_region = e.get("region")
            ref_index = e.get("index", -1)
            break
    else:
        ref_index = -1
    current = str(context.get("current_url") or "")
    best = None
    for e in elements:
        if e.get("disabled") or e.get("visible") is False:
            continue
        if str(e.get("role") or "").lower() not in {"link", "button"} and str(e.get("tag") or "") not in {"a", "button"}:
            continue
        nm = _name(e)
        if not nm or nm in tried_names or len(nm) < 3:
            continue
        # Page chrome (header/nav/footer/sidebar) is never an "alternate result": clicking site
        # navigation would wander away from the task instead of recovering it.
        if e.get("region") in {"header", "footer", "nav", "sidebar"}:
            continue
        if ref_region not in (None, "header", "footer", "nav", "sidebar") and e.get("region") != ref_region:
            continue
        href = str(e.get("href") or "")
        if href and (href == current or href.startswith("#") or href.lower().startswith("javascript:")):
            continue
        if e.get("index", 0) > ref_index:       # next one after the failed target
            best = e
            break
        best = best or e
    if best is None:
        return None
    role = str(best.get("role") or "link")
    target = {"role": role, "name": str(best.get("name") or best.get("text") or "")} if (best.get("name") or best.get("text")) else {}
    if not target and best.get("xpath"):
        target = {"xpath": str(best["xpath"])}
    return {"type": "click", "target": target, "reason": f"Recovery: alternate target '{nm_or(best)}' (previous target produced no progress)"}


def nm_or(el: dict) -> str:
    return str(el.get("name") or el.get("text") or "")[:60]


def scroll_action() -> dict:
    return {"type": "scroll", "direction": "down", "amount": 700, "reason": "Recovery: reveal more of the page"}
