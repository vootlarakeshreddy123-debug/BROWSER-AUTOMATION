"""Before/after progress classification for adaptive browser steps.

A successful Playwright call is NOT progress and a changed URL is not
necessarily progress.  After every action we compare an ``Observation`` taken
before with one taken after and classify the step:

    PROGRESS                      the page moved measurably toward the goal
    NO_PROGRESS                   nothing relevant changed
    WRONG_STATE                   navigated away from goal-relevant content
    ACTION_FAILED                 the browser action itself failed
    REPEATED_ACTION               same action on same state already tried
    PAGE_CHANGED_NO_GOAL_PROGRESS page changed, but not toward the goal

Goal relevance is computed from the user's own words (generic token overlap
with URL/title/headings/visible text) - never from site-specific rules.
Dependency-free (stdlib only).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


class Progress(str, Enum):
    PROGRESS = "PROGRESS"
    NO_PROGRESS = "NO_PROGRESS"
    WRONG_STATE = "WRONG_STATE"
    ACTION_FAILED = "ACTION_FAILED"
    REPEATED_ACTION = "REPEATED_ACTION"
    PAGE_CHANGED_NO_GOAL_PROGRESS = "PAGE_CHANGED_NO_GOAL_PROGRESS"


_STOP = {
    "the", "and", "for", "with", "that", "this", "then", "from", "into", "onto", "your", "you",
    "open", "click", "go", "visit", "find", "locate", "look", "search", "select", "choose",
    "first", "second", "top", "please", "page", "website", "site", "link", "button", "tell",
    "give", "show", "what", "which", "are", "its", "one", "all", "any", "out", "get",
}


def goal_tokens(instruction: str) -> list[str]:
    seen: list[str] = []
    for w in re.findall(r"[a-z0-9]+", str(instruction or "").lower()):
        if len(w) > 2 and w not in _STOP and w not in seen:
            seen.append(w)
    return seen


def _norm_url(url: str) -> str:
    p = urlparse(str(url or ""))
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}?{p.query}"


def _host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


def _iter_interactive(context: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key in ("interactive", "elements", "interactive_elements", "controls"):
        items = context.get(key)
        if isinstance(items, list):
            for it in items[:60]:
                if isinstance(it, dict):
                    role = str(it.get("role") or it.get("tag") or "")
                    name = str(it.get("name") or it.get("text") or it.get("aria_label") or it.get("placeholder") or "")[:60]
                    out.append((role, name.strip().lower()))
            break
    return out


@dataclass(frozen=True)
class Observation:
    url: str
    title: str
    text_digest: str
    text_head: str
    headings: tuple[str, ...]
    interactive: frozenset
    scroll_y: int
    page_id: int = 0
    page_count: int = 1

    @property
    def url_key(self) -> str:
        return _norm_url(self.url)


def observe(context: dict, url: str | None = None, page_id: int = 0, page_count: int = 1) -> Observation:
    ctx = context or {}
    body = str(ctx.get("body_text") or "")
    viewport = ctx.get("viewport") or {}
    heads = tuple(str(h)[:80] for h in (ctx.get("headings") or [])[:12])
    return Observation(
        url=str(url or ctx.get("current_url") or ctx.get("url") or ""),
        title=str(ctx.get("current_title") or ctx.get("title") or ""),
        text_digest=_h(body[:2000]),
        text_head=body[:600].lower(),
        headings=tuple(h.lower() for h in heads),
        interactive=frozenset(_iter_interactive(ctx)),
        scroll_y=int(viewport.get("scrollY") or 0) if isinstance(viewport, dict) else 0,
        page_id=page_id,
        page_count=page_count,
    )


def relevance(obs: Observation, tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    hay = " ".join([obs.url.lower(), obs.title.lower(), " ".join(obs.headings), obs.text_head])
    return sum(1 for t in tokens if t in hay) / len(tokens)


def action_fingerprint(action: dict | None) -> str:
    return json.dumps(action or {}, sort_keys=True, default=str, ensure_ascii=False)


@dataclass
class Verdict:
    kind: Progress
    reasons: list[str]
    relevance_before: float = 0.0
    relevance_after: float = 0.0

    @property
    def is_progress(self) -> bool:
        return self.kind == Progress.PROGRESS

    def to_dict(self) -> dict:
        return {"classification": self.kind.value, "reasons": self.reasons,
                "relevance_before": round(self.relevance_before, 2),
                "relevance_after": round(self.relevance_after, 2)}


@dataclass
class ProgressTracker:
    instruction: str
    max_repeat: int = 2
    tokens: list[str] = field(init=False)
    no_progress_streak: int = 0
    total_steps: int = 0
    counts: dict = field(default_factory=dict)
    _history: list[tuple[str, str, Progress]] = field(default_factory=list)  # (fingerprint, url_key, kind)
    _typed: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.tokens = goal_tokens(self.instruction)

    def previously_tried(self, action: dict | None, obs: Observation) -> int:
        fp = action_fingerprint(action)
        return sum(1 for f, u, k in self._history
                   if f == fp and u == obs.url_key and k != Progress.PROGRESS)

    @staticmethod
    def _landed_on_target(action: dict | None, before: Observation, after: Observation) -> bool:
        """Generic landing evidence: the words of what was clicked now describe the page."""
        a = action or {}
        t = a.get("target") or {}
        label = " ".join(str(x or "") for x in (t.get("name"), t.get("label"), a.get("url")))
        toks = goal_tokens(label)
        if not toks or before.url_key == after.url_key:
            return False

        def cov(o: Observation) -> float:
            hay = " ".join([o.url.lower(), o.title.lower(), " ".join(o.headings)])
            return sum(1 for x in toks if x in hay) / len(toks)

        return cov(after) >= 0.5 and cov(after) > cov(before)

    def classify(self, before: Observation, after: Observation, action: dict | None,
                 ok: bool, error: str | None = None) -> Verdict:
        atype = str((action or {}).get("type") or "").lower()
        rb, ra = relevance(before, self.tokens), relevance(after, self.tokens)
        reasons: list[str] = []
        fp = action_fingerprint(action)

        if not ok:
            kind = Progress.ACTION_FAILED
            reasons.append(f"browser action failed: {(error or 'unknown')[:160]}")
        else:
            url_changed = before.url_key != after.url_key
            title_changed = before.title != after.title
            heads_changed = before.headings != after.headings
            ui_changed = before.interactive != after.interactive
            text_changed = before.text_digest != after.text_digest
            scrolled = before.scroll_y != after.scroll_y
            page_changed = before.page_id != after.page_id and after.page_id != 0
            pages_changed = before.page_count != after.page_count
            structural = url_changed or title_changed or heads_changed or ui_changed or page_changed
            new_type = False
            if atype == "type":
                key = str((action or {}).get("text") or "").strip().lower()
                new_type = bool(key) and key not in self._typed
                if new_type:
                    self._typed.add(key)

            if page_changed and not (_host(before.url) != _host(after.url) and ra < rb):
                kind = Progress.PROGRESS
                reasons.append(
                    "active page changed (a new tab/page was opened and adopted)"
                    + (f"; page count {before.page_count}->{after.page_count}" if pages_changed else "")
                )
            elif self.previously_tried(action, before) >= self.max_repeat and not structural:
                kind = Progress.REPEATED_ACTION
                reasons.append(f"same action already tried {self.previously_tried(action, before)}x on this page state")
            elif atype == "type" and new_type:
                kind = Progress.PROGRESS
                reasons.append("new text entered into a field (verified by executor)")
            elif atype == "scroll" and (scrolled or text_changed):
                kind = Progress.PROGRESS
                reasons.append("scroll revealed different content")
            elif not structural and not text_changed and not scrolled:
                kind = Progress.NO_PROGRESS
                reasons.append("URL, title, headings, controls and visible text are unchanged")
            elif atype in ("click", "navigate") and self._landed_on_target(action, before, after):
                kind = Progress.PROGRESS
                reasons.append("landed on a page named after the element that was clicked")
            elif structural and ra >= rb:
                kind = Progress.PROGRESS
                bits = [n for n, c in (("url", url_changed), ("title", title_changed),
                                       ("headings", heads_changed), ("controls", ui_changed)) if c]
                reasons.append(f"page state changed ({', '.join(bits)}); goal relevance {rb:.2f}->{ra:.2f}")
            elif url_changed and _host(before.url) != _host(after.url) and ra < rb:
                kind = Progress.WRONG_STATE
                reasons.append(f"navigated to a different site with lower goal relevance ({rb:.2f}->{ra:.2f})")
            elif structural or text_changed:
                kind = Progress.PAGE_CHANGED_NO_GOAL_PROGRESS
                reasons.append(f"page changed but goal relevance did not improve ({rb:.2f}->{ra:.2f})")
            else:
                kind = Progress.NO_PROGRESS
                reasons.append("no measurable change")

        self._history.append((fp, before.url_key, kind))
        self.total_steps += 1
        self.counts[kind.value] = self.counts.get(kind.value, 0) + 1
        self.no_progress_streak = 0 if kind == Progress.PROGRESS else self.no_progress_streak + 1
        return Verdict(kind, reasons, rb, ra)
