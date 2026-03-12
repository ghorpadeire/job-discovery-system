"""
IrishJobs.ie scraper.

Current page structure (2025+):
  Card:    article[data-testid="job-item"]
  Title:   a[data-testid="job-item-title"]
  Company: span[data-at="job-item-company-name"]
  Salary:  span[data-at="job-item-salary-info"]
  Date:    span[data-at="job-item-timeago"]
  URL:     href on the title anchor (relative → prepend BASE)
"""
import logging
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

SOURCE = "irishjobs.ie"
BASE   = "https://www.irishjobs.ie"


class IrishJobsScraper(BaseScraper):
    source_name    = SOURCE
    base_url       = BASE
    search_queries = [
        "java developer",
        "cybersecurity",
        "python developer",
        "software engineer",
        "network security",
    ]
    location      = "Dublin"
    max_pages     = 4
    request_delay = 2.0

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, page: int = 1) -> str:
        slug = query.strip().replace(" ", "-").lower()
        loc  = self.location.strip().replace(" ", "-").lower()
        url  = f"{BASE}/jobs/{slug}/in-{loc}/"
        if page > 1:
            url += f"?page={page}"
        return url

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_jobs(self, soup: BeautifulSoup, query: str) -> list[dict]:
        cards = soup.select('article[data-testid="job-item"]')
        if not cards:
            logger.debug("  No cards found with primary selector")
            return []

        logger.debug(f"  Found {len(cards)} cards")
        jobs = []
        for card in cards:
            try:
                job = self._extract(card, query)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"  Card parse error: {exc}")
        return jobs

    def _extract(self, card, query: str) -> Optional[dict]:
        title_el = card.select_one('a[data-testid="job-item-title"]')
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        href    = title_el.get("href", "")
        job_url = urljoin(BASE, href) if href else ""

        company_el = card.select_one('[data-at="job-item-company-name"]')
        company    = company_el.get_text(strip=True) if company_el else "Unknown"

        salary_el = card.select_one('[data-at="job-item-salary-info"]')
        salary    = salary_el.get_text(strip=True) if salary_el else None

        date_el     = card.select_one('[data-at="job-item-timeago"]')
        date_posted = date_el.get_text(strip=True) if date_el else ""

        return {
            "title":       title,
            "company":     company,
            "date_posted": date_posted,
            "url":         job_url,
            "salary":      salary,
            "search_term": query,
            "source":      SOURCE,
        }
