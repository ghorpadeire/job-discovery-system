"""
SQLAlchemy models for the Job Discovery System.
Supports PostgreSQL 16 with JSON columns and full migration helpers.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON,
    String, Text, inspect, text
)
from sqlalchemy.orm import DeclarativeBase, relationship

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
#  Job
# ─────────────────────────────────────────────
class Job(Base):
    __tablename__ = "jobs"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    title             = Column(String(500), nullable=False)
    company           = Column(String(300), nullable=False)
    location          = Column(String(300))
    salary            = Column(String(200))
    date_posted       = Column(String(100))
    url               = Column(String(2000), unique=True)
    description       = Column(Text)
    source            = Column(String(50))          # "irishjobs" | "indeed"
    is_active         = Column(Boolean, default=True)
    first_seen        = Column(DateTime, default=_utcnow)
    last_seen         = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Scoring columns (added via migration if missing)
    legitimacy_score  = Column(Integer, nullable=True)
    score_breakdown   = Column(JSON, nullable=True)   # {signal_name: points}
    suspected_ghost   = Column(Boolean, default=False)
    tg_alerted        = Column(Boolean, default=False)
    ai_reasoning      = Column(Text, nullable=True)   # reserved for OpenAI

    # Relationships
    tracker = relationship(
        "ApplicationTracker",
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id} score={self.legitimacy_score} title={self.title!r}>"

    @property
    def score_tier(self) -> str:
        if self.legitimacy_score is None:
            return "unscored"
        if self.legitimacy_score >= 85:
            return "very_high"
        if self.legitimacy_score >= 70:
            return "high"
        if self.legitimacy_score >= 50:
            return "medium"
        return "low"


# ─────────────────────────────────────────────
#  ApplicationTracker
# ─────────────────────────────────────────────
VALID_STATUSES = ("saved", "applied", "interview", "offer", "rejected")


class ApplicationTracker(Base):
    __tablename__ = "application_tracker"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    job_id     = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    status     = Column(String(50), default="saved")
    notes      = Column(Text)
    applied_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    job = relationship("Job", back_populates="tracker")

    def __repr__(self) -> str:
        return f"<ApplicationTracker job_id={self.job_id} status={self.status}>"


# ─────────────────────────────────────────────
#  Migration helpers  (idempotent)
# ─────────────────────────────────────────────
_SCORING_COLS = {
    "legitimacy_score": "INTEGER",
    "score_breakdown":  "JSONB",
    "suspected_ghost":  "BOOLEAN DEFAULT FALSE",
    "tg_alerted":       "BOOLEAN DEFAULT FALSE",
    "ai_reasoning":     "TEXT",
}


def migrate_scoring_columns(engine) -> None:
    """Idempotently add scoring columns to the jobs table if missing."""
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    if "jobs" not in existing_tables:
        # Table doesn't exist yet — Base.metadata.create_all will handle it
        logger.info("jobs table not yet created; skipping scoring column migration")
        return

    existing_cols = {col["name"] for col in inspector.get_columns("jobs")}

    with engine.begin() as conn:
        for col_name, col_def in _SCORING_COLS.items():
            if col_name not in existing_cols:
                conn.execute(
                    text(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                )
                logger.info("Added column jobs.%s", col_name)


def migrate_tracker_table(engine) -> None:
    """Idempotently create the application_tracker table."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS application_tracker (
                id         SERIAL PRIMARY KEY,
                job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                status     VARCHAR(50) DEFAULT 'saved',
                notes      TEXT,
                applied_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT now()
            )
        """))
    logger.info("application_tracker table ensured")
