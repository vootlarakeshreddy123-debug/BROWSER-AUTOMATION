"""Detect unfilled template placeholders such as ``[a site]`` or ``<website>``.

A placeholder is not a search query. Turning "Open [a site]" into a Google
search for the literal text "[a site]" and then clicking an arbitrary result
invents a task the user never asked for.  Stdlib only.
"""
from __future__ import annotations

import re

_BRACKETED = re.compile(r"\[[^\[\]\n]{1,60}\]|<[^<>\n]{1,60}>|\{\{[^{}\n]{1,60}\}\}|\{[^{}\n]{1,60}\}")
_ABSTRACT_SITE = re.compile(
    r"^\s*(?:open|go\s+to|visit)\s+(?:a|any|some|the)\s+(?:concrete\s+|real\s+|random\s+)?(?:web\s?site|site|web\s?page|page|url)\b",
    re.IGNORECASE,
)


def find_placeholders(instruction: str) -> list[str]:
    text = str(instruction or "")
    found = [m.group(0) for m in _BRACKETED.finditer(text)]
    m = _ABSTRACT_SITE.match(text)
    if m:
        found.append(m.group(0).strip())
    return found


def placeholder_message(found: list[str]) -> str:
    return (
        f"The instruction contains an unfilled placeholder ({', '.join(found[:3])}). "
        "NEXA will not guess which website you meant or search for the placeholder text. "
        "Replace it with a real site (for example \"Open https://example.com ...\") and run it again."
    )
