"""
Career page checker — Signal 1 of the legitimacy scorer.

Given a company name, this module:
  1. Resolves the company's website (known-companies map, then Google-style heuristic)
  2. Finds the careers / jobs page URL
  3. Scrapes that page for job titles
  4. Returns True if the job title (normalised) appears on the company's own site

The check is best-effort: if the company site is inaccessible or not found the
signal is simply skipped (0 points), never raised as an error.

100 companies covering Dublin/Ireland tech, finance, consulting, cybersecurity,
pharma/healthtech, insurance, and local Irish firms.
"""
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known companies -> careers page  (100+ entries)
# ---------------------------------------------------------------------------

_KNOWN_CAREERS: dict[str, str] = {

    # -- Big Tech --------------------------------------------------------------
    "google":               "https://careers.google.com/jobs/results/?location=Ireland",
    "meta":                 "https://www.metacareers.com/jobs?offices[0]=Dublin%2C+Ireland",
    "facebook":             "https://www.metacareers.com/jobs?offices[0]=Dublin%2C+Ireland",
    "microsoft":            "https://jobs.microsoft.com/en-us/search?l=Dublin%2C+Ireland",
    "apple":                "https://jobs.apple.com/en-us/search?location=ireland-IRL",
    "amazon":               "https://www.amazon.jobs/en/search?country%5B%5D=IRL",
    "aws":                  "https://www.amazon.jobs/en/search?country%5B%5D=IRL",
    "linkedin":             "https://careers.linkedin.com/jobs",
    "stripe":               "https://stripe.com/jobs/search?l=dublin",
    "paypal":               "https://jobsearch.paypal-corp.com/en-US/jobs?keywords=&location=Dublin",
    "airbnb":               "https://careers.airbnb.com/positions/",
    "tiktok":               "https://careers.tiktok.com/position?location=CT_234",
    "bytedance":            "https://jobs.bytedance.com/en/position?keywords=dublin",
    "twitter":              "https://careers.x.com/en",
    "x corp":               "https://careers.x.com/en",

    # -- Finance / Fintech -----------------------------------------------------
    "jp morgan":            "https://www.jpmorgan.com/IE/en/about/careers",
    "jpmorgan":             "https://www.jpmorgan.com/IE/en/about/careers",
    "j.p. morgan":          "https://www.jpmorgan.com/IE/en/about/careers",
    "citi":                 "https://jobs.citi.com/search-jobs/Dublin",
    "citibank":             "https://jobs.citi.com/search-jobs/Dublin",
    "bank of ireland":      "https://careers.bankofireland.com/jobs",
    "aib":                  "https://aib.ie/careers",
    "allied irish banks":   "https://aib.ie/careers",
    "mastercard":           "https://careers.mastercard.com/us/en/search-results?location=Ireland",
    "visa":                 "https://jobs.smartrecruiters.com/Visa/search?q=&location=Dublin",
    "bny mellon":           "https://bnymellon.eightfold.ai/careers?query=ireland",
    "northern trust":       "https://careers.northerntrust.com/jobs",
    "state street":         "https://careers.statestreet.com/global/en/home",
    "fidelity":             "https://jobs.fidelity.com/search-jobs/Ireland/185",
    "coinbase":             "https://www.coinbase.com/careers/positions",
    "revolut":              "https://www.revolut.com/careers",
    "square":               "https://block.xyz/careers",
    "block":                "https://block.xyz/careers",
    "hsbc":                 "https://www.hsbc.com/careers",

    # -- Consulting / Professional Services ------------------------------------
    "accenture":            "https://www.accenture.com/ie-en/careers/jobsearch",
    "deloitte":             "https://apply.deloitte.com/careers/SearchJobs/?3_56_3=1890",
    "pwc":                  "https://www.pwc.ie/careers/search-jobs.html",
    "pricewaterhousecoopers": "https://www.pwc.ie/careers/search-jobs.html",
    "ey":                   "https://careers.ey.com/ey/jobs?country=ireland",
    "ernst young":          "https://careers.ey.com/ey/jobs?country=ireland",
    "kpmg":                 "https://home.kpmg/ie/en/home/careers.html",
    "capgemini":            "https://www.capgemini.com/ie-en/careers/job-search/",
    "ibm":                  "https://www.ibm.com/employment/search.html?country=IE",
    "cognizant":            "https://careers.cognizant.com/global/en",
    "infosys":              "https://careers.infosys.com/joblist?region=Ireland",
    "wipro":                "https://careers.wipro.com/careers-home/jobs",
    "tcs":                  "https://ibegin.tcs.com/iBegin/jobs/search",
    "dxc technology":       "https://dxc.wd1.myworkdayjobs.com/DXC_Jobs",
    "dxc":                  "https://dxc.wd1.myworkdayjobs.com/DXC_Jobs",

    # -- Enterprise Software / Hardware ----------------------------------------
    "oracle":               "https://careers.oracle.com/jobs/#en/sites/jobsearch/requisitions?keyword=&location=Ireland",
    "sap":                  "https://jobs.sap.com/search/?q=&location=Ireland",
    "salesforce":           "https://careers.salesforce.com/jobs#location=Ireland",
    "workday":              "https://workday.wd5.myworkdayjobs.com/en-US/Workday",
    "dell":                 "https://jobs.dell.com/search-jobs/Ireland",
    "dell technologies":    "https://jobs.dell.com/search-jobs/Ireland",
    "hpe":                  "https://careers.hpe.com/us/en/search-results?keywords=&location=Ireland",
    "hewlett packard":      "https://careers.hpe.com/us/en/search-results?keywords=&location=Ireland",
    "intel":                "https://jobs.intel.com/en/search#q=&location=Ireland",
    "cisco":                "https://jobs.cisco.com/jobs/SearchJobs/?21178=233",
    "ericsson":             "https://jobs.ericsson.com/en_US/jobs?location=Ireland",
    "vmware":               "https://careers.vmware.com/jobs",
    "broadcom":             "https://careers.broadcom.com/jobs",
    "qualcomm":             "https://careers.qualcomm.com/careers",
    "unum":                 "https://jobs.unum.com/search/?q=&locationsearch=Ireland",

    # -- Cybersecurity ---------------------------------------------------------
    "palo alto networks":   "https://careers.paloaltonetworks.com/jobs",
    "palo alto":            "https://careers.paloaltonetworks.com/jobs",
    "crowdstrike":          "https://crowdstrike.wd5.myworkdayjobs.com/en-US/crowdstrikecareers",
    "sophos":               "https://www.sophos.com/en-us/about/jobs",
    "proofpoint":           "https://proofpoint.wd5.myworkdayjobs.com/en-US/ProofpointCareers",
    "rapid7":               "https://www.rapid7.com/about/careers/",
    "integrity360":         "https://www.integrity360.com/careers",
    "trellix":              "https://careers.trellix.com/",
    "mcafee":               "https://careers.trellix.com/",
    "tenable":              "https://careers.tenable.com/jobs",
    "arctic wolf":          "https://arcticwolf.com/careers/",
    "bt":                   "https://careers.bt.com/go/View-All-Technology-Jobs/5089501/",
    "bt security":          "https://careers.bt.com/go/View-All-Technology-Jobs/5089501/",
    "forcepoint":           "https://www.forcepoint.com/careers",
    "recorded future":      "https://www.recordedfuture.com/careers",

    # -- Irish / Local Tech ----------------------------------------------------
    "version 1":            "https://www.version1.com/careers/",
    "ward solutions":       "https://ward.ie/careers/",
    "ergo":                 "https://ergogroup.ie/careers/",
    "auxilion":             "https://www.auxilion.com/careers",
    "trilogy technologies": "https://trilogytechnologies.com/careers/",
    "dataplex":             "https://dataplex.ie/careers/",
    "fexco":                "https://fexco.com/careers",
    "three ireland":        "https://www.three.ie/careers/",
    "three":                "https://www.three.ie/careers/",
    "eir":                  "https://eir.ie/careers/",
    "vodafone":             "https://careers.vodafone.ie/",
    "vodafone ireland":     "https://careers.vodafone.ie/",
    "atos":                 "https://atos.net/en/careers",
    "ntt data":             "https://careers.nttdata.com/global/en",
    "fujitsu":              "https://www.fujitsu.com/ie/about/careers/",

    # -- Cloud / SaaS ----------------------------------------------------------
    "hubspot":              "https://www.hubspot.com/careers",
    "zendesk":              "https://jobs.zendesk.com/",
    "mongodb":              "https://www.mongodb.com/careers",
    "atlassian":            "https://www.atlassian.com/company/careers/all-jobs",
    "dynatrace":            "https://careers.dynatrace.com/",
    "snowflake":            "https://careers.snowflake.com/us/en",
    "twilio":               "https://www.twilio.com/en-us/company/jobs",
    "docusign":             "https://careers.docusign.com/",
    "intercom":             "https://www.intercom.com/careers",
    "servicenow":           "https://careers.servicenow.com/jobs",
    "qualtrics":            "https://careers.qualtrics.com/jobs",
    "dropbox":              "https://jobs.dropbox.com/",
    "spotify":              "https://jobs.spotify.com/",
    "genesys":              "https://jobs.genesys.com/jobs",
    "amdocs":               "https://careers.amdocs.com/jobs",

    # -- Pharma / Healthcare IT ------------------------------------------------
    "johnson johnson":      "https://jobs.jnj.com/jobs",
    "j&j":                  "https://jobs.jnj.com/jobs",
    "pfizer":               "https://www.pfizer.com/about/careers",
    "abbvie":               "https://careers.abbvie.com/en/",
    "msd":                  "https://jobs.msd.com/",
    "merck":                "https://jobs.msd.com/",
    "roche":                "https://careers.roche.com/global/en/jobs",
    "regeneron":            "https://careers.regeneron.com/jobs",
    "boston scientific":    "https://jobs.bostonscientific.com/jobs",
    "medtronic":            "https://jobs.medtronic.com/jobs",
    "abbott":               "https://www.jobs.abbott/us/en",
    "stryker":              "https://careers.stryker.com/jobs",
    "optum":                "https://careers.unitedhealthgroup.com/job-search-results/?keyword=&location=Dublin",

    # -- Insurance / Additional Finance ----------------------------------------
    "axa":                  "https://careers.axa.com/jobs",
    "allianz":              "https://careers.allianz.com/",
    "zurich":               "https://www.zurichna.com/careers",
    "aviva":                "https://careers.aviva.com/jobs",
    "irish life":           "https://irishlife.ie/careers/",
    "liberty it":           "https://jobs.libertymutualgroup.com/jobs",
    "liberty mutual":       "https://jobs.libertymutualgroup.com/jobs",
    "ss&c technologies":    "https://www.ssctech.com/careers",
    "ss&c":                 "https://www.ssctech.com/careers",
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
    for k, url in _KNOWN_CAREERS.items():
        if k in key or key in k:
            return url

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
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    _JOB_PATTERN = re.compile(
        r"\b((?:junior|senior|lead|principal|staff|associate|graduate)?\s*"
        r"(?:[A-Z][a-z]+ ){1,3}"
        r"(?:engineer|developer|analyst|consultant|specialist|manager|administrator|"
        r"architect|designer|scientist|officer|director|coordinator|technician))\b"
    )
    titles = [_normalise_title(m.group(0)) for m in _JOB_PATTERN.finditer(text)]
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
        overlap = needle_tokens & haystack_tokens
        meaningful = overlap - {"and", "the", "a", "of", "in", "for"}
        if len(meaningful) >= 2 or needle in t or t in needle:
            logger.debug(f"  career page match: {needle!r} ~ {t!r}")
            return True

    logger.debug(f"  no career page match for {job_title!r} at {company!r}")
    return False


# ---------------------------------------------------------------------------
# KNOWN_COMPANIES list — used by AI careers scraper
# ---------------------------------------------------------------------------

KNOWN_COMPANIES: list[tuple[str, str]] = [
    # -- Big Tech --
    ("Google",                "https://careers.google.com/jobs/results/?location=Ireland"),
    ("Meta",                  "https://www.metacareers.com/jobs?offices[0]=Dublin%2C+Ireland"),
    ("Microsoft",             "https://jobs.microsoft.com/en-us/search?l=Dublin%2C+Ireland"),
    ("Apple",                 "https://jobs.apple.com/en-us/search?location=ireland-IRL"),
    ("Amazon",                "https://www.amazon.jobs/en/search?country%5B%5D=IRL"),
    ("LinkedIn",              "https://careers.linkedin.com/jobs"),
    ("Stripe",                "https://stripe.com/jobs/search?l=dublin"),
    ("PayPal",                "https://jobsearch.paypal-corp.com/en-US/jobs?keywords=&location=Dublin"),
    ("TikTok",                "https://careers.tiktok.com/position?location=CT_234"),
    ("Airbnb",                "https://careers.airbnb.com/positions/"),
    # -- Finance / Fintech --
    ("JP Morgan",             "https://www.jpmorgan.com/IE/en/about/careers"),
    ("Citi",                  "https://jobs.citi.com/search-jobs/Dublin"),
    ("Bank of Ireland",       "https://careers.bankofireland.com/jobs"),
    ("AIB",                   "https://aib.ie/careers"),
    ("Mastercard",            "https://careers.mastercard.com/us/en/search-results?location=Ireland"),
    ("Visa",                  "https://jobs.smartrecruiters.com/Visa/search?q=&location=Dublin"),
    ("BNY Mellon",            "https://bnymellon.eightfold.ai/careers?query=ireland"),
    ("Northern Trust",        "https://careers.northerntrust.com/jobs"),
    ("State Street",          "https://careers.statestreet.com/global/en/home"),
    ("Fidelity Investments",  "https://jobs.fidelity.com/search-jobs/Ireland/185"),
    ("Coinbase",              "https://www.coinbase.com/careers/positions"),
    ("Revolut",               "https://www.revolut.com/careers"),
    # -- Consulting / IT Services --
    ("Accenture",             "https://www.accenture.com/ie-en/careers/jobsearch"),
    ("Deloitte",              "https://apply.deloitte.com/careers/SearchJobs/?3_56_3=1890"),
    ("PwC",                   "https://www.pwc.ie/careers/search-jobs.html"),
    ("EY",                    "https://careers.ey.com/ey/jobs?country=ireland"),
    ("KPMG",                  "https://home.kpmg/ie/en/home/careers.html"),
    ("Capgemini",             "https://www.capgemini.com/ie-en/careers/job-search/"),
    ("IBM",                   "https://www.ibm.com/employment/search.html?country=IE"),
    ("Cognizant",             "https://careers.cognizant.com/global/en"),
    ("Infosys",               "https://careers.infosys.com/joblist?region=Ireland"),
    ("Wipro",                 "https://careers.wipro.com/careers-home/jobs"),
    ("TCS",                   "https://ibegin.tcs.com/iBegin/jobs/search"),
    ("DXC Technology",        "https://dxc.wd1.myworkdayjobs.com/DXC_Jobs"),
    # -- Enterprise Software / Hardware --
    ("Oracle",                "https://careers.oracle.com/jobs/#en/sites/jobsearch/requisitions?keyword=&location=Ireland"),
    ("SAP",                   "https://jobs.sap.com/search/?q=&location=Ireland"),
    ("Salesforce",            "https://careers.salesforce.com/jobs#location=Ireland"),
    ("Workday",               "https://workday.wd5.myworkdayjobs.com/en-US/Workday"),
    ("Dell Technologies",     "https://jobs.dell.com/search-jobs/Ireland"),
    ("HPE",                   "https://careers.hpe.com/us/en/search-results?keywords=&location=Ireland"),
    ("Intel",                 "https://jobs.intel.com/en/search#q=&location=Ireland"),
    ("Cisco",                 "https://jobs.cisco.com/jobs/SearchJobs/?21178=233"),
    ("Ericsson",              "https://jobs.ericsson.com/en_US/jobs?location=Ireland"),
    ("VMware",                "https://careers.vmware.com/jobs"),
    ("Unum",                  "https://jobs.unum.com/search/?q=&locationsearch=Ireland"),
    # -- Cybersecurity --
    ("Palo Alto Networks",    "https://careers.paloaltonetworks.com/jobs"),
    ("CrowdStrike",           "https://crowdstrike.wd5.myworkdayjobs.com/en-US/crowdstrikecareers"),
    ("Sophos",                "https://www.sophos.com/en-us/about/jobs"),
    ("Proofpoint",            "https://proofpoint.wd5.myworkdayjobs.com/en-US/ProofpointCareers"),
    ("Rapid7",                "https://www.rapid7.com/about/careers/"),
    ("Integrity360",          "https://www.integrity360.com/careers"),
    ("Trellix",               "https://careers.trellix.com/"),
    ("Tenable",               "https://careers.tenable.com/jobs"),
    ("Arctic Wolf",           "https://arcticwolf.com/careers/"),
    ("BT Security",           "https://careers.bt.com/go/View-All-Technology-Jobs/5089501/"),
    ("Recorded Future",       "https://www.recordedfuture.com/careers"),
    # -- Irish / Local Tech --
    ("Version 1",             "https://www.version1.com/careers/"),
    ("Ward Solutions",        "https://ward.ie/careers/"),
    ("Ergo",                  "https://ergogroup.ie/careers/"),
    ("Auxilion",              "https://www.auxilion.com/careers"),
    ("Trilogy Technologies",  "https://trilogytechnologies.com/careers/"),
    ("Dataplex",              "https://dataplex.ie/careers/"),
    ("Fexco",                 "https://fexco.com/careers"),
    ("Three Ireland",         "https://www.three.ie/careers/"),
    ("Eir",                   "https://eir.ie/careers/"),
    ("Vodafone Ireland",      "https://careers.vodafone.ie/"),
    ("Atos Ireland",          "https://atos.net/en/careers"),
    ("NTT Data",              "https://careers.nttdata.com/global/en"),
    ("Fujitsu Ireland",       "https://www.fujitsu.com/ie/about/careers/"),
    # -- Cloud / SaaS --
    ("HubSpot",               "https://www.hubspot.com/careers"),
    ("Zendesk",               "https://jobs.zendesk.com/"),
    ("MongoDB",               "https://www.mongodb.com/careers"),
    ("Atlassian",             "https://www.atlassian.com/company/careers/all-jobs"),
    ("Dynatrace",             "https://careers.dynatrace.com/"),
    ("Snowflake",             "https://careers.snowflake.com/us/en"),
    ("Twilio",                "https://www.twilio.com/en-us/company/jobs"),
    ("DocuSign",              "https://careers.docusign.com/"),
    ("Intercom",              "https://www.intercom.com/careers"),
    ("ServiceNow",            "https://careers.servicenow.com/jobs"),
    ("Qualtrics",             "https://careers.qualtrics.com/jobs"),
    ("Dropbox",               "https://jobs.dropbox.com/"),
    ("Spotify",               "https://jobs.spotify.com/"),
    ("Genesys",               "https://jobs.genesys.com/jobs"),
    ("Amdocs",                "https://careers.amdocs.com/jobs"),
    # -- Pharma / Healthcare IT --
    ("Johnson & Johnson",     "https://jobs.jnj.com/jobs"),
    ("Pfizer",                "https://www.pfizer.com/about/careers"),
    ("AbbVie",                "https://careers.abbvie.com/en/"),
    ("MSD Ireland",           "https://jobs.msd.com/"),
    ("Roche",                 "https://careers.roche.com/global/en/jobs"),
    ("Regeneron",             "https://careers.regeneron.com/jobs"),
    ("Boston Scientific",     "https://jobs.bostonscientific.com/jobs"),
    ("Medtronic",             "https://jobs.medtronic.com/jobs"),
    ("Abbott",                "https://www.jobs.abbott/us/en"),
    ("Stryker",               "https://careers.stryker.com/jobs"),
    ("Optum Ireland",         "https://careers.unitedhealthgroup.com/job-search-results/?keyword=&location=Dublin"),
    # -- Insurance / Additional Finance --
    ("AXA",                   "https://careers.axa.com/jobs"),
    ("Allianz Ireland",       "https://careers.allianz.com/"),
    ("Zurich Insurance",      "https://www.zurichna.com/careers"),
    ("Aviva",                 "https://careers.aviva.com/jobs"),
    ("Irish Life",            "https://irishlife.ie/careers/"),
    ("Liberty IT",            "https://jobs.libertymutualgroup.com/jobs"),
    ("SS&C Technologies",     "https://www.ssctech.com/careers"),
]
