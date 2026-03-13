"""
Live scrape progress emitter.

Architecture
------------
  run_all.py / scrapers/ai_careers.py
      └─ emitter.emit(event_type, **kwargs)
              └─ publishes JSON to Redis channel  "scrape:live"
              └─ appends to in-process history list (capped at 500)

  dashboard.py (SSE endpoint /live/stream)
      └─ background thread subscribes to Redis "scrape:live"
      └─ fans out to all connected clients via per-client queue.Queue

If Redis is unavailable the emitter degrades silently — events are only
stored in the in-process history list so the SSE feed still works for
clients connecting to the same process.

Event types
-----------
  run_start       — scraper run begins            {sources}
  run_done        — scraper run finishes          {total_jobs, new, updated}
  source_start    — a Playwright source begins    {source}
  source_done     — a Playwright source finishes  {source, job_count}
  page_done       — a search-result page parsed   {source, query, page, job_count}
  company_start   — AI scraper starts a company   {company, url}
  company_done    — AI scraper finishes a company {company, job_count}
  job_accepted    — job passes all filters        {title, company, source, url}
  job_filtered    — job rejected (reason given)   {title, company, source, reason}
  error           — any scraper error             {source, message}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Maximum events kept in history
_HISTORY_CAP = 500

# Redis channel name
CHANNEL = "scrape:live"


class ProgressEmitter:
    """
    Thread-safe event emitter.

    Usage::

        from core.progress import emitter

        emitter.emit("job_accepted", title="Backend Dev", company="Acme", ...)
    """

    def __init__(self) -> None:
        self._history:  deque[dict] = deque(maxlen=_HISTORY_CAP)
        self._lock:     threading.Lock = threading.Lock()
        self._redis_pub = None          # lazy-initialised Redis client
        self._redis_ok  = False

        # Live SSE listeners: list of queue.Queue (one per connected browser tab)
        self._listeners: list = []
        self._listener_lock = threading.Lock()

        self._try_connect_redis()

    # ------------------------------------------------------------------
    # Redis connection (best-effort)
    # ------------------------------------------------------------------

    def _try_connect_redis(self) -> None:
        try:
            import redis
            client = redis.Redis(host="localhost", port=6379, decode_responses=True)
            client.ping()
            self._redis_pub = client
            self._redis_ok  = True
            logger.debug("[progress] Redis connected — pub/sub active")
        except Exception as exc:
            logger.debug(f"[progress] Redis unavailable — in-process only: {exc}")
            self._redis_ok = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(self, event_type: str, **kwargs: Any) -> None:
        """Emit a progress event."""
        event = {
            "type":      event_type,
            "ts":        datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        with self._lock:
            self._history.append(event)

        payload = json.dumps(event, ensure_ascii=False, default=str)

        # Push to Redis for cross-process subscribers
        if self._redis_ok and self._redis_pub:
            try:
                self._redis_pub.publish(CHANNEL, payload)
            except Exception as exc:
                logger.debug(f"[progress] Redis publish failed: {exc}")
                self._redis_ok = False   # stop trying

        # Push directly to in-process SSE listeners
        with self._listener_lock:
            dead = []
            for q in self._listeners:
                try:
                    q.put_nowait(payload)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._listeners.remove(q)

    def get_history(self) -> list[dict]:
        """Return a copy of the recent event history (newest last)."""
        with self._lock:
            return list(self._history)

    def clear_history(self) -> None:
        """Wipe the in-process history buffer."""
        with self._lock:
            self._history.clear()

    # ------------------------------------------------------------------
    # SSE listener registration (used by dashboard.py)
    # ------------------------------------------------------------------

    def register_listener(self, q) -> None:
        """Register a queue.Queue to receive future events."""
        with self._listener_lock:
            self._listeners.append(q)

    def unregister_listener(self, q) -> None:
        """Remove a previously registered queue."""
        with self._listener_lock:
            if q in self._listeners:
                self._listeners.remove(q)

    @property
    def listener_count(self) -> int:
        with self._listener_lock:
            return len(self._listeners)

    # ------------------------------------------------------------------
    # Stats helper (for the live dashboard stat bar)
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """
        Derive live-run counters from history.
        Returns dict with keys: sources, companies, jobs_found,
        jobs_filtered, jobs_new, pages_done, running.
        """
        stats = {
            "sources":       0,
            "companies":     0,
            "jobs_found":    0,
            "jobs_filtered": 0,
            "jobs_new":      0,
            "pages_done":    0,
            "running":       False,
        }
        with self._lock:
            history = list(self._history)

        for ev in history:
            t = ev.get("type", "")
            if t == "run_start":
                # reset counters on new run
                stats = {k: 0 for k in stats}
                stats["running"] = True
            elif t == "run_done":
                stats["running"]   = False
                stats["jobs_new"]  = ev.get("new", 0)
            elif t == "source_start":
                stats["sources"] += 1
            elif t == "company_start":
                stats["companies"] += 1
            elif t == "page_done":
                stats["pages_done"] += 1
                stats["jobs_found"] += ev.get("job_count", 0)
            elif t == "company_done":
                stats["jobs_found"] += ev.get("job_count", 0)
            elif t == "job_filtered":
                stats["jobs_filtered"] += 1
            elif t == "job_accepted":
                pass   # counted via page_done / company_done

        return stats


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------
emitter = ProgressEmitter()
