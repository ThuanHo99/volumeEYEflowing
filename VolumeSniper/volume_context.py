"""Closed-candle market plans and prospective volume/price scenarios.

The range plan is built at the previous close, before the event candle exists.
Volume can create a non-directional watch; only subsequent price acceptance or
rejection can create a directional scenario. All timestamps are UTC.
"""

import asyncio
import json
import math
import os
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.config import DATA_DIR
from . import config
from .utils import interval_minutes


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    taker_buy: float | None

    @classmethod
    def from_kline(cls, k: dict) -> "Candle":
        volume = float(k["v"])
        buy = float(k["V"]) if "V" in k else None
        if buy is not None and not 0 <= buy <= volume:
            buy = None
        close = float(k["c"])
        return cls(
            ts=int(k["t"]), open=float(k["o"]), high=float(k["h"]),
            low=float(k["l"]), close=close, volume=volume,
            quote_volume=float(k.get("q", volume * close)), taker_buy=buy,
        )

    @property
    def delta(self) -> float | None:
        return None if self.taker_buy is None else 2 * self.taker_buy - self.volume


@dataclass(frozen=True)
class ContextSignal:
    symbol: str
    direction: str
    scenario: str  # BREAKOUT | FAILURE
    level: float
    price: float  # price at confirmation, never the earlier spike price
    invalidation: float
    rvol: float
    baseline: str  # seasonal | rolling (provisional)
    delta_pct: float | None
    cvd: float | None
    event_ts: int
    confirmed_ts: int
    plan_ts: int | None = None
    plan_upper: float | None = None
    plan_lower: float | None = None
    confirmed_close_ts: int | None = None  # when the closed-candle evidence exists


@dataclass(frozen=True)
class ContextPlan:
    symbol: str
    ready_ts: int  # close timestamp of the last candle used by this plan
    upper: float
    lower: float
    quieting: bool  # descriptive only; never a breakout prediction


@dataclass
class _Candidate:
    side: str  # side of the attempted break
    kind: str  # breakout | rejection
    level: float
    rvol: float
    baseline: str
    delta_pct: float | None
    cvd: float | None
    event_ts: int
    event_high: float
    event_low: float
    plan_ts: int
    plan_upper: float
    plan_lower: float
    plan_quieting: bool
    age: int = 0
    outside: int = 1
    inside: int = 0
    inside_close: float | None = None


@dataclass
class _Outcome:
    signal: ContextSignal
    age: int = 0
    best: float = 0.0
    worst: float = 0.0


class SeasonalVolumeBaseline:
    """Median minute-volume by UTC weekday/hour from earlier complete weeks.

    Each complete hour contributes one median, so a busy hour cannot dominate
    a slot. Until two earlier weeks exist, callers use an explicit rolling
    provisional baseline. Current hour and current candle never enter RVOL.
    """

    def __init__(self, path: Path | None = None):
        base = Path(DATA_DIR)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent / base
        self.path = path or base / "vs_seasonal_volume.json"
        self.samples: dict[str, list[list[float]]] = {}
        self.current: dict[str, tuple[int, list[float]]] = {}
        self.dirty = False
        self._revision = 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.samples = {k: v for k, v in raw.items() if isinstance(v, list)}
        except (OSError, ValueError):
            pass

    @staticmethod
    def _slot(symbol: str, hour_ms: int) -> str:
        dt = datetime.fromtimestamp(hour_ms / 1000, tz=timezone.utc)
        return f"{symbol}|{dt.weekday()}|{dt.hour}"

    def baseline(self, symbol: str, ts: int) -> float | None:
        hour = ts // 3_600_000 * 3_600_000
        values = [float(v) for h, v in self.samples.get(self._slot(symbol, hour), [])
                  if int(h) < hour and hour - int(h) <= 8 * 7 * 24 * 3_600_000]
        return statistics.median(values) if len(values) >= 2 else None

    def observe(self, symbol: str, candle: Candle) -> None:
        hour = candle.ts // 3_600_000 * 3_600_000
        active = self.current.get(symbol)
        if active is None:
            self.current[symbol] = (hour, [candle.volume])
            return
        old_hour, volumes = active
        if hour == old_hour:
            volumes.append(candle.volume)
            return
        if hour < old_hour:  # delayed or duplicate stream event
            return
        expected = max(1, 60 // interval_minutes(config.KLINE_INTERVAL))
        if len(volumes) >= max(1, math.ceil(expected * 0.75)):
            key = self._slot(symbol, old_hour)
            samples = [row for row in self.samples.get(key, [])
                       if old_hour - int(row[0]) <= 8 * 7 * 24 * 3_600_000]
            samples.append([old_hour, statistics.median(volumes)])
            self.samples[key] = samples[-8:]
            self.dirty = True
            self._revision += 1
        self.current[symbol] = (hour, [candle.volume])

    async def save(self) -> None:
        if not self.dirty:
            return
        snapshot = dict(self.samples)
        revision = self._revision
        path = self.path

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)

        await asyncio.to_thread(write)
        if self._revision == revision:
            self.dirty = False


