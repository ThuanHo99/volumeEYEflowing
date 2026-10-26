"""Local LLM output cannot become an order or bypass typed scenario rules."""

import json
from urllib.error import URLError

from VolumeSniper.adaptive_scenarios import FeatureSnapshot, FeatureValue
from VolumeSniper.local_scenario_proposer import LocalScenarioProposer


class FakeResponse:
    status = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.data[:limit]


def envelope(proposals):
    return json.dumps({
        "done": True,
        "message": {"content": json.dumps({"proposals": proposals})},
        "eval_count": 42, "eval_duration": 1_000_000_000,
    }).encode()


def snapshot():
    return FeatureSnapshot(200, {"rvol": FeatureValue(2.5, 199)})


def proposal():
    return {
        "scenario_id": "held_retest_01", "action": "LONG",
        "conditions": [{"feature": "rvol", "op": ">=", "threshold": 2.0}],
        "stop_mult": 1.0, "target_r": 2.0, "max_hold_minutes": 15,
        "rationale": "Price held a preplanned zone after unusual effort.",
    }


def test_local_llm_compiles_only_typed_bounded_proposal(monkeypatch):
    seen = {}

    def fake_open(request, timeout):
        seen["request"] = json.loads(request.data)
        return FakeResponse(envelope([proposal()]))

    monkeypatch.setattr("VolumeSniper.local_scenario_proposer.urlopen", fake_open)
    result = LocalScenarioProposer().propose(snapshot(), created_at_ms=201)
    assert result.status == "ok"
    assert result.proposals[0].scenario_id == "held_retest_01"
    assert result.proposals[0].created_at_ms == 201
    assert result.rationales[0].startswith("Price held")
    assert result.eval_count == 42
    assert seen["request"]["think"] is False
    assert seen["request"]["stream"] is False
    assert seen["request"]["format"]["type"] == "object"
    assert "future" not in seen["request"]["messages"][1]["content"]


def test_unavailable_or_unsafe_reply_fails_closed(monkeypatch):
    client = LocalScenarioProposer()
    assert client.propose(snapshot(), created_at_ms=199).status == "invalid_input"
    future = {"kind": "outcome", "matured_at_ms": 202, "net_pct": 1.0}
    assert client.propose(snapshot(), created_at_ms=201,
                          recent_outcomes=[future]).status == "invalid_input"

    monkeypatch.setattr("VolumeSniper.local_scenario_proposer.urlopen",
                        lambda *args, **kwargs: FakeResponse(envelope([
                            {**proposal(), "conditions": [
                                {"feature": "future_return", "op": ">", "threshold": 1.0}]}
                        ])))
    assert client.propose(snapshot(), created_at_ms=201).status == "malformed"

    monkeypatch.setattr("VolumeSniper.local_scenario_proposer.urlopen",
                        lambda *args, **kwargs: FakeResponse(envelope([
                            {**proposal(), "conditions": [
                                {"feature": "oi_change_pct", "op": ">", "threshold": 1.0}]}
                        ])))
    assert client.propose(snapshot(), created_at_ms=201).status == "malformed"

    def down(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr("VolumeSniper.local_scenario_proposer.urlopen", down)
    assert client.propose(snapshot(), created_at_ms=201).status == "unavailable"


def test_only_loopback_ollama_endpoint_is_accepted():
    try:
        LocalScenarioProposer(endpoint="http://example.com/api/chat")
    except ValueError:
        pass
    else:
        raise AssertionError("remote endpoint accepted")
