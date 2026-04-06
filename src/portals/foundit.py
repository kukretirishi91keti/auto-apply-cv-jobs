"""Foundit (formerly Monster India) portal scraper."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from playwright.async_api import Page

from src.config import AppConfig, Credentials
from src.portals.base import BasePortal, JobListing
from src.utils.browser import create_stealth_context, take_screenshot
from src.utils.rate_limiter import medium_pause, short_pause

logger = logging.getLogger(__name__)


class FounditPortal(BasePortal):
    name = "foundit"
    auto_apply_supported = True

    BASE_URL = "https://www.foundit.in"

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
            self.logger.error("Foundit credentials not configured")
            return False

        try:
            await page.goto(f"{self.BASE_URL}/seeker/login")
            await medium_pause()
            await page.fill('input[name="email"], input[type="email"]', email)
            await short_pause()
            await page.fill('input[name="password"], input[type="password"]', password)
            await short_pause()
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            self.logger.info("Foundit login attempted")
            return "login" not in page.url
        except Exception as e:
            self.logger.error("Foundit login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        page = await self._ensure_browser()
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/middleware/jobsearch?searchId=&query={quote_plus(keywords)}&locations={quote_plus(location)}"

        try:
            await page.goto(f"{self.BASE_URL}/srp/results?searchId=&query={quote_plus(keywords)}&locations={quote_plus(location)}")
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            job_cards = await page.query_selector_all("div.card-apply-content, div.jobTuple")
            self.logger.info("Found %d job cards on Foundit", len(job_cards))

            for card in job_cards[:25]:
                try:
                    title_el = await card.query_selector("a.card-title, a.title")
                    company_el = await card.query_selector("span.company-name, a.company-name")
                    location_el = await card.query_selector("span.loc, span.location-text")
                    salary_el = await card.query_selector("span.salary, span.sal")

                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    url = await title_el.get_attribute("href") or ""
                    if url.startswith("/"):
                        url = f"{self.BASE_URL}{url}"
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    loc = (await location_el.inner_text()).strip() if location_el else ""
                    salary = (await salary_el.inner_text()).strip() if salary_el else ""
                    ext_id = url.split("/")[-1][:20] if url else title[:20]

                    jobs.append(JobListing(
                        portal=self.name,
                        external_id=ext_id,
                        title=title,
                        company=company,
                        location=loc,
                        url=url,
                        salary=salary,
                    ))
                except Exception as e:
                    self.logger.debug("Error parsing Foundit card: %s", e)
        except Exception as e:
            self.logger.error("Foundit search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        page = await self._ensure_browser()

        try:
            await page.goto(job.url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            desc_el = await page.query_selector("div.job-desc, div.job-description")
            if desc_el:
                job.description = (await desc_el.inner_text()).strip()

            apply_btn = await page.query_selector(
                'button:has-text("Apply"), button:has-text("Apply Now")'
            )
            if not apply_btn:
                self.logger.warning("No apply button for Foundit job: %s", job.title)
                return False

            await apply_btn.click()
            await medium_pause()

            if self.config.apply.save_screenshots:
                await take_screenshot(page, self.name, job.external_id)

            self.logger.info("Applied on Foundit: %s at %s", job.title, job.company)
            return True
        except Exception as e:
            self.logger.error("Foundit apply error for %s: %s", job.title, e)
            return False

    async def health_check(self) -> bool:
        page = await self._ensure_browser()
        try:
            await page.goto(self.BASE_URL)
            await page.wait_for_load_state("networkidle")
            search_box = await page.query_selector('input[name="query"], input.keyword')
            return search_box is not None
        except Exception:
            return False
