"""Point-in-time price-first scenarios for the v3 research engine."""

import pytest

from VolumeSniper.chart_reasoning_v3 import ChartReasoningEngine
from VolumeSniper.adaptive_scenarios import snapshot_from_v3
from VolumeSniper.market_structure import build_market_map


MINUTE = 60_000
FRAME = 15 * MINUTE
SEED_FRAMES = 32
EVENT_TS = SEED_FRAMES * FRAME
SYMBOL = "BTCUSDT"


def minute(i: int, *, open_: float = 100, high: float = 100.2,
           low: float = 99.8, close: float = 100,
           volume: float = 100, closed: bool = True) -> dict:
    return {
        "t": EVENT_TS + i * MINUTE, "o": open_, "h": high,
        "l": low, "c": close, "v": volume, "V": volume / 2,
        "x": closed,
    }


def higher_bars() -> list[dict]:
    """A range with a near 105 resistance and an opposing zone near 115."""
    bars = []
    for i in range(SEED_FRAMES):
        high = {3: 105, 8: 115, 13: 105.05, 18: 115.1}.get(i, 101)
        low = {5: 95, 10: 85, 15: 95.05, 20: 85.05}.get(i, 99)
        bars.append({
            "ts": i * FRAME, "open": 100, "high": high,
            "low": low, "close": 100, "volume": 1500,
            "close_ts": (i + 1) * FRAME - 1,
        })
    return bars


def engine_with_prior_map(tmp_path, *, extra_higher=None) -> ChartReasoningEngine:
    engine = ChartReasoningEngine(journal_path=tmp_path / "v3.jsonl")
    history = [
        {**minute(i), "t": EVENT_TS + i * MINUTE}
        for i in range(-20, 0)
    ]
    higher = higher_bars()
    if extra_higher:
        higher.extend(extra_higher)
    engine.seed(SYMBOL, history, higher_timeframes={"15m": higher})
    return engine


def _kind(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["kind"] == kind]


def test_warmup_plan_explicitly_abstains_without_closed_structure(tmp_path):
    engine = ChartReasoningEngine(journal_path=tmp_path / "v3.jsonl")
    events = engine.on_kline(SYMBOL, minute(0))
    plan = engine.plans[SYMBOL]

    assert events == []
    assert plan.decision == "NO_TRADE"
    assert plan.reason == "insufficient_closed_structure"
    assert plan.to_dict()["research_only"] is True


def test_plan_is_prospective_and_records_closed_structure_provenance(tmp_path):
    engine = engine_with_prior_map(tmp_path)
    plan = engine.plans[SYMBOL]

    assert plan.as_of_ms == EVENT_TS - 1
    assert plan.market_map.data_cutoffs["15m"] == EVENT_TS - 1
    assert plan.market_map.bar_counts["15m"] == SEED_FRAMES
    assert plan.market_map.nearest_resistance.high < 106
    assert {row["scenario"] for row in plan.scenarios} >= {
        "BREAKOUT_UP", "FAILED_BREAK_UP",
    }
    assert all(zone.confirmed_ts <= plan.as_of_ms for zone in plan.market_map.zones)

    events = engine.on_kline(SYMBOL, minute(0, high=106, low=99.5, close=105.5))
    candidate = _kind(events, "candidate")[0]
    assert candidate["plan_ts"] == EVENT_TS - 1 < candidate["event_ts"]
    assert candidate["market_context"]["as_of_ms"] == candidate["plan_ts"]
    assert candidate["market_context"]["data_cutoffs"]["15m"] <= candidate["plan_ts"]
    assert candidate["observed_at"] == EVENT_TS + MINUTE - 1
    assert candidate["research_only"] is True
    assert _kind(events, "confirmed") == []


