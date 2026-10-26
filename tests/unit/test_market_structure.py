"""Point-in-time checks for the price-first structure map."""

from VolumeSniper.market_structure import aggregate_closed_bars, build_market_map


MINUTE = 60_000
BASE = 1_700_000_000_000 // (240 * MINUTE) * (240 * MINUTE)


def bar(i, timeframe, *, close=100.0, high=None, low=None, volume=10.0):
    width = {"1m": 1, "15m": 15, "1h": 60, "4h": 240}[timeframe] * MINUTE
    return {"ts": BASE + i * width, "open": close,
            "high": close + 1 if high is None else high,
            "low": close - 1 if low is None else low,
            "close": close, "volume": volume}


def minute_at(ts, close=100.0):
    return {"ts": ts, "open": close, "high": close + .2,
            "low": close - .2, "close": close, "volume": 1}


def test_aggregate_requires_full_closed_contiguous_bucket():
    minutes = [bar(i, "1m", close=100 + i / 100) for i in range(30)]
    first_close = BASE + 15 * MINUTE - 1
    out = aggregate_closed_bars(minutes, 15, as_of_ms=first_close)
    assert len(out) == 1
    assert out[0].ts == BASE
    assert out[0].close_ts == first_close
    assert out[0].volume == 150
    assert out[0].close == minutes[14]["close"]

    mid_second = BASE + 25 * MINUTE - 1
    assert len(aggregate_closed_bars(minutes, 15, as_of_ms=mid_second)) == 1
    assert len(aggregate_closed_bars(minutes, 15,
                                     as_of_ms=BASE + 30 * MINUTE - 1)) == 2

    # A missing minute must not be silently bridged into a made-up 15m bar.
    missing = [x for x in minutes if x["ts"] != BASE + 20 * MINUTE]
    assert aggregate_closed_bars(missing, 15,
                                 as_of_ms=BASE + 30 * MINUTE - 1) == ()


def test_explicit_higher_timeframes_exclude_current_unclosed_and_future_pivots():
    frame = [bar(i, "15m", high=101) for i in range(5)]
    frame[2]["high"] = 104
    frame[2]["close"] = 100
    as_of_before = BASE + 4 * 15 * MINUTE - 1  # only bars 0..3 closed
    one_minute = [minute_at(as_of_before - MINUTE + 1)]
    early = build_market_map("BTCUSDT", one_minute,
                             higher_timeframes={"15m": frame},
                             as_of_ms=as_of_before)
    assert early.data_cutoffs["15m"] == as_of_before
    assert not any(z.high == 104 for z in early.zones)

    as_of_after = BASE + 5 * 15 * MINUTE - 1
    one_minute = [minute_at(as_of_after - MINUTE + 1)]
    later = build_market_map("BTCUSDT", one_minute,
                             higher_timeframes={"15m": frame},
                             as_of_ms=as_of_after)
    resistance = next(z for z in later.zones if z.side == "resistance")
    assert resistance.high == 104
    assert resistance.origin_ts == frame[2]["ts"]
    assert resistance.confirmed_ts == as_of_after
    assert resistance.confirmed_ts <= later.as_of_ms
    assert later.to_dict()["zones"]


def test_repeated_swing_tests_create_provenanced_nearby_zones():
    rows = []
    for i in range(24):
        rows.append(bar(i, "15m", close=100,
                        high=103 if i % 4 == 2 else 101,
                        low=97 if i % 4 == 0 else 99))
    as_of = BASE + 24 * 15 * MINUTE - 1
    market = build_market_map("BTCUSDT", [minute_at(as_of - MINUTE + 1)],
                              higher_timeframes={"15m": rows}, as_of_ms=as_of)
    assert market.nearest_resistance is not None
    assert market.nearest_support is not None
    assert market.nearest_resistance.high == 103
    assert market.nearest_support.low == 97
    assert market.nearest_resistance.touches >= 3
    assert len(market.nearest_resistance.source_pivot_ts) >= 3
    assert all(ts <= as_of for ts in market.nearest_resistance.source_confirmed_ts)
    assert market.opposing_zone("LONG", 100) == market.nearest_resistance
    assert market.opposing_zone("SHORT", 100) == market.nearest_support
    assert market.regime == "range"


def test_trend_and_transition_are_price_structure_descriptions():
    as_of = BASE + 24 * 60 * MINUTE - 1
    last_minute = [minute_at(as_of - MINUTE + 1, close=111.5)]
    up = [bar(i, "1h", close=100 + i * .5,
              high=100 + i * .5 + .2, low=100 + i * .5 - .2)
          for i in range(24)]
    trend = build_market_map("BTCUSDT", last_minute,
                             higher_timeframes={"1h": up}, as_of_ms=as_of)
    assert trend.regime == "trend"
    assert trend.trend_direction == "up"
    assert trend.regime_timeframe == "1h"

    flat = [bar(i, "1h") for i in range(24)]
    flat[-1] = bar(23, "1h", close=103, high=103.5, low=99)
    transition = build_market_map("BTCUSDT", last_minute,
                                  higher_timeframes={"1h": flat}, as_of_ms=as_of)
    assert transition.regime == "transition"
    assert transition.trend_direction is None


def test_missing_4h_does_not_block_15m_map_and_gap_clears_old_levels():
    rows = [bar(i, "15m", high=103 if i == 2 else 101) for i in range(12)]
    rows.extend(bar(i, "15m") for i in range(14, 27))  # explicit 2-bar gap
    as_of = BASE + 27 * 15 * MINUTE - 1
    market = build_market_map("BTCUSDT", [minute_at(as_of - MINUTE + 1)],
                              higher_timeframes={"15m": rows}, as_of_ms=as_of)
    assert market.bar_counts["15m"] == 13
    assert market.regime_timeframe == "15m"
    assert market.bar_counts["4h"] == 0
    assert not any(z.high == 103 for z in market.zones)


def test_in_progress_1m_and_ambiguous_duplicate_are_excluded():
    minutes = [bar(i, "1m") for i in range(15)]
    minutes[14]["x"] = False
    assert aggregate_closed_bars(minutes, 15,
                                 as_of_ms=BASE + 15 * MINUTE - 1) == ()
    minutes[14]["x"] = True
    duplicate = dict(minutes[7], high=105)
    assert aggregate_closed_bars(minutes + [duplicate], 15,
                                 as_of_ms=BASE + 15 * MINUTE - 1) == ()
