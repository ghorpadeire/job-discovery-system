"""
Unified scraper runner.

Execution order
---------------
1. Playwright scrapers (IrishJobs, Indeed, ITJobs, Jobs.ie) — parallel pages
2. AI careers scraper  (Jina AI + GPT-4o-mini across 100 companies) — async
3. Upsert all results into PostgreSQL
4. Mark jobs absent from this run as inactive
5. Cross-source deduplication
6. Print summary table

Usage
-----
    py run_all.py                          # all sources
    py run_all.py --sources ij             # IrishJobs only
    py run_all.py --sources indeed         # Indeed only
    py run_all.py --sources itjobs         # ITJobs.ie only
    py run_all.py --sources jobsie         # Jobs.ie only
    py run_all.py --sources ai             # AI careers scraper only
    py run_all.py --sources ij indeed      # multiple sources
    py run_all.py --debug                  # save raw HTML to disk
    py run_all.py --no-headless            # show browser windows
    py run_all.py --no-dedup               # skip deduplication pass
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright
from sqlalchemy.orm import Session

from core.logging_config import setup_logging
setup_logging()

from core.cache import get_cache
from core.config import validate as config_validate
from core.database import check_connection, get_engine
from core.deduplicator import merge_duplicates, multi_source_jobs
from core.models import Job, make_fingerprint
from core.progress import emitter
from scrapers.base import USER_AGENT, ScraperResult
from scrapers.irishjobs import IrishJobsScraper
from scrapers.indeed import IndeedScraper
from scrapers.itjobs import ITJobsScraper
from scrapers.jobs_ie import JobsIEScraper

logger = logging.getLogger(__name__)

# Warn about missing optional config at startup
for _w in config_validate():
    logger.warning(_w)

# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def upsert_jobs(engine, jobs: list[dict]) -> tuple[int, int]:
    """
    Insert new jobs or update existing ones.
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

                current = list(existing.sources or [])
                if src not in current:
                    current.append(src)
                    existing.sources = current

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
    """Mark jobs not seen in this run as inactive."""
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
    ai_count:    int = 0,
) -> None:
    W = 55
    print(f"\n{'=' * W}")
    print(f"  RUN COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─' * W}")
    for r in results:
        status = "OK" if not r.errors else f"{len(r.errors)} error(s)"
        print(f"  {r.source:<24} {r.job_count:>4} scraped  [{status}]")
    if ai_count > 0:
        print(f"  {'ai_careers':<24} {ai_count:>4} scraped  [OK]")
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
    print(f"{'=' * W}")

# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------

