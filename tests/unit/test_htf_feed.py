"""Read-only higher-timeframe warmup and historical cutoff tests."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from VolumeSniper.htf_feed import INTERVAL_MS, fetch_closed_higher_timeframes


def _row(ts: int, period_ms: int, close: str = "101") -> list:
    return [ts, "100", "102", "99", close, "4", ts + period_ms - 1,
            "404", 10, "2", "202", "0"]


@asynccontextmanager
async def _serve(handler):
    app = web.Application()
    app.router.add_get("/fapi/v1/klines", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_warmup_retains_exchange_close_time_and_excludes_partial_bars():
    cutoff = INTERVAL_MS["4h"] - 1
    requests = []

    async def handler(request):
        requests.append(dict(request.query))
        period_ms = INTERVAL_MS[request.query["interval"]]
        return web.json_response([
            _row(cutoff + 1, period_ms),  # current, unclosed candle
            _row(0, period_ms),
            _row(0, period_ms),  # duplicate exchange row
            ["malformed"],
        ])

    async with _serve(handler) as url:
        result = await fetch_closed_higher_timeframes(
            ["btcusdt", "BTCUSDT"], url, limit=4, as_of_ms=cutoff,
        )

    assert list(result) == ["BTCUSDT"]
    assert set(result["BTCUSDT"]) == {"15m", "1h", "4h"}
    for interval, period_ms in INTERVAL_MS.items():
        assert result["BTCUSDT"][interval] == [{
            "ts": 0, "open": 100.0, "high": 102.0, "low": 99.0,
            "close": 101.0, "volume": 4.0, "close_ts": period_ms - 1,
        }]
    assert len(requests) == 3
    assert all(row["endTime"] == str(cutoff) and row["limit"] == "4"
               and row["symbol"] == "BTCUSDT" for row in requests)


@pytest.mark.asyncio
async def test_historical_cutoff_never_accepts_bar_that_closes_later():
    cutoff = INTERVAL_MS["15m"]

    async def handler(request):
        period_ms = INTERVAL_MS[request.query["interval"]]
        return web.json_response([_row(0, period_ms), _row(period_ms, period_ms)])

    async with _serve(handler) as url:
        result = await fetch_closed_higher_timeframes(
            ["ETHUSDT"], url, as_of_ms=cutoff,
        )

    assert [bar["ts"] for bar in result["ETHUSDT"]["15m"]] == [0]
    assert result["ETHUSDT"]["1h"] == []
    assert result["ETHUSDT"]["4h"] == []


@pytest.mark.asyncio
async def test_429_and_server_error_retry_but_client_error_does_not():
    cutoff = INTERVAL_MS["4h"] - 1
    count = {interval: 0 for interval in INTERVAL_MS}

    async def handler(request):
        interval = request.query["interval"]
        count[interval] += 1
        if interval == "15m" and count[interval] == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        if interval == "15m" and count[interval] == 2:
            return web.Response(status=503)
        if interval == "1h":
            return web.Response(status=400)
        return web.json_response([_row(0, INTERVAL_MS[interval])])

    async with _serve(handler) as url:
        result = await fetch_closed_higher_timeframes(
            ["BTCUSDT"], url, as_of_ms=cutoff,
        )

    assert count == {"15m": 3, "1h": 1, "4h": 1}
    assert len(result["BTCUSDT"]["15m"]) == 1
    assert result["BTCUSDT"]["1h"] == []
    assert len(result["BTCUSDT"]["4h"]) == 1


@pytest.mark.asyncio
async def test_concurrency_and_symbol_limit_are_bounded():
    active = 0
    peak = 0
    requests = 0

    async def handler(request):
        nonlocal active, peak, requests
        active += 1
        peak = max(peak, active)
        requests += 1
        await asyncio.sleep(0.02)
        active -= 1
        return web.json_response([_row(0, INTERVAL_MS[request.query["interval"]])])

    symbols = [f"COIN{i}USDT" for i in range(8)]
    async with _serve(handler) as url:
        result = await fetch_closed_higher_timeframes(
            symbols, url, as_of_ms=INTERVAL_MS["4h"] - 1, max_symbols=8,
        )
        with pytest.raises(ValueError, match="at most 7"):
            await fetch_closed_higher_timeframes(symbols, url, max_symbols=7)

    assert len(result) == 8
    assert requests == 24
    assert 1 < peak <= 6


@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_network():
    with pytest.raises(ValueError, match="limit"):
        await fetch_closed_higher_timeframes(["BTCUSDT"], "https://fapi.binance.com", limit=501)
    with pytest.raises(ValueError, match="symbols"):
        await fetch_closed_higher_timeframes(["BTC/USDT"], "https://fapi.binance.com")
    with pytest.raises(ValueError, match="as_of_ms"):
        await fetch_closed_higher_timeframes(["BTCUSDT"], "https://fapi.binance.com", as_of_ms=-1)
