# -*- coding: utf-8 -*-
"""
tg_notify.py -- One-shot Telegram notification script for GitHub Actions.

Usage:
  python tg_notify.py --digest   # send daily digest (jobs >= 70 from last 24 h)
  python tg_notify.py --alerts   # send instant alerts for unnotified jobs >= 85
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
from sqlalchemy import text
from telegram import Bot
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

load_dotenv()

BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = os.getenv("TELEGRAM_CHAT_ID", "")
DIGEST_MIN_SCORE = int(os.getenv("TG_DIGEST_MIN_SCORE", "70"))
ALERT_MIN_SCORE  = int(os.getenv("TG_ALERT_MIN_SCORE",  "85"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tg_notify")


def _relative_date(dt):
    if not dt:
        return "unknown date"
    delta = datetime.utcnow() - dt
    if delta.days == 0:
        hours = delta.seconds // 3600
        return "just now" if hours == 0 else f"{hours}h ago"
    return "yesterday" if delta.days == 1 else f"{delta.days} days ago"


def _score_bar(score, width=10):
    filled = round(score / 100 * width)
    return "\u2593" * filled + "\u2591" * (width - filled)


def _fmt_job(job, rank=None):
    score   = job.combined_score or 0.0
    company = escape_markdown(job.company or "Unknown", version=2)
    title   = escape_markdown(job.title   or "Unknown", version=2)
    posted  = escape_markdown(_relative_date(job.first_seen), version=2)
    url     = job.url or "https://irishjobs.ie"
    bar     = escape_markdown(_score_bar(score), version=2)
    score_s = escape_markdown(f"{score:.0f}", version=2)
    rank_line = f"*\\#{rank}* " if rank else ""
    return (
        f"{rank_line}{bar} *{score_s}/100*\n"
        f"\U0001f3e2 *{company}*\n"
        f"\U0001f4bc {title}\n"
        f"\U0001f4c5 Posted: {posted}\n"
        f"\U0001f517 [Apply Here]({url})"
    )


async def _send_chunked(bot, blocks, header=""):
    LIMIT = 4_000
    SEP   = "\n\n" + "\u2500" * 24 + "\n\n"
    chunk = header
    for block in blocks:
        section = block + SEP
        if len(chunk) + len(section) > LIMIT:
            await bot.send_message(CHAT_ID, chunk.strip(), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
            chunk = section
        else:
            chunk += section
    if chunk.strip():
        await bot.send_message(CHAT_ID, chunk.strip(), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)


def _get_db():
    from core.database import get_session
    from core.models import Job
    return get_session(), Job


def _ensure_tg_alerted():
    from core.database import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_alerted BOOLEAN DEFAULT FALSE"))
        conn.commit()


async def run_digest(bot):
    cutoff = datetime.utcnow() - timedelta(hours=24)
    session, Job = _get_db()
    try:
        jobs = (session.query(Job)
                .filter(Job.is_active == True, Job.combined_score >= DIGEST_MIN_SCORE, Job.first_seen >= cutoff)
                .order_by(Job.combined_score.desc()).all())
    finally:
        session.close()

    date_str  = escape_markdown(datetime.now().strftime("%d %b %Y"), version=2)
    threshold = escape_markdown(str(DIGEST_MIN_SCORE), version=2)

    if not jobs:
        await bot.send_message(CHAT_ID,
            f"\U0001f4ed *Daily Job Digest \u2014 {date_str}*\n\nNo new jobs scored \\>{threshold} in the last 24 hours\\.",
            parse_mode=ParseMode.MARKDOWN_V2)
        log.info("Digest sent: 0 jobs.")
        return

    count_s = escape_markdown(str(len(jobs)), version=2)
    header  = f"\U0001f305 *Daily Job Digest \u2014 {date_str}*\n*{count_s}* new job\\(s\\) scoring \\>{threshold}\n\n" + "\u2500" * 24 + "\n\n"
    await _send_chunked(bot, [_fmt_job(j, rank=i + 1) for i, j in enumerate(jobs)], header)
    log.info(f"Digest sent: {len(jobs)} job(s).")


async def run_alerts(bot):
    _ensure_tg_alerted()
    session, Job = _get_db()
    try:
        jobs = (session.query(Job)
                .filter(Job.is_active == True, Job.combined_score >= ALERT_MIN_SCORE, Job.tg_alerted.isnot(True))
                .order_by(Job.combined_score.desc()).all())

        if not jobs:
            log.info("Alerts: no new high-score jobs.")
            return

        thresh_s = escape_markdown(str(ALERT_MIN_SCORE), version=2)
        for job in jobs:
            await bot.send_message(CHAT_ID,
                f"\U0001f6a8 *High\\-Score Alert\\!*  \\(\u2265{thresh_s}/100\\)\n\n" + _fmt_job(job),
                parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
            job.tg_alerted = True

        session.commit()
        log.info(f"Alerts: {len(jobs)} message(s) sent.")
    except Exception as exc:
        session.rollback()
        log.error(f"Alert run failed: {exc}", exc_info=True)
        raise
    finally:
        session.close()


async def main():
    parser = argparse.ArgumentParser()
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--digest", action="store_true")
    group.add_argument("--alerts", action="store_true")
    args = parser.parse_args()

    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set")
        sys.exit(1)

    async with Bot(token=BOT_TOKEN) as bot:
        await run_digest(bot) if args.digest else await run_alerts(bot)


if __name__ == "__main__":
    asyncio.run(main())
