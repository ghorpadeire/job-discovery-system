"""
Cross-source job deduplication.

Priority:
  1. Exact URL match
  2. Normalised title + company fingerprint hash
"""
import hashlib
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from core.models import Job

logger = logging.getLogger(__name__)

# Suffixes to strip from company names
_COMPANY_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|inc|llc|corp|corporation|group|ireland|dublin|"
    r"technologies|technology|solutions|services|consulting|international)\b",
    re.IGNORECASE,
)

# Strip seniority prefixes/suffixes from titles for fingerprinting
_TITLE_NOISE = re.compile(
    r"\b(junior|senior|lead|principal|staff|associate|graduate|mid[-\s]level|"
    r"entry[-\s]level|contract|permanent|part[-\s]time|full[-\s]time)\b",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────
#  Normalisation helpers
# ─────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """Lowercase, strip seniority noise, remove punctuation, collapse whitespace."""
    t = title.lower()
    t = _TITLE_NOISE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_company(company: str) -> str:
    """Strip Ltd/Limited/plc suffixes, lowercase, collapse whitespace."""
    c = company.lower()
    c = _COMPANY_SUFFIXES.sub("", c)
    c = re.sub(r"[^\w\s]", " ", c)
    c = re.sub(r"\s+", " ", c).strip()
    return c


def job_fingerprint(title: str, company: str) -> str:
    """Return an MD5 hash of the normalised title + company pair."""
    key = f"{normalize_title(title)}|{normalize_company(company)}"
    return hashlib.md5(key.encode()).hexdigest()


# ─────────────────────────────────────────────
#  DB lookup
# ─────────────────────────────────────────────

def find_duplicate(session: Session, title: str, company: str, url: str) -> Optional[Job]:
    """
    Return an existing Job that matches this posting, or None if it's new.

    Strategy:
      1. Exact URL match (fastest, most reliable)
      2. Title+company fingerprint match (catches cross-source dupes)
    """
    # 1. Exact URL
    existing = session.query(Job).filter(Job.url == url).first()
    if existing:
        return existing

    # 2. Fingerprint — scan only active jobs to avoid touching archived data
    fp = job_fingerprint(title, company)
    for job in session.query(Job).filter(Job.is_active).all():
        if job_fingerprint(job.title, job.company) == fp:
            return job

    return None


# ─────────────────────────────────────────────
#  Batch merge
# ─────────────────────────────────────────────

def merge_duplicates(engine) -> int:
    """
    Batch deduplication pass.
    - Groups jobs by URL and deactivates older duplicates
    - Groups by title+company fingerprint and merges cross-source dupes
    Returns the number of duplicates deactivated.
    """
    from datetime import datetime, timezone
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine)
    session = Session()
    deactivated = 0

    try:
        # ── URL duplicates ──────────────────────────────────────────────
        url_seen: dict[str, int] = {}
        for job in session.query(Job).filter(Job.is_active).order_by(Job.first_seen).all():
            if job.url in url_seen:
                # Keep the older record (first_seen earlier), deactivate this one
                job.is_active = False
                deactivated += 1
                logger.debug("Deactivated URL duplicate: %s", job.url)
            else:
                url_seen[job.url] = job.id

        # ── Fingerprint duplicates ──────────────────────────────────────
        fp_seen: dict[str, Job] = {}
        for job in session.query(Job).filter(Job.is_active).order_by(Job.first_seen).all():
            fp = job_fingerprint(job.title, job.company)
            if fp in fp_seen:
                survivor = fp_seen[fp]
                # Update last_seen on survivor, deactivate this duplicate
                survivor.last_seen = datetime.now(timezone.utc)
                job.is_active = False
                deactivated += 1
                logger.debug(
                    "Deactivated fingerprint duplicate: '%s' @ '%s' (survivor id=%s)",
                    job.title, job.company, survivor.id,
                )
            else:
                fp_seen[fp] = job

        session.commit()
        logger.info("merge_duplicates: deactivated %d duplicates", deactivated)
        return deactivated

    except Exception as exc:
        session.rollback()
        logger.error("merge_duplicates failed: %s", exc)
        return 0
    finally:
        session.close()
