"""Stealth Playwright browser with anti-detection and session persistence."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from src.config import AppConfig, PROJECT_ROOT

logger = logging.getLogger(__name__)


@asynccontextmanager
async def create_stealth_context(
    config: AppConfig,
    portal_name: str,
) -> AsyncGenerator:  # yields tuple[Browser, BrowserContext, Page]
    """Create a stealth browser context with session persistence.

    Usage:
        async with create_stealth_context(config, "naukri") as (browser, context, page):
            await page.goto("https://naukri.com")
    """
    state_dir = PROJECT_ROOT / config.browser.state_dir / portal_name
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "state.json"

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=config.browser.headless,
        slow_mo=config.browser.slow_mo,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context_kwargs: dict = {
        "viewport": {"width": 1366, "height": 768},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
        "timezone_id": "Asia/Kolkata",
    }

    # Restore session if exists
    if state_file.exists():
        context_kwargs["storage_state"] = str(state_file)
        logger.info("Restoring session for %s", portal_name)

    context = await browser.new_context(**context_kwargs)

    # Anti-detection scripts
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
    """)

    page = await context.new_page()
    page.set_default_timeout(config.browser.timeout)

    try:
        yield browser, context, page
    finally:
        # Save session state
        try:
            await context.storage_state(path=str(state_file))
            logger.info("Saved session for %s", portal_name)
        except Exception as e:
            logger.warning("Failed to save session for %s: %s", portal_name, e)
        await context.close()
        await browser.close()
        await pw.stop()


async def take_screenshot(page: Page, portal_name: str, job_id: str) -> str:
    """Take a screenshot and return the file path."""
    screenshots_dir = PROJECT_ROOT / "data" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    path = screenshots_dir / f"{portal_name}_{job_id}.png"
    await page.screenshot(path=str(path), full_page=False)
    logger.info("Screenshot saved: %s", path)
    return str(path)
