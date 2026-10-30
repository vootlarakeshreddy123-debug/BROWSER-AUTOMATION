from __future__ import annotations

import re
from typing import Any, Callable

from playwright.async_api import Locator, Page, Frame

from agent.action_schema import Target


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def _tokens(*values: str | None) -> list[str]:
    words: list[str] = []
    for value in values:
        words.extend(re.findall(r"[a-z0-9]+", _text(value)))
    return [word for word in words if len(word) > 2]


def _reason_flags(reason: str) -> set[str]:
    lowered = _text(reason)
    flags: set[str] = set()

    if re.search(r"\b(first|top)\b", lowered):
        flags.add("first")
    if "repository" in lowered or re.search(r"\brepo\b", lowered):
        flags.add("repository")
    if "video" in lowered:
        flags.add("video")
    if "article" in lowered:
        flags.add("article")
    if "issue" in lowered:
        flags.add("issues")
    if re.search(r"\b(search|query)\b", lowered):
        flags.add("search")
    if re.search(r"\b(result|results)\b", lowered):
        flags.add("result")
    if re.search(r"\b(open|click|select|choose)\b", lowered):
        flags.add("click")

    return flags


def _target_text(target: Target | None) -> str:
    if target is None:
        return ""
    return _text(
        " ".join(
            str(getattr(target, field, None) or "")
            for field in ("name", "placeholder", "label")
        )
    )


# ---------------------------------------------------------------------------
# Primary resolver
# ---------------------------------------------------------------------------

def _primary_locator(page: Page, target: Target) -> Locator | None:
    role = getattr(target, "role", None)
    name = getattr(target, "name", None)
    css = getattr(target, "css", None)
    placeholder = getattr(target, "placeholder", None)
    label = getattr(target, "label", None)
    xpath = getattr(target, "xpath", None)
    exact = bool(getattr(target, "exact", False))

    if role and name:
        try:
            return page.get_by_role(role, name=name, exact=exact).first
        except Exception:
            pass

    if label:
        try:
            return page.get_by_label(label, exact=exact).first
        except Exception:
            pass

    if placeholder:
        try:
            return page.get_by_placeholder(placeholder, exact=exact).first
        except Exception:
            pass

    if xpath:
        try:
            return page.locator(f"xpath={xpath}").first
        except Exception:
            pass

    if css:
        try:
            return page.locator(css).first
        except Exception:
            pass

    if name:
        try:
            return page.get_by_text(name, exact=exact).first
        except Exception:
            pass

    return None


async def _wait_visible(locator: Locator, timeout_ms: int) -> Locator | None:
    try:
        if await locator.count() == 0:
            return None
        candidate = locator.first
        await candidate.wait_for(
            state="visible",
            timeout=max(200, min(timeout_ms, 1200)),
        )
        return candidate
    except Exception:
        return None


async def resolve_primary(
    page: Page,
    target: Target,
    reason: str = "",
    timeout_ms: int = 1500,
) -> Locator:
    # Search the main document first, then every live iframe. Playwright
    # locators naturally pierce open Shadow DOM, so no website-specific
    # selector rules are required.
    frames = [page]
    try:
        frames.extend([frame for frame in page.frames if frame is not page.main_frame])
    except Exception:
        pass

    for frame in frames:
        primary = _primary_locator(frame, target)
        if primary is not None:
            found = await _wait_visible(primary, timeout_ms)
            if found is not None:
                return found
        try:
            fallback = await resolve_fallback(frame, target, reason=reason)
            if fallback is not None:
                return fallback
        except Exception:
            continue

    try:
        target_dump = target.model_dump()
    except Exception:
        target_dump = str(target)

    raise ValueError(
        "Unable to resolve target in the current page or its frames. "
        f"reason={reason!r}, target={target_dump}"
    )


# ---------------------------------------------------------------------------
# Bulk DOM inspection
# ---------------------------------------------------------------------------

