"""Generic page-identity helpers (no website names).

``title_core`` removes a trailing site-brand segment ("Artificial intelligence - WikiLike",
"Machine Learning with Python | CourseHub") so a page can be compared with what the
user searched for.  A trailing segment is treated as the brand when it matches
  * the site's own logo/home link text found in the page header, or
  * a label of the host name, or
  * one of a few very common brand words kept for backwards compatibility.
Stdlib only.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_SEP = re.compile(r"\s+[-|–—:]\s+")
_LEGACY_BRANDS = {"wikipedia", "youtube", "github", "mdn web docs", "stackoverflow"}
_SEARCH_MARKERS = ("/search", "?q=", "&q=", "?query=", "&query=", "search_query=", "?search=", "&search=")


def _n(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def is_search_url(url: str) -> bool:
    u = str(url or "").lower()
    return any(m in u for m in _SEARCH_MARKERS)


def brand_names(context: dict) -> set[str]:
    brands = set(_LEGACY_BRANDS)
    host = (urlparse(str(context.get("current_url") or context.get("url") or "")).hostname or "").lower().removeprefix("www.")
    for label in host.split("."):
        if len(label) >= 3:
            brands.add(label)
    for el in (context.get("elements") or [])[:80]:
        if isinstance(el, dict) and el.get("region") in {"header", "nav"} and str(el.get("role") or el.get("tag")) in {"link", "a"}:
            name = _n(el.get("name") or el.get("text"))
            href = str(el.get("href") or "")
            if name and len(name) <= 30 and (href in {"/", ""} or href.rstrip("/").count("/") <= 2 and not urlparse(href).path.strip("/")):
                brands.add(name)
    return brands


def title_core(title: str, context: dict) -> str:
    t = _n(title)
    parts = _SEP.split(t)
    if len(parts) >= 2 and parts[-1].strip() in {b for b in brand_names(context)}:
        return " - ".join(parts[:-1]).strip()
    return t


def first_heading(context: dict) -> str:
    for h in context.get("headings") or []:
        text = h.get("text") if isinstance(h, dict) else h
        if _n(text):
            return _n(text)
    return ""


def page_is_entity(context: dict, query: str) -> bool:
    """The live page is the thing that was searched for: not a search page, and either the
    brand-stripped title or the first heading equals the query."""
    wanted = _n(query)
    url = str(context.get("current_url") or context.get("url") or "")
    if not wanted or is_search_url(url):
        return False
    title = context.get("current_title") or context.get("title") or ""
    return title_core(title, context) == wanted or first_heading(context) == wanted


def urls_equivalent(a: str, b: str) -> bool:
    pa, pb = urlparse(a or ""), urlparse(b or "")
    return ((pa.hostname or "").lower(), pa.path.rstrip("/"), pa.query) == ((pb.hostname or "").lower(), pb.path.rstrip("/"), pb.query)


def navigation_regression_reason(current_url: str, target_url: str, completed_actions: list | None) -> str | None:
    """Why a navigate action would destroy valid progress (None = fine to navigate).

    Generic: only URLs and the task's own action history.
      * target is exactly where we already are                       -> pointless
      * target is a site ROOT while the current page is deeper on the
        same site, or this task already did click/type/press/extract
        work -> would discard the progress
    """
    def key(u: str):
        p = urlparse(u or "")
        return (p.hostname or "").lower().removeprefix("www."), p.path.rstrip("/"), p.query

    cur, tgt = key(current_url), key(target_url)
    if not tgt[0] or not cur[0]:
        return None
    if cur == tgt:
        return f"already on {current_url}"
    same_site = cur[0] == tgt[0] or cur[0].endswith("." + tgt[0]) or tgt[0].endswith("." + cur[0])
    if same_site and tgt[1] == "" and not tgt[2]:
        deeper = cur[1] != "" or bool(cur[2])
        did_work = any(
            str(((i or {}).get("action") or {}).get("type") or "").lower() in {"click", "type", "press", "extract"}
            for i in completed_actions or []
        )
        if deeper or did_work:
            return f"navigating to the site root would discard progress made on {current_url}"
    return None
