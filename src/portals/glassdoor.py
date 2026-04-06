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
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
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
        """Search Glassdoor per keyword via HTTP — no browser needed."""
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
                self.logger.warning("Glassdoor search failed for '%s': %s", keyword, e)

        self.logger.info("Glassdoor total: %d unique jobs from %d keywords", len(all_jobs), len(keywords))
        return all_jobs

    async def _search_keyword(self, keyword: str) -> list[JobListing]:
        """Search for a single keyword."""
        jobs: list[JobListing] = []
        location = self.config.search.locations[0] if self.config.search.locations else ""

        params = {"sc.keyword": keyword, "locT": "C", "locKeyword": location}

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = await client.get(f"{self.BASE_URL}/Job/jobs.htm", params=params)
            if resp.status_code >= 400:
                self.logger.debug("Glassdoor returned %d for '%s'", resp.status_code, keyword)
                return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # Try structured data
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
                    jl = ji.get("jobLocation", {})
                    loc = ""
                    if isinstance(jl, dict):
                        addr = jl.get("address", {})
                        if isinstance(addr, dict):
                            loc = addr.get("addressLocality", "")
                    elif isinstance(jl, list) and jl:
                        addr = jl[0].get("address", {})
                        if isinstance(addr, dict):
                            loc = addr.get("addressLocality", "")
                    url = ji.get("url", "")
                    ext_id = url.split("jl=")[-1][:20] if "jl=" in url else title[:20]
                    if title:
                        jobs.append(JobListing(
                            portal=self.name, external_id=ext_id,
                            title=title, company=company, location=loc, url=url,
                        ))
            except (json.JSONDecodeError, TypeError):
                continue

        # HTML fallback
        if not jobs:
            cards = soup.select(
                "li.react-job-listing, li[data-test='jobListing'], "
                "div.JobCard_jobCard, li.JobsList_jobListItem"
            )
            for card in cards[:15]:
                try:
                    title_el = card.select_one(
                        "a[data-test='job-link'], a.jobTitle, a.JobCard_jobTitle, a.job-title"
                    )
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", "")
                    if url.startswith("/"):
                        url = f"{self.BASE_URL}{url}"
                    company_el = card.select_one(
                        "div.employer-name, span.EmployerProfile_compactEmployerName"
                    )
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    location_el = card.select_one("span.loc, div[data-test='emp-location']")
                    loc = location_el.get_text(strip=True) if location_el else ""
                    ext_id = url.split("jl=")[-1][:20] if "jl=" in url else title[:20]
                    jobs.append(JobListing(
                        portal=self.name, external_id=ext_id,
                        title=title, company=company, location=loc, url=url,
                    ))
                except Exception:
                    continue

        self.logger.info("Glassdoor found %d jobs for '%s'", len(jobs), keyword)
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
                resp = await client.get(f"{self.BASE_URL}/Job/jobs.htm?sc.keyword=software+engineer")
                return resp.status_code < 400
        except Exception:
            return False
