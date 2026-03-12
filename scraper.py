"""
IrishJobs.ie Scraper
====================
Searches for Java Developer and Cybersecurity roles in Dublin.
Stores results in SQLite via SQLAlchemy with deduplication and
active/inactive tracking.

Usage:
    python scraper.py               # normal run
    python scraper.py --debug       # dump raw HTML for selector inspection
    python scraper.py --no-headless # show browser window (useful for debugging)
"""

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright
from sqlalchemy.orm import Session

from models import Job, init_db, make_fingerprint

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.irishjobs.ie"

SEARCH_QUERIES = [
    "java developer",
    "cybersecurity",
]

LOCATION        = "Dublin"
DB_PATH         = "jobs.db"
MAX_PAGES       = 5       # safety cap — each page ~20 results
REQUEST_DELAY   = 2.0     # seconds between page requests (be polite)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def build_search_url(query: str, location: str = "Dublin", page: int = 1) -> str:
    """
    IrishJobs.ie primary search URL format.
    Falls back to ShowResults.aspx if the slug path doesn't work.
    """
    slug = query.strip().replace(" ", "-").lower()
    loc  = location.strip().replace(" ", "-").lower()
    url  = f"{BASE_URL}/jobs/{slug}/in-{loc}/"
    if page > 1:
        url += f"?page={page}"
    return url


def build_fallback_url(query: str, page: int = 1) -> str:
    """ShowResults.aspx fallback (older IrishJobs search)."""
    params = {
        "Keywords": query,
        "Location": "101",   # 101 = Dublin in IrishJobs location codes
        "radius":   "10",
        "SortType": "0",
    }
    if page > 1:
        params["Page"] = page
    return f"{BASE_URL}/ShowResults.aspx?{urlencode(params)}"

# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

# Ordered list of CSS selector patterns for job cards.
# IrishJobs.ie has changed its markup across versions; we try all of them.
JOB_CARD_SELECTORS = [
    "li[id^='job_']",                    # e.g. id="job_1234567"
    "article[class*='job']",
    "div[class*='job-result']",
    "div[class*='jobResult']",
    "div[class*='result-item']",
    "div[class*='ResultItem']",
    "[data-job-id]",
    "ul.results li",                      # plain <li> inside results list
]

TITLE_SELECTORS = [
    "h2 a", "h3 a",
    "[class*='title'] a",
    "[class*='job-title'] a",
    "[class*='jobTitle'] a",
    "a[class*='title']",
    "h2", "h3",
    "[class*='title']",
]

COMPANY_SELECTORS = [
    "[class*='company']",
    "[class*='employer']",
    "[class*='CompanyName']",
    "[class*='recruiter']",
    "span[class*='company']",
    "p[class*='company']",
]

DATE_SELECTORS = [
    "time",
    "[datetime]",
    "[class*='date']",
    "[class*='Date']",
    "[class*='posted']",
    "[class*='ago']",
]

SALARY_SELECTORS = [
    "[class*='salary']",
    "[class*='Salary']",
    "[class*='compensation']",
    "[class*='pay']",
    "[class*='wage']",
    "[class*='rate']",
]


def _first_text(card, selectors: list[str]) -> Optional[str]:
    """Try each CSS selector; return text of first match, or None."""
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return None


def _first_attr(card, selectors: list[str], attr: str) -> Optional[str]:
    """Try each CSS selector; return attribute value of first match, or None."""
    for sel in selectors:
        el = card.select_one(sel)
        if el and el.get(attr):
            return el[attr]
    return None


def parse_jobs(soup: BeautifulSoup, search_term: str) -> list[dict]:
    """
    Extract all job listings from a rendered page.
    Tries multiple selector patterns to handle different markup versions.
    """
    # Find job card containers
    cards = []
    for sel in JOB_CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            logger.debug(f"  Matched {len(cards)} cards with selector: {sel!r}")
            break

    if not cards:
        # Last-resort: collect every link whose href looks like a job URL
        logger.debug("  No card containers found — falling back to job-link scan")
        cards = soup.find_all("a", href=re.compile(r"/(job|jobs)/\d+", re.I))

    jobs = []
    for card in cards:
        try:
            job = _extract_card(card, search_term)
            if job:
                jobs.append(job)
        except Exception as exc:
            logger.debug(f"  Skipped a card: {exc}")

    return jobs


def _extract_card(card, search_term: str) -> Optional[dict]:
    """Pull structured data from a single job card element."""
    # --- Title ---
    title = _first_text(card, TITLE_SELECTORS)
    if not title and card.name == "a":
        title = card.get_text(strip=True)
    if not title or len(title) < 3:
        return None

    # --- URL ---
    href = _first_attr(card, ["h2 a", "h3 a", "[class*='title'] a", "a"], "href")
    if not href and card.name == "a":
        href = card.get("href", "")
    job_url = urljoin(BASE_URL, href) if href else ""

    # --- Company ---
    company = _first_text(card, COMPANY_SELECTORS) or "Unknown"

    # --- Date posted ---
    date_posted = (
        _first_attr(card, DATE_SELECTORS, "datetime")
        or _first_text(card, DATE_SELECTORS)
        or ""
    )

    # --- Salary ---
    salary = _first_text(card, SALARY_SELECTORS)

    return {
        "title":       title,
        "company":     company,
        "date_posted": date_posted,
        "url":         job_url,
        "salary":      salary,
        "search_term": search_term,
        "source":      "irishjobs.ie",
    }


