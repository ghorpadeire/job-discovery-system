"""
Indeed Ireland scraper (ie.indeed.com).

Indeed is a JavaScript-heavy SPA. Two extraction strategies are tried:

  1. Embedded JSON  — Indeed injects job data as a JSON blob inside a
     <script> tag. This is the most reliable source and doesn't depend
     on CSS class names (which Indeed rotates frequently).

  2. HTML fallback  — Uses data-testid attributes and other stable
     selectors as a fallback if the JSON blob is missing or changes.

Pagination uses the `start` query parameter (increments of 10).
"""
import json
import logging
import re
from typing import Optional
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

SOURCE = "indeed.ie"
BASE   = "https://ie.indeed.com"

# ---------------------------------------------------------------------------
# Selector tables (HTML fallback)
# ---------------------------------------------------------------------------

_CARD_SELECTORS = [
    "[data-testid='slider_item']",
    "[data-jk]",                         # data-jk holds the job key
    "div.job_seen_beacon",
    "div[class*='jobsearch-SerpJobCard']",
    "li[class*='result']",
    "td.resultContent",
]

_TITLE_SELECTORS = [
    "[data-testid='job-title'] span",
    "[data-testid='job-title']",
    "h2[class*='jobTitle'] a span",
    "h2[class*='jobTitle'] span",
    "h2 a span[title]",
    "h2 a",
    "h2",
]

_COMPANY_SELECTORS = [
    "[data-testid='company-name']",
    "span[class*='companyName']",
    "[class*='company']",
    "[class*='employer']",
]

_DATE_SELECTORS = [
    "[data-testid='myJobsStateDate']",
    "span[class*='date']",
    "[class*='date']",
    "span[class*='posted']",
]

_SALARY_SELECTORS = [
    "[data-testid='attribute_snippet_testid']",
    "[class*='estimated-salary']",
    "[class*='salary']",
    "[class*='compensation']",
]


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------

class IndeedScraper(BaseScraper):
    source_name    = SOURCE
    base_url       = BASE
    search_queries = ["java developer", "cybersecurity analyst"]
    location       = "Dublin, County Dublin"
    max_pages      = 5
    request_delay  = 2.0

    # Indeed shows ~15 results per page; pagination uses start=0,15,30...
    _PAGE_SIZE = 15

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, page: int = 1) -> str:
        params = {
            "q":       query,
            "l":       self.location,
            "sort":    "date",         # most recent first
            "start":   (page - 1) * self._PAGE_SIZE,
            "fromage": 30,             # posted within 30 days
        }
        return f"{BASE}/jobs?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Pagination detection
    # ------------------------------------------------------------------

    def has_next_page(self, soup: BeautifulSoup, page_num: int) -> bool:
        """Stop when Indeed's 'Next' button is absent."""
        return bool(
            soup.select_one(
                "a[data-testid='pagination-page-next'], "
                "a[aria-label='Next Page'], "
                "a[aria-label='Next'], "
                "nav[aria-label='pagination'] a:last-child"
            )
        )

    # ------------------------------------------------------------------
    # Main parse entry point
    # ------------------------------------------------------------------

    def parse_jobs(self, soup: BeautifulSoup, query: str) -> list[dict]:
        # Strategy 1: extract the embedded JSON blob
        jobs = self._parse_json(soup, query)
        if jobs:
            logger.debug(f"  JSON extraction: {len(jobs)} jobs")
            return jobs

        # Strategy 2: HTML fallback
        logger.debug("  JSON extraction failed — using HTML fallback")
        return self._parse_html(soup, query)

    # ------------------------------------------------------------------
    # Strategy 1 — embedded JSON
    # ------------------------------------------------------------------

    def _parse_json(self, soup: BeautifulSoup, query: str) -> list[dict]:
        """
        Indeed embeds job data in a <script> tag as:
            window.mosaic.providerData["mosaic-provider-jobcards"] = {...}
        or similar patterns. We extract the results array from that blob.
        """
        for script in soup.find_all("script"):
            text = script.string or ""
            # Match the results array inside mosaic provider data
            match = re.search(
                r'"results"\s*:\s*(\[\{.*?\}\])\s*,\s*"(?:totalResults|jobCount)"',
                text,
                re.DOTALL,
            )
            if not match:
                # Alternative pattern used in some regions
                match = re.search(
                    r'jobCards\s*=\s*(\[\{.*?\}\])',
                    text,
                    re.DOTALL,
                )
            if not match:
                continue

            try:
                results = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue

            jobs = []
            for item in results:
                title   = item.get("title")   or item.get("normalizedTitle") or ""
                company = item.get("company") or item.get("companyName")     or "Unknown"
                jk      = item.get("jobkey")  or item.get("jk")              or ""
                job_url = f"{BASE}/viewjob?jk={jk}" if jk else ""

                salary_obj = item.get("extractedSalary") or {}
                salary = (
                    f"{salary_obj.get('min', '')}–{salary_obj.get('max', '')} "
                    f"{salary_obj.get('type', '')}".strip("–").strip()
                    if salary_obj else item.get("salarySnippet", {}).get("text") or None
                )

                if title:
                    jobs.append({
                        "title":       title,
                        "company":     company,
                        "date_posted": item.get("formattedRelativeTime") or "",
                        "url":         job_url,
                        "salary":      salary or None,
                        "search_term": query,
                        "source":      SOURCE,
                    })
            return jobs

        return []

    # ------------------------------------------------------------------
    # Strategy 2 — HTML parsing
    # ------------------------------------------------------------------

    def _parse_html(self, soup: BeautifulSoup, query: str) -> list[dict]:
        cards = []
        for sel in _CARD_SELECTORS:
            cards = soup.select(sel)
            if cards:
                logger.debug(f"  HTML selector matched: {sel!r} ({len(cards)} cards)")
                break

        jobs = []
        for card in cards:
            try:
                job = self._extract_html(card, query)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"  Card parse error: {exc}")
        return jobs

    def _extract_html(self, card, query: str) -> Optional[dict]:
        title = self._text(card, _TITLE_SELECTORS)
        if not title or len(title) < 3:
            return None

        # Build job URL from data-jk attribute (most reliable on Indeed)
        jk = card.get("data-jk") or ""
        if not jk:
            jk_el = card.select_one("[data-jk]")
            jk    = jk_el.get("data-jk", "") if jk_el else ""
        job_url = f"{BASE}/viewjob?jk={jk}" if jk else ""

        # Fallback: grab href from title link
        if not job_url:
            href    = self._attr(card, _TITLE_SELECTORS, "href")
            job_url = urljoin(BASE, href) if href else ""

        company     = self._text(card, _COMPANY_SELECTORS) or "Unknown"
        date_posted = self._text(card, _DATE_SELECTORS) or ""
        salary      = self._text(card, _SALARY_SELECTORS)

        return {
            "title":       title,
            "company":     company,
            "date_posted": date_posted,
            "url":         job_url,
            "salary":      salary,
            "search_term": query,
            "source":      SOURCE,
        }

    # ------------------------------------------------------------------
    # Selector helpers (same pattern as IrishJobsScraper)
    # ------------------------------------------------------------------

    @staticmethod
    def _text(el, selectors: list[str]) -> Optional[str]:
        for sel in selectors:
            node = el.select_one(sel)
            if node:
                t = node.get_text(strip=True)
                if t:
                    return t
        return None

    @staticmethod
    def _attr(el, selectors: list[str], attr: str) -> Optional[str]:
        for sel in selectors:
            node = el.select_one(sel)
            if node and node.get(attr):
                return node[attr]
        return None
