"""
Central configuration for the Job Discovery System.

All env vars are read here.  Import this module everywhere instead of
calling os.getenv() directly — gives you one place to validate and
document every setting.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (works regardless of cwd)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


# ── PostgreSQL ────────────────────────────────────────────────────────────────
DB_HOST     = os.getenv("DB_HOST",     "localhost")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME",     "jobsdb")
DB_USER     = os.getenv("DB_USER",     "jobsuser")
DB_PASSWORD = os.getenv("DB_PASSWORD", "jobspass")

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB   = int(os.getenv("REDIS_DB",   "0"))

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL",   "gpt-4o-mini")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_PORT    = int(os.getenv("PORT",           "5000"))
DASHBOARD_DEBUG   = os.getenv("FLASK_ENV", "development") != "production"
SECRET_KEY        = os.getenv("SECRET_KEY", os.urandom(32).hex())

# ── Scraper behaviour ─────────────────────────────────────────────────────────
SCRAPER_HEADLESS     = os.getenv("SCRAPER_HEADLESS",     "true").lower() != "false"
GHOST_SCORE_THRESHOLD = int(os.getenv("GHOST_THRESHOLD", "30"))

# ── Scoring ───────────────────────────────────────────────────────────────────
HIGH_SCORE_ALERT_THRESHOLD = int(os.getenv("ALERT_SCORE", "85"))
DIGEST_MIN_SCORE           = int(os.getenv("DIGEST_SCORE", "70"))

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE  = os.getenv("LOG_FILE",  str(_ROOT / "logs" / "scraper.log"))


def validate() -> list[str]:
    """
    Return a list of configuration warnings (not fatal, just informative).
    Called at startup to surface missing optional keys.
    """
    warnings = []
    if not OPENAI_API_KEY:
        warnings.append("OPENAI_API_KEY not set — AI careers scraper will be disabled.")
    if not TELEGRAM_BOT_TOKEN:
        warnings.append("TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled.")
    if not TELEGRAM_CHAT_ID:
        warnings.append("TELEGRAM_CHAT_ID not set — Telegram notifications disabled.")
    return warnings
