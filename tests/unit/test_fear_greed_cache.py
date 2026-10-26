"""Unit tests for core.fear_greed_cache.

Tests cover cache set/get, expiry, concurrent access, invalidation, and freshness.
Cache is now producer-fed (set) rather than auto-fetching from API.
"""

import asyncio
from unittest import mock

import pytest

from core.fear_greed_cache import FearGreedCache
from core.market_sentiment import FearGreedData


@pytest.fixture
def sample_data():
    """Return a known FearGreedData for test use."""
    return FearGreedData(
        value=25, classification="Fear", timestamp=1711574400, time_until_update=56543
    )


class TestFearGreedCache:
    """Tests for the FearGreedCache producer-fed cache."""

    @mock.patch('core.fear_greed_cache.FEAR_GREED_CACHE_TTL', 60)
    def test_constructor_default_ttl(self):
        """FearGreedCache() should use FEAR_GREED_CACHE_TTL default (60)."""
        cache = FearGreedCache()
        assert cache._ttl == 60
        assert cache._data is None
        assert not cache.is_fresh()

    def test_constructor_custom_ttl(self):
        """FearGreedCache(ttl=30) should use custom TTL."""
        cache = FearGreedCache(ttl=30)
        assert cache._ttl == 30

    @pytest.mark.asyncio
    async def test_get_returns_none_when_empty(self):
        """get() should return None when no data has been set."""
        cache = FearGreedCache(ttl=60)
        result = await cache.get()
        assert result is None

    @pytest.mark.asyncio
    async def test_set_then_get_returns_data(self, sample_data):
        """After set(), get() should return the stored data."""
        cache = FearGreedCache(ttl=60)
        await cache.set(sample_data)
        result = await cache.get()
        assert result is not None
        assert result.value == 25
        assert result.classification == "Fear"
        assert cache.fetch_count == 1
        assert cache.cache_hits == 1

    @pytest.mark.asyncio
    async def test_get_returns_cached_data_on_second_call(self, sample_data):
        """Second get() within TTL should return cached data."""
        cache = FearGreedCache(ttl=60)
        await cache.set(sample_data)
        result1 = await cache.get()
        result2 = await cache.get()
        assert result2.value == 25
        assert result2.classification == "Fear"
        assert result1 is result2
        assert cache.cache_hits == 2
        assert cache.fetch_count == 1

    @pytest.mark.asyncio
    async def test_get_expires_and_returns_none(self, mocker, sample_data):
        """After TTL expires, get() should return None (caller must set again)."""
        fake_time = [100.0]
        mocker.patch("time.monotonic", side_effect=lambda: fake_time[0])

        cache = FearGreedCache(ttl=30)
        await cache.set(sample_data)
        result1 = await cache.get()
        assert result1.value == 25

        fake_time[0] = 131.0
        result2 = await cache.get()
        assert result2 is None

    @pytest.mark.asyncio
    async def test_concurrent_sets_only_last_matters(self, sample_data):
        """Two concurrent set() calls should store the last one."""
        cache = FearGreedCache(ttl=60)

        data2 = FearGreedData(
            value=75, classification="Greed", timestamp=2, time_until_update=0
        )

        async def set1():
            await cache.set(sample_data)

        async def set2():
            await cache.set(data2)

        await asyncio.gather(set1(), set2())
        result = await cache.get()
        assert result.value == 75
        assert result.classification == "Greed"

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self, sample_data):
        """invalidate() should force next get() to return None."""
        cache = FearGreedCache(ttl=60)
        await cache.set(sample_data)
        assert cache.is_fresh()

        await cache.invalidate()
        assert not cache.is_fresh()
        assert cache._data is None

        result = await cache.get()
        assert result is None

    def test_is_fresh_property(self, sample_data):
        """is_fresh() should reflect cache state without awaiting."""
        import time

        cache = FearGreedCache(ttl=60)
        assert not cache.is_fresh()
        cache._data = sample_data
        cache._last_fetch = time.monotonic()
        assert cache.is_fresh()

    def test_repr(self):
        """__repr__ should include ttl and fresh state."""
        cache = FearGreedCache(ttl=60)
        r = repr(cache)
        assert "60" in r
        assert "FearGreedCache" in r
