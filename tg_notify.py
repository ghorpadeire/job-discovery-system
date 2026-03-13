"""
tg_notify.py — One-shot Telegram notification script (designed for GitHub Actions).

Modes:
  py tg_notify.py --digest   Daily digest of jobs scored ≥ 70 from last 24h
  py tg_notify.py --alerts   Instant alerts for unnotified jobs scored ≥ 85

Runs once and exits with code 0 (success) or 1 (fatal error).
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tg_notify")

DIGEST_MIN_SCORE = 50   # Daily digest: score ≥ 50
ALERT_MIN_SCORE  = 40   # Instant alerts: score ≥ 40 (catches most real jobs)
ALERT_BATCH_SIZE = 15   # Max jobs per alert run (avoid Telegram flood)


# ─────────────────────────────────────────────
#  Message helpers
# ─────────────────────────────────────────────

def _fmt_job_line(job) -> str:
    score = job.legitimacy_score or 0
    badge = "🟢" if score >= 85 else "🟡"
    loc = f"\n   📍 {job.location}" if job.location else ""
    sal = f"  💰 {job.salary}" if job.salary else ""
    return (
        f"{badge} <b>[{score}]</b> {job.title} — {job.company}\n"
        f"{loc}{sal}\n"
        f"   🔗 {job.url}"
    )


def _chunk_messages(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    current_lines: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        ll = len(line) + 1
        if current_len + ll > limit:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = ll
        else:
            current_lines.append(line)
            current_len += ll
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


# ─────────────────────────────────────────────
#  DB helpers
# ─────────────────────────────────────────────

def _get_engine():
    from core.database import get_engine
    return get_engine()


def _digest_jobs():
    """Return jobs scored ≥ 70 posted or first_seen in last 24 hours."""
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        return (
            session.query(Job)
            .filter(
                Job.is_active,
                Job.legitimacy_score >= DIGEST_MIN_SCORE,
                Job.first_seen >= cutoff,
            )
            .order_by(Job.legitimacy_score.desc())
            .all()
        )
    finally:
        session.close()


def _unalerted_jobs():
    """Return jobs scored ≥ ALERT_MIN_SCORE that have not been Telegram-alerted yet.
    Ordered by score descending, capped at ALERT_BATCH_SIZE to avoid flooding.
    """
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        return (
            session.query(Job)
            .filter(
                Job.is_active,
                Job.legitimacy_score >= ALERT_MIN_SCORE,
                not Job.suspected_ghost,
                not Job.tg_alerted,
            )
            .order_by(Job.legitimacy_score.desc())
            .limit(ALERT_BATCH_SIZE)
            .all()
        )
    finally:
        session.close()


def _mark_alerted(job_ids: list[int]) -> None:
    """Set tg_alerted=True for the given job IDs."""
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        session.query(Job).filter(Job.id.in_(job_ids)).update(
            {"tg_alerted": True}, synchronize_session=False
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("Failed to mark jobs as alerted: %s", exc)
    finally:
        session.close()


# ─────────────────────────────────────────────
#  Sending
# ─────────────────────────────────────────────

async def _send_message(bot, chat_id: str, text: str) -> bool:
    """Send a single message via Telegram bot."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        logger.error("Send failed: %s", exc)
        return False


async def _send_chunked(bot, chat_id: str, text: str) -> None:
    for chunk in _chunk_messages(text):
        await _send_message(bot, chat_id, chunk)


# ─────────────────────────────────────────────
#  Digest mode
# ─────────────────────────────────────────────

async def run_digest(bot, chat_id: str) -> int:
    """Send daily digest of high-quality jobs from last 24 hours."""
    jobs = _digest_jobs()
    if not jobs:
        logger.info("Digest: no jobs found with score ≥ %d in last 24h", DIGEST_MIN_SCORE)
        now_str = datetime.now(timezone.utc).strftime("%a %d %b %Y %H:%M UTC")
        await bot.send_message(
            chat_id=int(chat_id),
            text=(
                f"📋 <b>Daily Digest — {now_str}</b>\n\n"
                "😴 <b>No new quality jobs found</b> in the last 24 hours.\n"
                "The scrapers ran but nothing passed the quality filter.\n\n"
                "<i>Next scrape: 07:00 or 19:00 UTC</i>"
            ),
            parse_mode="HTML",
        )
        return 0

    now_str = datetime.now(timezone.utc).strftime("%a %d %b %Y")
    v_high = [j for j in jobs if (j.legitimacy_score or 0) >= 85]
    high   = [j for j in jobs if 70 <= (j.legitimacy_score or 0) < 85]

    lines = [
        f"<b>📋 Daily Job Digest — {now_str}</b>",
        f"Found <b>{len(jobs)} quality jobs</b> in the last 24 hours.\n",
    ]

    if v_high:
        lines.append(f"<b>🟢 Very High Confidence (≥85) — {len(v_high)} jobs</b>")
        for job in v_high:
            lines.append(_fmt_job_line(job))
            lines.append("")

    if high:
        lines.append(f"<b>🟡 High Confidence (70–84) — {len(high)} jobs</b>")
        for job in high:
            lines.append(_fmt_job_line(job))
            lines.append("")

    lines.append("<i>Run /top10 or visit the dashboard for full details.</i>")

    await _send_chunked(bot, chat_id, "\n".join(lines))
    logger.info("Digest sent: %d jobs", len(jobs))
    return len(jobs)


