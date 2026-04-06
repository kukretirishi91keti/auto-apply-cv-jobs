"""Indeed portal scraper with auto-apply support."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class IndeedPortal(BasePortal):
    name = "indeed"
    auto_apply_supported = True

    BASE_URL = "https://in.indeed.com"

    def __init__(self, config: AppConfig, creds: Credentials):
        super().__init__(config, creds)
        self._page: Page | None = None
        self._ctx_manager = None

    async def _ensure_browser(self) -> Page:
        from src.utils.browser import create_stealth_context
        if self._page is None:
            self._ctx_manager = create_stealth_context(self.config, self.name)
            _, _, self._page = await self._ctx_manager.__aenter__()
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
        """Search using HTTP + BeautifulSoup — no browser needed."""
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/jobs?q={quote_plus(keywords)}&l={quote_plus(location)}&limit=25"

        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
                resp = await client.get(search_url)
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Try data-testid selectors first, then fallback classes
            title_links = soup.select('[data-testid="jobTitle"]')
            if not title_links:
                title_links = soup.select("h2.jobTitle a, a.jcs-JobTitle")

            self.logger.info("Indeed HTTP found %d job titles", len(title_links))

            for el in title_links[:25]:
                try:
                    title = el.get_text(strip=True)
                    url_path = el.get("href", "")
                    url = f"{self.BASE_URL}{url_path}" if url_path.startswith("/") else url_path

                    parent = el.find_parent("td") or el.find_parent("div", class_=True)
                    company_el = parent.select_one('[data-testid="company-name"], span.companyName') if parent else None
                    location_el = parent.select_one('[data-testid="text-location"], div.companyLocation') if parent else None
                    salary_el = parent.select_one('[data-testid="salaryRange"], div.salary-snippet-container') if parent else None
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    loc = location_el.get_text(strip=True) if location_el else ""
                    salary = salary_el.get_text(strip=True) if salary_el else ""

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
                    self.logger.debug("Error parsing Indeed job: %s", e)

        except Exception as e:
            self.logger.error("Indeed HTTP search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        from src.utils.browser import take_screenshot
        from src.utils.rate_limiter import medium_pause, short_pause
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

            apply_frame = page.frame("indeedapply-modal-preload-1") or page
            resume_input = await apply_frame.query_selector('input[type="file"]')
            if resume_input:
                await resume_input.set_input_files(cv_path)
                await short_pause()

            continue_btn = await apply_frame.query_selector('button:has-text("Continue"), button:has-text("Submit")')
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
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
                resp = await client.get(f"{self.BASE_URL}/jobs?q=test&limit=1")
                return resp.status_code == 200
        except Exception:
            return False
