"""What did a search action actually produce?  Decided from browser evidence only.

A query string in the URL is NOT proof of a results page, and a search does not always produce an intermediate
page: it may land directly on the requested content.  This module classifies the live page after a search from

  * whether the page / URL / DOM changed at all, and whether the typed text is still sitting in the field,
  * the page identity (brand-stripped title and first heading versus the query),
  * how many content links echo the query (a results list) versus a single entity page,
  * disambiguation / empty-result / error / blocked wording on thin pages.

No website name, URL scheme or CSS selector appears here.  Stdlib only (plus sibling generic helpers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from agent.blocked_page import detect_block
from agent.page_identity import first_heading, is_search_url, title_core
from agent.progress import Observation, goal_tokens


class Outcome(str, Enum):
    NOT_SUBMITTED = "query_entered_not_submitted"
    SEARCH_RESULTS_PAGE = "search_results_page"
    RESULTS_SAME_PAGE = "results_on_same_page"
    DIRECT_TARGET_PAGE = "direct_target_page"
    DISAMBIGUATION_PAGE = "disambiguation_page"
    EMPTY_RESULTS = "empty_results"
    ERROR_PAGE = "error_page"
    BLOCKED_PAGE = "blocked_page"
    UNRELATED_PAGE = "unrelated_page"
    UNCHANGED = "unchanged_state"
    AMBIGUOUS = "ambiguous"

    @property
    def search_executed(self) -> bool:
        return self in {Outcome.SEARCH_RESULTS_PAGE, Outcome.RESULTS_SAME_PAGE, Outcome.DIRECT_TARGET_PAGE,
                        Outcome.DISAMBIGUATION_PAGE, Outcome.EMPTY_RESULTS}


@dataclass
class OutcomeResult:
    outcome: Outcome
    reason: str
    evidence: dict = field(default_factory=dict)


_EMPTY = re.compile(r"\b(?:no\s+(?:results?|matches|items|pages)\s+(?:found|for|match)|did\s+not\s+match\s+any|nothing\s+(?:was\s+)?found|0\s+results?|no\s+results?)\b", re.I)
_ERROR_TITLE = re.compile(r"\b(?:404|not\s+found|page\s+not\s+found|error|something\s+went\s+wrong|unavailable)\b", re.I)
_DISAMBIG = re.compile(r"\b(?:may\s+refer\s+to|disambiguation|did\s+you\s+mean|which\s+one\s+do\s+you\s+mean|multiple\s+(?:meanings|matches))\b", re.I)
_NAV_REGIONS = {"header", "nav", "footer"}


def _n(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def echoing_links(context: dict, query: str, min_cover: float = 0.5) -> list[dict]:
    """Content links (not page chrome) whose visible name echoes the query - the shape of a results list."""
    toks = goal_tokens(_n(query))
    if not toks:
        return []
    out = []
    for el in context.get("elements") or []:
        if not isinstance(el, dict) or el.get("visible") is False or el.get("disabled"):
            continue
        if str(el.get("role") or el.get("tag")) not in {"link", "a"} or str(el.get("region") or "") in _NAV_REGIONS:
            continue
        hay = _n(str(el.get("name") or "") + " " + str(el.get("text") or "") + " " + str(el.get("href") or ""))
        if sum(1 for t in toks if t in hay) / len(toks) >= min_cover:
            out.append(el)
    return out


def _is_entity(context: dict, query: str) -> bool:
    wanted = _n(query)
    title = context.get("current_title") or context.get("title") or ""
    return bool(wanted) and (title_core(title, context) == wanted or first_heading(context) == wanted)


def classify_search_outcome(before: Observation, after: Observation, after_ctx: dict, query: str,
                            typed_ok: bool = True, submitted: bool | None = None,
                            field_still_holds_query: bool | None = None) -> OutcomeResult:
    ctx = after_ctx or {}
    url_changed = before.url_key != after.url_key
    page_changed = after.page_id != before.page_id
    dom_changed = before.text_digest != after.text_digest or before.headings != after.headings
    changed = url_changed or page_changed or dom_changed
    toks = goal_tokens(_n(query))
    hay = " ".join([after.title.lower(), " ".join(after.headings), after.text_head])
    cover = (sum(1 for t in toks if t in hay) / len(toks)) if toks else 0.0
    ev = {"url_changed": url_changed, "page_changed": page_changed, "dom_changed": dom_changed,
          "query_coverage": round(cover, 2), "url_has_query_marker": is_search_url(after.url)}

    block = detect_block(ctx)
    if block:
        return OutcomeResult(Outcome.BLOCKED_PAGE, block[1], {**ev, "block": block[0]})

    body = str(ctx.get("body_text") or "")
    thin = len(body.strip()) < 600
    head_text = f"{after.title} {' '.join(after.headings[:3])}"
    if (_ERROR_TITLE.search(head_text) and (thin or re.search(r"\b(?:404|not\s+found)\b", head_text, re.I))) and changed:
        return OutcomeResult(Outcome.ERROR_PAGE, "the page reports an error / not-found", ev)

    if not changed:
        if typed_ok and (field_still_holds_query is not False):
            return OutcomeResult(Outcome.NOT_SUBMITTED, "the query is in the field but nothing on the page changed", ev)
        return OutcomeResult(Outcome.UNCHANGED, "no URL, page or content change after the search", ev)
    if submitted is False and not changed:
        return OutcomeResult(Outcome.NOT_SUBMITTED, "no submission was observed", ev)

    links = echoing_links(ctx, query)
    ev["result_links"] = len(links)

    if _EMPTY.search(body[:1500]) and not links:
        return OutcomeResult(Outcome.EMPTY_RESULTS, "the page says no results were found", ev)
    if _is_entity(ctx, query) and len(links) < 3:
        return OutcomeResult(Outcome.DIRECT_TARGET_PAGE, "the search landed directly on the requested page (title/heading equals the query)", ev)
    if _DISAMBIG.search(body[:1200] + " " + head_text) and len(ctx.get("elements") or []) >= 3:
        return OutcomeResult(Outcome.DISAMBIGUATION_PAGE, "the page lists several possible meanings of the query", ev)
    if len(links) >= 2:
        same_doc = not (url_changed or page_changed)
        return OutcomeResult(Outcome.RESULTS_SAME_PAGE if same_doc else Outcome.SEARCH_RESULTS_PAGE,
                             f"{len(links)} content links echo the query", ev)
    if cover >= 0.5 and (url_changed or page_changed):
        if is_search_url(after.url):
            return OutcomeResult(Outcome.SEARCH_RESULTS_PAGE, f"results-style URL and the page mentions the query (coverage {cover:.2f})", ev)
        return OutcomeResult(Outcome.DIRECT_TARGET_PAGE, f"navigated to a page about the query (coverage {cover:.2f})", ev)
    if cover >= 0.5 and dom_changed:
        return OutcomeResult(Outcome.RESULTS_SAME_PAGE, f"content changed and now mentions the query (coverage {cover:.2f})", ev)
    if url_changed and cover < 0.3:
        return OutcomeResult(Outcome.UNRELATED_PAGE, f"the page changed but does not mention the query (coverage {cover:.2f})", ev)
    return OutcomeResult(Outcome.AMBIGUOUS, "something changed but there is no clear evidence of search results", ev)
