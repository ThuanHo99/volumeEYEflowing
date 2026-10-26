import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from VolumeSniper import config
from VolumeSniper.derivatives_context import DerivativesContext, explain_context, parse_observation
from ui.web_dashboard import SCANNER, derivatives_context_handler


def test_period_rows_exclude_unfinished_bar_and_keep_taker_delta():
    now = 1_800_000
    rows = [
        {"timestamp": 1_200_000, "buyVol": "6", "sellVol": "4", "buySellRatio": "1.5"},
        {"timestamp": 1_800_000, "buyVol": "100", "sellVol": "1", "buySellRatio": "100"},
    ]
    value, observed = parse_observation("taker_buy_sell", rows, now, 300_000)
    assert observed == 1_500_000
    assert value["delta"] == 2
    assert value["buy_sell_ratio"] == 1.5


def test_depth_snapshot_requires_uncrossed_book_and_preserves_exchange_id():
    now = 1_800_000
    book = {"lastUpdateId": 42, "T": now - 10, "bids": [["100", "2"]],
            "asks": [["101", "1"]]}
    value, observed = parse_observation("order_book", book, now, 300_000)
    assert observed == now - 10
    assert value["last_update_id"] == 42
    assert value["top20_imbalance"] == pytest.approx(99 / 301)
    book["asks"] = [["99", "1"]]
    assert parse_observation("order_book", book, now, 300_000) == (None, None)


def test_explanation_uses_only_fresh_evidence_and_avoids_direction_vote():
    snapshot = {"period": "5m", "metrics": {
        "open_interest_history": {"status": "ok", "value": {"notional_change_pct": 3}},
        "taker_buy_sell": {"status": "stale", "value": {"buy_sell_ratio": 9}},
        "order_book": {"status": "ok", "value": {"spread_bps": 2, "top20_imbalance": .3}},
    }}
    notes = explain_context(snapshot, scenario="FAILURE", event_delta_pct=40)
    assert any("OI notional changed" in note for note in notes)
    assert any("effort/result divergence" in note for note in notes)
    assert not any("9.00" in note for note in notes)
    assert any("orders can cancel" in note for note in notes)


@pytest.mark.asyncio
async def test_snapshot_fetches_all_sources_and_marks_missing_key(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_API_KEY", "")
    now = int(time.time() * 1000)
    period_start = now // 300_000 * 300_000 - 300_000
    calls = []

    async def fetch(path, params, keyed):
        calls.append((path, params, keyed))
        if path.endswith("/openInterestHist"):
            return [{"timestamp": period_start, "sumOpenInterest": "10",
                     "sumOpenInterestValue": "1000", "CMCCirculatingSupply": "100"}]
        if path.endswith("/openInterest"):
            return {"time": now - 100, "openInterest": "10"}
        if path.endswith("/premiumIndex"):
            return {"time": now - 100, "indexPrice": "10", "markPrice": "10.1",
                    "lastFundingRate": "0.0001", "nextFundingTime": now + 10_000}
        if path.endswith("/depth"):
            return {"T": now - 100, "lastUpdateId": 9,
                    "bids": [["10", "1"]], "asks": [["10.1", "1"]]}
        if path.endswith("/fundingRate"):
            return [{"fundingTime": now - 1_000_000, "fundingRate": "0.0002"}]
        if path.endswith("/basis"):
            return [{"timestamp": period_start, "basisRate": "0.01",
                     "indexPrice": "10", "futuresPrice": "10.1", "basis": "0.1"}]
        if path.endswith("/takerlongshortRatio"):
            return [{"timestamp": period_start, "buyVol": "6", "sellVol": "4"}]
        return [{"timestamp": period_start, "longShortRatio": "1.5",
                 "longAccount": "0.6", "shortAccount": "0.4"}]

    service = DerivativesContext(fetch=fetch)
    try:
        snapshot = await service.refresh("BTCUSDT")
        metrics = snapshot["metrics"]
        assert metrics["open_interest"]["status"] == "ok"
        assert metrics["top_trader_accounts"]["status"] == "api_key_required"
        assert metrics["top_trader_positions"]["status"] == "api_key_required"
        assert metrics["oi_to_market_cap"]["value"]["ratio_approx"] == pytest.approx(1)
        assert metrics["funding_history"]["value"]["settled_funding_rate"] == 0.0002
        assert len(calls) == 8
        assert any(path.endswith("/basis") and params["contractType"] == "PERPETUAL"
                   for path, params, _ in calls)
        await service.refresh("BTCUSDT")
        assert len(calls) == 8  # fresh cache does not poll again
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_api_key_ratio_endpoints_are_requested_when_configured(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_API_KEY", "test-key")
    now = int(time.time() * 1000)
    calls = []

    async def fetch(path, params, keyed):
        calls.append((path, keyed))
        if path.endswith("/topLongShortPositionRatio"):
            return [{"timestamp": now - 100, "longShortRatio": "2",
                     "longPosition": "0.667", "shortPosition": "0.333"}]
        if path.endswith("/topLongShortAccountRatio"):
            return [{"timestamp": now - 100, "longShortRatio": "1.5",
                     "longAccount": "0.6", "shortAccount": "0.4"}]
        return []

    service = DerivativesContext(fetch=fetch)
    try:
        metrics = (await service.refresh("BTCUSDT"))["metrics"]
        assert metrics["top_trader_accounts"]["value"]["long_short_ratio"] == 1.5
        assert metrics["top_trader_positions"]["value"]["long_share"] == 0.667
        assert all(keyed for path, keyed in calls if "topLongShort" in path)
    finally:
        await service.close()


def test_old_exchange_observation_is_stale_even_when_just_fetched(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_API_KEY", "")
    service = DerivativesContext(fetch=lambda *_: None)
    now = 1_800_000
    service._cache["BTCUSDT"] = {
        "open_interest_history": {"status": "ok", "value": {"notional_usd_approx": 100},
                                  "observed_at": 300_000, "fetched_at": now},
    }
    assert service.snapshot("BTCUSDT", now)["metrics"]["open_interest_history"]["status"] == "stale"


@pytest.mark.asyncio
async def test_dashboard_endpoint_refreshes_only_scanned_symbol():
    service = type("Service", (), {"refresh": AsyncMock(return_value={"symbol": "BTCUSDT", "metrics": {}})})()
    scanner = type("ScannerStub", (), {"_symbols": ["BTCUSDT"], "derivatives": service})()
    app = web.Application()
    app[SCANNER] = scanner
    response = await derivatives_context_handler(make_mocked_request(
        "GET", "/api/derivatives-context?symbol=BTC", app=app))
    assert response.status == 200
    service.refresh.assert_awaited_once_with("BTCUSDT")
    response = await derivatives_context_handler(make_mocked_request(
        "GET", "/api/derivatives-context?symbol=ETH", app=app))
    assert response.status == 404
    service.refresh.assert_awaited_once()
