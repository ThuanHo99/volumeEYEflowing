"""Price-first, point-in-time scenario engine for research only.

The plan used to judge a candle was fixed at an earlier closed candle. Every
event records both clocks. Zones are hypotheses derived from closed 15m/1h/4h
bars; a touch, volume spike, or positioning metric alone is never a trade.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from core.config import DATA_DIR
from .market_structure import (MINUTE_MS, TIMEFRAMES, MarketMap, StructureZone,
                               aggregate_closed_bars, build_market_map)

MAX_MINUTES = 300
MAX_HTF_BARS = 240
CANDIDATE_EXPIRY = 12
WATCH_COOLDOWN = 15 * MINUTE_MS
ASSUMED_ROUND_TRIP_COST_PCT = 0.20  # research gate; replay varies this assumption
MIN_ROOM_R = 1.5


@dataclass(frozen=True)
class ChartPlan:
    symbol: str
    as_of_ms: int
    decision: str
    reason: str | None
    market_map: MarketMap
    scenarios: tuple[dict, ...]
    research_only: bool = True

    def to_dict(self) -> dict:
        result = asdict(self)
        result["market_map"] = self.market_map.to_dict()
        return result


@dataclass
class _Candidate:
    scenario: str
    direction: str
    zone: StructureZone
    market_map: MarketMap
    event_ts: int
    plan_ts: int
    age: int = 0


@dataclass
class _Outcome:
    confirmed_ts: int
    direction: str
    price: float
    invalidation: float
    age: int = 0
    best_pct: float = 0.0
    worst_pct: float = 0.0
    five_close_pct: float | None = None


class ChartReasoningEngine:
    """Consume closed chronological 1m candles and emit timestamped evidence.

    ``seed`` is for already closed startup data; ``on_kline`` is the only
    decision path. The live scanner may later merge closed REST higher bars.
    Replay does not, so it constructs those bars from completed 1m buckets.
    """

    def __init__(self, *, journal_path: Path | None = None):
        self.history: dict[str, deque[dict]] = {}
        self.higher: dict[str, dict[str, deque[dict]]] = {}
        self.plans: dict[str, ChartPlan] = {}
        self.pending: dict[str, _Candidate] = {}
        self.outcomes: dict[str, list[_Outcome]] = {}
        self.records: deque[dict] = deque(maxlen=30_000)
        self._watch_at: dict[tuple[str, str, float, float], int] = {}
        self._plan_signature: dict[str, tuple] = {}
        self._gap_quarantine: dict[str, int] = {}
        self._write_lock = asyncio.Lock()
        base = Path(DATA_DIR)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent / base
        self.journal_path = journal_path or base / "vs_chart_v3_events.jsonl"

    @staticmethod
    def _minute(k: Mapping) -> dict | None:
        if not bool(k.get("x", k.get("closed", True))):
            return None
        try:
            ts = int(k.get("t", k.get("ts")))
            o = float(k.get("o", k.get("open")))
            h = float(k.get("h", k.get("high")))
            l = float(k.get("l", k.get("low")))
            c = float(k.get("c", k.get("close")))
            v = float(k.get("v", k.get("volume")))
        except (TypeError, ValueError):
            return None
        if (ts < 0 or ts % MINUTE_MS or not all(math.isfinite(x) for x in (o, h, l, c, v))
                or l <= 0 or v < 0 or l > min(o, c) or h < max(o, c)):
            return None
        result = {"t": ts, "o": o, "h": h, "l": l, "c": c, "v": v, "x": True}
        try:
            buy = float(k.get("V", k.get("taker_buy_volume")))
        except (TypeError, ValueError):
            buy = None
        if buy is not None and math.isfinite(buy) and 0 <= buy <= v:
            result["V"] = buy
        return result

    @staticmethod
    def _zone_id(zone: StructureZone) -> tuple:
        return (zone.side, zone.timeframe, zone.low, zone.high,
                zone.confirmed_ts)

    @staticmethod
    def _market_context(market_map: MarketMap) -> dict:
        """Compact, immutable-at-decision evidence for later paper learning."""
        return {
            "as_of_ms": market_map.as_of_ms,
            "regime": market_map.regime,
            "trend_direction": market_map.trend_direction,
            "regime_timeframe": market_map.regime_timeframe,
            "nearest_support": (asdict(market_map.nearest_support)
                                if market_map.nearest_support else None),
            "nearest_resistance": (asdict(market_map.nearest_resistance)
                                   if market_map.nearest_resistance else None),
            "data_cutoffs": dict(market_map.data_cutoffs),
            "bar_counts": dict(market_map.bar_counts),
            "quality_issues": list(market_map.quality_issues),
        }

    @staticmethod
    def _scenario_rows(market_map: MarketMap) -> tuple[dict, ...]:
        result: list[dict] = []
        for zone, side in ((market_map.nearest_resistance, "UP"),
                           (market_map.nearest_support, "DOWN")):
            if zone is None:
                continue
            breakout = "BREAKOUT_" + side
            failed = "FAILED_BREAK_" + side
            boundary = zone.high if side == "UP" else zone.low
            opposite = zone.low if side == "UP" else zone.high
            result.extend((
                {"scenario": breakout,
                 "zone": asdict(zone),
                 "trigger": f"Closed 1m beyond {boundary:.8g}; later shallow retest closes outside",
                 "invalidation": f"Close through opposite side {opposite:.8g} or deep penetration",
                 "expiry_candles": CANDIDATE_EXPIRY},
                {"scenario": failed,
                 "zone": asdict(zone),
                 "trigger": f"Pierce {boundary:.8g}, close through {opposite:.8g}; later no reclaim",
                 "invalidation": f"Close back beyond {boundary:.8g}",
                 "expiry_candles": CANDIDATE_EXPIRY},
            ))
        return tuple(result)

    def _higher_for(self, symbol: str) -> dict[str, deque[dict]]:
        return self.higher.setdefault(symbol, {
            tf: deque(maxlen=MAX_HTF_BARS) for tf in TIMEFRAMES
        })

    def _merge_higher(self, symbol: str, timeframe: str, rows: Sequence[object],
                      as_of_ms: int) -> None:
        if timeframe not in TIMEFRAMES:
            return
        minutes = TIMEFRAMES[timeframe]
        bars = self._higher_for(symbol)[timeframe]
        by_ts = {int(bar["ts"]): bar for bar in bars}
        for row in rows:
            try:
                if isinstance(row, Mapping):
                    ts = int(row.get("ts", row.get("t")))
                    o = float(row.get("open", row.get("o")))
                    h = float(row.get("high", row.get("h")))
                    l = float(row.get("low", row.get("l")))
                    c = float(row.get("close", row.get("c")))
                    v = float(row.get("volume", row.get("v")))
                else:
                    ts, o, h, l, c, v = (row.ts, row.open, row.high,
                                          row.low, row.close, row.volume)
                    ts, o, h, l, c, v = int(ts), float(o), float(h), float(l), float(c), float(v)
                close_ts = ts + minutes * MINUTE_MS - 1
                if (ts < 0 or ts % (minutes * MINUTE_MS) or close_ts > as_of_ms
                        or not all(math.isfinite(x) for x in (o, h, l, c, v))
                        or l <= 0 or v < 0 or l > min(o, c) or h < max(o, c)):
                    continue
                by_ts[ts] = {"ts": ts, "open": o, "high": h, "low": l,
                             "close": c, "volume": v, "close_ts": close_ts}
            except (AttributeError, TypeError, ValueError):
                continue
        bars.clear()
        bars.extend(by_ts[ts] for ts in sorted(by_ts)[-MAX_HTF_BARS:])

    def seed_higher_timeframes(self, symbol: str,
                               raw_by_tf: Mapping[str, Sequence[object]]) -> None:
        history = self.history.get(symbol)
        if not history:
            return
        as_of_ms = history[-1]["t"] + MINUTE_MS - 1
        for timeframe, rows in raw_by_tf.items():
            self._merge_higher(symbol, timeframe, rows, as_of_ms)
        self._refresh_plan(symbol, as_of_ms)

    def seed(self, symbol: str, raw_1m: Sequence[Mapping], *,
             higher_timeframes: Mapping[str, Sequence[object]] | None = None) -> None:
        self.history.pop(symbol, None)
        self.higher.pop(symbol, None)
        self.pending.pop(symbol, None)
        self.plans.pop(symbol, None)
        history = self.history.setdefault(symbol, deque(maxlen=MAX_MINUTES))
        for k in sorted(raw_1m, key=lambda row: int(row.get("t", row.get("ts", -1)))):
            bar = self._minute(k)
            if bar is None or (history and bar["t"] <= history[-1]["t"]):
                continue
            if history and bar["t"] != history[-1]["t"] + MINUTE_MS:
                history.clear()
            history.append(bar)
            self._complete_higher(symbol, bar["t"] + MINUTE_MS - 1)
        if history:
            as_of_ms = history[-1]["t"] + MINUTE_MS - 1
            if higher_timeframes:
                for timeframe, rows in higher_timeframes.items():
                    self._merge_higher(symbol, timeframe, rows, as_of_ms)
            self._refresh_plan(symbol, as_of_ms)

    def _complete_higher(self, symbol: str, as_of_ms: int) -> bool:
        history = self.history[symbol]
        changed = False
        for timeframe, minutes in TIMEFRAMES.items():
            if (as_of_ms + 1) % (minutes * MINUTE_MS):
                continue
            if len(history) < minutes:
                continue
            current_bucket = (as_of_ms + 1) - minutes * MINUTE_MS
            if history[-minutes]["t"] != current_bucket:
                continue
            completed = aggregate_closed_bars(list(history)[-minutes:], minutes,
                                              as_of_ms=as_of_ms)
            if completed:
                self._merge_higher(symbol, timeframe, [completed[-1]], as_of_ms)
                changed = True
        return changed

    def _refresh_plan(self, symbol: str, as_of_ms: int, *, reuse_structure: bool = False) -> None:
        history = self.history.get(symbol)
        if not history:
            return
        previous = self.plans.get(symbol)
        if reuse_structure and previous is not None:
            # Higher bars alone define swings and regime. Between their closes,
            # only the current price, nearest zones, 1m cutoff and staleness
            # change. Reusing the immutable zone set avoids rescanning all
            # pivots for every one-minute candle in a full-market replay.
            old = previous.market_map
            price = history[-1]["c"]
            supports = [z for z in old.zones if z.side == "support" and z.low <= price]
            resistances = [z for z in old.zones if z.side == "resistance" and z.high >= price]
            support = min(supports, key=lambda z: (
                max(price - z.high, 0), -TIMEFRAMES[z.timeframe], -z.touches,
            )) if supports else None
            resistance = min(resistances, key=lambda z: (
                max(z.low - price, 0), -TIMEFRAMES[z.timeframe], -z.touches,
            )) if resistances else None
            cutoffs = dict(old.data_cutoffs)
            cutoffs["1m"] = as_of_ms
            counts = dict(old.bar_counts)
            counts["1m"] = len(history)
            issues = []
            for tf, minutes in TIMEFRAMES.items():
                cutoff = cutoffs.get(tf)
                if cutoff is None:
                    issues.append(f"missing_{tf}")
                elif cutoff < as_of_ms - 2 * minutes * MINUTE_MS:
                    issues.append(f"stale_{tf}")
            market_map = replace(old, as_of_ms=as_of_ms, price=price,
                                 nearest_support=support, nearest_resistance=resistance,
                                 data_cutoffs=cutoffs, bar_counts=counts,
                                 quality_issues=tuple(issues))
        else:
            higher = {tf: list(rows) for tf, rows in self._higher_for(symbol).items()}
            market_map = build_market_map(symbol, list(history),
                                          higher_timeframes=higher, as_of_ms=as_of_ms)
        if market_map.bar_counts.get("15m", 0) < 12 or market_map.regime == "unclear":
            decision, reason = "NO_TRADE", "insufficient_closed_structure"
        elif market_map.nearest_support is None and market_map.nearest_resistance is None:
            decision, reason = "NO_TRADE", "no_nearby_structural_zone"
        else:
            decision, reason = "WAIT", None
        plan = ChartPlan(symbol, as_of_ms, decision, reason, market_map,
                         self._scenario_rows(market_map))
        self.plans[symbol] = plan
        signature = (decision, reason, market_map.regime, market_map.trend_direction,
                     self._zone_id(market_map.nearest_support)
                     if market_map.nearest_support else None,
                     self._zone_id(market_map.nearest_resistance)
                     if market_map.nearest_resistance else None,
                     tuple(market_map.quality_issues))
        if signature != self._plan_signature.get(symbol):
            self._plan_signature[symbol] = signature
            self.records.append({
                "kind": "plan", "symbol": symbol, "ts": as_of_ms - MINUTE_MS + 1,
                "observed_at": as_of_ms, "plan_ts": as_of_ms,
                "decision": decision, "reason": reason,
                "market_map": market_map.to_dict(), "research_only": True,
            })

    def _volume_evidence(self, history: Sequence[dict], candle: dict) -> dict:
        previous = [bar["v"] for bar in history[-60:] if bar["v"] > 0]
        baseline = statistics.median(previous) if len(previous) >= 10 else None
        rvol = candle["v"] / baseline if baseline and baseline > 0 else None
        buy = candle.get("V")
        delta = 2 * buy - candle["v"] if buy is not None else None
        range_width = candle["h"] - candle["l"]
        return {
            "rvol": rvol, "volume_baseline": "rolling_60_provisional" if baseline else "unavailable",
            "taker_delta": delta,
            "body_to_range": abs(candle["c"] - candle["o"]) / range_width if range_width else 0,
        }

    def _record(self, kind: str, symbol: str, candle: dict, plan_ts: int | None,
                **fields) -> dict:
        record = {"kind": kind, "symbol": symbol, "ts": candle["t"],
                  "observed_at": candle["t"] + MINUTE_MS - 1,
                  "plan_ts": plan_ts, "research_only": True, **fields}
        self.records.append(record)
        return record

    def _watch(self, symbol: str, candle: dict, plan: ChartPlan,
               volume: dict) -> list[dict]:
        result = []
        for zone in (plan.market_map.nearest_support,
                     plan.market_map.nearest_resistance):
            if zone is None or candle["h"] < zone.low or candle["l"] > zone.high:
                continue
            key = (symbol, zone.side, zone.low, zone.high)
            if candle["t"] - self._watch_at.get(key, -WATCH_COOLDOWN) < WATCH_COOLDOWN:
                continue
            self._watch_at[key] = candle["t"]
            result.append(self._record(
                "watch", symbol, candle, plan.as_of_ms, zone=asdict(zone),
                price=candle["c"], evidence=volume,
                reason="price_interacted_with_preplanned_zone; direction_unresolved",
            ))
        return result

    @staticmethod
    def _candidate_for(zone: StructureZone, candle: dict, prior_close: float) -> tuple[str, str] | None:
        if zone.side == "resistance":
            if prior_close > zone.high or candle["h"] <= zone.high:
                return None
            if candle["c"] > zone.high:
                return ("BREAKOUT_UP", "LONG")
            if candle["c"] < zone.low:
                return ("FAILED_BREAK_UP", "SHORT")
        else:
            if prior_close < zone.low or candle["l"] >= zone.low:
                return None
            if candle["c"] < zone.low:
                return ("BREAKOUT_DOWN", "SHORT")
            if candle["c"] > zone.high:
                return ("FAILED_BREAK_DOWN", "LONG")
        return None

    def _start_candidate(self, symbol: str, candle: dict, prior_close: float,
                         plan: ChartPlan, volume: dict) -> list[dict]:
        attempts = []
        for zone in (plan.market_map.nearest_support,
                     plan.market_map.nearest_resistance):
            if zone is None:
                continue
            result = self._candidate_for(zone, candle, prior_close)
            if result:
                attempts.append((zone, *result))
        if len(attempts) > 1:
            return [self._record("no_trade", symbol, candle, plan.as_of_ms,
                                 reason="both_support_and_resistance_crossed",
                                 evidence=volume)]
        if not attempts:
            return []
        zone, scenario, direction = attempts[0]
        self.pending[symbol] = _Candidate(scenario, direction, zone,
                                           plan.market_map, candle["t"], plan.as_of_ms)
        return [self._record("candidate", symbol, candle, plan.as_of_ms,
                             event_ts=candle["t"], scenario=scenario,
                             direction=direction, zone=asdict(zone),
                             event_close=candle["c"], evidence=volume,
                             market_context=self._market_context(plan.market_map))]

    @staticmethod
    def _buffer(zone: StructureZone, price: float) -> float:
        return max((zone.high - zone.low) * .1, price * .0002)

    def _feasibility(self, candidate: _Candidate, price: float) -> tuple[float, dict, str | None]:
        zone = candidate.zone
        buffer = self._buffer(zone, price)
        direction = candidate.direction
        invalidation = zone.low - buffer if direction == "LONG" else zone.high + buffer
        risk = abs(price - invalidation)
        if ((direction == "LONG" and invalidation >= price) or
                (direction == "SHORT" and invalidation <= price)):
            return invalidation, {}, "structural_stop_wrong_side"
        target_zone = candidate.market_map.opposing_zone(direction, price)
        if target_zone is None:
            return invalidation, {"risk": risk}, "next_opposing_zone_unknown"
        target = target_zone.low if direction == "LONG" else target_zone.high
        room = target - price if direction == "LONG" else price - target
        room_r = room / risk if risk else 0
        cost_abs = price * ASSUMED_ROUND_TRIP_COST_PCT / 100
        details = {"risk": risk, "risk_pct": risk / price * 100,
                   "next_zone": asdict(target_zone), "target_near_edge": target,
                   "room": room, "room_r": room_r,
                   "assumed_round_trip_cost_pct": ASSUMED_ROUND_TRIP_COST_PCT}
        if room <= 0:
            return invalidation, details, "no_room_to_next_zone"
        if room < MIN_ROOM_R * risk + cost_abs:
            return invalidation, details, "room_below_risk_and_cost_gate"
        return invalidation, details, None

    def _advance(self, symbol: str, candle: dict, volume: dict) -> list[dict]:
        candidate = self.pending[symbol]
        candidate.age += 1
        zone = candidate.zone
        buffer = self._buffer(zone, candle["c"])
        up = candidate.scenario.endswith("UP")
        breakout = candidate.scenario.startswith("BREAKOUT")
        boundary = zone.high if up else zone.low
        opposite = zone.low if up else zone.high
        beyond = candle["c"] > boundary if up else candle["c"] < boundary
        back_through = candle["c"] < opposite if up else candle["c"] > opposite
        result: list[dict] = []

        if breakout:
            # A retest may enter only the near third of a zone (plus a small
            # price tolerance). A sweep across most of the zone and a close
            # back outside is a different event, not a successful hold.
            allowed_penetration = (zone.high - zone.low) * .35 + buffer
            deep = (candle["l"] < boundary - allowed_penetration if up else
                    candle["h"] > boundary + allowed_penetration)
            shallow_retest = (candle["l"] <= boundary + buffer if up else
                              candle["h"] >= boundary - buffer)
            if back_through:
                # A break that loses the whole zone creates a *new* failed-break
                # hypothesis. A later candle must still prove no reclaim.
                candidate.scenario = "FAILED_BREAK_" + ("UP" if up else "DOWN")
                candidate.direction = "SHORT" if up else "LONG"
                candidate.event_ts = candle["t"]
                candidate.age = 0
                result.append(self._record("candidate", symbol, candle, candidate.plan_ts,
                                           scenario=candidate.scenario,
                                           direction=candidate.direction,
                                           zone=asdict(zone), event_ts=candle["t"],
                                           reason="break_lost_zone", evidence=volume))
            elif deep:
                kind = "no_trade" if beyond else "invalidated"
                result.append(self._record(kind, symbol, candle, candidate.plan_ts,
                                           scenario=candidate.scenario,
                                           reason="deep_sweep_reclaim" if beyond else "deep_retest_or_break_failed",
                                           event_ts=candidate.event_ts, evidence=volume))
                self.pending.pop(symbol, None)
            elif beyond and shallow_retest:
                result.extend(self._confirm(symbol, candle, candidate, volume))
        else:
            reclaimed = candle["c"] > boundary if up else candle["c"] < boundary
            remains_failed = candle["c"] < opposite if up else candle["c"] > opposite
            no_reclaim_wick = candle["h"] <= boundary + buffer if up else candle["l"] >= boundary - buffer
            if reclaimed:
                result.append(self._record("invalidated", symbol, candle, candidate.plan_ts,
                                           scenario=candidate.scenario, reason="level_reclaimed",
                                           event_ts=candidate.event_ts, evidence=volume))
                self.pending.pop(symbol, None)
            elif remains_failed and no_reclaim_wick:
                result.extend(self._confirm(symbol, candle, candidate, volume))
        if symbol in self.pending and self.pending[symbol].age >= CANDIDATE_EXPIRY:
            old = self.pending.pop(symbol)
            result.append(self._record("expired", symbol, candle, old.plan_ts,
                                       scenario=old.scenario, event_ts=old.event_ts,
                                       reason="no_valid_closed_candle_confirmation"))
        return result

    def _confirm(self, symbol: str, candle: dict, candidate: _Candidate,
                 volume: dict) -> list[dict]:
        invalidation, feasibility, reason = self._feasibility(candidate, candle["c"])
        self.pending.pop(symbol, None)
        common = {"scenario": candidate.scenario, "direction": candidate.direction,
                  "zone": asdict(candidate.zone), "event_ts": candidate.event_ts,
                  "price": candle["c"], "invalidation": invalidation,
                  "feasibility": feasibility, "evidence": volume,
                  "market_context": self._market_context(candidate.market_map)}
        if reason:
            return [self._record("no_trade", symbol, candle, candidate.plan_ts,
                                 reason=reason, **common)]
        row = self._record("confirmed", symbol, candle, candidate.plan_ts, **common)
        self.outcomes.setdefault(symbol, []).append(_Outcome(
            candle["t"], candidate.direction, candle["c"], invalidation))
        return [row]

    def _update_outcomes(self, symbol: str, candle: dict) -> None:
        remaining = []
        for outcome in self.outcomes.get(symbol, []):
            outcome.age += 1
            sign = 1 if outcome.direction == "LONG" else -1
            favorable = ((candle["h"] - outcome.price) if sign == 1 else
                         (outcome.price - candle["l"])) / outcome.price * 100
            adverse = ((candle["l"] - outcome.price) if sign == 1 else
                       (outcome.price - candle["h"])) / outcome.price * 100
            outcome.best_pct = max(outcome.best_pct, favorable)
            outcome.worst_pct = min(outcome.worst_pct, adverse)
            close_pct = sign * (candle["c"] - outcome.price) / outcome.price * 100
            if outcome.age == 5:
                outcome.five_close_pct = close_pct
            if outcome.age >= 15:
                self._record("outcome", symbol, candle, None,
                             confirmed_ts=outcome.confirmed_ts,
                             direction=outcome.direction,
                             five_close_pct=outcome.five_close_pct,
                             fifteen_close_pct=close_pct,
                             best_pct=outcome.best_pct,
                             worst_pct=outcome.worst_pct)
            else:
                remaining.append(outcome)
        self.outcomes[symbol] = remaining

    def on_kline(self, symbol: str, k: Mapping) -> list[dict]:
        candle = self._minute(k)
        if candle is None:
            return []
        history = self.history.setdefault(symbol, deque(maxlen=MAX_MINUTES))
        if history and candle["t"] <= history[-1]["t"]:
            return []
        events: list[dict] = []
        if history and candle["t"] != history[-1]["t"] + MINUTE_MS:
            old = self.pending.pop(symbol, None)
            if old:
                events.append(self._record("censored_gap", symbol, candle, old.plan_ts,
                                           event_ts=old.event_ts, scenario=old.scenario))
            for outcome in self.outcomes.pop(symbol, []):
                events.append(self._record("censored_gap", symbol, candle, None,
                                           confirmed_ts=outcome.confirmed_ts))
            history.clear()
            self.plans.pop(symbol, None)
            self._gap_quarantine[symbol] = candle["t"]
        prior = self.plans.get(symbol)
        evidence = self._volume_evidence(list(history), candle)
        if prior and history and prior.as_of_ms < candle["t"]:
            self._update_outcomes(symbol, candle)
            if symbol in self.pending:
                events.extend(self._advance(symbol, candle, evidence))
            elif (prior.decision == "WAIT" and
                  prior.market_map.bar_counts.get("15m", 0) >= 12 and
                  self._gap_quarantine.get(symbol, -1) < prior.market_map.data_cutoffs.get("15m", -1)):
                events.extend(self._watch(symbol, candle, prior, evidence))
                events.extend(self._start_candidate(symbol, candle, history[-1]["c"],
                                                    prior, evidence))
        history.append(candle)
        as_of_ms = candle["t"] + MINUTE_MS - 1
        higher_changed = self._complete_higher(symbol, as_of_ms)
        self._refresh_plan(symbol, as_of_ms,
                           reuse_structure=not higher_changed and prior is not None)
        return events

    async def flush_records(self) -> None:
        """Append unflushed research events to JSONL without losing new rows."""
        async with self._write_lock:
            if not self.records:
                return
            rows = list(self.records)
            self.records.clear()
            path = self.journal_path

            def write() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")

            try:
                await asyncio.to_thread(write)
            except Exception:
                self.records.extendleft(reversed(rows))
                raise
