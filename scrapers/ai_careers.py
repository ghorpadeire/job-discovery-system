"""
AI-powered company careers page scraper.

Strategy
--------
For each of the 100 companies in KNOWN_COMPANIES:
  1. Fetch the careers page via Jina AI Reader (r.jina.ai/{url})
     - Jina converts ANY page (including JS-heavy SPAs) to clean Markdown
     - Free tier, no API key needed
  2. If Jina returns thin content (<200 chars), fall back to direct httpx GET
  3. Send the Markdown to GPT-4o-mini with a structured extraction prompt
  4. Parse the JSON response into job dicts compatible with run_all.py

Target roles filtered:
  Java Developer / Software Engineer / Backend Developer
  Cybersecurity Analyst / Security Engineer / SOC Analyst / InfoSec
  IT Support / Helpdesk / Technical Support
  Network Engineer / Systems Administrator
  Graduate / Junior / Entry-level tech roles

All in Dublin, Ireland (or Remote/Hybrid from Ireland).

Usage
-----
  from scrapers.ai_careers import AICareersScaper
  jobs = await AICareersScaper().run_all()

Or via run_all.py:
  py run_all.py --sources ai
  py run_all.py --sources all          # includes AI scraper
"""

import asyncio
import json
import logging
import os
from typing import Optional

import httpx
from openai import AsyncOpenAI

from core.career_checker import KNOWN_COMPANIES
from core.progress import emitter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JINA_BASE        = "https://r.jina.ai/"
CONCURRENCY      = 4          # parallel company requests (be polite to Jina)
REQUEST_DELAY    = 1.2        # seconds between batches
JINA_TIMEOUT     = 20         # seconds per Jina fetch
OPENAI_MODEL     = "gpt-4o-mini"
MAX_MARKDOWN_LEN = 8_000      # chars sent to GPT (keeps cost low)
MIN_CONTENT_LEN  = 150        # min chars before trying fallback fetch

# Keywords that must appear in a job title/description for it to be relevant
TARGET_KEYWORDS = {
    "java", "spring", "kotlin",
    "cybersecurity", "cyber security", "security analyst", "soc analyst",
    "information security", "infosec", "network security", "penetration",
    "pentest", "security engineer", "security operations",
    "it support", "helpdesk", "help desk", "technical support", "desktop support",
    "service desk", "1st line", "2nd line",
    "software engineer", "software developer", "backend", "back-end",
    "full stack", "fullstack", "devops", "cloud engineer",
    "systems administrator", "sysadmin", "network engineer",
    "graduate", "junior", "entry level", "entry-level", "associate engineer",
    "python developer", "python engineer",
}

