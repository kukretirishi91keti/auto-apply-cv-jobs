"""Naukri.com portal scraper with full auto-apply support."""

from __future__ import annotations

import logging
import re
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
        from src.utils.browser import create_stealth_context
        if self._page is None:
            self._ctx_manager = create_stealth_context(self.config, self.name)
            self._browser, self._context, self._page = await self._ctx_manager.__aenter__()
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

            if "nlogin" not in page.url:
                self.logger.info("Naukri login successful")
                return True

            self.logger.error("Naukri login failed — still on login page")
            return False
        except Exception as e:
            self.logger.error("Naukri login error: %s", e)
            return False

    async def search_jobs(self) -> list[JobListing]:
        """Search Naukri via HTML scraping — one request per keyword."""
        all_jobs: list[JobListing] = []
        seen_ids: set[str] = set()
        location = self.config.search.locations[0] if self.config.search.locations else ""

        # Search per term (split comma-separated keywords)
        terms = self.get_search_terms(max_terms=8)
        if not terms:
            terms = ["jobs"]

        for keyword in terms:
            try:
                jobs = await self._search_keyword(keyword, location)
                for job in jobs:
                    if job.external_id not in seen_ids:
                        seen_ids.add(job.external_id)
                        all_jobs.append(job)
            except Exception as e:
                self.logger.warning("Naukri search failed for '%s': %s", keyword, e)

        self.logger.info("Naukri total: %d unique jobs from %d terms", len(all_jobs), len(terms))
        return all_jobs

    async def _search_keyword(self, keyword: str, location: str) -> list[JobListing]:
        """Search for a single keyword — try API first, then HTML fallback."""
        # Try API first (returns structured JSON, works without JS rendering)
        jobs = await self._search_api(keyword, location)
        if jobs:
            return jobs

        # Fallback: HTML scrape
        slug = keyword.lower().replace(" ", "-").replace(",", "")
        loc_slug = location.lower().replace(" ", "-") if location else ""
        search_url = f"{self.BASE_URL}/{quote_plus(slug)}-jobs"
        if loc_slug:
            search_url += f"-in-{quote_plus(loc_slug)}"
        if self.config.search.experience_years:
            search_url += f"?experience={self.config.search.experience_years}"

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = await client.get(search_url)
            if resp.status_code >= 400:
                self.logger.debug("Naukri HTML returned %d for '%s'", resp.status_code, keyword)
                return []

        soup = BeautifulSoup(resp.text, "html.parser")

        # Naukri embeds job data in script tags as JSON
        import json
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, dict) and ld.get("@type") == "ItemList":
                    for item in ld.get("itemListElement", []):
                        ji = item.get("item", item)
                        title = ji.get("title", ji.get("name", "")).strip()
                        company = ""
                        org = ji.get("hiringOrganization", {})
                        if isinstance(org, dict):
                            company = org.get("name", "Unknown")
                        loc = ""
                        jl = ji.get("jobLocation", {})
                        if isinstance(jl, dict):
                            addr = jl.get("address", {})
                            if isinstance(addr, dict):
                                loc = addr.get("addressLocality", "")
                        url = ji.get("url", "")
                        ext_id_match = re.search(r"-(\d{5,})", url)
                        ext_id = ext_id_match.group(1) if ext_id_match else title[:20]
                        salary = ji.get("baseSalary", {})
                        if isinstance(salary, dict):
                            sal_val = salary.get("value", {})
                            if isinstance(sal_val, dict):
                                salary = f"{sal_val.get('minValue', '')}-{sal_val.get('maxValue', '')}"
                            else:
                                salary = str(salary)
                        else:
                            salary = str(salary) if salary else ""

                        if title:
                            jobs.append(JobListing(
                                portal=self.name,
                                external_id=str(ext_id),
                                title=title,
                                company=company,
                                location=loc,
                                url=url,
                                salary=salary,
                            ))
            except (json.JSONDecodeError, TypeError):
                continue

        # Fallback: parse HTML job cards
        if not jobs:
            cards = soup.select("article.jobTuple, div.srp-jobtuple-wrapper, div.cust-job-tuple, div.jobTupleHeader")
            for card in cards[:30]:
                try:
                    title_el = card.select_one("a.title, a.jobTitle, a[class*='title']")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    url = title_el.get("href", "")
                    company_el = card.select_one("a.subTitle, a.comp-name, a[class*='comp']")
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    location_el = card.select_one("span.locWdth, span.loc-wrap, li.location, span[class*='loc']")
                    loc = location_el.get_text(strip=True) if location_el else ""
                    salary_el = card.select_one("span.salary, li.salary, span[class*='sal']")
                    sal = salary_el.get_text(strip=True) if salary_el else ""
                    ext_id_match = re.search(r"-(\d{5,})", url)
                    ext_id = ext_id_match.group(1) if ext_id_match else title[:20]
                    jobs.append(JobListing(
                        portal=self.name, external_id=str(ext_id),
                        title=title, company=company, location=loc, url=url, salary=sal,
                    ))
                except Exception:
                    continue

        self.logger.info("Naukri found %d jobs for '%s'", len(jobs), keyword)
        return jobs

    async def _search_api(self, keyword: str, location: str) -> list[JobListing]:
        """Fallback: try the Naukri JSON API."""
        jobs: list[JobListing] = []
        api_headers = {
            **HEADERS,
            "Accept": "application/json",
            "appid": "109",
            "systemid": "Naukri",
            "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "clientId": "d3skt0p",
        }
        params = {
            "noOfResults": 20,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": keyword,
            "location": location,
            "pageNo": 1,
        }
        if self.config.search.experience_years:
            params["experience"] = self.config.search.experience_years

        try:
            async with httpx.AsyncClient(headers=api_headers, timeout=30) as client:
                resp = await client.get("https://www.naukri.com/jobapi/v3/search", params=params)
                if resp.status_code >= 400:
                    return jobs
                data = resp.json()

            for item in data.get("jobDetails", []):
                title = item.get("title", "").strip()
                company = item.get("companyName", "Unknown").strip()
                placeholders = item.get("placeholders", [])
                loc = placeholders[0].get("label", "") if placeholders else ""
                salary = placeholders[1].get("label", "") if len(placeholders) > 1 else ""
                jd_url = item.get("jdURL", "")
                url = f"{self.BASE_URL}{jd_url}" if jd_url.startswith("/") else jd_url
                ext_id = str(item.get("jobId", "")) or title[:20]
                if title:
                    jobs.append(JobListing(
                        portal=self.name, external_id=ext_id,
                        title=title, company=company, location=loc, url=url, salary=salary,
                        description=item.get("jobDescription", ""),
                    ))
        except Exception as e:
            self.logger.debug("Naukri API fallback failed: %s", e)

        return jobs

    async def apply_to_job(self, job: JobListing, cv_path: str, cover_letter: str = "") -> bool:
        from src.utils.browser import take_screenshot
        from src.utils.rate_limiter import human_delay, medium_pause, short_pause
        page = await self._ensure_browser()

        try:
            await page.goto(job.url)
            await page.wait_for_load_state("networkidle")
            await medium_pause()

            desc_el = await page.query_selector("div.job-desc, section.job-desc")
            if desc_el:
                job.description = (await desc_el.inner_text()).strip()

            apply_btn = await page.query_selector(
                'button:has-text("Apply"), a:has-text("Apply on company site"), '
                'button:has-text("Apply Now"), button.apply-button'
            )
            if not apply_btn:
                self.logger.warning("No apply button found for: %s", job.title)
                return False

            await apply_btn.click()
            await human_delay(2, 4)

            chatbot = await page.query_selector("div.chatbot_container")
            if chatbot:
                self.logger.info("Chatbot detected, attempting to fill...")
                await self._handle_chatbot(page)

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
            return True
        except Exception as e:
            self.logger.error("Apply error for %s: %s", job.title, e)
            return False

    async def _handle_chatbot(self, page: Page) -> None:
        from src.utils.rate_limiter import human_delay, short_pause
        for _ in range(5):
            await human_delay(1, 2)
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
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
                resp = await client.get(f"{self.BASE_URL}/software-engineer-jobs")
                return resp.status_code == 200
        except Exception as e:
            self.logger.error("Naukri health check failed: %s", e)
            return False
