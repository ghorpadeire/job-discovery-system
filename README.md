<div align="center">

# Job Discovery & Ghost Job Filter System

**Automated job aggregation + AI-powered legitimacy scoring for the Irish tech market**

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![Playwright](https://img.shields.io/badge/Playwright-1.52-45ba4b?logo=playwright&logoColor=white)](https://playwright.dev)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o--mini-412991?logo=openai&logoColor=white)](https://openai.com)

*Built as a portfolio project by Pranav Ghorpade — MSc Cybersecurity, NCI Dublin*

</div>

---

## The Problem

The Irish job market is saturated with **ghost listings** — roles reposted indefinitely, already filled, or never real. A junior developer can waste hours applying to positions that will never move forward. This system was built to solve that.

## What It Does

| Feature | Description |
|---|---|
| Multi-source scraping | Scrapes IrishJobs, Indeed Ireland, Jobs.ie, ITJobs.ie in parallel |
| AI careers scraper | Scans 100 company careers pages via Jina AI + GPT-4o-mini |
| Ghost detection | 7-signal legitimacy scorer assigns 0–100 score per job |
| Web dashboard | Flask + Bootstrap 5 + HTMX live-filter dashboard |
| Live monitor | Real-time SSE stream showing every job being evaluated |
| Telegram alerts | Daily digest + instant alerts for high-scoring jobs |
| Application tracker | Kanban board: Saved → Applied → Interview → Offer → Rejected |
| Auto-deduplication | Cross-source duplicate merging via normalised fingerprints |

---

## Architecture

```
run_all.py  (CLI entry point)
  Phase 1 — Playwright scrapers  (parallel)
    IrishJobs  /  Indeed  /  ITJobs  /  Jobs.ie

  Phase 2 — AI Careers Scraper  (async, 4 concurrent)
    100 Companies  →  Jina AI  →  GPT-4o-mini  →  Jobs

  Phase 3 — Upsert → Mark inactive → Dedup → Auto-score

       PostgreSQL (jobsdb)            Redis (pub/sub)
               │                           │
       Flask Dashboard  ◄──────  SSE /live stream
               │
         Telegram Bot
```

---

## Legitimacy Scoring (0–100)

Each job is scored automatically after every scrape run using 7 independent signals:

| Signal | Points | What it checks |
|---|---:|---|
| `career_page_match` | 25 | Job title found on the company's official careers page |
| `recently_posted` | 20 | Posted within the last 14 days |
| `company_volume` | 15 | Company has 3+ active roles in the database |
| `not_a_repost` | 15 | First seen within the last 30 days |
| `url_resolves` | 10 | HTTP HEAD on the job URL returns 2xx |
| `has_salary` | 10 | Salary range is disclosed |
| `rich_description` | 5 | Job URL page has >200 words of content |

> **Ghost threshold:** Score < 30 marks a job as `suspected_ghost = True`

---

## Quick Start

### Prerequisites

- Python 3.12+
- PostgreSQL 16 (running on port 5432)
- Redis 7 (running on port 6379)
- OpenAI API key (for the AI careers scraper)

### 1. Clone and install

```bash
git clone https://github.com/ghorpadeire/job-discovery.git
cd job-discovery
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — fill in DB credentials and OpenAI API key
```

### 3. Initialise the database

```bash
psql -U postgres -c "CREATE DATABASE jobsdb;"
psql -U postgres -c "CREATE USER jobsuser WITH PASSWORD 'jobspass';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE jobsdb TO jobsuser;"
# Tables are auto-created on first run via SQLAlchemy create_all
```

### 4. Run the scraper

```bash
py run_all.py                       # all sources
py run_all.py --sources ij indeed   # IrishJobs + Indeed only
py run_all.py --sources ai          # AI careers scraper only
py run_all.py --debug               # save raw HTML to disk
```

### 5. Launch the dashboard

```bash
py dashboard.py                     # localhost:5000
py dashboard.py --host 0.0.0.0      # expose to LAN
py dashboard.py --public            # ngrok public HTTPS tunnel
```

### 6. Start the Telegram bot *(optional)*

```bash
# Requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
py telegram_bot.py
```

---

## Project Structure

```
job-discovery/
├── core/
│   ├── config.py           Centralised env-var config
│   ├── logging_config.py   Rotating file + colour console logging
│   ├── database.py         SQLAlchemy engine + session factory
│   ├── models.py           Job + ApplicationTracker ORM models
│   ├── cache.py            RedisCache with NullCache fallback
│   ├── scorer.py           7-signal legitimacy scorer
│   ├── deduplicator.py     Cross-source duplicate merger
│   ├── career_checker.py   100-company careers page finder
│   └── progress.py         SSE event emitter (Redis pub/sub)
├── scrapers/
│   ├── base.py             Abstract BaseScraper + ScraperResult
│   ├── irishjobs.py        IrishJobs.ie  (Playwright)
│   ├── indeed.py           Indeed Ireland (JSON blob + HTML fallback)
│   ├── itjobs.py           ITJobs.ie     (fail-fast on bot-block)
│   ├── jobs_ie.py          Jobs.ie       (Playwright)
│   └── ai_careers.py       Jina AI + GPT-4o-mini scraper
├── templates/
│   ├── base.html           Bootstrap 5.3 dark sidebar layout
│   ├── index.html          Dashboard home  (6 stat cards)
│   ├── jobs.html           Filterable jobs table (HTMX live search)
│   ├── job_detail.html     Score breakdown bars + AI reasoning
│   ├── apply_tracker.html  Kanban application tracker
│   ├── companies.html      Company credibility overview
│   ├── live.html           Real-time SSE scrape monitor
│   ├── 404.html            Custom 404 page
│   └── partials/
│       └── jobs_rows.html  HTMX partial for table rows
├── run_all.py              Main scraper CLI
├── dashboard.py            Flask web dashboard
├── telegram_bot.py         Telegram notification bot
├── score_jobs.py           Standalone scoring CLI
├── requirements.txt        Pinned Python dependencies
├── .env.example            Environment variable template
├── .gitignore
└── .github/
    └── workflows/
        └── ci.yml          GitHub Actions (lint + imports + DB test)
```

---

## CLI Reference

```bash
# ── Scraper ──────────────────────────────────────────────────
py run_all.py                        # all sources
py run_all.py --sources ij           # IrishJobs only
py run_all.py --sources indeed       # Indeed only
py run_all.py --sources itjobs       # ITJobs.ie only
py run_all.py --sources jobsie       # Jobs.ie only
py run_all.py --sources ai           # AI careers (100 companies)
py run_all.py --sources ij indeed    # multiple sources
py run_all.py --debug                # save raw HTML to disk
py run_all.py --no-headless          # show browser windows
py run_all.py --no-dedup             # skip deduplication pass

# ── Scorer ───────────────────────────────────────────────────
py score_jobs.py                     # score all unscored jobs
py score_jobs.py --rescore           # re-score everything
py score_jobs.py --min-score 70      # show jobs scoring ≥ 70
py score_jobs.py --show-ghosts       # show suspected ghost listings

# ── Dashboard ────────────────────────────────────────────────
py dashboard.py                      # localhost:5000
py dashboard.py --host 0.0.0.0       # LAN accessible
py dashboard.py --port 8080          # custom port
py dashboard.py --public             # ngrok public HTTPS tunnel
```

---

## AI Careers Scraper

The `ai_careers` scraper works in three stages for each of 100 Ireland-based tech companies:

1. **Fetch** — Uses [Jina AI Reader](https://r.jina.ai) to convert careers pages (including JS-heavy SPAs) to clean Markdown. Falls back to direct HTTP GET if Jina returns thin content.
2. **Extract** — Sends the Markdown to `gpt-4o-mini` with a structured extraction prompt. Returns JSON with `title`, `company`, `url`, `salary`, and `date_posted`.
3. **Validate** — A keyword filter guards against GPT hallucinations; any title not matching the target role list is silently dropped before the DB write.

Runs 4 companies concurrently with a 1.2 s delay between batches to be respectful to Jina's free tier.

---

## Live Scrape Monitor

Navigate to `/live` during a scrape run to watch every job being evaluated in real-time:

| Colour | Meaning |
|---|---|
| 🟢 Green | Job accepted — passed keyword filter |
| 🔴 Red | Job filtered — keyword mismatch, duplicate, etc. |
| 🔵 Blue | AI scraper starting a new company |
| 🟣 Purple | Playwright scraper status update |
| 🟡 Amber | Run-level event (start / complete) |

The terminal auto-scrolls, supports per-type filtering, replays recent history on page load, and auto-reconnects on disconnect. Works from any device on the same network, or publicly via the ngrok tunnel.

---

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/status` | Active job count, avg score, added today, high-confidence count |
| `/top10` | 10 highest-scoring active jobs with direct apply links |
| `/live` | Get the Live Monitor URL |
| `/help` | All commands |

**Automatic notifications:**
- 🌅 **Daily digest** at 09:00 — all new jobs (last 24 h) scoring ≥ 70
- 🚨 **Instant alerts** every 5 minutes for any job scoring ≥ 85

---

## Tech Stack

| Layer | Technology |
|---|---|
| Scraping | Playwright (Chromium), BeautifulSoup4, httpx |
| AI | OpenAI GPT-4o-mini, Jina AI Reader (free tier) |
| Database | PostgreSQL 16, SQLAlchemy 2.0 |
| Cache / Pub-Sub | Redis 7 |
| Web | Flask 3.1, Bootstrap 5.3, HTMX 2.0 |
| Notifications | python-telegram-bot 22, APScheduler 3 |
| Real-time | Server-Sent Events (SSE) |
| Dev tunnel | pyngrok |
| CI/CD | GitHub Actions |

---

## About

Built by **Pranav Ghorpade** as a portfolio project during MSc Cybersecurity at NCI Dublin.

| | |
|---|---|
| Education | MSc Cybersecurity, NCI Dublin |
| Certification | CEH Master |
| Location | Dublin, Ireland (Stamp 1G) |
| GitHub | [github.com/ghorpadeire](https://github.com/ghorpadeire) |

---

<div align="center">
<sub>Built with coffee and a healthy scepticism towards ghost job listings.</sub>
</div>
