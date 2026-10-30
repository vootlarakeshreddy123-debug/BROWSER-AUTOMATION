"""
DownloadManager.

Thin wrapper around Playwright's download event so `executor.py` doesn't
need to know the plumbing. Downloads are saved under settings.DOWNLOAD_DIR.
"""
from __future__ import annotations

import os
import time

from playwright.async_api import Download, Page

from config import settings


async def save_download(page: Page, trigger_locator_click) -> dict:
    """`trigger_locator_click` is an async callable that performs the click
    (or other action) expected to start the download. Returns metadata
    about the saved file."""
    os.makedirs(settings.DOWNLOAD_DIR, exist_ok=True)

    async with page.expect_download(timeout=settings.BROWSER_NAV_TIMEOUT_MS) as dl_info:
        await trigger_locator_click()
    download: Download = await dl_info.value

    suggested = download.suggested_filename or f"download_{int(time.time())}"
    dest = os.path.join(settings.DOWNLOAD_DIR, suggested)
    await download.save_as(dest)

    return {"filename": suggested, "path": dest, "url": download.url}
