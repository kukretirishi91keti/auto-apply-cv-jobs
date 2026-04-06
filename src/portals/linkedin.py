"""LinkedIn portal — scrape-only mode (auto-apply disabled due to bot detection)."""

from __future__ import annotations

import logging
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
}


class LinkedInPortal(BasePortal):
    name = "linkedin"
    auto_apply_supported = False  # Scrape-only

    # LinkedIn has a public guest job search API
    GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/sideBarJobCount"
    JOBS_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPostings/jobs"
    BASE_URL = "https://www.linkedin.com"

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
        self.logger.info("LinkedIn is scrape-only — login skipped")
        return True

    async def search_jobs(self) -> list[JobListing]:
        """Search using LinkedIn's public guest jobs HTML endpoint — no login needed."""
        jobs: list[JobListing] = []
        keywords = " ".join(self.config.search.keywords)
        location = self.config.search.locations[0] if self.config.search.locations else ""

        # LinkedIn guest job search page (returns HTML with job cards)
        search_url = "https://www.linkedin.com/jobs/search/"
        params = {
            "keywords": keywords,
            "location": location,
            "trk": "public_jobs_jobs-search-bar_search-submit",
            "position": 1,
            "pageNum": 0,
        }

        try:
            from bs4 import BeautifulSoup

            async with httpx.AsyncClient(
                headers={**HEADERS, "Accept": "text/html,*/*"},
                timeout=30,
                follow_redirects=True,
            ) as client:
                resp = await client.get(search_url, params=params)
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # LinkedIn guest pages use these selectors
            cards = soup.select("div.base-card, li.result-card, div.job-search-card")
            self.logger.info("LinkedIn HTTP found %d job cards", len(cards))

            for card in cards[:25]:
                try:
                    title_el = card.select_one("h3.base-search-card__title, h3.result-card__title")
                    company_el = card.select_one("h4.base-search-card__subtitle, h4.result-card__subtitle")
                    location_el = card.select_one("span.job-search-card__location")
                    link_el = card.select_one("a.base-card__full-link, a.result-card__full-card-link")

                    if not title_el:
                        continue

                    title = title_el.get_text(strip=True)
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    loc = location_el.get_text(strip=True) if location_el else ""
                    url = (link_el.get("href", "") if link_el else "").split("?")[0]

                    # Extract job ID from URL
                    ext_id = url.rstrip("/").split("-")[-1] if url else title[:20]

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
            self.logger.error("LinkedIn HTTP search error: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        """LinkedIn auto-apply is disabled. Jobs are scrape-only."""
        self.logger.info(
            "LinkedIn scrape-only: %s at %s — open manually: %s",
            job.title, job.company, job.url,
        )
        return False

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
                resp = await client.get("https://www.linkedin.com/jobs/search/?keywords=test")
                return resp.status_code == 200
        except Exception:
            return False
