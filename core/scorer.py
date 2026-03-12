"""
Legitimacy scoring engine — scores each job 0-100 across 7 signals.

Signal table
------------
#  Name                 Max pts  Description
1  career_page_match     25      Job title appears on the company's own careers site
2  recently_posted       20      date_posted within last 14 days
3  company_volume        15      Company NOT known to have <3 active DB roles (benefit of doubt)
4  not_a_repost          15      Job first_seen within last 30 days (not recycled listing)
5  url_resolves          10      The apply URL returns HTTP 2xx
6  has_salary            10      Salary field is populated
7  rich_description       5      Description length > 200 words (fetched from URL)
                         ---
                         100

Jobs scoring < 30 are flagged as suspected_ghost = True.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dateutil import parser as dateutil_parser
from sqlalchemy.orm import Session

from core.career_checker import title_matches_career_page
from core.models import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GHOST_THRESHOLD   = 30   # suspected_ghost = True below this score
RECENT_DAYS       = 14   # Signal 2: posted within N days
REPOST_DAYS       = 30   # Signal 4: first_seen within N days
MIN_VOLUME        = 3    # Signal 3: company needs at least this many active jobs
MIN_DESC_WORDS    = 200  # Signal 7: minimum words in description

SIGNAL_WEIGHTS = {
    "career_page_match": 25,
    "recently_posted":   20,
    "company_volume":    15,
    "not_a_repost":      15,
    "url_resolves":      10,
    "has_salary":        10,
    "rich_description":   5,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    """
    Parse a raw date string from a job posting.  Handles:
      - ISO dates:        "2025-05-01"
      - UK/EU dates:      "01/05/2025"
      - Relative strings: "2 days ago", "Posted today", "30+ days ago"
    Returns a timezone-aware UTC datetime, or None on failure.
    """
    if not raw:
        return None
    raw = raw.strip()
    now = datetime.now(timezone.utc)

    # Relative patterns
    m = re.match(r"(\d+)\+?\s+days?\s+ago", raw, re.IGNORECASE)
    if m:
        return now - timedelta(days=int(m.group(1)))

    for pat in (r"today", r"just\s+posted", r"less\s+than\s+a\s+day"):
        if re.search(pat, raw, re.IGNORECASE):
            return now

    m = re.match(r"(\d+)\s+hours?\s+ago", raw, re.IGNORECASE)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    m = re.match(r"(\d+)\s+weeks?\s+ago", raw, re.IGNORECASE)
    if m:
        return now - timedelta(weeks=int(m.group(1)))

    m = re.match(r"(\d+)\s+months?\s+ago", raw, re.IGNORECASE)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)

    # Absolute date — let dateutil handle all formats
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_description(url: str, timeout: int = 10) -> str:
    """
    Fetch a job description page and return its visible text (~best effort).
    Returns an empty string on any failure.
    """
    if not url:
        return ""
    try:
        r = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"},
        )
        if r.status_code >= 400:
            return ""
        # Strip tags, collapse whitespace
        text = re.sub(r"<[^>]+>", " ", r.text)
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return ""


def _url_resolves(url: str, timeout: int = 8) -> bool:
    """HEAD request — return True on 2xx/3xx response."""
    if not url:
        return False
    try:
        r = httpx.head(url, follow_redirects=True, timeout=timeout,
                       headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"})
        return r.status_code < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Company volume cache (per scoring run, avoids repeated DB queries)
# ---------------------------------------------------------------------------

class _VolumeCache:
    def __init__(self, session: Session):
        self._session = session
        self._cache: dict[str, int] = {}

    def count(self, company: str) -> int:
        key = company.lower().strip()
        if key not in self._cache:
            self._cache[key] = (
                self._session.query(Job)
                .filter(Job.is_active == True)
                .filter(Job.company.ilike(f"%{key}%"))
                .count()
            )
        return self._cache[key]


# ---------------------------------------------------------------------------
# Core scorer
# ---------------------------------------------------------------------------

class JobScorer:
    """
    Score a single Job row.

    Usage
    -----
        scorer = JobScorer(session, check_career_page=True)
        score, breakdown = scorer.score(job)
    """

    def __init__(self, session: Session, check_career_page: bool = True):
        self._session       = session
        self._vol_cache     = _VolumeCache(session)
        self._check_career  = check_career_page

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def score(self, job: Job) -> tuple[int, dict[str, int]]:
        """
        Calculate and return (total_score, breakdown_dict).
        Does NOT write to the DB — callers decide when to commit.
        """
        breakdown: dict[str, int] = {}
        now = datetime.now(timezone.utc)

        # Signal 1 — career page match (25 pts)
        if self._check_career:
            try:
                hit = title_matches_career_page(job.title, job.company)
                breakdown["career_page_match"] = SIGNAL_WEIGHTS["career_page_match"] if hit else 0
            except Exception as exc:
                logger.debug(f"  signal 1 error: {exc}")
                breakdown["career_page_match"] = 0
        else:
            breakdown["career_page_match"] = 0

        # Signal 2 — recently posted (20 pts)
        posted_dt = _parse_date(job.date_posted)
        if posted_dt:
            age_days = (now - posted_dt).days
            breakdown["recently_posted"] = SIGNAL_WEIGHTS["recently_posted"] if age_days <= RECENT_DAYS else 0
        else:
            breakdown["recently_posted"] = 0

        # Signal 3 — company volume (15 pts)
        # Benefit of the doubt: award points unless we can positively confirm low volume
        # (i.e. company IS in DB with fewer than MIN_VOLUME roles — not just unknown).
        vol = self._vol_cache.count(job.company)
        breakdown["company_volume"] = SIGNAL_WEIGHTS["company_volume"] if vol == 0 or vol >= MIN_VOLUME else 0

        # Signal 4 — not a repost (15 pts)
        first = job.first_seen
        if first:
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            age_days = (now - first).days
            breakdown["not_a_repost"] = SIGNAL_WEIGHTS["not_a_repost"] if age_days <= REPOST_DAYS else 0
        else:
            breakdown["not_a_repost"] = SIGNAL_WEIGHTS["not_a_repost"]  # benefit of doubt

        # Signal 5 — URL resolves (10 pts)
        breakdown["url_resolves"] = SIGNAL_WEIGHTS["url_resolves"] if _url_resolves(job.url) else 0

        # Signal 6 — has salary (10 pts)
        breakdown["has_salary"] = SIGNAL_WEIGHTS["has_salary"] if job.salary else 0

        # Signal 7 — rich description (5 pts)
        desc = _fetch_description(job.url)
        word_count = len(desc.split())
        breakdown["rich_description"] = SIGNAL_WEIGHTS["rich_description"] if word_count >= MIN_DESC_WORDS else 0

        total = sum(breakdown.values())
        logger.debug(
            f"  {job.title!r} @ {job.company!r}: {total}/100  {breakdown}"
        )
        return total, breakdown


# ---------------------------------------------------------------------------
# Batch scorer
# ---------------------------------------------------------------------------

def score_all_active_jobs(
    engine,
    check_career_page: bool = True,
    rescore: bool = False,
) -> int:
    """
    Score every active, unscored job (or all active jobs if rescore=True).
    Writes legitimacy_score, score_breakdown, suspected_ghost to DB.
    Returns the number of jobs scored.
    """
    scored = 0
    with Session(engine) as session:
        q = session.query(Job).filter(Job.is_active == True)
        if not rescore:
            q = q.filter(Job.legitimacy_score == None)
        jobs = q.all()

        if not jobs:
            logger.info("No jobs to score.")
            return 0

        scorer = JobScorer(session, check_career_page=check_career_page)

        for i, job in enumerate(jobs, 1):
            logger.info(f"  [{i}/{len(jobs)}] scoring: {job.title!r} @ {job.company!r}")
            try:
                total, breakdown = scorer.score(job)
            except Exception as exc:
                logger.warning(f"  scoring error for job {job.id}: {exc}")
                continue

            job.legitimacy_score = total
            job.score_breakdown  = breakdown
            job.suspected_ghost  = (total < GHOST_THRESHOLD)
            scored += 1

            # Commit in small batches to avoid large transactions
            if scored % 20 == 0:
                session.commit()
                logger.info(f"  committed {scored} so far…")

        session.commit()

    return scored
