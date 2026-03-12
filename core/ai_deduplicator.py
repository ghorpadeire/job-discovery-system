"""
Embedding-based duplicate detection.

Algorithm
---------
1. Generate text-embedding-3-small vectors for all active jobs without one.
2. Load all active jobs that have an embedding.
3. Build an (N × 1536) numpy matrix and compute the full cosine-similarity
   matrix in one vectorised operation — O(N²) but trivially fast for <10k jobs.
4. Any pair (i, j) where similarity >= THRESHOLD is considered a near-duplicate.
5. Keep the job with the lower id (older); merge sources + back-fill missing
   fields from the newer one; mark the newer one is_active=False.

Threshold
---------
0.92 catches cross-source reposts of the same role with minor wording changes
while avoiding false positives between similar-sounding but distinct roles
(e.g. "Java Developer" vs "Senior Java Developer").
"""
import logging
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from core.ai_client import get_embedding
from core.models import Job

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.92
EMBEDDING_MODEL      = "text-embedding-3-small"

# ---------------------------------------------------------------------------
# Text to embed
# ---------------------------------------------------------------------------

def _job_to_embed_text(job: Job) -> str:
    """
    Build the canonical text we embed for a job.
    Combines the most stable identifiers: title, company, description snippet.
    Salary and search_term are deliberately excluded (they vary by source).
    """
    parts = [
        job.title.strip(),
        job.company.strip(),
    ]
    if job.description:
        # First 400 chars of description carries the most signal
        parts.append(job.description[:400].strip())
    return " | ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Step 1 — ensure embeddings exist
# ---------------------------------------------------------------------------

def ensure_embeddings(engine, rescore: bool = False) -> int:
    """
    Generate and store embeddings for every active job that doesn't have one.
    If *rescore* is True, regenerate even existing embeddings.
    Returns number of jobs embedded.
    """
    embedded = 0
    with Session(engine) as session:
        q = session.query(Job).filter(Job.is_active == True)
        if not rescore:
            q = q.filter(Job.embedding == None)
        jobs = q.all()

        if not jobs:
            logger.info("  All active jobs already have embeddings.")
            return 0

        logger.info(f"  Generating embeddings for {len(jobs)} job(s)…")
        for i, job in enumerate(jobs, 1):
            text = _job_to_embed_text(job)
            logger.info(f"    [{i}/{len(jobs)}] {job.title!r} @ {job.company!r}")
            try:
                job.embedding = get_embedding(text, model=EMBEDDING_MODEL)
                embedded += 1
            except Exception as exc:
                logger.warning(f"    embedding failed (job {job.id}): {exc}")

            if embedded % 20 == 0:
                session.commit()
                logger.info(f"    …committed {embedded} embeddings so far")

        session.commit()

    return embedded


# ---------------------------------------------------------------------------
# Step 2 — find and merge near-duplicates
# ---------------------------------------------------------------------------

def find_and_merge_embedding_duplicates(engine) -> int:
    """
    Compute pairwise cosine similarities for all embedded active jobs.
    Merge any pair whose similarity >= SIMILARITY_THRESHOLD.
    Returns the number of duplicate records deactivated.
    """
    # ---- Load all active jobs with embeddings ----
    with Session(engine) as session:
        jobs = (
            session.query(Job)
            .filter(Job.is_active == True)
            .filter(Job.embedding != None)
            .order_by(Job.id)   # lower id = older = keep
            .all()
        )

    if len(jobs) < 2:
        logger.info("  Not enough embedded jobs for comparison.")
        return 0

    logger.info(f"  Building {len(jobs)}×{len(jobs)} cosine similarity matrix…")

    # ---- Build matrix — shape (N, 1536) ----
    ids  : list[int]        = []
    vecs : list[list[float]] = []

    for j in jobs:
        vec = j.embedding
        if isinstance(vec, list) and len(vec) > 0:
            ids.append(j.id)
            vecs.append(vec)

    if len(vecs) < 2:
        return 0

    mat = np.array(vecs, dtype=np.float32)          # shape (N, 1536)

    # L2-normalise each row then dot-product = cosine similarity
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    mat   = mat / norms
    sim   = mat @ mat.T                              # shape (N, N)

    # ---- Collect duplicate pairs ----
    # Only look at upper triangle (i < j) to avoid double-counting
    n = len(ids)
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= SIMILARITY_THRESHOLD:
                pairs.append((ids[i], ids[j], float(sim[i, j])))

    if not pairs:
        logger.info(f"  No near-duplicate pairs found above {SIMILARITY_THRESHOLD}.")
        return 0

    logger.info(f"  Found {len(pairs)} near-duplicate pair(s) — merging…")

    # ---- Merge: keep lower id, deactivate higher id ----
    deactivated  = 0
    seen_deact   : set[int] = set()   # guard against chained merges

    with Session(engine) as session:
        for keep_id, drop_id, score in pairs:
            if drop_id in seen_deact:
                continue                     # already merged in a prior pair

            job_keep = session.get(Job, keep_id)
            job_drop = session.get(Job, drop_id)

            if not job_keep or not job_drop:
                continue
            if not job_drop.is_active:
                continue                     # already inactive

            logger.info(
                f"    merge (sim={score:.4f}): "
                f"keep #{keep_id} {job_keep.title!r} | "
                f"drop #{drop_id} {job_drop.title!r}"
            )

            # Merge sources
            current = list(job_keep.sources or [])
            for src in (job_drop.sources or []):
                if src not in current:
                    current.append(src)
            job_keep.sources = current

            # Back-fill any missing fields on the survivor
            if not job_keep.url         and job_drop.url:         job_keep.url         = job_drop.url
            if not job_keep.salary      and job_drop.salary:      job_keep.salary      = job_drop.salary
            if not job_keep.description and job_drop.description: job_keep.description = job_drop.description

            # Deactivate the duplicate
            job_drop.is_active = False
            seen_deact.add(drop_id)
            deactivated += 1

        session.commit()

    return deactivated


# ---------------------------------------------------------------------------
# Convenience: run both steps
# ---------------------------------------------------------------------------

def run_embedding_pipeline(engine, rescore_embeddings: bool = False) -> dict:
    """Embed all unembedded jobs then find and merge duplicates."""
    embedded   = ensure_embeddings(engine, rescore=rescore_embeddings)
    merged     = find_and_merge_embedding_duplicates(engine)
    return {"embedded": embedded, "merged": merged}
