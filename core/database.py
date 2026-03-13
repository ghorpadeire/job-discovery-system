"""
Database engine factory and connection helpers.
Automatically runs migrations on startup.
"""
import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from core.models import Base, migrate_scoring_columns, migrate_tracker_table

load_dotenv()
logger = logging.getLogger(__name__)

_engine = None  # module-level singleton


def get_engine(database_url: str | None = None):
    """Return (and cache) a SQLAlchemy engine."""
    global _engine
    if _engine is not None:
        return _engine

    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in environment or .env file")

    # psycopg2 requires postgresql:// scheme
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    _engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args={"connect_timeout": 10},
    )
    _bootstrap(_engine)
    return _engine


# All columns that must exist on the jobs table (covers old DBs missing base cols)
_ALL_JOB_COLUMNS = [
    ("location",         "VARCHAR(300)"),
    ("salary",           "VARCHAR(200)"),
    ("date_posted",      "VARCHAR(100)"),
    ("description",      "TEXT"),
    ("source",           "VARCHAR(50)"),
    ("is_active",        "BOOLEAN DEFAULT TRUE"),
    ("first_seen",       "TIMESTAMP DEFAULT now()"),
    ("last_seen",        "TIMESTAMP DEFAULT now()"),
    ("legitimacy_score", "INTEGER"),
    ("score_breakdown",  "JSONB"),
    ("suspected_ghost",  "BOOLEAN DEFAULT FALSE"),
    ("tg_alerted",       "BOOLEAN DEFAULT FALSE"),
    ("ai_reasoning",     "TEXT"),
]


def _bootstrap(engine) -> None:
    """Create all tables and run idempotent migrations — works on fresh and existing DBs."""
    try:
        from sqlalchemy import inspect, text as _text
        # Create tables if they don't exist at all
        Base.metadata.create_all(engine)

        # Ensure every column exists on jobs table (handles old schema)
        inspector = inspect(engine)
        if "jobs" in inspector.get_table_names():
            existing = {col["name"] for col in inspector.get_columns("jobs")}
            with engine.begin() as conn:
                for col_name, col_def in _ALL_JOB_COLUMNS:
                    if col_name not in existing:
                        conn.execute(_text(
                            f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col_name} {col_def}"
                        ))
                        logger.info("Migration: added jobs.%s", col_name)

        migrate_tracker_table(engine)
        logger.info("Database bootstrap complete")
    except Exception as exc:
        logger.error("Database bootstrap failed: %s", exc)
        raise


def check_connection(database_url: str | None = None) -> bool:
    """Return True if the database is reachable, False otherwise."""
    try:
        engine = get_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection: OK")
        return True
    except OperationalError as exc:
        logger.error("Database connection FAILED: %s", exc)
        return False
    except Exception as exc:
        logger.error("Unexpected DB error: %s", exc)
        return False
