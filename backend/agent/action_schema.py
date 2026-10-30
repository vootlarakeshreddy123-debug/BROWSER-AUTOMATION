from __future__ import annotations

from enum import Enum
from typing import Literal, Optional
import re

from pydantic import BaseModel, Field, field_validator, model_validator


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    PRESS = "press"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    DOWNLOAD = "download"
    SCROLL = "scroll"
    HOVER = "hover"
    SELECT_OPTION = "select_option"
    CHECK = "check"
    UNCHECK = "uncheck"
    UPLOAD = "upload"
    # real browser/tab primitives (agent/goals.py -> goal runner)
    BACK = "back"
    FORWARD = "forward"
    NEW_TAB = "new_tab"
    SWITCH_TAB = "switch_tab"
    CLOSE_TAB = "close_tab"


class Target(BaseModel):
    """A live DOM target produced by the planner.

    Keep this model permissive enough to preserve useful evidence from the
    browser inspector, while keeping navigation/text/keyboard fields on the
    Action itself where the executor expects them.
    """

    role: Optional[str] = None
    name: Optional[str] = None
    css: Optional[str] = None
    exact: bool = False
    placeholder: Optional[str] = None
    label: Optional[str] = None
    xpath: Optional[str] = None


class Action(BaseModel):
    type: ActionType
    url: Optional[str] = None
    target: Optional[Target] = None
    text: Optional[str] = None
    key: Optional[str] = None
    direction: Literal["up", "down"] = "down"
    amount: int = Field(default=700, ge=100, le=5000)
    reason: str = Field(default="", description="Short explanation of the action.")
    value: Optional[str] = None
    file_path: Optional[str] = None

    # NEW_TAB uses ``url`` (optional: blank tab); SWITCH_TAB uses ``value`` = new|first|previous|last|other.
    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must be an absolute http(s) URL")
        return v

    @model_validator(mode="after")
    def typing_needs_text(self) -> "Action":
        # an empty typing action can never satisfy a requirement; it is rejected at the schema, not "executed"
        if self.type == ActionType.TYPE and (self.text is None or not str(self.text).strip()):
            raise ValueError("type action requires non-empty text")
        return self


class ActionPlan(BaseModel):
    """Plan returned by either the AI planner or a deterministic planner."""

    actions: list[Action] = Field(default_factory=list)
    source: Literal["fast_path", "ai", "fallback", "workflow"] = "fast_path"
    done: bool = False
    goal: Optional[str] = None
    expected_state: Optional[str] = None

    @model_validator(mode="after")
    def validate_plan(self) -> "ActionPlan":
        if not self.actions and not self.done:
            raise ValueError("plan must contain at least one action unless done=true")
        return self


SENSITIVE_ACTION_HINTS = (
    "buy", "purchase", "checkout", "pay", "payment",
    "delete", "remove", "send", "transfer", "place order",
    "confirm order", "withdraw", "refund",
)


def looks_sensitive(instruction: str) -> bool:
    """Detect requests that actually ask NEXA to perform consequential actions.

    Avoid substring matching: educational/informational requests such as
    "how to delete a file" must not be blocked merely because the word
    ``delete`` appears in the topic.
    """
    text = " ".join(str(instruction or "").lower().split())
    if not text:
        return False

    informational = re.search(
        r"\b(?:how|what|why|explain|learn|tutorial|documentation|about|meaning|difference)\b.*\b(?:buy|purchase|delete|remove|send|transfer|pay|checkout)\b",
        text,
    )
    if informational:
        return False

    direct = re.search(
        r"\b(?:buy|purchase|checkout|pay|send|transfer|withdraw|refund)\b(?:\s+(?:for|the|a|an|this|that|it|money|funds|order))?\s+",
        text,
    )
    if direct:
        return True

    return bool(re.search(
        r"(?:\bdelete\b|\bremove\b).*\b(?:file|folder|account|message|record|repository|item|post|email|data)\b|"
        r"\b(?:place|confirm)\s+(?:the\s+)?order\b|\bsubmit\s+(?:the\s+)?(?:order|payment|purchase|form)\b",
        text,
    ))
