"""Naukri.com portal scraper with full auto-apply support."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import httpx

if TYPE_CHECKING:
    from playwright.async_api import Page

from src.config import AppConfig, Credentials
from src.portals.base import BasePortal, JobListing

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "appid": "109",
    "systemid": "Naukri",
}


class NaukriPortal(BasePortal):
    name = "naukri"
    auto_apply_supported = True

    BASE_URL = "https://www.naukri.com"
    API_URL = "https://www.naukri.com/jobapi/v3/search"
    LOGIN_URL = "https://www.naukri.com/nlogin/login"

    def __init__(self, config: AppConfig, creds: Credentials):
        super().__init__(config, creds)
        self._page: Page | None = None
        self._browser = None
        self._context = None
        self._ctx_manager = None

    async def _ensure_browser(self) -> Page:
        from src.utils.browser import create_stealth_context
        if self._page is None:
            self._ctx_manager = create_stealth_context(self.config, self.name)
            self._browser, self._context, self._page = await self._ctx_manager.__aenter__()
        return self._page

    async def close(self) -> None:
        if self._ctx_manager:
            await self._ctx_manager.__aexit__(None, None, None)
            self._page = None

    async def login(self) -> bool:
        from src.utils.rate_limiter import medium_pause, short_pause
        page = await self._ensure_browser()
        email = self.get_credential("email")
        password = self.get_credential("password")

        if not email or not password:
            self.logger.error("Naukri credentials not configured")
            return False

        try:
            await page.goto(self.LOGIN_URL)
            await medium_pause()

            await page.fill('input[placeholder*="Email"]', email)
            await short_pause()
            await page.fill('input[placeholder*="Password"], input[type="password"]', password)
            await short_pause()
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            if "nlogin" not in page.url:
                self.logger.info("Naukri login successful")
                return True

            self.logger.error("Naukri login failed — still on login page")
            return False
        except Exception as e:
            self.logger.error("Naukri login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        """Search using Naukri's public JSON API — no browser needed."""
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        params = {
            "noOfResults": 30,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": keywords,
            "location": location,
            "pageNo": 1,
        }
        if self.config.search.experience_years:
            params["experience"] = self.config.search.experience_years

        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
                resp = await client.get(self.API_URL, params=params)
                resp.raise_for_status()
                data = resp.json()

            job_details = data.get("jobDetails", [])
            self.logger.info("Naukri API returned %d jobs", len(job_details))

            for item in job_details:
                try:
                    title = item.get("title", "").strip()
                    company = item.get("companyName", "Unknown").strip()
                    loc = item.get("placeholders", [{}])[0].get("label", "") if item.get("placeholders") else ""
                    salary = item.get("placeholders", [{}])[1].get("label", "") if len(item.get("placeholders", [])) > 1 else ""
                    jd_url = item.get("jdURL", "")
                    url = f"{self.BASE_URL}{jd_url}" if jd_url.startswith("/") else jd_url
                    ext_id = item.get("jobId", "") or str(hash(title + company))[:12]

                    if not title:
                        continue

                    jobs.append(JobListing(
                        portal=self.name,
                        external_id=str(ext_id),
                        title=title,
                        company=company,
                        location=loc,
                        url=url,
                        salary=salary,
                        description=item.get("jobDescription", ""),
                    ))
                except Exception as e:
                    self.logger.debug("Error parsing Naukri API job: %s", e)

        except Exception as e:
            self.logger.error("Naukri API search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        from src.utils.browser import take_screenshot
        from src.utils.rate_limiter import human_delay, medium_pause, short_pause
        page = await self._ensure_browser()

        try:
            await page.goto(job.url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            desc_el = await page.query_selector("div.job-desc, section.job-desc")
            if desc_el:
                job.description = (await desc_el.inner_text()).strip()

            apply_btn = await page.query_selector(
                'button:has-text("Apply"), a:has-text("Apply on company site"), '
                'button:has-text("Apply Now"), button.apply-button'
            )
            if not apply_btn:
                self.logger.warning("No apply button found for: %s", job.title)
                return False

            await apply_btn.click()
            await human_delay(2, 4)

            chatbot = await page.query_selector("div.chatbot_container")
            if chatbot:
                self.logger.info("Chatbot detected, attempting to fill...")
                await self._handle_chatbot(page)

            success = await page.query_selector(
                'div:has-text("applied successfully"), '
                'div:has-text("Application Submitted")'
            )

            if success or "applied" in (await page.content()).lower():
                if self.config.apply.save_screenshots:
                    await take_screenshot(page, self.name, job.external_id)
                self.logger.info("Applied to: %s at %s", job.title, job.company)
                return True

            self.logger.warning("Uncertain if application succeeded for: %s", job.title)
            if self.config.apply.save_screenshots:
                await take_screenshot(page, self.name, f"{job.external_id}_uncertain")
            return True
        except Exception as e:
            self.logger.error("Apply error for %s: %s", job.title, e)
            return False

    async def _handle_chatbot(self, page: Page) -> None:
        from src.utils.rate_limiter import human_delay, short_pause
        for _ in range(5):
            await human_delay(1, 2)
            options = await page.query_selector_all("div.chatbot_container button, div.chatbot_container input")
            if not options:
                break
            for opt in options:
                try:
                    await opt.click()
                    await short_pause()
                except Exception:
                    continue

    async def health_check(self) -> bool:
        """Quick check via HTTP — no browser needed."""
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
                resp = await client.get(self.API_URL, params={"noOfResults": 1, "keyword": "test"})
                return resp.status_code == 200
        except Exception as e:
            self.logger.error("Naukri health check failed: %s", e)
            return False
