"""
SQLAlchemy ORM models for the Job Discovery system.
"""
import hashlib
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, create_engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    title       = Column(String,  nullable=False)
    company     = Column(String,  nullable=False)
    date_posted = Column(String)          # raw string — formats vary per site
    url         = Column(String)
    salary      = Column(String)
    source      = Column(String, default="irishjobs.ie")
    search_term = Column(String)          # which query produced this result
    first_seen  = Column(DateTime, default=datetime.utcnow)
    last_seen   = Column(DateTime, default=datetime.utcnow)
    is_active   = Column(Boolean, default=True)
    fingerprint = Column(String,  unique=True, nullable=False)  # dedup key

    def __repr__(self):
        return f"<Job id={self.id} title={self.title!r} company={self.company!r}>"


def make_fingerprint(title: str, company: str) -> str:
    """Stable dedup key from (title, company). Case-insensitive."""
    key = f"{title.lower().strip()}|{company.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def init_db(db_path: str = "jobs.db"):
    """Create DB + tables, return engine."""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    return engine
