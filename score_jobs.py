"""
Legitimacy scorer CLI.

Usage
-----
    python score_jobs.py                        # score all unscored active jobs
    python score_jobs.py --rescore              # re-score even already-scored jobs
    python score_jobs.py --min-score 70         # show only jobs scoring ≥ 70
    python score_jobs.py --show-ghosts          # show only suspected ghost jobs
    python score_jobs.py --no-career-check      # skip the slow career-page signal
    python score_jobs.py --min-score 50 --rescore
"""
import argparse
import logging
import sys

# Fix Unicode output on Windows (cp1252 terminal can't handle box-drawing chars)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy.orm import Session

from core.logging_config import setup_logging
setup_logging()

from core.database import check_connection, get_engine
from core.models import Job, migrate_scoring_columns
from core.scorer import GHOST_THRESHOLD, score_all_active_jobs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _bar(score: int, width: int = 20) -> str:
    """Simple ASCII progress bar for the score."""
    filled = round(score / 100 * width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def print_scored_table(engine, min_score: int = 0, ghosts_only: bool = False) -> None:
    with Session(engine) as session:
        q = (
            session.query(Job)
            .filter(Job.is_active)
            .filter(Job.legitimacy_score is not None)
        )
        if min_score > 0:
            q = q.filter(Job.legitimacy_score >= min_score)
        if ghosts_only:
            q = q.filter(Job.suspected_ghost)
        jobs = q.order_by(Job.legitimacy_score.desc()).all()

    if not jobs:
        print("  (no matching scored jobs)")
        return

    W = 110
    header = f"{'SCORE':>5}  {'BAR':<22}  {'GHOST':5}  {'TITLE':<34}  {'COMPANY':<22}  SIGNALS"
    print(f"\n{'─' * W}")
    print(header)
    print(f"{'─' * W}")

    for j in jobs:
        score   = j.legitimacy_score or 0
        ghost   = "🚩 YES" if j.suspected_ghost else "  no"
        title   = (j.title[:32]   + "..") if len(j.title)   > 34 else j.title
        company = (j.company[:20] + "..") if len(j.company) > 22 else j.company

        # Compact breakdown: show signal initials + pts for non-zero signals
        bd = j.score_breakdown or {}
        signal_abbrev = {
            "career_page_match": "CP",
            "recently_posted":   "RP",
            "company_volume":    "CV",
            "not_a_repost":      "NR",
            "url_resolves":      "UR",
            "has_salary":        "SA",
            "rich_description":  "RD",
        }
        sig_str = " ".join(
            f"{abbr}:{bd.get(full, 0)}"
            for full, abbr in signal_abbrev.items()
            if bd.get(full, 0) > 0
        )

        print(
            f"{score:>5}  {_bar(score):<22}  {ghost:<5}  "
            f"{title:<34}  {company:<22}  {sig_str}"
        )

    print(f"{'─' * W}")
    ghost_count = sum(1 for j in jobs if j.suspected_ghost)
    print(
        f"  Showing {len(jobs)} job(s)"
        f"  |  {ghost_count} suspected ghost(s)"
        f"  |  min-score filter: {min_score}"
    )


def print_score_legend() -> None:
    print("\nSignal legend:")
    legends = [
        ("CP", "career_page_match", 25, "Title found on company's own careers site"),
        ("RP", "recently_posted",   20, "Posted within last 14 days"),
        ("CV", "company_volume",    15, "Company has 3+ active roles in DB"),
        ("NR", "not_a_repost",      15, "Job first seen within last 30 days"),
        ("UR", "url_resolves",      10, "Apply URL returns HTTP 2xx"),
        ("SA", "has_salary",        10, "Salary field is populated"),
        ("RD", "rich_description",   5, "Description > 200 words"),
    ]
    for abbr, _, pts, desc in legends:
        print(f"  {abbr}  ({pts:2d} pts)  {desc}")
    print(f"\n  Ghost threshold: score < {GHOST_THRESHOLD}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score job listings for legitimacy and print a ranked table."
    )
    parser.add_argument(
        "--min-score", type=int, default=0, metavar="N",
        help="Only display jobs with legitimacy_score >= N  (default: 0 = show all)",
    )
    parser.add_argument(
        "--show-ghosts", action="store_true",
        help="Show only suspected ghost jobs (score < 30)",
    )
    parser.add_argument(
        "--rescore", action="store_true",
        help="Re-score even jobs that already have a score",
    )
    parser.add_argument(
        "--no-career-check", dest="career_check", action="store_false", default=True,
        help="Skip career-page HTTP check (faster, but Signal 1 always = 0)",
    )
    parser.add_argument(
        "--no-score", action="store_true",
        help="Skip scoring pass; just display already-scored jobs",
    )
    parser.add_argument(
        "--legend", action="store_true",
        help="Print signal legend and exit",
    )
    args = parser.parse_args()

    if args.legend:
        print_score_legend()
        return

    if not check_connection():
        logger.error(
            "Cannot connect to PostgreSQL. "
            "Is Docker running? Try: docker compose up -d"
        )
        sys.exit(1)

    engine = get_engine()

    # Ensure scoring columns exist (safe no-op if already present)
    migrate_scoring_columns(engine)

    if not args.no_score:
        logger.info("Starting legitimacy scoring run…")
        count = score_all_active_jobs(
            engine,
            check_career_page=args.career_check,
            rescore=args.rescore,
        )
        logger.info(f"Scored {count} job(s).")

    print_score_legend()
    print_scored_table(engine, min_score=args.min_score, ghosts_only=args.show_ghosts)


if __name__ == "__main__":
    main()
