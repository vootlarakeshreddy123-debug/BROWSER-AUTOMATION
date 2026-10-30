"""Consequence classification and confirmation policy (stdlib only).

Distinctions NEXA keeps explicit:  drafting != sending, preparing != publishing, selecting != purchasing,
reviewing != deleting.  Anything on the right-hand side is *consequential* and is never executed without the
user's explicit confirmation, whatever the model or the instruction's wording suggested.

Labels are matched on the control's visible/accessible name, not on any website.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEND, PUBLISH, PURCHASE, DELETE, SUBMIT = "send", "publish", "purchase", "delete", "submit"

_PATTERNS: list[tuple[str, re.Pattern]] = [
    (SEND, re.compile(r"^\s*(?:send|send\s+(?:now|message|email|e-?mail|reply|invite)|reply\s+all|send\s*&\s*\w+)\s*$|\bsend\s+(?:now|message|email|e-?mail|invitation)\b", re.I)),
    (PUBLISH, re.compile(r"\b(?:publish|post(?:\s+(?:now|reply|comment|tweet|update))?|tweet|share\s+now|go\s+live|submit\s+(?:post|review|comment))\b", re.I)),
    (PURCHASE, re.compile(r"\b(?:place\s+(?:your\s+|the\s+)?order|buy\s+now|pay(?:\s+now)?|purchase|complete\s+(?:order|purchase|payment)|confirm\s+(?:order|payment|purchase)|checkout\s+now|subscribe\s+now|donate)\b", re.I)),
    (DELETE, re.compile(r"\b(?:delete|remove\s+(?:permanently|account|all)|erase|discard\s+all|empty\s+(?:trash|bin)|close\s+account|deactivate)\b", re.I)),
    (SUBMIT, re.compile(r"^\s*(?:submit|register|agree\s+and\s+submit)\s*$", re.I)),
]

_SENSITIVE_FIELD = re.compile(r"\b(?:password|passcode|card\s*number|credit\s*card|cvv|cvc|security\s*code|ssn|social\s+security|iban|routing|otp|one[- ]time)\b", re.I)


@dataclass
class Consequence:
    kind: str
    label: str

    def describe(self) -> str:
        return {SEND: "send a message", PUBLISH: "publish / post content", PURCHASE: "make a purchase or payment",
                DELETE: "delete content", SUBMIT: "submit a form"}[self.kind] + f' (button: "{self.label[:60]}")'


def classify_label(label: str | None) -> Consequence | None:
    """Consequence of activating a control with this visible/accessible name, or None."""
    text = " ".join(str(label or "").split())
    if not text or len(text) > 80:
        return None
    for kind, pat in _PATTERNS:
        if pat.search(text):
            return Consequence(kind, text)
    return None


def is_sensitive_field(*evidence: str | None) -> bool:
    return bool(_SENSITIVE_FIELD.search(" ".join(str(e or "") for e in evidence)))


# ---- instruction-level intent -------------------------------------------------------------------
_NO_SEND = re.compile(r"(?:do\s*n[o']?t|don't|never|without|not)\s+(?:to\s+)?(?:send|submit|post|publish)", re.I)


def says_do_not_send(instruction: str) -> bool:
    return bool(_NO_SEND.search(instruction or ""))


@dataclass
class ConfirmationRequest:
    kind: str                  # send | publish | purchase | delete | submit
    description: str           # what will happen, in plain words, shown to the user
    target_label: str = ""
    goal_index: int | None = None

    def to_dict(self) -> dict:
        return {"type": "confirmation", "kind": self.kind, "question": f"Do you want me to {self.description}?",
                "description": self.description, "target": self.target_label, "options": ["approve", "decline"]}


def scrub_text(value: str | None, field_kind: str | None = None) -> str:
    """Text that may be recorded in events/history: secrets and sensitive-field content are masked."""
    if value is None:
        return ""
    if field_kind in {"password", "card", "otp"}:
        return "[hidden]"
    from providers.http import redact
    return redact(value)
