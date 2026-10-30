"""Detect login / CAPTCHA / human-verification / access-blocked pages from the observed DOM.

Never tries to bypass anything: the caller reports the state honestly and may
wait for the user to resolve it in the live Chrome window.  Stdlib only.
"""
from __future__ import annotations

import re

# Strong phrases are decisive anywhere in the first screenful; weak words (a normal article can
# mention "captcha") only count in the URL/title or on a nearly empty page.
_CAPTCHA_STRONG = re.compile(
    r"are you a robot|i'm not a robot|unusual traffic|verify (?:that )?you are (?:a )?human|"
    r"press (?:and|&) hold|checking your browser before|human verification|prove you are human",
    re.I,
)
_CAPTCHA_WEAK = re.compile(r"captcha|recaptcha|hcaptcha|security check|just a moment|cloudflare", re.I)
_BLOCKED = re.compile(r"access denied|request blocked|pardon our interruption|forbidden|temporarily blocked|too many requests", re.I)
_TWO_FA = re.compile(r"two-factor|2-step|verification code|enter the code|one-time (?:code|password)", re.I)
_LOGIN_BTN = re.compile(r"\b(?:log\s?in|sign\s?in)\b", re.I)


def detect_block(context: dict) -> tuple[str, str] | None:
    """Returns (kind, human-readable reason) or None. kind: captcha | blocked | two_factor | login."""
    url = str(context.get("current_url") or context.get("url") or "")
    title = str(context.get("current_title") or context.get("title") or "")
    body = str(context.get("body_text") or "")[:1500]
    thin = len(body.strip()) < 400            # challenge pages are nearly empty; articles about CAPTCHAs are not
    if "/sorry/" in url or _CAPTCHA_STRONG.search(f"{title} {body}") or _CAPTCHA_WEAK.search(url) \
            or (thin and _CAPTCHA_WEAK.search(f"{title} {body}")):
        return "captcha", "The page asks for human verification (CAPTCHA / bot check)."
    if _BLOCKED.search(f"{title} {body[:300]}"):
        return "blocked", "The website is refusing access to this browser."
    if _TWO_FA.search(f"{title} {body[:500]}"):
        return "two_factor", "The page asks for a verification code (2FA)."
    return None


def looks_like_login_wall(context: dict) -> bool:
    """A login wall: login/sign-in controls present and no usable text field."""
    els = [e for e in (context.get("elements") or []) if isinstance(e, dict) and e.get("visible") is not False]
    has_login = any(_LOGIN_BTN.search(str(e.get("name") or e.get("text") or "")) for e in els)
    has_field = any(
        str(e.get("tag") or "") in {"textarea"} or e.get("contenteditable")
        or (str(e.get("tag") or "") == "input" and str(e.get("type") or "") not in {"password", "email", "checkbox", "submit", "hidden"})
        for e in els
    )
    return has_login and not has_field
