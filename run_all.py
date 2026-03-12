"""
Unified scraper runner.

Execution order
---------------
1. Start Playwright browser (single shared instance, one page per scraper)
2. Run IrishJobs.ie scraper
3. Run Indeed Ireland scraper
4. Upsert all results into PostgreSQL
5. Mark jobs absent from this run as inactive
6. Run cross-source deduplication (merge normalised-title duplicates)
7. Print summary table

Usage
-----
    python run_all.py                   # normal headless run
    python run_all.py --debug           # save raw HTML per source/query/page
    python run_all.py --no-headless     # show browser windows
    python run_all.py --no-dedup        # skip deduplication pass
    python run_all.py --sources ij      # run IrishJobs only
    python run_all.py --sources indeed  # run Indeed only
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional

# Fix Unicode output on Windows (cp1252 terminal can't handle box-drawing chars)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright
from sqlalchemy.orm import Session

from core.cache import get_cache
from core.database import check_connection, get_engine
from core.deduplicator import merge_duplicates, multi_source_jobs
from core.models import Job, make_fingerprint
from scrapers.base import USER_AGENT, ScraperResult
from scrapers.irishjobs import IrishJobsScraper
from scrapers.indeed import IndeedScraper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def upsert_jobs(engine, jobs: list[dict]) -> tuple[int, int]:
    """
    Insert new jobs or update existing ones.

    For existing jobs (matched by fingerprint):
      - Updates last_seen and is_active
      - Merges the new source into the sources[] list
      - Back-fills url / salary if previously missing

    Returns (new_count, updated_count).
    """
    new_count = updated_count = 0
    now       = datetime.utcnow()

    with Session(engine) as session:
        for data in jobs:
            fp       = make_fingerprint(data["title"], data["company"])
            existing = session.query(Job).filter_by(fingerprint=fp).first()
            src      = data.get("source", "unknown")

            if existing:
                existing.last_seen = now
                existing.is_active = True

                # Merge source
                current = list(existing.sources or [])
                if src not in current:
                    current.append(src)
                    existing.sources = current

                # Back-fill missing fields
                if data.get("url")    and not existing.url:    existing.url    = data["url"]
                if data.get("salary") and not existing.salary: existing.salary = data["salary"]
                updated_count += 1

            else:
                session.add(Job(
                    title       = data["title"],
                    company     = data["company"],
                    date_posted = data.get("date_posted") or "",
                    url         = data.get("url")         or "",
                    salary      = data.get("salary"),
                    source      = src,
                    sources     = [src],
                    search_term = data.get("search_term") or "",
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
    Any previously active job NOT present in this run's results
    is marked is_active=False (it disappeared from the site).
    Returns the count of deactivated jobs.
    """
    seen = {make_fingerprint(j["title"], j["company"]) for j in scraped_jobs}
    deactivated = 0

    with Session(engine) as session:
        for job in session.query(Job).filter_by(is_active=True).all():
            if job.fingerprint not in seen:
                job.is_active = False
                deactivated  += 1
        session.commit()

    return deactivated


def get_counts(engine) -> tuple[int, int]:
    with Session(engine) as session:
        active   = session.query(Job).filter_by(is_active=True).count()
        inactive = session.query(Job).filter_by(is_active=False).count()
    return active, inactive

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results_table(engine) -> None:
    with Session(engine) as session:
        jobs = (
            session.query(Job)
            .filter_by(is_active=True)
            .order_by(Job.first_seen.desc())
            .all()
        )

    if not jobs:
        print("  (no active jobs)")
        return

    W = 95
    print(f"\n{'─' * W}")
    print(f"{'TITLE':<36} {'COMPANY':<22} {'SOURCES':<24} {'DATE':<12} {'SALARY'}")
    print(f"{'─' * W}")
    for j in jobs:
        title   = (j.title[:34]   + "..") if len(j.title)   > 36 else j.title
        company = (j.company[:20] + "..") if len(j.company) > 22 else j.company
        sources = ", ".join(sorted(set(j.sources or [j.source or "?"])))
        if len(sources) > 24: sources = sources[:22] + ".."
        date    = (j.date_posted or "—")[:11]
        salary  = (j.salary      or "—")[:18]
        print(f"{title:<36} {company:<22} {sources:<24} {date:<12} {salary}")
    print(f"{'─' * W}")