SOURCE_NAME = "ai_careers"


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class AICareersScaper:
    """Scrapes 100 company career pages using Jina AI + GPT-4o-mini."""

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY not set. Add it to .env before running."
            )
        self.openai = AsyncOpenAI(api_key=api_key)
        self._companies = KNOWN_COMPANIES  # list[tuple[name, url]] from career_checker

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run_all(self) -> list[dict]:
        """
        Scrape all companies concurrently (CONCURRENCY limit).
        Returns a flat list of job dicts ready for upsert_jobs().
        """
        semaphore  = asyncio.Semaphore(CONCURRENCY)
        all_jobs:  list[dict] = []
        ok = failed = skipped = 0

        logger.info(
            f"[AI] Starting AI careers scraper — {len(self._companies)} companies"
        )
        emitter.emit("source_start", source=SOURCE_NAME,
                     company_count=len(self._companies))

        async with httpx.AsyncClient(
            timeout=JINA_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
        ) as http_client:
            tasks = [
                self._scrape_company(http_client, semaphore, name, url)
                for name, url in self._companies
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for (name, _), result in zip(self._companies, results):
            if isinstance(result, Exception):
                logger.warning(f"[AI] {name}: exception — {result}")
                failed += 1
            elif result:
                all_jobs.extend(result)
                ok += 1
                logger.info(f"[AI] {name}: {len(result)} job(s) found")
            else:
                skipped += 1

        logger.info(
            f"[AI] Done — {ok} companies with jobs, "
            f"{skipped} with 0 matches, {failed} errors. "
            f"Total jobs: {len(all_jobs)}"
        )
        emitter.emit("source_done", source=SOURCE_NAME, job_count=len(all_jobs))
        return all_jobs

    # ------------------------------------------------------------------
    # Per-company pipeline
    # ------------------------------------------------------------------

    async def _scrape_company(
        self,
        http_client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        company: str,
        careers_url: str,
    ) -> list[dict]:
        async with semaphore:
            await asyncio.sleep(REQUEST_DELAY)
            emitter.emit("company_start", company=company, url=careers_url)

            markdown = await self._fetch_markdown(http_client, careers_url)
            if not markdown:
                logger.debug(f"[AI] {company}: no content fetched")
                emitter.emit("company_done", company=company, job_count=0)
                return []

            jobs     = await self._extract_jobs(company, careers_url, markdown)
            validated = self._validate_and_tag(jobs, company, careers_url)
            emitter.emit("company_done", company=company, job_count=len(validated))
            return validated

    # ------------------------------------------------------------------
    # Step 1 — Fetch page as Markdown via Jina AI
    # ------------------------------------------------------------------

    async def _fetch_markdown(
        self,
        http_client: httpx.AsyncClient,
        url: str,
    ) -> Optional[str]:
        """
        Try Jina AI reader first (converts any page to clean Markdown).
        Fall back to plain httpx GET if Jina returns thin content.
        """
        # --- Jina AI reader ---
        jina_url = JINA_BASE + url
        try:
            resp = await http_client.get(jina_url)
            if resp.status_code == 200:
                text = resp.text.strip()
                if len(text) >= MIN_CONTENT_LEN:
                    logger.debug(f"[AI] Jina OK ({len(text)} chars): {url}")
                    return text[:MAX_MARKDOWN_LEN]
                logger.debug(f"[AI] Jina thin content ({len(text)} chars): {url}")
        except Exception as exc:
            logger.debug(f"[AI] Jina failed for {url}: {exc}")

        # --- Fallback: direct GET + strip HTML tags ---
        try:
            resp = await http_client.get(url)
            if resp.status_code < 400:
                import re
                text = re.sub(r"<[^>]+>", " ", resp.text)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) >= MIN_CONTENT_LEN:
                    logger.debug(f"[AI] Direct fetch OK ({len(text)} chars): {url}")
                    return text[:MAX_MARKDOWN_LEN]
        except Exception as exc:
            logger.debug(f"[AI] Direct fetch failed for {url}: {exc}")

        return None

    # ------------------------------------------------------------------
    # Step 2 — Extract jobs via GPT-4o-mini
    # ------------------------------------------------------------------

    async def _extract_jobs(
        self,
        company: str,
        careers_url: str,
        markdown: str,
    ) -> list[dict]:
        """Send page content to GPT-4o-mini and parse the JSON response."""

        prompt = f"""You are a job listing extractor. Analyse the following careers page content from {company} and extract ALL open job listings that match the target roles below.

TARGET ROLES (extract any job whose title matches):
- Java Developer, Spring Developer, Kotlin Developer
- Software Engineer, Software Developer, Backend Developer, Full Stack Developer
- Python Developer, Python Engineer
- Cybersecurity Analyst, Security Engineer, SOC Analyst, Penetration Tester, InfoSec Engineer, Security Operations
- IT Support, Helpdesk, Help Desk, Service Desk, Technical Support, Desktop Support, 1st Line Support, 2nd Line Support
- Network Engineer, Systems Administrator, Cloud Engineer, DevOps Engineer, Site Reliability Engineer
- Graduate Engineer, Junior Developer, Associate Engineer, Entry-Level Engineer (any tech discipline)

RULES:
- Extract ALL matching roles regardless of location (the company is Ireland-based so roles may be listed as Dublin, Ireland, or Global/Remote)
- Skip senior leadership (Director, VP, SVP, C-suite, Managing Director) UNLESS the title also contains "Graduate" or "Junior"
- If a job has no URL, use "{careers_url}"

Return a JSON object with a single "jobs" key containing an array.
Each element must have EXACTLY these keys:
  "title"       : string  — exact job title from the page
  "company"     : string  — always "{company}"
  "url"         : string  — direct apply/listing URL, or "{careers_url}"
  "salary"      : string  — salary range if shown, else ""
  "date_posted" : string  — date if shown (any format), else ""

If no matching jobs are found, return {{"jobs": []}}

Return ONLY valid JSON. No explanations, no markdown, no extra text.

--- CAREERS PAGE CONTENT ---
{markdown}
"""

        try:
            response = await self.openai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=2_000,
            )
            raw = response.choices[0].message.content
            data = json.loads(raw)
            jobs = data.get("jobs", [])
            if not isinstance(jobs, list):
                return []
            return jobs
        except json.JSONDecodeError as exc:
            logger.warning(f"[AI] {company}: JSON parse error — {exc}")
        except Exception as exc:
            logger.warning(f"[AI] {company}: OpenAI call failed — {exc}")
        return []

    # ------------------------------------------------------------------
    # Step 3 — Validate & tag jobs
    # ------------------------------------------------------------------

    def _validate_and_tag(
        self,
        raw_jobs: list[dict],
        company: str,
        careers_url: str,
    ) -> list[dict]:
        """
        Filter hallucinations and tag each job with metadata
        compatible with run_all.py / upsert_jobs().
        """
        out: list[dict] = []
        for job in raw_jobs:
            title = (job.get("title") or "").strip()
            if not title or len(title) < 4:
                continue  # skip empty / too-short titles

            # Keyword relevance guard (GPT sometimes drifts)
            title_lower = title.lower()
            if not any(kw in title_lower for kw in TARGET_KEYWORDS):
                logger.debug(f"[AI] Filtered irrelevant: {title!r} @ {company}")
                emitter.emit(
                    "job_filtered",
                    title=title, company=company, source=SOURCE_NAME,
                    reason="title keyword mismatch",
                )
                continue

            tagged = {
                "title":       title,
                "company":     (job.get("company") or company).strip(),
                "url":         (job.get("url")     or careers_url).strip(),
                "salary":      (job.get("salary")  or "").strip() or None,
                "date_posted": (job.get("date_posted") or "").strip() or "",
                "source":      SOURCE_NAME,
                "search_term": "ai_careers_scan",
            }
            emitter.emit(
                "job_accepted",
                title=tagged["title"], company=tagged["company"],
                source=SOURCE_NAME,   url=tagged["url"],
            )
            out.append(tagged)
        return out
