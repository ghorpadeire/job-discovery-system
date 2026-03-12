# -*- coding: utf-8 -*-
"""
dashboard.py — Flask web dashboard for the Job Discovery System.

Pages:
  /               Home: live stats overview
  /jobs           Filterable + sortable jobs table (HTMX live filtering)
  /job/<id>       Full job detail, score breakdown, AI analysis
  /apply-tracker  Kanban board (Saved → Applied → Interview → Offer → Rejected)
  /companies      Company credibility overview

Run:
  py dashboard.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, text

load_dotenv()

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.secret_key = os.urandom(24)

# ── DB imports (lazy to avoid import-order issues) ────────────────────────────
from core.database import get_engine, get_session
from core.models import ApplicationTracker, Job, TRACKER_STATUSES, migrate_tracker_table

# Ensure tracker table exists at import time (safe: checkfirst=True).
# Runs under both `py dashboard.py` and `gunicorn dashboard:app`.
migrate_tracker_table(get_engine())

# ── Helpers ───────────────────────────────────────────────────────────────────

def _score_class(score):
    """Bootstrap colour class based on combined/legitimacy score (0-100)."""
    if score is None:
        return "secondary"
    if score >= 70:
        return "success"
    if score >= 40:
        return "warning"
    return "danger"


def _score_pct(score, max_val=100):
    """Clamp score to 0-100 for progress bars."""
    if score is None:
        return 0
    return max(0, min(100, round(score / max_val * 100)))


def _relative_date(dt):
    """Human-friendly relative timestamp."""
    if dt is None:
        return "—"
    now = datetime.utcnow()
    diff = now - dt
    if diff.total_seconds() < 3600:
        return "just now"
    if diff.days == 0:
        return f"{int(diff.total_seconds() // 3600)}h ago"
    if diff.days == 1:
        return "yesterday"
    return f"{diff.days}d ago"


app.jinja_env.filters["score_class"] = _score_class
app.jinja_env.filters["score_pct"] = _score_pct
app.jinja_env.filters["relative_date"] = _relative_date


# ── Route helpers ──────────────────────────────────────────────────────────────

def _apply_job_filters(query, args):
    """Apply URL query params as SQLAlchemy filters to a Job query."""
    min_score = args.get("min_score", type=float)
    max_score = args.get("max_score", type=float)
    company   = args.get("company", "").strip()
    source    = args.get("source", "").strip()
    days_back = args.get("days_back", type=int)
    show_ghost = args.get("show_ghost", "")          # "1" = only ghosts
    scored_only = args.get("scored_only", "")        # "1" = only scored
    sort_by   = args.get("sort_by", "combined_score")

    if min_score is not None:
        query = query.filter(Job.combined_score >= min_score)
    if max_score is not None:
        query = query.filter(Job.combined_score <= max_score)
    if company:
        query = query.filter(Job.company.ilike(f"%{company}%"))
    if source:
        query = query.filter(Job.source == source)
    if days_back:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        query = query.filter(Job.first_seen >= cutoff)
    if show_ghost == "1":
        query = query.filter(Job.suspected_ghost == True)
    if scored_only == "1":
        query = query.filter(Job.combined_score.isnot(None))

    # Sorting
    sort_map = {
        "combined_score":   Job.combined_score.desc().nullslast(),
        "legitimacy_score": Job.legitimacy_score.desc().nullslast(),
        "relevance_score":  Job.relevance_score.desc().nullslast(),
        "first_seen":       Job.first_seen.desc(),
        "company":          Job.company.asc(),
    }
    order_col = sort_map.get(sort_by, Job.combined_score.desc().nullslast())
    query = query.order_by(order_col)
    return query


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

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

        # Top 5 recent high-score jobs
        top_jobs = (
            session.query(Job)
            .filter(Job.is_active == True, Job.combined_score.isnot(None))
            .order_by(Job.combined_score.desc())
            .limit(8)
            .all()
        )

        # Scores by source
        source_stats = session.execute(text("""
            SELECT source,
                   COUNT(*)                           AS cnt,
                   AVG(combined_score)                AS avg_combined
            FROM jobs
            WHERE is_active = TRUE AND combined_score IS NOT NULL
            GROUP BY source
            ORDER BY cnt DESC
        """)).fetchall()

    return render_template(
        "index.html",
        total_active=total_active,
        avg_legit=avg_legit,
        avg_combined=avg_combined,
        added_today=added_today,
        ghost_count=ghost_count,
        high_conf=high_conf,
        scored_count=scored_count,
        in_tracker=in_tracker,
        top_jobs=top_jobs,
        source_stats=source_stats,
    )


# ── /jobs ─────────────────────────────────────────────────────────────────────
@app.route("/jobs")
def jobs():
    """Full jobs page — initial load."""
    # Distinct sources for filter dropdown
    with get_session() as session:
        sources = [r[0] for r in session.execute(
            text("SELECT DISTINCT source FROM jobs WHERE source IS NOT NULL ORDER BY source")
        ).fetchall()]
        total = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0

    return render_template("jobs.html", sources=sources, total=total, args=request.args)


@app.route("/jobs/rows")
def jobs_rows():
    """HTMX partial — returns only the <tbody> rows."""
    with get_session() as session:
        q = session.query(Job).filter(Job.is_active == True)
        q = _apply_job_filters(q, request.args)
        jobs_list = q.limit(200).all()
        count = q.count()

    return render_template("partials/jobs_rows.html", jobs=jobs_list, count=count)


# ── /job/<id> ─────────────────────────────────────────────────────────────────
@app.route("/job/<int:job_id>")
def job_detail(job_id):
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return render_template("404.html"), 404

        tracker = session.query(ApplicationTracker).filter_by(job_id=job_id).first()

    # Parse score breakdown for display
    breakdown = {}
    signal_max = {
        "career_page_match": 25,
        "recently_posted":   20,
        "company_volume":    15,
        "not_a_repost":      15,
        "url_resolves":      10,
        "has_salary":        10,
        "rich_description":   5,
    }
    if job.score_breakdown:
        for signal, pts in job.score_breakdown.items():
            breakdown[signal] = {
                "pts":     pts,
                "max":     signal_max.get(signal, 10),
                "pct":     _score_pct(pts, signal_max.get(signal, 10)),
                "label":   signal.replace("_", " ").title(),
                "earned":  pts > 0,
            }

    return render_template(
        "job_detail.html",
        job=job,
        tracker=tracker,
        breakdown=breakdown,
        statuses=TRACKER_STATUSES,
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

    # Group by status
    columns = {s: [] for s in TRACKER_STATUSES}
    for entry, job in rows:
        if entry.status in columns:
            columns[entry.status].append({"entry": entry, "job": job})

    return render_template("apply_tracker.html", columns=columns, statuses=TRACKER_STATUSES)


@app.route("/apply-tracker/add/<int:job_id>", methods=["POST"])
def tracker_add(job_id):
    """Add a job to the tracker (defaults to 'saved')."""
    with get_session() as session:
        existing = session.query(ApplicationTracker).filter_by(job_id=job_id).first()
        if not existing:
            entry = ApplicationTracker(job_id=job_id, status="saved")
            session.add(entry)
            session.commit()

    # HTMX: if request wants partial, redirect to tracker; else go back to referrer
    if request.headers.get("HX-Request"):
        return redirect(url_for("apply_tracker")), 303
    return redirect(request.referrer or url_for("apply_tracker"))


@app.route("/apply-tracker/move/<int:entry_id>/<string:new_status>", methods=["POST"])
def tracker_move(entry_id, new_status):
    """Move a tracker entry to a new status."""
    if new_status not in TRACKER_STATUSES:
        return "Invalid status", 400

    with get_session() as session:
        entry = session.get(ApplicationTracker, entry_id)
        if entry:
            entry.status = new_status
            entry.updated_at = datetime.utcnow()
            if new_status == "applied" and entry.applied_at is None:
                entry.applied_at = datetime.utcnow()
            session.commit()

    if request.headers.get("HX-Request"):
        return redirect(url_for("apply_tracker")), 303
    return redirect(url_for("apply_tracker"))


@app.route("/apply-tracker/remove/<int:entry_id>", methods=["POST"])
def tracker_remove(entry_id):
    """Remove a job from the tracker."""
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
    """Save notes for a tracker entry."""
    notes = request.form.get("notes", "").strip()
    with get_session() as session:
        entry = session.get(ApplicationTracker, entry_id)
        if entry:
            entry.notes = notes or None
            entry.updated_at = datetime.utcnow()
            session.commit()
    return "", 204   # No-content — HTMX hides the save indicator


# ── /companies ────────────────────────────────────────────────────────────────
@app.route("/companies")
def companies():
    sort_by = request.args.get("sort", "open_roles")
    sort_map = {
        "open_roles":    "cnt DESC",
        "avg_score":     "avg_combined DESC NULLS LAST",
        "avg_legit":     "avg_legit DESC NULLS LAST",
        "ghost_rate":    "ghost_rate DESC NULLS LAST",
        "company":       "company ASC",
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


# ── 404 ───────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    port = int(os.environ.get("PORT", 5000))
    host = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"

    print("=" * 60)
    print(f"  Job Discovery Dashboard  →  http://{host}:{port}")
    print("=" * 60)
    app.run(debug=os.environ.get("FLASK_ENV") != "production",
            host=host, port=port, use_reloader=False)
