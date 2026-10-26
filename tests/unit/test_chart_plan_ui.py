"""The v3 dashboard endpoint exposes research plans without emitting alerts."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from ui.web_dashboard import (SCANNER, SIGNAL_BUFFER_KEY, WS_CLIENTS,
                              chart_plan_handler, consume_for_ui)


class StubPlan:
    def to_dict(self):
        return {
            "symbol": "BTCUSDT", "as_of_ms": 1_700_000_000_000,
            "decision": "WAIT", "research_only": False,
            "market_map": {
                "price": 100, "regime": "range", "trend_direction": None,
                "nearest_support": {"low": 98, "high": 99, "timeframe": "1h"},
                "nearest_resistance": {"low": 101, "high": 102, "timeframe": "1h"},
                "zones": [], "data_cutoffs": {"1h": 1_700_000_000_000},
                "quality_issues": [],
            },
            "scenarios": [{"scenario": "BREAKOUT_UP", "trigger": "close and hold",
                           "invalidation": "lose 101", "expiry_candles": 4}],
        }


def request_for(path, plans=None, *, enabled=True):
    app = web.Application()
    engine = SimpleNamespace(plans=plans or {}) if enabled else None
    app[SCANNER] = SimpleNamespace(chart_v3=engine)
    return make_mocked_request("GET", path, app=app)


@pytest.mark.asyncio
async def test_chart_plan_endpoint_serializes_prospective_plan_as_research_only():
    response = await chart_plan_handler(request_for(
        "/api/chart-plan?symbol=BTC/USDT", {"BTCUSDT": StubPlan()}))
    body = json.loads(response.text)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert body["symbol"] == "BTCUSDT"
    assert body["research_only"] is True
    assert body["decision"] == "WAIT"
    assert body["market_map"]["nearest_resistance"]["timeframe"] == "1h"
    assert body["scenarios"][0]["invalidation"] == "lose 101"


@pytest.mark.asyncio
async def test_chart_plan_endpoint_validates_symbol_and_handles_missing_state():
    invalid = await chart_plan_handler(request_for("/api/chart-plan?symbol=%3Cscript%3E"))
    assert invalid.status == 400
    missing = await chart_plan_handler(request_for("/api/chart-plan?symbol=BTC"))
    assert missing.status == 404
    assert json.loads(missing.text)["research_only"] is True
    disabled = await chart_plan_handler(request_for(
        "/api/chart-plan?symbol=BTC", enabled=False))
    assert disabled.status == 503
    assert json.loads(disabled.text)["status"] == "disabled"


@pytest.mark.asyncio
async def test_chart_event_reaches_research_client_without_entering_signal_buffer():
    class Socket:
        def __init__(self):
            self.messages = []

        async def send_str(self, message):
            self.messages.append(json.loads(message))

    socket = Socket()
    app = web.Application()
    app[WS_CLIENTS] = {socket}
    app[SIGNAL_BUFFER_KEY] = []
    queue = asyncio.Queue()
    task = asyncio.create_task(consume_for_ui(queue, app))
    event = {"type": "chart_v3_event", "symbol": "BTCUSDT", "kind": "WATCH",
             "timestamp": 1_700_000_000_000, "event": "resistance touched",
             "research_only": True}
    try:
        await queue.put(event)
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert socket.messages == [event]
    assert app[SIGNAL_BUFFER_KEY] == []
