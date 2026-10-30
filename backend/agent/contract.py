"""Authoritative task contract.

One immutable record of WHAT THE USER ASKED FOR, and one place that decides whether it has been achieved:

    instruction  ->  ordered Requirements (id, kind, value, expected postcondition, verification strategy)
    every Requirement carries its own state and its own evidence
    task status "success / VERIFIED_COMPLETE"  <=>  every requirement is VERIFIED_COMPLETE with evidence

Nothing here talks to a browser or an AI.  A click, a URL change, an AI "done" or an empty action list is only
evidence for the specific outcome it establishes - it never completes the task by itself, and a skipped
requirement is never verified.

Stdlib only (plus agent.goals).
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from agent.goals import Goal, GoalKind, GoalStatus, mask_value, redact_params


class ReqState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    ACTION_SUCCEEDED = "ACTION_SUCCEEDED"       # the action ran; its postcondition is NOT yet proven
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"     # postcondition observed on the live page
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {ReqState.VERIFIED_COMPLETE, ReqState.BLOCKED, ReqState.FAILED, ReqState.CANCELLED}

_VERIFY_STRATEGY = {
    GoalKind.NAVIGATE: "browser host matches the requested site",
    GoalKind.NEW_TAB: "a new tab is open on the requested page",
    GoalKind.SEARCH: "query typed into a search field, submitted, results state observed",
    GoalKind.TYPE: "typed text read back from the intended field",
    GoalKind.FILL: "value read back from the matching field on the live page",
    GoalKind.CLICK: "the click produced the expected page-state change",
    GoalKind.PRESS: "the key press produced an observable change",
    GoalKind.COMPOSE: "recipient, subject and body read back from the composer",
    GoalKind.SEND: "the user confirmed and the message left the composer",
    GoalKind.EXTRACT: "the extracted text exists in the live DOM",
    GoalKind.ADAPTIVE: "weak: observed page-state change attributable to this step",
}


@dataclass
class Requirement:
    id: int
    kind: str
    text: str
    site: str | None = None
    field_intent: str | None = None
    value: str | None = None                    # the exact value requested (kept raw in memory, masked in to_dict)
    depends_on: list[int] = field(default_factory=list)
    expected: str = ""
    verification: str = ""
    needs_confirmation: bool = False
    state: ReqState = ReqState.PENDING
    evidence: dict = field(default_factory=dict)
    blocker: str | None = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        v = self.value
        if v and self.field_intent in ("phone", "email"):
            v = mask_value(v)
        return {"id": self.id, "kind": self.kind, "text": self.text[:200], "site": self.site,
                "field_intent": self.field_intent, "value": v, "depends_on": self.depends_on,
                "expected": self.expected, "verification": self.verification,
                "needs_confirmation": self.needs_confirmation, "state": self.state.value,
                "evidence": _small(self.evidence), "blocker": self.blocker}


def _small(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if k in ("typed_text", "value", "password"):
            continue
        out[k] = v if isinstance(v, (int, float, bool, type(None))) else str(v)[:160]
    return out


@dataclass
class TaskContract:
    task_id: str
    instruction: str                            # immutable: never rewritten by any stage
    owner_id: str | None = None
    requirements: list[Requirement] = field(default_factory=list)
    provider: str | None = None                 # the planner that actually answered (never the configured one)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # -- construction -----------------------------------------------------------------------------
    @classmethod
    def from_goals(cls, instruction: str, goals: Iterable[Goal], task_id: str | None = None,
                   owner_id: str | None = None) -> "TaskContract":
        reqs: list[Requirement] = []
        prev: int | None = None
        for g in goals:
            p = g.params
            r = Requirement(
                id=g.id, kind=g.kind.value, text=g.text, site=p.get("site") or p.get("url"),
                field_intent=p.get("field"), value=(p.get("value") if g.kind == GoalKind.FILL else None),
                depends_on=[prev] if prev is not None else [],
                expected=_expected(g), verification=_VERIFY_STRATEGY.get(g.kind, ""),
                needs_confirmation=g.kind == GoalKind.SEND)
            if p.get("secret"):
                r.state, r.blocker = ReqState.BLOCKED, f"{p.get('field')} entry is never automated"
            reqs.append(r)
            prev = g.id
        return cls(task_id=task_id or uuid.uuid4().hex, instruction=instruction, owner_id=owner_id, requirements=reqs)

    # -- state ------------------------------------------------------------------------------------
    def sync(self, goals: Iterable[Goal]) -> None:
        """Mirror goal outcomes. COMPLETED needs evidence/note to count as VERIFIED, otherwise ACTION_SUCCEEDED."""
        by_id = {g.id: g for g in goals}
        for r in self.requirements:
            g = by_id.get(r.id)
            if g is None:
                continue
            r.evidence = dict(g.evidence or {})
            if g.params.get("_skipped") and not g.evidence.get("state_verified"):
                r.state = ReqState.ACTION_SUCCEEDED                 # a SKIPPED goal is never verified
                r.blocker = "skipped without proof that the page is already in the required state"
            elif g.status == GoalStatus.COMPLETED:
                proven = bool(g.evidence) or bool(g.note)
                r.state = ReqState.VERIFIED_COMPLETE if proven else ReqState.ACTION_SUCCEEDED
            elif g.status == GoalStatus.IN_PROGRESS:
                r.state = ReqState.RUNNING
            elif g.status == GoalStatus.BLOCKED:
                r.state, r.blocker = ReqState.BLOCKED, g.note
            elif g.status == GoalStatus.FAILED:
                r.state, r.blocker = ReqState.FAILED, g.note
            else:
                if r.state != ReqState.BLOCKED:
                    r.state = ReqState.PENDING
            r.updated_at = time.time()
        self.updated_at = time.time()

    def unmet(self) -> list[Requirement]:
        return [r for r in self.requirements if r.state != ReqState.VERIFIED_COMPLETE]

    def verified(self) -> list[Requirement]:
        return [r for r in self.requirements if r.state == ReqState.VERIFIED_COMPLETE]

    def is_verified_complete(self) -> bool:
        """THE completeness invariant: at least one requirement, and every one VERIFIED_COMPLETE."""
        return bool(self.requirements) and not self.unmet()

    @property
    def state(self) -> ReqState:
        if self.is_verified_complete():
            return ReqState.VERIFIED_COMPLETE
        states = {r.state for r in self.requirements}
        for s in (ReqState.FAILED, ReqState.BLOCKED, ReqState.CANCELLED):
            if s in states:
                return s
        if ReqState.RUNNING in states:
            return ReqState.RUNNING
        if ReqState.ACTION_SUCCEEDED in states:
            return ReqState.ACTION_SUCCEEDED
        return ReqState.PENDING

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "owner_id": self.owner_id, "instruction": redact_instruction(self.instruction),
                "state": self.state.value, "provider": self.provider,
                "requirements": [r.to_dict() for r in self.requirements],
                "created_at": self.created_at, "updated_at": self.updated_at}

    def planner_view(self) -> dict:
        """What a planner is told: the whole contract, split into verified and remaining."""
        return {"original_instruction": redact_instruction(self.instruction),
                "verified": [r.to_dict() for r in self.verified()],
                "remaining": [r.to_dict() for r in self.unmet()]}


def _expected(g: Goal) -> str:
    p = g.params
    k = g.kind
    if k == GoalKind.FILL:
        return f"the {p.get('field')} field on the live page holds the requested value; nothing is submitted"
    if k == GoalKind.NAVIGATE:
        return f"the browser is on {p.get('site') or p.get('url')}"
    if k == GoalKind.SEARCH:
        return f"results for {p.get('query')!r} are shown"
    if k == GoalKind.TYPE:
        return "the text is present in the intended field"
    return g.label()


# --------------------------------------------------------------------------------------------------
# coverage: every explicit value in the user's words must be represented by a goal
# --------------------------------------------------------------------------------------------------
_PHONE = re.compile(r"(?<![\w.@/-])\+?\d[\d\s().-]{6,}\d(?![\w@])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_QUOTED = re.compile(r"\"([^\"]{2,})\"|“([^”]{2,})”")
_URLS = re.compile(r"https?://\S+|\b(?:www\.)?[\w-]+\.(?:com|org|net|in|io|co|edu|gov|app|dev)\b\S*", re.I)
_SECRETISH = re.compile(r"\b(?:password|passcode|otp|pin|cvv)\b", re.I)


def explicit_values(instruction: str) -> list[tuple[str, str]]:
    """[(kind, raw value)] of phone numbers, e-mail addresses and quoted strings in the user's words."""
    text = _URLS.sub(" ", instruction or "")
    out: list[tuple[str, str]] = []
    for m in _EMAIL.finditer(instruction or ""):
        out.append(("email", m.group(0)))
    no_email = _EMAIL.sub(" ", text)
    for m in _PHONE.finditer(no_email):
        digits = re.sub(r"\D", "", m.group(0))
        if 7 <= len(digits) <= 15:
            out.append(("phone", m.group(0).strip()))
    for m in _QUOTED.finditer(instruction or ""):
        out.append(("quoted", (m.group(1) or m.group(2)).strip()))
    return out


