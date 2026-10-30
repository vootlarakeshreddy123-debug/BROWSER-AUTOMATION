"""Site NAME -> URL, using the project's existing known-site table (planner.SITE_ADAPTERS).

Unknown names return None - the caller must report that honestly instead of guessing
(e.g. turning an unknown word into a Google search).
"""
from __future__ import annotations

import re


def resolve_site_url(name: str) -> str | None:
    from agent.planner import SITE_ADAPTERS

    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    if not key:
        return None
    for adapter in SITE_ADAPTERS:
        if key in adapter.aliases:
            return adapter.url
    return None
