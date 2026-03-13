"""
Jobs.ie scraper — Ireland's second-largest general job board.

Search URL pattern:
  https://www.jobs.ie/search/?q={query}&location=Dublin&page={page}

Selectors target the current 2024/2025 listing card structure.
Run with --debug to dump raw HTML if selectors break after a site redesign.
"""
import logging
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class JobsIEScraper(BaseScraper):
    source_name    = "jobs.ie"
    base_url       = "https://www.jobs.ie"
    location       = "Dublin"
    max_pages      = 5
    request_delay  = 2.0

    search_queries = [
        "java developer",
        "cybersecurity",
        "IT support",
        "software engineer",
        "security analyst",
        "python developer",
        "helpdesk",
        "graduate IT",
    ]

    # ------------------------------------------------------------------
    # URL builder
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, page: int = 1) -> str:
        import urllib.parse
        q   = urllib.parse.quote_plus(query)
        loc = urllib.parse.quote_plus(self.location)
        return f"{self.base_url}/search/?q={q}&location={loc}&page={page}"

    # ------------------------------------------------------------------
    # Parser
    # ------------------------------------------------------------------

    def parse_jobs(self, soup: BeautifulSoup, query: str) -> list[dict]:
        jobs: list[dict] = []

        # jobs.ie listing cards — try multiple selector strategies
        cards = (
            soup.select("article.job-item")          # primary 2024
            or soup.select("div[class*='job-result']")
            or soup.select("li[class*='job']")
            or soup.select(".job-listing-item")
            or soup.select("[data-testid='job-card']")
        )

        if not cards:
            # Broad fallback: links to /jobs/ detail pages
            links = soup.select("a[href*='/jobs/']")
            if links:
                return self._parse_links_fallback(links, query)
            logger.debug("[jobs.ie] No job cards found")
            return []

        for card in cards:
            try:
                job = self._parse_card(card, query)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"[jobs.ie] Card parse error: {exc}")

        return jobs

    def _parse_card(self, card, query: str) -> dict | None:
        # Title
        title_el = (
            card.select_one("h2 a")
            or card.select_one("h3 a")
            or card.select_one(".job-title a")
            or card.select_one("[class*='title'] a")
            or card.select_one("a.job-link")
        )
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        # URL
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = self.base_url + href
        if not href:
            href = self.base_url

        # Company
        company_el = (
            card.select_one(".company-name")
            or card.select_one("[class*='company']")
            or card.select_one(".employer-name")
            or card.select_one("[class*='employer']")
        )
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        # Salary
        salary_el = (
            card.select_one("[class*='salary']")
            or card.select_one("[class*='remuneration']")
        )
        salary = salary_el.get_text(strip=True) if salary_el else None

        # Date
        date_el = (
            card.select_one("time")
            or card.select_one("[class*='date']")
            or card.select_one("[class*='posted']")
        )
        date_text = ""
        if date_el:
            date_text = date_el.get("datetime", "") or date_el.get_text(strip=True)
        date_str = self._parse_date(date_text)

        return {
            "title":       title,
            "company":     company,
            "url":         href,
            "salary":      salary,
            "date_posted": date_str,
            "source":      self.source_name,
            "search_term": query,
        }

    def _parse_links_fallback(self, links, query: str) -> list[dict]:
        jobs = []
        seen = set()
        for a in links:
            title = a.get_text(strip=True)
            href  = a.get("href", "")
            if not title or len(title) < 5 or href in seen:
                continue
            seen.add(href)
            if href and not href.startswith("http"):
                href = self.base_url + href
            jobs.append({
                "title":       title,
                "company":     "Unknown",
                "url":         href or self.base_url,
                "salary":      None,
                "date_posted": "",
                "source":      self.source_name,
                "search_term": query,
            })
        return jobs

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def has_next_page(self, soup: BeautifulSoup, page_num: int) -> bool:
        next_btn = (
            soup.select_one("a[aria-label='Next page']")
            or soup.select_one("a[rel='next']")
            or soup.select_one(".pagination-next")
            or soup.find("a", string=re.compile(r"Next|>", re.I))
        )
        return next_btn is not None

    # ------------------------------------------------------------------
    # Date helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(text: str) -> str:
        if not text:
            return ""
        text = text.strip().lower()
        try:
            from dateutil import parser as dparser
            return dparser.parse(text).strftime("%Y-%m-%d")
        except Exception:
            pass
        if "today" in text or "just now" in text:
            return datetime.utcnow().strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\s*day", text)
        if m:
            d = datetime.utcnow() - timedelta(days=int(m.group(1)))
            return d.strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\s*hour", text)
        if m:
            return datetime.utcnow().strftime("%Y-%m-%d")
        return text[:20]