def get_next_page_url(soup: BeautifulSoup, current_url: str, page_num: int) -> Optional[str]:
    """Return the URL for the next results page, or None if not found."""
    # Explicit next link
    for sel in ["a[rel='next']", "a.next", "[class*='next'] a", "[aria-label='Next'] a",
                "[aria-label='Next page'] a"]:
        el = soup.select_one(sel)
        if el and el.get("href"):
            return urljoin(BASE_URL, el["href"])

    # Replace existing page parameter
    if "page=" in current_url:
        return re.sub(r"page=\d+", f"page={page_num}", current_url)

    # Append page parameter
    if page_num == 2:
        sep = "&" if "?" in current_url else "?"
        return f"{current_url}{sep}page={page_num}"

    return None

# ---------------------------------------------------------------------------
# Playwright scraping
# ---------------------------------------------------------------------------

async def load_page(page, url: str, fallback_url: str) -> Optional[str]:
    """
    Navigate to `url`; fall back to `fallback_url` on timeout.
    Returns rendered HTML or None on total failure.
    """
    for attempt_url in [url, fallback_url]:
        try:
            logger.info(f"  GET {attempt_url}")
            await page.goto(attempt_url, wait_until="networkidle", timeout=30_000)
            # Wait for job card to appear (up to 8 s)
            for sel in JOB_CARD_SELECTORS:
                try:
                    await page.wait_for_selector(sel, timeout=8_000)
                    break
                except PlaywrightTimeout:
                    continue
            return await page.content()
        except PlaywrightTimeout:
            logger.warning(f"  Timeout on {attempt_url}")
        except Exception as exc:
            logger.warning(f"  Error loading {attempt_url}: {exc}")

    logger.error(f"  Both URLs failed — skipping this query")
    return None


async def scrape_query(page, query: str, debug: bool = False) -> list[dict]:
    """Scrape all pages for a single search query."""
    all_jobs: list[dict] = []
    current_url = build_search_url(query, LOCATION, page=1)
    fallback_url = build_fallback_url(query, page=1)

    html = await load_page(page, current_url, fallback_url)
    if not html:
        return all_jobs

    if debug:
        fname = f"debug_{query.replace(' ', '_')}_p1.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"  Debug HTML saved → {fname}")

    soup     = BeautifulSoup(html, "html.parser")
    jobs     = parse_jobs(soup, query)
    all_jobs.extend(jobs)
    logger.info(f"  Page 1 → {len(jobs)} jobs")

    # Paginate
    for page_num in range(2, MAX_PAGES + 1):
        next_url = get_next_page_url(soup, current_url, page_num)
        if not next_url:
            break

        await asyncio.sleep(REQUEST_DELAY)
        try:
            logger.info(f"  GET {next_url}")
            await page.goto(next_url, wait_until="networkidle", timeout=30_000)
            html = await page.content()
        except (PlaywrightTimeout, Exception) as exc:
            logger.warning(f"  Pagination error on page {page_num}: {exc}")
            break

        soup     = BeautifulSoup(html, "html.parser")
        new_jobs = parse_jobs(soup, query)
        if not new_jobs:
            logger.info(f"  Page {page_num} → 0 jobs (stopping)")
            break

        logger.info(f"  Page {page_num} → {len(new_jobs)} jobs")
        all_jobs.extend(new_jobs)

    return all_jobs

# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def upsert_jobs(engine, scraped_jobs: list[dict]) -> tuple[int, int]:
    """
    Insert new jobs, update `last_seen` + `is_active` for existing ones.
    Returns (new_count, updated_count).
    """
    new_count     = 0
    updated_count = 0
    now           = datetime.utcnow()

    with Session(engine) as session:
        for data in scraped_jobs:
            fp       = make_fingerprint(data["title"], data["company"])
            existing = session.query(Job).filter_by(fingerprint=fp).first()

            if existing:
                existing.last_seen = now
                existing.is_active = True
                # Backfill URL if we now have one
                if data.get("url") and not existing.url:
                    existing.url = data["url"]
                # Backfill salary if we now have one
                if data.get("salary") and not existing.salary:
                    existing.salary = data["salary"]
                updated_count += 1
            else:
                session.add(Job(
                    title       = data["title"],
                    company     = data["company"],
                    date_posted = data.get("date_posted") or "",
                    url         = data.get("url") or "",
                    salary      = data.get("salary"),
                    source      = data.get("source", "irishjobs.ie"),
                    search_term = data.get("search_term", ""),
                    fingerprint = fp,
                    first_seen  = now,
                    last_seen   = now,
                    is_active   = True,
                ))
                new_count += 1

        session.commit()

    return new_count, updated_count


