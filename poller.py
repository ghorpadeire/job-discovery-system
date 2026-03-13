"""
poller.py — Local background poller that runs forever.

Every 30 minutes:
  1. Scrapes IrishJobs + Indeed for new jobs
  2. Scores all unscored jobs
  3. Sends Telegram alerts for every unnotified job with score ≥ 40

This is the LOCAL replacement for GitHub Actions scheduling.
Run it in a separate terminal and leave it running all day.

Usage:
  py poller.py                   # scrape + score + alert every 30 min
  py poller.py --interval 15     # run every 15 minutes instead
  py poller.py --alert-only      # just send alerts, skip scraping
  py poller.py --once            # run once and exit (useful for testing)
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("poller")

DEFAULT_INTERVAL_MINUTES = 30


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _separator(label: str = ""):
    width = 60
    if label:
        pad = (width - len(label) - 2) // 2
        logger.info("─" * pad + f" {label} " + "─" * pad)
    else:
        logger.info("─" * width)


def _scrape_and_score() -> dict:
    """Run scrapers + scorer. Returns stats dict."""
    stats = {"new": 0, "dups": 0, "scored": 0, "errors": []}
    try:
        from core.database import get_engine
        from scrapers.irishjobs import IrishJobsScraper
        from scrapers.indeed import IndeedScraper
        from core.scorer import score_all_active_jobs

        engine = get_engine()

        _separator("Scraping IrishJobs.ie")
        try:
            new, dups = IrishJobsScraper().run(engine)
            stats["new"] += new
            stats["dups"] += dups
            logger.info("IrishJobs: +%d new  |  %d duplicates", new, dups)
        except Exception as exc:
            logger.error("IrishJobs scraper error: %s", exc)
            stats["errors"].append(f"IrishJobs: {exc}")

        _separator("Scraping Indeed Ireland")
        try:
            new, dups = IndeedScraper().run(engine)
            stats["new"] += new
            stats["dups"] += dups
            logger.info("Indeed: +%d new  |  %d duplicates", new, dups)
        except Exception as exc:
            logger.error("Indeed scraper error: %s", exc)
            stats["errors"].append(f"Indeed: {exc}")

        _separator("Scoring new jobs")
        try:
            scored = score_all_active_jobs(engine, check_career_page=False, rescore=False)
            stats["scored"] = scored
            logger.info("Scored %d jobs", scored)
        except Exception as exc:
            logger.error("Scorer error: %s", exc)
            stats["errors"].append(f"Scorer: {exc}")

    except Exception as exc:
        logger.error("Fatal scrape/score error: %s", exc)
        stats["errors"].append(str(exc))

    return stats


def _send_telegram_alerts() -> int:
    """Send Telegram alerts for all unnotified jobs. Returns count sent."""
    try:
        import asyncio as _asyncio
        from tg_notify import main_async
        return _asyncio.run(main_async("alerts"))
    except Exception as exc:
        logger.error("Telegram alert error: %s", exc)
        return 0


def _send_startup_message():
    """Send a Telegram message when the poller starts."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    try:
        import urllib.request, json
        msg = (
            "🤖 <b>JobScout Poller Started</b>\n\n"
            f"Checking every {DEFAULT_INTERVAL_MINUTES} minutes.\n"
            "You'll get a message every time new jobs are found.\n\n"
            "Send /status to your bot to check stats anytime."
        )
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Startup message sent to Telegram")
    except Exception as exc:
        logger.warning("Could not send startup message: %s", exc)


def _send_no_jobs_message(new_found: int = 0):
    """Send a 'nothing new this cycle' message to Telegram."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import urllib.request, json
        now_str = datetime.now(timezone.utc).strftime("%H:%M")
        if new_found > 0:
            # Jobs were scraped but all scored too low (ghosts/low quality)
            detail = f"{new_found} listing(s) found but all filtered out as low quality or ghost jobs."
        else:
            detail = "No new listings found on IrishJobs.ie or Indeed this cycle."
        msg = (
            f"\u23f0 <b>No new jobs — {now_str} UTC</b>\n"
            f"{detail}\n"
            f"<i>Next check in 30 minutes.</i>"
        )
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Sent 'no new jobs' message to Telegram")
    except Exception as exc:
        logger.warning("Could not send no-jobs message: %s", exc)


def run_one_cycle(alert_only: bool = False) -> dict:
    """Run a single poll cycle. Returns stats."""
    _separator(f"Poll Cycle — {_now()}")

    stats = {"new": 0, "dups": 0, "scored": 0, "alerts_sent": 0, "errors": []}

    if not alert_only:
        scrape_stats = _scrape_and_score()
        stats.update(scrape_stats)

    _separator("Sending Telegram Alerts")
    sent = _send_telegram_alerts()
    stats["alerts_sent"] = sent

    if sent > 0:
        logger.info("✅ Sent %d Telegram alert(s)", sent)
    else:
        logger.info("ℹ️  No new jobs to alert about this cycle")
        # Notify Telegram so you know the poller is still alive
        _send_no_jobs_message(stats.get('new', 0))

    _separator("Cycle Summary")
    logger.info("  New jobs found:     %d", stats["new"])
    logger.info("  Duplicates skipped: %d", stats["dups"])
    logger.info("  Jobs scored:        %d", stats["scored"])
    logger.info("  Telegram alerts:    %d", stats["alerts_sent"])
    if stats["errors"]:
        for e in stats["errors"]:
            logger.warning("  Error: %s", e)

    return stats


def main():
    parser = argparse.ArgumentParser(description="JobScout local background poller")
    parser.add_argument("--interval",   type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help=f"Minutes between scrapes (default: {DEFAULT_INTERVAL_MINUTES})")
    parser.add_argument("--alert-only", action="store_true",
                        help="Skip scraping, only send alerts for existing unnotified jobs")
    parser.add_argument("--once",       action="store_true",
                        help="Run once and exit")
    args = parser.parse_args()

    # ── Check DB ────────────────────────────────────────────────────
    from core.database import check_connection
    if not check_connection():
        logger.error("Cannot connect to database. Is PostgreSQL running?")
        sys.exit(1)

    interval_sec = args.interval * 60

    print()
    print("=" * 60)
    print(f"  JobScout Poller — running every {args.interval} minutes")
    print(f"  Scraping: {'NO (alert-only mode)' if args.alert_only else 'YES'}")
    print(f"  Press CTRL+C to stop")
    print("=" * 60)
    print()

    if not args.once:
        _send_startup_message()

    cycle = 1
    while True:
        logger.info("Starting cycle #%d", cycle)
        try:
            run_one_cycle(alert_only=args.alert_only)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as exc:
            logger.error("Unexpected error in cycle #%d: %s", cycle, exc)

        if args.once:
            logger.info("--once flag set, exiting after first cycle.")
            break

        logger.info("Next cycle in %d minutes. Sleeping...", args.interval)
        logger.info("─" * 60)

        try:
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break

        cycle += 1


if __name__ == "__main__":
    main()
