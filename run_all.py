"""
run_all.py — Unified CLI runner for the Job Discovery System.

Usage:
  py run_all.py                      # scrape only
  py run_all.py --score              # scrape + score all new jobs
  py run_all.py --no-irishjobs       # skip IrishJobs
  py run_all.py --no-indeed          # skip Indeed
  py run_all.py --score --rescore    # scrape + re-score everything

Example (full pipeline):
  py run_all.py --score
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_all")


def _banner(text: str) -> None:
    logger.info("─" * 60)
    logger.info("  %s", text)
    logger.info("─" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Job Discovery System — unified runner")
    parser.add_argument("--score",         action="store_true", help="Score jobs after scraping")
    parser.add_argument("--rescore",       action="store_true", help="Re-score already-scored jobs")
    parser.add_argument("--no-irishjobs", action="store_true", help="Skip IrishJobs scraper")
    parser.add_argument("--no-indeed",    action="store_true", help="Skip Indeed scraper")
    parser.add_argument("--no-career-check", action="store_true",
                        help="Skip career page check (faster but less accurate scoring)")
    args = parser.parse_args()

    _banner(f"Job Discovery System — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Database connection check ────────────────────────────────────
    from core.database import check_connection, get_engine
    if not check_connection():
        logger.error("Cannot connect to database. Ensure PostgreSQL is running and DATABASE_URL is set.")
        logger.error("Check .env file: DATABASE_URL=postgresql://jobsuser:jobspass@localhost:5432/jobsdb")
        return 1

    engine = get_engine()
    logger.info("Database connection: OK")

    # ── Scrapers ─────────────────────────────────────────────────────
    total_new = 0
    total_dups = 0
    start_time = time.time()

    if not args.no_irishjobs:
        _banner("IrishJobs.ie Scraper")
        try:
            from scrapers.irishjobs import IrishJobsScraper
            scraper = IrishJobsScraper()
            new, dups = scraper.run(engine)
            total_new += new
            total_dups += dups
            logger.info("IrishJobs: +%d new | %d duplicates", new, dups)
        except Exception as exc:
            logger.error("IrishJobs scraper failed: %s", exc)

    if not args.no_indeed:
        _banner("Indeed Ireland Scraper")
        try:
            from scrapers.indeed import IndeedScraper
            scraper = IndeedScraper()
            new, dups = scraper.run(engine)
            total_new += new
            total_dups += dups
            logger.info("Indeed: +%d new | %d duplicates", new, dups)
        except Exception as exc:
            logger.error("Indeed scraper failed: %s", exc)

    if not args.no_remoteok:
        _banner("RemoteOK API Scraper")
        try:
            from scrapers.remoteok import RemoteOKScraper
            scraper = RemoteOKScraper()
            new, dups = scraper.run(engine)
            total_new += new
            total_dups += dups
            logger.info("RemoteOK: +%d new | %d duplicates", new, dups)
        except Exception as exc:
            logger.error("RemoteOK scraper failed: %s", exc)

    # ── Summary ──────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    _banner("Scraping Summary")
    logger.info("New jobs found:       %d", total_new)
    logger.info("Duplicates skipped:   %d", total_dups)
    logger.info("Time elapsed:         %.1fs", elapsed)

    # ── Deduplication pass ───────────────────────────────────────────
    try:
        from core.deduplicator import merge_duplicates
        deactivated = merge_duplicates(engine)
        if deactivated:
            logger.info("Deduplication: removed %d cross-source duplicates", deactivated)
    except Exception as exc:
        logger.warning("Deduplication pass failed: %s", exc)

    # ── Scoring ──────────────────────────────────────────────────────
    if args.score or args.rescore:
        _banner("Scoring Jobs")
        try:
            from core.scorer import score_all_active_jobs, GHOST_THRESHOLD
            check_career_page = not args.no_career_check
            scored = score_all_active_jobs(
                engine,
                check_career_page=check_career_page,
                rescore=args.rescore,
            )
            logger.info("Jobs scored: %d", scored)
        except Exception as exc:
            logger.error("Scoring failed: %s", exc)

    # ── Final statistics ─────────────────────────────────────────────
    _banner("Final Statistics")
    try:
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy import func
        from core.models import Job

        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            total   = session.query(func.count(Job.id)).scalar() or 0
            active  = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
            avg_s   = session.query(func.avg(Job.legitimacy_score)).filter(
                Job.is_active == True, Job.legitimacy_score != None
            ).scalar()
            ghosts  = session.query(func.count(Job.id)).filter(
                Job.is_active == True, Job.suspected_ghost == True
            ).scalar() or 0
            high    = session.query(func.count(Job.id)).filter(
                Job.is_active == True, Job.legitimacy_score >= 70
            ).scalar() or 0
            v_high  = session.query(func.count(Job.id)).filter(
                Job.is_active == True, Job.legitimacy_score >= 85
            ).scalar() or 0

            logger.info("Total jobs in DB:     %d", total)
            logger.info("Active jobs:          %d", active)
            logger.info("Average score:        %s", f"{avg_s:.1f}" if avg_s else "N/A (not scored)")
            logger.info("High confidence ≥70:  %d", high)
            logger.info("Very high ≥85:        %d", v_high)
            logger.info("Suspected ghosts:     %d", ghosts)
        finally:
            session.close()
    except Exception as exc:
        logger.warning("Could not compute final stats: %s", exc)

    logger.info("─" * 60)
    logger.info("  Done! Run 'py score_jobs.py' for a detailed scoring report.")
    logger.info("─" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
