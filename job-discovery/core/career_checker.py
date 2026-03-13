"""
career_checker.py  —  Signal 1 of the legitimacy scorer.

Architecture (5-layer fallback):
  Layer 1: Hardcoded known careers URLs (instant, no network)
  Layer 2: DuckDuckGo search  "company careers site:company.com/jobs OR site:greenhouse.io"
  Layer 3: ATS platform detection  (Workday JSON API, Greenhouse, Lever, SmartRecruiters, etc.)
  Layer 4: Blind URL probing  (www.company.com/careers, jobs.company.com, ...)
  Layer 5: Graceful failure — return False, never crash

This version is production-ready for Irish/EU companies including SMEs,
companies on modern ATS platforms, and companies with .ie domains.
"""
import logging
import re
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Layer 1: Known careers URLs
# ─────────────────────────────────────────────
_KNOWN_CAREERS: dict[str, str] = {
    "accenture":          "https://www.accenture.com/ie-en/careers",
    "amazon":             "https://www.amazon.jobs/en/locations/dublin-ireland",
    "apple":              "https://jobs.apple.com/en-us/search?location=dublin-IRE",
    "bank of ireland":    "https://careers.bankofireland.com",
    "citi":               "https://jobs.citi.com/search?location=Dublin",
    "deloitte":           "https://jobs2.deloitte.com/ie/en",
    "dell":               "https://jobs.dell.com/en/ireland",
    "dxc technology":     "https://dxc.com/us/en/careers",
    "ey":                 "https://careers.ey.com/ey/jobs",
    "google":             "https://careers.google.com/jobs/results/?location=Dublin%2C+Ireland",
    "hpe":                "https://careers.hpe.com/us/en/search-results?keywords=ireland",
    "ibm":                "https://www.ibm.com/careers/search?field_keyword_08=Dublin",
    "intel":              "https://jobs.intel.com/en/search#q=&location=Ireland",
    "kpmg":               "https://jobs.kpmg.ie",
    "mastercard":         "https://careers.mastercard.com/us/en/search-results?keywords=dublin",
    "microsoft":          "https://jobs.careers.microsoft.com/global/en/search?lc=Dublin%2C+Ireland",
    "oracle":             "https://careers.oracle.com/jobs/#en/sites/jobsearch/requisitions?keyword=ireland",
    "pwc":                "https://www.pwc.ie/careers.html",
    "salesforce":         "https://careers.salesforce.com/en/jobs/?location_name=Dublin",
    "sap":                "https://jobs.sap.com/search/?q=&locname=Dublin%2C+Ireland",
    "tcs":                "https://ibegin.tcs.com/iBegin/jobs/search.do",
    "unum":               "https://jobs.unum.com/search?q=ireland",
    "workday":            "https://www.workday.com/en-us/company/careers/open-positions.html",
    # Irish-specific
    "aib":                "https://aib.ie/careers",
    "esb":                "https://esb.ie/careers",
    "eir":                "https://eir.ie/about/careers/",
    "vodafone ireland":   "https://careers.vodafone.ie",
    "irish life":         "https://irishlife.ie/about-us/careers/",
    "aldi ireland":       "https://careers.aldi.ie",
    "lidl ireland":       "https://careers.lidl.ie",
    "three ireland":      "https://www.three.ie/careers",
    "fiserv":             "https://careers.fiserv.com",
    "stripe":             "https://stripe.com/jobs/search?location=Dublin",
    "hubspot":            "https://www.hubspot.com/jobs/search?q=dublin",
    "zendesk":            "https://jobs.zendesk.com/us/en/ireland",
    "intercom":           "https://www.intercom.com/careers",
    "meta":               "https://www.metacareers.com/jobs?offices%5B0%5D=Dublin%2C%20Ireland",
    "linkedin":           "https://careers.linkedin.com/",
    "twitter":            "https://careers.twitter.com/en/jobs.html",
    "tiktok":             "https://careers.tiktok.com/position?keywords=ireland",
    "palantir":           "https://jobs.lever.co/palantir",
}

