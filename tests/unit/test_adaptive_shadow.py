"""Closed-candle shadow loop works without a named v3 event trigger."""

from dataclasses import replace

import pytest

from VolumeSniper.adaptive_observation import observe_chart
from VolumeSniper.adaptive_scenarios import (Condition, ScenarioLedger,
                                             ScenarioProposal)
from VolumeSniper.adaptive_shadow import AdaptiveShadowLoop
from VolumeSniper.local_scenario_proposer import ProposalResult
from VolumeSniper.market_structure import MarketMap, StructureZone

MINUTE = 60_000


def bar(i, *, high=100.2, low=99.8, close=100):
    return {"t": i * MINUTE, "o": 100, "h": high,
            "l": low, "c": close, "v": 10, "V": 5, "x": True}


def zone(side, low, high):
    return StructureZone(side, low, high, "15m", 0, 0, 0,
                         2, (0,), (0,), (0,))


def market_map(i):
    support = zone("support", 99, 99.5)
    resistance = zone("resistance", 102, 102.5)
    clock = (i + 1) * MINUTE - 1
    return MarketMap("BTCUSDT", clock, 100, "range", None, "15m",
                     (support, resistance), support, resistance,
                     {"1m": clock, "15m": 0}, {"1m": i + 1, "15m": 12}, ())


def test_forward_shadow_records_decision_then_stop_first_loss(tmp_path):
    path = tmp_path / "adaptive.jsonl"
    proposal = ScenarioProposal(
        "fresh_chart", 1, (Condition("regime_range", ">=", 1),),
        "LONG", 1, 2, 15, MINUTE)
    ScenarioLedger(path).append_proposal(proposal)
    loop = AdaptiveShadowLoop(path, asof_ms=0)
    first = bar(0)
    assert loop.on_bar("BTCUSDT", first, market_map(0), [],
                       tick_size=0.01)["settled"] == []
    opened = loop.on_bar("BTCUSDT", bar(1), market_map(1), [first],
                         tick_size=0.01)
    assert len(opened["paper_queued"]) == 1
    assert opened["recommendation"]["scenario"] is None
    assert [row["kind"] for row in loop.ledger.read_rows()] == ["proposal", "decision"]

    settled = loop.on_bar("BTCUSDT", bar(2, high=103, low=98),
                          market_map(2), [first, bar(1)], tick_size=0.01)
    result = settled["settled"][0]
    assert result.status == "evaluated" and result.reason == "stop"
    assert result.ambiguous_same_minute
    rows = loop.ledger.read_rows()
    assert [row["kind"] for row in rows] == ["proposal", "decision", "outcome"]
    assert rows[-1]["net_pct"] < 0
    assert loop.ranker.score(proposal).samples == 1


def test_restart_censors_unobserved_position_instead_of_making_up_fill(tmp_path):
    path = tmp_path / "adaptive.jsonl"
    proposal = ScenarioProposal(
        "fresh_chart", 1, (Condition("regime_range", ">=", 1),),
        "LONG", 1, 2, 15, MINUTE)
    ScenarioLedger(path).append_proposal(proposal)
    first = AdaptiveShadowLoop(path, asof_ms=0)
    first.on_bar("BTCUSDT", bar(0), market_map(0), [], tick_size=0.01)
    first.on_bar("BTCUSDT", bar(1), market_map(1), [bar(0)], tick_size=0.01)
    second = AdaptiveShadowLoop(path, asof_ms=3 * MINUTE)
    assert second.ledger.read_rows()[-1]["kind"] == "censored"
    assert second.ranker.score(proposal).samples == 0