async def _bulk_inspect(
    page: Page,
    selector: str,
    *,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Inspect a selector in one browser evaluation round trip."""
    script = """
    (elements, limit) => {
        const output = [];

        function clean(value) {
            return String(value || "")
                .replace(/\\s+/g, " ")
                .trim()
                .slice(0, 800);
        }

        function visible(el) {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (
                style.display === "none" ||
                style.visibility === "hidden" ||
                style.visibility === "collapse" ||
                Number(style.opacity || 1) === 0
            ) return false;
            if (
                el.hasAttribute("hidden") ||
                el.getAttribute("aria-hidden") === "true"
            ) return false;

            const rect = el.getBoundingClientRect();
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom >= 0 &&
                rect.right >= 0
            );
        }

        function disabled(el) {
            return (
                el.hasAttribute("disabled") ||
                el.getAttribute("aria-disabled") === "true"
            );
        }

        const max = Math.min(
            Array.isArray(elements) ? elements.length : 0,
            limit || 300
        );

        for (let index = 0; index < max; index++) {
            const el = elements[index];
            if (!visible(el) || disabled(el)) continue;

            const rect = el.getBoundingClientRect();

            output.push({
                index,
                tag: String(el.tagName || "").toLowerCase(),
                text: clean(el.innerText || el.textContent || ""),
                aria: clean(el.getAttribute("aria-label")),
                title: clean(el.getAttribute("title")),
                placeholder: clean(el.getAttribute("placeholder")),
                name: clean(el.getAttribute("name")),
                role: clean(el.getAttribute("role")),
                type: clean(el.getAttribute("type")),
                href: clean(el.getAttribute("href")),
                value: clean(el.value),
                y: Number(rect.top || 0),
                x: Number(rect.left || 0),
                width: Number(rect.width || 0),
                height: Number(rect.height || 0)
            });
        }

        return output;
    }
    """

    try:
        return await page.locator(selector).evaluate_all(script, limit)
    except Exception:
        return []


def _metadata_locator(
    page: Page,
    selector: str,
    metadata: dict[str, Any],
) -> Locator | None:
    try:
        return page.locator(selector).nth(int(metadata["index"]))
    except Exception:
        return None


def _metadata_haystack(item: dict[str, Any]) -> str:
    return _text(
        " ".join(
            str(item.get(key, ""))
            for key in (
                "text",
                "aria",
                "title",
                "placeholder",
                "name",
                "role",
                "href",
                "value",
            )
        )
    )


def _position_score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("y", 10000)) + float(item.get("x", 0)) * 0.01
    except Exception:
        return 10000.0


def _find_best(
    items: list[dict[str, Any]],
    score_fn: Callable[[dict[str, Any]], float],
) -> dict[str, Any] | None:
    scored: list[tuple[float, int, dict[str, Any]]] = []

    for item in items:
        try:
            score = float(score_fn(item))
        except Exception:
            continue
        scored.append((score, int(item.get("index", 0)), item))

    if not scored:
        return None

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return scored[0][2]


# ---------------------------------------------------------------------------
# Semantic helpers
# ---------------------------------------------------------------------------

def _github_repository_href(href: str, text: str = "") -> bool:
    value = (href or "").strip()

    if not value or not value.startswith("/"):
        return False
    if "?" in value or "#" in value:
        return False

    parts = [part for part in value.split("/") if part]
    if len(parts) != 2:
        return False

    blocked = {
        "search", "topics", "collections", "marketplace", "explore",
        "features", "settings", "notifications", "organizations", "orgs",
        "login", "signup", "about", "pricing", "enterprise", "security",
        "customer-stories",
    }

    return parts[0].lower() not in blocked and parts[1].lower() not in blocked


def _is_wikipedia_special(href: str) -> bool:
    lowered = href.lower()
    return any(
        marker in lowered
        for marker in (
            "/wiki/special:",
            "/wiki/help:",
            "/wiki/talk:",
            "/wiki/file:",
            "/wiki/category:",
            "/wiki/portal:",
        )
    )


def _region_bonus(item: dict[str, Any]) -> float:
    # element_resolver receives compact DOM data rather than the inspector's
    # richer snapshot, so infer main-content preference from common metadata.
    haystack = _metadata_haystack(item)
    if any(word in haystack for word in ("sign in", "login", "privacy", "terms")):
        return -10.0
    return 0.0


# ---------------------------------------------------------------------------
# Main fallback resolver
# ---------------------------------------------------------------------------

async def resolve_fallback(
    page: Page,
    target: Target,
    reason: str = "",
) -> Locator | None:
    flags = _reason_flags(reason)
    target_text = _target_text(target)
    target_tokens = set(
        _tokens(
            getattr(target, "name", None),
            getattr(target, "placeholder", None),
            getattr(target, "label", None),
            reason,
        )
    )
    url = (page.url or "").lower()

    def score_item(
        item: dict[str, Any],
        *,
        content_bonus: float = 0.0,
        href_bonus: float = 0.0,
    ) -> float:
        haystack = _metadata_haystack(item)
        score = content_bonus + href_bonus + _region_bonus(item)

        if target_text and target_text == haystack:
            score += 50
        elif target_text and target_text in haystack:
            score += 24

        for token in target_tokens:
            if token in haystack:
                score += 5

        tag = _text(str(item.get("tag", "")))
        if tag in {"a", "button"}:
            score += 2

        # Small top-of-page preference, never enough to beat semantic evidence.
        y = max(0.0, _position_score(item))
        score += max(0.0, 4.0 - min(y / 500.0, 4.0))
        return score

    def choose(
        selector: str,
        items: list[dict[str, Any]],
        minimum: float = 0.0,
    ) -> Locator | None:
        if not items:
            return None

        selected = _find_best(
            items,
            lambda item: score_item(item),
        )
        if selected is None:
            return None

        if score_item(selected) < minimum:
            return None

        return _metadata_locator(page, selector, selected)

    # 1. Search control. Prefer semantic target evidence instead of blindly
    # taking the first search input when several exist.
    if "search" in flags or "search" in target_text or "query" in flags:
        search_selectors = [
            "input[type='search']",
            "input[placeholder*='search' i]",
            "input[aria-label*='search' i]",
            "textarea[placeholder*='search' i]",
            "[role='searchbox']",
            "input[name*='search' i]",
        ]

        candidates: list[tuple[float, str, dict[str, Any]]] = []

        for selector in search_selectors:
            items = await _bulk_inspect(page, selector, limit=40)
            for item in items:
                score = 20.0
                haystack = _metadata_haystack(item)
                for token in target_tokens:
                    if token in haystack:
                        score += 8
                if "search" in haystack:
                    score += 4
                score -= max(0.0, min(_position_score(item) / 1000.0, 3.0))
                candidates.append((score, selector, item))

        if candidates:
            candidates.sort(
                key=lambda entry: (-entry[0], int(entry[2].get("index", 0)))
            )
            _, selector, item = candidates[0]
            result = _metadata_locator(page, selector, item)
            if result is not None:
                return result

    # 2. GitHub repository.
    if "repository" in flags and "github.com" in url:
        for selector in ("main a[href]", "[role='main'] a[href]", "article a[href]"):
            items = await _bulk_inspect(page, selector, limit=300)
            matching = [
                item
                for item in items
                if _github_repository_href(
                    str(item.get("href", "")),
                    str(item.get("text", "")),
                )
            ]
            if matching:
                selected = min(
                    matching,
                    key=lambda item: (_position_score(item), int(item.get("index", 0))),
                )
                result = _metadata_locator(page, selector, selected)
                if result is not None:
                    return result

    # 3. Generic repository.
    if "repository" in flags:
        for selector in ("main a[href]", "article a[href]", "[role='main'] a[href]"):
            items = await _bulk_inspect(page, selector, limit=300)
            matching = []
            for item in items:
                haystack = _metadata_haystack(item)
                href = _text(str(item.get("href", "")))
                if "repository" in haystack or re.search(r"\brepo\b", haystack) or "/repo" in href:
                    matching.append(item)
            if matching:
                selected = min(
                    matching,
                    key=lambda item: (_position_score(item), int(item.get("index", 0))),
                )
                result = _metadata_locator(page, selector, selected)
                if result is not None:
                    return result

    # 4. YouTube video. Prefer actual /watch results and semantic target text.
    if "video" in flags and ("youtube.com" in url or "youtu.be" in url):
        for selector in (
            "ytd-video-renderer a[href]",
            "ytd-rich-item-renderer a[href]",
            "main a[href]",
            "a[href]",
        ):
            items = await _bulk_inspect(page, selector, limit=250)
            matching = [
                item
                for item in items
                if "/watch" in _text(str(item.get("href", "")))
            ]
            if matching:
                selected = _find_best(
                    matching,
                    lambda item: score_item(item, content_bonus=12),
                )
                if selected is not None:
                    result = _metadata_locator(page, selector, selected)
                    if result is not None:
                        return result

    # 5. Wikipedia article.
    if "article" in flags and "wikipedia.org" in url:
        for selector in ("main a[href]", "article a[href]", "[role='main'] a[href]"):
            items = await _bulk_inspect(page, selector, limit=250)
            matching = [
                item
                for item in items
                if "/wiki/" in str(item.get("href", ""))
                and not _is_wikipedia_special(str(item.get("href", "")))
            ]
            if matching:
                selected = _find_best(
                    matching,
                    lambda item: score_item(item, content_bonus=10),
                )
                if selected is not None:
                    result = _metadata_locator(page, selector, selected)
                    if result is not None:
                        return result

    # 6. Issues.
    if "issues" in flags:
        for selector in ("main a[href]", "nav a[href]", "a[href]"):
            items = await _bulk_inspect(page, selector, limit=200)
            matching = [
                item
                for item in items
                if _text(str(item.get("href", ""))).rstrip("/").endswith("/issues")
            ]
            if matching:
                selected = min(
                    matching,
                    key=lambda item: (_position_score(item), int(item.get("index", 0))),
                )
                result = _metadata_locator(page, selector, selected)
                if result is not None:
                    return result

    # 7. First/top result. This path is deliberately semantic: it will not
    # click an arbitrary header link merely because it is first in the DOM.
    if "first" in flags and ("result" in flags or "click" in flags):
        selectors = (
            "main a[href]",
            "main button",
            "article a[href]",
            "[role='main'] a[href]",
        )

        blocked = (
            "sign in", "sign up", "login", "menu", "settings",
            "home", "help", "privacy", "terms", "notifications",
        )

        for selector in selectors:
            items = await _bulk_inspect(page, selector, limit=250)
            if not items:
                continue

            scored: list[tuple[float, int, dict[str, Any]]] = []

            for item in items:
                haystack = _metadata_haystack(item)
                score = score_item(item, content_bonus=5)

                if any(word in haystack for word in blocked):
                    score -= 20

                href = _text(str(item.get("href", "")))
                if href.startswith("javascript:") or href in {"#", ""}:
                    score -= 15

                scored.append((score, int(item.get("index", 0)), item))

            scored.sort(key=lambda entry: (-entry[0], entry[1]))
            if scored and scored[0][0] >= 5:
                result = _metadata_locator(page, selector, scored[0][2])
                if result is not None:
                    return result

    # 8. Explicit target name/label/placeholder.
    if target_text:
        for selector in (
            "button",
            "[role='button']",
            "a[href]",
            "input",
            "textarea",
            "select",
            "[role='link']",
        ):
            items = await _bulk_inspect(page, selector, limit=250)
            result = choose(selector, items, minimum=1.0)
            if result is not None:
                return result

    # 9. Final generic semantic search. Fail closed if there is no evidence.
    selector = (
        "a[href], button, input, textarea, select, "
        "[role='button'], [role='link'], [contenteditable='true']"
    )
    items = await _bulk_inspect(page, selector, limit=300)
    return choose(selector, items, minimum=1.0)


# ---------------------------------------------------------------------------
# Editable/textbox resolver
# ---------------------------------------------------------------------------

async def resolve_editable(
    page: Page,
    target: Target | None = None,
    reason: str = "",
) -> Locator:
    if target is not None:
        try:
            return await resolve_primary(
                page,
                target,
                reason=reason,
                timeout_ms=1000,
            )
        except Exception:
            pass

    reason_text = _text(reason)
    search_hint = "search" in reason_text or "query" in reason_text

    selectors: list[str] = []
    if search_hint:
        selectors.extend(
            [
                "input[type='search']",
                "input[placeholder*='search' i]",
                "input[aria-label*='search' i]",
                "[role='searchbox']",
                "input[name*='search' i]",
            ]
        )

    selectors.extend(
        [
            "[role='textbox']",
            "textarea",
            "input:not([type='hidden'])",
            "[contenteditable='true']",
        ]
    )

    seen: set[str] = set()
    for selector in selectors:
        if selector in seen:
            continue
        seen.add(selector)

        items = await _bulk_inspect(page, selector, limit=50)
        if not items:
            continue

        selected = min(
            items,
            key=lambda item: (
                _position_score(item),
                int(item.get("index", 0)),
            ),
        )
        result = _metadata_locator(page, selector, selected)
        if result is not None:
            return result

    raise ValueError("No editable textbox was found on the page.")
