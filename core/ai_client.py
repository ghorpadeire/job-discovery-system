"""
OpenAI API client — singleton, rate-limited, auto-retrying.

Rate limits (Tier-1 defaults — conservative targets):
  text-embedding-3-small : 3,000 RPM  → we cap at 500 RPM (safety margin)
  gpt-4o-mini            : 500  RPM  → we cap at 100 RPM

Retry policy:
  - Rate-limit (429): exponential back-off starting at 10s
  - Server error (5xx / timeout): exponential back-off starting at 2s
  - Any other error: raise immediately
"""
import logging
import os
import threading
import time
from functools import wraps
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client singleton
# ---------------------------------------------------------------------------

_client     = None
_client_lock = threading.Lock()


def get_client():
    """Return the shared OpenAI client, creating it on first call."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from openai import OpenAI
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise RuntimeError(
                        "OPENAI_API_KEY not set.\n"
                        "Add it to your .env file:  OPENAI_API_KEY=sk-..."
                    )
                _client = OpenAI(api_key=api_key)
                logger.debug("OpenAI client initialised.")
    return _client


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (per-endpoint)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Simple request-per-minute limiter.
    Enforces a minimum interval of (60 / rpm) seconds between calls.
    Thread-safe.
    """
    def __init__(self, rpm: int):
        self._interval = 60.0 / rpm
        self._last     = 0.0
        self._lock     = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now   = time.monotonic()
            delta = now - self._last
            if delta < self._interval:
                time.sleep(self._interval - delta)
            self._last = time.monotonic()


# One limiter per endpoint family
_embedding_rl = _RateLimiter(rpm=500)
_chat_rl      = _RateLimiter(rpm=100)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def with_retry(max_retries: int = 4, base_delay: float = 2.0):
    """
    Decorator factory: wrap an OpenAI API call with retry + back-off.

    Usage
    -----
        @with_retry(max_retries=4)
        def call_api():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc  = exc
                    msg       = str(exc).lower()
                    # Rate-limit — back off longer
                    if "rate limit" in msg or "429" in msg:
                        delay = 10.0 * (2 ** attempt)
                        logger.warning(
                            f"[{func.__name__}] Rate limited. "
                            f"Waiting {delay:.0f}s (attempt {attempt+1}/{max_retries})…"
                        )
                        time.sleep(delay)
                    # Transient server errors
                    elif any(k in msg for k in ("500", "502", "503", "504", "timeout", "connection")):
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"[{func.__name__}] Transient error: {exc}. "
                            f"Retry {attempt+1}/{max_retries} in {delay:.1f}s…"
                        )
                        time.sleep(delay)
                    else:
                        # Non-retryable (auth error, invalid request, etc.)
                        raise
            raise RuntimeError(
                f"{func.__name__} failed after {max_retries} retries. "
                f"Last error: {last_exc}"
            )
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Public API wrappers
# ---------------------------------------------------------------------------

def get_embedding(
    text: str,
    model: str = "text-embedding-3-small",
) -> list[float]:
    """
    Return a float vector for *text*.
    Input is truncated to 8191 tokens (API limit for this model).
    Rate-limited to 500 RPM; retries on transient errors.
    """
    _embedding_rl.wait()

    @with_retry(max_retries=4, base_delay=2.0)
    def _call():
        return (
            get_client()
            .embeddings.create(
                input=text.strip()[:8191],
                model=model,
            )
            .data[0]
            .embedding
        )

    return _call()


def chat_completion(
    messages: list[dict],
    model: str       = "gpt-4o-mini",
    temperature: float = 0.1,
    max_tokens: int    = 256,
) -> str:
    """
    Single chat completion — returns the assistant message string.
    Rate-limited to 100 RPM; retries on transient errors.
    """
    _chat_rl.wait()

    @with_retry(max_retries=4, base_delay=2.0)
    def _call():
        resp = get_client().chat.completions.create(
            model       = model,
            messages    = messages,
            temperature = temperature,
            max_tokens  = max_tokens,
        )
        return resp.choices[0].message.content or ""

    return _call()


def check_api_key() -> bool:
    """Quick smoke-test: verifies the key is set and accepted by the API."""
    try:
        get_embedding("test")
        return True
    except RuntimeError:
        return False          # key not set
    except Exception as exc:
        logger.error(f"OpenAI API key check failed: {exc}")
        return False
