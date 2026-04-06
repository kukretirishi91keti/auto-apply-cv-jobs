"""Glassdoor portal — scrape-only mode (auto-apply disabled due to bot detection)."""

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


class GlassdoorPortal(BasePortal):
    name = "glassdoor"
    auto_apply_supported = False  # Scrape-only

    BASE_URL = "https://www.glassdoor.co.in"

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
        self.logger.info("Glassdoor is scrape-only — login skipped")
        return True

    async def search_jobs(self) -> list[JobListing]:
        """Search using HTTP + BeautifulSoup — no browser needed."""
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        search_url = f"{self.BASE_URL}/Job/jobs.htm"
        params = {
            "sc.keyword": keywords,
            "locT": "C",
            "locKeyword": location,
        }

        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
                resp = await client.get(search_url, params=params)
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            cards = soup.select(
                "li.react-job-listing, li[data-test='jobListing'], "
                "div.JobCard_jobCard, li.JobsList_jobListItem"
            )
            self.logger.info("Glassdoor HTTP found %d job cards", len(cards))

            for card in cards[:20]:
                try:
                    title_el = card.select_one(
                        "a[data-test='job-link'], a.jobTitle, "
                        "a.JobCard_jobTitle, a.job-title"
                    )
                    company_el = card.select_one(
                        "div.employer-name, span.EmployerProfile_compactEmployerName, "
                        "span.EmployerProfile_employerName"
                    )
                    location_el = card.select_one(
                        "span.loc, div[data-test='emp-location'], span.JobCard_location"
                    )
                    salary_el = card.select_one(
                        "span[data-test='detailSalary'], span.salary-estimate"
                    )

                    if not title_el:
                        continue

                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", "")
                    if url.startswith("/"):
                        url = f"{self.BASE_URL}{url}"
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    loc = location_el.get_text(strip=True) if location_el else ""
                    salary = salary_el.get_text(strip=True) if salary_el else ""
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
            self.logger.error("Glassdoor HTTP search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        """Glassdoor auto-apply is disabled. Jobs are scrape-only."""
        self.logger.info(
            "Glassdoor scrape-only: %s at %s — open manually: %s",
            job.title, job.company, job.url,
        )
        return False

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
                resp = await client.get(f"{self.BASE_URL}/Job/jobs.htm?sc.keyword=test")
                return resp.status_code == 200
        except Exception:
            return False
