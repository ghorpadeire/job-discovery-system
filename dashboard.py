# -*- coding: utf-8 -*-
"""
dashboard.py — Flask web dashboard for the Job Discovery System.

Pages:
  /               Home: live stats overview
  /jobs           Filterable + sortable jobs table (HTMX live filtering)
  /job/<id>       Full job detail, score breakdown, AI analysis
  /apply-tracker  Kanban board (Saved → Applied → Interview → Offer → Rejected)
  /companies      Company credibility overview
  /live           Real-time scrape monitor (SSE)

Run:
  py dashboard.py                  # localhost only, port 5000
  py dashboard.py --public         # + pyngrok public URL
  py dashboard.py --port 8080
  py dashboard.py --host 0.0.0.0   # expose to LAN
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, text

from core.logging_config import setup_logging
setup_logging()

import logging
logger = logging.getLogger(__name__)

from core.config import (
    DASHBOARD_PORT, SECRET_KEY,
    REDIS_HOST, REDIS_PORT,
    TELEGRAM_BOT_TOKEN,
)

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.secret_key = SECRET_KEY

# ── Public URL (set by --public / pyngrok) ────────────────────────────────────
_public_url: str = ""

# ── DB imports (lazy to avoid import-order issues) ────────────────────────────
from core.database import get_engine, get_session
from core.models import ApplicationTracker, Job, TRACKER_STATUSES, migrate_tracker_table
from core.progress import emitter, CHANNEL

# Ensure tracker table exists at import time (safe: checkfirst=True).
migrate_tracker_table(get_engine())

# ── Helpers ───────────────────────────────────────────────────────────────────

def _score_class(score):
    if score is None: return "secondary"
    if score >= 70:   return "success"
    if score >= 40:   return "warning"
    return "danger"


def _score_pct(score, max_val=100):
    if score is None: return 0
    return max(0, min(100, round(score / max_val * 100)))


def _relative_date(dt):
    if dt is None: return "—"
    now  = datetime.utcnow()
    diff = now - dt
    if diff.total_seconds() < 3600:  return "just now"
    if diff.days == 0:               return f"{int(diff.total_seconds() // 3600)}h ago"
    if diff.days == 1:               return "yesterday"
    return f"{diff.days}d ago"


app.jinja_env.filters["score_class"]    = _score_class
app.jinja_env.filters["score_pct"]      = _score_pct
app.jinja_env.filters["relative_date"]  = _relative_date


# ── Route helpers ──────────────────────────────────────────────────────────────

def _apply_job_filters(query, args):
    min_score   = args.get("min_score",  type=float)
    max_score   = args.get("max_score",  type=float)
    company     = args.get("company",    "").strip()
    source      = args.get("source",     "").strip()
    days_back   = args.get("days_back",  type=int)
    show_ghost  = args.get("show_ghost",  "")
    scored_only = args.get("scored_only", "")
    sort_by     = args.get("sort_by",    "combined_score")

    if min_score  is not None: query = query.filter(Job.combined_score >= min_score)
    if max_score  is not None: query = query.filter(Job.combined_score <= max_score)
    if company:                query = query.filter(Job.company.ilike(f"%{company}%"))
    if source:                 query = query.filter(Job.source == source)
    if days_back:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        query  = query.filter(Job.first_seen >= cutoff)
    if show_ghost  == "1": query = query.filter(Job.suspected_ghost == True)
    if scored_only == "1": query = query.filter(Job.combined_score.isnot(None))

    sort_map = {
        "combined_score":   Job.combined_score.desc().nullslast(),
        "legitimacy_score": Job.legitimacy_score.desc().nullslast(),
        "relevance_score":  Job.relevance_score.desc().nullslast(),
        "first_seen":       Job.first_seen.desc(),
        "company":          Job.company.asc(),
    }
    query = query.order_by(sort_map.get(sort_by, Job.combined_score.desc().nullslast()))
    return query


# ── LAN IP helper ──────────────────────────────────────────────────────────────

def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.context_processor
def inject_now():
    return {"now": datetime.now().strftime("%H:%M")}


# ── / ─────────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    with get_session() as session:
        total_active  = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
        avg_legit     = session.query(func.avg(Job.legitimacy_score)).filter(
                            Job.is_active == True, Job.legitimacy_score.isnot(None)).scalar()
        avg_legit     = round(avg_legit, 1) if avg_legit else None
        avg_combined  = session.query(func.avg(Job.combined_score)).filter(
                            Job.is_active == True, Job.combined_score.isnot(None)).scalar()
        avg_combined  = round(avg_combined, 1) if avg_combined else None

        cutoff_today  = datetime.utcnow() - timedelta(hours=24)
        added_today   = session.query(func.count(Job.id)).filter(
                            Job.first_seen >= cutoff_today).scalar() or 0

        ghost_count   = session.query(func.count(Job.id)).filter(
                            Job.is_active == True, Job.suspected_ghost == True).scalar() or 0

        high_conf     = session.query(func.count(Job.id)).filter(
                            Job.is_active == True, Job.combined_score >= 70).scalar() or 0

        scored_count  = session.query(func.count(Job.id)).filter(
                            Job.is_active == True, Job.combined_score.isnot(None)).scalar() or 0

        in_tracker    = session.query(func.count(ApplicationTracker.id)).scalar() or 0

        top_jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.combined_score.isnot(None))
            .order_by(Job.combined_score.desc())
            .limit(8).all()
        )

        source_stats = session.execute(text("""
            SELECT source,
                   COUNT(*)                AS cnt,
                   AVG(combined_score)     AS avg_combined
            FROM jobs
            WHERE is_active = TRUE AND combined_score IS NOT NULL
            GROUP BY source
            ORDER BY cnt DESC
        """)).fetchall()

    return render_template(
        "index.html",
        total_active=total_active,  avg_legit=avg_legit,      avg_combined=avg_combined,
        added_today=added_today,    ghost_count=ghost_count,  high_conf=high_conf,
        scored_count=scored_count,  in_tracker=in_tracker,    top_jobs=top_jobs,
        source_stats=source_stats,
    )


# ── /jobs ─────────────────────────────────────────────────────────────────────
@app.route("/jobs")
def jobs():
    with get_session() as session:
        sources = [r[0] for r in session.execute(
            text("SELECT DISTINCT source FROM jobs WHERE source IS NOT NULL ORDER BY source")
        ).fetchall()]
        total = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
    return render_template("jobs.html", sources=sources, total=total, args=request.args)


@app.route("/jobs/rows")
def jobs_rows():
    with get_session() as session:
        q         = session.query(Job).filter(Job.is_active == True)
        q         = _apply_job_filters(q, request.args)
        jobs_list = q.limit(200).all()
        count     = q.count()
    return render_template("partials/jobs_rows.html", jobs=jobs_list, count=count)


# ── /job/<id> ─────────────────────────────────────────────────────────────────
@app.route("/job/<int:job_id>")
def job_detail(job_id):
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return render_template("404.html"), 404
        tracker = session.query(ApplicationTracker).filter_by(job_id=job_id).first()

    breakdown  = {}
    signal_max = {
        "career_page_match": 25, "recently_posted": 20, "company_volume": 15,
        "not_a_repost":      15, "url_resolves":    10, "has_salary":     10,
        "rich_description":   5,
    }
    if job.score_breakdown:
        for signal, pts in job.score_breakdown.items():
            breakdown[signal] = {
                "pts":    pts,
                "max":    signal_max.get(signal, 10),
                "pct":    _score_pct(pts, signal_max.get(signal, 10)),
                "label":  signal.replace("_", " ").title(),
                "earned": pts > 0,
            }

    return render_template(
        "job_detail.html",
        job=job, tracker=tracker, breakdown=breakdown, statuses=TRACKER_STATUSES,
    )


# ── /apply-tracker ────────────────────────────────────────────────────────────
@app.route("/apply-tracker")
def apply_tracker():
    with get_session() as session:
        rows = (
            session.query(ApplicationTracker, Job)
            .join(Job, ApplicationTracker.job_id == Job.id)
            .order_by(ApplicationTracker.updated_at.desc())
            .all()
        )
    columns = {s: [] for s in TRACKER_STATUSES}
    for entry, job in rows:
        if entry.status in columns:
            columns[entry.status].append({"entry": entry, "job": job})
    return render_template("apply_tracker.html", columns=columns, statuses=TRACKER_STATUSES)


@app.route("/apply-tracker/add/<int:job_id>", methods=["POST"])
def tracker_add(job_id):
    with get_session() as session:
        existing = session.query(ApplicationTracker).filter_by(job_id=job_id).first()
        if not existing:
            session.add(ApplicationTracker(job_id=job_id, status="saved"))
            session.commit()
    if request.headers.get("HX-Request"):
        return redirect(url_for("apply_tracker")), 303
    return redirect(request.referrer or url_for("apply_tracker"))


@app.route("/apply-tracker/move/<int:entry_id>/<string:new_status>", methods=["POST"])
def tracker_move(entry_id, new_status):
    if new_status not in TRACKER_STATUSES:
        return "Invalid status", 400
    with get_session() as session:
        entry = session.get(ApplicationTracker, entry_id)
        if entry:
            entry.status     = new_status
            entry.updated_at = datetime.utcnow()
            if new_status == "applied" and entry.applied_at is None:
                entry.applied_at = datetime.utcnow()
            session.commit()
    if request.headers.get("HX-Request"):
        return redirect(url_for("apply_tracker")), 303
    return redirect(url_for("apply_tracker"))


@app.route("/apply-tracker/remove/<int:entry_id>", methods=["POST"])
def tracker_remove(entry_id):
    with get_session() as session:
        entry = session.get(ApplicationTracker, entry_id)
        if entry:
            session.delete(entry)
            session.commit()
    if request.headers.get("HX-Request"):
        return redirect(url_for("apply_tracker")), 303
    return redirect(url_for("apply_tracker"))


@app.route("/apply-tracker/notes/<int:entry_id>", methods=["POST"])
def tracker_notes(entry_id):
    notes = request.form.get("notes", "").strip()
    with get_session() as session:
        entry = session.get(ApplicationTracker, entry_id)
        if entry:
            entry.notes      = notes or None
            entry.updated_at = datetime.utcnow()
            session.commit()
    return "", 204


# ── /companies ────────────────────────────────────────────────────────────────
@app.route("/companies")
def companies():
    sort_by  = request.args.get("sort", "open_roles")
    sort_map = {
        "open_roles":  "cnt DESC",
        "avg_score":   "avg_combined DESC NULLS LAST",
        "avg_legit":   "avg_legit DESC NULLS LAST",
        "ghost_rate":  "ghost_rate DESC NULLS LAST",
        "company":     "company ASC",
    }
    order_clause = sort_map.get(sort_by, "cnt DESC")

    with get_session() as session:
        rows = session.execute(text(f"""
            SELECT
                company,
                COUNT(*)                                      AS cnt,
                AVG(legitimacy_score)::NUMERIC(5,1)           AS avg_legit,
                AVG(combined_score)::NUMERIC(5,1)             AS avg_combined,
                SUM(CASE WHEN suspected_ghost THEN 1 ELSE 0 END) AS ghost_cnt,
                ROUND(
                    100.0 * SUM(CASE WHEN suspected_ghost THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0)
                )                                             AS ghost_rate,
                MAX(first_seen)                               AS last_seen,
                ARRAY_AGG(DISTINCT source)                    AS sources
            FROM jobs
            WHERE is_active = TRUE
            GROUP BY company
            ORDER BY {order_clause}
            LIMIT 200
        """)).fetchall()

    return render_template("companies.html", rows=rows, sort_by=sort_by)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE SCRAPE MONITOR  (SSE)
# ══════════════════════════════════════════════════════════════════════════════

# Track connected SSE clients count across threads
_sse_client_count = 0
_sse_client_lock  = threading.Lock()


@app.route("/live")
def live():
    """Live Scrape Monitor page."""
    return render_template("live.html", public_url=_public_url or None)


@app.route("/live/stream")
def live_stream():
    """
    SSE endpoint.  Each browser tab gets its own queue.Queue.
    Events arrive from:
      • emitter.register_listener(q)  — in-process events from run_all.py
      • Redis subscriber thread        — cross-process events
    """
    global _sse_client_count

    client_q: queue.Queue = queue.Queue(maxsize=200)
    emitter.register_listener(client_q)

    with _sse_client_lock:
        _sse_client_count += 1
        viewers = _sse_client_count

    # Tell the new client its initial viewer count + history size
    init_payload = json.dumps({
        "type":    "init",
        "viewers": viewers,
    })

    def generate():
        global _sse_client_count
        try:
            # Send init event
            yield f"data: {init_payload}\n\n"

            heartbeat_interval = 15   # seconds
            last_hb            = time.time()

            while True:
                # Poll queue with short timeout so heartbeats keep firing
                try:
                    payload = client_q.get(timeout=1.0)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    pass

                # Heartbeat
                now = time.time()
                if now - last_hb >= heartbeat_interval:
                    hb = json.dumps({"type": "heartbeat", "ts": datetime.utcnow().isoformat()})
                    yield f"data: {hb}\n\n"
                    last_hb = now

        except GeneratorExit:
            pass
        finally:
            emitter.unregister_listener(client_q)
            with _sse_client_lock:
                _sse_client_count = max(0, _sse_client_count - 1)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",     # disable nginx buffering
        },
    )


@app.route("/live/history")
def live_history():
    """Return recent event history as JSON (for page-load replay)."""
    return jsonify(emitter.get_history())


@app.route("/live/stats")
def live_stats():
    """Return current run stats as JSON."""
    return jsonify(emitter.get_stats())


@app.route("/live/clear", methods=["POST"])
def live_clear():
    """Wipe the in-process history buffer."""
    emitter.clear_history()
    return "", 204


# ── Redis → in-process bridge (background thread) ─────────────────────────────

def _redis_subscriber_thread():
    """
    Subscribe to Redis 'scrape:live' channel and forward messages to
    all in-process SSE listeners.  Reconnects on failure.
    """
    while True:
        try:
            import redis
            r   = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            ps  = r.pubsub(ignore_subscribe_messages=True)
            ps.subscribe(CHANNEL)
            app.logger.info(f"[SSE] Redis subscriber connected on channel '{CHANNEL}'")
            for message in ps.listen():
                if message and message.get("type") == "message":
                    data = message.get("data", "")
                    # Fan out to all connected SSE clients
                    with emitter._listener_lock:
                        for q in list(emitter._listeners):
                            try:
                                q.put_nowait(data)
                            except queue.Full:
                                pass
        except Exception as exc:
            app.logger.debug(f"[SSE] Redis subscriber error: {exc} — retrying in 5s")
            time.sleep(5)


# Start the Redis bridge in a daemon thread (only if Redis is available)
_redis_bridge = threading.Thread(target=_redis_subscriber_thread, daemon=True)
_redis_bridge.start()


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════

_BOT_TOKEN = TELEGRAM_BOT_TOKEN


def _tg_send(chat_id, text):
    if not _BOT_TOKEN: return
    url  = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        app.logger.error(f"Telegram send error: {exc}")


def _tg_status(chat_id):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as session:
        active      = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
        avg_score   = session.query(func.avg(Job.combined_score)).filter(
                          Job.is_active == True, Job.combined_score.isnot(None)).scalar()
        added_today = session.query(func.count(Job.id)).filter(
                          Job.is_active == True, Job.first_seen >= today_start).scalar() or 0
        high_conf   = session.query(func.count(Job.id)).filter(
                          Job.is_active == True, Job.combined_score >= 70).scalar() or 0
        very_high   = session.query(func.count(Job.id)).filter(
                          Job.is_active == True, Job.combined_score >= 85).scalar() or 0

    avg_s = f"{avg_score:.1f}" if avg_score else "N/A"
    now_s = datetime.now().strftime("%d %b %Y, %H:%M")
    msg = (
        f"<b>📈 Job Search Status</b>\n🕒 {now_s}\n\n"
        f"📋 Active jobs:   <b>{active}</b>\n"
        f"⭐ Avg score:     <b>{avg_s}/100</b>\n"
        f"🆕 Added today:   <b>{added_today}</b>\n"
        f"✅ Score ≥70:     <b>{high_conf}</b>\n"
        f"🚀 Score ≥85:     <b>{very_high}</b>"
    )
    _tg_send(chat_id, msg)


def _tg_top10(chat_id):
    with get_session() as session:
        jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.combined_score.isnot(None))
            .order_by(Job.combined_score.desc())
            .limit(10).all()
        )
    if not jobs:
        _tg_send(chat_id, "No scored jobs found yet.")
        return
    lines = ["<b>🏆 Top 10 Jobs</b>\n"]
    for i, job in enumerate(jobs, 1):
        lines.append(
            f"<b>#{i}</b> {job.combined_score or 0:.0f}/100\n"
            f"🏢 {job.company or 'Unknown'}\n"
            f"💼 {job.title or 'Unknown'}\n"
            f'🔗 <a href="{job.url or "#"}">Apply Here</a>\n'
        )
    chunk = ""
    for block in lines:
        if len(chunk) + len(block) > 3800:
            _tg_send(chat_id, chunk); chunk = block
        else:
            chunk += block
    if chunk: _tg_send(chat_id, chunk)


def _tg_help(chat_id):
    _tg_send(chat_id, (
        "<b>🤖 Job Search Bot — Commands</b>\n\n"
        "/status  — DB snapshot (job counts, avg score)\n"
        "/top10   — 10 highest-scoring active jobs\n"
        "/live    — get the live monitor URL\n"
        "/help    — this message\n\n"
        "<i>You also receive:</i>\n"
        "• 🌅 Daily digest at 10:05 (jobs ≥70)\n"
        "• 🚨 Instant alert every 30 min for jobs ≥85"
    ))


def _tg_live(chat_id):
    url = _public_url or "http://127.0.0.1:5000"
    _tg_send(chat_id, (
        f"<b>📡 Live Scrape Monitor</b>\n"
        f'<a href="{url}/live">{url}/live</a>\n\n'
        f"<i>Watch every job being evaluated in real-time.</i>"
    ))


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update  = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = message.get("chat", {}).get("id")
    text    = (message.get("text") or "").strip()

    if not chat_id or not text:
        return "ok", 200

    cmd = text.split("@")[0].lower()
    if   cmd == "/status":          _tg_status(chat_id)
    elif cmd == "/top10":           _tg_top10(chat_id)
    elif cmd == "/live":            _tg_live(chat_id)
    elif cmd in ("/help", "/start"): _tg_help(chat_id)

    return "ok", 200


# ══════════════════════════════════════════════════════════════════════════════
# ACTIONS  —  manual trigger for scraper / scorer tasks
# ══════════════════════════════════════════════════════════════════════════════

import subprocess
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent

# Definitions: id, label, description, command args
_ACTIONS = [
    {
        "id":    "scan_all",
        "label": "Scan All Sources",
        "desc":  "Run all scrapers: IrishJobs, Indeed, ITJobs, Jobs.ie + AI careers",
        "icon":  "bi-play-circle-fill",
        "color": "success",
        "args":  ["run_all.py"],
    },
    {
        "id":    "scan_ij",
        "label": "Scan IrishJobs",
        "desc":  "Scrape IrishJobs.ie for Java, cybersecurity and IT support roles",
        "icon":  "bi-globe2",
        "color": "primary",
        "args":  ["run_all.py", "--sources", "ij"],
    },
    {
        "id":    "scan_indeed",
        "label": "Scan Indeed",
        "desc":  "Scrape Indeed Ireland",
        "icon":  "bi-globe2",
        "color": "primary",
        "args":  ["run_all.py", "--sources", "indeed"],
    },
    {
        "id":    "scan_itjobs",
        "label": "Scan ITJobs.ie",
        "desc":  "Scrape ITJobs.ie (auto-skips if site is unreachable)",
        "icon":  "bi-globe2",
        "color": "primary",
        "args":  ["run_all.py", "--sources", "itjobs"],
    },
    {
        "id":    "scan_jobsie",
        "label": "Scan Jobs.ie",
        "desc":  "Scrape Jobs.ie",
        "icon":  "bi-globe2",
        "color": "primary",
        "args":  ["run_all.py", "--sources", "jobsie"],
    },
    {
        "id":    "scan_ai",
        "label": "AI Careers Scan",
        "desc":  "Query 100 company careers pages via Jina AI + GPT-4o-mini",
        "icon":  "bi-stars",
        "color": "warning",
        "args":  ["run_all.py", "--sources", "ai"],
    },
    {
        "id":    "score",
        "label": "Score Jobs",
        "desc":  "Score all unscored active jobs (7-signal legitimacy scorer)",
        "icon":  "bi-patch-check",
        "color": "info",
        "args":  ["score_jobs.py"],
    },
    {
        "id":    "rescore",
        "label": "Rescore Everything",
        "desc":  "Force re-score every active job, even already-scored ones",
        "icon":  "bi-arrow-repeat",
        "color": "secondary",
        "args":  ["score_jobs.py", "--rescore"],
    },
]

# ── Task state (one running task at a time) ───────────────────────────────────
_task_lock   = threading.Lock()
_task_state: dict = {
    "id":        None,   # action id
    "label":     None,
    "pid":       None,
    "running":   False,
    "started_at": None,
    "log_lines": [],     # list of {"ts", "line", "level"}
    "return_code": None,
}
_task_listeners: list[queue.Queue] = []
_task_listener_lock = threading.Lock()


def _task_broadcast(payload: dict):
    """Push a JSON string to all /actions/stream clients."""
    msg = json.dumps(payload)
    with _task_listener_lock:
        for q in list(_task_listeners):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def _run_task_thread(action_id: str, label: str, cmd_args: list[str]):
    global _task_state
    py_exe = sys.executable  # same interpreter as the dashboard
    cmd = [py_exe] + cmd_args

    started = datetime.utcnow()
    with _task_lock:
        _task_state.update({
            "id": action_id, "label": label,
            "running": True, "started_at": started.isoformat(),
            "log_lines": [], "return_code": None, "pid": None,
        })

    _task_broadcast({"type": "start", "id": action_id, "label": label,
                     "ts": started.isoformat()})

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with _task_lock:
            _task_state["pid"] = proc.pid

        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip()
            if not line:
                continue
            # Classify line level for colour coding
            ll = line.lower()
            if any(x in ll for x in ("error", "fail", "exception", "traceback")):
                level = "error"
            elif any(x in ll for x in ("warn", "warning")):
                level = "warn"
            elif any(x in ll for x in ("ok", "success", "scored", "new jobs", "accepted")):
                level = "ok"
            else:
                level = "info"

            entry = {"ts": datetime.utcnow().strftime("%H:%M:%S"),
                     "line": line, "level": level}
            with _task_lock:
                _task_state["log_lines"].append(entry)
                # Keep only last 500 lines in memory
                if len(_task_state["log_lines"]) > 500:
                    _task_state["log_lines"] = _task_state["log_lines"][-500:]

            _task_broadcast({"type": "line", **entry})

        proc.wait()
        rc = proc.returncode

    except Exception as exc:
        rc = -1
        err_entry = {"ts": datetime.utcnow().strftime("%H:%M:%S"),
                     "line": f"[dashboard] Task error: {exc}", "level": "error"}
        with _task_lock:
            _task_state["log_lines"].append(err_entry)
        _task_broadcast({"type": "line", **err_entry})

    finally:
        with _task_lock:
            _task_state["running"]     = False
            _task_state["return_code"] = rc
        _task_broadcast({"type": "done", "return_code": rc,
                         "ts": datetime.utcnow().isoformat()})
        logger.info(f"[actions] task '{action_id}' finished rc={rc}")


@app.route("/actions")
def actions():
    with _task_lock:
        state = dict(_task_state)
    return render_template("actions.html", actions=_ACTIONS, state=state)


@app.route("/actions/run", methods=["POST"])
def actions_run():
    action_id = request.form.get("action_id", "").strip()
    action    = next((a for a in _ACTIONS if a["id"] == action_id), None)
    if not action:
        return jsonify({"error": "Unknown action"}), 400

    with _task_lock:
        if _task_state["running"]:
            return jsonify({"error": "A task is already running"}), 409

    t = threading.Thread(
        target=_run_task_thread,
        args=(action["id"], action["label"], action["args"]),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "id": action_id, "label": action["label"]})


@app.route("/actions/kill", methods=["POST"])
def actions_kill():
    with _task_lock:
        pid     = _task_state.get("pid")
        running = _task_state.get("running")
    if not running or not pid:
        return jsonify({"error": "No task running"}), 400
    try:
        import signal as _signal
        os.kill(pid, _signal.SIGTERM)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/actions/status")
def actions_status():
    """Returns current task state as JSON (for page load)."""
    with _task_lock:
        state = dict(_task_state)
        state["log_lines"] = list(state["log_lines"])
    return jsonify(state)


@app.route("/actions/stream")
def actions_stream():
    """SSE stream for live task output."""
    client_q: queue.Queue = queue.Queue(maxsize=400)
    with _task_listener_lock:
        _task_listeners.append(client_q)

    # Send current state immediately so newly connected clients are in sync
    with _task_lock:
        init = dict(_task_state)
        init["log_lines"] = list(init["log_lines"])
    init["type"] = "state"

    def generate():
        try:
            yield f"data: {json.dumps(init)}\n\n"
            while True:
                try:
                    payload = client_q.get(timeout=20)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    # heartbeat
                    yield f"data: {json.dumps({'type':'hb'})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _task_listener_lock:
                try:
                    _task_listeners.remove(client_q)
                except ValueError:
                    pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE  —  all-time history of every job & company seen
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/database")
def database():
    with get_session() as session:
        # All-time totals (active + inactive)
        total_ever   = session.query(func.count(Job.id)).scalar() or 0
        total_active = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
        total_inactive = total_ever - total_active
        total_companies = session.execute(
            text("SELECT COUNT(DISTINCT company) FROM jobs WHERE company IS NOT NULL")
        ).scalar() or 0
        total_sources = session.execute(
            text("SELECT COUNT(DISTINCT source) FROM jobs WHERE source IS NOT NULL")
        ).scalar() or 0

        # Per-source all-time breakdown
        source_rows = session.execute(text("""
            SELECT source,
                   COUNT(*)                                       AS total,
                   SUM(CASE WHEN is_active THEN 1 ELSE 0 END)    AS active,
                   SUM(CASE WHEN NOT is_active THEN 1 ELSE 0 END) AS inactive,
                   AVG(combined_score)::NUMERIC(5,1)              AS avg_score,
                   MIN(first_seen)                                AS first_seen,
                   MAX(first_seen)                                AS last_seen
            FROM jobs
            WHERE source IS NOT NULL
            GROUP BY source
            ORDER BY total DESC
        """)).fetchall()

        # Monthly ingestion (last 12 months)
        monthly_rows = session.execute(text("""
            SELECT TO_CHAR(DATE_TRUNC('month', first_seen), 'Mon YYYY') AS month,
                   COUNT(*) AS cnt
            FROM jobs
            WHERE first_seen >= NOW() - INTERVAL '12 months'
            GROUP BY DATE_TRUNC('month', first_seen)
            ORDER BY DATE_TRUNC('month', first_seen)
        """)).fetchall()

    return render_template(
        "database.html",
        total_ever=total_ever,
        total_active=total_active,
        total_inactive=total_inactive,
        total_companies=total_companies,
        total_sources=total_sources,
        source_rows=source_rows,
        monthly_rows=monthly_rows,
    )


@app.route("/database/jobs")
def database_jobs():
    """HTMX partial: all-time job table (active + inactive)."""
    with get_session() as session:
        q = session.query(Job)

        # Filters
        source    = request.args.get("source", "").strip()
        company   = request.args.get("company", "").strip()
        status    = request.args.get("status", "all")   # all / active / inactive
        days_back = request.args.get("days_back", type=int)
        keyword   = request.args.get("q", "").strip()

        if source:   q = q.filter(Job.source == source)
        if company:  q = q.filter(Job.company.ilike(f"%{company}%"))
        if keyword:  q = q.filter(
            (Job.title.ilike(f"%{keyword}%")) | (Job.company.ilike(f"%{keyword}%"))
        )
        if status == "active":   q = q.filter(Job.is_active == True)
        if status == "inactive": q = q.filter(Job.is_active == False)
        if days_back:
            cutoff = datetime.utcnow() - timedelta(days=days_back)
            q = q.filter(Job.first_seen >= cutoff)

        total = q.count()
        jobs_list = q.order_by(Job.first_seen.desc()).limit(300).all()

        sources = [r[0] for r in session.execute(
            text("SELECT DISTINCT source FROM jobs WHERE source IS NOT NULL ORDER BY source")
        ).fetchall()]

    return render_template(
        "partials/database_jobs.html",
        jobs=jobs_list, total=total, sources=sources,
        args=request.args,
    )


@app.route("/database/companies")
def database_companies():
    """HTMX partial: all-time company list."""
    with get_session() as session:
        sort_by = request.args.get("sort", "total")
        order_map = {
            "total":      "total DESC",
            "active":     "active DESC",
            "avg_score":  "avg_score DESC NULLS LAST",
            "first_seen": "first_seen ASC",
            "last_seen":  "last_seen DESC",
            "company":    "company ASC",
        }
        order = order_map.get(sort_by, "total DESC")
        keyword = request.args.get("q", "").strip()
        where   = f"AND company ILIKE '%{keyword}%'" if keyword else ""

        rows = session.execute(text(f"""
            SELECT
                company,
                COUNT(*)                                        AS total,
                SUM(CASE WHEN is_active THEN 1 ELSE 0 END)     AS active,
                AVG(combined_score)::NUMERIC(5,1)              AS avg_score,
                ARRAY_AGG(DISTINCT source)                     AS sources,
                MIN(first_seen)                                AS first_seen,
                MAX(first_seen)                                AS last_seen
            FROM jobs
            WHERE company IS NOT NULL {where}
            GROUP BY company
            ORDER BY {order}
            LIMIT 300
        """)).fetchall()

    return render_template(
        "partials/database_companies.html",
        rows=rows, sort_by=sort_by, args=request.args,
    )


# ── 404 ───────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def _start_ngrok(port: int) -> str:
    """Start a pyngrok tunnel and return the public https URL."""
    try:
        from pyngrok import ngrok
        tunnel     = ngrok.connect(port, "http")
        public_url = tunnel.public_url.replace("http://", "https://")
        return public_url
    except ImportError:
        print("  [!] pyngrok not installed — run: py -m pip install pyngrok")
        return ""
    except Exception as exc:
        print(f"  [!] ngrok error: {exc}")
        return ""


def main():
    global _public_url

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Job Discovery Dashboard")
    parser.add_argument("--host",   default=None,  help="Bind host (default: 127.0.0.1, or 0.0.0.0 with --public)")
    parser.add_argument("--port",   default=5000,  type=int, help="Port (default: 5000)")
    parser.add_argument("--public", action="store_true",     help="Open a public ngrok tunnel")
    args = parser.parse_args()

    port = int(os.environ.get("PORT", args.port or DASHBOARD_PORT))
    host = args.host or ("0.0.0.0" if (args.public or os.environ.get("RENDER")) else "127.0.0.1")

    lan_ip = _lan_ip()

    if args.public:
        _public_url = _start_ngrok(port)

    print("=" * 62)
    print("  🔍  Job Discovery Dashboard")
    print("─" * 62)
    print(f"  Local    →  http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"  LAN      →  http://{lan_ip}:{port}")
    if _public_url:
        print(f"  Public   →  {_public_url}")
    print(f"  Live Mon →  http://127.0.0.1:{port}/live")
    if _public_url:
        print(f"  Live (P) →  {_public_url}/live")
    print("=" * 62)

    app.run(
        debug      = os.environ.get("FLASK_ENV") != "production",
        host       = host,
        port       = port,
        use_reloader = False,     # must be False for SSE threads to work correctly
        threaded   = True,        # needed for concurrent SSE connections
    )


if __name__ == "__main__":
    main()
