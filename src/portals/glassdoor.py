"""Glassdoor portal — scrape-only mode (auto-apply disabled due to bot detection)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

if TYPE_CHECKING:
    from playwright.async_api import Page

from src.config import AppConfig, Credentials
from src.portals.base import BasePortal, JobListing
from src.utils.browser import create_stealth_context
from src.utils.rate_limiter import medium_pause, short_pause

logger = logging.getLogger(__name__)


class GlassdoorPortal(BasePortal):
    name = "glassdoor"
    auto_apply_supported = False  # Scrape-only

    BASE_URL = "https://www.glassdoor.co.in"

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
            self.logger.error("Glassdoor credentials not configured")
            return False

        try:
            await page.goto(f"{self.BASE_URL}/profile/login_input.htm")
            await medium_pause()
            await page.fill('input#inlineUserEmail, input[name="username"]', email)
            await short_pause()

            continue_btn = await page.query_selector('button:has-text("Continue"), button[type="submit"]')
            if continue_btn:
                await continue_btn.click()
                await medium_pause()

            password_field = await page.query_selector('input#inlineUserPassword, input[type="password"]')
            if password_field:
                await password_field.fill(password)
                await short_pause()
                submit = await page.query_selector('button:has-text("Sign In"), button[type="submit"]')
                if submit:
                    await submit.click()
                await page.wait_for_load_state("networkidle")
                await medium_pause()

            self.logger.info("Glassdoor login attempted")
            return "login" not in page.url
        except Exception as e:
            self.logger.error("Glassdoor login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        page = await self._ensure_browser()
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/Job/jobs.htm?sc.keyword={quote_plus(keywords)}&locT=C&locKeyword={quote_plus(location)}"

        try:
            await page.goto(search_url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            job_cards = await page.query_selector_all("li.react-job-listing, li[data-test='jobListing']")
            self.logger.info("Found %d job cards on Glassdoor (scrape-only)", len(job_cards))

            for card in job_cards[:20]:
                try:
                    title_el = await card.query_selector("a[data-test='job-link'], a.jobTitle")
                    company_el = await card.query_selector("div.employer-name, span.EmployerProfile_compactEmployerName")
                    location_el = await card.query_selector("span.loc, div[data-test='emp-location']")
                    salary_el = await card.query_selector("span[data-test='detailSalary'], span.salary-estimate")

                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    url = await title_el.get_attribute("href") or ""
                    if url.startswith("/"):
                        url = f"{self.BASE_URL}{url}"
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    loc = (await location_el.inner_text()).strip() if location_el else ""
                    salary = (await salary_el.inner_text()).strip() if salary_el else ""
                    ext_id = url.split("jl=")[-1][:20] if "jl=" in url else title[:20]

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
                    self.logger.debug("Error parsing Glassdoor card: %s", e)
        except Exception as e:
            self.logger.error("Glassdoor search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        """Glassdoor auto-apply is disabled. Jobs are scrape-only."""
        self.logger.info(
            "Glassdoor scrape-only: %s at %s — open manually: %s",
            job.title, job.company, job.url,
        )
        return False

    async def health_check(self) -> bool:
        page = await self._ensure_browser()
        try:
            await page.goto(self.BASE_URL)
            await page.wait_for_load_state("networkidle")
            search_box = await page.query_selector('input[data-test="search-bar-keyword-input"], input#KeywordSearch')
            return search_box is not None
        except Exception:
            return False
