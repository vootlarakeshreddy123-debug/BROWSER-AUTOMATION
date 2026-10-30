"""
Lightweight result validation.

Deliberately cheap: one URL/title/status check after navigation, not a
screenshot-and-full-DOM-scan cycle. The one thing it must never do is call
a navigate that returned HTTP 200 a "success" if the destination is
actually a bot-block/interstitial page - that's the literal
no-false-success requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BLOCK_MARKERS = (
    "/sorry/",              # Google's automated-traffic interstitial
    "recaptcha",
    "are you a robot",
    "unusual traffic",
    "captcha",
    "access denied",
    "request blocked",
    "pardon our interruption",
)


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def validate_navigation(url: str, title: str, status: Optional[int]) -> ValidationResult:
    if status is not None and status >= 400:
        return ValidationResult(False, f"Page responded with HTTP {status}.")

    haystack = f"{url} {title}".lower()
    for marker in BLOCK_MARKERS:
        if marker in haystack:
            return ValidationResult(
                False, "The website blocked or challenged the automated request."
            )

    if not url or url == "about:blank":
        return ValidationResult(False, "Navigation did not land on a real page.")

    return ValidationResult(True, "Page loaded successfully.")
