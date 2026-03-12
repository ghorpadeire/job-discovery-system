"""
AI scoring pipeline CLI.

Steps (all optional via flags):
  1. Migrate AI columns into the DB (idempotent)
  2. Generate / update embeddings for unembedded jobs
  3. Find and merge near-duplicate jobs (cosine similarity >= 0.92)
  4. Score relevance (0-10) and ghost probability (0-1) via GPT-4o-mini
  5. Print a ranked summary table

Usage examples
--------------
  py ai_score_jobs.py                        # full pipeline + display all
  py ai_score_jobs.py --min-combined 60      # show only combined_score >= 60
  py ai_score_jobs.py --only-dedup           # embed + dedup only, no LLM scoring
  py ai_score_jobs.py --only-score           # LLM scoring only, skip embedding
  py ai_score_jobs.py --rescore              # re-run LLM scoring on already-scored jobs
  py ai_score_jobs.py --no-embed             # skip embedding step, still dedup + score
  py ai_score_jobs.py --no-display           # run pipeline silently, no table output
  py ai_score_jobs.py --no-score             # embed + dedup only (alias for --only-dedup)
"""

import argparse
import logging
import os
import sys

# Fix Unicode output on Windows (cp1252 terminal can't handle box-drawing chars)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_env() -> bool:
    """Verify required environment variables are present."""
    db_url = os.getenv("DATABASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")

    ok = True
    if not db_url:
        logger.error("DATABASE_URL not set in .env")
        ok = False
    if not api_key:
        logger.error(
            "OPENAI_API_KEY not set in .env\n"
            "  Add it:  OPENAI_API_KEY=sk-..."
        )
        ok = False
    return ok


