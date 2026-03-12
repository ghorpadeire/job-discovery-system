# -*- coding: utf-8 -*-
"""
services/scorer_loop.py — Scoring service for Docker.

Behaviour:
  1. Waits for PostgreSQL and Redis to be reachable.
  2. Subscribes to the Redis "jobs:events" pub/sub channel.
  3. Runs legitimacy scoring whenever a "scrape_done" event is received.
  4. If AI_SCORE_ENABLED=true and OPENAI_API_KEY is set, also runs AI scoring.
  5. Falls back to a periodic run every SCORER_FALLBACK_HOURS even if no
     pub/sub event arrives (handles restarts, missed signals, etc.).

Run (Docker):
  docker compose up scorer

Run (local):
  AI_SCORE_ENABLED=false python services/scorer_loop.py
"""

import logging
import os
import sys
import time
import threading
from datetime import datetime

import redis as redis_lib
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scorer_svc")

# ── Config ─────────────────────────────────────────────────────────────────────
REDIS_URL             = os.getenv("REDIS_URL", "redis://redis:6379/0")
SCORER_FALLBACK_HOURS = int(os.getenv("SCORER_FALLBACK_HOURS", "6"))
AI_SCORE_ENABLED      = os.getenv("AI_SCORE_ENABLED", "false").lower() == "true"
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")

# Guard flag — prevent two scoring runs overlapping
_scoring_lock = threading.Lock()


# ── Wait helpers ───────────────────────────────────────────────────────────────

def _wait_for_db(max_retries: int = 30, delay: float = 5.0) -> None:
    from core.database import check_connection
    logger.info("Waiting for PostgreSQL…")
    for attempt in range(1, max_retries + 1):
        if check_connection():
            logger.info("PostgreSQL ready.")
            return
        logger.warning(f"  DB not ready ({attempt}/{max_retries}), retrying in {delay}s…")
        time.sleep(delay)
    logger.error("Could not connect to PostgreSQL. Aborting.")
    sys.exit(1)


def _wait_for_redis(max_retries: int = 20, delay: float = 3.0) -> redis_lib.Redis:
    logger.info("Waiting for Redis…")
    for attempt in range(1, max_retries + 1):
        try:
            client = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
            client.ping()
            logger.info("Redis ready.")
            return client
        except Exception:
            logger.warning(f"  Redis not ready ({attempt}/{max_retries}), retrying in {delay}s…")
            time.sleep(delay)
    logger.error("Could not connect to Redis. Aborting.")
    sys.exit(1)


# ── Core scoring job ───────────────────────────────────────────────────────────

def run_scoring(trigger: str = "manual") -> None:
    """Run legitimacy scoring (and optionally AI scoring) for all unscored jobs."""
    if not _scoring_lock.acquire(blocking=False):
        logger.info(f"Scoring already in progress, skipping ({trigger} trigger).")
        return

    try:
        logger.info(f"{'─' * 50}")
        logger.info(f"Scoring triggered by: {trigger} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")

        # ── Legitimacy scoring ───────────────────────────────────────────────
        from core.database import get_engine
        from core.models import migrate_scoring_columns
        from core.scorer import score_all_active_jobs

        engine = get_engine()
        migrate_scoring_columns(engine)
        scored = score_all_active_jobs(engine, rescore=False)
        logger.info(f"Legitimacy scoring done — {scored} job(s) scored.")

        # ── AI scoring (optional) ────────────────────────────────────────────
        if AI_SCORE_ENABLED and OPENAI_API_KEY:
            logger.info("AI scoring enabled — running ai_score_jobs…")
            try:
                # Import lazily to avoid OpenAI client init if not needed
                from core.ai_scorer import score_unscored_jobs
                ai_count = score_unscored_jobs(engine)
                logger.info(f"AI scoring done — {ai_count} job(s) processed.")
            except Exception as exc:
                logger.exception(f"AI scoring failed: {exc}")
        elif AI_SCORE_ENABLED and not OPENAI_API_KEY:
            logger.warning("AI_SCORE_ENABLED=true but OPENAI_API_KEY is not set — skipping.")

    except Exception as exc:
        logger.exception(f"Scoring error: {exc}")
    finally:
        _scoring_lock.release()
        logger.info("Scoring cycle complete.")


# ── Fallback periodic thread ───────────────────────────────────────────────────

def _fallback_thread() -> None:
    """Run scoring on a fixed interval regardless of pub/sub events."""
    interval_secs = SCORER_FALLBACK_HOURS * 3600
    logger.info(f"Fallback scorer thread started — runs every {SCORER_FALLBACK_HOURS}h.")
    while True:
        time.sleep(interval_secs)
        logger.info("Fallback timer fired.")
        run_scoring(trigger=f"fallback/{SCORER_FALLBACK_HOURS}h")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Job Discovery — Scorer Service starting…")
    logger.info(f"  AI scoring : {'enabled' if AI_SCORE_ENABLED else 'disabled'}")
    logger.info(f"  Fallback   : every {SCORER_FALLBACK_HOURS}h")

    _wait_for_db()
    redis_client = _wait_for_redis()

    # Run once on startup to score anything already in the DB
    run_scoring(trigger="startup")

    # Fallback periodic scoring (background thread)
    t = threading.Thread(target=_fallback_thread, daemon=True)
    t.start()

    # Subscribe to pub/sub events from the scraper
    pubsub = redis_client.pubsub()
    pubsub.subscribe("jobs:events")
    logger.info("Subscribed to Redis channel 'jobs:events' — waiting for events…")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        event = message["data"]
        if isinstance(event, bytes):
            event = event.decode()

        logger.info(f"Received event: '{event}'")
        if event == "scrape_done":
            run_scoring(trigger="scrape_done")
        else:
            logger.debug(f"Unknown event '{event}' — ignored.")


if __name__ == "__main__":
    main()