def test_price_touch_is_watch_at_normal_volume_without_direction(tmp_path):
    engine = engine_with_prior_map(tmp_path)

    events = engine.on_kline(SYMBOL, minute(0, high=104.8, low=99.5,
                                             close=100.4, volume=100))

    watches = _kind(events, "watch")
    assert len(watches) == 1
    assert watches[0]["zone"]["side"] == "resistance"
    assert watches[0]["evidence"]["rvol"] == 1.0
    assert watches[0]["plan_ts"] < watches[0]["ts"]
    assert "direction_unresolved" in watches[0]["reason"]
    assert _kind(events, "candidate") == []
    assert _kind(events, "confirmed") == []


def test_shallow_retest_confirms_only_after_feasible_next_candle(tmp_path):
    engine = engine_with_prior_map(tmp_path)
    first = engine.on_kline(SYMBOL, minute(0, high=106, low=99.5, close=105.5))
    assert len(_kind(first, "candidate")) == 1
    assert _kind(first, "confirmed") == []

    confirmation_bar = minute(1, open_=105.5, high=106,
                              low=104.95, close=105.7)
    later = engine.on_kline(SYMBOL, confirmation_bar)

    confirmed = _kind(later, "confirmed")
    assert len(confirmed) == 1
    row = confirmed[0]
    assert (row["scenario"], row["direction"]) == ("BREAKOUT_UP", "LONG")
    assert row["plan_ts"] == EVENT_TS - 1
    assert row["event_ts"] == EVENT_TS
    assert row["observed_at"] == EVENT_TS + 2 * MINUTE - 1
    assert row["price"] == 105.7  # confirmation, not breakout-event price
    assert row["invalidation"] < row["zone"]["low"]
    assert row["feasibility"]["room_r"] > 1.5
    assert row["feasibility"]["next_zone"]["low"] > row["price"]
    assert row["market_context"]["as_of_ms"] == row["plan_ts"]
    assert row["market_context"]["nearest_resistance"]["high"] < 106
    snapshot = snapshot_from_v3(row, confirmation_bar)
    assert snapshot.context_asof_ms == row["observed_at"]
    assert snapshot.features["zone_touches"].observed_at_ms == row["plan_ts"]
    assert snapshot.features["body_to_range"].observed_at_ms == row["observed_at"]
    future_context = {**row["market_context"],
                      "data_cutoffs": {**row["market_context"]["data_cutoffs"],
                                       "15m": row["observed_at"]}}
    with pytest.raises(ValueError, match="future higher-timeframe"):
        snapshot_from_v3({**row, "market_context": future_context}, confirmation_bar)


def test_deep_sweep_reclaim_is_no_trade_even_if_close_outside(tmp_path):
    engine = engine_with_prior_map(tmp_path)
    engine.on_kline(SYMBOL, minute(0, high=106, low=99.5, close=105.5))

    later = engine.on_kline(SYMBOL, minute(1, open_=105.5, high=106,
                                             low=103.9, close=105.7))

    no_trade = _kind(later, "no_trade")
    assert len(no_trade) == 1
    assert no_trade[0]["reason"] == "deep_sweep_reclaim"
    assert no_trade[0]["plan_ts"] == EVENT_TS - 1
    assert _kind(later, "confirmed") == []
    assert SYMBOL not in engine.pending


def test_failed_break_requires_a_later_closed_no_reclaim_candle(tmp_path):
    engine = engine_with_prior_map(tmp_path)

    first = engine.on_kline(SYMBOL, minute(0, high=106, low=99,
                                             close=104.0))
    candidate = _kind(first, "candidate")
    assert len(candidate) == 1
    assert (candidate[0]["scenario"], candidate[0]["direction"]) == (
        "FAILED_BREAK_UP", "SHORT",
    )
    assert _kind(first, "confirmed") == []

    later = engine.on_kline(SYMBOL, minute(1, open_=104, high=104.8,
                                             low=103, close=103.9))
    confirmed = _kind(later, "confirmed")
    assert len(confirmed) == 1
    assert confirmed[0]["scenario"] == "FAILED_BREAK_UP"
    assert confirmed[0]["event_ts"] == EVENT_TS
    assert confirmed[0]["price"] == 103.9
    assert confirmed[0]["feasibility"]["room_r"] > 1.5


