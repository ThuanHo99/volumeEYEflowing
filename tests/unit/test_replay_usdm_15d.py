"""Conservative one-minute execution and point-in-time TP/SL tests."""

from scripts.replay_usdm_15d import _parse_row, read_symbol, simulate
from scripts.usdm_15d_data import DAY_MS


MINUTE = 60_000


def bar(i, o=100.0, h=101.0, l=99.0, c=100.0):
    return {"t": i * MINUTE, "o": o, "h": h, "l": l, "c": c,
            "v": 1.0, "q": 100.0, "x": True}


def signal(direction="LONG", stop=99.0, target=None):
    return {"confirmed_ts": 0, "direction": direction,
            "invalidation": stop, "planned_target": target}


def test_same_minute_target_and_stop_assumes_stop_first():
    rows = [bar(0), bar(1, 100, 102, 98, 101)]
    result = simulate(rows, {r["t"]: i for i, r in enumerate(rows)},
                      signal(), (1.0, 1.0, 1), 0.01,
                      period_end_ms=3 * MINUTE)
    assert result["status"] == "evaluated"
    assert result["exit_reason"] == "stop"
    assert result["ambiguous_same_minute"] is True
    assert result["gross_pct"] < 0


def test_stop_gap_fills_at_adverse_open_and_tp_is_capped_by_prior_zone():
    rows = [bar(0), bar(1, 100, 100.5, 99.5, 100),
            bar(2, 98.5, 99, 98, 98.6)]
    index = {r["t"]: i for i, r in enumerate(rows)}
    stopped = simulate(rows, index, signal(), (1.0, 2.0, 2), 0.01,
                       period_end_ms=4 * MINUTE)
    assert stopped["exit_reason"] == "stop"
    assert stopped["exit"] == 98.5

    zone = simulate(rows[:2], index, signal(target=100.8), (1.0, 2.0, 1), 0.01,
                    period_end_ms=3 * MINUTE)
    assert zone["target"] <= 100.8 + 1e-9


def test_missing_entry_or_future_boundary_is_censored():
    gap = [bar(0), bar(2)]
    assert simulate(gap, {r["t"]: i for i, r in enumerate(gap)},
                    signal(), (1.0, 1.5, 2), 0.01,
                    period_end_ms=4 * MINUTE)["status"] == "censored_gap"
    contiguous = [bar(0), bar(1)]
    assert simulate(contiguous, {r["t"]: i for i, r in enumerate(contiguous)},
                    signal(), (1.0, 1.5, 2), 0.01,
                    period_end_ms=MINUTE)["status"] == "censored_boundary"


def test_kline_parser_rejects_unclosed_and_impossible_rows():
    good = [0, "100", "101", "99", "100", "10", 59_999,
            "1000", 7, "5", "500", 0]
    assert _parse_row(good, 0, MINUTE) is not None
    assert _parse_row(good, MINUTE, 2 * MINUTE) is None
    assert _parse_row([*good[:6], 60_000, *good[7:]], 0, MINUTE) is None
    impossible = list(good)
    impossible[3] = "102"
    assert _parse_row(impossible, 0, MINUTE) is None


def test_zero_volume_exit_is_not_counted_as_executable():
    rows = [bar(0), bar(1, 100, 100.5, 99.5, 100),
            {**bar(2, 100, 102, 100, 101), "v": 0}]
    result = simulate(rows, {r["t"]: i for i, r in enumerate(rows)},
                      signal(), (1.0, 1.0, 2), 0.01,
                      period_end_ms=4 * MINUTE)
    assert result["status"] == "censored_no_liquidity_exit"


def test_settled_contract_has_no_missing_source_after_delivery(tmp_path):
    manifest = {"start_ms": 0, "end_ms_exclusive": 2 * DAY_MS}
    meta = {"symbol": "ENDEDUSDT", "onboard_ms": 0,
            "delivery_ms": DAY_MS}
    _, coverage = read_symbol(tmp_path, manifest, meta)
    assert coverage["missing_source_days"] == ["1970-01-01"]


def test_exact_tick_high_triggers_short_stop():
    rows = [bar(0), bar(1, 100, 100.3, 99.9, 100)]
    result = simulate(rows, {r["t"]: i for i, r in enumerate(rows)},
                      signal(direction="SHORT", stop=100.3),
                      (1.0, 2.0, 1), 0.1,
                      period_end_ms=3 * MINUTE)
    assert result["status"] == "evaluated"
    assert result["stop"] == 100.3
    assert result["exit_reason"] == "stop"
