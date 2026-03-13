"""
score_jobs.py — Scoring CLI and report generator.

Usage:
  py score_jobs.py                     # score unscored jobs, show top results
  py score_jobs.py --rescore           # re-score all active jobs
  py score_jobs.py --min-score 70      # show only jobs scoring ≥ 70
  py score_jobs.py --show-ghosts       # show only suspected ghost jobs
  py score_jobs.py --rescore --min-score 50 --no-career-check  # fast rescore
"""
import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("score_jobs")

# Signal icons for the report
_SIGNAL_ICONS: dict[str, tuple[str, str]] = {
    # signal_name: (hit_icon, miss_icon)
    "career_page_match": ("✓ career_page", "✗ career_page"),
    "recently_posted":   ("✓ recent",      "✗ old_posting"),
    "company_volume":    ("✓ vol≥3",        "✗ low_vol"),
    "not_a_repost":      ("✓ fresh",        "✗ repost"),
    "url_resolves":      ("✓ url_ok",       "✗ url_dead"),
    "has_salary":        ("✓ salary",       "✗ no_salary"),
    "rich_description":  ("✓ rich_desc",    "✗ thin_desc"),
}


def _fmt_signals(breakdown: dict | None) -> str:
    if not breakdown:
        return "not scored"
    parts = []
    for signal, (hit, miss) in _SIGNAL_ICONS.items():
        pts = breakdown.get(signal, 0)
        parts.append(hit if pts > 0 else miss)
    return "  ".join(parts)


def _score_badge(score: int) -> str:
    if score >= 85:
        return f"[●●● {score:3d}]"
    if score >= 70:
        return f"[●●○ {score:3d}]"
    if score >= 50:
        return f"[●○○ {score:3d}]"
    return f"[○○○ {score:3d}]"


def main() -> int:
    parser = argparse.ArgumentParser(description="Job legitimacy scorer and reporter")
    parser.add_argument("--min-score",       type=int, default=0,
                        help="Show only jobs with score >= N (default: 0 = all)")
    parser.add_argument("--show-ghosts",     action="store_true",
                        help="Show only suspected ghost jobs")
    parser.add_argument("--rescore",         action="store_true",
                        help="Re-score all active jobs even if already scored")
    parser.add_argument("--no-career-check", action="store_true",
                        help="Skip career page check (faster but less accurate)")
    parser.add_argument("--limit",           type=int, default=50,
                        help="Max rows to display in report (default: 50)")
    args = parser.parse_args()

    # ── DB connection ────────────────────────────────────────────────
    from core.database import check_connection, get_engine
    if not check_connection():
        logger.error("Cannot connect to database.")
        return 1
    engine = get_engine()

    # ── Score jobs ───────────────────────────────────────────────────
    from core.scorer import score_all_active_jobs, GHOST_THRESHOLD
    logger.info("Starting scoring pass (rescore=%s)...", args.rescore)
    scored = score_all_active_jobs(
        engine,
        check_career_page=not args.no_career_check,
        rescore=args.rescore,
    )
    logger.info("Scored %d jobs", scored)

    # ── Query results ────────────────────────────────────────────────
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import func
    from core.models import Job

    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        query = session.query(Job).filter(
            Job.is_active == True,
            Job.legitimacy_score != None,
        )

        if args.show_ghosts:
            query = query.filter(Job.suspected_ghost == True)
        elif args.min_score > 0:
            query = query.filter(Job.legitimacy_score >= args.min_score)

        jobs = (
            query.order_by(Job.legitimacy_score.desc())
            .limit(args.limit)
            .all()
        )

        # ── Report ───────────────────────────────────────────────────
        print()
        print("=" * 100)
        title = "GHOST JOB REPORT" if args.show_ghosts else f"TOP JOBS (score ≥ {args.min_score})"
        print(f"  {title}  —  {len(jobs)} results shown")
        print("=" * 100)
        print(f"{'#':>3}  {'SCORE':>7}  {'TITLE':<40}  {'COMPANY':<30}  SIGNALS")
        print("-" * 100)

        for rank, job in enumerate(jobs, 1):
            score = job.legitimacy_score or 0
            title_trunc = (job.title or "")[:38]
            company_trunc = (job.company or "")[:28]
            badge = _score_badge(score)
            signals = _fmt_signals(job.score_breakdown)

            print(f"{rank:>3}  {badge}  {title_trunc:<40}  {company_trunc:<30}")
            print(f"     {'':>7}  {signals}")
            if job.suspected_ghost:
                print(f"     {'':>7}  ⚠  SUSPECTED GHOST")
            print()

        # ── Summary ──────────────────────────────────────────────────
        print("=" * 100)
        total    = session.query(func.count(Job.id)).filter(Job.is_active == True).scalar() or 0
        unscored = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.legitimacy_score == None
        ).scalar() or 0
        avg_s    = session.query(func.avg(Job.legitimacy_score)).filter(
            Job.is_active == True, Job.legitimacy_score != None
        ).scalar()
        ghosts   = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.suspected_ghost == True
        ).scalar() or 0
        high     = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.legitimacy_score >= 70
        ).scalar() or 0
        v_high   = session.query(func.count(Job.id)).filter(
            Job.is_active == True, Job.legitimacy_score >= 85
        ).scalar() or 0

        print(f"  Active jobs:          {total}")
        print(f"  Unscored:             {unscored}")
        print(f"  Average score:        {f'{avg_s:.1f}' if avg_s else 'N/A'}")
        print(f"  Very high (≥85):      {v_high}")
        print(f"  High (≥70):           {high}")
        print(f"  Suspected ghosts:     {ghosts} ({ghosts/max(total,1)*100:.0f}%)")
        print("=" * 100)
        print()

        return 0

    except Exception as exc:
        logger.error("Report failed: %s", exc)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
