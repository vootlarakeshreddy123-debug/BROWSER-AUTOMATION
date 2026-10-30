"""Deterministic "type this text into the page's message box" workflow.

Handles instructions like "open chatgpt and type hello" without any AI call:

    navigate (bootstrap, elsewhere) -> find the composer in the LIVE DOM
    -> TYPE the exact text -> verify the editable really contains it.

Generic: the composer is chosen from the inspected DOM (textarea / input /
contenteditable / role=textbox, accessible name, placeholder, region) - there
is no site name and no site CSS selector in this module.  Pressing Enter is
never done unless the instruction explicitly asks to send/submit, and a
"send" request is left to the adaptive path (not claimed here).

Stdlib only except for the Action schema import inside ``build_type_action``.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_TYPE_VERB = r"(?:type|enter|write|input|put)"
_QUOTED = r"(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|“(?P<cq>[^”]+)”)"
_TYPE_RE = re.compile(
    rf"\b{_TYPE_VERB}\s+(?:in\s+|into\s+)?(?:{_QUOTED}|(?P<bare>[^\"'“”]+?))\s*(?:(?:,|\band\b|\bthen\b)\s*(?P<tail>.*))?$",
    re.IGNORECASE,
)
_SEND_RE = re.compile(r"\b(?:send|submit|press\s+enter|hit\s+enter|and\s+enter|post|ask\s+it)\b", re.IGNORECASE)
_SEARCH_RE = re.compile(r"\bsearch(?:\s+for)?\b|\blook\s+for\b|\bfind\b", re.IGNORECASE)
_TARGET_SUFFIX = re.compile(
    r"\s+(?:into|in|on)\s+(?:the\s+)?(?:chat|message|input|text|prompt|compos\w*|box|field|textbox)\b.*$", re.IGNORECASE
)

_LOGIN_WORDS = re.compile(r"\b(?:log\s?in|sign\s?in|sign\s?up|create\s+account|continue\s+with)\b", re.IGNORECASE)
_COMPOSER_HINT = re.compile(r"\b(?:message|ask|chat|prompt|type|write|anything|talk|reply)\b", re.IGNORECASE)
_SKIP_INPUT_TYPES = {"password", "email", "checkbox", "radio", "submit", "button", "file", "hidden", "number", "tel", "date"}


@dataclass
class TypingRequest:
    text: str
    wants_send: bool = False


def parse_typing_request(instruction: str) -> TypingRequest | None:
    text = " ".join(str(instruction or "").split())
    if not text or _SEARCH_RE.search(text):
        return None
    m = _TYPE_RE.search(text)
    if not m:
        return None
    value = m.group("dq") or m.group("sq") or m.group("cq")
    if value is None:
        value = (m.group("bare") or "").strip().rstrip(".!")
        value = _TARGET_SUFFIX.sub("", value).strip()
    if not value:
        return None
    tail = (m.group("tail") or "")
    wants_send = bool(_SEND_RE.search(tail)) or bool(_SEND_RE.search(text[m.end("bare") if m.group("bare") else m.start():]))
    if m.group("bare") and _SEND_RE.search(m.group("bare")):
        wants_send = True
        value = _SEND_RE.split(value)[0].strip(" ,")
    return TypingRequest(text=value, wants_send=wants_send)


def _editable_candidates(context: dict) -> list[dict]:
    out = []
    for el in context.get("elements") or context.get("interactive_elements") or []:
        if not isinstance(el, dict) or el.get("disabled") or el.get("visible") is False:
            continue
        tag = str(el.get("tag") or "").lower()
        role = str(el.get("role") or "").lower()
        itype = str(el.get("type") or "").lower()
        editable = (
            tag == "textarea"
            or bool(el.get("contenteditable"))
            or role in {"textbox", "searchbox"}
            or (tag == "input" and itype not in _SKIP_INPUT_TYPES)
        )
        if editable:
            out.append(el)
    return out


def pick_composer(context: dict, allow_search_boxes: bool = False) -> dict | None:
    best, best_score = None, -10**9
    for el in _editable_candidates(context):
        hay = " ".join(str(el.get(k) or "") for k in ("name", "placeholder", "aria_label", "label"))
        tag = str(el.get("tag") or "").lower()
        score = 0
        if el.get("in_main"):
            score += 30
        if tag == "textarea" or el.get("contenteditable"):
            score += 25
        if _COMPOSER_HINT.search(hay):
            score += 40
        if re.search(r"\bsearch\b", hay, re.IGNORECASE) or str(el.get("role") or "") == "searchbox" or str(el.get("type") or "") == "search":
            # A site-search box is NOT a message composer: excluded outright (a mere penalty let it win
            # whenever the real composer had not rendered yet, so text went into the wrong field).
            if not allow_search_boxes:
                continue
        if str(el.get("region") or "") in {"header", "nav", "footer", "sidebar"}:
            score -= 20
        if score > best_score:
            best, best_score = el, score
    return best


def login_wall(context: dict) -> bool:
    for el in context.get("elements") or []:
        if isinstance(el, dict) and el.get("visible") is not False and _LOGIN_WORDS.search(str(el.get("name") or el.get("text") or "")):
            return True
    return False


def target_for(el: dict) -> dict:
    """Most semantic locator first: role+name, then placeholder, aria-label, xpath last."""
    role = str(el.get("role") or "").lower()
    name = str(el.get("name") or "").strip()
    if role in {"textbox", "searchbox"} and name:
        return {"role": role, "name": name}
    if el.get("placeholder"):
        return {"placeholder": str(el["placeholder"])}
    if el.get("aria_label"):
        return {"label": str(el["aria_label"])}
    if el.get("xpath"):
        return {"xpath": str(el["xpath"])}
    return {}


def already_typed(completed_actions: list[dict], text: str) -> int:
    n = 0
    for item in completed_actions or []:
        a = (item or {}).get("action") or {}
        if str(a.get("type") or "").lower() == "type" and str(a.get("text") or "").strip().lower() == text.strip().lower():
            n += 1
    return n


VERIFY_TYPED_JS = r"""
(expected) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = n(expected);
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = Array.from(document.querySelectorAll('textarea,input,[contenteditable],[role="textbox"]')).filter(vis);
  for (const el of els) {
    const val = n(el.value !== undefined && el.value !== '' ? el.value : (el.innerText || el.textContent));
    if (val.includes(want)) return {ok: true, tag: el.tagName.toLowerCase(), id: el.id || null};
  }
  return {ok: false};
}
"""


async def verify_typed_in_dom(page: Any, text: str) -> bool:
    if page is None:
        return False
    try:
        res = await page.evaluate(VERIFY_TYPED_JS, text)
    except Exception:
        return False
    return bool(res and res.get("ok"))


@dataclass
class TypingStep:
    kind: str                       # not_applicable | act | blocked
    action: dict | None = None
    reason: str = ""
    waited_s: float = 0.0
    request: TypingRequest | None = None


async def next_typing_step(
    instruction: str,
    context: dict,
    completed_actions: list[dict],
    snapshot: Callable[[], Awaitable[dict]] | None = None,
    wait_budget_s: float = 6.0,
) -> TypingStep:
    req = parse_typing_request(instruction)
    if req is None:
        return TypingStep("not_applicable")
    if req.wants_send:
        return TypingStep("not_applicable", reason="instruction asks to send/submit; not handled deterministically", request=req)
    if already_typed(completed_actions, req.text) >= 2:
        return TypingStep("not_applicable", reason="typed twice already; verification still failing", request=req)

    waited = 0.0
    ctx = context
    while True:
        el = pick_composer(ctx)
        if el is not None:
            target = target_for(el)
            action = {"type": "type", "text": req.text, "reason": f"Type the requested text into the page's message box ({el.get('tag')})"}
            if target:
                action["target"] = target
            return TypingStep("act", action=action, waited_s=waited, request=req)
        if login_wall(ctx):
            return TypingStep(
                "blocked", waited_s=waited, request=req,
                reason="The page shows a login/sign-up wall and no message box is available, so the text cannot be typed. "
                       "Log in in the live Chrome tab and run the task again.",
            )
        if snapshot is None or waited >= wait_budget_s:
            return TypingStep("not_applicable", reason="no message box found on the live page", waited_s=waited, request=req)
        await asyncio.sleep(0.5)
        waited += 0.5
        try:
            ctx = await snapshot()
        except Exception:
            return TypingStep("not_applicable", reason="could not re-inspect the page", waited_s=waited, request=req)
