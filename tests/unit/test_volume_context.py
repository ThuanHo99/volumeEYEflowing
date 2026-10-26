"""Prospective volume/price scenarios: no direction before confirmation."""

import json
from datetime import datetime, timezone

import pytest

from VolumeSniper import config
from VolumeSniper.volume_context import ContextSignal, SeasonalVolumeBaseline, VolumeContextEngine


def test_binance_kline_endpoints_use_same_environment_and_market_route():
    config.validate_market_data_endpoints(
        "https://fapi.binance.com",
        "wss://fstream.binance.com/market/stream?streams=",
    )
    with pytest.raises(ValueError, match="mix production and testnet"):
        config.validate_market_data_endpoints(
            "https://fapi.binance.com",
            "wss://fstream.binancefuture.com/stream?streams=",
        )
    with pytest.raises(ValueError, match="mix production and testnet"):
        config.validate_market_data_endpoints(
            "https://demo-fapi.binance.com",
            "wss://fstream.binance.com/market/stream?streams=",
        )
    with pytest.raises(ValueError, match="/market/stream"):
        config.validate_market_data_endpoints(
            "https://fapi.binance.com",
            "wss://fstream.binance.com/stream?streams=",
        )
    with pytest.raises(ValueError, match="streams="):
        config.validate_market_data_endpoints(
            "https://fapi.binance.com",
            "wss://fstream.binance.com/market/stream",
        )


BASE_TS = 1_700_000_000_000 // 60_000 * 60_000


def kline(i, *, open_=100, high=101, low=99, close=100,
          volume=100, buy=50, closed=True):
    return {
        "t": BASE_TS + i * 60_000, "o": str(open_), "h": str(high),
        "l": str(low), "c": str(close), "v": str(volume),
        "q": str(volume * close), "V": str(buy), "x": closed,
    }


def warm_engine(tmp_path):
    engine = VolumeContextEngine(SeasonalVolumeBaseline(tmp_path / "baseline.json"))
    engine.seed("BTCUSDT", [kline(i) for i in range(20)])
    return engine


def test_breakout_requires_later_hold_and_uses_confirmed_price(tmp_path):
    engine = warm_engine(tmp_path)
    plan = engine.plans["BTCUSDT"]
    assert (plan.upper, plan.lower, plan.ready_ts) == (101, 99, BASE_TS + 20 * 60_000 - 1)
    event = engine.on_kline("BTCUSDT", kline(20, high=102.5, low=99.9,
                                             close=102, volume=400, buy=300))
    assert event is None
    assert engine.pending["BTCUSDT"].rvol == 4  # current candle excluded

    signal = engine.on_kline("BTCUSDT", kline(21, open_=102, high=102.2,
                                              low=100.99, close=101.5))
    assert signal is not None
    assert (signal.direction, signal.scenario) == ("LONG", "BREAKOUT")
    assert signal.price == 101.5
    assert signal.level == 101
    assert signal.plan_ts < signal.event_ts
    assert signal.confirmed_close_ts == BASE_TS + 22 * 60_000 - 1
    assert (signal.plan_lower, signal.plan_upper) == (99, 101)
    assert signal.invalidation < signal.level
    assert signal.delta_pct == 50
    assert signal.baseline == "rolling (provisional)"


def test_volume_at_preplanned_resistance_is_watch_not_direction(tmp_path):
    engine = warm_engine(tmp_path)
    assert engine.on_kline("BTCUSDT", kline(20, high=101, low=99.2,
                                            close=100.95, volume=400, buy=350)) is None
    assert not engine.pending
    watches = engine.drain_watches()
    assert len(watches) == 1
    assert watches[0]["edge"] == "upper"
    assert watches[0]["plan_ts"] < watches[0]["ts"]
    assert watches[0]["delta_pct"] == 75
    assert engine.on_kline("BTCUSDT", kline(21, high=102, low=100.8,
                                            close=101.3, volume=400)) is None
    signal = engine.on_kline("BTCUSDT", kline(22, high=101.5, low=101.01,
                                              close=101.4))
    assert signal is not None
    assert (signal.direction, signal.scenario) == ("LONG", "BREAKOUT")