def test_gap_censors_pending_hypothesis_and_confirmed_outcome(tmp_path):
    pending_engine = engine_with_prior_map(tmp_path)
    pending_engine.on_kline(SYMBOL, minute(0, high=106, low=99.5,
                                            close=105.5))
    skipped = pending_engine.on_kline(SYMBOL, minute(2, open_=105.5,
                                                    high=106, low=104.95,
                                                    close=105.7))
    assert len(_kind(skipped, "censored_gap")) == 1
    assert _kind(skipped, "confirmed") == []
    assert SYMBOL not in pending_engine.pending
    assert len(pending_engine.history[SYMBOL]) == 1

    confirmed_engine = engine_with_prior_map(tmp_path)
    confirmed_engine.on_kline(SYMBOL, minute(0, high=106, low=99.5,
                                              close=105.5))
    confirmed_engine.on_kline(SYMBOL, minute(1, open_=105.5, high=106,
                                              low=104.95, close=105.7))
    skipped = confirmed_engine.on_kline(SYMBOL, minute(3, open_=105.7,
                                                      high=106, low=105,
                                                      close=105.8))
    assert len(_kind(skipped, "censored_gap")) == 1
    assert _kind(skipped, "outcome") == []
    assert confirmed_engine.outcomes.get(SYMBOL, []) == []


def test_future_higher_timeframe_bar_never_enters_prior_plan(tmp_path):
    future = {
        "ts": EVENT_TS, "open": 100, "high": 200,
        "low": 99, "close": 150, "volume": 1500,
        # A claimed source close time is ignored in favor of the timeframe.
        "close_ts": EVENT_TS - 1,
    }
    engine = engine_with_prior_map(tmp_path, extra_higher=[future])
    plan = engine.plans[SYMBOL]

    assert plan.as_of_ms == EVENT_TS - 1
    assert plan.market_map.data_cutoffs["15m"] == EVENT_TS - 1
    assert plan.market_map.bar_counts["15m"] == SEED_FRAMES
    assert all(bar["ts"] < EVENT_TS for bar in engine.higher[SYMBOL]["15m"])
    assert max(zone.high for zone in plan.market_map.zones) < 200
    assert plan.market_map.nearest_resistance.high < 106

    # The same as-of gate applies when REST warmup supplies 1h/4h rows later.
    engine.seed_higher_timeframes(SYMBOL, {
        "1h": [future], "4h": [future],
    })
    refreshed = engine.plans[SYMBOL]
    assert refreshed.market_map.bar_counts["1h"] == 0
    assert refreshed.market_map.bar_counts["4h"] == 0
    assert refreshed.market_map.data_cutoffs["1h"] is None
    assert refreshed.market_map.data_cutoffs["4h"] is None


def test_incremental_map_matches_full_closed_bar_rebuild(tmp_path):
    engine = ChartReasoningEngine(journal_path=tmp_path / "v3.jsonl")
    for i in range(400):
        price = 100 + (i % 47 - 23) * 0.03
        row = minute(i, open_=price, high=price + 0.2,
                     low=price - 0.2, close=price + 0.01)
        engine.on_kline(SYMBOL, row)
        if i % 37 != 0 and i not in (14, 15, 59, 60, 239, 240):
            continue
        plan = engine.plans[SYMBOL]
        rebuilt = build_market_map(
            SYMBOL, list(engine.history[SYMBOL]),
            higher_timeframes={tf: list(rows)
                               for tf, rows in engine.higher[SYMBOL].items()},
            as_of_ms=plan.as_of_ms,
        )
        assert plan.market_map.to_dict() == rebuilt.to_dict()
