"""Indeed portal scraper with auto-apply support."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

if TYPE_CHECKING:
    from playwright.async_api import Page

from src.config import AppConfig, Credentials
from src.portals.base import BasePortal, JobListing
from src.utils.browser import create_stealth_context, take_screenshot
from src.utils.rate_limiter import medium_pause, short_pause

logger = logging.getLogger(__name__)


class IndeedPortal(BasePortal):
    name = "indeed"
    auto_apply_supported = True

    BASE_URL = "https://in.indeed.com"

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
            self.logger.error("Indeed credentials not configured")
            return False

        try:
            await page.goto(f"{self.BASE_URL}/account/login")
            await medium_pause()
            await page.fill('input[name="__email"], input#login-email-input', email)
            await short_pause()

            submit = await page.query_selector('button[type="submit"]')
            if submit:
                await submit.click()
            await medium_pause()

            password_field = await page.query_selector('input[type="password"]')
            if password_field:
                await password_field.fill(password)
                await short_pause()
                submit = await page.query_selector('button[type="submit"]')
                if submit:
                    await submit.click()
                await page.wait_for_load_state("networkidle")
                await medium_pause()

            self.logger.info("Indeed login attempted")
            return True
        except Exception as e:
            self.logger.error("Indeed login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        page = await self._ensure_browser()
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/jobs?q={quote_plus(keywords)}&l={quote_plus(location)}"

        try:
            await page.goto(search_url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            # Wait for results container to render
            try:
                await page.wait_for_selector(
                    'div#mosaic-provider-jobcards, div.jobsearch-ResultsList, div[id*="mosaic"]',
                    timeout=10000,
                )
            except Exception:
                self.logger.warning("Indeed results container not found, trying anyway")

            job_cards = await page.query_selector_all(
                'div.job_seen_beacon, li[data-testid="slider_item"], div.cardOutline, td.resultContent'
            )
            self.logger.info("Found %d job cards on Indeed (Playwright)", len(job_cards))

            for card in job_cards[:25]:
                try:
                    title_el = await card.query_selector(
                        'a[data-testid="jobTitle"], h2.jobTitle a, a.jcs-JobTitle'
                    )
                    company_el = await card.query_selector(
                        "span[data-testid='company-name'], span.companyName"
                    )
                    location_el = await card.query_selector(
                        "div[data-testid='text-location'], div.companyLocation"
                    )
                    salary_el = await card.query_selector(
                        "[data-testid='salaryRange'], div.salary-snippet-container"
                    )

                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    url_path = await title_el.get_attribute("href") or ""
                    url = f"{self.BASE_URL}{url_path}" if url_path.startswith("/") else url_path
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    loc = (await location_el.inner_text()).strip() if location_el else ""
                    salary = (await salary_el.inner_text()).strip() if salary_el else ""

                    ext_id = url_path.split("jk=")[-1][:16] if "jk=" in url_path else title[:20]

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
                    self.logger.debug("Error parsing Indeed job card: %s", e)

            # BeautifulSoup fallback if Playwright selectors missed everything
            if not jobs:
                self.logger.info("Playwright selectors found 0 jobs, trying BeautifulSoup fallback")
                jobs = await self._bs4_fallback(page)

        except Exception as e:
            self.logger.error("Indeed search error: %s", e)

        return jobs

    async def _bs4_fallback(self, page: Page) -> list[JobListing]:
        """Parse job cards from raw HTML using BeautifulSoup."""
        from bs4 import BeautifulSoup

        jobs: list[JobListing] = []
        try:
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Try data-testid based selectors first
            title_links = soup.select('[data-testid="jobTitle"]')
            if not title_links:
                title_links = soup.select("h2.jobTitle a, a.jcs-JobTitle")

            self.logger.info("BeautifulSoup found %d title elements", len(title_links))

            for el in title_links[:25]:
                title = el.get_text(strip=True)
                url_path = el.get("href", "")
                url = f"{self.BASE_URL}{url_path}" if url_path.startswith("/") else url_path

                parent = el.find_parent("td") or el.find_parent("div", class_=True)
                company_el = parent.select_one('[data-testid="company-name"], span.companyName') if parent else None
                location_el = parent.select_one('[data-testid="text-location"], div.companyLocation') if parent else None
                company = company_el.get_text(strip=True) if company_el else "Unknown"
                loc = location_el.get_text(strip=True) if location_el else ""

                ext_id = url_path.split("jk=")[-1][:16] if "jk=" in url_path else title[:20]

                jobs.append(JobListing(
                    portal=self.name,
                    external_id=ext_id,
                    title=title,
                    company=company,
                    location=loc,
                    url=url,
                ))
        except Exception as e:
            self.logger.error("BeautifulSoup fallback error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        page = await self._ensure_browser()

        try:
            await page.goto(job.url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            desc_el = await page.query_selector("div#jobDescriptionText")
            if desc_el:
                job.description = (await desc_el.inner_text()).strip()

            apply_btn = await page.query_selector(
                'button#indeedApplyButton, button:has-text("Apply now"), '
                'a:has-text("Apply now")'
            )
            if not apply_btn:
                self.logger.warning("No apply button for Indeed job: %s", job.title)
                return False

            await apply_btn.click()
            await medium_pause()

            # Handle Indeed's apply flow (may open iframe)
            apply_frame = page.frame("indeedapply-modal-preload-1") or page
            resume_input = await apply_frame.query_selector('input[type="file"]')  # type: ignore[union-attr]
            if resume_input:
                await resume_input.set_input_files(cv_path)
                await short_pause()

            continue_btn = await apply_frame.query_selector('button:has-text("Continue"), button:has-text("Submit")')  # type: ignore[union-attr]
            if continue_btn:
                await continue_btn.click()
                await medium_pause()

            if self.config.apply.save_screenshots:
                await take_screenshot(page, self.name, job.external_id)

            self.logger.info("Applied on Indeed: %s at %s", job.title, job.company)
            return True
        except Exception as e:
            self.logger.error("Indeed apply error for %s: %s", job.title, e)
            return False

    async def health_check(self) -> bool:
        page = await self._ensure_browser()
        try:
            await page.goto(self.BASE_URL)
            await page.wait_for_load_state("networkidle")
            search_box = await page.query_selector('input#text-input-what')
            return search_box is not None
        except Exception:
            return False