def _build_engine():
    db_url = os.getenv("DATABASE_URL")
    return create_engine(db_url, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_GHOST_LABELS = {
    (0.0, 0.30): ("REAL",    "\033[32m"),   # green
    (0.30, 0.60): ("MAYBE",   "\033[33m"),   # yellow
    (0.60, 1.01): ("GHOST?",  "\033[31m"),   # red
}

_RESET = "\033[0m"


def _ghost_label(prob: float) -> tuple[str, str]:
    for (lo, hi), (label, colour) in _GHOST_LABELS.items():
        if lo <= prob < hi:
            return label, colour
    return "GHOST?", "\033[31m"


def _bar(score: float, width: int = 20) -> str:
    """ASCII progress bar for a 0-100 combined score."""
    filled = int(round(score / 100 * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def display_results(engine, min_combined: float = 0.0) -> None:
    """Print a ranked table of AI-scored jobs."""
    from core.models import Job

    with Session(engine) as session:
        jobs = (
            session.query(Job)
            .filter(Job.is_active == True)
            .filter(Job.combined_score != None)
            .order_by(Job.combined_score.desc())
            .all()
        )

    if not jobs:
        print("\n  No AI-scored jobs found.\n")
        return

    shown = [j for j in jobs if (j.combined_score or 0) >= min_combined]

    print(f"\n{'─'*90}")
    print(
        f"  {'#':>3}  {'Score':>5}  {'Bar':<22}  {'Rel':>3}  {'Ghost':<7}  "
        f"{'Title':<35}  Company"
    )
    print(f"{'─'*90}")

    for rank, job in enumerate(shown, 1):
        cs   = job.combined_score or 0.0
        rel  = job.relevance_score or 0.0
        gp   = job.ghost_probability if job.ghost_probability is not None else 0.5
        label, colour = _ghost_label(gp)

        title   = (job.title   or "")[:34]
        company = (job.company or "")[:28]
        bar     = _bar(cs)

        print(
            f"  {rank:>3}  {cs:>5.1f}  {bar}  {rel:>3.1f}  "
            f"{colour}{label:<7}{_RESET}  {title:<35}  {company}"
        )

        # Show AI reasoning if present
        reasoning = job.ai_reasoning or {}
        if reasoning.get("relevance"):
            print(f"       \033[90m  Fit:   {reasoning['relevance'][:75]}{_RESET}")
        if reasoning.get("ghost"):
            print(f"       \033[90m  Ghost: {reasoning['ghost'][:75]}{_RESET}")

    print(f"{'─'*90}")
    total = len(jobs)
    print(
        f"  Showing {len(shown)} of {total} scored job(s)"
        + (f"  (min combined score: {min_combined})" if min_combined > 0 else "")
    )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Run the AI scoring pipeline and display results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--min-combined", type=float, default=0.0, metavar="N",
        help="Only display jobs with combined_score >= N (default: 0)",
    )
    p.add_argument(
        "--rescore", action="store_true",
        help="Re-run LLM scoring even on already-scored jobs",
    )
    p.add_argument(
        "--no-embed", action="store_true",
        help="Skip embedding generation (still runs dedup and scoring)",
    )
    p.add_argument(
        "--only-dedup", "--no-score", action="store_true",
        help="Run embedding + dedup only; skip LLM relevance/ghost scoring",
    )
    p.add_argument(
        "--only-score", action="store_true",
        help="Run LLM scoring only; skip embedding and dedup steps",
    )
    p.add_argument(
        "--no-display", action="store_true",
        help="Suppress the results table (useful for scripting)",
    )
    p.add_argument(
        "--no-fetch", action="store_true",
        help="Do not fetch job descriptions from apply URLs",
    )
    p.add_argument(
        "--rescore-embeddings", action="store_true",
        help="Regenerate embeddings even for jobs that already have one",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show DEBUG-level log output",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ---- Environment checks ----
    if not _check_env():
        return 1

    engine = _build_engine()

    # ---- Step 0: Migrate DB columns (idempotent) ----
    logger.info("Ensuring AI columns exist in DB…")
    from core.models import migrate_ai_columns
    try:
        migrate_ai_columns(engine)
        logger.info("  DB columns OK.")
    except Exception as exc:
        logger.error(f"  Migration failed: {exc}")
        return 1

    # ---- Step 1 & 2: Embeddings + Dedup ----
    run_embed  = not args.only_score
    run_dedup  = not args.only_score
    run_score  = not args.only_dedup

    if run_embed and not args.no_embed:
        logger.info("─── Embedding pipeline ───")
        from core.ai_deduplicator import run_embedding_pipeline
        try:
            result = run_embedding_pipeline(
                engine,
                rescore_embeddings=args.rescore_embeddings,
            )
            logger.info(
                f"  Embeddings generated: {result['embedded']}  "
                f"Duplicates merged: {result['merged']}"
            )
        except Exception as exc:
            logger.error(f"  Embedding pipeline failed: {exc}")
            # Non-fatal — continue to scoring
    elif run_dedup and args.no_embed:
        # Dedup only (no new embeddings)
        logger.info("─── Dedup only (skipping embedding generation) ───")
        from core.ai_deduplicator import find_and_merge_embedding_duplicates
        try:
            merged = find_and_merge_embedding_duplicates(engine)
            logger.info(f"  Duplicates merged: {merged}")
        except Exception as exc:
            logger.error(f"  Dedup failed: {exc}")

    # ---- Step 3: Relevance + Ghost scoring ----
    if run_score:
        logger.info("─── AI scoring (relevance + ghost) ───")
        from core.ai_scorer import ai_score_all_active_jobs
        try:
            scored = ai_score_all_active_jobs(
                engine,
                rescore=args.rescore,
                fetch_descriptions=not args.no_fetch,
            )
            logger.info(f"  Jobs scored: {scored}")
        except Exception as exc:
            logger.error(f"  AI scoring failed: {exc}")
            return 1

    # ---- Step 4: Display ----
    if not args.no_display:
        display_results(engine, min_combined=args.min_combined)

    return 0


if __name__ == "__main__":
    sys.exit(main())
