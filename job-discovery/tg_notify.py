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

DIGEST_MIN_SCORE = 70
ALERT_MIN_SCORE  = 85


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
                Job.is_active == True,
                Job.legitimacy_score >= DIGEST_MIN_SCORE,
                Job.first_seen >= cutoff,
            )
            .order_by(Job.legitimacy_score.desc())
            .all()
        )
    finally:
        session.close()


def _unalerted_jobs():
    """Return jobs scored ≥ 85 that have not been Telegram-alerted yet."""
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        return (
            session.query(Job)
            .filter(
                Job.is_active == True,
                Job.legitimacy_score >= ALERT_MIN_SCORE,
                Job.tg_alerted == False,
            )
            .order_by(Job.legitimacy_score.desc())
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

    lines.append(f"<i>Run /top10 or visit the dashboard for full details.</i>")

    await _send_chunked(bot, chat_id, "\n".join(lines))
    logger.info("Digest sent: %d jobs", len(jobs))
    return len(jobs)


# ─────────────────────────────────────────────
#  Alerts mode
# ─────────────────────────────────────────────

async def run_alerts(bot, chat_id: str) -> int:
    """Send individual alerts for unnotified high-confidence jobs."""
    jobs = _unalerted_jobs()
    if not jobs:
        logger.info("Alerts: no new jobs with score ≥ %d", ALERT_MIN_SCORE)
        return 0

    sent_ids = []
    for job in jobs:
        score = job.legitimacy_score or 0
        sal = f"\n💰 {job.salary}" if job.salary else ""
        loc = f"\n📍 {job.location}" if job.location else ""
        date = f"\n📅 {job.date_posted}" if job.date_posted else ""

        breakdown = job.score_breakdown or {}
        signals_hit = [k for k, v in breakdown.items() if v > 0]
        signals_str = " · ".join(signals_hit) if signals_hit else "no breakdown"

        msg = (
            f"🚨 <b>NEW JOB ALERT</b> — Score: {score}/100\n\n"
            f"<b>{job.title}</b>\n"
            f"🏢 {job.company}{loc}{sal}{date}\n\n"
            f"✅ Signals: {signals_str}\n\n"
            f"🔗 <a href='{job.url}'>{job.url}</a>"
        )

        ok = await _send_message(bot, chat_id, msg)
        if ok:
            sent_ids.append(job.id)

    if sent_ids:
        _mark_alerted(sent_ids)
        logger.info("Alerts sent: %d new high-confidence jobs", len(sent_ids))

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


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot Telegram notifier")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--digest", action="store_true", help="Send daily digest")
    group.add_argument("--alerts", action="store_true", help="Send instant alerts for ≥85 jobs")
    args = parser.parse_args()

    mode = "digest" if args.digest else "alerts"
    return asyncio.run(main_async(mode))


if __name__ == "__main__":
    sys.exit(main())