def _represented(kind: str, value: str, goals: Iterable[Goal]) -> bool:
    digits = re.sub(r"\D", "", value)
    for g in goals:
        if g.kind == GoalKind.ADAPTIVE:
            continue                              # free text is NOT a representation: it can drop the value silently
        for k, v in g.params.items():
            if k.startswith("_") or not isinstance(v, str):
                continue
            if kind == "phone" and digits and digits in re.sub(r"\D", "", v):
                return True
            if kind != "phone" and value.lower() in v.lower():
                return True
    return False


def coverage_gaps(instruction: str, goals: Iterable[Goal]) -> list[dict]:
    """Explicit values that no goal carries.  A non-empty result means a requirement would be silently dropped."""
    goals = list(goals)
    gaps = []
    for kind, value in explicit_values(instruction):
        if not _represented(kind, value, goals):
            gaps.append({"kind": kind, "value": value})
    return gaps


def gap_question(gaps: list[dict]) -> str:
    g = gaps[0]
    shown = mask_value(g["value"]) if g["kind"] in ("phone", "email") else repr(g["value"][:60])
    return (f"I could not turn the {g['kind']} {shown} in your request into a step I can verify. "
            "Tell me where it goes, for example: \"type it in the phone number field\".")


def redact_instruction(text: str, mask_ids: bool = True) -> str:
    """Instruction text for logs/history/display: secrets after 'password/otp/pin ... is|as|=' are removed and, with
    ``mask_ids`` (default), phone numbers / e-mail addresses are masked.  A provider prompt uses mask_ids=False so the
    model can still see the identifier the user asked for."""
    t = str(text or "")
    if mask_ids:
        t = _EMAIL.sub(lambda m: mask_value(m.group(0)), t)
        t = _PHONE.sub(lambda m: mask_value(m.group(0)) if 7 <= len(re.sub(r"\D", "", m.group(0))) <= 15 else m.group(0), t)
    t = re.sub(r"(?i)\b(password|passcode|otp|pin|cvv|one[\s-]time\s+(?:password|code))\b(\s*(?:is|as|=|:|to|with)?\s*)(\S+)",
               lambda m: f"{m.group(1)}{m.group(2)}[hidden]", t)
    t = re.sub(r"(?i)\b(sk-[A-Za-z0-9_-]{8,}|gsk_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{10,})", "[hidden]", t)
    return t


