"""
Redis cache with NullCache fallback.
The system works normally when Redis is unavailable — it just doesn't cache.
"""
import json
import logging
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # 1 hour


class RedisCache:
    """Thin wrapper around redis-py with JSON serialisation."""

    def __init__(self, client):
        self._r = client

    def get(self, key: str) -> Any | None:
        try:
            raw = self._r.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Cache GET error for %r: %s", key, exc)
            return None

    def set(self, key: str, value: Any, ttl: int = DEFAULT_TTL) -> bool:
        try:
            self._r.setex(key, ttl, json.dumps(value, default=str))
            return True
        except Exception as exc:
            logger.warning("Cache SET error for %r: %s", key, exc)
            return False

    def delete(self, key: str) -> bool:
        try:
            self._r.delete(key)
            return True
        except Exception as exc:
            logger.warning("Cache DELETE error for %r: %s", key, exc)
            return False

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False


class NullCache:
    """No-op cache used when Redis is unavailable."""

    def get(self, key: str) -> None:
        return None

    def set(self, key: str, value: Any, ttl: int = DEFAULT_TTL) -> bool:
        return False

    def delete(self, key: str) -> bool:
        return False

    def ping(self) -> bool:
        return False


def get_cache() -> RedisCache | NullCache:
    """
    Try to connect to Redis.  Falls back to NullCache silently so the
    rest of the application never needs to handle Redis being down.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis as redis_lib

        client = redis_lib.from_url(redis_url, socket_connect_timeout=2, decode_responses=True)
        client.ping()
        logger.info("Redis cache: connected (%s)", redis_url)
        return RedisCache(client)
    except Exception as exc:
        logger.info("Redis unavailable (%s) — using NullCache", exc)
        return NullCache()
