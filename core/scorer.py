"""
7-Signal Legitimacy Scorer.

Score range: 0–100
Ghost threshold: < 30

Signals:
  1. career_page_match   25 pts  — title found on company's own careers page
  2. recently_posted     20 pts  — posted within last 14 days
  3. company_volume      15 pts  — company has ≥3 active jobs (benefit of doubt if 0)
  4. not_a_repost        15 pts  — first seen ≤30 days ago (benefit of doubt if unknown)
  5. url_resolves        10 pts  — URL returns HTTP < 400
  6. has_salary          10 pts  — salary field is non-empty
  7. rich_description     5 pts  — page word count ≥ 200

Design principle: penalise only when there is *confirmed* evidence of low quality.
If data is absent/unknown → benefit of the doubt.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from dateutil import parser as dateutil_parser
from sqlalchemy.orm import Session

from core.models import Job

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────
GHOST_THRESHOLD = 30
RECENT_DAYS     = 14
REPOST_DAYS     = 30
MIN_VOLUME      = 3
MIN_DESC_WORDS  = 200

SIGNAL_WEIGHTS: dict[str, int] = {
    "career_page_match": 25,
    "recently_posted":   20,
    "company_volume":    15,
    "not_a_repost":      15,
    "url_resolves":      10,
    "has_salary":        10,
    "rich_description":   5,
}

_HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(10.0),
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"},
    follow_redirects=True,
)

# ──────────────────────────────────────────────────────────
#  Volume cache (per-run, avoids repeated DB queries)
# ──────────────────────────────────────────────────────────
class _VolumeCache:
    def __init__(self):
        self._cache: dict[str, int] = {}

    def get(self, session: Session, company: str) -> int:
        key = company.lower().strip()
        if key not in self._cache:
            count = (
                session.query(Job)
                .filter(Job.company.ilike(f"%{company}%"), Job.is_active)
                .count()
            )
            self._cache[key] = count
        return self._cache[key]

    def clear(self):
        self._cache.clear()


# ──────────────────────────────────────────────────────────
#  Date parsing
# ──────────────────────────────────────────────────────────

def _parse_date(raw: str) -> Optional[datetime]:
    """
    Parse human-readable and ISO date strings into UTC datetime.

    Handles:
      "today", "just posted", "X hours ago", "X days ago",
      "X weeks ago", "X months ago", ISO 8601, DD/MM/YYYY
    """
    if not raw:
        return None
    now = datetime.now(timezone.utc)
    raw_lower = raw.lower().strip()

    try:
        # Relative time expressions
        if raw_lower in ("today", "just posted", "0 days ago"):
            return now

        m = re.search(r"(\d+)\s+(hour|day|week|month)s?\s+ago", raw_lower)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            from datetime import timedelta
            deltas = {
                "hour":  timedelta(hours=n),
                "day":   timedelta(days=n),
                "week":  timedelta(weeks=n),
                "month": timedelta(days=n * 30),
            }
            return now - deltas[unit]

        # DD/MM/YYYY (European format)
        m2 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m2:
            d, mo, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
            return datetime(y, mo, d, tzinfo=timezone.utc)

        # Fallback: dateutil
        return dateutil_parser.parse(raw, dayfirst=True).replace(tzinfo=timezone.utc)

    except Exception as exc:
        logger.debug("_parse_date could not parse %r: %s", raw, exc)
        return None


# ──────────────────────────────────────────────────────────
#  HTTP helpers
# ──────────────────────────────────────────────────────────

def _fetch_description(url: str) -> str:
    """Fetch URL, strip HTML tags, return plain text. Returns '' on error."""
    try:
        resp = _HTTP_CLIENT.get(url, timeout=10.0)
        if resp.status_code >= 400:
            return ""
        html = resp.text
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    except Exception as exc:
        logger.debug("_fetch_description failed for %s: %s", url, exc)
        return ""


def _url_resolves(url: str) -> bool:
    """Return True if URL responds with HTTP status < 400."""
    try:
        resp = _HTTP_CLIENT.head(url, timeout=8.0)
        return resp.status_code < 400
    except Exception:
        # HEAD not supported — try GET
        try:
            resp = _HTTP_CLIENT.get(url, timeout=8.0)
            return resp.status_code < 400
        except Exception:
            return False


# ──────────────────────────────────────────────────────────
#  JobScorer
# ──────────────────────────────────────────────────────────
class JobScorer:
    def __init__(self, check_career_page: bool = True):
        self.check_career_page = check_career_page
        self._vol_cache = _VolumeCache()
        self._session: Optional[Session] = None

    def set_session(self, session: Session):
        self._session = session
        self._vol_cache.clear()

    def score(self, job: Job) -> tuple[int, dict[str, int]]:
        """
        Score a single job.
        Returns (total_score, breakdown_dict).
        Never raises — all signals are individually guarded.
        """
        breakdown: dict[str, int] = {}

        # ── Signal 1: career_page_match ─────────────────────────
        try:
            if self.check_career_page:
                from core.career_checker import title_matches_career_page
                match = title_matches_career_page(job.title, job.company)
                breakdown["career_page_match"] = SIGNAL_WEIGHTS["career_page_match"] if match else 0
            else:
                breakdown["career_page_match"] = 0
        except Exception as exc:
            logger.warning("Signal 1 error for job %s: %s", job.id, exc)
            breakdown["career_page_match"] = 0

        # ── Signal 2: recently_posted ────────────────────────────
        try:
            parsed = _parse_date(job.date_posted or "")
            if parsed is None:
                breakdown["recently_posted"] = 0
            else:
                age_days = (datetime.now(timezone.utc) - parsed).days
                breakdown["recently_posted"] = (
                    SIGNAL_WEIGHTS["recently_posted"] if age_days <= RECENT_DAYS else 0
                )
        except Exception as exc:
            logger.warning("Signal 2 error for job %s: %s", job.id, exc)
            breakdown["recently_posted"] = 0

        # ── Signal 3: company_volume ─────────────────────────────
        try:
            if self._session is None:
                breakdown["company_volume"] = SIGNAL_WEIGHTS["company_volume"]  # benefit of doubt
            else:
                vol = self._vol_cache.get(self._session, job.company)
                if vol == 0:
                    # Company not seen before — benefit of doubt
                    breakdown["company_volume"] = SIGNAL_WEIGHTS["company_volume"]
                elif vol >= MIN_VOLUME:
                    # Confirmed active hiring
                    breakdown["company_volume"] = SIGNAL_WEIGHTS["company_volume"]
                else:
                    # vol is 1 or 2 — confirmed low volume
                    breakdown["company_volume"] = 0
        except Exception as exc:
            logger.warning("Signal 3 error for job %s: %s", job.id, exc)
            breakdown["company_volume"] = SIGNAL_WEIGHTS["company_volume"]

        # ── Signal 4: not_a_repost ───────────────────────────────
        try:
            if job.first_seen is None:
                breakdown["not_a_repost"] = SIGNAL_WEIGHTS["not_a_repost"]  # benefit of doubt
            else:
                first = job.first_seen
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - first).days
                breakdown["not_a_repost"] = (
                    SIGNAL_WEIGHTS["not_a_repost"] if age_days <= REPOST_DAYS else 0
                )
        except Exception as exc:
            logger.warning("Signal 4 error for job %s: %s", job.id, exc)
            breakdown["not_a_repost"] = SIGNAL_WEIGHTS["not_a_repost"]

        # ── Signal 5: url_resolves ───────────────────────────────
        try:
            breakdown["url_resolves"] = (
                SIGNAL_WEIGHTS["url_resolves"] if job.url and _url_resolves(job.url) else 0
            )
        except Exception as exc:
            logger.warning("Signal 5 error for job %s: %s", job.id, exc)
            breakdown["url_resolves"] = 0

        # ── Signal 6: has_salary ─────────────────────────────────
        breakdown["has_salary"] = (
            SIGNAL_WEIGHTS["has_salary"]
            if (job.salary and job.salary.strip())
            else 0
        )

        # ── Signal 7: rich_description ───────────────────────────
        try:
            if job.url:
                text = _fetch_description(job.url)
                wc = len(text.split())
                breakdown["rich_description"] = (
                    SIGNAL_WEIGHTS["rich_description"] if wc >= MIN_DESC_WORDS else 0
                )
            else:
                breakdown["rich_description"] = 0
        except Exception as exc:
            logger.warning("Signal 7 error for job %s: %s", job.id, exc)
            breakdown["rich_description"] = 0

        total = sum(breakdown.values())
        return total, breakdown


# ──────────────────────────────────────────────────────────
#  Batch scoring
# ──────────────────────────────────────────────────────────

def score_all_active_jobs(
    engine,
    check_career_page: bool = True,
    rescore: bool = False,
) -> int:
    """
    Score all active jobs that haven't been scored yet (or all if rescore=True).
    Commits every 20 jobs.
    Returns the number of jobs scored.
    """
    from sqlalchemy.orm import sessionmaker

    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    scorer = JobScorer(check_career_page=check_career_page)
    scorer.set_session(session)
    count = 0

    try:
        query = session.query(Job).filter(Job.is_active)
        if not rescore:
            query = query.filter(Job.legitimacy_score is None)

        jobs = query.all()
        total = len(jobs)
        logger.info("Scoring %d jobs (check_career_page=%s)", total, check_career_page)

        for i, job in enumerate(jobs, 1):
            try:
                score, breakdown = scorer.score(job)
                job.legitimacy_score = score
                job.score_breakdown   = breakdown
                job.suspected_ghost   = score < GHOST_THRESHOLD
                count += 1

                if count % 20 == 0:
                    session.commit()
                    logger.info("  Scored %d/%d jobs...", count, total)

            except Exception as exc:
                logger.error("Failed to score job id=%s: %s", job.id, exc)
                session.rollback()

        session.commit()
        logger.info("Scoring complete: %d jobs scored", count)
        return count

    except Exception as exc:
        session.rollback()
        logger.error("score_all_active_jobs failed: %s", exc)
        return count
    finally:
        session.close()
