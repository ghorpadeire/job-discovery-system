"""
AI-powered job scoring using GPT-4o-mini.

Two LLM signals
---------------
  relevance_score    (0–10)  — profile fit for Pranav Ghorpade
  ghost_probability  (0–1)   — likelihood this is a ghost/fake listing

Combined score formula (0–100)
-------------------------------
  combined = (legitimacy_score * 0.6) + (relevance_score * 10 * 0.4)

  legitimacy_score is from the rule-based scorer (core/scorer.py).
  If a job hasn't been legitimacy-scored yet it is treated as 0 for the
  combined calculation but the field is left NULL in the DB.

Description fetching
--------------------
  Before scoring, we attempt a lightweight httpx GET on the job's apply URL
  and strip HTML to get the visible text.  The description is cached in
  `job.description` so the two prompt calls share one fetch.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from core.ai_client import chat_completion
from core.models import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Candidate profile (fixed — update here when profile changes)
# ---------------------------------------------------------------------------

_PROFILE = """\
Name:          Pranav Ghorpade
Education:     MSc Cybersecurity, NCI Dublin (2024)
Certifications: CEH Master (Certified Ethical Hacker)
Skills:        Java, Spring Boot, Python, Docker, Linux, Network Security, SQL, Git
Experience:    Academic / project work only — NO paid employment yet
Visa:          Irish Stamp 1G (full work rights; needs employer sponsorship for future permits)
Location:      Dublin, Ireland — open to remote or hybrid within Ireland
Seeking:       Entry-level, Graduate, or Junior roles ONLY
NOT suitable:  Senior, Lead, Manager, Architect, or 3+ years required roles\
""".strip()

# ---------------------------------------------------------------------------
# Description fetcher
# ---------------------------------------------------------------------------

_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"}
_DESC_TRIM     = 800    # max chars of description sent to each prompt


def fetch_description(url: str, timeout: int = 10) -> str:
    """
    GET the job apply page and return stripped visible text.
    Returns empty string on any failure — never raises.
    """
    if not url:
        return ""
    try:
        r = httpx.get(url, follow_redirects=True, timeout=timeout,
                      headers=_FETCH_HEADERS)
        if r.status_code >= 400:
            return ""
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]         # cap before returning; callers can trim further
    except Exception as exc:
        logger.debug(f"  fetch_description failed ({url!r}): {exc}")
        return ""


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    """Extract the first JSON object from *raw*, handling markdown fences."""
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    m   = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"No JSON object found in response: {raw!r}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------------------
# Signal 1 — Relevance (0–10)
# ---------------------------------------------------------------------------

_RELEVANCE_SYSTEM = """\
You are a career advisor evaluating job fit for a specific candidate.

Given the candidate profile and a job posting, respond with EXACTLY this JSON
and nothing else (no markdown, no extra text):
  {"score": <integer 0-10>, "reason": "<one concise sentence>"}

Scoring guide:
  10  Perfect match — entry/junior/grad level, matches skills, Dublin / remote Ireland
  7-9 Good fit — most criteria match, minor gaps
  4-6 Partial fit — some mismatch (slightly senior, partial skill overlap)
  1-3 Poor fit — wrong level (senior), wrong domain, or wrong location
  0   Completely irrelevant\
""".strip()


def score_relevance(job: Job, description: str = "") -> tuple[float, str]:
    """
    Returns (relevance_score 0–10, one-sentence reason).
    Falls back to (0.0, "error: …") on failure.
    """
    desc_snippet = (description or job.description or "No description available.")[:_DESC_TRIM]
    days_open    = _days_open(job)

    job_block = (
        f"Title:       {job.title}\n"
        f"Company:     {job.company}\n"
        f"Salary:      {job.salary or 'Not stated'}\n"
        f"Days listed: {days_open}\n"
        f"Sources:     {', '.join(job.sources or [job.source or '?'])}\n"
        f"\nDescription:\n{desc_snippet}"
    )

    messages = [
        {"role": "system", "content": _RELEVANCE_SYSTEM},
        {"role": "user",   "content": f"CANDIDATE PROFILE:\n{_PROFILE}\n\nJOB POSTING:\n{job_block}"},
    ]

    try:
        raw    = chat_completion(messages, temperature=0.1, max_tokens=128)
        data   = _extract_json(raw)
        score  = float(data.get("score",  0))
        reason = str(data.get("reason", "")).strip()
        return max(0.0, min(10.0, score)), reason
    except Exception as exc:
        logger.warning(f"  relevance scoring failed (job {job.id}): {exc}")
        return 0.0, f"error: {exc}"


# ---------------------------------------------------------------------------
# Signal 2 — Ghost probability (0–1)
# ---------------------------------------------------------------------------

_GHOST_SYSTEM = """\
You are a job posting authenticity analyst.

Analyse whether the job is a "ghost job" — a listing with no real intent to hire,
posted to harvest CVs, pad pipelines, or fulfill compliance requirements.