def print_summary(
    results:     list[ScraperResult],
    new:         int,
    updated:     int,
    removed:     int,
    merged:      int,
    active:      int,
    inactive:    int,
    multi:       int,
    cache_stats: dict,
) -> None:
    W = 50
    print(f"\n{'═' * W}")
    print(f"  RUN COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─' * W}")
    for r in results:
        status = "OK" if not r.errors else f"{len(r.errors)} error(s)"
        print(f"  {r.source:<20} {r.job_count:>4} scraped  [{status}]")
    print(f"{'─' * W}")
    print(f"  New jobs inserted   : {new}")
    print(f"  Existing updated    : {updated}")
    print(f"  Jobs removed        : {removed}")
    print(f"  Duplicates merged   : {merged}")
    print(f"{'─' * W}")
    print(f"  Total active        : {active}")
    print(f"  Total inactive      : {inactive}")
    print(f"  Multi-source jobs   : {multi}")
    if cache_stats:
        hits   = cache_stats.get("hits",   0)
        misses = cache_stats.get("misses", 0)
        print(f"  Cache hits/misses   : {hits}/{misses}")
    print(f"{'═' * W}")

# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------

async def run(
    sources:     list[str],
    debug:       bool,
    headless:    bool,
    run_dedup:   bool,
) -> None:
    # --- Pre-flight checks ---
    logger.info("Starting job scraper run")

    if not check_connection():
        logger.error(
            "Cannot connect to PostgreSQL. "
            "Is Docker running? Try: docker compose up -d"
        )
        sys.exit(1)

    engine = get_engine()
    cache  = get_cache()

    # --- Instantiate requested scrapers ---
    all_scrapers = []
    if "ij" in sources or "all" in sources:
        all_scrapers.append(IrishJobsScraper(cache=cache))
    if "indeed" in sources or "all" in sources:
        all_scrapers.append(IndeedScraper(cache=cache))

    if not all_scrapers:
        logger.error(f"No valid sources in: {sources}. Use: ij, indeed, all")
        sys.exit(1)

    all_jobs: list[dict]          = []
    results:  list[ScraperResult] = []

    # --- Run scrapers ---
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=USER_AGENT)

        # Block images/media/fonts — faster, no functional impact on text scraping
        async def _block_resources(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                await route.abort()
            else:
                await route.continue_()

        for scraper in all_scrapers:
            page = await context.new_page()
            await page.route("**/*", _block_resources)

            logger.info(f"\n{'═' * 50}")
            logger.info(f"Scraper: {scraper.source_name}")
            logger.info(f"{'═' * 50}")

            result = await scraper.run(page, debug=debug)
            results.append(result)
            all_jobs.extend(result.jobs)

            await page.close()

        await browser.close()

    # --- Deduplicate scraped results in memory before DB writes ---
    seen:        set[str]   = set()
    unique_jobs: list[dict] = []
    for j in all_jobs:
        fp = make_fingerprint(j["title"], j["company"])
        if fp not in seen:
            seen.add(fp)
            unique_jobs.append(j)

    total_raw    = len(all_jobs)
    total_unique = len(unique_jobs)
    logger.info(
        f"\nTotal scraped: {total_raw}  |  Unique (in-memory dedup): {total_unique}"
    )

    if not unique_jobs:
        logger.warning(
            "No jobs scraped. Run with --debug to inspect the raw HTML."
        )
        return

    # --- Persist ---
    new_count, updated_count = upsert_jobs(engine, unique_jobs)
    removed_count             = mark_inactive(engine, unique_jobs)

    # --- Cross-source deduplication (normalised title+company merge) ---
    merged_count = 0
    if run_dedup:
        logger.info("\nRunning cross-source deduplication...")
        merged_count = merge_duplicates(engine)
        logger.info(f"  Merged {merged_count} duplicate(s)")

    # --- Final counts ---
    active, inactive = get_counts(engine)
    multi_count      = len(multi_source_jobs(engine))
    cache_stats      = cache.stats()

    print_summary(
        results, new_count, updated_count, removed_count,
        merged_count, active, inactive, multi_count, cache_stats,
    )
    print_results_table(engine)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run all job scrapers and update the database."
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=["all"],
        metavar="SRC",
        help="Scrapers to run: ij (IrishJobs), indeed, all  (default: all)",
    )
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
        help="Show browser windows",
    )
    parser.add_argument(
        "--no-dedup",
        dest="run_dedup",
        action="store_false",
        default=True,
        help="Skip the cross-source deduplication pass",
    )
    args = parser.parse_args()
    asyncio.run(run(
        sources   = [s.lower() for s in args.sources],
        debug     = args.debug,
        headless  = args.headless,
        run_dedup = args.run_dedup,
    ))


if __name__ == "__main__":
    main()