async def run(
    sources:     list[str],
    debug:       bool,
    headless:    bool,
    run_dedup:   bool,
) -> None:
    logger.info("Starting job scraper run")
    emitter.emit("run_start", sources=sources)

    if not check_connection():
        logger.error("Cannot connect to PostgreSQL.")
        sys.exit(1)

    engine = get_engine()
    cache  = get_cache()

    # --- Determine which Playwright scrapers to run ---
    pw_scrapers = []
    run_all_pw  = "all" in sources

    if run_all_pw or "ij" in sources:
        pw_scrapers.append(IrishJobsScraper(cache=cache))
    if run_all_pw or "indeed" in sources:
        pw_scrapers.append(IndeedScraper(cache=cache))
    if run_all_pw or "itjobs" in sources:
        pw_scrapers.append(ITJobsScraper(cache=cache))
    if run_all_pw or "jobsie" in sources:
        pw_scrapers.append(JobsIEScraper(cache=cache))

    run_ai = run_all_pw or "ai" in sources

    if not pw_scrapers and not run_ai:
        logger.error(
            f"No valid sources in: {sources}. "
            "Use: ij, indeed, itjobs, jobsie, ai, all"
        )
        sys.exit(1)

    all_jobs: list[dict]          = []
    results:  list[ScraperResult] = []
    ai_count: int                 = 0

    # -------------------------------------------------------------------
    # Phase 1 — Playwright scrapers
    # -------------------------------------------------------------------
    if pw_scrapers:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context(user_agent=USER_AGENT)

            async def _block_resources(route):
                if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                    await route.abort()
                else:
                    await route.continue_()

            for scraper in pw_scrapers:
                page = await context.new_page()
                await page.route("**/*", _block_resources)

                logger.info(f"\n{'=' * 50}")
                logger.info(f"Scraper: {scraper.source_name}")
                logger.info(f"{'=' * 50}")
                emitter.emit("source_start", source=scraper.source_name)

                result = await scraper.run(page, debug=debug)
                results.append(result)
                all_jobs.extend(result.jobs)

                emitter.emit(
                    "source_done",
                    source=scraper.source_name,
                    job_count=result.job_count,
                )
                if result.errors:
                    for err in result.errors:
                        emitter.emit("error", source=scraper.source_name, message=str(err))

                await page.close()

            await browser.close()

    # -------------------------------------------------------------------
    # Phase 2 — AI careers scraper (Jina AI + GPT-4o-mini)
    # -------------------------------------------------------------------
    if run_ai:
        logger.info(f"\n{'=' * 50}")
        logger.info("AI careers scraper — 100 company career pages")
        logger.info(f"{'=' * 50}")
        try:
            from scrapers.ai_careers import AICareersScaper
            ai_scraper = AICareersScaper()
            ai_jobs    = await ai_scraper.run_all()
            ai_count   = len(ai_jobs)
            all_jobs.extend(ai_jobs)
            logger.info(f"AI scraper: {ai_count} jobs collected")
        except Exception as exc:
            logger.error(f"AI scraper failed: {exc}", exc_info=True)

    # -------------------------------------------------------------------
    # In-memory dedup before DB write
    # -------------------------------------------------------------------
    seen:        set[str]   = set()
    unique_jobs: list[dict] = []
    for j in all_jobs:
        fp = make_fingerprint(j["title"], j["company"])
        if fp not in seen:
            seen.add(fp)
            unique_jobs.append(j)

    logger.info(
        f"\nTotal scraped: {len(all_jobs)}  |  "
        f"Unique (in-memory dedup): {len(unique_jobs)}"
    )

    if not unique_jobs:
        logger.warning("No jobs scraped. Run with --debug to inspect raw HTML.")
        return

    # -------------------------------------------------------------------
    # Persist
    # -------------------------------------------------------------------
    new_count, updated_count = upsert_jobs(engine, unique_jobs)
    removed_count             = mark_inactive(engine, unique_jobs)

    merged_count = 0
    if run_dedup:
        logger.info("\nRunning cross-source deduplication...")
        merged_count = merge_duplicates(engine)
        logger.info(f"  Merged {merged_count} duplicate(s)")

    active, inactive = get_counts(engine)
    multi_count      = len(multi_source_jobs(engine))
    cache_stats      = cache.stats()

    # -------------------------------------------------------------------
    # Auto-score all unscored (and newly inserted) jobs
    # -------------------------------------------------------------------
    if new_count > 0 or updated_count > 0:
        logger.info("\nAuto-scoring new/updated jobs…")
        try:
            from core.scorer import score_all_active_jobs
            scored = score_all_active_jobs(engine)
            logger.info(f"  Scored {scored} job(s)")
        except Exception as exc:
            logger.warning(f"  Auto-scoring failed: {exc}")

    emitter.emit(
        "run_done",
        total_jobs = len(all_jobs),
        new        = new_count,
        updated    = updated_count,
        removed    = removed_count,
        merged     = merged_count,
        active     = active,
    )

    print_summary(
        results, new_count, updated_count, removed_count,
        merged_count, active, inactive, multi_count, cache_stats,
        ai_count=ai_count,
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
        help=(
            "Scrapers to run: ij (IrishJobs), indeed, itjobs, jobsie, ai, all  "
            "(default: all)"
        ),
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