# ATS platform patterns — detect from careers page URL or HTML
_ATS_PATTERNS = [
    # (pattern_in_url_or_html, api_url_template, ats_name)
    (r"greenhouse\.io",         "https://boards.greenhouse.io/embed/job_board?for={slug}", "greenhouse"),
    (r"lever\.co",              "https://jobs.lever.co/{slug}", "lever"),
    (r"workday\.com",           None, "workday"),   # handled separately
    (r"smartrecruiters\.com",   "https://careers.smartrecruiters.com/{slug}", "smartrecruiters"),
    (r"taleo\.net",             None, "taleo"),
    (r"myworkdayjobs\.com",     None, "workday"),
    (r"icims\.com",             None, "icims"),
    (r"bamboohr\.com",          "https://{slug}.bamboohr.com/careers", "bamboohr"),
    (r"recruitee\.com",         "https://recruitee.com/o/{slug}", "recruitee"),
    (r"ashbyhq\.com",           "https://jobs.ashbyhq.com/{slug}", "ashby"),
    (r"personio\.com",          None, "personio"),
]

# ─────────────────────────────────────────────
#  Normalisation helpers
# ─────────────────────────────────────────────
_SENIORITY_WORDS = re.compile(
    r"\b(junior|senior|lead|principal|staff|associate|graduate|mid[-\s]level|"
    r"entry[-\s]level|sr|jr)\b",
    re.IGNORECASE,
)

_COMPANY_NOISE = re.compile(
    r"\b(ltd|limited|plc|inc|llc|corp|group|ireland|dublin|"
    r"technologies|technology|solutions|services|consulting|international|"
    r"global|europe|emea)\b",
    re.IGNORECASE,
)

# Job title endings to look for on careers pages
_TITLE_ENDINGS = (
    r"engineer|developer|analyst|consultant|specialist|manager|administrator|"
    r"architect|designer|scientist|officer|director|coordinator|technician|"
    r"devops|sre|support|helpdesk|tester|qa|security|network"
)

_HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(8.0),
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"},
    follow_redirects=True,
)


def _normalise_title(title: str) -> str:
    t = _SENIORITY_WORDS.sub("", title.lower())
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _company_slug(company: str) -> str:
    c = _COMPANY_NOISE.sub("", company.lower())
    c = re.sub(r"[^a-z0-9]", "", c)
    return c.strip()


def _candidate_urls(company: str) -> list[str]:
    slug = _company_slug(company)
    if not slug:
        return []
    return [
        f"https://www.{slug}.com/careers",
        f"https://www.{slug}.com/jobs",
        f"https://careers.{slug}.com",
        f"https://jobs.{slug}.com",
        f"https://www.{slug}.ie/careers",
        f"https://www.{slug}.ie/jobs",
        f"https://{slug}.com/careers",
        f"https://careers.{slug}.ie",
    ]


# ─────────────────────────────────────────────
#  Layer 2: DuckDuckGo search
# ─────────────────────────────────────────────

