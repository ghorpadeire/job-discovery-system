# JobScout v2.0 — Irish Job Discovery & Ghost Job Filter System

Automated job hunting assistant that scrapes Irish job boards, scores every
listing on a 0–100 legitimacy scale, filters out ghost/fake jobs, and delivers
real opportunities to Telegram and a web dashboard.

Built for **Pranav Ghorpade** — MSc Cybersecurity, NCI Dublin  
Target: Entry-level Java Developer | Junior Cybersecurity Analyst | IT Support

---

## Quick Start (Local, Windows)

### Prerequisites
- Python 3.13 (`py` command)
- PostgreSQL 16 running on port 5432
- Redis running on port 6379
- Working directory: `F:\CLAUDE_CODE\JOB SEARCH\`

### 1. Install dependencies
```bat
py -m pip install -r requirements.txt
py -m playwright install chromium
```

### 2. Create your database
```bat
"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE USER jobsuser WITH PASSWORD 'jobspass';"
"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE jobsdb OWNER jobsuser;"
```

### 3. Configure .env
Edit `.env` and fill in your Telegram bot token and chat ID.
Get them from @BotFather and @userinfobot on Telegram.

### 4. Run the full pipeline
```bat
py run_all.py --score
```

### 5. Start the dashboard
```bat
py dashboard.py
```
Open http://localhost:5000

### 6. Start the Telegram bot (local polling)
```bat
py telegram_bot.py
```

---

## Commands

| Command | Description |
|---------|-------------|
| `py run_all.py --score` | Scrape all boards + score jobs |
| `py run_all.py --no-indeed` | Only scrape IrishJobs |
| `py run_all.py --no-irishjobs` | Only scrape Indeed |
| `py score_jobs.py` | Score unscored jobs |
| `py score_jobs.py --rescore` | Re-score all jobs |
| `py score_jobs.py --min-score 70` | Show only quality jobs |
| `py score_jobs.py --show-ghosts` | Audit ghost jobs |
| `py tg_notify.py --digest` | Send daily Telegram digest |
| `py tg_notify.py --alerts` | Send instant alerts for ≥85 jobs |
| `py telegram_bot.py` | Start persistent Telegram bot |
| `py dashboard.py` | Start web dashboard on port 5000 |

---

## 7-Signal Scoring System

| Signal | Points | Description |
|--------|--------|-------------|
| `career_page_match` | 25 | Job title found on company's real careers page |
| `recently_posted` | 20 | Posted within last 14 days |
| `company_volume` | 15 | Company has ≥3 active jobs (benefit of doubt if 0) |
| `not_a_repost` | 15 | First seen ≤30 days ago |
| `url_resolves` | 10 | Job URL responds (not dead link) |
| `has_salary` | 10 | Salary information provided |
| `rich_description` | 5 | Job page has ≥200 words |

**Score tiers:**
- `≥85` — Very high confidence → Instant Telegram alert
- `70–84` — High confidence → Daily digest
- `50–69` — Medium confidence
- `30–49` — Low confidence
- `<30` — Suspected ghost job

### Career Checker — 5-Layer Architecture
Signal 1 is the most powerful. It uses:
1. **Hardcoded known URLs** for 40+ major companies (Google, Microsoft, Accenture, AIB, etc.)
2. **DuckDuckGo search** for companies not in the known list
3. **ATS platform detection** — Workday JSON API, Greenhouse, Lever, SmartRecruiters
4. **Blind URL probing** — tries `www.{company}.com/careers`, `jobs.{company}.ie`, etc.
5. **Graceful failure** — always returns False on errors, never crashes

---

## Deployment — Render + GitHub Actions

### Step 1: Cloud database (Neon.tech — free)
1. Sign up at [neon.tech](https://neon.tech)
2. Create a database called `jobsdb`
3. Copy the connection string (format: `postgresql://user:pass@ep-xxx.neon.tech/jobsdb?sslmode=require`)

