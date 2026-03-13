"""
dashboard.py — Flask web dashboard + Telegram webhook handler.

Routes:
  GET  /                → Home (stats + top jobs)
  GET  /jobs            → Full filterable jobs table
  GET  /jobs/partial    → HTMX partial for live filtering
  GET  /jobs/<id>       → Job detail with score breakdown
  POST /jobs/<id>/track → Update application tracker
  GET  /tracker         → Kanban board
  GET  /companies       → Company credibility table
  POST /telegram/webhook → Telegram webhook handler

Run locally:  py dashboard.py
Production:   gunicorn dashboard:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
"""
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone  # noqa: F401
from functools import lru_cache  # noqa: F401

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, abort
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dashboard")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me-in-prod")


# ─────────────────────────────────────────────
#  DB helpers
# ─────────────────────────────────────────────

def _get_engine():
    from core.database import get_engine
    return get_engine()


def _new_session():
    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


# ─────────────────────────────────────────────
#  Stats helper
# ─────────────────────────────────────────────

def _get_stats(session) -> dict:
    from core.models import Job
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    total   = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
    avg_s   = session.query(func.avg(Job.legitimacy_score)).filter(
        Job.is_active == True, Job.legitimacy_score != None
    ).scalar()
    high    = session.query(func.count(Job.id)).filter(
        Job.is_active == True, Job.legitimacy_score >= 70
    ).scalar() or 0
    v_high  = session.query(func.count(Job.id)).filter(
        Job.is_active == True, Job.legitimacy_score >= 85
    ).scalar() or 0
    ghosts  = session.query(func.count(Job.id)).filter(
        Job.is_active == True, Job.suspected_ghost == True
    ).scalar() or 0
    new_today = session.query(func.count(Job.id)).filter(
        Job.is_active == True, Job.first_seen >= today
    ).scalar() or 0

    ij_count = session.query(func.count(Job.id)).filter(
        Job.is_active == True, Job.source == "irishjobs"
    ).scalar() or 0
    ind_count = session.query(func.count(Job.id)).filter(
        Job.is_active == True, Job.source == "indeed"
    ).scalar() or 0

    return {
        "total": total,
        "avg_score": round(avg_s, 1) if avg_s else None,
        "high": high,
        "v_high": v_high,
        "ghosts": ghosts,
        "new_today": new_today,
        "irishjobs": ij_count,
        "indeed": ind_count,
    }


# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    session = _new_session()
    try:
        from core.models import Job
        stats = _get_stats(session)
        top_jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.legitimacy_score != None)
            .order_by(Job.legitimacy_score.desc())
            .limit(10)
            .all()
        )
        return render_template("index.html", stats=stats, top_jobs=top_jobs)
    except Exception as exc:
        logger.error("index error: %s", exc)
        return render_template("index.html", stats={}, top_jobs=[], error=str(exc))
    finally:
        session.close()


@app.route("/jobs")
def jobs():
    return render_template("jobs.html")


@app.route("/jobs/partial")
def jobs_partial():
    session = _new_session()
    try:
        from core.models import Job

        q        = request.args.get("q", "").strip()
        min_score = int(request.args.get("min_score", 0) or 0)
        source   = request.args.get("source", "")
        ghost    = request.args.get("ghost", "")
        sort     = request.args.get("sort", "score")
        page     = int(request.args.get("page", 1) or 1)
        per_page = 25

        query = session.query(Job).filter(Job.is_active == True)

        if q:
            query = query.filter(
                Job.title.ilike(f"%{q}%") | Job.company.ilike(f"%{q}%")
            )
        if min_score:
            query = query.filter(Job.legitimacy_score >= min_score)
        if source in ("irishjobs", "indeed"):
            query = query.filter(Job.source == source)
        if ghost == "true":
            query = query.filter(Job.suspected_ghost == True)
        elif ghost == "false":
            query = query.filter(Job.suspected_ghost == False)

        if sort == "score":
            query = query.order_by(Job.legitimacy_score.desc().nulls_last())
        elif sort == "date":
            query = query.order_by(Job.first_seen.desc())
        elif sort == "company":
            query = query.order_by(Job.company.asc())

        total  = query.count()
        offset = (page - 1) * per_page
        jobs_page = query.offset(offset).limit(per_page).all()

        return render_template(
            "partials/jobs_rows.html",
            jobs=jobs_page,
            total=total,
            page=page,
            per_page=per_page,
        )
    except Exception as exc:
        logger.error("jobs_partial error: %s", exc)
        return f"<tr><td colspan='8'>Error loading jobs: {exc}</td></tr>", 500
    finally:
        session.close()


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    session = _new_session()
    try:
        from core.models import Job, ApplicationTracker, VALID_STATUSES
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            abort(404)

        tracker = session.query(ApplicationTracker).filter(
            ApplicationTracker.job_id == job_id
        ).first()

        # Build signal breakdown display data
        from core.scorer import SIGNAL_WEIGHTS
        signals = []
        breakdown = job.score_breakdown or {}
        for signal, max_pts in SIGNAL_WEIGHTS.items():
            earned = breakdown.get(signal, 0)
            signals.append({
                "name": signal.replace("_", " ").title(),
                "key": signal,
                "earned": earned,
                "max": max_pts,
                "hit": earned > 0,
                "pct": int(earned / max_pts * 100) if max_pts else 0,
            })

        return render_template(
            "job_detail.html",
            job=job,
            tracker=tracker,
            signals=signals,
            valid_statuses=VALID_STATUSES,
        )
    except Exception as exc:
        logger.error("job_detail error: %s", exc)
        abort(500)
    finally:
        session.close()


