# -*- coding: utf-8 -*-
"""
services/scraper_loop.py — Scheduled scraper service for Docker.

Behaviour:
  1. Waits for PostgreSQL and Redis to be reachable.
  2. Runs the full scrape + dedup pipeline immediately on start.
  3. Publishes "scrape_done" to the Redis "jobs:events" channel so the
     scorer service can react immediately.
  4. Repeats every SCRAPE_INTERVAL_HOURS (default: 12).
  5. Writes a heartbeat file (/tmp/scraper.heartbeat) for Docker health check.

Run (Docker):
  docker compose up scraper

Run (local):
  SCRAPE_INTERVAL_HOURS=1 python services/scraper_loop.py
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
import redis as redis_lib
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scraper_svc")

# ── Config ─────────────────────────────────────────────────────────────────────
SCRAPE_INTERVAL_HOURS = int(os.getenv("SCRAPE_INTERVAL_HOURS", "12"))
SCRAPE_SOURCES        = os.getenv("SCRAPE_SOURCES", "all").split(",")
REDIS_URL             = os.getenv("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_FILE        = Path("/tmp/scraper.heartbeat")


# ── Wait helpers ───────────────────────────────────────────────────────────────

def _wait_for_db(max_retries: int = 30, delay: float = 5.0) -> None:
    """Block until PostgreSQL is reachable."""
    from core.database import check_connection
    logger.info("Waiting for PostgreSQL…")
    for attempt in range(1, max_retries + 1):
        if check_connection():
            logger.info("PostgreSQL ready.")
            return
        logger.warning(f"  DB not ready (attempt {attempt}/{max_retries}), retrying in {delay}s…")
        time.sleep(delay)
    logger.error("Could not connect to PostgreSQL. Aborting.")
    sys.exit(1)


def _wait_for_redis(max_retries: int = 20, delay: float = 3.0) -> redis_lib.Redis:
    """Block until Redis is reachable, return client."""
    logger.info("Waiting for Redis…")
    for attempt in range(1, max_retries + 1):
        try:
            client = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
            client.ping()
            logger.info("Redis ready.")
            return client
        except Exception:
            logger.warning(f"  Redis not ready (attempt {attempt}/{max_retries}), retrying in {delay}s…")
            time.sleep(delay)
    logger.error("Could not connect to Redis. Aborting.")
    sys.exit(1)


# ── Core job ───────────────────────────────────────────────────────────────────

def run_scrape_job(redis_client: redis_lib.Redis) -> None:
    """Execute one full scrape + dedup cycle, then signal the scorer."""
    logger.info(f"{'═' * 50}")
    logger.info(f"Starting scrape — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(f"Sources: {SCRAPE_SOURCES}")
    logger.info(f"{'═' * 50}")

    # Lazy import to avoid Playwright init at import time
    from run_all import run as run_all

    try:
        asyncio.run(run_all(
            sources   = SCRAPE_SOURCES,
            debug     = False,
            headless  = True,
            run_dedup = True,
        ))
        logger.info("Scrape completed successfully.")

        # Notify scorer via Redis pub/sub
        subs = redis_client.publish("jobs:events", "scrape_done")
        logger.info(f"Published 'scrape_done' to jobs:events ({subs} subscriber(s)).")

    except Exception as exc:
        logger.exception(f"Scrape failed: {exc}")

    finally:
        # Update Docker health-check heartbeat
        HEARTBEAT_FILE.touch()
        logger.info(f"Heartbeat updated: {HEARTBEAT_FILE}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Job Discovery — Scraper Service starting…")
    logger.info(f"  Interval : every {SCRAPE_INTERVAL_HOURS}h")
    logger.info(f"  Sources  : {SCRAPE_SOURCES}")

    _wait_for_db()
    redis_client = _wait_for_redis()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        func        = run_scrape_job,
        trigger     = IntervalTrigger(hours=SCRAPE_INTERVAL_HOURS),
        args        = [redis_client],
        id          = "scrape",
        name        = f"Full scrape every {SCRAPE_INTERVAL_HOURS}h",
        replace_existing = True,
        next_run_time    = datetime.utcnow(),   # run immediately on startup
        misfire_grace_time = 3600,
    )

    logger.info(f"Scheduler ready — first run starts now, then every {SCRAPE_INTERVAL_HOURS}h.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scraper service stopped.")


if __name__ == "__main__":
    main()
