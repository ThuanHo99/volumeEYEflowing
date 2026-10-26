"""Forward one-minute paper fills for generated research scenarios."""

import pytest

from VolumeSniper.adaptive_paper import AdaptivePaperBook
from VolumeSniper.adaptive_scenarios import (Condition, FeatureSnapshot,
                                             FeatureValue, ScenarioProposal)

MINUTE = 60_000


def bar(i, *, o=100.0, h=100.5, l=99.5, c=100.0, v=1.0):
    return {"t": i * MINUTE, "o": o, "h": h, "l": l,
            "c": c, "v": v, "x": True}


def proposal(action="LONG", *, hold=3, target_r=1.0):
    return ScenarioProposal(
        "candidate_a", 1, (Condition("rvol", ">=", 2.0),),
        action, 1.0, target_r, hold, 0,
    )


def snapshot():
    return FeatureSnapshot(MINUTE - 1,
                           {"rvol": FeatureValue(2.5, MINUTE - 1)})


def test_next_open_and_same_minute_ambiguous_stop_first():
    book = AdaptivePaperBook()
    assert book.on_closed_bar("BTCUSDT", bar(0)) == []
    book.queue("d1", "BTCUSDT", proposal(), snapshot(),
               invalidation=99, planned_target=None, tick_size=0.01)
    result = book.on_closed_bar("BTCUSDT", bar(1, h=102, l=98, c=101))[0]
    assert result.status == "evaluated"
    assert result.entry_ts == MINUTE
    assert result.entry == 100
    assert result.stop == 99 and result.target == 101
    assert result.exit == 99 and result.reason == "stop"
    assert result.ambiguous_same_minute
    assert result.net_pct == pytest.approx(-1.2)


def test_exact_tick_short_stop_and_adverse_stop_gap():
    short = AdaptivePaperBook()
    short.on_closed_bar("BTCUSDT", bar(0))
    short.queue("short", "BTCUSDT", proposal("SHORT", target_r=2.0),
                snapshot(), invalidation=100.3, planned_target=None,
                tick_size=0.1)
    stopped = short.on_closed_bar("BTCUSDT", bar(1, h=100.3, l=99.9))[0]
    assert stopped.stop == 100.3
    assert stopped.reason == "stop"

    long = AdaptivePaperBook()
    long.on_closed_bar("ETHUSDT", bar(0))
    long.queue("long", "ETHUSDT", proposal(target_r=2.0), snapshot(),
               invalidation=99, planned_target=None, tick_size=0.01)
    assert long.on_closed_bar("ETHUSDT", bar(1, h=100.5, l=99.5)) == []
    gapped = long.on_closed_bar("ETHUSDT", bar(2, o=98.5, h=99, l=98, c=98.6))[0]
    assert gapped.reason == "stop" and gapped.exit == 98.5


def test_zero_volume_entry_or_exit_censors_paper_position():
    book = AdaptivePaperBook()
    book.on_closed_bar("BTCUSDT", bar(0))
    book.queue("d1", "BTCUSDT", proposal(), snapshot(),
               invalidation=99, planned_target=None, tick_size=0.01)
    zero_entry = book.on_closed_bar("BTCUSDT", bar(1, v=0))[0]
    assert zero_entry.status == "censored"
    assert zero_entry.reason == "zero_volume_entry"

    book.queue("d2", "BTCUSDT", proposal(),
               FeatureSnapshot(2 * MINUTE - 1,
                               {"rvol": FeatureValue(2.5, 2 * MINUTE - 1)}),
               invalidation=99, planned_target=None, tick_size=0.01)
    assert book.on_closed_bar("BTCUSDT", bar(2, h=100.5, l=99.5)) == []
    zero_exit = book.on_closed_bar("BTCUSDT", bar(3, h=102, l=100, c=101, v=0))[0]
    assert zero_exit.reason == "zero_volume_exit"
    assert zero_exit.net_pct is None


def test_gap_censors_and_unmatched_or_future_decisions_rejected():
    book = AdaptivePaperBook()
    book.on_closed_bar("BTCUSDT", bar(0))
    with pytest.raises(ValueError, match="does not match"):
        book.queue("bad", "BTCUSDT", proposal(),
                   FeatureSnapshot(MINUTE - 1,
                                   {"rvol": FeatureValue(1, MINUTE - 1)}),
                   invalidation=99, planned_target=None, tick_size=0.01)
    book.queue("d1", "BTCUSDT", proposal(), snapshot(),
               invalidation=99, planned_target=None, tick_size=0.01)
    censored = book.on_closed_bar("BTCUSDT", bar(2))[0]
    assert censored.reason == "censored_gap"
    with pytest.raises(ValueError, match="duplicate or out-of-order"):
        book.on_closed_bar("BTCUSDT", bar(2))
