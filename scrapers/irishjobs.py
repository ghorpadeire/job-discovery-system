"""
IrishJobs.ie scraper — Playwright-based, handles pagination and anti-bot measures.

Targets:
  - /jobs/it-jobs/
  - /jobs/science-pharma-food/   (for variety & future expansion)

Max 5 pages per category to avoid overloading the site.
"""
import asyncio
import logging
import random
from typing import Optional

from scrapers.base import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

BASE_URL = "https://www.irishjobs.ie"

SEARCH_PATHS = [
    "/jobs/it-jobs/",
    "/jobs/science-pharma-food/",
]

MAX_PAGES = 5

# Realistic Chrome UA on Windows
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class IrishJobsScraper(BaseScraper):
    def __init__(self):
        super().__init__("irishjobs")

    def scrape(self) -> list[ScraperResult]:
        """Run async Playwright scraper in a new event loop."""
        try:
            return asyncio.run(self._async_scrape())
        except Exception as exc:
            self.logger.error("IrishJobs scrape failed: %s", exc)
            return []

    async def _async_scrape(self) -> list[ScraperResult]:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout

        results: list[ScraperResult] = []
        seen_urls: set[str] = set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-http2",           # avoid ERR_HTTP2_PROTOCOL_ERROR
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-IE",
                extra_http_headers={
                    "Accept-Language": "en-IE,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page = await context.new_page()

            # Suppress resource-heavy requests
            await page.route(
                "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,css}",
                lambda route: route.abort(),
            )

            for path in SEARCH_PATHS:
                url = BASE_URL + path
                self.logger.info("Scraping IrishJobs: %s", url)

                for page_num in range(1, MAX_PAGES + 1):
                    page_url = url if page_num == 1 else f"{url}?page={page_num}"
                    try:
                        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(random.randint(1500, 3000))

                        # Try to dismiss cookie banner
                        try:
                            await page.click(
                                "button:has-text('Accept'), button:has-text('Accept All'), "
                                "[id*=cookie] button, [class*=cookie] button",
                                timeout=3000,
                            )
                        except Exception:
                            pass

                        cards = await self._extract_cards(page)
                        if not cards:
                            self.logger.debug("No cards found on page %d, stopping", page_num)
                            break

                        for card in cards:
                            if card.url and card.url not in seen_urls:
                                seen_urls.add(card.url)
                                results.append(card)

                        # Check for next page
                        has_next = await self._has_next_page(page)
                        if not has_next:
                            break

                        await asyncio.sleep(random.uniform(1.0, 2.5))

                    except PWTimeout:
                        self.logger.warning("Timeout on %s page %d", path, page_num)
                        break
                    except Exception as exc:
                        self.logger.warning("Error on %s page %d: %s", path, page_num, exc)
                        break

            await browser.close()

        self.logger.info("IrishJobs: scraped %d jobs", len(results))
        return results

    async def _extract_cards(self, page) -> list[ScraperResult]:
        """Extract all job cards from the current page."""
        cards = []
        try:
            # IrishJobs uses article tags or divs with job listing classes
            selectors = [
                "article[data-testid*='job']",
                "article.job-listing",
                "div.job-listing",
                "li[data-testid*='job']",
                "[class*='jobResult']",
                "[class*='job-result']",
                "[class*='searchResult']",
            ]

            job_elements = []
            for sel in selectors:
                elements = await page.query_selector_all(sel)
                if elements:
                    job_elements = elements
                    self.logger.debug("Using selector: %s (%d elements)", sel, len(elements))
                    break

            if not job_elements:
                # Fallback: look for any links to /job/ pages
                links = await page.query_selector_all("a[href*='/job/'], a[href*='/jobs/']")
                return await self._extract_from_links(page, links)

            for el in job_elements:
                card = await self._parse_card(el)
                if card:
                    cards.append(card)

        except Exception as exc:
            self.logger.warning("_extract_cards error: %s", exc)

        return cards

    async def _parse_card(self, el) -> Optional[ScraperResult]:
        """Parse a single job card element into a ScraperResult."""
        try:
            # Title (h2, h3, or element with title/heading class)
            title = ""
            for sel in ["h2", "h3", "h4", "[class*='title']", "[class*='heading']", "a[title]"]:
                el_title = await el.query_selector(sel)
                if el_title:
                    title = (await el_title.inner_text()).strip()
                    if title:
                        break
            if not title:
                return None

            # Company
            company = ""
            for sel in ["[class*='company']", "[class*='employer']", "[data-testid*='company']", "span.company"]:
                el_company = await el.query_selector(sel)
                if el_company:
                    company = (await el_company.inner_text()).strip()
                    if company:
                        break
            if not company:
                company = "Unknown Company"

            # Location
            location = ""
            for sel in ["[class*='location']", "[data-testid*='location']", "span.location", "[class*='place']"]:
                el_loc = await el.query_selector(sel)
                if el_loc:
                    location = (await el_loc.inner_text()).strip()
                    if location:
                        break

            # Salary
            salary = ""
            for sel in ["[class*='salary']", "[data-testid*='salary']", "[class*='pay']"]:
                el_sal = await el.query_selector(sel)
                if el_sal:
                    salary = (await el_sal.inner_text()).strip()
                    if salary:
                        break

            # Date
            date_posted = ""
            for sel in ["[class*='date']", "[data-testid*='date']", "time", "[class*='posted']", "[class*='age']"]:
                el_date = await el.query_selector(sel)
                if el_date:
                    date_posted = (
                        await el_date.get_attribute("datetime")
                        or await el_date.inner_text()
                    )
                    date_posted = (date_posted or "").strip()
                    if date_posted:
                        break

            # URL
            url = ""
            for sel in ["a[href*='/job/']", "a[href*='/jobs/']", "a[href]", "h2 a", "h3 a"]:
                el_url = await el.query_selector(sel)
                if el_url:
                    href = await el_url.get_attribute("href")
                    if href:
                        url = href if href.startswith("http") else BASE_URL + href
                        break
            if not url:
                return None

            return ScraperResult(
                title=title,
                company=company,
                location=location,
                salary=salary,
                date_posted=date_posted,
                url=url,
                description="",
                source="irishjobs",
            )

        except Exception as exc:
            logger.debug("_parse_card error: %s", exc)
            return None

    async def _extract_from_links(self, page, links) -> list[ScraperResult]:
        """Fallback: extract jobs from raw links when card selectors fail."""
        results = []
        for link in links[:50]:  # cap at 50
            try:
                title = await link.inner_text()
                href = await link.get_attribute("href")
                if not title or not href:
                    continue
                url = href if href.startswith("http") else BASE_URL + href
                results.append(ScraperResult(
                    title=title.strip(),
                    company="Unknown Company",
                    location="",
                    salary="",
                    date_posted="",
                    url=url,
                    description="",
                    source="irishjobs",
                ))
            except Exception:
                pass
        return results

    async def _has_next_page(self, page) -> bool:
        """Return True if a 'Next' pagination button is present and clickable."""
        try:
            next_selectors = [
                "a[aria-label='Next']",
                "a[rel='next']",
                "a:has-text('Next')",
                "button:has-text('Next')",
                "[class*='pagination'] a:last-child",
                "li.next a",
            ]
            for sel in next_selectors:
                el = await page.query_selector(sel)
                if el:
                    is_disabled = await el.get_attribute("disabled")
                    if not is_disabled:
                        return True
            return False
        except Exception:
            return False
