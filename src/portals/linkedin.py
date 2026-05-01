"""LinkedIn portal — scrape-only mode (auto-apply disabled due to bot detection)."""

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


class LinkedInPortal(BasePortal):
    name = "linkedin"
    auto_apply_supported = False  # Scrape-only

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
        """Search LinkedIn per keyword + location via public guest page."""
        all_jobs: list[JobListing] = []
        seen_ids: set[str] = set()

        terms = self.get_search_terms(max_terms=10)
        if not terms:
            terms = ["jobs"]

        # Cap locations to avoid HTTP request explosion (terms × locations × 3 pages)
        locations = (self.config.search.locations or [""])[:5]

        for keyword in terms:
            for location in locations:
                try:
                    jobs = await self._search_keyword(keyword, location)
                    for job in jobs:
                        if job.external_id not in seen_ids:
                            seen_ids.add(job.external_id)
                            all_jobs.append(job)
                except Exception as e:
                    self.logger.warning("LinkedIn search failed for '%s' in '%s': %s", keyword, location, e)

        self.logger.info("LinkedIn total: %d unique jobs from %d terms × %d locations", len(all_jobs), len(terms), len(locations))
        return all_jobs

    # LinkedIn f_E values: 1=Internship, 2=Entry, 3=Associate, 4=Mid-Senior, 5=Director, 6=Executive
    _SENIORITY_MAP = {
        "internship": "1", "entry": "2", "associate": "3",
        "mid-senior": "4", "senior": "4", "mid": "4",
        "director": "5", "vp": "6", "executive": "6",
    }

    async def _search_keyword(self, keyword: str, location: str = "") -> list[JobListing]:
        """Search for a single keyword+location via LinkedIn guest page with pagination."""
        jobs: list[JobListing] = []

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            # Fetch up to 3 pages (0, 1, 2) = ~75 results per keyword+location
            for page_num in range(3):
                params = {
                    "keywords": keyword,
                    "location": location,
                    "trk": "public_jobs_jobs-search-bar_search-submit",
                    "position": 1,
                    "pageNum": page_num,
                }

                # Add seniority filter if configured
                seniority = self.config.search.seniority_levels
                if seniority:
                    levels = set()
                    for s in seniority:
                        mapped = self._SENIORITY_MAP.get(s.lower())
                        if mapped:
                            levels.add(mapped)
                    if levels:
                        params["f_E"] = ",".join(sorted(levels))

                resp = await client.get(f"{self.BASE_URL}/jobs/search/", params=params)
                if resp.status_code >= 400:
                    self.logger.debug("LinkedIn returned %d for '%s' page %d", resp.status_code, keyword, page_num)
                    break

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("div.base-card, li.result-card, div.job-search-card")

                if not cards:
                    # Try structured data fallback before giving up
                    if page_num == 0:
                        fallback_jobs = self._parse_structured_data(soup)
                        jobs.extend(fallback_jobs)
                    break

                page_jobs = self._parse_cards(cards)
                if not page_jobs and page_num == 0:
                    page_jobs = self._parse_structured_data(soup)
                jobs.extend(page_jobs)

                if len(cards) < 10:
                    break  # last page (fewer results than expected)

        self.logger.info("LinkedIn found %d jobs for '%s' in '%s'", len(jobs), keyword, location)
        return jobs

    def _parse_cards(self, cards: list) -> list[JobListing]:
        """Parse LinkedIn job cards into JobListing objects."""
        jobs: list[JobListing] = []
        for card in cards:
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

        return jobs

    def _parse_structured_data(self, soup: BeautifulSoup) -> list[JobListing]:
        """Fallback: extract jobs from JSON-LD structured data in the page."""
        import json

        jobs: list[JobListing] = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(script.string or "")
                items = []
                if isinstance(ld, dict) and ld.get("@type") == "ItemList":
                    items = ld.get("itemListElement", [])
                for item in items:
                    ji = item.get("item", item) if isinstance(item, dict) else item
                    if not isinstance(ji, dict):
                        continue
                    title = ji.get("title", ji.get("name", "")).strip()
                    org = ji.get("hiringOrganization", {})
                    company = org.get("name", "Unknown") if isinstance(org, dict) else "Unknown"
                    url = ji.get("url", "")
                    ext_id = url.rstrip("/").split("-")[-1] if url else title[:20]
                    if title:
                        jobs.append(JobListing(
                            portal=self.name, external_id=ext_id,
                            title=title, company=company, url=url,
                        ))
            except (json.JSONDecodeError, TypeError):
                continue

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
                resp = await client.get(f"{self.BASE_URL}/jobs/search/?keywords=software+engineer")
                return resp.status_code < 400
        except Exception:
            return False
