"""
Career page checker — Signal 1 of the legitimacy scorer.

Given a company name, this module:
  1. Resolves the company's website (known-companies map, then Google-style heuristic)
  2. Finds the careers / jobs page URL
  3. Scrapes that page for job titles
  4. Returns True if the job title (normalised) appears on the company's own site

The check is best-effort: if the company site is inaccessible or not found the
signal is simply skipped (0 points), never raised as an error.
"""
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known companies → careers page (expand as you encounter more)
# ---------------------------------------------------------------------------

_KNOWN_CAREERS: dict[str, str] = {
    "accenture":          "https://www.accenture.com/ie-en/careers/jobsearch",
    "amazon":             "https://www.amazon.jobs/en/search?country%5B%5D=IRL",
    "apple":              "https://jobs.apple.com/en-us/search?location=ireland-IRL",
    "bank of ireland":    "https://careers.bankofireland.com/jobs",
    "citi":               "https://jobs.citi.com/search-jobs/Dublin",
    "deloitte":           "https://apply.deloitte.com/careers/SearchJobs/?3_56_3=1890",
    "dell":               "https://jobs.dell.com/search-jobs/Ireland",
    "dxc technology":     "https://dxc.wd1.myworkdayjobs.com/DXC_Jobs",
    "ey":                 "https://careers.ey.com/ey/jobs?country=ireland",
    "google":             "https://careers.google.com/jobs/results/?location=Ireland",
    "hpe":                "https://careers.hpe.com/us/en/search-results?keywords=&location=Ireland",
    "ibm":                "https://www.ibm.com/employment/search.html?country=IE",
    "intel":              "https://jobs.intel.com/en/search#q=&location=Ireland",
    "kpmg":               "https://home.kpmg/ie/en/home/careers.html",
    "mastercard":         "https://careers.mastercard.com/us/en/search-results?location=Ireland",
    "microsoft":          "https://jobs.microsoft.com/en-us/search?l=Dublin%2C+Ireland",
    "oracle":             "https://careers.oracle.com/jobs/#en/sites/jobsearch/requisitions?keyword=&location=Ireland",
    "pwc":                "https://www.pwc.ie/careers/search-jobs.html",
    "salesforce":         "https://careers.salesforce.com/jobs#location=Ireland",
    "sap":                "https://jobs.sap.com/search/?q=&location=Ireland",
    "tcs":                "https://ibegin.tcs.com/iBegin/jobs/search",
    "unum":               "https://jobs.unum.com/search/?q=&locationsearch=Ireland",
    "workday":            "https://workday.wd5.myworkdayjobs.com/en-US/Workday",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TITLE_NOISE = re.compile(
    r"\b(junior|senior|lead|principal|staff|associate|graduate|mid[\s\-]?level)\b",
    re.IGNORECASE,
)


def _normalise_title(title: str) -> str:
    """Strip seniority words and reduce to a lowercase token set."""
    t = _TITLE_NOISE.sub("", title).lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return " ".join(t.split())


def _company_slug(company: str) -> str:
    """Best-guess domain slug from company name."""
    c = company.lower().strip()
    c = re.sub(r"\b(ltd\.?|limited|plc\.?|inc\.?|llc\.?|group|ireland|dublin)\b\.?", "", c)
    c = re.sub(r"[^a-z0-9]", "", c)
    return c


def _candidate_urls(company: str) -> list[str]:
    """Return a short list of candidate careers-page URLs to probe."""
    slug = _company_slug(company)
    if not slug:
        return []
    return [
        f"https://www.{slug}.com/careers",
        f"https://www.{slug}.com/jobs",
        f"https://careers.{slug}.com",
        f"https://jobs.{slug}.com",
        f"https://www.{slug}.ie/careers",
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_careers_url(company: str) -> Optional[str]:
    """
    Return the careers URL for *company*, or None if undiscoverable.
    Uses the known map first, then probes heuristic domains.
    """
    key = company.lower().strip()
    # Exact or partial match in known map
    for k, url in _KNOWN_CAREERS.items():
        if k in key or key in k:
            return url

    # Probe candidate URLs with a HEAD request
    candidates = _candidate_urls(company)
    for url in candidates:
        try:
            r = httpx.head(url, follow_redirects=True, timeout=6)
            if r.status_code < 400:
                logger.debug(f"  career URL probe hit: {url}")
                return url
        except Exception:
            pass

    return None


def jobs_on_career_page(careers_url: str, timeout: int = 12) -> list[str]:
    """
    Fetch *careers_url* (simple GET, no JS rendering) and return a list of
    normalised job titles found in the page text.

    This is a heuristic extraction — it looks for common patterns rather than
    site-specific selectors so it works across many company sites without
    bespoke scrapers.
    """
    try:
        r = httpx.get(careers_url, follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"})
        if r.status_code >= 400:
            return []
    except Exception as exc:
        logger.debug(f"  career page fetch failed: {exc}")
        return []

    text = r.text
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    # Common patterns that appear near job titles on careers pages
    # e.g. "Software Engineer", "Java Developer", "IT Support Analyst"
    _JOB_PATTERN = re.compile(
        r"\b((?:junior|senior|lead|principal|staff|associate|graduate)?\s*"
        r"(?:[A-Z][a-z]+ ){1,3}"
        r"(?:engineer|developer|analyst|consultant|specialist|manager|administrator|"
        r"architect|designer|scientist|officer|director|coordinator|technician))\b"
    )
    titles = [_normalise_title(m.group(0)) for m in _JOB_PATTERN.finditer(text)]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in titles:
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def title_matches_career_page(job_title: str, company: str) -> bool:
    """
    Return True if *job_title* appears (approximately) on the company's
    own careers page.  False on any failure.
    """
    url = find_careers_url(company)
    if not url:
        logger.debug(f"  no careers URL found for: {company!r}")
        return False

    page_titles = jobs_on_career_page(url)
    if not page_titles:
        logger.debug(f"  no titles extracted from: {url}")
        return False

    needle = _normalise_title(job_title)
    needle_tokens = set(needle.split())

    for t in page_titles:
        haystack_tokens = set(t.split())
        # Match if ≥ 2 meaningful tokens overlap (or full title contained)
        overlap = needle_tokens & haystack_tokens
        meaningful = overlap - {"and", "the", "a", "of", "in", "for"}
        if len(meaningful) >= 2 or needle in t or t in needle:
            logger.debug(f"  career page match: {needle!r} ~ {t!r}")
            return True

    logger.debug(f"  no career page match for {job_title!r} at {company!r}")
    return False
