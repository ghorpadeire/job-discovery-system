"""
Centralised logging configuration.

Call `setup_logging()` once at the top of each entry-point script
(run_all.py, dashboard.py, telegram_bot.py, score_jobs.py).

Features
--------
- Rotating file handler  — logs/scraper.log, max 5 MB × 5 backups
- Colour-coded console output (Windows-safe)
- Single call: setup_logging()
- Respects LOG_LEVEL env var  (default INFO)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


# ANSI colour codes (skip on Windows without ANSI support)
_COLOURS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[35m",   # magenta
}
_RESET = "\033[0m"
_ANSI_ENABLED = sys.platform != "win32" or os.environ.get("FORCE_COLOR")


class _ColourFormatter(logging.Formatter):
    """Console formatter with optional ANSI colour."""

    FMT = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    DATEFMT = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if _ANSI_ENABLED:
            colour = _COLOURS.get(record.levelname, "")
            return f"{colour}{msg}{_RESET}"
        return msg


def setup_logging(
    level: str | None = None,
    log_file: str | Path | None = None,
) -> None:
    """
    Configure root logger with:
    - Colour console handler (stdout)
    - Rotating file handler (logs/scraper.log by default)

    Safe to call multiple times — idempotent.
    """
    if logging.root.handlers:
        return   # already configured

    level = level or os.getenv("LOG_LEVEL", "INFO")
    numeric = getattr(logging, level.upper(), logging.INFO)

    # ── Console ──────────────────────────────────────────────────────────────
    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(numeric)
    console.setFormatter(
        _ColourFormatter(
            fmt=_ColourFormatter.FMT,
            datefmt=_ColourFormatter.DATEFMT,
        )
    )

    handlers: list[logging.Handler] = [console]

    # ── File (rotating) ──────────────────────────────────────────────────────
    if log_file is None:
        log_file = os.getenv(
            "LOG_FILE",
            str(Path(__file__).resolve().parent.parent / "logs" / "scraper.log"),
        )
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes    = 5 * 1024 * 1024,   # 5 MB
            backupCount = 5,
            encoding    = "utf-8",
        )
        fh.setLevel(logging.DEBUG)   # file always captures everything
        fh.setFormatter(
            logging.Formatter(
                fmt     = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
                datefmt = "%Y-%m-%d %H:%M:%S",
            )
        )
        handlers.append(fh)
    except OSError as exc:
        console.warning(f"[logging] Cannot open log file {log_path}: {exc}")

    logging.basicConfig(level=numeric, handlers=handlers)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "playwright", "openai", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
