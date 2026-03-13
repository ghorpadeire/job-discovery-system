"""
ITJobs.ie scraper — Ireland's dedicated IT job board.

Note: itjobs.ie applies aggressive bot-detection; if connections timeout on
your network, the scraper skips gracefully (5-second timeout) so the overall
run is not blocked. The class remains available for networks / VPNs where the
site is accessible.

Search URL pattern (when site is reachable):
  https://www.itjobs.ie/jobs?q={query}&l=Dublin&pg={page}
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from scrapers.base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

# Short timeout so a blocked site doesn't stall the whole run
_FETCH_TIMEOUT_MS = 8_000


class ITJobsScraper(BaseScraper):
    source_name    = "itjobs.ie"
    base_url       = "https://www.itjobs.ie"
    location       = "Dublin"
    max_pages      = 5
    request_delay  = 2.0

    search_queries = [
        "java developer",
        "cybersecurity analyst",
        "IT support",
        "software engineer",
        "security engineer",
        "python developer",
        "network engineer",
        "graduate developer",
    ]

    # ------------------------------------------------------------------
    # URL builder
    # ------------------------------------------------------------------

    def build_search_url(self, query: str, page: int = 1) -> str:
        import urllib.parse
        q   = urllib.parse.quote_plus(query)
        loc = urllib.parse.quote_plus(self.location)
        return f"{self.base_url}/jobs?q={q}&l={loc}&pg={page}"

    # ------------------------------------------------------------------
    # Override run() so we can skip fast if the site is unreachable
    # ------------------------------------------------------------------

    async def run(self, page: Page, debug: bool = False) -> ScraperResult:
        # Quick reachability probe before committing to all queries
        probe_url = self.build_search_url("developer", 1)
        try:
            await page.goto(
                probe_url,
                wait_until="domcontentloaded",
                timeout=_FETCH_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.warning(
                f"[{self.source_name}] Unreachable — skipping "
                f"(network/bot-detection issue). Detail: {exc}"
            )
            return ScraperResult(
                source=self.source_name,
                errors=[f"Site unreachable: {exc}"],
            )

        # Site responded — run full scrape via parent class
        return await super().run(page, debug=debug)

    # ------------------------------------------------------------------
    # Override _fetch to use shorter timeout + domcontentloaded
    # ------------------------------------------------------------------

    async def _fetch(self, page: Page, url: str) -> Optional[str]:
        try:
            logger.info(f"  GET {url}")
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_FETCH_TIMEOUT_MS,
            )
            return await page.content()
        except PlaywrightTimeout:
            logger.warning(f"  Timeout: {url}")
        except Exception as exc:
            logger.warning(f"  Fetch error: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parser
    # ------------------------------------------------------------------

    def parse_jobs(self, soup: BeautifulSoup, query: str) -> list[dict]:
        jobs: list[dict] = []

        cards = (
            soup.select("article.job")
            or soup.select("div.job-item")
            or soup.select("[data-testid='job-item']")
            or soup.select("li.job-listing")
            or soup.select(".job-card")
        )

        if not cards:
            cards = soup.select("a[href*='/job/']")
            if cards:
                return self._parse_links_fallback(cards, query)
            return []

        for card in cards:
            try:
                job = self._parse_card(card, query)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"[itjobs.ie] Card parse error: {exc}")

        return jobs

    def _parse_card(self, card, query: str) -> dict | None:
        title_el = (
            card.select_one("h2 a") or card.select_one("h3 a")
            or card.select_one(".job-title a") or card.select_one("a.title")
            or card.select_one("[class*='title'] a")
        )
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = self.base_url + href

        company_el = (
            card.select_one(".company-name") or card.select_one("[class*='company']")
            or card.select_one("span.employer")
        )
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        salary_el = (
            card.select_one(".salary") or card.select_one("[class*='salary']")
            or card.select_one("[class*='remuneration']")
        )
        salary = salary_el.get_text(strip=True) if salary_el else None

        date_el = (
            card.select_one("time") or card.select_one("[class*='date']")
            or card.select_one("[class*='posted']")
        )
        date_text = ""
        if date_el:
            date_text = date_el.get("datetime", "") or date_el.get_text(strip=True)

        return {
            "title":       title,
            "company":     company,
            "url":         href or self.base_url,
            "salary":      salary,
            "date_posted": self._parse_date(date_text),
            "source":      self.source_name,
            "search_term": query,
        }

    def _parse_links_fallback(self, links, query: str) -> list[dict]:
        jobs = []
        for a in links:
            title = a.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            href = a.get("href", "")
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
            soup.select_one("a[aria-label='Next']")
            or soup.select_one("a.pagination-next")
            or soup.select_one("[class*='pagination'] a[rel='next']")
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
            return (datetime.utcnow() - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\s*hour", text)
        if m:
            return datetime.utcnow().strftime("%Y-%m-%d")
        return text[:20]
