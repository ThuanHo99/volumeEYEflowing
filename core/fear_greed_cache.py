"""Fear & Greed Index cache with TTL-based lazy refresh. Feeds from news sentiment analysis."""
import asyncio
import logging
import time
from typing import Optional

from core.config import FEAR_GREED_CACHE_TTL
from core.market_sentiment import FearGreedData

logger = logging.getLogger(__name__)


class FearGreedCache:
    """TTL-based cache for market sentiment data (derived from news, not API).

    Producers call set() with freshly-derived data.
    Consumers call get() — returns cached data if within TTL, None on miss.
    """

    def __init__(self, ttl: int = None):
        self._ttl: int = ttl if ttl is not None else FEAR_GREED_CACHE_TTL
        self._data: Optional[FearGreedData] = None
        self._last_fetch: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._fetch_count: int = 0
        self._cache_hits: int = 0

    async def set(self, data: FearGreedData) -> None:
        """Store derived sentiment data into the cache."""
        async with self._lock:
            self._data = data
            self._last_fetch = time.monotonic()
            self._fetch_count += 1

    async def get(self) -> Optional[FearGreedData]:
        """Return cached FearGreedData if within TTL, None on cache miss."""
        async with self._lock:
            if self._data is not None and (time.monotonic() - self._last_fetch) < self._ttl:
                self._cache_hits += 1
                logger.debug(f"FearGreedCache hit (age={time.monotonic() - self._last_fetch:.1f}s)")
                return self._data
            return None

    async def invalidate(self) -> None:
        """Force next get() to return None."""
        async with self._lock:
            self._data = None
            self._last_fetch = 0.0

    def is_fresh(self) -> bool:
        """Return True if cached data exists and is within TTL."""
        return self._data is not None and (time.monotonic() - self._last_fetch) < self._ttl

    @property
    def fetch_count(self) -> int:
        return self._fetch_count

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    def __repr__(self) -> str:
        return f"FearGreedCache(ttl={self._ttl}s, fresh={self.is_fresh()}, fetch_count={self._fetch_count})"


__all__ = ["FearGreedCache"]
