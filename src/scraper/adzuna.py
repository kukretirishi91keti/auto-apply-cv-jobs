"""Adzuna API job scraper for India."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterator

import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"


@dataclass
class AdzunaJob:
    job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    salary_min: float | None = None
    salary_max: float | None = None
    posted_date: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def salary_display(self) -> str:
        if self.salary_min and self.salary_max:
            return f"₹{self.salary_min:,.0f} – ₹{self.salary_max:,.0f}"
        if self.salary_min:
            return f"₹{self.salary_min:,.0f}+"
        return "Not disclosed"


class AdzunaScraper:
    """Fetch jobs from Adzuna India API.

    Usage:
        scraper = AdzunaScraper(config)
        for job in scraper.search_all():
            result = match_job(job.title, job.description, ...)
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        portal_cfg = config.portals.get("adzuna", {})
        self.app_id: str = portal_cfg.get("app_id", "")
        self.app_key: str = portal_cfg.get("app_key", "")
        self.country: str = portal_cfg.get("country", "in")
        self.results_per_page: int = int(portal_cfg.get("results_per_page", 50))
        self.max_pages: int = int(portal_cfg.get("max_pages", 5))
        self.enabled: bool = portal_cfg.get("enabled", False)

        if not self.app_id or not self.app_key:
            raise ValueError(
                "Adzuna app_id and app_key must be set in config portals.adzuna"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_all(self) -> Iterator[AdzunaJob]:
        """Search Adzuna for all configured keywords and locations.

        De-duplicates by job_id across all searches.
        Yields AdzunaJob objects ready to pass to match_job().
        """
        if not self.enabled:
            logger.info("Adzuna scraper is disabled in config")
            return

        seen_ids: set[str] = set()
        search_queries = self._build_search_queries()

        logger.info(
            "Adzuna: starting search with %d queries", len(search_queries)
        )

        for query, location in search_queries:
            for job in self._search(query, location):
                if job.job_id not in seen_ids:
                    seen_ids.add(job.job_id)
                    yield job

        logger.info("Adzuna: total unique jobs fetched: %d", len(seen_ids))

    def search_query(self, query: str, location: str = "India") -> list[AdzunaJob]:
        """Search a single query — useful for testing."""
        return list(self._search(query, location))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_search_queries(self) -> list[tuple[str, str]]:
        """Build (query, location) pairs from config keywords + locations.

        Only uses the first ~10 terms (before comma-split depth) as actual
        search strings — same rule as LinkedIn.  Pairs each with each location.
        """
        raw_keywords = self.config.search.keywords or []
        search_terms: list[str] = []

        for kw_line in raw_keywords:
            for part in kw_line.split(","):
                term = part.strip()
                if term:
                    search_terms.append(term)
                if len(search_terms) >= 12:   # cap: don't hammer the API
                    break
            if len(search_terms) >= 12:
                break

        locations = self.config.search.locations or ["India"]

        # Always include bare "India" as a fallback location
        if "India" not in locations:
            locations = list(locations) + ["India"]

        pairs = [(term, loc) for term in search_terms for loc in locations]
        logger.debug("Adzuna: %d (query, location) pairs built", len(pairs))
        return pairs

    def _search(self, query: str, location: str) -> Iterator[AdzunaJob]:
        """Paginate through Adzuna results for one query+location."""
        for page in range(1, self.max_pages + 1):
            jobs, total = self._fetch_page(query, location, page)

            if not jobs:
                logger.debug(
                    "Adzuna: no results on page %d for '%s' @ %s",
                    page, query, location,
                )
                break

            logger.debug(
                "Adzuna: page %d/%d — '%s' @ %s — %d jobs (total: %d)",
                page, self.max_pages, query, location, len(jobs), total,
            )

            yield from jobs

            # Stop early if we've fetched everything
            if page * self.results_per_page >= total:
                break

            time.sleep(0.5)   # polite rate limiting

    def _fetch_page(
        self,
        query: str,
        location: str,
        page: int,
    ) -> tuple[list[AdzunaJob], int]:
        """Fetch one page from Adzuna API. Returns (jobs, total_count)."""
        url = f"{ADZUNA_BASE}/{self.country}/search/{page}"

        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": self.results_per_page,
            "what": query,
            "where": location,
            "sort_by": "date",            # freshest first
            "content-type": "application/json",
        }

        # Add experience/seniority filters if available
        exp_years = getattr(self.config.search, "experience_years", 0)
        if exp_years >= 8:
            # Adzuna salary proxy for seniority — jobs above ~20 LPA (INR)
            # 2_000_000 INR ≈ 20 LPA — filters out junior noise
            params["salary_min"] = 2_000_000

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            logger.error("Adzuna HTTP error for '%s': %s", query, e)
            return [], 0
        except requests.exceptions.RequestException as e:
            logger.error("Adzuna request failed for '%s': %s", query, e)
            return [], 0

        data = resp.json()
        total = data.get("count", 0)
        raw_jobs = data.get("results", [])

        jobs = [self._parse_job(r) for r in raw_jobs if self._parse_job(r)]
        return [j for j in jobs if j is not None], total

    def _parse_job(self, raw: dict) -> AdzunaJob | None:
        """Parse one Adzuna result dict into AdzunaJob."""
        try:
            job_id = str(raw.get("id", ""))
            title = raw.get("title", "").strip()
            description = raw.get("description", "").strip()
            url = raw.get("redirect_url", "")

            # Location
            loc_obj = raw.get("location", {})
            location_parts = loc_obj.get("display_name", "")

            # Company
            company_obj = raw.get("company", {})
            company = company_obj.get("display_name", "Unknown")

            # Salary
            salary_min = raw.get("salary_min")
            salary_max = raw.get("salary_max")

            # Category
            cat_obj = raw.get("category", {})
            category = cat_obj.get("label", "")

            # Date
            posted_date = raw.get("created", "")

            if not job_id or not title:
                return None

            return AdzunaJob(
                job_id=job_id,
                title=title,
                company=company,
                location=location_parts,
                description=description,
                url=url,
                salary_min=float(salary_min) if salary_min else None,
                salary_max=float(salary_max) if salary_max else None,
                posted_date=posted_date,
                category=category,
            )
        except Exception as e:
            logger.warning("Adzuna: failed to parse job: %s — %s", raw.get("id"), e)
            return None