# --------------------------------------------------------------------------------------------------
# ONE policy for "does this deterministic plan finish the whole task?"  (router, orchestrator, planner wrappers)
# --------------------------------------------------------------------------------------------------
def fast_path_covers(instruction: str, action_types: Iterable[str], resolver=None) -> dict:
    """A fast path is BOOTSTRAP ONLY unless every requirement of the instruction is covered by its actions.

    Returns {"complete": bool, "covered": [goal ids], "remaining": [goal ids], "reason": str}.
    A navigation-only plan covers the navigation goals and nothing else - never a fill, search, click, compose...
    """
    from agent.goals import decompose
    types = {str(t) for t in action_types}
    goals = decompose(instruction, resolver)
    nav_only = types <= {"navigate", "new_tab"}
    covered = [g.id for g in goals if nav_only and g.kind in (GoalKind.NAVIGATE, GoalKind.NEW_TAB)]
    if nav_only and len(goals) == 1 and goals[0].kind == GoalKind.CLICK and goals[0].params.get("maybe_site"):
        covered = [goals[0].id]                      # "open <name>" whose name the planner resolved to a site
    remaining = [g.id for g in goals if g.id not in covered]
    gaps = coverage_gaps(instruction, goals)
    complete = bool(goals) and not remaining and not gaps
    reason = ("the plan covers every requirement" if complete else
              f"fast path covers goals {covered}; remaining goals {remaining}" + (f"; uncovered values {[g['kind'] for g in gaps]}" if gaps else ""))
    return {"complete": complete, "covered": covered, "remaining": remaining, "reason": reason}


def unmet_typed_values(instruction: str, actions: Iterable[dict]) -> list[str]:
    """Legacy (single-flow) loop: explicit values that no executed TYPE action carried.  Empty list = nothing missing."""
    typed = []
    for a in actions:
        a = a.get("action") if isinstance(a, dict) and "action" in a else a
        if isinstance(a, dict) and str(a.get("type")) in ("type", "ActionType.TYPE") and a.get("text"):
            typed.append(str(a["text"]))
    blob = " ".join(typed).lower()
    digits = re.sub(r"\D", "", blob)
    missing = []
    for kind, value in explicit_values(instruction):
        if kind == "quoted":
            continue              # quoted text may be a search query / literal handled by the search contract
        ok = (re.sub(r"\D", "", value) in digits) if kind == "phone" else (value.lower() in blob)
        if not ok:
            missing.append(kind)
    return missing
