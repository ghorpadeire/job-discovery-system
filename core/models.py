"""
SQLAlchemy ORM models — PostgreSQL with JSON `sources` tracking.

The `sources` column records every platform a job was found on, e.g.
    ["irishjobs.ie", "indeed.ie"]
so the deduplication layer can merge cross-platform duplicates.
"""
import hashlib
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"

    id          = Column(Integer,  primary_key=True, autoincrement=True)
    title       = Column(String,   nullable=False)
    company     = Column(String,   nullable=False)
    date_posted = Column(String)                          # raw string — formats vary
    url         = Column(String)
    salary      = Column(String)
    source      = Column(String)                          # first/primary source
    sources     = Column(JSON, nullable=False, default=list)  # all platforms seen on
    search_term = Column(String)                          # query that found this job
    first_seen  = Column(DateTime, default=datetime.utcnow)
    last_seen   = Column(DateTime, default=datetime.utcnow)
    is_active   = Column(Boolean,  default=True)
    fingerprint = Column(String,   unique=True, nullable=False)  # MD5(title|company)

    # --- Scoring (Phase 3) ---
    legitimacy_score = Column(Integer,  default=None)   # 0-100; None = not yet scored
    score_breakdown  = Column(JSON,     default=None)   # {signal_name: points_awarded}
    suspected_ghost  = Column(Boolean,  default=False)  # True when score < 30

    # --- AI layer (Phase 4) ---
    description      = Column(Text,    default=None)    # fetched job description text
    embedding        = Column(JSON,    default=None)    # text-embedding-3-small vector (1536 floats)
    relevance_score  = Column(Float,   default=None)    # 0-10 profile match
    ghost_probability= Column(Float,   default=None)    # 0-1 likelihood of ghost job
    ai_reasoning     = Column(JSON,    default=None)    # {"relevance": "...", "ghost": "..."}
    combined_score   = Column(Float,   default=None)    # (legitimacy*0.6) + (relevance*10*0.4)

    # --- Telegram bot (Phase 5) ---
    tg_alerted       = Column(Boolean, default=False)   # True once an instant alert was sent

    def __repr__(self) -> str:
        return (
            f"<Job id={self.id} title={self.title!r} "
            f"company={self.company!r} sources={self.sources}>"
        )


TRACKER_STATUSES = ["saved", "applied", "interview", "offer", "rejected"]


class ApplicationTracker(Base):
    """Tracks which jobs the user is actively pursuing through the hiring funnel."""
    __tablename__ = "application_tracker"

    id         = Column(Integer,  primary_key=True, autoincrement=True)
    job_id     = Column(Integer,  ForeignKey("jobs.id"), nullable=False, unique=True)
    status     = Column(String,   nullable=False, default="saved")  # saved/applied/interview/offer/rejected
    notes      = Column(Text,     default=None)
    applied_at = Column(DateTime, default=None)   # set when status moves to "applied"
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<ApplicationTracker job_id={self.job_id} status={self.status!r}>"


def migrate_tracker_table(engine) -> None:
    """Idempotent: create application_tracker table if it doesn't exist."""
    Base.metadata.tables["application_tracker"].create(engine, checkfirst=True)


def make_fingerprint(title: str, company: str) -> str:
    """Stable, case-insensitive dedup key derived from title + company."""
    key = f"{title.lower().strip()}|{company.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def migrate_ai_columns(engine) -> None:
    """
    Idempotent: add the six AI-layer columns to an existing jobs table.
    Safe to call even if columns already exist.
    """
    stmts = [
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description       TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS embedding         JSON",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS relevance_score   FLOAT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ghost_probability FLOAT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_reasoning      JSON",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS combined_score    FLOAT",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.commit()


def migrate_scoring_columns(engine) -> None:
    """
    Idempotent: add the three scoring columns to an existing table.
    Safe to call even if columns already exist (catches duplicate-column errors).
    """
    stmts = [
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS legitimacy_score INTEGER",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS score_breakdown  JSON",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS suspected_ghost  BOOLEAN DEFAULT FALSE",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.commit()
