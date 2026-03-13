"""
RemoteOK scraper — uses the official public JSON API.
No browser needed, no bot detection. Returns tech/remote jobs.
"""
import logging
import time
import urllib.request
import json
from typing import Optional

from scrapers.base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"

# Tags relevant to Pranav's target roles
TARGET_TAGS = {
    "java", "javascript", "python", "cybersecurity", "security",
    "software", "developer", "engineer", "backend", "fullstack",
    "full-stack", "it", "helpdesk", "support", "spring", "devops",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class RemoteOKScraper(BaseScraper):
    def __init__(self):
        super().__init__("remoteok")

    def scrape(self) -> list[ScraperResult]:
        try:
            req = urllib.request.Request(
                API_URL,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())

            # First element is a legal notice, skip it
            jobs = [j for j in data if isinstance(j, dict) and j.get("id")]

            results = []
            for job in jobs:
                tags = [str(t).lower() for t in (job.get("tags") or [])]
                # Filter to relevant roles
                if not any(t in TARGET_TAGS for t in tags):
                    continue

                title = job.get("position") or ""
                company = job.get("company") or ""
                url = job.get("url") or f"https://remoteok.com/l/{job.get('id','')}"
                location = "Remote"
                salary = ""
                if job.get("salary_min") and job.get("salary_max"):
                    salary = f"${job['salary_min']:,}–${job['salary_max']:,}/yr"
                elif job.get("salary_min"):
                    salary = f"${job['salary_min']:,}+/yr"

                description = job.get("description") or ""
                date_posted = job.get("date") or ""

                if not title or not company:
                    continue

                results.append(ScraperResult(
                    title=title,
                    company=company,
                    location=location,
                    salary=salary,
                    url=url,
                    date_posted=date_posted,
                    description=description[:2000],
                    source="remoteok",
                ))

            self.logger.info("RemoteOK: scraped %d relevant jobs", len(results))
            return results

        except Exception as exc:
            self.logger.error("RemoteOK scrape failed: %s", exc)
            return []
