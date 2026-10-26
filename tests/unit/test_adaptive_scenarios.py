"""Prospective contracts for an adaptive shadow scenario layer."""

from dataclasses import FrozenInstanceError

import pytest

from VolumeSniper.adaptive_scenarios import (
    Condition, FeatureSnapshot, FeatureValue, ScenarioLedger,
    ScenarioProposal, ScenarioRanker, evaluate,
)


def proposal(version=1, action="LONG", created=100, threshold=2.0):
    return ScenarioProposal(
        "held_break", version, (Condition("rvol", ">=", threshold),),
        action, 1.0, 2.0, 15, created,
    )


def snapshot(value=2.5, observed=199, asof=199):
    return FeatureSnapshot(asof, {"rvol": FeatureValue(value, observed)})


def test_proposal_is_immutable_and_rejects_generated_code_or_unbounded_risk():
    item = proposal()
    assert item.key == ("held_break", 1)
    with pytest.raises(FrozenInstanceError):
        item.action = "SHORT"
    with pytest.raises(ValueError, match="allowlisted"):
        Condition("future_return", ">", 0)
    with pytest.raises(ValueError, match="allowlisted"):
        Condition("rvol", "__import__", 1)
    with pytest.raises(ValueError, match="bounded"):
        ScenarioProposal("held_break", 1, item.conditions, "LONG", 10, 2, 15, 100)
    with pytest.raises(ValueError, match="1..240"):
        ScenarioProposal("held_break", 1, item.conditions, "LONG", 1, 2, 1000, 100)
    with pytest.raises(ValueError, match="action"):
        ScenarioProposal("held_break", 1, item.conditions, "BUY", 1, 2, 15, 100)


def test_evaluation_uses_only_allowed_point_in_time_numeric_features():
    item = proposal()
    assert evaluate(item, snapshot(), 200)
    assert not evaluate(item, snapshot(value=1.5), 200)
    assert not evaluate(item, FeatureSnapshot(199, {}), 200)
    with pytest.raises(ValueError, match="future feature"):
        snapshot(observed=201, asof=199)
    with pytest.raises(ValueError, match="future"):
        evaluate(item, snapshot(), 198)
    with pytest.raises(ValueError, match="did not exist"):
        evaluate(proposal(created=201), snapshot(), 200)
    with pytest.raises(ValueError, match="unallowlisted"):
        FeatureSnapshot(200, {"next_return": FeatureValue(1, 199)})


def test_ledger_is_append_only_versioned_and_reloadable(tmp_path):
    path = tmp_path / "scenario_events.jsonl"
    ledger = ScenarioLedger(path)
    item = proposal()
    ledger.append_proposal(item)
    with pytest.raises(ValueError, match="already recorded"):
        ledger.append_proposal(item)
    decision = ledger.append_decision("d1", "BTCUSDT", item, snapshot(), 200)
    assert decision["context_asof_ms"] == 199
    assert decision["features"]["rvol"] == {"value": 2.5, "observed_at_ms": 199}
    with pytest.raises(ValueError, match="already recorded"):
        ledger.append_decision("d1", "BTCUSDT", item, snapshot(), 200)
    with pytest.raises(ValueError, match="did not mature"):
        ledger.append_outcome("d1", 200, -0.2)
    outcome = ledger.append_outcome("d1", 300, -0.2)
    assert outcome["scenario_id"] == "held_break"
    with pytest.raises(ValueError, match="already recorded"):
        ledger.append_outcome("d1", 301, 1.0)

    reloaded = ScenarioLedger(path)
    assert [row["kind"] for row in reloaded.read_rows()] == [
        "proposal", "decision", "outcome"]
    with pytest.raises(ValueError, match="already recorded"):
        reloaded.append_decision("d1", "BTCUSDT", item, snapshot(), 200)
    with pytest.raises(ValueError, match="already recorded"):
        reloaded.append_proposal(proposal(version=1, created=201))
    second = proposal(version=2, created=301, threshold=3.0)
    reloaded.append_proposal(second)
    with pytest.raises(ValueError, match="immutable"):
        reloaded.append_decision("d2", "BTCUSDT", proposal(version=2, created=301),
                                 snapshot(), 400)