Respond with EXACTLY this JSON and nothing else:
  {"ghost_probability": <float 0.0-1.0>, "reason": "<one concise sentence>"}

Ghost indicators (raise probability):
  - Listed 30+ days with no update
  - Vague or generic description
  - No specific tech stack or team context
  - No salary range
  - Well-known company name but role unverifiable
  - Multiple identical postings from the same company

Legitimacy indicators (lower probability):
  - Specific tech stack, team size, or project mentioned
  - Clear salary band
  - Posted within 14 days
  - Seen on multiple independent sources
  - Real hiring signals in description (interview process, team details)\
""".strip()


def score_ghost_probability(job: Job, description: str = "") -> tuple[float, str]:
    """
    Returns (ghost_probability 0–1, one-sentence reason).
    Falls back to (0.5, "error: …") on failure.
    """
    desc_snippet = (description or job.description or "No description available.")[:_DESC_TRIM]
    days_open    = _days_open(job)

    job_block = (
        f"Title:       {job.title}\n"
        f"Company:     {job.company}\n"
        f"Salary:      {job.salary or 'Not stated'}\n"
        f"Days listed: {days_open}\n"
        f"Sources:     {', '.join(job.sources or [job.source or '?'])}\n"
        f"\nDescription:\n{desc_snippet}"
    )

    messages = [
        {"role": "system", "content": _GHOST_SYSTEM},
        {"role": "user",   "content": f"Analyse this job posting:\n\n{job_block}"},
    ]

    try:
        raw    = chat_completion(messages, temperature=0.1, max_tokens=128)
        data   = _extract_json(raw)
        prob   = float(data.get("ghost_probability", 0.5))
        reason = str(data.get("reason", "")).strip()
        return max(0.0, min(1.0, prob)), reason
    except Exception as exc:
        logger.warning(f"  ghost scoring failed (job {job.id}): {exc}")
        return 0.5, f"error: {exc}"


# ---------------------------------------------------------------------------
# Combined score
# ---------------------------------------------------------------------------

def compute_combined_score(
    legitimacy_score: Optional[int],
    relevance_score:  float,
) -> float:
    """
    combined = (legitimacy_score × 0.6) + (relevance_score × 10 × 0.4)
    Range: 0–100.
    """
    ls = float(legitimacy_score or 0)
    rs = float(relevance_score  or 0)
    return round(ls * 0.6 + rs * 10.0 * 0.4, 1)


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def ai_score_all_active_jobs(
    engine,
    rescore:            bool = False,
    fetch_descriptions: bool = True,
) -> int:
    """
    For every active, un-AI-scored job (or all active if *rescore*):
      1. Optionally fetch and store the description from the apply URL.
      2. Call score_relevance() and score_ghost_probability().
      3. Compute and store combined_score.

    Returns the number of jobs scored.
    """
    scored = 0
    with Session(engine) as session:
        q = session.query(Job).filter(Job.is_active == True)
        if not rescore:
            q = q.filter(Job.relevance_score == None)
        jobs = q.all()

        if not jobs:
            logger.info("  No jobs pending AI scoring.")
            return 0

        logger.info(f"  AI scoring {len(jobs)} job(s)…")

        for i, job in enumerate(jobs, 1):
            logger.info(
                f"  [{i}/{len(jobs)}] {job.title!r} @ {job.company!r}"
            )

            # ---- Fetch description (cached in job.description) ----
            if fetch_descriptions and not job.description and job.url:
                logger.debug(f"    fetching description from {job.url}")
                raw_desc = fetch_description(job.url)
                if raw_desc:
                    job.description = raw_desc[:3000]

            desc = job.description or ""

            # ---- Relevance ----
            rel_score, rel_reason = score_relevance(job, description=desc)

            # ---- Ghost probability ----
            ghost_prob, ghost_reason = score_ghost_probability(job, description=desc)

            # ---- Combined ----
            combined = compute_combined_score(job.legitimacy_score, rel_score)

            # ---- Write to DB ----
            job.relevance_score   = rel_score
            job.ghost_probability = ghost_prob
            job.ai_reasoning      = {
                "relevance": rel_reason,
                "ghost":     ghost_reason,
            }
            job.combined_score = combined
            scored += 1

            logger.debug(
                f"    relevance={rel_score:.1f}  ghost={ghost_prob:.2f}  "
                f"combined={combined:.1f}"
            )

            # Commit every 10 jobs to minimise re-work on failure
            if scored % 10 == 0:
                session.commit()
                logger.info(f"    …committed {scored} scored so far")

        session.commit()

    return scored


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _days_open(job: Job) -> int:
    """Days since job.first_seen, or 0 if unknown."""
    if not job.first_seen:
        return 0
    fs = job.first_seen
    if fs.tzinfo is None:
        fs = fs.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - fs).days)
