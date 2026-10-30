"""Clause splitting for multi-step natural-language goals.

Lives in its own module (not inside orchestrator.py) on purpose: a stale or
partially-copied orchestrator.py can no longer fail mid-task with
``NameError: _CLAUSE_SPLIT``.  The orchestrator imports ``split_goal_clauses``
at module import time, so any mismatch between files fails LOUDLY AT STARTUP
(ImportError) instead of in the middle of a user task.

Stdlib only.
"""
from __future__ import annotations

import re

CLAUSE_SPLIT = re.compile(
    r"\s*(?:,|;|\bthen\b|\bafter\s+that\b|\band\b)\s*",
    re.IGNORECASE,
)

NAV_CLAUSE = re.compile(
    r"^\s*(?:please\s+)?(?:open|go\s+to|goto|visit|click(?:\s+on)?|select|choose|navigate\s+to|tap|expand)\b(?P<rest>.*)$",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def split_goal_clauses(instruction: str) -> list[str]:
    """Split an instruction into ordered clauses (URLs removed, empties dropped)."""
    text = URL_RE.sub(" ", str(instruction or ""))
    return [p.strip() for p in CLAUSE_SPLIT.split(text) if p and p.strip()]


def navigation_clauses(instruction: str) -> list[tuple[str, str]]:
    """(clause, text-after-verb) for each open/click/go-to style clause."""
    out: list[tuple[str, str]] = []
    for part in split_goal_clauses(instruction):
        m = NAV_CLAUSE.match(part)
        if m:
            out.append((part, m.group("rest")))
    return out