def test_breakout_without_retest_expires_without_trade_direction(tmp_path):
    engine = warm_engine(tmp_path)
    assert engine.on_kline("BTCUSDT", kline(20, high=102.5, close=102,
                                            volume=400)) is None
    for i in (21, 22, 23):
        assert engine.on_kline("BTCUSDT", kline(i, open_=102, high=103,
                                                low=101.5, close=102.5)) is None
    assert not engine.pending
    assert not any(r["kind"] == "confirmed" for r in engine.records)
    assert any(r["kind"] == "expired" for r in engine.records)


def test_wick_rejection_waits_for_price_to_move_away(tmp_path):
    engine = warm_engine(tmp_path)
    assert engine.on_kline("BTCUSDT", kline(20, high=102, low=99,
                                            close=100.5, volume=400, buy=300)) is None
    signal = engine.on_kline("BTCUSDT", kline(21, high=100.7, low=98.5,
                                             close=98.8))
    assert signal is not None
    assert (signal.direction, signal.scenario) == ("SHORT", "FAILURE")
    assert signal.price == 98.8
    assert signal.invalidation > signal.level


def test_high_volume_inside_range_has_no_direction(tmp_path):
    engine = warm_engine(tmp_path)
    assert engine.on_kline("BTCUSDT", kline(20, high=100.8, low=99.2,
                                            close=100.4, volume=400, buy=350)) is None
    assert "BTCUSDT" not in engine.pending


def test_live_candle_and_duplicate_are_ignored(tmp_path):
    engine = warm_engine(tmp_path)
    assert engine.on_kline("BTCUSDT", kline(20, close=102, volume=400, closed=False)) is None
    assert "BTCUSDT" not in engine.pending
    assert engine.on_kline("BTCUSDT", kline(19, close=102, volume=400)) is None
    assert len(engine.history["BTCUSDT"]) == 20


@pytest.mark.asyncio
async def test_seasonal_slot_uses_only_prior_matching_week_hours(tmp_path):
    path = tmp_path / "baseline.json"
    baseline = SeasonalVolumeBaseline(path)
    hour = int(datetime(2026, 10, 22, 12, tzinfo=timezone.utc).timestamp() * 1000)
    slot = baseline._slot("BTCUSDT", hour)
    baseline.samples[slot] = [
        [hour - 14 * 86_400_000, 100],
        [hour - 7 * 86_400_000, 200],
    ]
    baseline.dirty = True
    assert baseline.baseline("BTCUSDT", hour) == 150
    assert baseline.baseline("BTCUSDT", hour + 3_600_000) is None
    await baseline.save()
    assert SeasonalVolumeBaseline(path).baseline("BTCUSDT", hour) == 150


def test_seasonal_hour_collects_complete_hour_medians(tmp_path):
    baseline = SeasonalVolumeBaseline(tmp_path / "baseline.json")
    hour = int(datetime(2026, 10, 22, 12, tzinfo=timezone.utc).timestamp() * 1000)
    for weeks_ago, volume in ((2, 100), (1, 200)):
        start = hour - weeks_ago * 7 * 86_400_000
        for minute in range(45):
            baseline.observe("BTCUSDT", type("C", (), {"ts": start + minute * 60_000,
                                                       "volume": volume})())
        baseline.observe("BTCUSDT", type("C", (), {"ts": start + 3_600_000,
                                                   "volume": volume})())
    assert baseline.baseline("BTCUSDT", hour) == 150


