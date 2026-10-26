"""Forward-only one-minute paper execution for versioned scenario proposals.

The book never sends exchange orders. It accepts a proposal only after a
closed candle, enters at the next contiguous candle open, and settles using
the same conservative stop-first OHLC convention as the USD-M replay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .adaptive_scenarios import FeatureSnapshot, ScenarioProposal, evaluate

MINUTE_MS = 60_000


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _tick_round(value: float, tick: float, *, up: bool) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    amount = Decimal(str(round(value, 12)))
    quantum = Decimal(str(tick))
    direction = ROUND_CEILING if up else ROUND_FLOOR
    return float((amount / quantum).to_integral_value(rounding=direction) * quantum)


@dataclass(frozen=True, slots=True)
class PaperIntent:
    decision_id: str
    symbol: str
    scenario_id: str
    version: int
    direction: str
    decided_at_ms: int
    invalidation: float
    planned_target: float | None
    tick_size: float
    stop_mult: float
    target_r: float
    max_hold_minutes: int
    cost_pct: float


@dataclass(frozen=True, slots=True)
class PaperResult:
    decision_id: str
    symbol: str
    scenario_id: str
    version: int
    status: str  # evaluated | censored
    reason: str
    matured_at_ms: int
    entry_ts: int | None = None
    exit_ts: int | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    exit: float | None = None
    gross_pct: float | None = None
    net_pct: float | None = None
    ambiguous_same_minute: bool = False


@dataclass(slots=True)
class _Position:
    intent: PaperIntent
    entry_ts: int
    entry: float
    stop: float
    target: float
    age: int = 0


class AdaptivePaperBook:
    """Evaluate independent hypothetical proposals on chronological closed 1m bars."""

    def __init__(self):
        self._last_ts: dict[str, int] = {}
        self._pending: dict[str, list[PaperIntent]] = {}
        self._open: dict[str, list[_Position]] = {}
        self._decision_ids: set[str] = set()

    def has_exposure(self, symbol: str, scenario_id: str,
                     version: int) -> bool:
        """Keep one pending/open independent trial per scenario and symbol."""
        return any(
            intent.scenario_id == scenario_id and intent.version == version
            for intent in self._pending.get(symbol, [])
        ) or any(
            position.intent.scenario_id == scenario_id and
            position.intent.version == version
            for position in self._open.get(symbol, [])
        )

    def queue(self, decision_id: str, symbol: str, proposal: ScenarioProposal,
              snapshot: FeatureSnapshot, *, invalidation: float,
              planned_target: float | None, tick_size: float,
              cost_pct: float = 0.2) -> PaperIntent:
        """Queue at a closed decision candle; the next minute supplies entry."""
        if not decision_id or decision_id in self._decision_ids:
            raise ValueError("decision_id must be new and nonempty")
        if proposal.action not in {"LONG", "SHORT"}:
            raise ValueError("only directional proposals have a paper entry")
        decided = snapshot.context_asof_ms
        if self._last_ts.get(symbol) != decided - MINUTE_MS + 1:
            raise ValueError("decision does not follow this symbol's latest closed minute")
        if not evaluate(proposal, snapshot, decided):
            raise ValueError("proposal does not match point-in-time features")
        stop = _number(invalidation, "invalidation")
        target = (_number(planned_target, "planned_target")
                  if planned_target is not None else None)
        tick = _number(tick_size, "tick_size")
        cost = _number(cost_pct, "cost_pct")
        if stop <= 0 or (target is not None and target <= 0) or tick <= 0 or cost < 0:
            raise ValueError("invalid paper price, tick, or cost")
        intent = PaperIntent(
            decision_id, symbol, proposal.scenario_id, proposal.version,
            proposal.action, decided, stop, target, tick,
            proposal.stop_mult, proposal.target_r, proposal.max_hold_minutes, cost,
        )
        self._pending.setdefault(symbol, []).append(intent)
        self._decision_ids.add(decision_id)
        return intent

    @staticmethod
    def _censor(intent: PaperIntent, reason: str, matured_at_ms: int,
                position: _Position | None = None) -> PaperResult:
        return PaperResult(
            intent.decision_id, intent.symbol, intent.scenario_id, intent.version,
            "censored", reason, matured_at_ms,
            entry_ts=position.entry_ts if position else None,
            entry=position.entry if position else None,
            stop=position.stop if position else None,
            target=position.target if position else None,
        )

    @staticmethod
    def _enter(intent: PaperIntent, bar: dict) -> _Position | PaperResult:
        close_clock = bar["t"] + MINUTE_MS - 1
        if bar["v"] <= 0:
            return AdaptivePaperBook._censor(intent, "zero_volume_entry", close_clock)
        side = 1 if intent.direction == "LONG" else -1
        entry = bar["o"]
        base_risk = side * (entry - intent.invalidation)
        if base_risk <= 0:
            return AdaptivePaperBook._censor(intent, "invalidated_before_entry", close_clock)
        raw_stop = entry - side * intent.stop_mult * base_risk
        stop = _tick_round(raw_stop, intent.tick_size, up=side == -1)
        risk = side * (entry - stop)
        if stop <= 0 or risk < 2 * intent.tick_size:
            return AdaptivePaperBook._censor(intent, "invalid_stop", close_clock)
        target = entry + side * intent.target_r * risk
        if intent.planned_target is not None:
            target = (min(target, intent.planned_target) if side == 1 else
                      max(target, intent.planned_target))
        target = _tick_round(target, intent.tick_size, up=side == -1)
        if side * (target - entry) <= 0:
            return AdaptivePaperBook._censor(intent, "target_passed_before_entry", close_clock)
        return _Position(intent, bar["t"], entry, stop, target)

    @staticmethod
    def _advance(position: _Position, bar: dict) -> PaperResult | None:
        intent = position.intent
        position.age += 1
        side = 1 if intent.direction == "LONG" else -1
        stop_hit = bar["l"] <= position.stop if side == 1 else bar["h"] >= position.stop
        target_hit = bar["h"] >= position.target if side == 1 else bar["l"] <= position.target
        matured = bar["t"] + MINUTE_MS - 1
        if bar["v"] <= 0 and (stop_hit or target_hit or position.age >= intent.max_hold_minutes):
            return AdaptivePaperBook._censor(intent, "zero_volume_exit", matured, position)
        if stop_hit:
            exit_price = (min(bar["o"], position.stop) if side == 1 else
                          max(bar["o"], position.stop))
            reason = "stop"
        elif target_hit:
            exit_price = position.target
            reason = "target"
        elif position.age >= intent.max_hold_minutes:
            exit_price = bar["c"]
            reason = "timeout"
        else:
            return None
        gross = side * (exit_price - position.entry) / position.entry * 100
        return PaperResult(
            intent.decision_id, intent.symbol, intent.scenario_id,
            intent.version, "evaluated", reason, matured,
            entry_ts=position.entry_ts, exit_ts=bar["t"],
            entry=position.entry, stop=position.stop, target=position.target,
            exit=exit_price, gross_pct=gross, net_pct=gross - intent.cost_pct,
            ambiguous_same_minute=stop_hit and target_hit,
        )

    def on_closed_bar(self, symbol: str, k: dict) -> list[PaperResult]:
        """Reveal exactly one closed minute, update active paper positions."""
        try:
            ts = int(k["t"])
            bar = {"t": ts, "o": _number(k["o"], "open"),
                   "h": _number(k["h"], "high"),
                   "l": _number(k["l"], "low"),
                   "c": _number(k["c"], "close"),
                   "v": _number(k["v"], "volume")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid closed 1m bar") from exc
        if (not k.get("x") or ts < 0 or ts % MINUTE_MS or bar["l"] <= 0 or
                bar["v"] < 0 or bar["l"] > min(bar["o"], bar["c"]) or
                bar["h"] < max(bar["o"], bar["c"])):
            raise ValueError("invalid closed 1m bar")
        last = self._last_ts.get(symbol)
        if last is not None and ts <= last:
            raise ValueError("duplicate or out-of-order paper candle")
        results = []
        if last is not None and ts != last + MINUTE_MS:
            for intent in self._pending.pop(symbol, []):
                results.append(self._censor(intent, "censored_gap", ts - 1))
            for position in self._open.pop(symbol, []):
                results.append(self._censor(position.intent, "censored_gap", ts - 1,
                                            position))
        else:
            active = []
            for position in self._open.pop(symbol, []):
                result = self._advance(position, bar)
                if result is None:
                    active.append(position)
                else:
                    results.append(result)
            for intent in self._pending.pop(symbol, []):
                if ts != intent.decided_at_ms + 1:
                    results.append(self._censor(intent, "censored_gap", ts - 1))
                    continue
                entered = self._enter(intent, bar)
                if isinstance(entered, PaperResult):
                    results.append(entered)
                    continue
                result = self._advance(entered, bar)
                if result is None:
                    active.append(entered)
                else:
                    results.append(result)
            if active:
                self._open[symbol] = active
        self._last_ts[symbol] = ts
        return results
