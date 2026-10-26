"""v3 shadow must receive closed candles without affecting live alert paths."""

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from VolumeSniper import config
from VolumeSniper.scanner import Scanner


BASE = 1_700_000_000_000 // 60_000 * 60_000


def kline(i, closed=True):
    return {
        "t": BASE + i * 60_000, "o": "100", "h": "101", "l": "99",
        "c": "100", "v": "100", "q": "10000", "x": closed,
    }


class ShadowEngine:
    def __init__(self):
        self.seeded = []
        self.htf_seeded = []
        self.received = []
        self.records = deque()

    def seed(self, symbol, raw1m):
        self.seeded.append((symbol, [row["t"] for row in raw1m]))

    def seed_higher_timeframes(self, symbol, raw_by_tf):
        self.htf_seeded.append((symbol, raw_by_tf))

    def on_kline(self, symbol, k):
        self.received.append((symbol, k["t"]))
        return [{"kind": "confirmed", "symbol": symbol, "ts": k["t"],
                 "observed_at": k["t"] + 59_999, "direction": "LONG"}]


@pytest.mark.asyncio
async def test_adaptive_paper_observes_chart_without_suppressing_v3_events():
    scanner = object.__new__(Scanner)
    scanner.context = None
    scanner.chart_v3 = ShadowEngine()
    scanner.chart_v3.plans = {"BTCUSDT": SimpleNamespace(market_map="asof-map")}
    scanner.chart_v3.history = {"BTCUSDT": deque([kline(0), kline(1)])}
    scanner._v3_symbols = {"BTCUSDT"}
    scanner._v3_confirmed = 0
    scanner._pending_spikes = {}
    scanner._msg_count = 0
    scanner.detector = type("D", (), {"process_kline": AsyncMock()})()
    scanner.ui_queue = asyncio.Queue()
    observed = []

    class Paper:
        proposer = None

        def on_bar(self, symbol, bar, market_map, prior, *, tick_size,
                   derivatives=None):
            observed.append((symbol, bar["t"], market_map,
                             [item["t"] for item in prior], tick_size))
            return {"paper_queued": [], "settled": [], "plans": {},
                    "snapshot": SimpleNamespace(context_asof_ms=bar["t"] + 59_999)}

    scanner.adaptive_shadow = Paper()
    scanner._adaptive_ticks = {"BTCUSDT": 0.01}
    scanner._adaptive_prompt_symbol = "BTCUSDT"
    await scanner._handle_message(json.dumps({
        "data": {"e": "kline", "s": "BTCUSDT", "k": kline(1)}}))
    assert observed == [("BTCUSDT", BASE + 60_000, "asof-map", [BASE], 0.01)]
    assert scanner.ui_queue.get_nowait()["type"] == "chart_v3_event"


@pytest.mark.asyncio
async def test_shadow_processes_selected_closed_kline_without_telegram():
    scanner = object.__new__(Scanner)
    scanner.context = None
    scanner.chart_v3 = ShadowEngine()
    scanner._v3_symbols = {"BTCUSDT"}
    scanner._v3_confirmed = 0
    scanner._pending_spikes = {}
    scanner._msg_count = 0
    scanner.detector = type("D", (), {"process_kline": AsyncMock()})()
    scanner.telegram = type("T", (), {"send_context_alert": AsyncMock(),
                                       "send_alert": AsyncMock()})()
    scanner.ui_queue = asyncio.Queue()

    def message(symbol, k):
        return json.dumps({"stream": "btc", "data": {"e": "kline", "s": symbol, "k": k}})

    await scanner._handle_message(message("BTCUSDT", kline(1, closed=False)))
    await scanner._handle_message(message("ETHUSDT", kline(1)))
    assert scanner.chart_v3.received == []
    await scanner._handle_message(message("BTCUSDT", kline(1)))
    assert scanner.chart_v3.received == [("BTCUSDT", BASE + 60_000)]
    assert scanner._v3_confirmed == 1
    item = scanner.ui_queue.get_nowait()
    assert item["type"] == "chart_v3_event"
    assert item["research_only"] is True
    assert item["event"]["kind"] == "confirmed"
    scanner.telegram.send_context_alert.assert_not_awaited()
    scanner.telegram.send_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_warmup_is_bounded_and_rest_fetch_uses_prior_cutoff(monkeypatch):
    scanner = object.__new__(Scanner)
    scanner.chart_v3 = ShadowEngine()
    scanner._v3_symbols = set()
    scanner._v3_warmup_task = None
    scanner._symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    state = lambda rows: type("S", (), {"context_history": deque(rows)})()
    scanner.detector = type("D", (), {"states": {
        "BTCUSDT": state([kline(0), kline(1)]),
        "ETHUSDT": state([kline(0)]),
        "SOLUSDT": state([kline(0)]),
    }})()
    monkeypatch.setattr(config, "V3_MAX_SYMBOLS", 2)
    calls = []

    async def fake_fetch(symbols, base_url, *, limit, as_of_ms, max_symbols):
        calls.append((symbols, base_url, limit, as_of_ms, max_symbols))
        return {s: {"15m": [{"ts": BASE - 900_000, "close_ts": BASE - 1}]}
                for s in symbols}

    monkeypatch.setattr("VolumeSniper.htf_feed.fetch_closed_higher_timeframes", fake_fetch)
    scanner._start_v3_warmup()
    assert scanner._v3_symbols == {"BTCUSDT", "ETHUSDT"}
    assert scanner.chart_v3.seeded == [
        ("BTCUSDT", [BASE, BASE + 60_000]), ("ETHUSDT", [BASE])]
    await scanner._v3_warmup_task
    assert calls == [(["BTCUSDT", "ETHUSDT"], config.BINANCE_REST_URL,
                      100, BASE + 59_999, 2)]
    assert {s for s, _ in scanner.chart_v3.htf_seeded} == scanner._v3_symbols


@pytest.mark.asyncio
async def test_v3_derivatives_enrichment_is_timestamped_after_event_and_isolated():
    scanner = object.__new__(Scanner)
    scanner.chart_v3 = ShadowEngine()
    scanner.context = type("C", (), {"records": deque()})()
    snapshot = {"metrics": {"open_interest": {"status": "ok"}}}
    scanner.derivatives = type("D", (), {
        "refresh": AsyncMock(return_value=snapshot),
    })()
    scanner._derivatives_tasks = set()

    scanner._schedule_derivatives_refresh("BTCUSDT", "v3_candidate", BASE)
    await asyncio.gather(*scanner._derivatives_tasks)

    assert len(scanner.chart_v3.records) == 1
    row = scanner.chart_v3.records[0]
    assert row["event_ts"] == BASE
    assert row["observed_at"] >= BASE
    assert row["post_event"] is True
    assert row["snapshot"] == snapshot
    assert list(scanner.context.records) == []
