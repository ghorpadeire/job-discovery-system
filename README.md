# 🔍 Job Discovery & Ghost Job Filter System

> Automated job scraping, legitimacy scoring, and application tracking — purpose-built to cut through ghost jobs and stale listings in the Irish tech market.

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791?style=flat&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=flat&logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat&logo=docker&logoColor=white)](https://docker.com)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-45ba4b?style=flat&logo=playwright&logoColor=white)](https://playwright.dev)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o--mini-412991?style=flat&logo=openai&logoColor=white)](https://openai.com)
[![CI/CD](https://img.shields.io/badge/GitHub_Actions-CI%2FCD-2088FF?style=flat&logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat)](LICENSE)

---

## 🧩 Problem Statement

Job hunting in Ireland is frustrating for a specific reason: **ghost jobs**.

Recruiters routinely post roles that are already filled, indefinitely "evergreen", or never genuinely open. Entry-level candidates waste hours tailoring CVs for listings that will never result in a call. Aggregators like Indeed and IrishJobs surface these alongside real vacancies with no way to tell them apart.

This project solves that by:

1. **Scraping** fresh job listings automatically from multiple Irish sources
2. **Scoring** every listing across 7 legitimacy signals (0–100)
3. **Flagging** likely ghost jobs before you ever click "Apply"
4. **Tracking** real applications through a Kanban pipeline
5. **Alerting** via Telegram when genuinely strong matches arrive

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Docker Compose Stack                       │
│                                                                 │
│  ┌─────────────┐   scrape_done   ┌──────────────┐              │
│  │   scraper   │ ──────────────► │    scorer    │              │
│  │  (Python /  │   Redis pub/sub │  (Python /   │              │
│  │ APScheduler)│                 │  SQLAlchemy) │              │
│  └──────┬──────┘                 └──────┬───────┘              │
│         │                               │                       │
│         │  writes jobs                  │  writes scores        │
│         ▼                               ▼                       │
│  ┌──────────────────────────────────────────────┐               │
│  │              PostgreSQL 15                   │               │
│  │   jobs table · application_tracker table     │               │
│  └────────────────────┬─────────────────────────┘               │
│                       │                                         │
│                       │  reads                                  │
│                       ▼                                         │
│  ┌─────────────────────────────┐   ┌──────────────────────┐    │
│  │        web (Flask)          │   │   telegram_bot.py    │    │
│  │  Dashboard · Filter · Track │   │  Instant alerts      │    │
│  │  port 5000                  │   │  Daily digest        │    │
│  └─────────────────────────────┘   └──────────────────────┘    │
│                                                                 │
│  ┌──────────────┐                                               │
│  │    Redis 7   │  ← cache · pub/sub bus · rate-limit store     │
│  └──────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘

  Scraping layer:  Playwright (Chromium, headless) + httpx
  Sources:         IrishJobs.ie  ·  Indeed Ireland
```

---

## ✨ Features

| Feature | Description |
|---|---|
| **Multi-source scraping** | IrishJobs.ie (Playwright) + Indeed Ireland (JSON + HTML fallback) |
| **Ghost job scoring** | 7-signal legitimacy score per listing (0–100) |
| **AI relevance scoring** | GPT-4o-mini rates job fit against a custom profile |
| **Deduplication** | Normalised cross-source dedup — same job, one row |
| **Career page verification** | Checks company's own site for the role (Signal 1) |
| **Flask dashboard** | Dark-mode UI with live HTMX filtering |
| **Application Kanban** | Saved → Applied → Interview → Offer → Rejected |
| **Company credibility** | Per-company ghost rate, avg score, role volume |
| **Telegram alerts** | Instant push for score ≥ 85, daily digest at 09:00 |
| **Docker Compose** | One-command full-stack deployment |
| **CI/CD pipeline** | GitHub Actions: lint → test → Docker build → GHCR push |

---

## 📊 Scoring Algorithm

Each job is scored 0–100 across seven weighted signals:

| # | Signal | Points | Logic |
|---|---|---|---|
| 1 | **Career page match** | 25 | Title found on company's own careers site |
| 2 | **Recently posted** | 20 | Posted within the last 14 days |
| 3 | **Company volume** | 15 | Company has ≥ 3 active roles in the database |
| 4 | **Not a repost** | 15 | `first_seen` within the last 30 days |
| 5 | **URL resolves** | 10 | HTTP HEAD to job URL returns 2xx |
| 6 | **Has salary** | 10 | Salary field is populated |
| 7 | **Rich description** | 5 | > 200 words in the full job description |

**Ghost job threshold:** combined score < 30

**AI scoring (optional, Phase 4):**
- Embeddings via `text-embedding-3-small` for semantic similarity
- Reasoning via `gpt-4o-mini` for relevance explanation
- Cost: ~$0.01–$0.05 per full rescore of 200 jobs

---

## 🖼️ Screenshots

> _Dashboard screenshots — coming soon_

| Page | Description |
|---|---|
| **Home** | KPI cards (active jobs, avg score, added today, ghost count, high-confidence, in tracker) |
| **Jobs** | Filterable table with live HTMX search, score pills, source badges |
| **Job Detail** | Signal breakdown bars, AI reasoning, inline tracker panel |
| **Apply Tracker** | Kanban board across 5 pipeline stages |
| **Companies** | Credibility table with ghost rates and progress bars |

---

## 🚀 Quick Start — Docker (Recommended)

### Prerequisites
- Docker Desktop (Mac/Linux) or Docker Engine + Compose plugin
- A free [OpenAI API key](https://platform.openai.com/api-keys) (optional — only needed for AI scoring)
- A Telegram bot token from [@BotFather](https://t.me/BotFather) (optional — only needed for alerts)

### 1. Clone & configure

```bash
git clone https://github.com/ghorpadeire/job-discovery-system.git
cd job-discovery-system

cp .env.example .env
# Edit .env — at minimum fill in OPENAI_API_KEY and TELEGRAM_* if desired
```

### 2. Start the full stack

```bash
docker compose up -d
```

This starts PostgreSQL, Redis, the scraper service, the scorer service, and the Flask dashboard.

### 3. Open the dashboard

```
http://localhost:5000
```

The scraper runs immediately on startup and again every 12 hours. The scorer triggers automatically after each scrape via Redis pub/sub.

### Useful commands

```bash
# View live logs from all services
docker compose logs -f

# View logs from a specific service
docker compose logs -f scraper

# Force an immediate scrape
docker compose exec scraper python run_all.py

# Force an immediate rescore
docker compose exec scorer python score_jobs.py --rescore

# Stop everything (preserves volumes)
docker compose down

# Stop and wipe all data
docker compose down -v
```

---

## 🛠️ Manual Install (No Docker)

### Prerequisites
- Python 3.13+
- PostgreSQL 16 running locally on port 5432
- Redis 7 running locally on port 6379

### 1. Clone & install

```bash
git clone https://github.com/ghorpadeire/job-discovery-system.git
cd job-discovery-system

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium --with-deps
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env:
#   DATABASE_URL=postgresql://jobsuser:jobspass@localhost:5432/jobsdb
#   REDIS_URL=redis://localhost:6379/0
#   OPENAI_API_KEY=sk-...    (optional)
#   TELEGRAM_BOT_TOKEN=...   (optional)
#   TELEGRAM_CHAT_ID=...     (optional)
```

### 3. Initialise the database

```bash
# Create the DB and user (as postgres superuser)
psql -U postgres -c "CREATE USER jobsuser WITH PASSWORD 'jobspass';"
psql -U postgres -c "CREATE DATABASE jobsdb OWNER jobsuser;"

# Tables are created automatically by SQLAlchemy on first run
```

### 4. Run

```bash
# Scrape + score (one shot)
py run_all.py
py score_jobs.py --rescore

# Launch dashboard
py dashboard.py
# → http://localhost:5000

# Show only ghost jobs (score < 30)
py score_jobs.py --show-ghosts

# Show high-confidence jobs (score ≥ 70)
py score_jobs.py --min-score 70
```

---

## 📁 Project Structure

```
job-discovery-system/
│
├── core/
│   ├── models.py            # SQLAlchemy: Job, ApplicationTracker
│   ├── database.py          # Engine factory, check_connection()
│   ├── cache.py             # RedisCache + NullCache fallback
│   ├── deduplicator.py      # Cross-source deduplication
│   ├── career_checker.py    # Company careers-page signal
│   └── scorer.py            # 7-signal scorer + AI scoring
│
├── scrapers/
│   ├── base.py              # Abstract BaseScraper, ScraperResult
│   ├── irishjobs.py         # IrishJobs.ie — Playwright scraper
│   └── indeed.py            # Indeed Ireland — JSON + HTML fallback
│
├── services/                # Docker service entry points
│   ├── scraper_loop.py      # APScheduler loop (every 12h)
│   └── scorer_loop.py       # Redis sub + fallback timer
│
├── templates/               # Jinja2 / Bootstrap 5 dark theme
│   ├── base.html            # Sidebar layout, HTMX, Bootstrap Icons
│   ├── index.html           # Home — KPI cards + top jobs
│   ├── jobs.html            # Job list with HTMX live filters
│   ├── job_detail.html      # Signal bars, AI reasoning, tracker
│   ├── apply_tracker.html   # Kanban board
│   ├── companies.html       # Company credibility table
│   ├── 404.html
│   └── partials/
│       └── jobs_rows.html   # HTMX partial — table rows only
│
├── .github/
│   └── workflows/
│       └── ci.yml           # Lint → Test → Docker build → GHCR push
│
├── dashboard.py             # Flask app + all routes
├── run_all.py               # Unified scraper CLI
├── score_jobs.py            # Scoring CLI
├── telegram_bot.py          # Telegram alerts + daily digest
├── Dockerfile               # Shared Python 3.13-slim image
├── docker-compose.yml       # Full stack: postgres, redis, scraper, scorer, web
├── init.sql                 # DB schema bootstrapped on first Docker run
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 🗺️ Roadmap

- [x] **Phase 1** — PostgreSQL schema + SQLAlchemy models
- [x] **Phase 2** — IrishJobs.ie Playwright scraper
- [x] **Phase 3** — Indeed Ireland scraper (JSON blob + HTML fallback)
- [x] **Phase 4** — 7-signal legitimacy scorer + OpenAI AI relevance scoring
- [x] **Phase 5** — Telegram bot (instant alerts + daily digest)
- [x] **Phase 6** — Flask dashboard (HTMX, Kanban tracker, company credibility)
- [x] **Phase 7** — Docker Compose full stack + GitHub Actions CI/CD
- [ ] **Phase 8** — LinkedIn Jobs scraper
- [ ] **Phase 9** — CV auto-matching with semantic similarity
- [ ] **Phase 10** — One-click apply draft generation (GPT-4o)
- [ ] **Phase 11** — Email digest fallback (SMTP)
- [ ] **Phase 12** — Public demo deployment (Fly.io / Render)

---

## ⚙️ Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | ✅ | — | PostgreSQL connection string |
| `REDIS_URL` | ✅ | — | Redis connection string |
| `OPENAI_API_KEY` | ⚠️ optional | — | Enables AI relevance scoring |
| `TELEGRAM_BOT_TOKEN` | ⚠️ optional | — | Enables Telegram notifications |
| `TELEGRAM_CHAT_ID` | ⚠️ optional | — | Your personal chat ID |
| `SCRAPE_INTERVAL_HOURS` | ❌ | `12` | How often the scraper runs |
| `SCRAPE_SOURCES` | ❌ | `all` | `all` \| `ij` \| `indeed` |
| `AI_SCORE_ENABLED` | ❌ | `false` | Enable OpenAI scoring in scorer service |
| `TG_ALERT_MIN_SCORE` | ❌ | `85` | Score threshold for instant Telegram alert |
| `TG_DIGEST_MIN_SCORE` | ❌ | `70` | Score threshold for daily digest |
| `TG_DIGEST_HOUR` | ❌ | `9` | Hour for daily digest (24h local time) |
| `DASHBOARD_PORT` | ❌ | `5000` | Flask listen port |
| `FLASK_ENV` | ❌ | `development` | `development` \| `production` |

See [`.env.example`](.env.example) for the full reference.

---

## 🧪 Running Tests

```bash
pip install pytest pytest-asyncio pytest-cov
pytest tests/ --cov=core --cov=scrapers -v
```

CI runs the same suite against a real PostgreSQL + Redis container on every push. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## 🐳 Container Registry

Every push to `main` builds and pushes a multi-platform image (`linux/amd64` + `linux/arm64`) to GitHub Container Registry:

```bash
docker pull ghcr.io/ghorpadeire/job-discovery-system:latest
```

---

## 👤 About the Author

**Pranav Ghorpade**

MSc Cybersecurity — National College of Ireland, Dublin
CEH Master certified
Stamp 1G (Graduate) — open to entry-level opportunities in Dublin and remote-Europe roles

This project was built as a portfolio piece to demonstrate end-to-end engineering skills across:
- Python backend development (async, ORM, scheduling, REST)
- Data engineering (scraping, deduplication, normalisation)
- DevOps (Docker Compose, GitHub Actions, GHCR, multi-platform builds)
- AI integration (OpenAI embeddings + GPT reasoning)
- Frontend (Flask, HTMX, Bootstrap 5)

> **Target roles:** Junior Software Developer (Java/Python) · Junior Cybersecurity Analyst · IT Support

📎 [github.com/ghorpadeire](https://github.com/ghorpadeire)
📍 Dublin, Ireland

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.
