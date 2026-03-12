"""
Redis-backed cache for page URLs and job fingerprints.

Purpose
-------
- Page cache  : skip re-fetching search result pages scraped today
- Job cache   : quickly check if a fingerprint was already processed today
  (avoids hitting the DB for every job on repeat runs)

Both caches use a 24-hour TTL so the scraper always refreshes daily.

If Redis is unavailable the `NullCache` fallback is used automatically —
the scraper continues to work, just without caching.
"""
import hashlib
import logging
import os
from typing import Optional

import redis
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_CACHE_TTL = 86_400          # 24 hours in seconds
_DEFAULT_REDIS = "redis://localhost:6379/0"


class RedisCache:
    """Live Redis-backed cache."""

    def __init__(self, url: Optional[str] = None):
        redis_url = url or os.getenv("REDIS_URL", _DEFAULT_REDIS)
        self._client = redis.from_url(redis_url, decode_responses=True)

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Page-level cache (search result pages)
    # ------------------------------------------------------------------

    def _page_key(self, url: str) -> str:
        return f"page:{hashlib.md5(url.encode()).hexdigest()}"

    def is_page_cached(self, url: str) -> bool:
        try:
            return bool(self._client.exists(self._page_key(url)))
        except Exception as exc:
            logger.debug(f"Redis read error: {exc}")
            return False

    def cache_page(self, url: str) -> None:
        try:
            self._client.setex(self._page_key(url), _CACHE_TTL, "1")
        except Exception as exc:
            logger.debug(f"Redis write error: {exc}")

    # ------------------------------------------------------------------
    # Job-level cache (fingerprints)
    # ------------------------------------------------------------------

    def _job_key(self, fingerprint: str) -> str:
        return f"job:{fingerprint}"

    def is_job_cached(self, fingerprint: str) -> bool:
        try:
            return bool(self._client.exists(self._job_key(fingerprint)))
        except Exception as exc:
            logger.debug(f"Redis read error: {exc}")
            return False

    def cache_job(self, fingerprint: str) -> None:
        try:
            self._client.setex(self._job_key(fingerprint), _CACHE_TTL, "1")
        except Exception as exc:
            logger.debug(f"Redis write error: {exc}")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        try:
            info = self._client.info("stats")
            return {
                "hits":   info.get("keyspace_hits",   0),
                "misses": info.get("keyspace_misses", 0),
            }
        except Exception:
            return {}


class NullCache:
    """No-op drop-in when Redis is unavailable."""

    def ping(self)                        -> bool: return False
    def is_page_cached(self, url: str)    -> bool: return False
    def cache_page(self, url: str)        -> None: pass
    def is_job_cached(self, fp: str)      -> bool: return False
    def cache_job(self, fp: str)          -> None: pass
    def stats(self)                       -> dict: return {}


def get_cache() -> "RedisCache | NullCache":
    """
    Try to connect to Redis. Return a live RedisCache on success,
    or a NullCache if Redis is unreachable.
    """
    cache = RedisCache()
    if cache.ping():
        logger.info("Redis connected — caching enabled")
        return cache
    logger.warning("Redis unavailable — running without cache (NullCache)")
    return NullCache()
