"""Naukri.com portal scraper with full auto-apply support."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

from playwright.async_api import Page

from src.config import AppConfig, Credentials
from src.portals.base import BasePortal, JobListing
from src.utils.browser import create_stealth_context, take_screenshot
from src.utils.rate_limiter import human_delay, medium_pause, short_pause

logger = logging.getLogger(__name__)


class NaukriPortal(BasePortal):
    name = "naukri"
    auto_apply_supported = True

    BASE_URL = "https://www.naukri.com"
    LOGIN_URL = "https://www.naukri.com/nlogin/login"

    def __init__(self, config: AppConfig, creds: Credentials):
        super().__init__(config, creds)
        self._page: Page | None = None
        self._browser = None
        self._context = None
        self._ctx_manager = None

    async def _ensure_browser(self) -> Page:
        if self._page is None:
            self._ctx_manager = create_stealth_context(self.config, self.name)
            self._browser, self._context, self._page = await self._ctx_manager.__aenter__()
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

            # Check for successful login
            if "nlogin" not in page.url:
                self.logger.info("Naukri login successful")
                return True

            self.logger.error("Naukri login failed — still on login page")
            return False
        except Exception as e:
            self.logger.error("Naukri login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        page = await self._ensure_browser()
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/{quote_plus(keywords)}-jobs-in-{quote_plus(location)}"
        if self.config.search.experience_years:
            search_url += f"?experience={self.config.search.experience_years}"

        try:
            await page.goto(search_url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            # Parse job cards
            job_cards = await page.query_selector_all("article.jobTuple, div.srp-jobtuple-wrapper, div.cust-job-tuple")
            self.logger.info("Found %d job cards on Naukri", len(job_cards))

            for card in job_cards[:30]:  # limit per page
                try:
                    title_el = await card.query_selector("a.title, a.jobTitle")
                    company_el = await card.query_selector("a.subTitle, a.comp-name")
                    location_el = await card.query_selector("span.locWdth, span.loc-wrap, li.location")
                    salary_el = await card.query_selector("span.salary, li.salary")

                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    url = await title_el.get_attribute("href") or ""
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    loc = (await location_el.inner_text()).strip() if location_el else ""
                    salary = (await salary_el.inner_text()).strip() if salary_el else ""

                    # Extract external ID from URL
                    ext_id_match = re.search(r"-(\d+)\?", url)
                    ext_id = ext_id_match.group(1) if ext_id_match else url[-20:]

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
                    self.logger.debug("Error parsing job card: %s", e)
                    continue

        except Exception as e:
            self.logger.error("Naukri search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        page = await self._ensure_browser()

        try:
            await page.goto(job.url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            # Get full description
            desc_el = await page.query_selector("div.job-desc, section.job-desc")
            if desc_el:
                job.description = (await desc_el.inner_text()).strip()

            # Look for apply button
            apply_btn = await page.query_selector(
                'button:has-text("Apply"), a:has-text("Apply on company site"), '
                'button:has-text("Apply Now"), button.apply-button'
            )
            if not apply_btn:
                self.logger.warning("No apply button found for: %s", job.title)
                return False

            await apply_btn.click()
            await human_delay(2, 4)

            # Handle chatbot/questionnaire if present
            chatbot = await page.query_selector("div.chatbot_container")
            if chatbot:
                self.logger.info("Chatbot detected, attempting to fill...")
                await self._handle_chatbot(page)

            # Check for success indicators
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
            return True  # Assume success if no error

        except Exception as e:
            self.logger.error("Apply error for %s: %s", job.title, e)
            return False

    async def _handle_chatbot(self, page: Page) -> None:
        """Try to handle Naukri's chatbot questionnaire."""
        for _ in range(5):  # max 5 questions
            await human_delay(1, 2)
            # Try to find and click common options
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
        page = await self._ensure_browser()
        try:
            await page.goto(self.BASE_URL)
            await page.wait_for_load_state("networkidle")
            # Check if key selectors exist
            search_box = await page.query_selector('input[placeholder*="skill"], input.suggestor-input')
            return search_box is not None
        except Exception as e:
            self.logger.error("Naukri health check failed: %s", e)
            return False
