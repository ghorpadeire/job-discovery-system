"""
Database engine and session factory.

Reads DATABASE_URL from the environment (via .env).
Defaults to a local PostgreSQL instance matching docker-compose.yml.
"""
import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core.models import Base

load_dotenv()

logger = logging.getLogger(__name__)

_engine = None

_DEFAULT_URL = "postgresql://jobsuser:jobspass@localhost:5432/jobsdb"


def get_engine():
    global _engine
    if _engine is None:
        url = os.getenv("DATABASE_URL", _DEFAULT_URL)
        _engine = create_engine(
            url,
            pool_pre_ping=True,   # reconnect after idle drop
            pool_size=5,
            max_overflow=10,
        )
        Base.metadata.create_all(_engine)
        logger.info(f"Database ready: {url.split('@')[-1]}")  # hide credentials
    return _engine


def get_session() -> Session:
    """Return a new SQLAlchemy session bound to the shared engine."""
    return Session(get_engine())


def check_connection() -> bool:
    """Return True if the database is reachable."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error(f"Database connection failed: {exc}")
        return False
