"""
telegram_bot.py — Persistent polling Telegram bot (for local use only).

Run:  py telegram_bot.py

Commands:
  /start   — Welcome message
  /help    — Command list
  /status  — DB statistics
  /top10   — Top 10 jobs by legitimacy score
  /ghosts  — List suspected ghost jobs
  /search <keyword> — Search jobs by title/company

Note: This uses long-polling. Use the Flask webhook handler
(dashboard.py) for production/Render deployment instead.
"""
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_bot")

HELP_TEXT = (
    "<b>🤖 Job Discovery Bot</b>\n\n"
    "<b>Commands:</b>\n"
    "/status — System statistics\n"
    "/top10  — Top 10 legitimate jobs\n"
    "/ghosts — Suspected ghost jobs\n"
    "/search &lt;keyword&gt; — Search jobs\n"
    "/help   — This message\n\n"
    "<i>High confidence jobs (≥85) trigger instant alerts automatically.</i>"
)


def _get_engine():
    from core.database import get_engine
    return get_engine()


def _db_stats() -> dict:
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import func
    from core.models import Job
    from datetime import datetime, timezone, timedelta

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        total   = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
        avg_s   = session.query(func.avg(Job.legitimacy_score)).filter(
            Job.is_active == True, Job.legitimacy_score != None
        ).scalar()
        today   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        new_today = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.first_seen >= today
        ).scalar() or 0
        high    = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.legitimacy_score >= 70
        ).scalar() or 0
        v_high  = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.legitimacy_score >= 85
        ).scalar() or 0
        ghosts  = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.suspected_ghost == True
        ).scalar() or 0
        return {
            "total": total,
            "avg_score": round(avg_s, 1) if avg_s else None,
            "new_today": new_today,
            "high": high,
            "v_high": v_high,
            "ghosts": ghosts,
        }
    finally:
        session.close()


def _status_msg() -> str:
    s = _db_stats()
    avg = f"{s['avg_score']}/100" if s["avg_score"] else "not yet scored"
    return (
        "<b>📊 Job Scanner Status</b>\n\n"
        f"Active jobs:             {s['total']}\n"
        f"Avg legitimacy score:    {avg}\n"
        f"Today's new jobs:        {s['new_today']}\n"
        f"High confidence (≥70):   {s['high']}\n"
        f"Very high (≥85):         {s['v_high']}\n"
        f"Suspected ghosts:        {s['ghosts']}"
    )


def _top_jobs_msg(limit: int = 10) -> list[str]:
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.legitimacy_score != None)
            .order_by(Job.legitimacy_score.desc())
            .limit(limit)
            .all()
        )
        if not jobs:
            return ["No scored jobs found. Run <code>py run_all.py --score</code> first."]

        lines = [f"<b>🏆 Top {limit} Jobs by Legitimacy Score</b>\n"]
        for i, job in enumerate(jobs, 1):
            score = job.legitimacy_score or 0
            if score >= 85:
                badge = "🟢"
            elif score >= 70:
                badge = "🟡"
            else:
                badge = "🔴"
            loc = f" | 📍 {job.location}" if job.location else ""
            sal = f" | 💰 {job.salary}" if job.salary else ""
            lines.append(
                f"{i}. {badge} <b>[{score}]</b> {job.title}\n"
                f"   🏢 {job.company}{loc}{sal}\n"
                f"   🔗 <a href='{job.url}'>View Job</a>\n"
            )
        return _chunk_messages("\n".join(lines))
    finally:
        session.close()


def _ghost_jobs_msg() -> list[str]:
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.suspected_ghost == True)
            .order_by(Job.legitimacy_score.asc())
            .limit(20)
            .all()
        )
        if not jobs:
            return ["✅ No suspected ghost jobs found!"]

        lines = [f"<b>👻 Suspected Ghost Jobs ({len(jobs)})</b>\n"]
        for job in jobs:
            score = job.legitimacy_score or 0
            lines.append(
                f"⚠️ <b>[{score}]</b> {job.title}\n"
                f"   🏢 {job.company}\n"
                f"   🔗 <a href='{job.url}'>View</a>\n"
            )
        return _chunk_messages("\n".join(lines))
    finally:
        session.close()


def _search_jobs_msg(keyword: str) -> list[str]:
    from sqlalchemy.orm import sessionmaker
    from core.models import Job

    if not keyword.strip():
        return ["Usage: /search &lt;keyword&gt;  e.g. /search java developer"]

    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        jobs = (
            session.query(Job)
            .filter(
                Job.is_active == True,
                (Job.title.ilike(f"%{keyword}%") | Job.company.ilike(f"%{keyword}%")),
            )
            .order_by(Job.legitimacy_score.desc())
            .limit(10)
            .all()
        )
        if not jobs:
            return [f"No jobs found for <b>{keyword}</b>"]

        lines = [f"<b>🔍 Search: '{keyword}' ({len(jobs)} results)</b>\n"]
        for job in jobs:
            score = job.legitimacy_score
            score_str = f"[{score}] " if score is not None else ""
            badge = "🟢" if (score or 0) >= 70 else "🔴"
            lines.append(
                f"{badge} <b>{score_str}{job.title}</b>\n"
                f"   🏢 {job.company}\n"
                f"   🔗 <a href='{job.url}'>View</a>\n"
            )
        return _chunk_messages("\n".join(lines))
    finally:
        session.close()


def _chunk_messages(text: str, limit: int = 4096) -> list[str]:
    """Split a long message into ≤4096 char chunks at newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


async def run_bot():
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_html(
            "<b>👋 Welcome to the Job Discovery Bot!</b>\n\n"
            "I scan Irish job boards, filter out ghost jobs, and surface real opportunities.\n\n"
            + HELP_TEXT
        )

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_html(HELP_TEXT)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.reply_html(_status_msg())
        except Exception as exc:
            await update.message.reply_text(f"Error getting status: {exc}")

    async def cmd_top10(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            for chunk in _top_jobs_msg(10):
                await update.message.reply_html(
                    chunk, disable_web_page_preview=True
                )
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    async def cmd_ghosts(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            for chunk in _ghost_jobs_msg():
                await update.message.reply_html(
                    chunk, disable_web_page_preview=True
                )
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyword = " ".join(context.args) if context.args else ""
        try:
            for chunk in _search_jobs_msg(keyword):
                await update.message.reply_html(
                    chunk, disable_web_page_preview=True
                )
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("top10",  cmd_top10))
    app.add_handler(CommandHandler("ghosts", cmd_ghosts))
    app.add_handler(CommandHandler("search", cmd_search))

    logger.info("Telegram bot started (polling)...")
    await app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(run_bot())