def test_observation_is_action_independent_and_rejects_future_context():
    observation = observe_chart(market_map(1), bar(1), [bar(0)],
                                tick_size=0.01)
    assert set(observation.plans) == {"LONG", "SHORT"}
    assert observation.snapshot.features["regime_range"].value == 1
    assert observation.snapshot.features["long_room_r"].value > 1
    assert observation.snapshot.features["short_room_r"].value > 0
    future = replace(market_map(1), data_cutoffs={"1m": 2 * MINUTE - 1,
                                                    "15m": 3 * MINUTE - 1})
    with pytest.raises(ValueError, match="future"):
        observe_chart(future, bar(1), [bar(0)], tick_size=0.01)
    with pytest.raises(ValueError, match="future"):
        observe_chart(market_map(1), bar(1), [bar(1)], tick_size=0.01)


def test_derivative_facts_require_predecision_fetch_and_source_clocks():
    asof = 2 * MINUTE - 1
    previous = MINUTE - 1
    derivatives = {"as_of": asof, "metrics": {
        "funding": {"status": "ok", "observed_at": previous,
                    "fetched_at": previous,
                    "value": {"latest_funding_rate": 0.001,
                              "index_price": 100}},
        "open_interest_history": {
            "status": "ok", "observed_at": previous,
            "fetched_at": previous,
            "value": {"notional_change_pct": 2,
                      "notional_usd_approx": 1000,
                      "cmc_circulating_supply": 20}},
        "order_book": {"status": "ok", "observed_at": previous,
                       "fetched_at": asof + 1,
                       "value": {"spread_bps": 2,
                                 "top20_imbalance": 0.5}},
    }}
    found = observe_chart(market_map(1), bar(1), [bar(0)],
                          tick_size=0.01, derivatives=derivatives).snapshot.features
    assert found["funding_rate_pct"].value == pytest.approx(0.1)
    assert found["oi_change_pct"].observed_at_ms == previous
    assert found["oi_to_market_cap_ratio"].value == pytest.approx(0.5)
    assert "spread_bps" not in found and "depth_imbalance" not in found
    with pytest.raises(ValueError, match="future"):
        observe_chart(market_map(1), bar(1), [bar(0)], tick_size=0.01,
                      derivatives={**derivatives, "as_of": asof + 1})


def test_model_revises_version_after_mature_loss_and_rotates_active_slot(tmp_path):
    path = tmp_path / "adaptive.jsonl"
    original = ScenarioProposal(
        "fresh_chart", 1, (Condition("regime_range", ">=", 1),),
        "LONG", 1, 2, 15, MINUTE)
    ScenarioLedger(path).append_proposal(original)
    loop = AdaptiveShadowLoop(path, asof_ms=0, max_proposals=1)
    first, second = bar(0), bar(1)
    loop.on_bar("BTCUSDT", first, market_map(0), [], tick_size=0.01)
    loop.on_bar("BTCUSDT", second, market_map(1), [first], tick_size=0.01)
    loop.on_bar("BTCUSDT", bar(2, high=103, low=98), market_map(2),
                [first, second], tick_size=0.01)
    observed_feedback = []

    class RevisingModel:
        def propose(self, snapshot, *, created_at_ms, recent_outcomes):
            observed_feedback.extend(recent_outcomes)
            revised = ScenarioProposal(
                "fresh_chart", 1,
                (Condition("regime_range", ">=", 1),
                 Condition("long_room_r", ">=", 2)),
                "LONG", 1, 2, 15, created_at_ms,
                "Retry only with more room after a stop")
            return ProposalResult("ok", (revised,), (revised.rationale,))

    loop.proposer = RevisingModel()
    snapshot = observe_chart(market_map(2), bar(2, high=103, low=98),
                             [first, second], tick_size=0.01).snapshot
    result = loop.propose(snapshot, created_at_ms=4 * MINUTE)
    assert result.proposals[0].key == ("fresh_chart", 2)
    assert observed_feedback[0]["exit_reason"] == "stop"
    assert observed_feedback[0]["conditions"][0]["feature"] == "regime_range"
    assert observed_feedback[0]["entry_features"]["long_room_r"] > 1
    assert loop.active == {("fresh_chart", 2)}
    assert [row["kind"] for row in loop.ledger.read_rows()][-2:] == [
        "retired", "proposal"]
