"""
Abstract base scraper class.
All scrapers inherit from BaseScraper and implement scrape().
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import sessionmaker

from core.deduplicator import find_duplicate
from core.models import Job

logger = logging.getLogger(__name__)


@dataclass
class ScraperResult:
    title:       str
    company:     str
    location:    str
    salary:      str
    date_posted: str
    url:         str
    description: str
    source:      str
    # Optional extras
    raw_data:    dict = field(default_factory=dict)


class BaseScraper(ABC):
    """Abstract base class for all job board scrapers."""

    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"scrapers.{name}")

    @abstractmethod
    def scrape(self) -> list[ScraperResult]:
        """
        Fetch job listings from the source.
        Must return a list of ScraperResult objects.
        Must never raise — catch all exceptions internally and return partial results.
        """
        ...

    def save_to_db(self, results: list[ScraperResult], engine) -> tuple[int, int]:
        """
        Upsert results into the database.
        - New jobs: INSERT
        - Existing jobs: update last_seen
        Returns (new_count, duplicate_count).
        """
        Session = sessionmaker(bind=engine)
        session = Session()
        new_count = 0
        dup_count = 0

        try:
            batch = []
            for result in results:
                try:
                    existing = find_duplicate(session, result.title, result.company, result.url)
                    if existing:
                        # Update last_seen to signal this job is still active
                        existing.last_seen = datetime.now(timezone.utc)
                        existing.is_active = True
                        dup_count += 1
                    else:
                        job = Job(
                            title       = result.title[:500],
                            company     = result.company[:300],
                            location    = (result.location or "")[:300],
                            salary      = (result.salary or "")[:200],
                            date_posted = (result.date_posted or "")[:100],
                            url         = (result.url or "")[:2000],
                            description = result.description or "",
                            source      = result.source[:50],
                            is_active   = True,
                            first_seen  = datetime.now(timezone.utc),
                            last_seen   = datetime.now(timezone.utc),
                        )
                        session.add(job)
                        batch.append(job)
                        new_count += 1

                    # Commit in batches of 50
                    if (new_count + dup_count) % 50 == 0:
                        session.commit()

                except Exception as exc:
                    self.logger.warning("Failed to save result %r: %s", result.url, exc)
                    session.rollback()

            session.commit()
            self.logger.info(
                "Saved: %d new, %d duplicates skipped", new_count, dup_count
            )
            return new_count, dup_count

        except Exception as exc:
            session.rollback()
            self.logger.error("save_to_db failed: %s", exc)
            return new_count, dup_count
        finally:
            session.close()

    def run(self, engine) -> tuple[int, int]:
        """Scrape + save in one call. Returns (new_count, dup_count)."""
        self.logger.info("Starting scrape: %s", self.name)
        results = self.scrape()
        self.logger.info("Scraped %d results from %s", len(results), self.name)
        return self.save_to_db(results, engine)