@pytest.mark.asyncio
async def test_prospective_journal_records_outcome_after_five_candles(tmp_path):
    engine = warm_engine(tmp_path)
    engine.on_kline("BTCUSDT", kline(20, high=102.5, close=102, volume=400))
    signal = engine.on_kline("BTCUSDT", kline(21, open_=102, high=102.2,
                                             low=100.99, close=101.5))
    assert signal is not None
    for i in range(22, 27):
        engine.on_kline("BTCUSDT", kline(i, open_=101.5, high=102.5,
                                         low=100.5, close=101.6))
    outcomes = [r for r in engine.records if r["kind"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["horizon_candles"] == 5
    assert outcomes[0]["return_pct_gross"] > 0
    await engine.flush_records()
    assert not engine.records
    assert engine.journal_path.read_text(encoding="utf-8").count("\n") >= 3


def test_gap_censors_outcome_and_discards_stale_levels(tmp_path):
    engine = warm_engine(tmp_path)
    engine.on_kline("BTCUSDT", kline(20, high=102.5, close=102, volume=400))
    assert engine.on_kline("BTCUSDT", kline(21, open_=102, high=102.2,
                                            low=100.99, close=101.5)) is not None
    engine.on_kline("BTCUSDT", kline(22, open_=101.5, high=102.5,
                                     low=101.2, close=102.3))
    engine.on_kline("BTCUSDT", kline(100, high=102.5, close=102, volume=400))
    assert not engine.pending
    assert "BTCUSDT" not in engine.plans
    assert len(engine.history["BTCUSDT"]) == 1
    assert any(r["kind"] == "censored_gap" for r in engine.records)


@pytest.mark.asyncio
async def test_research_mode_publishes_ui_but_not_telegram(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from VolumeSniper.scanner import Scanner

    monkeypatch.setattr(config, "CONTEXT_TELEGRAM_ALERTS_ENABLED", False)
    scanner = object.__new__(Scanner)
    scanner._context_confirmed = 0
    scanner.detector = type("D", (), {"states": {}})()
    scanner.ui_queue = asyncio.Queue()
    scanner.telegram = type("T", (), {"send_context_alert": AsyncMock()})()
    signal = ContextSignal("BTCUSDT", "LONG", "BREAKOUT", 101, 102,
                           100.95, 3, "rolling (provisional)", 20,
                           50, BASE_TS, BASE_TS + 60_000)
    await scanner._publish_context_signal(signal)
    msg = await scanner.ui_queue.get()
    assert msg["type"] == "volume_sniper_signal"
    assert msg["price"] == 102
    assert msg["confidence"] is None
    scanner.telegram.send_context_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_contested_watch_is_ui_only():
    import asyncio
    from VolumeSniper.scanner import Scanner

    scanner = object.__new__(Scanner)
    scanner.ui_queue = asyncio.Queue()
    await scanner._publish_context_watch({
        "symbol": "BTCUSDT", "edge": "upper", "price": 100.95,
        "plan_ts": BASE_TS, "plan_upper": 101, "plan_lower": 99,
        "ts": BASE_TS + 60_000, "rvol": 4,
        "baseline": "rolling (provisional)", "delta_pct": 75,
        "cvd": 300, "repeated_tests": 1,
        "quieting_before_event": False,
    })
    msg = await scanner.ui_queue.get()
    assert msg["type"] == "volume_context_watch"
    assert msg["direction"] == "WATCH"
    assert msg["plan_ts"] < msg["timestamp"]


@pytest.mark.asyncio
async def test_dashboard_exposes_plan_before_event_candle(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from ui.web_dashboard import SCANNER, context_plan_handler

    engine = warm_engine(tmp_path)
    app = web.Application()
    app[SCANNER] = type("ScannerStub", (), {"context": engine})()
    request = make_mocked_request("GET", "/api/context-plan?symbol=BTC", app=app)
    response = await context_plan_handler(request)
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["status"] == "WAIT"
    assert (payload["lower"], payload["upper"]) == (99, 101)
    assert payload["ready_ts"] < BASE_TS + 20 * 60_000
    assert payload["upper_breakout_invalidation"] < 101
    assert payload["upper_failure_invalidation"] > 101

    engine.on_kline("BTCUSDT", kline(20, high=102.5, close=102, volume=400))
    response = await context_plan_handler(request)
    pending_plan = json.loads(response.text)
    assert pending_plan["status"] == "PENDING"
    assert pending_plan["upper"] == 101  # original plan, not the spike high
    assert pending_plan["pending_side"] == "LONG"


@pytest.mark.asyncio
async def test_detector_legacy_ratio_excludes_spike_candle(monkeypatch):
    from VolumeSniper.volume_detector import KlineState, VolumeDetector

    monkeypatch.setattr(config, "STRATEGY_MODE", "legacy")
    monkeypatch.setattr(config, "VOLUME_SPIKE_RATIO", 5.0)
    found = []

    async def on_spike(symbol, state, ratio, delta):
        found.append(ratio)

    detector = VolumeDetector(on_spike)
    state = KlineState(symbol="BTCUSDT", avg_volume=100, avg_volume_usdt=10_000)
    for _ in range(config.VOLUME_CANDLES_AVG):
        state.ohlc_history.append((100, 101, 99, 100, 100))
        state.quote_volume_history.append(10_000)
    detector.states["BTCUSDT"] = state
    await detector.process_kline("BTCUSDT", kline(20, open_=100, high=102,
                                                  close=101, volume=1000, buy=600))
    assert found == [10.0]
