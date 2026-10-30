"""Generic semantic ranking of editable controls.

Given the inspector's element list and an *intent* ("search", "compose", "email", "text", ...), return the
editable controls best-first, each with the evidence that earned its score, plus the controls that were
rejected and why.  Nothing here knows a website: every decision comes from role, accessible name, label,
placeholder, input type, autocomplete, form context, region, geometry and the words of the user's target
description ("the email field", "the message box").

Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- field kinds ---------------------------------------------------------------------------------
SEARCH, COMPOSER, EMAIL, PASSWORD, PHONE, NUMBER, DATE, URL, TEXT, CHOICE, NOT_TEXT = (
    "search", "composer", "email", "password", "phone", "number", "date", "url", "text", "choice", "not_text")
# message-composition intents (recipient / subject / body of a draft)
RECIPIENT, SUBJECT, BODY = "recipient", "subject", "body"
_RECIPIENT_WORDS = re.compile(r"\b(?:to|recipients?|send\s+to|e-?mail|address(?:ee)?)\b", re.I)
_CC_WORDS = re.compile(r"\b(?:cc|bcc|carbon|blind)\b", re.I)
_SUBJECT_WORDS = re.compile(r"\bsubject\b", re.I)
_BODY_WORDS = re.compile(r"\b(?:body|message|compose|write|content|text)\b", re.I)

_NON_TEXT_INPUT = {"checkbox", "radio", "submit", "button", "file", "hidden", "image", "reset", "range", "color"}
_PRIVATE_KINDS = {EMAIL, PASSWORD, PHONE, NUMBER, DATE}

_SEARCH_WORDS = re.compile(r"\b(?:search|find|query|lookup|look\s?up|keyword)s?\b|^q$|^query$|^s$", re.I)
_COMPOSER_WORDS = re.compile(r"\b(?:message|ask|chat|prompt|compose|write|reply|talk|anything|comment|editor|body)\b", re.I)
_EMAIL_WORDS = re.compile(r"\be-?mail\b", re.I)
_PASSWORD_WORDS = re.compile(r"\b(?:password|passcode|pin)\b", re.I)
_PHONE_WORDS = re.compile(r"\b(?:phone|mobile|telephone|tel|cell)(?:[\s_-]?(?:number|no))?\b", re.I)
_SIGNUP_CONTEXT = re.compile(
    r"\b(?:newsletter|subscribe|subscription|sign[\s-]?up|signup|register|create\s+account|join|mailing\s+list|log[\s-]?in|sign[\s-]?in)\b", re.I)
_STOP = {"the", "a", "an", "into", "in", "on", "to", "of", "field", "box", "input", "text", "textbox", "bar", "area"}


@dataclass
class Candidate:
    element: dict
    score: float
    kind: str
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        e = self.element
        return str(e.get("name") or e.get("placeholder") or e.get("aria_label") or e.get("name_attr") or e.get("tag") or "?")


@dataclass
class Ranking:
    intent: str
    candidates: list[Candidate]
    rejected: list[tuple[dict, str]]

    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _s(el: dict, *keys: str) -> str:
    return " ".join(str(el.get(k) or "") for k in keys).strip()


def evidence_text(el: dict) -> str:
    """Everything the page says about this control, lower-cased."""
    return _s(el, "name", "placeholder", "aria_label", "label_text", "name_attr", "title", "autocomplete").lower()


def classify_field(el: dict) -> str:
    """What kind of data this control collects, from its own attributes first, then its labels."""
    tag = str(el.get("tag") or "").lower()
    role = str(el.get("role") or "").lower()
    itype = str(el.get("input_type") or el.get("type") or "").lower()
    if tag == "select" or (role in {"combobox", "listbox"} and tag != "input"):
        return CHOICE
    if tag == "input" and itype in _NON_TEXT_INPUT:
        return NOT_TEXT
    editable = (tag == "textarea" or bool(el.get("contenteditable")) or role in {"textbox", "searchbox"}
                or (tag == "input" and itype not in _NON_TEXT_INPUT))
    if not editable:
        return NOT_TEXT
    ev = evidence_text(el)
    auto = str(el.get("autocomplete") or "").lower()
    if itype == "password" or _PASSWORD_WORDS.search(ev):
        return PASSWORD
    if itype == "email" or "email" in auto or _EMAIL_WORDS.search(ev):
        return EMAIL
    if itype == "tel" or "tel" in auto or _PHONE_WORDS.search(ev):
        return PHONE
    if itype in {"number"}:
        return NUMBER
    if itype in {"date", "datetime-local", "time", "month", "week"}:
        return DATE
    if itype == "url":
        return URL
    if itype == "search" or role == "searchbox" or _SEARCH_WORDS.search(_s(el, "name", "placeholder", "aria_label", "label_text", "name_attr").lower()):
        return SEARCH
    if tag == "textarea" or el.get("contenteditable"):
        return COMPOSER
    return TEXT


def intent_from_target(text: str) -> str:
    """Intent implied by a target description such as 'the email field' or 'the message box'."""
    t = (text or "").lower()
    if _EMAIL_WORDS.search(t):
        return EMAIL
    if _PASSWORD_WORDS.search(t):
        return PASSWORD
    if _PHONE_WORDS.search(t):
        return PHONE
    if re.search(r"\bsearch\b", t):
        return SEARCH
    if _COMPOSER_WORDS.search(t) or re.search(r"\b(?:editor|textarea|composer)\b", t):
        return COMPOSER
    return TEXT


def _hint_tokens(hint: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (hint or "").lower()) if w not in _STOP and len(w) > 1]


def _score(el: dict, kind: str, intent: str, hint_tokens: list[str], reasons: list[str]) -> float | None:
    """Score for this intent, or None to reject.  Reasons are appended for observability."""
    ev = evidence_text(el)
    ctx = (str(el.get("form_hint") or "") + " " + ev).lower()
    region = str(el.get("region") or "")
    tag = str(el.get("tag") or "").lower()
    score = 0.0

    def add(n: float, why: str) -> None:
        nonlocal score
        score += n
        reasons.append(f"{n:+.0f} {why}")

    # kind compatibility -------------------------------------------------------------------------
    if intent in (SEARCH, COMPOSER, TEXT):
        if kind in _PRIVATE_KINDS:
            return None        # never put a query / message into an email, password, phone, number or date field
        if kind == CHOICE:
            return None
        if _SIGNUP_CONTEXT.search(ctx) and kind != SEARCH:
            return None        # newsletter / sign-up / login forms are not where a query or message goes
    elif intent == RECIPIENT:
        if kind in (PASSWORD, PHONE, NUMBER, DATE, CHOICE):
            return None
        if _SIGNUP_CONTEXT.search(ctx):
            return None        # a newsletter / sign-up e-mail box is never the recipient of a message
    elif intent in (SUBJECT, BODY):
        if kind in (PASSWORD, PHONE, NUMBER, DATE, CHOICE, EMAIL, SEARCH):
            return None
        if _SIGNUP_CONTEXT.search(ctx):
            return None
    elif intent in _PRIVATE_KINDS:
        combined = (intent == PHONE and kind in (EMAIL, TEXT) and bool(_PHONE_WORDS.search(ev)))
        if kind != intent and not combined:
            return None        # an explicit email/password/phone target accepts only that kind of field
        if combined:
            reasons.append("+0 combined 'phone or email' identifier field (label mentions a phone number)")

    # intent-specific evidence -------------------------------------------------------------------
    if intent == SEARCH:
        if kind == SEARCH:
            add(70, "search-type/named control")
        if str(el.get("input_type") or "") == "search":
            add(15, "type=search")
        if re.search(r"\bsearch\b", str(el.get("form_hint") or ""), re.I):
            add(40, "inside a search form/landmark")
        if str(el.get("name_attr") or "").lower() in {"q", "query", "s", "search", "keyword", "keywords", "term"}:
            add(25, "query-like field name")
        if kind in (TEXT, COMPOSER):
            add(5, "generic text field (weak)")
        if tag == "textarea" or el.get("contenteditable"):
            add(-15, "multi-line field is rarely a search box")
        if region == "footer":
            add(-40, "footer region")
        elif region == "sidebar":
            add(-10, "sidebar region")
        elif region in {"header", "nav", "main"}:
            add(6, f"{region} region")
    elif intent == RECIPIENT:
        label = _s(el, "name", "placeholder", "aria_label", "label_text", "name_attr")
        if _CC_WORDS.search(label) and not re.search(r"\bto\b", label, re.I):
            return None        # Cc / Bcc is not the primary recipient
        if _SUBJECT_WORDS.search(label):
            return None
        if _RECIPIENT_WORDS.search(label):
            add(80, "recipient-like label (To / recipients / e-mail)")
        if kind == EMAIL:
            add(25, "type=email")
        if kind == SEARCH:
            add(-60, "search box")
        if tag == "textarea" or el.get("contenteditable"):
            add(-10, "multi-line field")
        if el.get("in_main"):
            add(5, "inside main content")
    elif intent == SUBJECT:
        label = _s(el, "name", "placeholder", "aria_label", "label_text", "name_attr")
        if not _SUBJECT_WORDS.search(label):
            return None        # a subject field announces itself; never guess one
        add(90, "subject label")
        if tag == "textarea" or el.get("contenteditable"):
            add(-10, "multi-line field")
    elif intent == BODY:
        label = _s(el, "name", "placeholder", "aria_label", "label_text", "name_attr")
        if _SUBJECT_WORDS.search(label) or (_RECIPIENT_WORDS.search(label) and not _BODY_WORDS.search(label)):
            return None
        if kind == COMPOSER:
            add(35, "multi-line / editable region")
        else:
            add(-30, "single-line field is rarely a message body")
        if _BODY_WORDS.search(label):
            add(40, "body/message-like label")
        if el.get("in_main"):
            add(10, "inside main content")
    elif intent == COMPOSER:
        if kind == SEARCH:
            return None        # a site-search box is not a message composer
        if kind == COMPOSER:
            add(25, "multi-line / editable region")
        if _COMPOSER_WORDS.search(_s(el, "name", "placeholder", "aria_label", "label_text")):
            add(40, "message-like label")
        if el.get("in_main"):
            add(30, "inside main content")
        if region in {"header", "nav", "footer", "sidebar"}:
            add(-20, f"{region} region")
    elif intent == TEXT:
        if kind == SEARCH:
            add(-30, "search box is the last resort for generic typing")
        if kind == COMPOSER:
            add(20, "multi-line / editable region")
        if _COMPOSER_WORDS.search(_s(el, "name", "placeholder", "aria_label", "label_text")):
            add(20, "message-like label")
        if el.get("in_main"):
            add(25, "inside main content")
        if region in {"header", "nav", "footer", "sidebar"}:
            add(-20, f"{region} region")
    else:                      # explicit private kind that matched
        add(80 if kind == intent else 60, f"{intent} field" if kind == intent else "phone-or-email identifier field")
        if _SIGNUP_CONTEXT.search(ctx):
            add(-10, "sign-up/newsletter context")
        if el.get("in_main"):
            add(10, "inside main content")
        if region == "footer":
            add(-15, "footer region")

    # words from the user's target description -----------------------------------------------------
    if hint_tokens:
        hits = [t for t in hint_tokens if t in ev or t in str(el.get("form_hint") or "").lower()]
        if hits:
            add(min(60, 25 * len(hits)), f"label matches request words {hits}")
        elif intent == TEXT:
            add(-5, "no request word in labels")
    # tie-break: earlier on the page
    try:
        top = float(el.get("viewport_top", 0) or 0)
        add(-min(top / 400.0, 5), "lower on the page")
    except (TypeError, ValueError):
        pass
    return score


def rank_fields(context: dict, intent: str = TEXT, hint: str = "", allow_weak_fallback: bool = True) -> Ranking:
    """Rank the editable controls in ``context`` for ``intent``.

    ``intent``: search | composer | email | password | phone | text.  ``hint`` is the user's own words for the
    target ("the email field", "chat box"); when it implies a stronger intent than ``intent`` it wins.
    """
    implied = intent_from_target(hint) if hint else TEXT
    if intent in (TEXT, "") and implied != TEXT:
        intent = implied
    tokens = _hint_tokens(hint)
    ranked: list[Candidate] = []
    rejected: list[tuple[dict, str]] = []
    for el in context.get("elements") or context.get("interactive_elements") or []:
        if not isinstance(el, dict):
            continue
        if el.get("visible") is False:
            rejected.append((el, "not visible")); continue
        if el.get("disabled"):
            rejected.append((el, "disabled")); continue
        if el.get("readonly"):
            rejected.append((el, "read-only")); continue
        kind = classify_field(el)
        if kind == NOT_TEXT:
            continue
        reasons: list[str] = []
        sc = _score(el, kind, intent, tokens, reasons)
        if sc is None:
            rejected.append((el, f"{kind} field is not a valid target for intent '{intent}'"))
            continue
        ranked.append(Candidate(el, sc, kind, reasons))
    ranked.sort(key=lambda c: -c.score)
    if not allow_weak_fallback:
        ranked = [c for c in ranked if c.score >= 20]
    return Ranking(intent, ranked, rejected)


def ambiguous_semantic_key(cand: Candidate, others: list[Candidate]) -> bool:
    key = (cand.element.get("role"), cand.element.get("name"), cand.element.get("placeholder"))
    return sum(1 for o in others if (o.element.get("role"), o.element.get("name"), o.element.get("placeholder")) == key) > 1


def target_dict(cand: Candidate, others: list[Candidate] | None = None) -> dict:
    """Most semantic locator that is unique on the page; xpath when the semantic one would be ambiguous."""
    el = cand.element
    role = str(el.get("role") or "").lower()
    name = str(el.get("name") or "").strip()
    if others and ambiguous_semantic_key(cand, others) and el.get("xpath"):
        return {"xpath": str(el["xpath"])}
    if role in {"textbox", "searchbox"} and name:
        return {"role": role, "name": name}
    if el.get("placeholder"):
        return {"placeholder": str(el["placeholder"])}
    if el.get("aria_label"):
        return {"label": str(el["aria_label"])}
    if el.get("xpath"):
        return {"xpath": str(el["xpath"])}
    return {}


VERIFY_IN_FIELD_JS = r"""
({xpath, placeholder, label, expected}) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  let el = null;
  if (xpath) {
    try { el = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; } catch (e) {}
  }
  if (!el && placeholder) el = Array.from(document.querySelectorAll('[placeholder]')).find(e => e.getAttribute('placeholder') === placeholder) || null;
  if (!el && label) el = Array.from(document.querySelectorAll('[aria-label]')).find(e => e.getAttribute('aria-label') === label) || null;
  if (!el) return {found: false, ok: false};
  const raw = (el.value !== undefined && el.value !== '') ? el.value : (el.innerText || el.textContent || '');
  const want = n(expected), have = n(raw);
  let count = 0;
  if (want) { for (let i = have.indexOf(want); i >= 0; i = have.indexOf(want, i + want.length)) count++; }
  return {found: true, ok: have.includes(want), count: count, value: String(raw).slice(0, 200), tag: el.tagName.toLowerCase()};
}
"""


def _normalise_xpath(xp: str | None) -> str | None:
    if not xp:
        return None
    return "/html[1]" + xp if xp.startswith("/body") else xp


def value_matches(field_kind: str, actual: str, expected: str) -> bool:
    """Strict read-back comparison for identifier fields (digits for phones, case-insensitive for e-mail)."""
    a, e = str(actual or ""), str(expected or "")
    if field_kind == PHONE:
        da, de = re.sub(r"\D", "", a), re.sub(r"\D", "", e)
        return bool(de) and (da == de or (len(da) > len(de) and da.endswith(de) and len(da) - len(de) <= 3))
    if field_kind == EMAIL:
        return a.strip().lower() == e.strip().lower() and bool(e.strip())
    return a.strip() == e.strip() and bool(e.strip())


async def verify_in_field(page: Any, cand: Candidate, expected: str) -> dict:
    """Independently read the chosen control back from the live DOM.  ``found=False`` means it could not be located."""
    el = cand.element
    try:
        res = await page.evaluate(VERIFY_IN_FIELD_JS, {
            "xpath": _normalise_xpath(el.get("xpath")), "placeholder": el.get("placeholder") or None,
            "label": el.get("aria_label") or None, "expected": expected})
    except Exception as exc:  # page closed / navigated
        return {"found": False, "ok": False, "error": repr(exc)}
    return res or {"found": False, "ok": False}
