"""Job aggregator API clients — JSearch (RapidAPI) and Adzuna.

These provide structured JSON job results without needing a browser.
JSearch covers: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs.
Adzuna covers: India-specific jobs across multiple boards.
"""

from __future__ import annotations

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


# ─── Unified search ───


async def aggregator_search(
    terms: list[str],
    location: str = "",
    rapidapi_key: str = "",
    adzuna_app_id: str = "",
    adzuna_app_key: str = "",
    max_terms: int = 8,
) -> list[JobListing]:
    """Search across all configured aggregator APIs.

    Returns deduplicated job listings from JSearch + Adzuna.
    """
    all_jobs: list[JobListing] = []
    seen: set[str] = set()  # dedup by (title_lower, company_lower)

    has_jsearch = bool(rapidapi_key)
    has_adzuna = bool(adzuna_app_id and adzuna_app_key)

    if not has_jsearch and not has_adzuna:
        logger.warning("No aggregator API keys configured. Set RAPIDAPI_KEY and/or ADZUNA_APP_ID + ADZUNA_APP_KEY.")
        return []

    for term in terms[:max_terms]:
        # JSearch
        if has_jsearch:
            try:
                jobs = await jsearch_search(term, location, rapidapi_key=rapidapi_key)
                for job in jobs:
                    key = (job.title.lower(), job.company.lower())
                    if key not in seen:
                        seen.add(key)
                        all_jobs.append(job)
            except Exception as e:
                logger.warning("JSearch failed for '%s': %s", term, e)

        # Adzuna
        if has_adzuna:
            try:
                jobs = await adzuna_search(term, location, app_id=adzuna_app_id, app_key=adzuna_app_key)
                for job in jobs:
                    key = (job.title.lower(), job.company.lower())
                    if key not in seen:
                        seen.add(key)
                        all_jobs.append(job)
            except Exception as e:
                logger.warning("Adzuna failed for '%s': %s", term, e)

    logger.info("Aggregator total: %d unique jobs from %d terms", len(all_jobs), min(len(terms), max_terms))
    return all_jobs
