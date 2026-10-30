"""Extraction requirements and DOM-provenance verification.

An extract action returning a non-empty string is NOT evidence that the user's
request was satisfied.  The requested kind (heading, title, first sentence,
section, price, ...) determines which DOM provenance is acceptable, and the
value is re-checked against the live page.

Generic: no website names anywhere in this module.
"""
from __future__ import annotations

import re
from typing import Any

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

_PRICE = re.compile(r"(?:[$€£₹¥]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:usd|eur|gbp|inr|rs\.?))", re.I)
_DATE = re.compile(r"\b(?:\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?)\b", re.I)
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_ORD = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1}


def norm(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def extraction_spec(instruction: str, contract: dict | None = None) -> dict:
    """What the user asked to be extracted: kind + position."""
    text = norm(instruction).lower()
    contract = contract or {}
    kind = contract.get("extraction_kind")
    if re.search(r"\bfirst\s+sentence\b", text) or contract.get("first_sentence"):
        kind = "first_sentence"
    elif re.search(r"\bheadings?\b", text) and not re.search(r"\b(?:under|below|beneath|section)\b", text):
        kind = "heading"
    elif re.search(r"\btitle\b", text) and not re.search(r"\bsection\b", text):
        kind = "title"
    elif re.search(r"\bphone\b|\btelephone\b", text):
        kind = "phone"
    elif re.search(r"\b(?:table|cell)\b", text):
        kind = "table_value"
    elif contract.get("section_requested"):
        kind = "section"
    position = 0
    m = re.search(r"\b(first|second|third|fourth|fifth|last)\b", text)
    if m:
        position = _ORD[m.group(1)]
    return {"kind": kind or "generic", "position": position}


def validate_record(spec: dict, record: dict) -> tuple[bool, str]:
    """Check an extraction record's kind/provenance against the request."""
    kind = spec.get("kind")
    rk = str(record.get("kind") or "").lower()
    text = norm(record.get("text"))
    if not text:
        return False, "empty value"
    prov_tag = str(record.get("tag") or "").lower()
    if kind == "heading":
        if rk != "heading" or prov_tag not in HEADING_TAGS:
            return False, f"requested a heading but value has kind={rk!r}, tag={prov_tag!r} (not from an h1-h6 element)"
        return True, f"heading element <{prov_tag}>"
    if kind == "title":
        if rk != "title":
            return False, f"requested a title but value has kind={rk!r}"
        return True, "document title"
    if kind == "first_sentence":
        if rk != "first_sentence":
            return False, f"requested a first sentence but value has kind={rk!r}"
        return True, "first sentence of page content"
    if kind == "section":
        if rk not in {"section", "first_sentence"} or not norm(record.get("section_heading")):
            return False, "section value lacks section-heading provenance"
        return True, "section under a heading"
    if kind == "price":
        return (True, "price pattern") if _PRICE.search(text) else (False, "value does not look like a price")
    if kind == "date":
        return (True, "date pattern") if _DATE.search(text) else (False, "value does not look like a date")
    if kind == "phone":
        return (True, "phone pattern") if _PHONE.search(text) else (False, "value does not look like a phone number")
    if kind in {"count", "number"}:
        return (True, "numeric") if re.search(r"\d", text) else (False, "value contains no number")
    if kind == "table_value":
        return (True, "table cell") if prov_tag in {"td", "th"} else (False, "value did not come from a table cell")
    return True, "generic text"


VERIFY_EXTRACTION_JS = r"""
(rec) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const val = n(rec.text);
  if (!val) return {ok:false, reason:'empty'};
  const kind = rec.kind;
  if (kind === 'title') return {ok: n(document.title) === val, reason:'document.title'};
  if (kind === 'heading') {
    const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
    const hit = hs.find(h => n(h.innerText || h.textContent) === val);
    return hit ? {ok:true, tag: hit.tagName.toLowerCase(), reason:'matching heading element'} : {ok:false, reason:'no heading element has this text'};
  }
  if (kind === 'first_sentence' || kind === 'section' || kind === 'text') {
    const blocks = Array.from(document.querySelectorAll('p,li,blockquote,div,td,th,pre'));
    const probe = val.slice(0, 120);
    const hit = blocks.find(b => n(b.innerText || b.textContent).includes(probe));
    return hit ? {ok:true, tag: hit.tagName.toLowerCase(), reason:'value found in page content'} : {ok:false, reason:'value not found in page content'};
  }
  const body = n(document.body ? document.body.innerText : '');
  return body.includes(val.slice(0, 120)) ? {ok:true, reason:'value visible on page'} : {ok:false, reason:'value not visible on page'};
}
"""


async def verify_in_dom(page: Any, record: dict) -> tuple[bool, str]:
    """Re-check the extracted value against the CURRENT live page."""
    if page is None:
        return False, "no active page to verify against"
    try:
        res = await page.evaluate(VERIFY_EXTRACTION_JS, {"text": record.get("text"), "kind": record.get("kind")})
    except Exception as exc:  # page closed / navigated
        return False, f"DOM verification failed: {exc}"
    return bool(res.get("ok")), str(res.get("reason") or "")
