"""Job aggregator API clients — JSearch, Adzuna, RemoteOK, WeWorkRemotely.

These provide structured JSON job results without needing a browser.
JSearch covers: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs.
Adzuna covers: India, UK, US, and 10+ other countries.
RemoteOK covers: Remote tech/dev/design jobs worldwide.
WeWorkRemotely covers: Remote jobs across categories.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from src.portals.base import JobListing

logger = logging.getLogger(__name__)


# ─── JSearch (RapidAPI) ───


JSEARCH_HOST = "jsearch.p.rapidapi.com"
JSEARCH_URL = f"https://{JSEARCH_HOST}/search"


async def jsearch_search(
    query: str,
    location: str = "",
    page: int = 1,
    num_pages: int = 1,
    rapidapi_key: str = "",
) -> list[JobListing]:
    """Search jobs via JSearch API (RapidAPI).

    Covers: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs.
    Free tier: sign up at rapidapi.com, no credit card needed.
    """
    if not rapidapi_key:
        logger.debug("JSearch: no RAPIDAPI_KEY configured, skipping")
        return []

    headers = {
        "X-RapidAPI-Key": rapidapi_key,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }

    search_query = f"{query} in {location}" if location else query
    params = {
        "query": search_query,
        "page": str(page),
        "num_pages": str(num_pages),
    }

    jobs: list[JobListing] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(JSEARCH_URL, headers=headers, params=params)
            if resp.status_code == 429:
                logger.warning("JSearch: rate limited (429)")
                return jobs
            if resp.status_code >= 400:
                logger.warning("JSearch: HTTP %d for '%s'", resp.status_code, query)
                return jobs

            data = resp.json()
            results = data.get("data", [])
            logger.info("JSearch returned %d results for '%s'", len(results), query)

            for item in results:
                title = item.get("job_title", "").strip()
                company = item.get("employer_name", "Unknown").strip()
                city = item.get("job_city", "")
                state = item.get("job_state", "")
                country = item.get("job_country", "")
                loc_parts = [p for p in [city, state, country] if p]
                loc = ", ".join(loc_parts)
                url = item.get("job_apply_link") or item.get("job_google_link", "")
                description = item.get("job_description", "")[:500]
                ext_id = item.get("job_id", "") or title[:20]

                # Salary
                sal_min = item.get("job_min_salary")
                sal_max = item.get("job_max_salary")
                sal_period = item.get("job_salary_period", "")
                salary = ""
                if sal_min and sal_max:
                    salary = f"{sal_min}-{sal_max} {sal_period}"
                elif sal_min:
                    salary = f"{sal_min}+ {sal_period}"

                # Determine source portal
                publisher = (item.get("job_publisher") or "").lower()
                portal = "jsearch"
                if "linkedin" in publisher:
                    portal = "linkedin"
                elif "indeed" in publisher:
                    portal = "indeed"
                elif "glassdoor" in publisher:
                    portal = "glassdoor"
                elif "ziprecruiter" in publisher:
                    portal = "ziprecruiter"

                if title:
                    jobs.append(JobListing(
                        portal=portal,
                        external_id=str(ext_id),
                        title=title,
                        company=company,
                        location=loc,
                        url=url,
                        description=description,
                        salary=salary,
                        metadata={"source": "jsearch", "publisher": publisher},
                    ))

    except Exception as e:
        logger.error("JSearch error for '%s': %s", query, e)

    return jobs


# ─── Adzuna ───


ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


async def adzuna_search(
    query: str,
    location: str = "",
    country: str = "in",  # "in" = India, "gb" = UK, "us" = US
    page: int = 1,
    results_per_page: int = 20,
    app_id: str = "",
    app_key: str = "",
) -> list[JobListing]:
    """Search jobs via Adzuna API.

    Covers: India, UK, US, and 10+ other countries.
    Free tier: sign up at developer.adzuna.com.
    """
    if not app_id or not app_key:
        logger.debug("Adzuna: no ADZUNA_APP_ID/ADZUNA_APP_KEY configured, skipping")
        return []

    url = ADZUNA_URL.format(country=country, page=page)
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": query,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }
    if location:
        params["where"] = location

    jobs: list[JobListing] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                logger.warning("Adzuna: HTTP %d for '%s'", resp.status_code, query)
                return jobs

            data = resp.json()
            results = data.get("results", [])
            logger.info("Adzuna returned %d results for '%s'", len(results), query)

            for item in results:
                title = item.get("title", "").strip()
                company_obj = item.get("company", {})
                company = company_obj.get("display_name", "Unknown") if isinstance(company_obj, dict) else "Unknown"
                loc_obj = item.get("location", {})
                loc_parts = loc_obj.get("display_name", "") if isinstance(loc_obj, dict) else ""
                url = item.get("redirect_url", "")
                description = item.get("description", "")[:500]
                ext_id = item.get("id", "") or title[:20]

                sal_min = item.get("salary_min")
                sal_max = item.get("salary_max")
                salary = ""
                if sal_min and sal_max:
                    salary = f"₹{sal_min:,.0f}-₹{sal_max:,.0f}"
                elif sal_min:
                    salary = f"₹{sal_min:,.0f}+"

                if title:
                    jobs.append(JobListing(
                        portal="adzuna",
                        external_id=str(ext_id),
                        title=title,
                        company=company,
                        location=loc_parts,
                        url=url,
                        description=description,
                        salary=salary,
                        metadata={"source": "adzuna", "country": country},
                    ))

    except Exception as e:
        logger.error("Adzuna error for '%s': %s", query, e)

    return jobs


# ─── RemoteOK ───


REMOTEOK_URL = "https://remoteok.com/api"


async def remoteok_search(
    query: str = "",
    tags: list[str] | None = None,
) -> list[JobListing]:
    """Search remote jobs via RemoteOK free JSON API.

    No API key needed. Rate limit: be polite (1 req/sec).
    """
    jobs: list[JobListing] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"User-Agent": "auto-apply-cv-jobs/1.0"}
            resp = await client.get(REMOTEOK_URL, headers=headers)
            if resp.status_code >= 400:
                logger.warning("RemoteOK: HTTP %d", resp.status_code)
                return jobs

            data = resp.json()
            # First item is metadata, skip it
            results = data[1:] if len(data) > 1 else []
            query_lower = query.lower()

            for item in results:
                title = item.get("position", "").strip()
                company = item.get("company", "Unknown").strip()
                loc = item.get("location", "Remote")
                url = item.get("url", "")
                if url and not url.startswith("http"):
                    url = f"https://remoteok.com{url}"
                description = item.get("description", "")[:500]
                ext_id = item.get("id", "") or title[:20]
                salary = ""
                sal_min = item.get("salary_min")
                sal_max = item.get("salary_max")
                if sal_min and sal_max:
                    salary = f"${sal_min:,}-${sal_max:,}"

                job_tags = [t.lower() for t in item.get("tags", [])]

                # Filter by query if provided
                if query_lower:
                    searchable = f"{title} {company} {description} {' '.join(job_tags)}".lower()
                    if query_lower not in searchable:
                        continue

                if title:
                    jobs.append(JobListing(
                        portal="remoteok",
                        external_id=str(ext_id),
                        title=title,
                        company=company,
                        location=loc or "Remote",
                        url=url,
                        description=description,
                        salary=salary,
                        metadata={"source": "remoteok", "tags": job_tags},
                    ))

        logger.info("RemoteOK returned %d results for '%s'", len(jobs), query)
    except Exception as e:
        logger.error("RemoteOK error: %s", e)

    return jobs


# ─── WeWorkRemotely ───


WEWORKREMOTELY_RSS = "https://weworkremotely.com/categories/remote-{category}-jobs.rss"
WWR_CATEGORIES = [
    "programming",
    "design",
    "devops-sysadmin",
    "management-finance",
    "product",
    "customer-support",
    "sales-marketing",
]


async def weworkremotely_search(
    query: str = "",
    categories: list[str] | None = None,
) -> list[JobListing]:
    """Search remote jobs via WeWorkRemotely RSS feeds.

    No API key needed. Fetches RSS XML and parses job listings.
    """
    import xml.etree.ElementTree as ET

    jobs: list[JobListing] = []
    cats = categories or WWR_CATEGORIES[:3]  # default: top 3 categories
    query_lower = query.lower()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for category in cats:
                url = WEWORKREMOTELY_RSS.format(category=category)
                try:
                    resp = await client.get(url, headers={"User-Agent": "auto-apply-cv-jobs/1.0"})
                    if resp.status_code >= 400:
                        logger.debug("WWR: HTTP %d for category %s", resp.status_code, category)
                        continue

                    root = ET.fromstring(resp.text)
                    items = root.findall(".//item")

                    for item in items:
                        title_raw = item.findtext("title", "").strip()
                        # WWR format: "Company: Job Title"
                        if ":" in title_raw:
                            company, title = title_raw.split(":", 1)
                            company = company.strip()
                            title = title.strip()
                        else:
                            title = title_raw
                            company = "Unknown"

                        link = item.findtext("link", "")
                        description = item.findtext("description", "")[:500]
                        pub_date = item.findtext("pubDate", "")
                        ext_id = link.split("/")[-1] if link else title[:20]

                        # Filter by query
                        if query_lower:
                            searchable = f"{title} {company} {description} {category}".lower()
                            if query_lower not in searchable:
                                continue

                        if title:
                            jobs.append(JobListing(
                                portal="weworkremotely",
                                external_id=str(ext_id),
                                title=title,
                                company=company,
                                location="Remote",
                                url=link,
                                description=description,
                                salary="",
                                metadata={"source": "weworkremotely", "category": category, "pub_date": pub_date},
                            ))

                except Exception as e:
                    logger.debug("WWR category %s error: %s", category, e)

        logger.info("WeWorkRemotely returned %d results for '%s'", len(jobs), query)
    except Exception as e:
        logger.error("WeWorkRemotely error: %s", e)

    return jobs


# ─── Unified search ───


async def aggregator_search(
    terms: list[str],
    location: str = "",
    rapidapi_key: str = "",
    adzuna_app_id: str = "",
    adzuna_app_key: str = "",
    max_terms: int = 8,
    include_remote: bool = True,
) -> list[JobListing]:
    """Search across all configured aggregator APIs.

    Returns deduplicated job listings from JSearch + Adzuna + RemoteOK + WeWorkRemotely.
    RemoteOK and WeWorkRemotely are free (no API key needed).
    """
    all_jobs: list[JobListing] = []
    seen: set[str] = set()  # dedup by (title_lower, company_lower)

    has_jsearch = bool(rapidapi_key)
    has_adzuna = bool(adzuna_app_id and adzuna_app_key)

    def _add_jobs(jobs: list[JobListing]) -> int:
        added = 0
        for job in jobs:
            key = (job.title.lower(), job.company.lower())
            if key not in seen:
                seen.add(key)
                all_jobs.append(job)
                added += 1
        return added

    for i, term in enumerate(terms[:max_terms]):
        # JSearch
        if has_jsearch:
            try:
                if i > 0:
                    await asyncio.sleep(1.5)
                jobs = await jsearch_search(term, location, rapidapi_key=rapidapi_key)
                _add_jobs(jobs)
            except Exception as e:
                logger.warning("JSearch failed for '%s': %s", term, e)

        # Adzuna
        if has_adzuna:
            try:
                jobs = await adzuna_search(term, location, app_id=adzuna_app_id, app_key=adzuna_app_key)
                _add_jobs(jobs)
            except Exception as e:
                logger.warning("Adzuna failed for '%s': %s", term, e)

    # Free remote job sources (no API key needed)
    if include_remote:
        for term in terms[:3]:  # limit to top 3 terms for free APIs
            try:
                jobs = await remoteok_search(term)
                added = _add_jobs(jobs)
                if added:
                    logger.info("RemoteOK added %d jobs for '%s'", added, term)
            except Exception as e:
                logger.warning("RemoteOK failed for '%s': %s", term, e)

            try:
                jobs = await weworkremotely_search(term)
                added = _add_jobs(jobs)
                if added:
                    logger.info("WeWorkRemotely added %d jobs for '%s'", added, term)
            except Exception as e:
                logger.warning("WeWorkRemotely failed for '%s': %s", term, e)

    logger.info("Aggregator total: %d unique jobs from %d terms (sources: %s)",
                len(all_jobs), min(len(terms), max_terms),
                ", ".join(filter(None, [
                    "JSearch" if has_jsearch else None,
                    "Adzuna" if has_adzuna else None,
                    "RemoteOK" if include_remote else None,
                    "WeWorkRemotely" if include_remote else None,
                ])))
    return all_jobs
