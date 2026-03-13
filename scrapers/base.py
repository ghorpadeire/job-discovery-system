"""
Abstract base class for all job scrapers.

Each concrete scraper (IrishJobs, Indeed, etc.) subclasses BaseScraper and
implements:
    - build_search_url(query, page) → str
    - parse_jobs(soup, query)       → list[dict]

The base class handles:
    - Playwright page fetching with timeout handling
    - Page-level Redis caching (skip pages scraped today)
    - Pagination loop with configurable max_pages
    - Per-query debug HTML dumps
    - Structured ScraperResult return type
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from bs4 import BeautifulSoup
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

if TYPE_CHECKING:
    from core.cache import NullCache, RedisCache

from core.progress import emitter

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class ScraperResult:
    source:        str
    jobs:          list[dict] = field(default_factory=list)
    pages_scraped: int        = 0
    errors:        list[str]  = field(default_factory=list)

    @property
    def job_count(self) -> int:
        return len(self.jobs)

    def __str__(self) -> str:
        return (
            f"[{self.source}] {self.job_count} jobs / "
            f"{self.pages_scraped} pages / {len(self.errors)} errors"
        )


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class BaseScraper(ABC):
    # --- Subclasses must set these ---
    source_name:    str       = ""
    base_url:       str       = ""
    search_queries: list[str] = []
    location:       str       = "Dublin"

    # --- Subclasses may override these ---
    max_pages:     int   = 5
    request_delay: float = 2.0      # seconds between page requests

    def __init__(self, cache: "Optional[RedisCache | NullCache]" = None):
        self.cache = cache

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def build_search_url(self, query: str, page: int = 1) -> str:
        """Return the full URL for the given query and page number."""
        ...

    @abstractmethod
    def parse_jobs(self, soup: BeautifulSoup, query: str) -> list[dict]:
        """
        Extract job listings from a rendered page.

        Each dict must contain at minimum:
            title, company, source

        Optional keys: url, date_posted, salary, search_term
        """
        ...

    def has_next_page(self, soup: BeautifulSoup, page_num: int) -> bool:
        """
        Return False to stop pagination early.
        Default: always return True (pagination stops when parse_jobs returns []).
        """
        return True

    # ------------------------------------------------------------------
    # Orchestration (called by run_all.py)
    # ------------------------------------------------------------------

    async def run(self, page: Page, debug: bool = False) -> ScraperResult:
        """Run all configured queries and return a ScraperResult."""
        result = ScraperResult(source=self.source_name)

        for query in self.search_queries:
            logger.info(f"\n[{self.source_name}] '{query}' in {self.location}")
            try:
                jobs, pages = await self._scrape_query(page, query, debug)
                result.jobs.extend(jobs)
                result.pages_scraped += pages
                logger.info(f"  → {len(jobs)} jobs across {pages} page(s)")
            except Exception as exc:
                msg = f"Error on query '{query}': {exc}"
                logger.error(msg, exc_info=True)
                result.errors.append(msg)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _scrape_query(
        self, page: Page, query: str, debug: bool
    ) -> tuple[list[dict], int]:
        jobs:       list[dict] = []
        pages_done: int        = 0

        for page_num in range(1, self.max_pages + 1):
            url = self.build_search_url(query, page_num)

            # --- Redis page cache check ---
            if self.cache and self.cache.is_page_cached(url):
                logger.info(f"  CACHE HIT — skipping: {url}")
                break

            html = await self._fetch(page, url)
            if not html:
                break

            # --- Cache this page ---
            if self.cache:
                self.cache.cache_page(url)

            # --- Optional debug dump ---
            if debug:
                fname = (
                    f"debug_{self.source_name.replace('.', '_')}"
                    f"_{query.replace(' ', '_')}_p{page_num}.html"
                )
                with open(fname, "w", encoding="utf-8") as fh:
                    fh.write(html)
                logger.info(f"  Debug HTML → {fname}")

            soup     = BeautifulSoup(html, "html.parser")
            new_jobs = self.parse_jobs(soup, query)
            pages_done += 1

            logger.info(f"  Page {page_num} → {len(new_jobs)} jobs")
            emitter.emit(
                "page_done",
                source    = self.source_name,
                query     = query,
                page      = page_num,
                job_count = len(new_jobs),
            )

            if not new_jobs:
                logger.debug("  Empty page — stopping pagination")
                break

            jobs.extend(new_jobs)

            if not self.has_next_page(soup, page_num):
                break

            await asyncio.sleep(self.request_delay)

        return jobs, pages_done

    async def _fetch(self, page: Page, url: str) -> Optional[str]:
        """Navigate to URL and return rendered HTML, or None on failure."""
        try:
            logger.info(f"  GET {url}")
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            return await page.content()
        except PlaywrightTimeout:
            logger.warning(f"  Timeout: {url}")
        except Exception as exc:
            logger.warning(f"  Fetch error: {exc} ({url})")
        return None