class VolumeContextEngine:
    def __init__(self, baseline: SeasonalVolumeBaseline | None = None):
        self.baseline = baseline or SeasonalVolumeBaseline()
        self.history: dict[str, deque[Candle]] = {}
        self.plans: dict[str, ContextPlan] = {}
        self.pending: dict[str, _Candidate] = {}
        self.watch_events: deque[dict] = deque(maxlen=1_000)
        self.watch_streaks: dict[tuple[str, str], tuple[int, int]] = {}
        self.last_alert: dict[str, int] = {}
        self.outcomes: dict[str, list[_Outcome]] = {}
        self.records: deque[dict] = deque(maxlen=20_000)
        self._write_lock = asyncio.Lock()
        self.journal_path = self.baseline.path.with_name("vs_context_events.jsonl")

    def seed(self, symbol: str, klines: list[dict]) -> None:
        history = self.history.setdefault(symbol, deque(maxlen=80))
        for k in klines:
            candle = Candle.from_kline(k)
            if not history or candle.ts > history[-1].ts:
                history.append(candle)
        plan = self._build_plan(symbol, list(history))
        if plan:
            self.plans[symbol] = plan

    def _build_plan(self, symbol: str, history: list[Candle]) -> ContextPlan | None:
        lookback = max(5, config.CONTEXT_LOOKBACK_CANDLES)
        if len(history) < lookback:
            return None
        recent = history[-lookback:]
        half = min(5, len(recent) // 2)
        previous, latest = recent[-2 * half:-half], recent[-half:]
        quieting = (
            statistics.median(x.volume for x in latest) < statistics.median(x.volume for x in previous)
            and max(x.high for x in latest) - min(x.low for x in latest)
            < max(x.high for x in previous) - min(x.low for x in previous)
        )
        return ContextPlan(
            symbol=symbol,
            ready_ts=recent[-1].ts + interval_minutes(config.KLINE_INTERVAL) * 60_000 - 1,
            upper=max(x.high for x in recent), lower=min(x.low for x in recent),
            quieting=quieting,
        )

    def drain_watches(self) -> list[dict]:
        watches = list(self.watch_events)
        self.watch_events.clear()
        return watches

    def on_kline(self, symbol: str, k: dict) -> ContextSignal | None:
        if not bool(k.get("x")):
            return None
        candle = Candle.from_kline(k)
        history = self.history.setdefault(symbol, deque(maxlen=80))
        if history and candle.ts <= history[-1].ts:
            return None
        interval_ms = interval_minutes(config.KLINE_INTERVAL) * 60_000
        if history and candle.ts - history[-1].ts > 2 * interval_ms:
            # A reconnect gap invalidates both range levels and pending thesis.
            history.clear()
            self.plans.pop(symbol, None)
            self.pending.pop(symbol, None)
            for key in (symbol, "upper"), (symbol, "lower"):
                self.watch_streaks.pop(key, None)
            for item in self.outcomes.pop(symbol, []):
                self.records.append({"kind": "censored_gap", "symbol": symbol,
                                     "confirmed_ts": item.signal.confirmed_ts,
                                     "ts": candle.ts})
        prior = list(history)
        plan = self.plans.get(symbol)
        self._update_outcomes(symbol, candle)
        emitted = self._advance(symbol, candle) if symbol in self.pending else None
        if emitted is None and symbol not in self.pending:
            self._start_candidate(symbol, candle, prior, plan)
        history.append(candle)
        next_plan = self._build_plan(symbol, list(history))
        if next_plan:
            self.plans[symbol] = next_plan
        self.baseline.observe(symbol, candle)
        return emitted

    def _start_candidate(self, symbol: str, c: Candle, prior: list[Candle],
                         plan: ContextPlan | None) -> None:
        lookback = max(5, config.CONTEXT_LOOKBACK_CANDLES)
        if plan is None or len(prior) < lookback or c.close <= 0 or c.volume <= 0:
            return
        recent = prior[-lookback:]
        seasonal = self.baseline.baseline(symbol, c.ts)
        baseline = seasonal if seasonal is not None else statistics.median(x.volume for x in recent)
        if baseline <= 0 or c.volume / baseline < config.CONTEXT_RVOL_MIN:
            return
        if c.quote_volume < config.MIN_AVG_VOL_USDT:
            return
        high, low = plan.upper, plan.lower
        tol = config.CONTEXT_LEVEL_TOLERANCE_PCT / 100
        attempt: tuple[str, str, float] | None = None
        if c.close > high * (1 + tol):
            attempt = ("LONG", "breakout", high)
        elif c.close < low * (1 - tol):
            attempt = ("SHORT", "breakout", low)
        elif c.high > high * (1 + tol) and c.close <= high:
            attempt = ("LONG", "rejection", high)
        elif c.low < low * (1 - tol) and c.close >= low:
            attempt = ("SHORT", "rejection", low)
        if attempt is None:
            self._watch_contested_edge(symbol, c, recent, plan, seasonal,
                                       baseline, tol)
            return
        delta_pct = None if c.delta is None else c.delta / c.volume * 100
        deltas = [x.delta for x in recent if x.delta is not None]
        if c.delta is not None:
            deltas.append(c.delta)
        self.pending[symbol] = _Candidate(
            side=attempt[0], kind=attempt[1], level=attempt[2],
            rvol=c.volume / baseline,
            baseline="seasonal" if seasonal is not None else "rolling (provisional)",
            delta_pct=delta_pct, cvd=sum(deltas) if deltas else None,
            event_ts=c.ts,
            event_high=c.high, event_low=c.low,
            plan_ts=plan.ready_ts, plan_upper=plan.upper,
            plan_lower=plan.lower, plan_quieting=plan.quieting,
        )
        self.records.append({
            "kind": "candidate", "symbol": symbol, "ts": c.ts,
            "attempt_side": attempt[0], "setup": attempt[1],
            "level": attempt[2], "event_close": c.close,
            "plan_ts": plan.ready_ts, "plan_upper": plan.upper,
            "plan_lower": plan.lower,
            "observed_at": c.ts + interval_minutes(config.KLINE_INTERVAL) * 60_000 - 1,
            "rvol": c.volume / baseline,
            "baseline": "seasonal" if seasonal is not None else "rolling (provisional)",
            "delta_pct": delta_pct,
        })

    def _watch_contested_edge(self, symbol: str, c: Candle,
                              recent: list[Candle], plan: ContextPlan,
                              seasonal: float | None, baseline: float,
                              tol: float) -> None:
        """Record effort without price progress as a watch, never a direction."""
        width = plan.upper - plan.lower
        if width <= 0 or not plan.lower <= c.close <= plan.upper:
            return
        edge_margin = max(width * 0.1, c.close * tol)
        upper_distance = plan.upper - c.close
        lower_distance = c.close - plan.lower
        if min(upper_distance, lower_distance) > edge_margin:
            return
        edge = "upper" if upper_distance <= lower_distance else "lower"
        key = (symbol, edge)
        last_ts, last_count = self.watch_streaks.get(key, (-10**18, 0))
        interval_ms = interval_minutes(config.KLINE_INTERVAL) * 60_000
        count = last_count + 1 if c.ts - last_ts <= 3 * interval_ms else 1
        self.watch_streaks[key] = (c.ts, count)
        deltas = [x.delta for x in recent if x.delta is not None]
        if c.delta is not None:
            deltas.append(c.delta)
        row = {
            "kind": "watch", "symbol": symbol, "ts": c.ts,
            "observed_at": c.ts + interval_ms - 1,
            "plan_ts": plan.ready_ts, "plan_upper": plan.upper,
            "plan_lower": plan.lower, "edge": edge,
            "repeated_tests": count,
            "price": c.close, "rvol": c.volume / baseline,
            "baseline": "seasonal" if seasonal is not None else "rolling (provisional)",
            "delta_pct": None if c.delta is None else c.delta / c.volume * 100,
            "cvd": sum(deltas) if deltas else None,
            "quieting_before_event": plan.quieting,
            "interpretation": "high effort near a preplanned edge; direction unresolved",
        }
        self.records.append(row)
        self.watch_events.append(row)

    def _advance(self, symbol: str, c: Candle) -> ContextSignal | None:
        p = self.pending[symbol]
        p.age += 1
        tol = config.CONTEXT_LEVEL_TOLERANCE_PCT / 100
        above = c.close > p.level
        outside = above if p.side == "LONG" else c.close < p.level
        near_retest = (c.low <= p.level * (1 + tol) if p.side == "LONG"
                       else c.high >= p.level * (1 - tol))
        result: tuple[str, str] | None = None
        if p.kind == "breakout":
            if outside:
                p.outside += 1
                p.inside = 0
                p.inside_close = None
                held_retest = (c.close > p.level * (1 + tol) if p.side == "LONG"
                               else c.close < p.level * (1 - tol))
                if near_retest and held_retest:
                    result = (p.side, "BREAKOUT")
            else:
                p.inside += 1
                moves_away = (c.close < p.inside_close if p.side == "LONG"
                              else c.close > p.inside_close) if p.inside_close is not None else False
                if p.inside >= 2 and moves_away and abs(c.close - p.level) / p.level >= tol:
                    result = ("SHORT" if p.side == "LONG" else "LONG", "FAILURE")
                p.inside_close = c.close
        else:  # wick through level, then close back inside
            if outside:
                del self.pending[symbol]  # reclaimed level: rejection thesis invalid
                self.records.append({"kind": "invalidated", "symbol": symbol,
                                     "event_ts": p.event_ts, "ts": c.ts})
                return None
            p.inside += 1
            moved_away = (c.close < p.event_low if p.side == "LONG"
                          else c.close > p.event_high)
            if moved_away:
                result = ("SHORT" if p.side == "LONG" else "LONG", "FAILURE")
        if p.age >= config.CONTEXT_CONFIRM_CANDLES or result is not None:
            del self.pending[symbol]
        if result is None:
            if p.age >= config.CONTEXT_CONFIRM_CANDLES:
                self.records.append({"kind": "expired", "symbol": symbol,
                                     "event_ts": p.event_ts, "ts": c.ts})
            return None
        if c.ts - self.last_alert.get(symbol, -10**18) < config.ALERT_COOLDOWN_SEC * 1000:
            return None
        self.last_alert[symbol] = c.ts
        direction, scenario = result
        invalidation = p.level * (1 - tol if direction == "LONG" else 1 + tol)
        signal = ContextSignal(
            symbol=symbol, direction=direction, scenario=scenario,
            level=p.level, price=c.close, invalidation=invalidation,
            rvol=p.rvol, baseline=p.baseline, delta_pct=p.delta_pct,
            cvd=p.cvd, event_ts=p.event_ts, confirmed_ts=c.ts,
            plan_ts=p.plan_ts, plan_upper=p.plan_upper,
            plan_lower=p.plan_lower,
            confirmed_close_ts=c.ts + interval_minutes(config.KLINE_INTERVAL) * 60_000 - 1,
        )
        self.records.append({
            "kind": "confirmed", "symbol": symbol, "event_ts": p.event_ts,
            "ts": c.ts, "observed_at": signal.confirmed_close_ts,
            "direction": direction, "scenario": scenario,
            "level": p.level, "price": c.close, "invalidation": invalidation,
            "plan_ts": p.plan_ts, "plan_upper": p.plan_upper,
            "plan_lower": p.plan_lower,
            "rvol": p.rvol, "baseline": p.baseline,
            "delta_pct": p.delta_pct, "cvd": p.cvd,
        })
        self.outcomes.setdefault(symbol, []).append(_Outcome(signal))
        return signal

    def _update_outcomes(self, symbol: str, candle: Candle) -> None:
        active = self.outcomes.get(symbol, [])
        remaining: list[_Outcome] = []
        for item in active:
            item.age += 1
            entry = item.signal.price
            sign = 1 if item.signal.direction == "LONG" else -1
            favorable = (candle.high - entry) / entry * 100 if sign == 1 else (entry - candle.low) / entry * 100
            adverse = (candle.low - entry) / entry * 100 if sign == 1 else (entry - candle.high) / entry * 100
            item.best = max(item.best, favorable)
            item.worst = min(item.worst, adverse)
            if item.age in (5, 15):
                self.records.append({
                    "kind": "outcome", "symbol": symbol,
                    "event_ts": item.signal.event_ts,
                    "confirmed_ts": item.signal.confirmed_ts,
                    "ts": candle.ts, "horizon_candles": item.age,
                    "direction": item.signal.direction,
                    "return_pct_gross": sign * (candle.close - entry) / entry * 100,
                    "max_favorable_pct": item.best,
                    "max_adverse_pct": item.worst,
                })
            if item.age < 15:
                remaining.append(item)
        self.outcomes[symbol] = remaining

    async def flush_records(self) -> None:
        async with self._write_lock:
            if not self.records:
                return
            records = list(self.records)
            path = self.journal_path

            def write() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    for row in records:
                        f.write(json.dumps(row, separators=(",", ":")) + "\n")

            await asyncio.to_thread(write)
            for _ in records:
                self.records.popleft()
