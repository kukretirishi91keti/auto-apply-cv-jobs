"""Foundit (formerly Monster India) portal scraper."""

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
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


class FounditPortal(BasePortal):
    name = "foundit"
    auto_apply_supported = True

    BASE_URL = "https://www.foundit.in"

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
        """Search Foundit per keyword via HTTP — no browser needed."""
        all_jobs: list[JobListing] = []
        seen_ids: set[str] = set()

        keywords = self.config.search.keywords[:5]
        if not keywords:
            keywords = ["jobs"]

        for keyword in keywords:
            try:
                jobs = await self._search_keyword(keyword)
                for job in jobs:
                    if job.external_id not in seen_ids:
                        seen_ids.add(job.external_id)
                        all_jobs.append(job)
            except Exception as e:
                self.logger.warning("Foundit search failed for '%s': %s", keyword, e)

        self.logger.info("Foundit total: %d unique jobs from %d keywords", len(all_jobs), len(keywords))
        return all_jobs

    async def _search_keyword(self, keyword: str) -> list[JobListing]:
        """Search for a single keyword."""
        jobs: list[JobListing] = []
        location = self.config.search.locations[0] if self.config.search.locations else ""

        params = {"query": keyword, "locations": location}

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = await client.get(f"{self.BASE_URL}/srp/results", params=params)
            if resp.status_code >= 400:
                self.logger.debug("Foundit returned %d for '%s'", resp.status_code, keyword)
                return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # Try structured data first
        import json
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(script.string or "")
                items = []
                if isinstance(ld, dict) and ld.get("@type") == "ItemList":
                    items = ld.get("itemListElement", [])
                elif isinstance(ld, list):
                    items = ld
                for item in items:
                    ji = item.get("item", item) if isinstance(item, dict) else item
                    if not isinstance(ji, dict) or ji.get("@type") != "JobPosting":
                        continue
                    title = ji.get("title", "").strip()
                    org = ji.get("hiringOrganization", {})
                    company = org.get("name", "Unknown") if isinstance(org, dict) else "Unknown"
                    url = ji.get("url", "")
                    ext_id = url.split("/")[-1][:20] if url else title[:20]
                    if title:
                        jobs.append(JobListing(
                            portal=self.name, external_id=ext_id,
                            title=title, company=company, url=url,
                            description=ji.get("description", "")[:500],
                        ))
            except (json.JSONDecodeError, TypeError):
                continue

        # HTML fallback
        if not jobs:
            cards = soup.select("div.card-apply-content, div.jobTuple, div.srpResultCardContainer")
            for card in cards[:15]:
                try:
                    title_el = card.select_one("a.card-title, a.title, a.jobTitle")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", "")
                    if url.startswith("/"):
                        url = f"{self.BASE_URL}{url}"
                    company_el = card.select_one("span.company-name, a.company-name, span.companyName")
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    location_el = card.select_one("span.loc, span.location-text, span.locWdth")
                    loc = location_el.get_text(strip=True) if location_el else ""
                    salary_el = card.select_one("span.salary, span.sal")
                    salary = salary_el.get_text(strip=True) if salary_el else ""
                    ext_id = url.split("/")[-1][:20] if url else title[:20]
                    jobs.append(JobListing(
                        portal=self.name, external_id=ext_id,
                        title=title, company=company, location=loc, url=url, salary=salary,
                    ))
                except Exception:
                    continue

        self.logger.info("Foundit found %d jobs for '%s'", len(jobs), keyword)
        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        from src.utils.browser import take_screenshot
        from src.utils.rate_limiter import medium_pause
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
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
                resp = await client.get(f"{self.BASE_URL}/srp/results?query=software+engineer")
                return resp.status_code < 400
        except Exception:
            return False