def _ddg_search_careers(company: str) -> Optional[str]:
    """
    Search DuckDuckGo for the company's careers page.
    Returns the most likely careers URL or None.
    """
    try:
        slug = _company_slug(company)
        query = f"{company} careers jobs site ireland OR site:{slug}.com/careers"
        params = urlencode({"q": query, "format": "json", "no_redirect": "1", "no_html": "1"})
        resp = _HTTP_CLIENT.get(
            f"https://api.duckduckgo.com/?{params}",
            timeout=6.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Check AbstractURL and RelatedTopics
            if data.get("AbstractURL"):
                return data["AbstractURL"]
            for topic in data.get("RelatedTopics", [])[:5]:
                url = topic.get("FirstURL", "")
                if any(kw in url.lower() for kw in ["career", "job", "work"]):
                    return url
    except Exception as exc:
        logger.debug("DDG search failed for %r: %s", company, exc)
    return None


# ─────────────────────────────────────────────
#  Layer 3: ATS platform detection
# ─────────────────────────────────────────────

def _detect_ats(careers_url: str, html: str) -> Optional[str]:
    """
    If the page uses a known ATS, return the actual job listing URL.
    Workday uses a separate JSON API.
    """
    combined = careers_url + " " + html[:2000]
    for pattern, api_template, ats_name in _ATS_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            logger.debug("ATS detected: %s on %s", ats_name, careers_url)
            if ats_name == "workday":
                return _workday_jobs_url(careers_url)
            return careers_url  # use original URL for HTML scraping
    return None


def _workday_jobs_url(url: str) -> Optional[str]:
    """
    Workday exposes a JSON API at:
    https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
    Try to extract tenant from URL.
    """
    m = re.search(r"https?://([^.]+)\.myworkdayjobs\.com", url)
    if not m:
        return url  # return original URL
    tenant = m.group(1)
    # Workday JSON endpoint
    board_match = re.search(r"myworkdayjobs\.com/([^/?]+)", url)
    board = board_match.group(1) if board_match else tenant
    return f"https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"


def _fetch_workday_titles(api_url: str) -> list[str]:
    """Fetch job titles from Workday's undocumented JSON API."""
    try:
        payload = {"limit": 20, "offset": 0, "searchText": ""}
        resp = _HTTP_CLIENT.post(
            api_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=8.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            jobs = data.get("jobPostings", []) or data.get("jobs", [])
            return [_normalise_title(j.get("title", "")) for j in jobs if j.get("title")]
    except Exception as exc:
        logger.debug("Workday API error: %s", exc)
    return []


# ─────────────────────────────────────────────
#  Page fetching and title extraction
# ─────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Remove HTML tags, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def find_careers_url(company: str) -> Optional[str]:
    """
    Multi-layer careers URL discovery.
    Returns the best careers URL found, or None.
    """
    # Layer 1: hardcoded known map (partial name match)
    company_lower = company.lower()
    for known_name, url in _KNOWN_CAREERS.items():
        if known_name in company_lower or company_lower in known_name:
            logger.debug("Layer 1 (known): %s → %s", company, url)
            return url

    # Layer 4: blind URL probing (do this before DDG to avoid rate limits)
    for candidate in _candidate_urls(company):
        try:
            resp = _HTTP_CLIENT.head(candidate, timeout=6.0)
            if resp.status_code < 400:
                logger.debug("Layer 4 (probe): %s → %s", company, candidate)
                return candidate
        except Exception:
            pass
        time.sleep(0.2)

    # Layer 2: DuckDuckGo search
    ddg_url = _ddg_search_careers(company)
    if ddg_url:
        logger.debug("Layer 2 (DDG): %s → %s", company, ddg_url)
        return ddg_url

    logger.debug("No careers URL found for %r", company)
    return None


def jobs_on_career_page(careers_url: str) -> list[str]:
    """
    Fetch the careers page and return a list of normalised job titles found.
    Handles both regular HTML and Workday JSON API.
    """
    try:
        # Workday JSON path
        if "myworkdayjobs.com/wday/cxs" in careers_url:
            return _fetch_workday_titles(careers_url)

        resp = _HTTP_CLIENT.get(careers_url, timeout=8.0)
        if resp.status_code >= 400:
            return []

        html = resp.text

        # Check for ATS redirect
        ats_url = _detect_ats(careers_url, html)
        if ats_url and ats_url != careers_url:
            if "myworkdayjobs.com/wday/cxs" in ats_url:
                return _fetch_workday_titles(ats_url)
            # Re-fetch from ATS URL
            try:
                resp2 = _HTTP_CLIENT.get(ats_url, timeout=8.0)
                if resp2.status_code < 400:
                    html = resp2.text
            except Exception:
                pass  # use original HTML

        text = _strip_html(html)
        title_re = re.compile(
            r"\b[\w\s\-/]+?\s(?:" + _TITLE_ENDINGS + r")\b",
            re.IGNORECASE,
        )
        raw_titles = title_re.findall(text)
        return list({_normalise_title(t) for t in raw_titles if len(t.split()) <= 8})

    except Exception as exc:
        logger.debug("jobs_on_career_page error for %s: %s", careers_url, exc)
        return []


def title_matches_career_page(job_title: str, company: str) -> bool:
    """
    Return True if this job title plausibly appears on the company's careers page.
    Never raises — always returns False on any error.
    """
    try:
        careers_url = find_careers_url(company)
        if not careers_url:
            return False

        page_titles = jobs_on_career_page(careers_url)
        if not page_titles:
            return False

        needle = _normalise_title(job_title)
        needle_tokens = set(needle.split())

        # Need at least 2 non-trivial tokens to match
        stop_words = {"a", "an", "the", "and", "or", "for", "in", "at", "of", "to", "with"}
        meaningful_needle = needle_tokens - stop_words
        if len(meaningful_needle) < 2:
            return False

        for page_title in page_titles:
            page_tokens = set(page_title.split()) - stop_words
            # Substring match
            if needle in page_title or page_title in needle:
                return True
            # Token overlap ≥ 2
            if len(meaningful_needle & page_tokens) >= 2:
                return True

        return False

    except Exception as exc:
        logger.debug("title_matches_career_page failed: %s", exc)
        return False
