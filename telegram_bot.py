# -*- coding: utf-8 -*-
"""
telegram_bot.py — Telegram notification bot for the job discovery system.

Features
--------
  • Daily digest at 09:00 — all new jobs (last 24 h) scored ≥ 70
  • Instant alert      — fires within 5 min whenever a job scores ≥ 85
  • /status            — active job count, avg score, added today, high-confidence count
  • /top10             — ten highest-scoring active jobs

First-time setup
----------------
  1. Create a bot via @BotFather on Telegram → copy the token.
  2. Start a chat with your new bot (send it any message first).
  3. Find your numeric chat ID — easiest way:
       https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     Look for  "chat": {"id": 123456789, ...}  in the response.
  4. Add to your .env:
       TELEGRAM_BOT_TOKEN=123456789:ABC-your-token-here
       TELEGRAM_CHAT_ID=987654321
  5. Install deps (once):
       py -m pip install "python-telegram-bot[job-queue]" apscheduler
  6. Run:
       py telegram_bot.py

Scheduling notes
----------------
  APScheduler's AsyncIOScheduler runs inside the same event loop as the
  python-telegram-bot Application.  The daily digest uses a CronTrigger
  (fires at 09:00 in whatever timezone the host is set to).  The instant-
  alert poller uses an IntervalTrigger (every ALERT_POLL_MINS minutes).
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

# Windows: ensure UTF-8 output (emoji, Unicode chars in job titles)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, text
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

from core.logging_config import setup_logging
setup_logging()

from core.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    HIGH_SCORE_ALERT_THRESHOLD,
    DIGEST_MIN_SCORE as _DIGEST_MIN,
)
from core.database import get_engine, get_session
from core.models import Job

# ─────────────────────────────────────────────────────────────
# Config — override any of these via environment variables
# ─────────────────────────────────────────────────────────────
BOT_TOKEN        = TELEGRAM_BOT_TOKEN
CHAT_ID          = TELEGRAM_CHAT_ID
DIGEST_MIN_SCORE = _DIGEST_MIN
ALERT_MIN_SCORE  = HIGH_SCORE_ALERT_THRESHOLD
ALERT_POLL_MINS  = int(os.getenv("TG_ALERT_POLL_MINS",  "5"))    # alert poll interval (minutes)
DIGEST_HOUR      = int(os.getenv("TG_DIGEST_HOUR",      "9"))    # 24-h hour for daily digest
DIGEST_MINUTE    = int(os.getenv("TG_DIGEST_MINUTE",    "0"))    # minute for daily digest

log = logging.getLogger("tgbot")

# ─────────────────────────────────────────────────────────────
# DB migration — ensure tg_alerted column exists
# ─────────────────────────────────────────────────────────────

def _ensure_tg_alerted_column() -> None:
    """
    Idempotent: add tg_alerted BOOLEAN DEFAULT FALSE to the jobs table.
    Safe to call on every startup — ignored if the column already exists.
    """
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE jobs "
            "ADD COLUMN IF NOT EXISTS tg_alerted BOOLEAN DEFAULT FALSE"
        ))
        conn.commit()
    log.info("DB column tg_alerted … OK")


# ─────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────

def _relative_date(dt: datetime | None) -> str:
    """Return a human-readable age string for a UTC datetime."""
    if not dt:
        return "unknown date"
    delta = datetime.utcnow() - dt
    if delta.days == 0:
        hours = delta.seconds // 3600
        return "just now" if hours == 0 else f"{hours}h ago"
    elif delta.days == 1:
        return "yesterday"
    else:
        return f"{delta.days} days ago"


def _score_bar(score: float, width: int = 10) -> str:
    """Return a compact Unicode progress bar, e.g. ▓▓▓▓▓▓░░░░ for 60 %."""
    filled = round(score / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _fmt_job(job: Job, rank: int | None = None) -> str:
    """
    Format one job as a MarkdownV2 block ready for Telegram.

    Example output:
        #1 ▓▓▓▓▓▓▓▓░░ 82/100
        🏢 J.P. Morgan
        💼 Software Engineer – Java / Backend
        📅 Posted: 2 days ago
        🔗 Apply Here
    """
    score    = job.combined_score or 0.0
    company  = escape_markdown(job.company or "Unknown", version=2)
    title    = escape_markdown(job.title   or "Unknown", version=2)
    posted   = escape_markdown(_relative_date(job.first_seen), version=2)
    url      = job.url or "https://irishjobs.ie"
    bar      = escape_markdown(_score_bar(score), version=2)
    score_s  = escape_markdown(f"{score:.0f}", version=2)

    rank_line = f"*\\#{rank}* " if rank else ""
    return (
        f"{rank_line}{bar} *{score_s}/100*\n"
        f"🏢 *{company}*\n"
        f"💼 {title}\n"
        f"📅 Posted: {posted}\n"
        f"🔗 [Apply Here]({url})"
    )


async def _send_chunked(
    bot: Bot,
    chat_id: str | int,
    blocks: list[str],
    header: str = "",
) -> None:
    """
    Send a list of formatted job blocks, splitting into multiple messages
    if they exceed Telegram's 4 096-character limit.
    """
    LIMIT     = 4_000
    SEPARATOR = "\n\n" + "─" * 24 + "\n\n"

    chunk = header
    for block in blocks:
        section = block + SEPARATOR
        if len(chunk) + len(section) > LIMIT:
            await bot.send_message(
                chat_id,
                chunk.strip(),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            chunk = section
        else:
            chunk += section

    if chunk.strip():
        await bot.send_message(
            chat_id,
            chunk.strip(),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )


# ─────────────────────────────────────────────────────────────
# Scheduled task 1: daily digest
# ─────────────────────────────────────────────────────────────

async def send_daily_digest(bot: Bot) -> None:
    """
    Fired every morning at DIGEST_HOUR:DIGEST_MINUTE.
    Sends every active job first seen in the last 24 hours with
    combined_score ≥ DIGEST_MIN_SCORE, ranked highest-first.
    """
    log.info("Running daily digest …")
    cutoff  = datetime.utcnow() - timedelta(hours=24)
    session = get_session()
    try:
        jobs = (
            session.query(Job)
            .filter(
                Job.is_active,
                Job.combined_score >= DIGEST_MIN_SCORE,
                Job.first_seen     >= cutoff,
            )
            .order_by(Job.combined_score.desc())
            .all()
        )
    finally:
        session.close()

    date_str  = escape_markdown(datetime.now().strftime("%d %b %Y"), version=2)
    threshold = escape_markdown(str(DIGEST_MIN_SCORE), version=2)

    if not jobs:
        log.info("Digest: 0 qualifying jobs — skipping notification (silent).")
        return

    count_str = escape_markdown(str(len(jobs)), version=2)
    header = (
        f"🌅 *Daily Job Digest — {date_str}*\n"
        f"*{count_str}* new job\\(s\\) scoring \\>{threshold}\n\n"
        + "─" * 24 + "\n\n"
    )
    blocks = [_fmt_job(j, rank=i + 1) for i, j in enumerate(jobs)]
    await _send_chunked(bot, CHAT_ID, blocks, header)
    log.info(f"Digest sent: {len(jobs)} job(s).")


# ─────────────────────────────────────────────────────────────
# Scheduled task 2: instant alert poller
# ─────────────────────────────────────────────────────────────

async def check_instant_alerts(bot: Bot) -> None:
    """
    Polls every ALERT_POLL_MINS minutes.
    Sends an immediate alert for each active job with
    combined_score ≥ ALERT_MIN_SCORE that has not been alerted yet,
    then marks it tg_alerted=True so it is never sent twice.
    """
    session = get_session()
    try:
        jobs = (
            session.query(Job)
            .filter(
                Job.is_active,
                Job.combined_score >= ALERT_MIN_SCORE,
                # Catch both False and NULL (newly inserted rows)
                Job.tg_alerted.isnot(True),
            )
            .order_by(Job.combined_score.desc())
            .all()
        )

        if not jobs:
            return

        log.info(f"Instant alert: {len(jobs)} high-score job(s).")

        for job in jobs:
            escape_markdown(f"{job.combined_score:.0f}", version=2)
            thresh_s = escape_markdown(str(ALERT_MIN_SCORE), version=2)
            header = (
                f"🚨 *High\\-Score Alert\\!*  \\(≥{thresh_s}/100\\)\n\n"
            )
            msg = header + _fmt_job(job)
            await bot.send_message(
                CHAT_ID,
                msg,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            job.tg_alerted = True

        session.commit()

    except Exception as exc:
        session.rollback()
        log.error(f"Alert check failed: {exc}", exc_info=True)
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# /status command
# ─────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /status — reply with a snapshot of the job database:
      • Total active jobs
      • Average combined score
      • Jobs added today
      • Jobs scoring ≥ DIGEST_MIN_SCORE (high-confidence)
      • Jobs scoring ≥ ALERT_MIN_SCORE  (very high-confidence)
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    session     = get_session()
    try:
        active_total  = (
            session.query(func.count(Job.id))
            .filter(Job.is_active)
            .scalar() or 0
        )
        avg_score     = (
            session.query(func.avg(Job.combined_score))
            .filter(Job.is_active, Job.combined_score.isnot(None))
            .scalar()
        )
        added_today   = (
            session.query(func.count(Job.id))
            .filter(Job.is_active, Job.first_seen >= today_start)
            .scalar() or 0
        )
        high_conf     = (
            session.query(func.count(Job.id))
            .filter(Job.is_active, Job.combined_score >= DIGEST_MIN_SCORE)
            .scalar() or 0
        )
        very_high     = (
            session.query(func.count(Job.id))
            .filter(Job.is_active, Job.combined_score >= ALERT_MIN_SCORE)
            .scalar() or 0
        )
    finally:
        session.close()

    avg_s      = f"{avg_score:.1f}" if avg_score else "N/A"
    date_s     = escape_markdown(datetime.now().strftime("%d %b %Y, %H:%M"), version=2)
    avg_e      = escape_markdown(avg_s, version=2)
    dig_thresh = escape_markdown(str(DIGEST_MIN_SCORE), version=2)
    alt_thresh = escape_markdown(str(ALERT_MIN_SCORE),  version=2)

    msg = (
        f"📈 *Job Search Status*\n"
        f"🕒 {date_s}\n\n"
        f"📋 Active jobs:          *{active_total}*\n"
        f"⭐ Avg combined score:   *{avg_e}/100*\n"
        f"🆕 Added today:          *{added_today}*\n"
        f"✅ Score ≥{dig_thresh}:         *{high_conf}*\n"
        f"🚀 Score ≥{alt_thresh}:         *{very_high}*\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ─────────────────────────────────────────────────────────────
# /top10 command
# ─────────────────────────────────────────────────────────────

async def cmd_top10(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /top10 — return the 10 highest-scoring active jobs from the database,
    regardless of when they were first seen.
    """
    session = get_session()
    try:
        jobs = (
            session.query(Job)
            .filter(Job.is_active, Job.combined_score.isnot(None))
            .order_by(Job.combined_score.desc())
            .limit(10)
            .all()
        )
    finally:
        session.close()

    if not jobs:
        await update.message.reply_text(
            "No scored jobs found yet\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    header = "🏆 *Top 10 Jobs*\n\n" + "─" * 24 + "\n\n"
    blocks = [_fmt_job(j, rank=i + 1) for i, j in enumerate(jobs)]
    await _send_chunked(context.bot, update.effective_chat.id, blocks, header)


# ─────────────────────────────────────────────────────────────
# /help command
# ─────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands."""
    msg = (
        "🤖 *Job Search Bot — Commands*\n\n"
        "/status  — DB snapshot \\(job counts, avg score, today's additions\\)\n"
        "/top10   — 10 highest\\-scoring active jobs\n"
        "/help    — this message\n\n"
        "_You'll also receive:_\n"
        f"• 🌅 Daily digest at {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} "
        f"\\(jobs scored ≥{DIGEST_MIN_SCORE}\\)\n"
        f"• 🚨 Instant alert when a job scores ≥{ALERT_MIN_SCORE}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ─────────────────────────────────────────────────────────────
# Main entry-point
# ─────────────────────────────────────────────────────────────

async def main() -> None:
    # ── Pre-flight checks ─────────────────────────────────────
    if not BOT_TOKEN:
        log.error(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Add it to .env:  TELEGRAM_BOT_TOKEN=123456:ABC-your-token"
        )
        sys.exit(1)

    if not CHAT_ID:
        log.error(
            "TELEGRAM_CHAT_ID is not set.\n"
            "Get it from:  https://api.telegram.org/bot<TOKEN>/getUpdates\n"
            "Then add to .env:  TELEGRAM_CHAT_ID=987654321"
        )
        sys.exit(1)

    # ── DB migration ──────────────────────────────────────────
    _ensure_tg_alerted_column()

    # ── Build Application ─────────────────────────────────────
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("top10",  cmd_top10))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("start",  cmd_help))

    bot = app.bot

    # ── APScheduler ───────────────────────────────────────────
    scheduler = AsyncIOScheduler()

    # Daily digest — fires at DIGEST_HOUR:DIGEST_MINUTE in local time
    scheduler.add_job(
        send_daily_digest,
        CronTrigger(hour=DIGEST_HOUR, minute=DIGEST_MINUTE),
        args=[bot],
        id="daily_digest",
        name=f"Daily digest at {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}",
        misfire_grace_time=300,          # allow up to 5-min late fire
        replace_existing=True,
    )

    # Instant alert — runs every ALERT_POLL_MINS minutes
    scheduler.add_job(
        check_instant_alerts,
        IntervalTrigger(minutes=ALERT_POLL_MINS),
        args=[bot],
        id="instant_alerts",
        name=f"Alert poller every {ALERT_POLL_MINS} min",
        replace_existing=True,
    )

    scheduler.start()
    log.info(
        f"Scheduler started — digest at {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}, "
        f"alert poll every {ALERT_POLL_MINS} min."
    )

    # ── Start polling Telegram ────────────────────────────────
    log.info("Bot is live. Press Ctrl-C to stop.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep the event loop alive until the user interrupts
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown signal received.")
    finally:
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