# ─────────────────────────────────────────────
#  Alerts mode
# ─────────────────────────────────────────────

def _score_emoji(score: int) -> str:
    if score >= 85: return "🟢"
    if score >= 70: return "🟡"
    if score >= 50: return "🔵"
    return "🔴"


async def run_alerts(bot, chat_id: str) -> int:
    """Send individual alerts for every unnotified job above the threshold."""
    jobs = _unalerted_jobs()
    if not jobs:
        logger.info("Alerts: no new unalerted jobs with score ≥ %d", ALERT_MIN_SCORE)
        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
        await bot.send_message(
            chat_id=int(chat_id),
            text=(
                f"⏰ <b>30-min check — {now_str}</b>\n\n"
                "😔 <b>No new jobs</b> this cycle.\n"
                "<i>Checking again in 30 minutes.</i>"
            ),
            parse_mode="HTML",
        )
        return 0

    # Send a summary header first if more than 3 jobs
    if len(jobs) >= 3:
        header = (
            f"📬 <b>{len(jobs)} new job{'s' if len(jobs)>1 else ''} found!</b>\n"
            f"Sending individual alerts now..."
        )
        await _send_message(bot, chat_id, header)

    import asyncio
    sent_ids = []
    for job in jobs:
        score = job.legitimacy_score or 0
        emoji = _score_emoji(score)
        sal  = f"\n💰 <b>Salary:</b> {job.salary}" if job.salary else ""
        loc  = f"\n📍 <b>Location:</b> {job.location}" if job.location else ""
        date = f"\n📅 <b>Posted:</b> {job.date_posted}" if job.date_posted else ""
        src  = f"\n📌 <b>Source:</b> {(job.source or '').upper()}"

        breakdown = job.score_breakdown or {}
        signals_hit  = [k.replace('_',' ') for k, v in breakdown.items() if v > 0]
        signals_miss = [k.replace('_',' ') for k, v in breakdown.items() if v == 0]
        sig_str = ""
        if signals_hit:
            sig_str += "\n✅ " + "  ✅ ".join(signals_hit)
        if signals_miss:
            sig_str += "\n❌ " + "  ❌ ".join(signals_miss)

        msg = (
            f"{emoji} <b>[{score}/100] {job.title}</b>\n"
            f"🏢 <b>{job.company}</b>{loc}{sal}{date}{src}\n"
            f"{sig_str}\n\n"
            f"🔗 <a href='{job.url}'>Apply Now</a>"
        )

        ok = await _send_message(bot, chat_id, msg)
        if ok:
            sent_ids.append(job.id)
        await asyncio.sleep(0.4)  # avoid Telegram rate limit

    if sent_ids:
        _mark_alerted(sent_ids)
        logger.info("Alerts sent: %d jobs", len(sent_ids))

    return len(sent_ids)


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

async def main_async(mode: str) -> int:
    from telegram import Bot

    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return 1
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set")
        return 1

    # Verify DB connectivity
    from core.database import check_connection
    if not check_connection():
        logger.error("Cannot connect to database")
        return 1

    async with Bot(token=token) as bot:
        if mode == "digest":
            count = await run_digest(bot, chat_id)
        elif mode == "alerts":
            count = await run_alerts(bot, chat_id)
        else:
            logger.error("Unknown mode: %s. Use --digest or --alerts", mode)
            return 1

    logger.info("Done. Sent %d notification(s).", count)
    return 0


def _reset_all_alerts() -> int:
    """Mark all jobs as tg_alerted=False so they will be re-sent."""
    from sqlalchemy.orm import sessionmaker
    from core.models import Job
    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        count = session.query(Job).filter(Job.tg_alerted).update(
            {"tg_alerted": False}, synchronize_session=False
        )
        session.commit()
        logger.info("Reset tg_alerted on %d jobs — all will be re-alerted", count)
        return count
    except Exception as exc:
        session.rollback()
        logger.error("Reset failed: %s", exc)
        return 0
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram notifier for JobScout")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--digest",       action="store_true", help="Send daily digest (score ≥ 50)")
    group.add_argument("--alerts",       action="store_true", help="Send alerts for new unnotified jobs")
    group.add_argument("--reset-alerts", action="store_true", help="Unmark all jobs so they get re-sent")
    args = parser.parse_args()

    if args.reset_alerts:
        from core.database import check_connection
        if not check_connection():
            logger.error("Cannot connect to database")
            return 1
        count = _reset_all_alerts()
        print(f"Reset {count} jobs — run --alerts now to send them all")
        return 0

    mode = "digest" if args.digest else "alerts"
    return asyncio.run(main_async(mode))


if __name__ == "__main__":
    sys.exit(main())
