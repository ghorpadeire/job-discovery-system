"""
Indeed Ireland scraper — JSON blob first, HTML fallback.

Searches multiple queries relevant to Pranav's target roles:
  java developer, software developer, cybersecurity analyst, IT support,
  helpdesk, systems engineer

Strategy:
  1. Look for embedded JSON in page source (fastest, most reliable)
  2. Fall back to HTML parsing with BeautifulSoup
"""
import asyncio
import json
import logging
import random
import re
from typing import Optional
from urllib.parse import quote_plus

from scrapers.base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

BASE_URL = "https://ie.indeed.com"

# All searches relevant to Pranav's target roles
SEARCH_QUERIES = [
    "java developer",
    "software developer",
    "cybersecurity analyst",
    "IT support",
    "helpdesk",
    "systems engineer",
    "spring boot developer",
    "junior developer",
    "graduate developer",
]

LOCATION = "Dublin"
MAX_PAGES = 3  # Indeed can get aggressive with anti-bot; keep low

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _build_url(query: str, location: str = LOCATION, start: int = 0) -> str:
    return (
        f"{BASE_URL}/jobs?q={quote_plus(query)}&l={quote_plus(location)}&start={start}"
    )


class IndeedScraper(BaseScraper):
    def __init__(self):
        super().__init__("indeed")

    def scrape(self) -> list[ScraperResult]:
        try:
            return asyncio.run(self._async_scrape())
        except Exception as exc:
            self.logger.error("Indeed scrape failed: %s", exc)
            return []

    async def _async_scrape(self) -> list[ScraperResult]:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout

        results: list[ScraperResult] = []
        seen_urls: set[str] = set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-http2",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-IE",
                extra_http_headers={
                    "Accept-Language": "en-IE,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            page = await context.new_page()

            # Suppress heavy resources
            await page.route(
                "**/*.{png,jpg,jpeg,gif,svg,woff,woff2}",
                lambda route: route.abort(),
            )

            for query in SEARCH_QUERIES:
                self.logger.info("Indeed: searching '%s' in %s", query, LOCATION)

                for page_num in range(MAX_PAGES):
                    start = page_num * 10
                    url = _build_url(query, LOCATION, start)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(random.randint(2000, 4000))

                        # Accept cookie consent if shown
                        try:
                            await page.click(
                                "button:has-text('Accept'), [aria-label*='consent'], "
                                "button[id*='cookie'], button[class*='cookie']",
                                timeout=3000,
                            )
                        except Exception:
                            pass

                        content = await page.content()

                        # Strategy 1: JSON blob
                        json_jobs = self._extract_json_jobs(content)
                        if json_jobs:
                            for job in json_jobs:
                                if job.url and job.url not in seen_urls:
                                    seen_urls.add(job.url)
                                    results.append(job)
                            self.logger.debug(
                                "  Query '%s' page %d: %d jobs (JSON)", query, page_num + 1, len(json_jobs)
                            )
                        else:
                            # Strategy 2: HTML fallback
                            html_jobs = await self._extract_html_jobs(page)
                            for job in html_jobs:
                                if job.url and job.url not in seen_urls:
                                    seen_urls.add(job.url)
                                    results.append(job)
                            self.logger.debug(
                                "  Query '%s' page %d: %d jobs (HTML fallback)",
                                query, page_num + 1, len(html_jobs)
                            )

                        await asyncio.sleep(random.uniform(1.5, 3.0))

                    except PWTimeout:
                        self.logger.warning("Timeout: Indeed '%s' page %d", query, page_num + 1)
                        break
                    except Exception as exc:
                        self.logger.warning("Error: Indeed '%s' page %d: %s", query, page_num + 1, exc)
                        break

            await browser.close()

        self.logger.info("Indeed: scraped %d unique jobs", len(results))
        return results

    def _extract_json_jobs(self, html_content: str) -> list[ScraperResult]:
        """
        Extract jobs from Indeed's embedded mosaic JSON data.
        Indeed embeds job data as a JS variable in the page source.
        """
        results = []
        try:
            # Pattern 1: mosaic provider jobcards
            patterns = [
                r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});',
                r'"jobsInPage"\s*:\s*(\[.*?\])',
                r'window\._initialData\s*=\s*(\{.*?"jobResults".*?\});',
                r'"jobs"\s*:\s*(\[(?:\{[^{}]*\}(?:,\{[^{}]*\})*)\])',
            ]

            for pattern in patterns:
                m = re.search(pattern, html_content, re.DOTALL)
                if not m:
                    continue

                raw = m.group(1)
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Navigate to job array
                jobs_list = None
                if isinstance(data, list):
                    jobs_list = data
                elif isinstance(data, dict):
                    # Try nested paths
                    for path in [
                        ["metaData", "mosaicProviderJobCardsModel", "results"],
                        ["results"],
                        ["jobResults", "results"],
                        ["jobCards"],
                    ]:
                        obj = data
                        for key in path:
                            obj = obj.get(key, {}) if isinstance(obj, dict) else None
                            if obj is None:
                                break
                        if isinstance(obj, list):
                            jobs_list = obj
                            break

                if not jobs_list:
                    continue

                for job_data in jobs_list:
                    parsed = self._parse_json_job(job_data)
                    if parsed:
                        results.append(parsed)

                if results:
                    break  # stop after first successful pattern

        except Exception as exc:
            logger.debug("_extract_json_jobs error: %s", exc)

        return results

    def _parse_json_job(self, job_data: dict) -> Optional[ScraperResult]:
        """Parse a single job from Indeed's JSON structure."""
        try:
            title = (
                job_data.get("displayTitle")
                or job_data.get("title")
                or job_data.get("normalizedTitle")
                or ""
            )
            if not title:
                return None

            company = (
                job_data.get("company")
                or job_data.get("companyName")
                or job_data.get("employer", {}).get("name", "")
                or "Unknown"
            )

            location = (
                job_data.get("formattedLocation")
                or job_data.get("location")
                or ""
            )

            # Salary from snippet
            salary = ""
            salary_data = job_data.get("extractedSalary") or job_data.get("salarySnippet") or {}
            if isinstance(salary_data, dict):
                min_s = salary_data.get("min") or ""
                max_s = salary_data.get("max") or ""
                currency = salary_data.get("currency", "EUR")
                if min_s and max_s:
                    salary = f"{currency} {min_s} – {max_s}"
                elif min_s:
                    salary = f"{currency} {min_s}+"
            elif isinstance(salary_data, str):
                salary = salary_data

            date_posted = (
                job_data.get("pubDate")
                or job_data.get("formattedRelativeTime")
                or job_data.get("datePosted")
                or ""
            )

            job_key = (
                job_data.get("jobkey")
                or job_data.get("jobKey")
                or job_data.get("id")
                or ""
            )

            if job_key:
                url = f"{BASE_URL}/viewjob?jk={job_key}"
            else:
                url = job_data.get("viewJobLink") or job_data.get("url") or ""
                if url and not url.startswith("http"):
                    url = BASE_URL + url

            if not url:
                return None

            return ScraperResult(
                title=str(title).strip(),
                company=str(company).strip(),
                location=str(location).strip(),
                salary=str(salary).strip(),
                date_posted=str(date_posted).strip(),
                url=url,
                description=str(job_data.get("snippet", "")).strip(),
                source="indeed",
            )

        except Exception as exc:
            logger.debug("_parse_json_job error: %s", exc)
            return None

    async def _extract_html_jobs(self, page) -> list[ScraperResult]:
        """HTML fallback scraper using Playwright selectors."""
        results = []
        try:
            # Indeed's HTML structure varies; try multiple selectors
            card_selectors = [
                "div.job_seen_beacon",
                "div[class*='jobsearch-SerpJobCard']",
                "li[class*='css-']",
                "div[data-testid='job-card']",
                "td.resultContent",
                "[class*='resultCard']",
            ]

            cards = []
            for sel in card_selectors:
                cards = await page.query_selector_all(sel)
                if cards:
                    break

            for card in cards:
                try:
                    # Title
                    title = ""
                    for t_sel in ["h2 a span", "h2 span", "[class*='jobTitle'] span", "a[id*='job']"]:
                        el = await card.query_selector(t_sel)
                        if el:
                            title = (await el.inner_text()).strip()
                            if title:
                                break

                    if not title:
                        continue

                    # Company
                    company = ""
                    for c_sel in [
                        "[data-testid='company-name']",
                        "span[class*='companyName']",
                        "[class*='company']",
                    ]:
                        el = await card.query_selector(c_sel)
                        if el:
                            company = (await el.inner_text()).strip()
                            if company:
                                break
                    company = company or "Unknown"

                    # Location
                    location = ""
                    for l_sel in [
                        "[data-testid='text-location']",
                        "div[class*='companyLocation']",
                        "[class*='location']",
                    ]:
                        el = await card.query_selector(l_sel)
                        if el:
                            location = (await el.inner_text()).strip()
                            if location:
                                break

                    # Salary
                    salary = ""
                    for s_sel in [
                        "[class*='salary']",
                        "[data-testid*='salary']",
                        "div[class*='metadata'] div",
                    ]:
                        el = await card.query_selector(s_sel)
                        if el:
                            text = (await el.inner_text()).strip()
                            if any(c in text for c in ["€", "£", "$", "per", "year", "hour", "salary"]):
                                salary = text
                                break

                    # Date
                    date_posted = ""
                    for d_sel in ["[class*='date']", "span[class*='date']", "[data-testid*='date']"]:
                        el = await card.query_selector(d_sel)
                        if el:
                            date_posted = (await el.inner_text()).strip()
                            if date_posted:
                                break

                    # URL
                    url = ""
                    for u_sel in ["h2 a", "a[id*='job']", "a[href*='/rc/']", "a[href*='viewjob']"]:
                        el = await card.query_selector(u_sel)
                        if el:
                            href = await el.get_attribute("href")
                            if href:
                                url = href if href.startswith("http") else BASE_URL + href
                                break

                    if not url:
                        continue

                    results.append(ScraperResult(
                        title=title,
                        company=company,
                        location=location,
                        salary=salary,
                        date_posted=date_posted,
                        url=url,
                        description="",
                        source="indeed",
                    ))

                except Exception as exc:
                    logger.debug("HTML card parse error: %s", exc)

        except Exception as exc:
            logger.warning("_extract_html_jobs error: %s", exc)

        return results
