"""Goal-specific verification from observed EVIDENCE (never from the planner's claim).

Every function returns ``Verdict(ok, reason)``.  The inputs are
``progress.Observation`` objects (before / after the goal's actions), the
executor's per-action evidence dicts, and a few values supplied by the runner.

Deliberately NOT lexical matching of the instruction against page text: the word
"type" or "back" is task language, not page content.  Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlparse

from agent.goals import Goal, GoalKind
from agent.page_identity import is_search_url, urls_equivalent
from agent.progress import Observation, goal_tokens


@dataclass
class Verdict:
    ok: bool
    reason: str
    evidence: dict | None = None


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unquote_plus(str(text or "")).lower()).strip()


def host_matches_site(url: str, site: str | None, expected_url: str | None) -> bool:
    h = _host(url)
    if not h:
        return False
    if expected_url:
        eh = _host(expected_url)
        if eh and (h == eh or h.endswith("." + eh) or eh.endswith("." + h)):
            return True
        # known sites often redirect to a regional/sub domain sharing the registrable label
        if eh and h.split(".")[-2:] == eh.split(".")[-2:]:
            return True
    if site:
        label = re.sub(r"[^a-z0-9]", "", site.lower())
        if label and label in re.sub(r"[^a-z0-9]", "", h):
            return True
    return False


def verify_navigate(goal: Goal, after: Observation) -> Verdict:
    if not after.url or after.url.startswith("about:blank"):
        return Verdict(False, "browser is not on a real page")
    ok = host_matches_site(after.url, goal.params.get("site"), goal.params.get("url"))
    if goal.params.get("url") and not ok and urls_equivalent(after.url, goal.params["url"]):
        ok = True
    return Verdict(ok, f"landed on {_host(after.url)}" if ok else f"expected {_host(goal.params.get('url') or '') or goal.params.get('site')}, browser is on {_host(after.url)}")


def verify_search(goal: Goal, before: Observation, after: Observation, typed_ok: bool, submitted: bool) -> Verdict:
    q = _norm(goal.params.get("query"))
    if not typed_ok:
        return Verdict(False, "the query text was not found in the search field")
    if not submitted:
        return Verdict(False, "the search was typed but not submitted")
    url_n = _norm(after.url)
    in_url = bool(q) and (q in url_n or q.replace(" ", "+") in after.url.lower() or q.replace(" ", "%20") in after.url.lower())
    changed = before.url_key != after.url_key or before.text_digest != after.text_digest
    toks = goal_tokens(q)
    hay = " ".join([after.title.lower(), " ".join(after.headings), after.text_head])
    cover = (sum(1 for t in toks if t in hay) / len(toks)) if toks else 0.0
    if in_url:
        return Verdict(True, "the query is in the results URL", {"query_in_url": True})
    if changed and (is_search_url(after.url) or cover >= 0.5):
        return Verdict(True, f"results page shown (query coverage {cover:.2f})", {"coverage": cover})
    return Verdict(False, "no evidence that the search was executed (URL/results unchanged)")


def verify_click(goal: Goal, before: Observation, after: Observation, evidence: list[dict]) -> Verdict:
    new_page = any(e.get("new_page_detected") for e in evidence) or after.page_id != before.page_id
    if new_page:
        return Verdict(True, "a new page opened and became active", {"new_page": True})
    if before.url_key != after.url_key:
        # An ordinal click ("open the first result") must LEAVE the results page. Comparing host+path
        # (not "?q=" markers) so a result page that keeps the query string still counts as opened.
        pb, pa = urlparse(before.url), urlparse(after.url)
        same_page = (pb.hostname, pb.path.rstrip("/")) == (pa.hostname, pa.path.rstrip("/"))
        if goal.params.get("ordinal") and same_page:
            return Verdict(False, "still on the same results page (only the query string changed)")
        return Verdict(True, f"navigated to {after.url}")
    if before.title != after.title or before.headings != after.headings or before.interactive != after.interactive:
        if goal.params.get("ordinal"):
            return Verdict(False, "page content changed but the requested result was not opened")
        return Verdict(True, "page content changed after the click")
    return Verdict(False, "the click produced no observable change")


def verify_history(goal: Goal, before: Observation, after: Observation, expected_url: str | None) -> Verdict:
    if before.url_key == after.url_key:
        return Verdict(False, f"browser history did not move {goal.kind.value}")
    if expected_url and not urls_equivalent(after.url, expected_url):
        return Verdict(False, f"history moved, but to {after.url} instead of the expected {expected_url}")
    return Verdict(True, f"history moved {goal.kind.value} to {after.url}")


def verify_scroll(goal: Goal, before: dict, after: dict) -> Verdict:
    d = goal.params.get("direction", "down")
    moved = after.get("scroll_y") != before.get("scroll_y")
    if moved:
        return Verdict(True, f"scroll position {before.get('scroll_y')} -> {after.get('scroll_y')}")
    at_edge = (after.get("scroll_y", 0) <= 0) if d == "up" else (
        after.get("scroll_y", 0) + after.get("inner_height", 0) >= after.get("scroll_height", 0) - 2)
    if at_edge:
        return Verdict(True, "already at the " + ("top" if d == "up" else "bottom") + " of the page", {"at_edge": True})
    return Verdict(False, "scroll position did not change")


def verify_new_tab(goal: Goal, before: Observation, after: Observation) -> Verdict:
    if after.page_count <= before.page_count:
        return Verdict(False, f"page count did not increase ({before.page_count} -> {after.page_count})")
    if after.page_id == before.page_id:
        return Verdict(False, "the new tab was opened but not adopted as the active page")
    url, site = goal.params.get("url"), goal.params.get("site")
    if url or site:
        if not host_matches_site(after.url, site, url):
            return Verdict(False, f"new tab is on {_host(after.url)}, expected {_host(url or '') or site}")
    return Verdict(True, f"new tab active: {after.url}")


def verify_switch(before: Observation, after: Observation) -> Verdict:
    if after.page_id != before.page_id:
        return Verdict(True, f"active page changed to {after.url}")
    return Verdict(False, "the active page did not change")


def verify_close(before: Observation, after: Observation) -> Verdict:
    if after.page_count < before.page_count:
        return Verdict(True, f"tab closed ({before.page_count} -> {after.page_count}); active page {after.url}")
    return Verdict(False, "the tab count did not decrease")


def verify_press(goal: Goal, before: dict, after: dict) -> Verdict:
    key = str(goal.params.get("key") or "")
    changed = (before.get("url") != after.get("url") or before.get("text_hash") != after.get("text_hash")
               or before.get("inputs") != after.get("inputs") or before.get("scroll_y") != after.get("scroll_y")
               or before.get("focus") != after.get("focus") or before.get("page_count") != after.get("page_count"))
    if changed:
        return Verdict(True, f"{key} changed the page state")
    if key.lower() in {"escape", "shift", "control", "alt", "meta", "tab"}:
        return Verdict(True, f"{key} delivered (no visible change expected)", {"no_visible_change": True})
    return Verdict(False, f"pressing {key} changed nothing observable")
