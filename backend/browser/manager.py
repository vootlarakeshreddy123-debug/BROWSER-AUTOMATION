"""Persistent, real Chrome session for the Browser Automation Agent.

NEXA owns one dedicated Chrome window and one task tab.  Reusing one
controlled tab prevents the browser automation page from becoming different
from the tab the user actually sees.  Explicit browser actions can still
create additional tabs when the executor is extended to support them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from config import settings
from browser.window_focus import (
    activate_chrome_window,
    find_chrome_window,
    windows_for_process,
)

logger = logging.getLogger("browser.manager")


class BrowserManager:
    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._current_page: Optional[Page] = None

        self._chrome_process: Optional[subprocess.Popen] = None

        self._starting = False

        self._chrome_hwnd: int | None = None
        self._known_chrome_hwnds: set[int] = set()

        self._profile_dir: Path | None = None
        self._cdp_port: int | None = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _context_alive(self) -> bool:
        if self._browser is None or self._context is None:
            return False

        try:
            return (
                self._browser.is_connected()
                and self._context in self._browser.contexts
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Chrome startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._context_alive():
            return

        if self._starting:
            while self._starting:
                await asyncio.sleep(0.03)

            if self._context_alive():
                return

        self._starting = True

        try:
            await self._stop_internal()

            self._playwright = await async_playwright().start()

            # Remember existing Chrome windows so that the focus code can
            # distinguish the dedicated automation window.
            self._known_chrome_hwnds = set()

            try:
                from browser.window_focus import _visible_chrome_windows

                self._known_chrome_hwnds = set(
                    _visible_chrome_windows()
                )
            except Exception:
                pass

            executable = self._find_chrome()

            # Find an unused local TCP port.
            self._cdp_port = self._free_port()

            # Give automation its own Chrome profile.
            self._profile_dir = (
                Path(tempfile.gettempdir())
                / "browser-automation-agent-chrome-profile"
            )

            self._profile_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            # Remove a stale DevToolsActivePort file if Chrome previously
            # terminated unexpectedly.
            active_port_file = (
                self._profile_dir / "DevToolsActivePort"
            )

            try:
                if active_port_file.exists():
                    active_port_file.unlink()
            except Exception:
                pass

            args = [
                executable,
                f"--remote-debugging-port={self._cdp_port}",
                f"--user-data-dir={self._profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                "--start-maximized",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion",
                "--remote-allow-origins=*",
                "about:blank",
            ]

            creationflags = 0

            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            logger.info(
                "Starting dedicated Chrome on CDP port %s.",
                self._cdp_port,
            )

            self._chrome_process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )

            # Give Chrome a moment to create its process/window before
            # polling the DevTools endpoint.
            await asyncio.sleep(0.25)

            await self._wait_for_cdp(self._cdp_port)

            if self._playwright is None:
                raise RuntimeError(
                    "Playwright was not initialized."
                )

            self._browser = (
                await self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self._cdp_port}"
                )
            )

            contexts = self._browser.contexts

            if not contexts:
                raise RuntimeError(
                    "Chrome connected, but no browser context is available."
                )

            self._context = contexts[0]
            self._attach_context_listeners()
            self._current_page = None
            self._chrome_hwnd = None

            logger.info(
                "Dedicated Chrome session connected on CDP port %s.",
                self._cdp_port,
            )

        except Exception:
            await self._stop_internal()
            raise

        finally:
            self._starting = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _free_port() -> int:
        with socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        ) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _find_chrome() -> str:
        candidates: list[str] = []

        if settings.BROWSER_CHROME_PATH:
            candidates.append(
                settings.BROWSER_CHROME_PATH
            )

        if sys.platform == "win32":
            local = os.environ.get(
                "LOCALAPPDATA",
                "",
            )

            program = os.environ.get(
                "PROGRAMFILES",
                "",
            )

            program_x86 = os.environ.get(
                "PROGRAMFILES(X86)",
                "",
            )

            candidates.extend(
                [
                    str(
                        Path(local)
                        / "Google/Chrome/Application/chrome.exe"
                    ),
                    str(
                        Path(program)
                        / "Google/Chrome/Application/chrome.exe"
                    ),
                    str(
                        Path(program_x86)
                        / "Google/Chrome/Application/chrome.exe"
                    ),
                ]
            )

        which = shutil.which("chrome")

        if which:
            candidates.insert(0, which)

        for path in candidates:
            if path and Path(path).exists():
                logger.info(
                    "Using Chrome executable: %s",
                    path,
                )
                return path

        raise RuntimeError(
            "Google Chrome was not found. "
            "Install Chrome or set BROWSER_CHROME_PATH in .env."
        )

    # ------------------------------------------------------------------
    # CDP readiness
    # ------------------------------------------------------------------

    async def _wait_for_cdp(
        self,
        port: int,
    ) -> None:
        # Chrome can occasionally take a few seconds to expose the
        # DevTools endpoint, especially on the first launch of a profile.
        deadline = time.monotonic() + 30.0

        url = (
            f"http://127.0.0.1:{port}"
            "/json/version"
        )

        last_error: Exception | None = None

        while time.monotonic() < deadline:
            # If the Chrome process died, report that immediately instead
            # of waiting for the complete timeout.
            if (
                self._chrome_process is not None
                and self._chrome_process.poll() is not None
            ):
                return_code = self._chrome_process.returncode

                raise RuntimeError(
                    "Chrome exited before remote debugging became ready "
                    f"(exit code {return_code})."
                )

            try:
                await asyncio.to_thread(
                    self._probe_url,
                    url,
                )

                logger.info(
                    "Chrome remote debugging is ready on port %s.",
                    port,
                )

                return

            except Exception as exc:
                last_error = exc

                await asyncio.sleep(0.15)

        raise RuntimeError(
            "Chrome remote debugging did not become ready: "
            f"{last_error}"
        )

    @staticmethod
    def _probe_url(
        url: str,
    ) -> None:
        with urllib.request.urlopen(
            url,
            timeout=1.5,
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"HTTP {response.status}"
                )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _stop_internal(self) -> None:
        self._current_page = None
        self._chrome_hwnd = None
        self._known_chrome_hwnds = set()

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass

            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

            self._playwright = None

        process = self._chrome_process
        self._chrome_process = None

        if (
            process is not None
            and process.poll() is None
        ):
            try:
                process.terminate()

                await asyncio.to_thread(
                    process.wait,
                    3,
                )

            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        self._context = None
        self._cdp_port = None

    async def stop(self) -> None:
        await self._stop_internal()

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._context_alive()

    @property
    def current_page(self) -> Optional[Page]:
        page = self._current_page

        if page is None:
            return None

        try:
            if page.is_closed():
                self._current_page = None
                return None

        except Exception:
            self._current_page = None
            return None

        return page

    # ------------------------------------------------------------------
    # Controlled task tab
    # ------------------------------------------------------------------

    async def _activate_page_via_cdp(self, page: Page) -> bool:
        """Activate the exact Chromium target represented by ``page``.

        ``page.bring_to_front()`` is normally sufficient, but on Windows a
        visible Chrome window can contain several tabs and the OS foreground
        window does not guarantee that the Playwright target is the selected
        tab.  CDP Target.activateTarget removes that ambiguity.
        """
        if self._context is None:
            return False

        try:
            if page.is_closed() or page not in self._context.pages:
                return False

            page_session = await self._context.new_cdp_session(page)
            try:
                info = await page_session.send("Target.getTargetInfo")
                target_id = (info.get("targetInfo") or {}).get("targetId")
                if not target_id:
                    return False
            finally:
                try:
                    await page_session.detach()
                except Exception:
                    pass

            if self._browser is not None:
                browser_session = await self._browser.new_browser_cdp_session()
                try:
                    await browser_session.send(
                        "Target.activateTarget",
                        {"targetId": target_id},
                    )
                finally:
                    try:
                        await browser_session.detach()
                    except Exception:
                        pass

            return True
        except Exception as exc:
            logger.debug("CDP tab activation failed: %s", exc)
            return False

    async def _close_unowned_task_tabs(self, keep: Page) -> None:
        """Remove stale pages from the dedicated automation context.

        NEXA launches a private Chrome profile, so there is no reason to keep
        old task tabs around.  Keeping exactly one controlled page prevents a
        stale page such as an old MDN CSS tab from remaining visible while the
        orchestrator operates on a different page object.
        """
        if self._context is None:
            return

        for candidate in list(self._context.pages):
            if candidate is keep:
                continue
            try:
                if not candidate.is_closed():
                    await candidate.close(run_before_unload=False)
            except Exception:
                pass

    async def new_task_page(
        self,
        *,
        fresh_tab: bool = False,
    ) -> tuple[BrowserContext, Page]:
        """Return the single NEXA-controlled page and make it foreground.

        The previous implementation created a fresh tab for every task.  That
        made it possible for Chrome to visibly remain on an old tab even while
        Playwright was controlling the newly-created one.  NEXA now uses one
        task tab inside its dedicated Chrome profile and explicitly activates
        that exact Chromium target before returning it.
        """

        await self.start()

        assert self._context is not None

        pages = [p for p in self._context.pages if not p.is_closed()]

        page: Page | None = None

        current = self.current_page
        if not fresh_tab and current is not None and current in pages:
            page = current
        elif not fresh_tab and pages:
            # The dedicated profile should normally contain only the initial
            # about:blank page. Reuse the first live page if the current
            # reference was lost.
            page = pages[0]
        else:
            try:
                page = await self._context.new_page()
            except Exception as exc:
                logger.warning(
                    "Chrome context became unavailable; recreating it: %s",
                    exc,
                )
                await self._stop_internal()
                await self.start()
                assert self._context is not None
                page = await self._context.new_page()

        page.set_default_timeout(settings.BROWSER_ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(settings.BROWSER_NAV_TIMEOUT_MS)

        self._current_page = page
        tp = getattr(self, "_task_pages", None)
        if tp is None:
            tp = self._task_pages = []
        if page not in tp:
            tp.append(page)
            del tp[:-20]

        # Remove stale pages before activating the controlled page.  This is
        # intentionally done in the dedicated NEXA Chrome profile only.
        await self._close_unowned_task_tabs(page)

        focused = await self.focus_current_page()
        if not focused:
            raise RuntimeError(
                "NEXA created/selected its controlled Chrome tab but could "
                "not bring that exact tab to the foreground."
            )

        logger.info(
            "NEXA controlled tab ready: url=%s title=%s",
            page.url,
            await self._safe_page_title(page),
        )

        return self._context, page

    @staticmethod
    async def _safe_page_title(page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Window focusing
    # ------------------------------------------------------------------

    async def _prepare_window(
        self,
        page: Page,
    ) -> bool:
        # First select the exact Chromium tab.  This is the critical part of
        # keeping the visible Chrome tab and Playwright page synchronized.
        await self._activate_page_via_cdp(page)

        try:
            await page.bring_to_front()
        except Exception:
            pass

        title = ""
        try:
            title = await page.title()
        except Exception:
            pass

        hwnds: list[int] = []

        # The dedicated Chrome process tree is the strongest Windows signal.
        if self._chrome_process is not None:
            try:
                pid = self._chrome_process.pid
                if pid is not None:
                    hwnds.extend(windows_for_process(pid))
            except Exception:
                pass

        # Only fall back to title matching if the process-tree lookup failed.
        if not hwnds:
            try:
                hwnd = find_chrome_window(
                    title,
                    self._known_chrome_hwnds,
                )
                if hwnd:
                    hwnds.append(hwnd)
            except Exception:
                pass

        for hwnd in hwnds:
            self._chrome_hwnd = hwnd
            try:
                if activate_chrome_window(hwnd):
                    return True
            except Exception:
                pass

        return False


    async def adopt_page(self, page: Page) -> bool:
        """Make an existing popup/new tab the NEXA-controlled page."""
        if self._context is None or page is None:
            return False
        try:
            if page.is_closed() or page not in self._context.pages:
                return False
            page.set_default_timeout(settings.BROWSER_ACTION_TIMEOUT_MS)
            page.set_default_navigation_timeout(settings.BROWSER_NAV_TIMEOUT_MS)
            self._current_page = page
            tp = getattr(self, "_task_pages", None)
            if tp is None:
                tp = self._task_pages = []
            if page not in tp:
                tp.append(page)
                del tp[:-20]
            await self.focus_current_page()
            logger.info("NEXA adopted new browser page: url=%s title=%s", page.url, await self._safe_page_title(page))
            return True
        except Exception as exc:
            logger.debug("Could not adopt browser page: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Generic new-tab / popup detection and adoption
    #
    # One mechanism, used by the executor after any click:
    #   snapshot pages -> act -> diff context.pages -> adopt the new page.
    # Every adoption is appended to ``adoption_log`` so the orchestrator can
    # (a) switch ITS local Page reference and (b) emit
    # NEW_PAGE_DETECTED / PAGE_ADOPTED / ACTIVE_PAGE_CHANGED events.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Page-lifecycle API (the ONE place that manipulates Playwright pages)
    #   get_all_pages / get_active_page / set_active_page / detect+adopt new pages
    #   wait_until_ready / page_identity / page_state_signature
    #   close_page / switch_page / history_back / history_forward / open_new_tab
    # ------------------------------------------------------------------

    def get_all_pages(self) -> list[Page]:
        return self.pages_snapshot()

    def get_active_page(self) -> Page | None:
        return self.current_page

    def detect_new_pages(self, before: list[Page]) -> list[Page]:
        """Pages that exist now but did not exist in the ``before`` snapshot (oldest first)."""
        known = {id(p) for p in before}
        return [p for p in self.pages_snapshot() if id(p) not in known]

    async def set_active_page(self, page: Page) -> bool:
        return await self.adopt_page(page)

    @staticmethod
    def page_identity(page: Page | None) -> int:
        """Stable small integer per Page object for the life of the process (0 = no page)."""
        if page is None:
            return 0
        pid = getattr(page, "_nexa_page_id", None)
        if pid is None:
            BrowserManager._id_counter = getattr(BrowserManager, "_id_counter", 0) + 1
            pid = BrowserManager._id_counter
            try:
                page._nexa_page_id = pid  # type: ignore[attr-defined]
            except Exception:
                return id(page) % 1_000_000
        return pid

    async def wait_until_ready(self, page: Page, timeout_ms: int = 4000) -> None:
        """Wait (bounded) until the document is interactive/complete and its text stopped changing.

        Polls ``document.readyState`` instead of ``wait_for_load_state``: after a history
        navigation restored from the back/forward cache Playwright never re-fires the load-state
        event, so wait_for_load_state sat for its whole timeout (measured: 4.2 s per back/forward).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.5, timeout_ms / 1000.0)
        last = None
        stable = 0
        while loop.time() < deadline:
            try:
                sig = await page.evaluate("[document.readyState, location.href, (document.body && document.body.innerText || '').length]")
            except Exception:
                await asyncio.sleep(0.15)       # mid-navigation: execution context not ready yet
                continue
            if sig == last and sig[0] in ("interactive", "complete"):
                stable += 1
                if stable >= 1:
                    return
            else:
                stable = 0
            last = sig
            await asyncio.sleep(0.15)

    async def page_state_signature(self, page: Page | None) -> dict:
        """URL, title, page id/count, scroll, focus, input values, text hash - for no-progress detection."""
        if page is None:
            return {}
        try:
            data = await page.evaluate(
                """() => {
                  const a = document.activeElement;
                  const inputs = Array.from(document.querySelectorAll('input,textarea,[contenteditable]'))
                    .slice(0, 12).map(e => (e.value !== undefined && e.value !== '' ? e.value : (e.innerText || '')).slice(0, 80));
                  const t = (document.body && document.body.innerText) || '';
                  let h = 0; for (let i = 0; i < Math.min(t.length, 4000); i++) h = (h * 31 + t.charCodeAt(i)) | 0;
                  return {scroll_y: Math.round(window.scrollY), scroll_height: document.documentElement.scrollHeight,
                          inner_height: window.innerHeight,
                          focus: a ? (a.tagName.toLowerCase() + '#' + (a.id || '') + '.' + (a.getAttribute('name') || '')) : '',
                          inputs, text_hash: h};
                }"""
            )
        except Exception:
            data = {}
        data.update({"url": page.url, "title": await self._safe_page_title(page),
                     "page_id": self.page_identity(page), "page_count": len(self.pages_snapshot())})
        return data

    async def history_back(self, page: Page, timeout_ms: int = 6000) -> dict:
        return await self._history_move(page, "back", timeout_ms)

    async def history_forward(self, page: Page, timeout_ms: int = 6000) -> dict:
        return await self._history_move(page, "forward", timeout_ms)

    async def _history_move(self, page: Page, direction: str, timeout_ms: int) -> dict:
        """REAL browser history navigation (never a guessed URL)."""
        before = page.url
        loop = asyncio.get_running_loop()
        _t0 = loop.time()
        try:
            if direction == "back":
                await page.go_back(wait_until="commit", timeout=timeout_ms)
            else:
                await page.go_forward(wait_until="commit", timeout=timeout_ms)
        except Exception as exc:
            logger.debug("page.go_%s raised: %s", direction, exc)
        if page.url == before:
            # same-document history entries (hash / pushState) make go_back return None: use the history API
            try:
                await page.evaluate("history.back()" if direction == "back" else "history.forward()")
            except Exception:
                pass
            deadline = loop.time() + 2.5
            while loop.time() < deadline and page.url == before:
                await asyncio.sleep(0.1)
        _t1 = loop.time()
        await self.wait_until_ready(page)
        logger.info("history_%s: navigate=%.2fs ready=%.2fs %s -> %s", direction, _t1 - _t0, loop.time() - _t1, before, page.url)
        return {"before_url": before, "after_url": page.url, "moved": page.url != before}

    async def open_new_tab(self, url: str | None, nav_timeout_ms: int | None = None) -> Page | None:
        """Open a NEW tab in the controlled context, load ``url`` and make it the active page."""
        if self._context is None:
            return None
        origin = self.current_page
        page = await self._context.new_page()
        page.set_default_timeout(settings.BROWSER_ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(settings.BROWSER_NAV_TIMEOUT_MS)
        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms or settings.BROWSER_NAV_TIMEOUT_MS)
            except Exception as exc:
                logger.warning("New tab navigation to %s raised: %s", url, exc)
        old_url = origin.url if origin is not None else None
        if not await self.adopt_page(page):
            return None
        self.adoption_log.append({"action": "new_tab", "from_url": old_url, "to_url": page.url,
                                  "title": await self._safe_page_title(page), "page_count": len(self.pages_snapshot())})
        return page

    async def switch_page(self, which: str = "other") -> Page | None:
        """Activate another open tab: new/newest/last, first/original, previous, other/next."""
        pages = self.pages_snapshot()
        cur = self.current_page
        if len(pages) < 2 or cur is None:
            return None
        idx = pages.index(cur) if cur in pages else len(pages) - 1
        which = (which or "other").lower()
        if which in {"new", "newest", "last"}:
            target = pages[-1]
        elif which in {"first", "original", "old", "earlier"}:
            target = pages[0]
        elif which == "previous":
            target = pages[idx - 1] if idx > 0 else pages[-1]
        elif which == "second":
            target = pages[1]
        else:  # other / next
            target = pages[(idx + 1) % len(pages)]
        if target is cur:
            target = pages[(idx + 1) % len(pages)]
        old_url = cur.url
        if not await self.adopt_page(target):
            return None
        self.adoption_log.append({"action": "switch_tab", "from_url": old_url, "to_url": target.url,
                                  "title": await self._safe_page_title(target), "page_count": len(pages)})
        return target

    async def close_page(self, page: Page | None = None) -> Page | None:
        """Close a tab and activate the most recent remaining one."""
        page = page or self.current_page
        if page is None:
            return None
        pages = self.pages_snapshot()
        if len(pages) < 2:
            return None          # never close the last tab: the user keeps a live Chrome window
        idx = pages.index(page) if page in pages else len(pages) - 1
        survivor = pages[idx - 1] if idx > 0 else pages[1]
        old_url = page.url
        try:
            await page.close()
        except Exception as exc:
            logger.debug("close failed: %s", exc)
        if not await self.adopt_page(survivor):
            return None
        self.adoption_log.append({"action": "close_tab", "from_url": old_url, "to_url": survivor.url,
                                  "title": await self._safe_page_title(survivor), "page_count": len(self.pages_snapshot())})
        return survivor

    def _attach_context_listeners(self) -> None:
        """Remember every page the context creates (popups can appear later than the
        click that caused them). Adoption of such late popups is decided in
        ``adopt_late_popups`` - only pages whose opener is a page NEXA controls."""
        ctx = self._context
        if ctx is None or getattr(ctx, "_nexa_listener", False):
            return
        self._created_pages = []

        def on_page(page: Page) -> None:
            self._created_pages.append(page)
            del self._created_pages[:-50]

        try:
            ctx.on("page", on_page)
            ctx._nexa_listener = True  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("Could not attach context page listener: %s", exc)

    async def adopt_late_popups(self, current: Page | None) -> Page | None:
        """Adopt a popup/new tab opened by the controlled page AFTER the click returned.

        Safe by construction: only pages whose ``opener`` is the page NEXA is
        controlling (or one NEXA already adopted) are adopted; unrelated tabs the
        user opened themselves are left alone.
        """
        if self._context is None or current is None:
            return None
        known_ok = {id(current)}
        for candidate in list(getattr(self, "_created_pages", []))[::-1]:
            try:
                if candidate.is_closed() or candidate is current or id(candidate) in getattr(self, "_seen_pages", set()):
                    continue
                opener = await candidate.opener()
            except Exception:
                continue
            if opener is not None and id(opener) in known_ok | {id(p) for p in getattr(self, "_task_pages", [])}:
                seen = getattr(self, "_seen_pages", None)
                if seen is None:
                    seen = self._seen_pages = set()
                seen.add(id(candidate))
                try:
                    await candidate.wait_for_load_state("domcontentloaded", timeout=4000)
                except Exception:
                    pass
                old_url = current.url
                if await self.adopt_page(candidate):
                    self.adoption_log.append({
                        "action": "late_popup", "from_url": old_url, "to_url": candidate.url,
                        "title": await self._safe_page_title(candidate),
                        "page_count": len(self.pages_snapshot()),
                    })
                    return candidate
        return None

    def pages_snapshot(self) -> list[Page]:
        if self._context is None:
            return []
        try:
            return [p for p in self._context.pages if not p.is_closed()]
        except Exception:
            return []

    @property
    def adoption_log(self) -> list[dict]:
        log = getattr(self, "_adoption_log", None)
        if log is None:
            log = self._adoption_log = []
        return log

    def drain_adoptions(self) -> list[dict]:
        out = list(self.adoption_log)
        self.adoption_log.clear()
        return out

    async def adopt_new_pages(
        self,
        before: list[Page],
        origin: Page | None = None,
        wait_ms: int | None = None,
        action: str = "click",
    ) -> Page | None:
        """Detect a page created since ``before`` and adopt it.

        Waits at most ``wait_ms`` (default BROWSER_NEW_PAGE_WAIT_MS), returning
        early as soon as a new page appears or the origin page itself navigated
        (same-tab navigation needs no adoption).
        """
        if self._context is None:
            return None
        wait_ms = settings.BROWSER_NEW_PAGE_WAIT_MS if wait_ms is None else wait_ms
        origin_url = None
        try:
            origin_url = origin.url if origin is not None else None
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, wait_ms) / 1000.0
        known = {id(p) for p in before}
        new_page: Page | None = None
        while True:
            fresh = [p for p in self.pages_snapshot() if id(p) not in known]
            if fresh:
                new_page = fresh[-1]
                break
            try:
                if origin is not None and origin.url != origin_url:
                    return None          # same-tab navigation
            except Exception:
                pass
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.1)

        old_url = origin_url
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        if not await self.adopt_page(new_page):
            return None
        try:
            title = await self._safe_page_title(new_page)
        except Exception:
            title = ""
        self.adoption_log.append({
            "action": action,
            "from_url": old_url,
            "to_url": new_page.url,
            "title": title,
            "page_count": len(self.pages_snapshot()),
        })
        return new_page

    async def adopt_newest_page(self, previous: Page | None = None) -> Page | None:
        """Adopt the newest open page when a click created a popup/tab."""
        if self._context is None:
            return None
        pages = [p for p in self._context.pages if not p.is_closed()]
        candidates = [p for p in pages if p is not previous]
        if not candidates:
            return None
        page = candidates[-1]
        return page if await self.adopt_page(page) else None

    async def focus_current_page(self) -> bool:
        """Bring the controlled tab to the foreground - bounded.

        The CDP/Playwright calls inside can block for as long as a hung
        navigation keeps the tab busy (measured ~70s after a navigation
        timeout), which used to delay the failure report far past every
        configured timeout. The whole operation is now capped.
        """
        try:
            return await asyncio.wait_for(self._focus_current_page_inner(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("focus_current_page exceeded 8s; continuing without foreground confirmation.")
            return False

    async def _focus_current_page_inner(self) -> bool:
        page = self.current_page
        if page is None:
            return False

        for attempt in range(12):
            try:
                if page.is_closed():
                    self._current_page = None
                    return False
                if self._context is None or page not in self._context.pages:
                    self._current_page = None
                    return False
            except Exception:
                pass

            await self._activate_page_via_cdp(page)

            try:
                await page.bring_to_front()
            except Exception:
                pass

            if await self._prepare_window(page):
                # Give Chrome one event-loop turn to commit the active tab.
                await asyncio.sleep(0.08)
                return True

            await asyncio.sleep(0.12)

        logger.warning("Unable to focus NEXA controlled Chrome tab after retries.")
        return False


browser_manager = BrowserManager()