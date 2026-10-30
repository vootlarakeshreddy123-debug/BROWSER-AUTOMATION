from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse
import ipaddress

from agent.action_schema import Action, ActionType
from agent.recovery import RetryBudget
from browser import element_resolver
from config import settings


logger = logging.getLogger("browser.executor")


class ExecutionError(Exception):
    """Raised when a browser action cannot be completed safely."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


def _action_timeout_ms() -> int:
    """
    Keep individual Playwright actions bounded.

    The orchestrator already owns the overall task timeout, so an individual
    browser action should not consume most of the task budget.
    """
    try:
        configured = int(
            getattr(
                settings,
                "BROWSER_ACTION_TIMEOUT_MS",
                7000,
            )
        )
    except (TypeError, ValueError):
        configured = 7000

    return max(1000, min(configured, 12000))


def _resolver_timeout_ms() -> int:
    """Short target-resolution timeout; resolver performs DOM fallback."""
    return max(500, min(_action_timeout_ms(), 3000))


async def execute_plan(
    page: Any,
    actions: list[Action],
    retry_budget: RetryBudget,
) -> dict[str, Any]:
    """
    Execute a browser plan sequentially.

    Retry policy:
        - target resolution owns retries
        - individual click/type/press operations do NOT start their own
          independent retry loops
        - after an execution failure, the orchestrator can observe the fresh
          DOM and ask the planner for a different action

    This keeps retry counts predictable and prevents nested retry explosions.
    """
    if page is None:
        raise ExecutionError("Browser page is not available.")

    if not actions:
        return {
            "message": "No browser actions were required.",
            "final_url": getattr(page, "url", ""),
            "final_title": await _safe_title(page),
        }

    extracted_values: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    from browser.manager import browser_manager

    for action in actions:
        _adopted_before = len(browser_manager.adoption_log)
        _ev_started = asyncio.get_running_loop().time()
        _ev_before = {
            "url": getattr(page, "url", ""),
            "page_id": browser_manager.page_identity(page),
            "page_count": len(browser_manager.pages_snapshot()) or 1,
        }
        if not isinstance(action, Action):
            try:
                action = Action.model_validate(action)
            except Exception as exc:
                raise ExecutionError(
                    f"Invalid browser action: {exc}",
                    cause=exc,
                ) from exc

        try:
            if action.type == ActionType.NAVIGATE:
                await _do_navigate(page, action)

            elif action.type == ActionType.CLICK:
                await _do_click(page, action, retry_budget)

            elif action.type == ActionType.TYPE:
                await _do_type(page, action, retry_budget)

            elif action.type == ActionType.PRESS:
                await _do_press(page, action, retry_budget)

            elif action.type == ActionType.WAIT_FOR:
                await _do_wait_for(page, action, retry_budget)

            elif action.type == ActionType.EXTRACT:
                extracted_values.append(await _do_extract(page, action, retry_budget))

            elif action.type == ActionType.DOWNLOAD:
                await _do_download(page, action, retry_budget)

            elif action.type == ActionType.SCROLL:
                await _do_scroll(page, action)

            elif action.type == ActionType.HOVER:
                await _do_hover(page, action, retry_budget)

            elif action.type == ActionType.SELECT_OPTION:
                await _do_select_option(page, action, retry_budget)

            elif action.type == ActionType.CHECK:
                await _do_check(page, action, retry_budget, checked=True)

            elif action.type == ActionType.UNCHECK:
                await _do_check(page, action, retry_budget, checked=False)

            elif action.type == ActionType.UPLOAD:
                await _do_upload(page, action, retry_budget)

            elif action.type == ActionType.BACK:
                moved = await browser_manager.history_back(page)
                if not moved["moved"]:
                    raise ExecutionError("Browser history did not move back (there may be no earlier page).")

            elif action.type == ActionType.FORWARD:
                moved = await browser_manager.history_forward(page)
                if not moved["moved"]:
                    raise ExecutionError("Browser history did not move forward (there may be no later page).")

            elif action.type == ActionType.NEW_TAB:
                if action.url:
                    _validate_navigation_url(action.url)
                new_page = await browser_manager.open_new_tab(action.url)
                if new_page is None:
                    raise ExecutionError("Could not open a new tab.")

            elif action.type == ActionType.SWITCH_TAB:
                switched = await browser_manager.switch_page(action.value or "other")
                if switched is None:
                    raise ExecutionError("There is no other open tab to switch to.")

            elif action.type == ActionType.CLOSE_TAB:
                survivor = await browser_manager.close_page(page)
                if survivor is None:
                    raise ExecutionError("Cannot close the only open tab.")

            else:
                raise ExecutionError(
                    f"Unsupported browser action type: {action.type!r}"
                )

        except ExecutionError:
            raise

        except Exception as exc:
            raise ExecutionError(
                f"Browser action '{action.type.value}' failed: {exc}",
                cause=exc,
            ) from exc

        _ev_page = browser_manager.current_page if len(browser_manager.adoption_log) > _adopted_before else page
        _ev_page = _ev_page or page
        evidence.append({
            "action": action.type.value,
            "target": (action.target.model_dump(exclude_none=True) if action.target else None),
            "success": True,
            "duration_ms": int((asyncio.get_running_loop().time() - _ev_started) * 1000),
            "before_url": _ev_before["url"],
            "after_url": getattr(_ev_page, "url", ""),
            "before_page_id": _ev_before["page_id"],
            "after_page_id": browser_manager.page_identity(_ev_page),
            "page_count_before": _ev_before["page_count"],
            "page_count_after": len(browser_manager.pages_snapshot()) or 1,
            "new_page_detected": len(browser_manager.adoption_log) > _adopted_before,
            "typed_text": action.text if action.type == ActionType.TYPE else None,
            "key": action.key if action.type == ActionType.PRESS else None,
        })

        # A click may have opened (and adopted) a new tab: every following
        # action in this plan, and the returned final_url/title, must use it.
        if len(browser_manager.adoption_log) > _adopted_before:
            current = browser_manager.current_page
            if current is not None:
                page = current

    message = "Browser actions completed."
    if extracted_values:
        values = []
        for item in extracted_values:
            text = str(item.get("text") or "").strip()
            if text:
                values.append(text)
        if values:
            message += " Extracted: " + " | ".join(values)

    return {
        "message": message,
        "final_url": getattr(page, "url", ""),
        "final_title": await _safe_title(page),
        "extracted": extracted_values,
        "evidence": evidence,
    }


async def _page_context(page: Any) -> str:
    try:
        return str(page.url)
    except Exception:
        return ""


async def _safe_title(page: Any) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def _wait_after_interaction(page: Any) -> None:
    """
    Give dynamic pages a short opportunity to settle.

    Do not use long network-idle waits: modern sites frequently keep
    connections open forever.
    """
    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=900,
        )
    except Exception:
        pass

    await asyncio.sleep(0.25)


async def _do_scroll(page: Any, action: Action) -> None:
    amount = max(100, min(int(action.amount or 700), 5000))

    try:
        if action.direction == "up":
            amount = -amount

        await page.mouse.wheel(0, amount)
        await asyncio.sleep(0.35)

    except Exception as exc:
        raise ExecutionError(
            f"Scroll failed: {exc}",
            cause=exc,
        ) from exc


def _validate_navigation_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ExecutionError("Only http(s) navigation is allowed.")
    if parsed.username or parsed.password:
        raise ExecutionError("URLs containing embedded credentials are not allowed.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ExecutionError("Navigation URL has no hostname.")
    if getattr(settings, "ALLOW_PRIVATE_NETWORK", False):
        return
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise ExecutionError("Private/local network navigation is disabled by NEXA security policy. Set ALLOW_PRIVATE_NETWORK=true if this is an intentional local task.")
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
            raise ExecutionError("Private/local network navigation is disabled by NEXA security policy. Set ALLOW_PRIVATE_NETWORK=true if this is an intentional local task.")
    except ValueError:
        pass


async def _do_navigate(page: Any, action: Action) -> None:
    url = (action.url or "").strip()

    if not url:
        raise ExecutionError("Navigate action has no URL.")

    if not (
        url.lower().startswith("http://")
        or url.lower().startswith("https://")
    ):
        raise ExecutionError(
            "Navigate action requires an absolute http(s) URL."
        )
    _validate_navigation_url(url)

    timeout = _action_timeout_ms()

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout,
        )
    except Exception as exc:
        # A page can successfully change URL while a site's load event remains
        # slow. Verify the live URL before treating the navigation as failed.
        try:
            current_url = str(page.url or "")
        except Exception:
            current_url = ""

        if current_url and url.rstrip("/") in current_url:
            logger.warning(
                "Navigation raised after URL changed; accepting current page: "
                "%s",
                current_url,
            )
        else:
            raise ExecutionError(
                f"Navigation failed for {url}: {exc}",
                cause=exc,
            ) from exc

    await _wait_after_interaction(page)


def _retry_available(retry_budget: RetryBudget) -> bool:
    """
    Consume one retry from the shared task budget.

    RetryBudget is intentionally kept opaque here; this helper only relies on
    its public consume() contract.
    """
    try:
        return bool(retry_budget.consume())
    except Exception:
        return False


async def _resolve_with_retry(
    page: Any,
    action: Action,
    retry_budget: RetryBudget,
):
    """
    Resolve an action target with one shared retry mechanism.

    The resolver itself performs semantic fallback (role/name, labels,
    placeholders, DOM metadata, relevant-result heuristics, etc.). This
    function only decides whether to give resolution another short chance.
    """
    if action.target is None:
        raise ExecutionError(
            f"Action '{action.type.value}' requires a target."
        )

    last_error: Exception | None = None

    while True:
        try:
            return await element_resolver.resolve_primary(
                page,
                action.target,
                reason=action.reason,
                timeout_ms=_resolver_timeout_ms(),
            )

        except Exception as exc:
            last_error = exc

            if not _retry_available(retry_budget):
                break

            # Let a dynamic page settle before asking the resolver again.
            await asyncio.sleep(0.25)

    raise ExecutionError(
        "Unable to resolve the requested browser element. "
        f"reason={action.reason!r}; error={last_error}",
        cause=last_error,
    ) from last_error


async def _do_click(
    page: Any,
    action: Action,
    retry_budget: RetryBudget,
) -> None:
    locator = await _resolve_with_retry(
        page,
        action,
        retry_budget,
    )

    timeout = _action_timeout_ms()

    from browser.manager import browser_manager

    pages_before = browser_manager.pages_snapshot()
    try:
        await locator.click(timeout=timeout)
    except Exception as exc:
        text = str(exc).lower()

        if "intercepts pointer events" in text:
            raise ExecutionError(
                "The target is covered by another page element or modal. "
                "The planner should inspect the current DOM and choose the "
                "visible target/overlay action instead of blindly retrying.",
                cause=exc,
            ) from exc

        if "target closed" in text or "page closed" in text:
            raise ExecutionError(
                "The browser page closed while clicking.",
                cause=exc,
            ) from exc

        raise ExecutionError(
            f"Click failed for '{action.reason or 'target'}': {exc}",
            cause=exc,
        ) from exc

    # Generic new-tab/popup handling (single mechanism): diff the context's
    # pages, adopt the new one. The orchestrator re-reads the active page from
    # the browser manager after every action.
    try:
        adopted = await browser_manager.adopt_new_pages(pages_before, origin=page, action="click")
    except Exception as exc:
        adopted = None
        logger.debug("New-page detection failed: %s", exc)
    if adopted is not None:
        return

    await _wait_after_interaction(page)


async def _do_type(
    page: Any,
    action: Action,
    retry_budget: RetryBudget,
) -> None:
    text = action.text

    if text is None or not str(text).strip():
        raise ExecutionError("Type action has no text.")

    # If a target exists, use the same semantic resolver as click/press.
    # If it does not, resolve_editable can select an appropriate textbox from
    # the live DOM, using the action reason to prefer search controls.
    if action.target is not None:
        locator = await _resolve_with_retry(
            page,
            action,
            retry_budget,
        )
    else:
        last_error: Exception | None = None
        while True:
            try:
                locator = await element_resolver.resolve_editable(
                    page,
                    target=None,
                    reason=action.reason,
                )
                break
            except Exception as exc:
                last_error = exc
                if not _retry_available(retry_budget):
                    raise ExecutionError(
                        "Unable to find an editable textbox for the type "
                        f"action: {exc}",
                        cause=exc,
                    ) from exc
                await asyncio.sleep(0.25)

    timeout = _action_timeout_ms()

    try:
        # fill() works for normal inputs/textareas and is deterministic.
        await locator.fill(
            str(text),
            timeout=timeout,
        )

    except Exception as exc:
        # Some contenteditable/custom controls do not support fill().
        try:
            await locator.click(timeout=timeout)
            await locator.press("Control+A", timeout=timeout)
            await locator.type(
                str(text),
                timeout=timeout,
            )
        except Exception as fallback_exc:
            raise ExecutionError(
                f"Typing failed for '{action.reason or 'target'}': "
                f"{fallback_exc}",
                cause=fallback_exc,
            ) from fallback_exc

    await self_verify_typed_text(
        locator,
        str(text),
        reason=action.reason,
    )

    await _wait_after_interaction(page)


async def self_verify_typed_text(
    locator: Any,
    expected: str,
    *,
    reason: str = "",
) -> None:
    """
    Verify text actually reached the resolved control.

    Unlike the previous executor, a mismatch is a real execution failure.
    That allows the adaptive loop to inspect the fresh DOM instead of
    incorrectly recording a successful type action.
    """
    expected = str(expected)

    if expected == "":
        return

    normalized_expected = " ".join(
        expected.split()
    ).casefold()

    # Inputs/textareas expose input_value().
    try:
        value = await locator.input_value(timeout=1200)
        normalized_value = " ".join(
            str(value or "").split()
        ).casefold()

        if normalized_expected in normalized_value:
            return

        raise ExecutionError(
            "Typed text was not present in the input after typing. "
            f"expected={expected!r}, actual={value!r}, "
            f"reason={reason!r}"
        )

    except ExecutionError:
        raise

    except Exception:
        pass

    # Contenteditable and custom controls may expose text_content instead.
    try:
        content = await locator.text_content(timeout=1200)
        normalized_content = " ".join(
            str(content or "").split()
        ).casefold()

        if normalized_expected in normalized_content:
            return

        # If the control clearly exposes text but it does not match, this is
        # a genuine failure. Do not silently accept it.
        if normalized_content:
            raise ExecutionError(
                "Typed text was not present in the target content after "
                f"typing. expected={expected!r}, actual={content!r}, "
                f"reason={reason!r}"
            )

    except ExecutionError:
        raise

    except Exception:
        # Some custom browser widgets do not expose either value or text.
        # In that narrow case, the browser action itself succeeded and the
        # next DOM observation remains authoritative.
        logger.debug(
            "Could not directly verify typed text for reason=%r",
            reason,
            exc_info=True,
        )


async def _do_press(
    page: Any,
    action: Action,
    retry_budget: RetryBudget,
) -> None:
    key = (action.key or "").strip()

    if not key:
        raise ExecutionError("Press action has no key.")

    timeout = _action_timeout_ms()

    if action.target is not None:
        locator = await _resolve_with_retry(
            page,
            action,
            retry_budget,
        )

        try:
            await locator.focus(timeout=timeout)
            await locator.press(
                key,
                timeout=timeout,
            )
        except Exception as exc:
            raise ExecutionError(
                f"Key press '{key}' failed for "
                f"'{action.reason or 'target'}': {exc}",
                cause=exc,
            ) from exc

    else:
        try:
            await page.keyboard.press(key)
        except Exception as exc:
            raise ExecutionError(
                f"Key press '{key}' failed: {exc}",
                cause=exc,
            ) from exc

    await _wait_after_interaction(page)


async def _do_wait_for(
    page: Any,
    action: Action,
    retry_budget: RetryBudget,
) -> None:
    if action.target is None:
        # A wait without a target is still useful as a short stabilization
        # action. Keep it bounded so an AI cannot stall the task indefinitely.
        await asyncio.sleep(0.75)
        return

    # resolve_primary already waits for visibility and performs semantic
    # fallback. Do not add a second nested Playwright wait loop.
    await _resolve_with_retry(
        page,
        action,
        retry_budget,
    )


async def _do_extract(page: Any, action: Action, retry_budget: RetryBudget) -> dict[str, Any]:
    # Extract page information from a target or semantic DOM evidence.
    if action.target is not None:
        locator = await _resolve_with_retry(page, action, retry_budget)
        try:
            text = await locator.inner_text(timeout=_action_timeout_ms())
        except Exception as exc:
            raise ExecutionError(f"Extraction failed for '{action.reason or 'target'}': {exc}", cause=exc) from exc
        return {
            "reason": action.reason,
            "text": " ".join(str(text or "").split())[:5000],
            "url": getattr(page, "url", ""),
            "method": "target",
            "page_title": await _safe_title(page),
            "kind": "target_text",
        }

    try:
        value = await page.evaluate(_SEMANTIC_EXTRACT_JS, action.reason or "extract the requested information")
    except Exception as exc:
        raise ExecutionError(f"Semantic extraction failed for '{action.reason or 'page'}': {exc}", cause=exc) from exc

    if not isinstance(value, dict) or not str(value.get("text") or "").strip():
        raise ExecutionError(f"Semantic extraction found no matching content for: {action.reason or 'requested information'}")
    value["reason"] = action.reason
    value["url"] = getattr(page, "url", "")
    value["page_title"] = await _safe_title(page)
    value["method"] = "semantic_dom"
    return value


_SEMANTIC_EXTRACT_JS = r'''
(reason) => {
  const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const lower = (v) => normalize(v).toLowerCase();
  const firstSentence = (v) => {
    const text = normalize(v);
    if (!text) return '';
    const m = text.match(/^.*?[.!?](?:\s|$)/);
    return (m ? m[0] : text).trim();
  };
  const tokensFrom = (v) => lower(v).match(/[a-z0-9]{3,}/g) || [];
  const stop = new Set([
    'the','and','for','from','with','that','this','section','sentence',
    'paragraph','first','last','tell','give','show','read','report',
    'extract','information','about','explains','explaining','describes',
    'describe','covers','cover','find','finds','relationship','between',
    'relevant','page','content','requested','request'
  ]);
  const wanted = [...new Set(tokensFrom(reason).filter(t => !stop.has(t)))];
  // Keep semantic extraction task-agnostic.  Multi-word concepts are
  // represented by their tokens rather than by a hard-coded topic list.
  const phraseWanted = [];
  const normalizedReason = lower(reason);
  const conceptMatches = normalizedReason.match(/\b[a-z0-9]+(?:\s+[a-z0-9]+){1,3}\b/g) || [];
  for (const phrase of conceptMatches) {
    const parts = phrase.split(/\s+/).filter(t => t.length >= 3 && !stop.has(t));
    if (parts.length >= 2) phraseWanted.push(parts.join(' '));
  }
  const isFirstSentence = /first\s+sentence/i.test(reason);
  const isFirstHeading = /first\s+(?:heading|title)/i.test(reason);
  const asksTitle = /\btitle\b/i.test(reason) && !/section/i.test(reason);
  const sectionRequested = /\bsection\b/i.test(reason);
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || s.display === 'none' || s.visibility === 'hidden') return false;
    if (el.closest('header,nav,footer,aside,[role="navigation"],[role="banner"],[role="contentinfo"],[aria-hidden="true"]')) return false;
    return true;
  };
  const semanticRegion = (el) => {
    const region = el.closest('main,[role="main"],article,[role="article"]');
    return region ? 'content' : 'other';
  };
  if (asksTitle) return {text: normalize(document.title), kind:'title', tag:'title'};

  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).filter(visible);
  // A request for a heading is answered ONLY from a real heading element -
  // never from paragraph text (that fallback was the "first heading returned a
  // sentence" false success).
  const wantsHeading = /\bheadings?\b/i.test(reason) && !/\b(?:under|below|beneath|section)\b/i.test(reason);
  if (wantsHeading || isFirstHeading) {
    const ords = {first: 0, second: 1, third: 2, fourth: 3, fifth: 4, last: -1};
    const om = lower(reason).match(/\b(first|second|third|fourth|fifth|last)\b/);
    const pos = om ? ords[om[1]] : 0;
    const inContent = headings.filter(x => semanticRegion(x) === 'content');
    const pool = inContent.length ? inContent : headings;
    const h = pos === -1 ? pool[pool.length - 1] : pool[pos];
    if (!h) return null;
    const t = normalize(h.innerText || h.textContent);
    return t ? {text: t, kind:'heading', tag: h.tagName.toLowerCase(), index: headings.indexOf(h), section_heading: t} : null;
  }

  const buildSection = (h) => {
    const level = Number(h.tagName.slice(1));
    const parts = [];
    let node = h.nextElementSibling;
    let guard = 0;
    while (node && guard++ < 160) {
      if (/^H[1-6]$/.test(node.tagName) && Number(node.tagName.slice(1)) <= level) break;
      if (visible(node) && /^(P|DIV|LI|BLOCKQUOTE|PRE|TABLE|UL|OL)$/.test(node.tagName)) {
        const t = normalize(node.innerText || node.textContent);
        if (t && t.length > 2) parts.push(t);
      }
      node = node.nextElementSibling;
    }
    return normalize(parts.join(' '));
  };

  if (sectionRequested || isFirstSentence) {
    let best = null;
    let bestScore = -Infinity;
    for (const h of headings) {
      const level = Number(h.tagName.slice(1));
      const headingText = normalize(h.innerText || h.textContent);
      const headingLower = lower(headingText);
      const sectionText = buildSection(h);
      const combined = lower(`${headingText} ${sectionText.slice(0, 5000)}`);
      if (!combined) continue;

      let score = 0;
      let headingHits = 0;
      let contentHits = 0;
      for (const token of wanted) {
        if (headingLower.includes(token)) { score += 7; headingHits += 1; }
        else if (combined.includes(token)) { score += 2; contentHits += 1; }
      }
      let phraseHits = 0;
      for (const phrase of phraseWanted) {
        if (headingLower.includes(phrase)) { score += 10; phraseHits += 1; }
        else if (combined.includes(phrase)) { score += 4; phraseHits += 1; }
      }
      if (wanted.length && headingHits === wanted.length) score += 14;
      if (wanted.length && contentHits >= Math.max(1, Math.ceil(wanted.length * 0.5))) score += 5;
      if (phraseHits >= 2) score += 10;
      if (level === 1) score -= 10;
      if (/^(references|external links|see also|contents|navigation|languages)$/i.test(headingText)) score -= 100;
      if (semanticRegion(h) === 'content') score += 8;
      if (sectionText.length > 80) score += 2;

      if (score > bestScore) {
        bestScore = score;
        best = {heading: headingText, text: sectionText, level};
      }
    }

    if (best && best.text && bestScore > 0) {
      return {
        text: isFirstSentence ? firstSentence(best.text) : best.text.slice(0,5000),
        kind: isFirstSentence ? 'first_sentence' : 'section',
        section_heading: best.heading,
      };
    }
  }

  const blocks = Array.from(document.querySelectorAll('main p,article p,main li,article li,main blockquote,article blockquote,p,li,blockquote'))
    .filter(visible);
  let bestBlock = null;
  let bestBlockScore = -Infinity;
  for (const el of blocks) {
    const t = normalize(el.innerText || el.textContent);
    if (!t || t.length < 20) continue;
    const lt = lower(t);
    let score = 0;
    for (const token of wanted) if (lt.includes(token)) score += 4;
    if (semanticRegion(el) === 'content') score += 4;
    if (t.length > 50) score += 1;
    if (score > bestBlockScore) { bestBlockScore = score; bestBlock = t; }
  }
  if (bestBlock && bestBlockScore > 0) {
    return {text: isFirstSentence ? firstSentence(bestBlock) : bestBlock.slice(0,5000), kind: isFirstSentence ? 'first_sentence' : 'text'};
  }
  return null;
}
'''

async def _do_hover(page: Any, action: Action, retry_budget: RetryBudget) -> None:
    locator = await _resolve_with_retry(page, action, retry_budget)
    try:
        await locator.hover(timeout=_action_timeout_ms())
    except Exception as exc:
        raise ExecutionError(f"Hover failed for '{action.reason or 'target'}': {exc}", cause=exc) from exc
    await _wait_after_interaction(page)


async def _do_select_option(page: Any, action: Action, retry_budget: RetryBudget) -> None:
    locator = await _resolve_with_retry(page, action, retry_budget)
    value = action.value or action.text
    if not value:
        raise ExecutionError("select_option action requires value or text.")
    try:
        await locator.select_option(label=value, timeout=_action_timeout_ms())
    except Exception:
        try:
            await locator.select_option(value=value, timeout=_action_timeout_ms())
        except Exception as exc:
            raise ExecutionError(f"Select option failed for '{action.reason or value}': {exc}", cause=exc) from exc
    await _wait_after_interaction(page)


async def _do_check(page: Any, action: Action, retry_budget: RetryBudget, *, checked: bool) -> None:
    locator = await _resolve_with_retry(page, action, retry_budget)
    try:
        if checked:
            await locator.check(timeout=_action_timeout_ms())
        else:
            await locator.uncheck(timeout=_action_timeout_ms())
    except Exception as exc:
        raise ExecutionError(f"Checkbox action failed for '{action.reason or 'target'}': {exc}", cause=exc) from exc
    await _wait_after_interaction(page)


async def _do_upload(page: Any, action: Action, retry_budget: RetryBudget) -> None:
    locator = await _resolve_with_retry(page, action, retry_budget)
    path = (action.file_path or action.text or "").strip()
    if not path:
        raise ExecutionError("upload action requires a file_path.")
    import os
    if not os.path.isfile(path):
        raise ExecutionError(f"Upload file does not exist: {path}")
    try:
        await locator.set_input_files(path, timeout=_action_timeout_ms())
    except Exception as exc:
        raise ExecutionError(f"File upload failed: {exc}", cause=exc) from exc
    await _wait_after_interaction(page)


async def _do_download(
    page: Any,
    action: Action,
    retry_budget: RetryBudget,
) -> None:
    if action.target is None:
        raise ExecutionError("Download action requires a target.")

    locator = await _resolve_with_retry(
        page,
        action,
        retry_budget,
    )

    timeout = _action_timeout_ms()

    try:
        async with page.expect_download(
            timeout=timeout,
        ) as download_info:
            await locator.click(
                timeout=timeout,
            )

        download = await download_info.value

        # Do not silently discard a failed download.
        failure = await download.failure()
        if failure:
            raise ExecutionError(
                f"Download failed: {failure}"
            )

    except ExecutionError:
        raise

    except Exception as exc:
        raise ExecutionError(
            f"Download action failed for "
            f"'{action.reason or 'target'}': {exc}",
            cause=exc,
        ) from exc

    await _wait_after_interaction(page)


# Backward-compatible private helper name used by older code/tests.
_verify_typed_text = self_verify_typed_text
