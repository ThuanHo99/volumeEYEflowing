"""Point-in-time replay and execution assumptions for the v3 shadow harness."""

from collections import deque

import pytest

from scripts.replay_chart_v3 import MINUTE_MS, replay_data


START = 1_700_000_000_000 // MINUTE_MS * MINUTE_MS


def bar(i, *, open_=100, high=101, low=99, close=100, closed=True):
    return {
        "start_ts": START + i * MINUTE_MS,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": 100, "is_closed": closed,
    }


def cache(rows):
    return {
        "BTC/USDT:USDT": {
            "1m": rows,
            # Deliberately impossible future levels. The replay must ignore
            # cached aggregate bars and feed only 1m observations.
            "15m": [{"start_ts": START, "high": 9999999}],
            "1h": [{"start_ts": START, "close": -1}],
        }
    }


class FakeEngine:
    """Prospective plan, watch, no-trade and one closed-candle confirmation."""

    def __init__(self):
        self.records = deque()
        self.seen = []

    def on_kline(self, symbol, k):
        assert k["x"] is True
        self.seen.append(k["t"])
        assert self.seen == sorted(self.seen)
        i = (k["t"] - START) // MINUTE_MS
        if i == 0:
            self.records.append({"kind": "plan", "symbol": symbol, "ts": k["t"]})
        elif i == 1:
            prior_close = START + MINUTE_MS - 1
            self.records.append({"kind": "watch", "symbol": symbol, "ts": k["t"],
                                 "plan_ts": prior_close, "observed_at": k["t"] + MINUTE_MS - 1})
            self.records.append({"kind": "no_trade", "symbol": symbol, "ts": k["t"],
                                 "reason": "unclear_retest"})
        elif i == 2:
            assert self.seen == [START, START + MINUTE_MS, START + 2 * MINUTE_MS]
            self.records.append({
                "kind": "confirmed", "symbol": symbol, "ts": k["t"],
                "event_ts": START + MINUTE_MS,
                "observed_at": k["t"] + MINUTE_MS - 1,
                "plan_ts": START + MINUTE_MS - 1,
                "direction": "LONG", "scenario": "BREAKOUT",
                "price": k["c"], "invalidation": 90,
            })
        return []


def test_next_open_entry_5_and_15_candles_cost_and_abstention():
    rows = [bar(i) for i in range(19)]
    rows[3] = bar(3, open_=105, high=110, low=104, close=109)
    for i in range(4, 19):
        rows[i] = bar(i, open_=109, high=111, low=108, close=110)
    report = replay_data(cache(rows), 0.2, {"v3": FakeEngine})
    engine = report["engines"]["v3"]
    assert report["closed_1m_candles"] == 19
    assert engine["event_counts"]["watch"] == 1
    assert engine["event_counts"]["no_trade"] == 1
    assert engine["event_counts"]["confirmed"] == 1
    assert engine["prospective_checks"] == {"valid": 2}
    for horizon in ("5", "15"):
        outcome = engine["horizons"][horizon]
        assert outcome["evaluated"] == 1
        assert outcome["stopped"] == 0
        assert outcome["gross_mean_pct"] == pytest.approx((110 - 105) / 105 * 100)
        assert outcome["net_mean_pct_assumed"] == pytest.approx((110 - 105) / 105 * 100 - 0.2)


def test_cached_future_higher_timeframes_cannot_change_decisions():
    rows = [bar(i) for i in range(19)]
    source = cache(rows)
    changed = cache(rows)
    changed["BTC/USDT:USDT"]["15m"] = [{"start_ts": START, "close": 42}]
    changed["BTC/USDT:USDT"]["4h"] = [{"start_ts": START, "high": 1e100}]
    assert replay_data(source, 0.2, {"v3": FakeEngine}) == replay_data(
        changed, 0.2, {"v3": FakeEngine})


def test_intrabar_stop_and_gap_censoring():
    rows = [bar(i) for i in range(19)]
    rows[3] = bar(3, open_=100, high=102, low=89, close=101)
    result = replay_data(cache(rows), 0.2, {"v3": FakeEngine})
    outcome = result["engines"]["v3"]["horizons"]["5"]
    assert outcome["stopped"] == 1
    assert outcome["gross_mean_pct"] == pytest.approx(-10.0)
    assert outcome["net_mean_pct_assumed"] == pytest.approx(-10.2)

    rows_without_next_bar = [c for i, c in enumerate(rows) if i != 3]
    result = replay_data(cache(rows_without_next_bar), 0.2, {"v3": FakeEngine})
    outcome = result["engines"]["v3"]["horizons"]["5"]
    assert outcome["evaluated"] == 0
    assert outcome["excluded_or_censored"] == {"censored_gap": 1}

    # A stop hit before a later cache gap is a known outcome, not censored.
    early_stop_then_gap = [c for i, c in enumerate(rows) if i != 8]
    result = replay_data(cache(early_stop_then_gap), 0.2, {"v3": FakeEngine})
    outcome = result["engines"]["v3"]["horizons"]["15"]
    assert outcome["evaluated"] == 1
    assert outcome["stopped"] == 1


def test_late_plan_is_reported_and_excluded_from_outcomes():
    class LatePlan(FakeEngine):
        def on_kline(self, symbol, k):
            super().on_kline(symbol, k)
            if k["t"] == START + 2 * MINUTE_MS:
                self.records[-1]["plan_ts"] = START + MINUTE_MS
            return []

    report = replay_data(cache([bar(i) for i in range(19)]), 0.2,
                         {"v3": LatePlan})
    engine = report["engines"]["v3"]
    assert engine["prospective_checks"] == {"valid": 1, "late_plan": 1}
    assert engine["horizons"]["5"]["evaluated"] == 0


def test_unclosed_rows_are_not_fed_and_cost_is_validated():
    rows = [bar(i) for i in range(19)] + [bar(19, closed=False)]
    report = replay_data(cache(rows), 0.2, {"v3": FakeEngine})
    assert report["skipped_input_rows"] == {"unclosed": 1}
    with pytest.raises(ValueError, match="cost_pct"):
        replay_data(cache(rows), -0.1, {"v3": FakeEngine})
