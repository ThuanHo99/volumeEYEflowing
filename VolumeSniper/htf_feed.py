"""Bounded, read-only USD-M higher-timeframe kline warmup.

Only exchange-complete bars at or before ``as_of_ms`` are returned.  The
exchange close timestamp is retained so a caller can prove that a higher
timeframe observation was available before it used that observation.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Iterable

import aiohttp


LOGGER = logging.getLogger(__name__)
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
MAX_CONCURRENT_REQUESTS = 6
MAX_ATTEMPTS = 3
_SYMBOL = re.compile(r"^[A-Z0-9]{3,24}$")


def _parse_closed_bars(payload: object, interval: str, as_of_ms: int) -> list[dict]:
    """Validate Binance's 12-item kline rows and remove partial/future bars."""
    if not isinstance(payload, list):
        raise ValueError("Binance kline payload is not a list")

    period_ms = INTERVAL_MS[interval]
    by_open: dict[int, dict] = {}
    for raw in payload:
        if not isinstance(raw, list) or len(raw) < 7:
            continue
        try:
            ts, close_ts = int(raw[0]), int(raw[6])
            values = [float(raw[i]) for i in range(1, 6)]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in values):
            continue
        open_, high, low, close, volume = values
        if (ts < 0 or close_ts != ts + period_ms - 1 or
                close_ts > as_of_ms or ts % period_ms != 0 or
                min(open_, high, low, close) <= 0 or volume < 0 or
                low > min(open_, close) or high < max(open_, close)):
            continue
        by_open[ts] = {
            "ts": ts, "open": open_, "high": high, "low": low,
            "close": close, "volume": volume, "close_ts": close_ts,
        }
    return [by_open[ts] for ts in sorted(by_open)]


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Cap server-requested and exponential delays so warmup cannot hang."""
    delay = min(0.25 * 2 ** attempt, 2.0)
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), 2.0))
        except ValueError:
            pass
    return delay


async def _request_klines(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    symbol: str,
    interval: str,
    limit: int,
    as_of_ms: int,
) -> list[dict]:
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
        "endTime": as_of_ms,
    }
    url = f"{base_url.rstrip('/')}/fapi/v1/klines"
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with semaphore:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        payload = await response.json()
                        return _parse_closed_bars(payload, interval, as_of_ms)
                    if response.status == 429 or 500 <= response.status < 600:
                        if attempt < MAX_ATTEMPTS - 1:
                            delay = _retry_delay(attempt, response.headers.get("Retry-After"))
                        else:
                            raise RuntimeError(f"Binance HTTP {response.status}")
                    else:
                        raise RuntimeError(f"Binance HTTP {response.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Binance kline request failed: {exc}") from exc
            delay = _retry_delay(attempt)
        await asyncio.sleep(delay)
    raise RuntimeError("Binance kline retry limit exhausted")


async def fetch_closed_higher_timeframes(
    symbols: Iterable[str],
    base_url: str,
    *,
    limit: int = 100,
    as_of_ms: int | None = None,
    max_symbols: int = 32,
) -> dict[str, dict[str, list[dict]]]:
    """Fetch 15m/1h/4h USD-M candles for a selected, bounded symbol set.

    Returns ``{symbol: {interval: [ascending closed bars]}}``.  A failed
    symbol/interval fetch yields an empty list and a warning, never fabricated
    prices.  Capture ``as_of_ms`` once at the start so slow responses cannot
    admit candles that closed while this warmup was running.
    """
    if not isinstance(base_url, str) or not base_url.startswith(("https://", "http://")):
        raise ValueError("base_url must be an HTTP(S) URL")
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    if not isinstance(max_symbols, int) or max_symbols < 1:
        raise ValueError("max_symbols must be a positive integer")
    cutoff = int(time.time() * 1000) if as_of_ms is None else as_of_ms
    if not isinstance(cutoff, int) or cutoff < 0:
        raise ValueError("as_of_ms must be a nonnegative Unix millisecond timestamp")

    selected = list(dict.fromkeys(str(symbol).upper() for symbol in symbols))
    if len(selected) > max_symbols:
        raise ValueError(f"higher-timeframe warmup accepts at most {max_symbols} symbols")
    if any(not _SYMBOL.fullmatch(symbol) for symbol in selected):
        raise ValueError("symbols must be Binance-style uppercase alphanumeric names")
    result: dict[str, dict[str, list[dict]]] = {
        symbol: {interval: [] for interval in INTERVAL_MS} for symbol in selected
    }
    if not selected:
        return result

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    timeout = aiohttp.ClientTimeout(total=8)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        jobs = [
            (symbol, interval, _request_klines(
                session, semaphore, base_url, symbol, interval, limit, cutoff,
            ))
            for symbol in selected for interval in INTERVAL_MS
        ]
        outcomes = await asyncio.gather(*(job for _, _, job in jobs), return_exceptions=True)
        for (symbol, interval, _), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                LOGGER.warning("Higher-timeframe warmup failed for %s %s: %s",
                               symbol, interval, outcome)
            else:
                result[symbol][interval] = outcome
    return result