@app.route("/jobs/<int:job_id>/track", methods=["POST"])
def track_job(job_id: int):
    session = _new_session()
    try:
        from core.models import Job, ApplicationTracker, VALID_STATUSES

        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return jsonify({"error": "Job not found"}), 404

        status = request.form.get("status", "saved")
        if status not in VALID_STATUSES:
            return jsonify({"error": f"Invalid status. Valid: {VALID_STATUSES}"}), 400
        notes = request.form.get("notes", "")

        tracker = session.query(ApplicationTracker).filter(
            ApplicationTracker.job_id == job_id
        ).first()

        if tracker:
            tracker.status     = status
            tracker.notes      = notes
            tracker.updated_at = datetime.now(timezone.utc)
            if status == "applied" and not tracker.applied_at:
                tracker.applied_at = datetime.now(timezone.utc)
        else:
            tracker = ApplicationTracker(
                job_id     = job_id,
                status     = status,
                notes      = notes,
                applied_at = datetime.now(timezone.utc) if status == "applied" else None,
            )
            session.add(tracker)

        session.commit()
        return jsonify({"success": True, "status": status})

    except Exception as exc:
        session.rollback()
        logger.error("track_job error: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        session.close()


@app.route("/tracker")
def tracker():
    session = _new_session()
    try:
        from core.models import ApplicationTracker, Job, VALID_STATUSES

        all_statuses = VALID_STATUSES
        columns = {}
        for status in all_statuses:
            items = (
                session.query(ApplicationTracker, Job)
                .join(Job, ApplicationTracker.job_id == Job.id)
                .filter(ApplicationTracker.status == status)
                .order_by(ApplicationTracker.updated_at.desc())
                .all()
            )
            columns[status] = items

        return render_template("apply_tracker.html", columns=columns, statuses=all_statuses)
    except Exception as exc:
        logger.error("tracker error: %s", exc)
        return render_template("apply_tracker.html", columns={}, statuses=[], error=str(exc))
    finally:
        session.close()


@app.route("/companies")
def companies():
    session = _new_session()
    try:
        from core.models import Job
        from sqlalchemy import case

        rows = (
            session.query(
                Job.company,
                func.count(Job.id).label("total_jobs"),
                func.avg(Job.legitimacy_score).label("avg_score"),
                func.max(Job.legitimacy_score).label("max_score"),
                (func.sum(case((Job.suspected_ghost == True, 1), else_=0)) * 100.0
                 / func.count(Job.id)).label("ghost_pct"),
            )
            .filter(Job.is_active == True)
            .group_by(Job.company)
            .order_by(func.avg(Job.legitimacy_score).desc().nulls_last())
            .all()
        )

        companies_data = []
        for row in rows:
            companies_data.append({
                "name":       row.company,
                "total_jobs": row.total_jobs,
                "avg_score":  round(row.avg_score, 1) if row.avg_score else None,
                "max_score":  row.max_score,
                "ghost_pct":  round(row.ghost_pct, 0) if row.ghost_pct else 0,
            })

        return render_template("companies.html", companies=companies_data)
    except Exception as exc:
        logger.error("companies error: %s", exc)
        return render_template("companies.html", companies=[], error=str(exc))
    finally:
        session.close()


# ─────────────────────────────────────────────
#  Telegram webhook
# ─────────────────────────────────────────────

def _tg_send(chat_id: str, text: str) -> None:
    """Send Telegram message using urllib (no external deps)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        logger.error("_tg_send failed: %s", exc)


def _tg_send_chunked(chat_id: str, text: str, limit: int = 4096) -> None:
    """Split and send long messages."""
    if len(text) <= limit:
        _tg_send(chat_id, text)
        return
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0
    for line in lines:
        ll = len(line) + 1
        if current_len + ll > limit:
            _tg_send(chat_id, "\n".join(current))
            current = [line]
            current_len = ll
        else:
            current.append(line)
            current_len += ll
    if current:
        _tg_send(chat_id, "\n".join(current))


def _tg_status(chat_id: str) -> None:
    session = _new_session()
    try:
        stats = _get_stats(session)
        avg = f"{stats['avg_score']}/100" if stats["avg_score"] else "not scored"
        msg = (
            "<b>📊 Job Scanner Status</b>\n\n"
            f"Active jobs:             {stats['total']}\n"
            f"Avg score:               {avg}\n"
            f"Today's new jobs:        {stats['new_today']}\n"
            f"High confidence (≥70):   {stats['high']}\n"
            f"Very high (≥85):         {stats['v_high']}\n"
            f"Suspected ghosts:        {stats['ghosts']}\n\n"
            f"IrishJobs:               {stats['irishjobs']}\n"
            f"Indeed:                  {stats['indeed']}"
        )
        _tg_send(chat_id, msg)
    except Exception as exc:
        _tg_send(chat_id, f"Error: {exc}")
    finally:
        session.close()


def _tg_top10(chat_id: str) -> None:
    session = _new_session()
    try:
        from core.models import Job
        jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.legitimacy_score != None)
            .order_by(Job.legitimacy_score.desc())
            .limit(10)
            .all()
        )
        if not jobs:
            _tg_send(chat_id, "No scored jobs yet. Run the scraper first.")
            return

        lines = ["<b>🏆 Top 10 Jobs</b>\n"]
        for i, job in enumerate(jobs, 1):
            score = job.legitimacy_score or 0
            badge = "🟢" if score >= 85 else ("🟡" if score >= 70 else "🔴")
            loc = f"  📍 {job.location}" if job.location else ""
            lines.append(
                f"{i}. {badge} <b>[{score}]</b> {job.title}\n"
                f"   🏢 {job.company}{loc}\n"
                f"   🔗 <a href='{job.url}'>View</a>"
            )
        _tg_send_chunked(chat_id, "\n\n".join(lines))
    except Exception as exc:
        _tg_send(chat_id, f"Error: {exc}")
    finally:
        session.close()


def _tg_help(chat_id: str) -> None:
    _tg_send(chat_id,
        "<b>🤖 Job Discovery Bot</b>\n\n"
        "/status — System stats\n"
        "/top10  — Top 10 legit jobs\n"
        "/ghosts — Ghost job list\n"
        "/help   — This message"
    )


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Handle incoming Telegram webhook updates."""
    try:
        data = request.get_json(silent=True) or {}
        # Extract message from update or edited_message
        msg = data.get("message") or data.get("edited_message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()

        if not chat_id or not text:
            return "ok", 200

        # Route commands
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            _tg_help(chat_id)
        elif cmd == "/status":
            _tg_status(chat_id)
        elif cmd == "/top10":
            _tg_top10(chat_id)
        else:
            _tg_help(chat_id)

    except Exception as exc:
        logger.error("webhook error: %s", exc)

    # Always return 200 — never let Telegram retry
    return "ok", 200


# ─────────────────────────────────────────────
#  Webhook registration (run once at deployment)
# ─────────────────────────────────────────────

def register_webhook(render_url: str) -> bool:
    """
    Point Telegram at this server's webhook endpoint.
    Call once after deploying to Render:
      from dashboard import register_webhook
      register_webhook("https://your-app.onrender.com")
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return False

    webhook_url = f"{render_url.rstrip('/')}/telegram/webhook"
    api_url = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = json.dumps({"url": webhook_url}).encode()
    try:
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info("Webhook registered: %s", webhook_url)
                return True
            else:
                logger.error("Webhook registration failed: %s", result)
                return False
    except Exception as exc:
        logger.error("register_webhook error: %s", exc)
        return False


# ─────────────────────────────────────────────
#  Template filters
# ─────────────────────────────────────────────

@app.template_filter("score_color")
def score_color(score) -> str:
    if score is None:
        return "secondary"
    if score >= 85:
        return "success"
    if score >= 70:
        return "warning"
    if score >= 50:
        return "info"
    return "danger"


@app.template_filter("score_label")
def score_label(score) -> str:
    if score is None:
        return "Unscored"
    if score >= 85:
        return "Very High"
    if score >= 70:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"


@app.template_filter("timeago")
def timeago(dt) -> str:
    if not dt:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    if delta.days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            return "just now"
        return f"{hours}h ago"
    if delta.days == 1:
        return "1 day ago"
    if delta.days < 30:
        return f"{delta.days} days ago"
    if delta.days < 365:
        return f"{delta.days // 30}mo ago"
    return f"{delta.days // 365}y ago"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    logger.info("Starting dashboard on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
