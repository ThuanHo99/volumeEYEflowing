"""Small, auditable scenario proposals and delayed paper-outcome learning.

This module cannot place orders or execute generated code. A proposal is data:
bounded execution parameters and predicates over explicitly allowlisted,
timestamped numeric observations. The ledger is append-only and single-writer.
Outcomes must be closed before the ranker can learn from them.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


# Names describe facts observable by the decision clock. The caller must
# calculate them from closed candles or genuinely available market snapshots.
ALLOWED_FEATURES = frozenset({
    "rvol", "body_to_range", "taker_delta_pct", "room_r", "risk_pct",
    "zone_touches", "zone_width_pct", "distance_to_zone_pct",
    "trend_efficiency", "oi_change_pct", "funding_rate_pct",
    "spread_bps", "depth_imbalance", "basis_bps",
    "long_room_r", "short_room_r", "long_risk_pct", "short_risk_pct",
    "support_touches", "resistance_touches",
    "distance_to_support_pct", "distance_to_resistance_pct",
    "regime_trend", "regime_range", "trend_up", "trend_down",
    "top_trader_account_ratio", "top_trader_position_ratio",
    "global_long_short_ratio", "taker_buy_sell_ratio",
    "oi_to_market_cap_ratio",
})
ALLOWED_OPERATORS = frozenset({"<", "<=", ">", ">="})
ALLOWED_ACTIONS = frozenset({"LONG", "SHORT", "NO_TRADE"})
_ID = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_SYMBOL = re.compile(r"^[A-Z0-9_]{2,40}$")


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _timestamp(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer millisecond timestamp")
    return value


@dataclass(frozen=True, slots=True)
class Condition:
    feature: str
    op: str
    threshold: float

    def __post_init__(self) -> None:
        if self.feature not in ALLOWED_FEATURES:
            raise ValueError(f"feature is not allowlisted: {self.feature}")
        if self.op not in ALLOWED_OPERATORS:
            raise ValueError(f"operator is not allowlisted: {self.op}")
        threshold = _finite_number(self.threshold, "threshold")
        if abs(threshold) > 1_000_000:
            raise ValueError("threshold is outside the allowed numeric range")
        object.__setattr__(self, "threshold", threshold)


@dataclass(frozen=True, slots=True)
class ScenarioProposal:
    scenario_id: str
    version: int
    conditions: tuple[Condition, ...]
    action: str
    stop_mult: float
    target_r: float
    max_hold_minutes: int
    created_at_ms: int
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not _ID.fullmatch(self.scenario_id):
            raise ValueError("scenario_id must be a short lowercase identifier")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or not 1 <= self.version <= 1_000_000:
            raise ValueError("version must be a positive bounded integer")
        conditions = tuple(self.conditions)
        if not 1 <= len(conditions) <= 8 or not all(isinstance(c, Condition) for c in conditions):
            raise ValueError("a proposal requires 1..8 valid conditions")
        object.__setattr__(self, "conditions", conditions)
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError("action must be LONG, SHORT or NO_TRADE")
        stop_mult = _finite_number(self.stop_mult, "stop_mult")
        target_r = _finite_number(self.target_r, "target_r")
        if not 0.5 <= stop_mult <= 3.0 or not 0.5 <= target_r <= 5.0:
            raise ValueError("stop_mult or target_r is outside the bounded policy range")
        object.__setattr__(self, "stop_mult", stop_mult)
        object.__setattr__(self, "target_r", target_r)
        if (isinstance(self.max_hold_minutes, bool) or
                not isinstance(self.max_hold_minutes, int) or
                not 1 <= self.max_hold_minutes <= 240):
            raise ValueError("max_hold_minutes must be 1..240")
        _timestamp(self.created_at_ms, "created_at_ms")
        if not isinstance(self.rationale, str) or len(self.rationale) > 500:
            raise ValueError("rationale must be at most 500 characters")

    @property
    def key(self) -> tuple[str, int]:
        return self.scenario_id, self.version


@dataclass(frozen=True, slots=True)
class FeatureValue:
    value: float
    observed_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _finite_number(self.value, "feature value"))
        _timestamp(self.observed_at_ms, "observed_at_ms")


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    context_asof_ms: int
    features: Mapping[str, FeatureValue]

    def __post_init__(self) -> None:
        _timestamp(self.context_asof_ms, "context_asof_ms")
        copied = dict(self.features)
        for name, observed in copied.items():
            if name not in ALLOWED_FEATURES or not isinstance(observed, FeatureValue):
                raise ValueError(f"invalid or unallowlisted feature: {name}")
            if observed.observed_at_ms > self.context_asof_ms:
                raise ValueError(f"future feature: {name}")
        object.__setattr__(self, "features", MappingProxyType(copied))


def snapshot_from_v3(event: Mapping[str, object], candle: Mapping[str, object]) -> FeatureSnapshot:
    """Extract only closed-candle facts and the plan frozen before the event."""
    if event.get("kind") != "confirmed":
        raise ValueError("snapshot requires a confirmed v3 research event")
    ts = _timestamp(event.get("ts"), "event ts")
    observed_at = _timestamp(event.get("observed_at"), "observed_at")
    plan_ts = _timestamp(event.get("plan_ts"), "plan_ts")
    event_ts = _timestamp(event.get("event_ts"), "event_ts")
    if (int(candle.get("t", -1)) != ts or not candle.get("x") or
            observed_at != ts + 59_999 or plan_ts >= event_ts or event_ts > ts):
        raise ValueError("v3 event/candle timing is not prospective")
    context = event.get("market_context")
    if not isinstance(context, Mapping) or context.get("as_of_ms") != plan_ts:
        raise ValueError("missing point-in-time market context")
    cutoffs = context.get("data_cutoffs")
    if (not isinstance(cutoffs, Mapping) or
            any(value is not None and _timestamp(value, "data cutoff") > plan_ts
                for value in cutoffs.values())):
        raise ValueError("future higher-timeframe data cutoff")
    evidence = event.get("evidence")
    feasibility = event.get("feasibility")
    zone = event.get("zone")
    if not all(isinstance(item, Mapping) for item in (evidence, feasibility, zone)):
        raise ValueError("missing confirmed evidence")
    price = _finite_number(event.get("price"), "confirmed price")
    if price <= 0:
        raise ValueError("confirmed price must be positive")
    features: dict[str, FeatureValue] = {}

    def add(name: str, value: object, clock: int) -> None:
        if value is not None:
            features[name] = FeatureValue(_finite_number(value, name), clock)

    add("rvol", evidence.get("rvol"), observed_at)
    add("body_to_range", evidence.get("body_to_range"), observed_at)
    add("risk_pct", feasibility.get("risk_pct"), observed_at)
    add("room_r", feasibility.get("room_r"), observed_at)
    add("zone_touches", zone.get("touches"), plan_ts)
    low = _finite_number(zone.get("low"), "zone low")
    high = _finite_number(zone.get("high"), "zone high")
    add("zone_width_pct", (high - low) / price * 100, plan_ts)
    boundary = high if event.get("direction") == "LONG" else low
    add("distance_to_zone_pct", abs(price - boundary) / price * 100, observed_at)
    volume = _finite_number(candle.get("v"), "volume")
    taker_buy = candle.get("V")
    if taker_buy is not None and volume > 0:
        buy = _finite_number(taker_buy, "taker buy")
        if not 0 <= buy <= volume:
            raise ValueError("invalid taker buy volume")
        add("taker_delta_pct", (2 * buy - volume) / volume * 100, observed_at)
    return FeatureSnapshot(observed_at, features)


def evaluate(proposal: ScenarioProposal, snapshot: FeatureSnapshot,
             decision_at_ms: int) -> bool:
    """Match a proposal using only observations available at the decision."""
    _timestamp(decision_at_ms, "decision_at_ms")
    if proposal.created_at_ms > decision_at_ms:
        raise ValueError("proposal did not exist at decision time")
    if snapshot.context_asof_ms > decision_at_ms:
        raise ValueError("context is from the future")
    for condition in proposal.conditions:
        observed = snapshot.features.get(condition.feature)
        if observed is None:
            return False
        value, threshold = observed.value, condition.threshold
        matched = {
            "<": value < threshold, "<=": value <= threshold,
            ">": value > threshold, ">=": value >= threshold,
        }[condition.op]
        if not matched:
            return False
    return True


def _proposal_row(proposal: ScenarioProposal) -> dict:
    """Use JSON-native lists so reloaded proposals compare byte-for-byte."""
    return {
        "kind": "proposal", "scenario_id": proposal.scenario_id,
        "version": proposal.version,
        "conditions": [asdict(condition) for condition in proposal.conditions],
        "action": proposal.action, "stop_mult": proposal.stop_mult,
        "target_r": proposal.target_r,
        "max_hold_minutes": proposal.max_hold_minutes,
        "created_at_ms": proposal.created_at_ms,
        "rationale": proposal.rationale,
    }


class ScenarioLedger:
    """Single-writer append-only JSONL proposal, decision and outcome ledger."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._proposals: dict[tuple[str, int], dict] = {}
        self._decisions: dict[str, dict] = {}
        self._outcomes: set[str] = set()
        self._retired: set[tuple[str, int]] = set()
        for row in self.read_rows():
            kind = row.get("kind")
            if kind == "proposal":
                row.setdefault("rationale", "")
                self._proposals[(row["scenario_id"], row["version"])] = row
            elif kind == "decision":
                self._decisions[row["decision_id"]] = row
            elif kind in {"outcome", "censored"}:
                self._outcomes.add(row["decision_id"])
            elif kind == "retired":
                self._retired.add((row["scenario_id"], row["version"]))
            else:
                raise ValueError(f"unknown ledger record kind: {kind}")

    def read_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _append(self, row: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row

    def append_proposal(self, proposal: ScenarioProposal) -> dict:
        with self._lock:
            if proposal.key in self._proposals:
                raise ValueError("scenario version already recorded")
            existing = [version for name, version in self._proposals if name == proposal.scenario_id]
            if existing and proposal.version <= max(existing):
                raise ValueError("scenario version must increase")
            if existing and proposal.created_at_ms <= self._proposals[(proposal.scenario_id, max(existing))]["created_at_ms"]:
                raise ValueError("new scenario version must be created later")
            row = _proposal_row(proposal)
            self._append(row)
            self._proposals[proposal.key] = row
            return row

    def append_retire(self, proposal: ScenarioProposal, retired_at_ms: int,
                      reason: str) -> dict:
        """Stop future trials while preserving immutable old decisions/outcomes."""
        _timestamp(retired_at_ms, "retired_at_ms")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 80:
            raise ValueError("invalid retirement reason")
        with self._lock:
            if proposal.key not in self._proposals or proposal.key in self._retired:
                raise ValueError("proposal missing or already retired")
            if retired_at_ms < proposal.created_at_ms:
                raise ValueError("retirement predates proposal creation")
            row = {"kind": "retired", "scenario_id": proposal.scenario_id,
                   "version": proposal.version,
                   "retired_at_ms": retired_at_ms, "reason": reason}
            self._append(row)
            self._retired.add(proposal.key)
            return row

    def append_decision(self, decision_id: str, symbol: str,
                        proposal: ScenarioProposal, snapshot: FeatureSnapshot,
                        decision_at_ms: int, *,
                        execution_plan: Mapping[str, object] | None = None) -> dict:
        if not isinstance(decision_id, str) or not 1 <= len(decision_id) <= 128:
            raise ValueError("decision_id must be a nonempty short string")
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError("invalid symbol")
        if not evaluate(proposal, snapshot, decision_at_ms):
            raise ValueError("proposal conditions did not match this snapshot")
        plan = None
        if execution_plan is not None:
            if set(execution_plan) != {"invalidation", "planned_target", "tick_size", "cost_pct"}:
                raise ValueError("invalid execution plan fields")
            plan = {
                "invalidation": _finite_number(execution_plan["invalidation"], "invalidation"),
                "planned_target": (_finite_number(execution_plan["planned_target"], "planned_target")
                                   if execution_plan["planned_target"] is not None else None),
                "tick_size": _finite_number(execution_plan["tick_size"], "tick_size"),
                "cost_pct": _finite_number(execution_plan["cost_pct"], "cost_pct"),
            }
            if (plan["invalidation"] <= 0 or plan["tick_size"] <= 0 or
                    plan["cost_pct"] < 0 or
                    (plan["planned_target"] is not None and plan["planned_target"] <= 0)):
                raise ValueError("invalid execution plan values")
        with self._lock:
            if proposal.key not in self._proposals:
                raise ValueError("record proposal before decision")
            if proposal.key in self._retired:
                raise ValueError("retired proposal cannot make new decisions")
            if self._proposals[proposal.key] != _proposal_row(proposal):
                raise ValueError("scenario version differs from the recorded immutable proposal")
            if decision_id in self._decisions:
                raise ValueError("decision_id already recorded")
            row = {
                "kind": "decision", "decision_id": decision_id,
                "symbol": symbol, "scenario_id": proposal.scenario_id,
                "version": proposal.version, "action": proposal.action,
                "decision_at_ms": decision_at_ms,
                "context_asof_ms": snapshot.context_asof_ms,
                "features": {name: asdict(value)
                             for name, value in snapshot.features.items()},
                "execution_plan": plan,
            }
            self._append(row)
            self._decisions[decision_id] = row
            return row

    def append_outcome(self, decision_id: str, matured_at_ms: int,
                       net_pct: float, *, execution: Mapping[str, object] | None = None) -> dict:
        _timestamp(matured_at_ms, "matured_at_ms")
        net_pct = _finite_number(net_pct, "net_pct")
        execution_row = None
        if execution is not None:
            try:
                encoded = json.dumps(dict(execution), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid execution details") from exc
            if len(encoded) > 4096:
                raise ValueError("execution details too large")
            execution_row = json.loads(encoded)
        with self._lock:
            decision = self._decisions.get(decision_id)
            if decision is None:
                raise ValueError("outcome has no recorded decision")
            if decision_id in self._outcomes:
                raise ValueError("outcome already recorded")
            if decision["action"] == "NO_TRADE":
                raise ValueError("NO_TRADE has no fill outcome")
            if matured_at_ms <= decision["decision_at_ms"]:
                raise ValueError("outcome did not mature after decision")
            row = {
                "kind": "outcome", "decision_id": decision_id,
                "scenario_id": decision["scenario_id"],
                "version": decision["version"], "action": decision["action"],
                "decision_at_ms": decision["decision_at_ms"],
                "matured_at_ms": matured_at_ms, "net_pct": net_pct,
                "execution": execution_row,
            }
            self._append(row)
            self._outcomes.add(decision_id)
            return row

    def append_censor(self, decision_id: str, matured_at_ms: int, reason: str) -> dict:
        """Record an unscorable paper decision without teaching the ranker."""
        _timestamp(matured_at_ms, "matured_at_ms")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 80:
            raise ValueError("invalid censor reason")
        with self._lock:
            decision = self._decisions.get(decision_id)
            if decision is None or decision_id in self._outcomes:
                raise ValueError("decision missing or already resolved")
            if matured_at_ms <= decision["decision_at_ms"]:
                raise ValueError("censor did not mature after decision")
            row = {"kind": "censored", "decision_id": decision_id,
                   "scenario_id": decision["scenario_id"],
                   "version": decision["version"],
                   "matured_at_ms": matured_at_ms, "reason": reason}
            self._append(row)
            self._outcomes.add(decision_id)
            return row


@dataclass(frozen=True, slots=True)
class ScenarioScore:
    samples: int
    estimated_net_pct: float
    lower_confidence_pct: float
    eligible: bool


class ScenarioRanker:
    """Shrink mature net outcomes toward a nonpositive prior; abstain by default."""

    def __init__(self, min_samples: int = 30, prior_strength: float = 30.0,
                 prior_mean_pct: float = 0.0):
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
            raise ValueError("min_samples must be positive")
        strength = _finite_number(prior_strength, "prior_strength")
        prior = _finite_number(prior_mean_pct, "prior_mean_pct")
        if strength <= 0 or prior > 0:
            raise ValueError("prior_strength must be positive and prior_mean_pct nonpositive")
        self.min_samples = min_samples
        self.prior_strength = strength
        self.prior_mean_pct = prior
        self._results: dict[tuple[str, int], tuple[int, float, float]] = {}
        self._seen: dict[str, tuple] = {}

    def update(self, outcome: Mapping[str, object], asof_ms: int) -> bool:
        """Return False for an outcome not yet mature or already seen."""
        _timestamp(asof_ms, "asof_ms")
        if outcome.get("kind") != "outcome":
            raise ValueError("ranker only accepts closed outcome records")
        matured = _timestamp(outcome.get("matured_at_ms"), "matured_at_ms")
        decision_at = _timestamp(outcome.get("decision_at_ms"), "decision_at_ms")
        if matured <= decision_at:
            raise ValueError("outcome must mature after its decision")
        if matured > asof_ms:
            return False
        if outcome.get("action") not in {"LONG", "SHORT"}:
            raise ValueError("ranker cannot learn from a non-trade")
        key = outcome.get("scenario_id"), outcome.get("version")
        if (not isinstance(key[0], str) or not _ID.fullmatch(key[0]) or
                isinstance(key[1], bool) or not isinstance(key[1], int) or key[1] < 1):
            raise ValueError("invalid outcome scenario key")
        decision_id = outcome.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("outcome needs decision_id")
        net = _finite_number(outcome.get("net_pct"), "net_pct")
        signature = (key, matured, net)
        if decision_id in self._seen:
            if self._seen[decision_id] != signature:
                raise ValueError("conflicting duplicate outcome")
            return False
        count, total, squares = self._results.get(key, (0, 0.0, 0.0))
        self._results[key] = count + 1, total + net, squares + net * net
        self._seen[decision_id] = signature
        return True

    def score(self, proposal: ScenarioProposal) -> ScenarioScore:
        count, total, squares = self._results.get(proposal.key, (0, 0.0, 0.0))
        estimate = (total + self.prior_strength * self.prior_mean_pct) / (
            count + self.prior_strength)
        sample_variance = (max(0.0, squares - total * total / count) / (count - 1)
                           if count > 1 else 0.0)
        # The floor prevents identical early paper returns from appearing certain.
        standard_error = max(math.sqrt(sample_variance), 0.2) / math.sqrt(max(count, 1))
        lower = estimate - 1.96 * standard_error
        return ScenarioScore(count, estimate, lower,
                             count >= self.min_samples and lower > 0)

    def choose(self, proposals: list[ScenarioProposal] | tuple[ScenarioProposal, ...],
               snapshot: FeatureSnapshot, decision_at_ms: int) -> ScenarioProposal | None:
        """Choose the best sufficiently tested matching action, or abstain."""
        matching = [p for p in proposals if evaluate(p, snapshot, decision_at_ms)]
        if any(p.action == "NO_TRADE" for p in matching):
            return None
        ranked = []
        for proposal in matching:
            if proposal.action not in {"LONG", "SHORT"}:
                continue
            score = self.score(proposal)
            if score.eligible:
                ranked.append((score.lower_confidence_pct,
                               proposal.scenario_id, proposal.version, proposal))
        return max(ranked, key=lambda row: row[:3])[3] if ranked else None