def test_ranker_waits_for_maturity_and_samples_then_abstains_on_loss():
    item = proposal()
    ranker = ScenarioRanker(min_samples=2, prior_strength=2, prior_mean_pct=0)
    first = {"kind": "outcome", "decision_id": "d1", "scenario_id": "held_break",
             "version": 1, "action": "LONG", "decision_at_ms": 200,
             "matured_at_ms": 300, "net_pct": 1.0}
    assert not ranker.update(first, asof_ms=299)
    assert ranker.score(item).samples == 0
    assert ranker.choose([item], snapshot(), 200) is None
    assert ranker.update(first, asof_ms=300)
    assert not ranker.update(first, asof_ms=300)
    assert ranker.score(item).samples == 1
    second = {**first, "decision_id": "d2", "net_pct": -2.0}
    assert ranker.update(second, asof_ms=300)
    score = ranker.score(item)
    assert score.samples == 2 and score.estimated_net_pct < 0
    assert not score.eligible
    assert ranker.choose([item], snapshot(), 300) is None


def test_ranker_version_isolation_shrinkage_and_no_trade_veto():
    first = proposal()
    second = proposal(version=2, created=100)
    veto = proposal(version=3, action="NO_TRADE", created=100)
    ranker = ScenarioRanker(min_samples=2, prior_strength=2)
    for n in range(2):
        row = {"kind": "outcome", "decision_id": f"d{n}",
               "scenario_id": "held_break", "version": 1,
               "action": "LONG", "decision_at_ms": 200,
               "matured_at_ms": 300, "net_pct": 1.0}
        ranker.update(row, asof_ms=300)
    score = ranker.score(first)
    assert score.samples == 2 and score.estimated_net_pct == 0.5
    assert ranker.score(second).samples == 0
    assert ranker.choose([first, second], snapshot(), 300) == first
    assert ranker.choose([first, veto], snapshot(), 300) is None


def test_ledger_rejects_nonmatching_or_future_decisions(tmp_path):
    ledger = ScenarioLedger(tmp_path / "events.jsonl")
    item = proposal()
    ledger.append_proposal(item)
    with pytest.raises(ValueError, match="did not match"):
        ledger.append_decision("d1", "BTCUSDT", item, snapshot(value=1.0), 200)
    with pytest.raises(ValueError, match="future"):
        ledger.append_decision("d1", "BTCUSDT", item, snapshot(), 198)
    assert [row["kind"] for row in ledger.read_rows()] == ["proposal"]


def test_one_lucky_paper_trade_does_not_promote_scenario():
    item = proposal()
    ranker = ScenarioRanker(min_samples=30, prior_strength=30)
    for n in range(30):
        ranker.update({
            "kind": "outcome", "decision_id": f"lucky-{n}",
            "scenario_id": item.scenario_id, "version": item.version,
            "action": "LONG", "decision_at_ms": 200,
            "matured_at_ms": 300, "net_pct": 5.0 if n == 0 else 0.0,
        }, asof_ms=300)
    score = ranker.score(item)
    assert score.samples == 30
    assert score.estimated_net_pct > 0
    assert score.lower_confidence_pct < 0
    assert ranker.choose([item], snapshot(), 300) is None


def test_execution_plan_and_censored_paper_result_are_auditable(tmp_path):
    ledger = ScenarioLedger(tmp_path / "paper.jsonl")
    item = proposal()
    ledger.append_proposal(item)
    plan = {"invalidation": 99.0, "planned_target": 103.0,
            "tick_size": 0.01, "cost_pct": 0.2}
    decision = ledger.append_decision("d1", "BTCUSDT", item, snapshot(),
                                      200, execution_plan=plan)
    assert decision["execution_plan"] == plan
    censor = ledger.append_censor("d1", 300, "censored_gap")
    assert censor["kind"] == "censored"
    with pytest.raises(ValueError, match="already resolved"):
        ledger.append_censor("d1", 400, "again")
    assert ScenarioLedger(ledger.path).read_rows()[-1]["kind"] == "censored"
