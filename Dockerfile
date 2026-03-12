# ─────────────────────────────────────────────────────────────────────────────
# Job Discovery System — Python image
# Shared by: scraper, scorer, web services
#
# Build:  docker build -t job-discovery .
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.13-slim AS base

# System deps for Playwright Chromium + psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (layer-cached separately from code) ───────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium (headless) — large but necessary for scraping
RUN playwright install chromium --with-deps

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# Non-root user (security best-practice)
RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Default entrypoint — overridden per service in docker-compose.yml
CMD ["python", "dashboard.py"]