### Step 2: Cloud Redis (Upstash — free)
1. Sign up at [upstash.com](https://upstash.com)
2. Create a Redis database
3. Copy the connection string

### Step 3: GitHub setup
1. Push all code to a GitHub repo
2. Go to Settings → Secrets and Variables → Actions
3. Add these secrets:
   - `DATABASE_URL` — your Neon connection string
   - `REDIS_URL` — your Upstash connection string
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `OPENAI_API_KEY`

### Step 4: Render deployment
1. Go to [render.com](https://render.com) → New → Blueprint
2. Connect your GitHub repo
3. Render reads `render.yaml` and auto-deploys
4. Set env vars in Render dashboard (same as GitHub secrets)

### Step 5: Register Telegram webhook
Once deployed, run once:
```python
from dashboard import register_webhook
register_webhook("https://your-app.onrender.com")
```

### Step 6: Verify
1. Send `/status` to your Telegram bot
2. Trigger GitHub Actions manually (mode: `digest`)
3. Open `https://your-app.onrender.com` in browser

---

## Architecture

```
run_all.py ──► IrishJobsScraper ──► PostgreSQL
               IndeedScraper    ──►     │
                                        │
score_jobs.py ◄───────────────── scorer.py
                                  │
                              career_checker.py
                              (5-layer ATS detection)
                                        │
tg_notify.py ◄──────────── DB queries ─┘
telegram_bot.py ◄──────────────────────┘
dashboard.py (Flask + HTMX) ◄──────────┘

GitHub Actions (cron) → tg_notify.py (--digest / --alerts)
Render (web) ← Telegram webhook → dashboard.py /telegram/webhook
```

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Command list |
| `/status` | System statistics |
| `/top10` | Top 10 jobs by score |
| `/ghosts` | List suspected ghost jobs |
| `/search <keyword>` | Search jobs |

---

## File Structure

```
JOB SEARCH/
├── .env                          # Credentials (never commit!)
├── .github/workflows/telegram.yml # GitHub Actions — daily digest + alerts
├── core/
│   ├── models.py                 # SQLAlchemy models (Job, ApplicationTracker)
│   ├── database.py               # Engine factory + migrations
│   ├── cache.py                  # Redis + NullCache fallback
│   ├── deduplicator.py           # Cross-source dedup
│   ├── career_checker.py         # 5-layer ATS career page checker
│   └── scorer.py                 # 7-signal legitimacy scorer
├── scrapers/
│   ├── base.py                   # Abstract base + upsert logic
│   ├── irishjobs.py              # Playwright scraper for IrishJobs.ie
│   └── indeed.py                 # JSON+HTML scraper for Indeed IE
├── templates/                    # Flask/Jinja2 templates
│   ├── base.html                 # Dark sidebar layout
│   ├── index.html                # Dashboard home (6 stat cards)
│   ├── jobs.html                 # Filterable jobs table (HTMX)
│   ├── job_detail.html           # Job detail + score breakdown
│   ├── apply_tracker.html        # Kanban application tracker
│   ├── companies.html            # Company credibility table
│   └── partials/jobs_rows.html   # HTMX live filter partial
├── run_all.py                    # Unified CLI runner
├── score_jobs.py                 # Scoring CLI + report
├── tg_notify.py                  # One-shot GitHub Actions notifier
├── telegram_bot.py               # Persistent polling bot (local)
├── dashboard.py                  # Flask app + Telegram webhook
├── requirements.txt              # Full deps
├── requirements-notify.txt       # Lightweight deps (CI only)
├── Procfile                      # Render/Heroku process file
└── render.yaml                   # Render blueprint
```

---

## Improvements over v1

| Problem | v2 Solution |
|---------|-------------|
| Hardcoded 20 Big Tech companies only | 40+ companies + 5-layer ATS detection |
| Blind URL guessing (failed for 90%) | Workday JSON API, Greenhouse/Lever detection |
| No Irish company support | ESB, eir, AIB, Irish Life, Vodafone IE etc. |
| Silent failures on ATS-hosted jobs | Classified failure types + graceful fallback |
| No benefit-of-doubt scoring | company_volume + not_a_repost give benefit of doubt |
| No cross-source deduplication | Title+company fingerprint hash dedup |
| Dashboard too basic | Full HTMX filtering, Kanban tracker, company intel |

---

*Built with Python 3.13, PostgreSQL 16, Playwright, Flask, Bootstrap 5.3, HTMX 2.0*