def mark_inactive(engine, scraped_jobs: list[dict]) -> int:
    """
    Any job in the DB that was previously active but NOT seen in this run
    gets marked is_active=False (i.e. it was removed from the site).
    Returns count of jobs deactivated.
    """
    seen_fps = {
        make_fingerprint(j["title"], j["company"]) for j in scraped_jobs
    }

    deactivated = 0
    with Session(engine) as session:
        active_jobs = session.query(Job).filter_by(is_active=True).all()
        for job in active_jobs:
            if job.fingerprint not in seen_fps:
                job.is_active = False
                deactivated  += 1
        session.commit()

    return deactivated


def count_jobs(engine) -> tuple[int, int]:
    """Return (active_count, inactive_count)."""
    with Session(engine) as session:
        active   = session.query(Job).filter_by(is_active=True).count()
        inactive = session.query(Job).filter_by(is_active=False).count()
    return active, inactive

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_active_jobs(engine) -> None:
    with Session(engine) as session:
        jobs = (
            session.query(Job)
            .filter_by(is_active=True)
            .order_by(Job.first_seen.desc())
            .all()
        )

    if not jobs:
        print("  (no active jobs in database)")
        return

    W = 80
    print(f"\n{'─' * W}")
    print(f"{'TITLE':<38} {'COMPANY':<22} {'POSTED':<12} {'SALARY'}")
    print(f"{'─' * W}")
    for j in jobs:
        title   = (j.title[:36]   + "..") if len(j.title)   > 38 else j.title
        company = (j.company[:20] + "..") if len(j.company) > 22 else j.company
        date    = (j.date_posted or "—")[:11]
        salary  = (j.salary or "—")[:20]
        print(f"{title:<38} {company:<22} {date:<12} {salary}")
    print(f"{'─' * W}")


def print_summary(new: int, updated: int, removed: int, active: int, inactive: int) -> None:
    width = 44
    print(f"\n{'═' * width}")
    print(f"  SCRAPE COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─' * width}")
    print(f"  New jobs found   : {new}")
    print(f"  Existing updated : {updated}")
    print(f"  Jobs removed     : {removed}")
    print(f"{'─' * width}")
    print(f"  Total active     : {active}")
    print(f"  Total inactive   : {inactive}")
    print(f"{'═' * width}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(debug: bool = False, headless: bool = True) -> None:
    logger.info("IrishJobs.ie Scraper starting")
    logger.info(f"Queries: {SEARCH_QUERIES}  |  Location: {LOCATION}")

    engine = init_db(DB_PATH)
    logger.info(f"Database: {DB_PATH}")

    all_scraped: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)
        page    = await context.new_page()

        # Block images/fonts/media to speed up scraping
        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )

        for query in SEARCH_QUERIES:
            logger.info(f"\n{'─' * 50}")
            logger.info(f"Query: '{query}' in {LOCATION}")
            logger.info(f"{'─' * 50}")
            try:
                jobs = await scrape_query(page, query, debug=debug)
                all_scraped.extend(jobs)
                logger.info(f"Subtotal for '{query}': {len(jobs)} raw listings")
            except Exception as exc:
                logger.error(f"Unexpected error scraping '{query}': {exc}", exc_info=True)

            await asyncio.sleep(REQUEST_DELAY)

        await browser.close()

    # --- De-duplicate scraped results before touching the DB ---
    seen: set[str] = set()
    unique_jobs: list[dict] = []
    for j in all_scraped:
        fp = make_fingerprint(j["title"], j["company"])
        if fp not in seen:
            seen.add(fp)
            unique_jobs.append(j)

    total_raw    = len(all_scraped)
    total_unique = len(unique_jobs)
    logger.info(f"\nTotal scraped: {total_raw}  |  Unique (deduped): {total_unique}")

    if not unique_jobs:
        logger.warning("No jobs scraped this run.")
        logger.warning(
            "Tip: run with --debug to save the raw HTML, then inspect "
            "it to find the correct CSS selectors for the current site markup."
        )
        return

    new_count, updated_count = upsert_jobs(engine, unique_jobs)
    removed_count             = mark_inactive(engine, unique_jobs)
    active, inactive          = count_jobs(engine)

    print_summary(new_count, updated_count, removed_count, active, inactive)
    print_active_jobs(engine)


def main() -> None:
    parser = argparse.ArgumentParser(description="IrishJobs.ie scraper")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save raw HTML to disk for selector inspection",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        default=True,
        help="Show the browser window (useful for debugging)",
    )
    args = parser.parse_args()
    asyncio.run(run(debug=args.debug, headless=args.headless))


if __name__ == "__main__":
    main()
