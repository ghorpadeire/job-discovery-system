"""
Cross-source deduplication layer.

Two jobs are treated as duplicates when their *normalised* (title, company)
fingerprints match — even if the raw strings differ slightly, e.g.:

    "Junior Java Developer"  @ "Accenture Ireland Ltd."
    "Java Developer (Entry)" @ "Accenture"

Both normalise to the same key and will be merged.

Merge rules
-----------
- The record with the earliest `first_seen` is kept as canonical.
- Its `sources` list absorbs all sources from the duplicates.
- Missing fields (url, salary, date_posted) are back-filled from duplicates.
- Duplicate records are marked `is_active = False`.

This runs *after* all scrapers have finished for the session.
"""
import hashlib
import logging
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from core.models import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_TITLE_NOISE = re.compile(
    r"\b(junior|senior|lead|principal|staff|associate|graduate|grad|"
    r"jr\.?|sr\.?|entry[\s\-]level|new\s+grad|mid[\s\-]level|contract|"
    r"permanent|temp(?:orary)?|hybrid|remote|onsite|part[\s\-]time|"
    r"full[\s\-]time)\b"
    r"|\(.*?\)"          # anything in parentheses
    r"|\[.*?\]",         # anything in square brackets
    re.IGNORECASE,
)

_COMPANY_NOISE = re.compile(
    r"\b(ltd\.?|limited|plc\.?|inc\.?|llc|gmbh|b\.v\.|s\.a\.|"
    r"ireland|dublin|group|holdings|technologies|technology|"
    r"solutions|services|consulting|international|global)\b\.?",
    re.IGNORECASE,
)


def _norm_title(title: str) -> str:
    t = _TITLE_NOISE.sub(" ", title.lower())
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _norm_company(company: str) -> str:
    c = _COMPANY_NOISE.sub(" ", company.lower())
    c = re.sub(r"[^\w\s]", " ", c)
    return " ".join(c.split())


def normalised_fingerprint(title: str, company: str) -> str:
    """MD5 of (normalised title | normalised company)."""
    key = f"{_norm_title(title)}|{_norm_company(company)}"
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Main dedup function
# ---------------------------------------------------------------------------

def merge_duplicates(engine) -> int:
    """
    Scan all active jobs, group by normalised fingerprint, and merge groups
    that contain more than one record.

    Returns the number of duplicate records deactivated.
    """
    merged_count = 0

    with Session(engine) as session:
        active_jobs: list[Job] = (
            session.query(Job).filter_by(is_active=True).all()
        )

        # Group by normalised fingerprint
        groups: dict[str, list[Job]] = defaultdict(list)
        for job in active_jobs:
            nfp = normalised_fingerprint(job.title, job.company)
            groups[nfp].append(job)

        for nfp, jobs in groups.items():
            if len(jobs) == 1:
                continue

            # Sort: oldest first → that's the canonical record
            jobs.sort(key=lambda j: j.first_seen or datetime.min)
            canonical = jobs[0]
            duplicates = jobs[1:]

            # Merge sources and back-fill missing fields
            merged_sources = set(canonical.sources or [])
            for dup in duplicates:
                merged_sources.update(dup.sources or [])
                if not canonical.salary      and dup.salary:      canonical.salary      = dup.salary
                if not canonical.url         and dup.url:         canonical.url         = dup.url
                if not canonical.date_posted and dup.date_posted: canonical.date_posted = dup.date_posted

                dup.is_active = False
                merged_count += 1
                logger.debug(
                    f"  Merged dup id={dup.id} '{dup.title}' @ '{dup.company}' "
                    f"{dup.sources} → canonical id={canonical.id}"
                )

            canonical.sources = sorted(merged_sources)

        session.commit()

    return merged_count


# ---------------------------------------------------------------------------
# Reporting helper
# ---------------------------------------------------------------------------

def multi_source_jobs(engine) -> list[Job]:
    """Return all active jobs found on more than one platform."""
    with Session(engine) as session:
        jobs = session.query(Job).filter_by(is_active=True).all()
        return [j for j in jobs if len(j.sources or []) > 1]
