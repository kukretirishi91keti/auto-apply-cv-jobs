"""LinkedIn portal — scrape-only mode (auto-apply disabled due to bot detection)."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from playwright.async_api import Page

from src.config import AppConfig, Credentials
from src.portals.base import BasePortal, JobListing
from src.utils.browser import create_stealth_context
from src.utils.rate_limiter import medium_pause, short_pause

logger = logging.getLogger(__name__)


class LinkedInPortal(BasePortal):
    name = "linkedin"
    auto_apply_supported = False  # Scrape-only

    BASE_URL = "https://www.linkedin.com"

    def __init__(self, config: AppConfig, creds: Credentials):
        super().__init__(config, creds)
        self._page: Page | None = None
        self._ctx_manager = None

    async def _ensure_browser(self) -> Page:
        if self._page is None:
            self._ctx_manager = create_stealth_context(self.config, self.name)
            _, _, self._page = await self._ctx_manager.__aenter__()
        return self._page

    async def close(self) -> None:
        if self._ctx_manager:
            await self._ctx_manager.__aexit__(None, None, None)
            self._page = None

    async def login(self) -> bool:
        page = await self._ensure_browser()
        email = self.get_credential("email")
        password = self.get_credential("password")

        if not email or not password:
            self.logger.error("LinkedIn credentials not configured")
            return False

        try:
            await page.goto(f"{self.BASE_URL}/login")
            await medium_pause()
            await page.fill('input#username', email)
            await short_pause()
            await page.fill('input#password', password)
            await short_pause()
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            # Check for CAPTCHA/challenge
            if "challenge" in page.url or "checkpoint" in page.url:
                self.logger.warning("LinkedIn security challenge detected — manual intervention needed")
                return False

            self.logger.info("LinkedIn login attempted")
            return "feed" in page.url or "mynetwork" in page.url
        except Exception as e:
            self.logger.error("LinkedIn login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        page = await self._ensure_browser()
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/jobs/search/?keywords={quote_plus(keywords)}&location={quote_plus(location)}"

        try:
            await page.goto(search_url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            job_cards = await page.query_selector_all("div.job-card-container, li.jobs-search-results__list-item")
            self.logger.info("Found %d job cards on LinkedIn (scrape-only)", len(job_cards))

            for card in job_cards[:20]:
                try:
                    title_el = await card.query_selector("a.job-card-list__title, strong")
                    company_el = await card.query_selector("span.job-card-container__primary-description, a.job-card-container__company-name")
                    location_el = await card.query_selector("li.job-card-container__metadata-item")

                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    url_el = await card.query_selector("a[href*='/jobs/view/']")
                    url = (await url_el.get_attribute("href") or "") if url_el else ""
                    if url and not url.startswith("http"):
                        url = f"{self.BASE_URL}{url}"
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    loc = (await location_el.inner_text()).strip() if location_el else ""
                    ext_id = url.split("/view/")[-1].split("/")[0] if "/view/" in url else title[:20]

                    jobs.append(JobListing(
                        portal=self.name,
                        external_id=ext_id,
                        title=title,
                        company=company,
                        location=loc,
                        url=url,
                    ))
                except Exception as e:
                    self.logger.debug("Error parsing LinkedIn card: %s", e)
        except Exception as e:
            self.logger.error("LinkedIn search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        """LinkedIn auto-apply is disabled. Jobs are scrape-only."""
        self.logger.info(
            "LinkedIn scrape-only: %s at %s — open manually: %s",
            job.title, job.company, job.url,
        )
        return False

    async def health_check(self) -> bool:
        page = await self._ensure_browser()
        try:
            await page.goto(f"{self.BASE_URL}/jobs")
            await page.wait_for_load_state("networkidle")
            search_box = await page.query_selector('input[aria-label*="Search"]')
            return search_box is not None
        except Exception:
            return False
