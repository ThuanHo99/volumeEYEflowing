"""Point-in-time price structure from closed candles.

This module describes observable levels; it does not assign a trade direction.
All timestamps are Unix milliseconds in UTC. ``ts`` is a candle's open time,
and a candle may enter the map only after its computed ``close_ts``. In a
historical replay, callers must pass the replay clock as ``as_of_ms``.

The zone and regime rules are deterministic research hypotheses, not learned
support/resistance probabilities. The caller should retain the map's cutoffs
and provenance when journaling a prospective plan.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass


MINUTE_MS = 60_000
TIMEFRAMES = {"15m": 15, "1h": 60, "4h": 240}
MAX_ONE_MINUTE = 14 * 24 * 60
MAX_BARS_PER_FRAME = 240
MAX_ZONES_PER_FRAME_SIDE = 6


@dataclass(frozen=True)
class PriceBar:
    timeframe: str
    ts: int
    close_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class StructureZone:
    side: str  # support (swing low) | resistance (swing high)
    low: float
    high: float
    timeframe: str
    origin_ts: int  # first swing's open; it was not yet known at this time
    confirmed_ts: int  # latest confirming bar's close
    last_touch_ts: int  # close of latest distinct visit to the zone
    touches: int
    source_pivot_ts: tuple[int, ...]
    source_confirmed_ts: tuple[int, ...]
    touch_ts: tuple[int, ...]


@dataclass(frozen=True)
class MarketMap:
    symbol: str
    as_of_ms: int
    price: float | None
    regime: str  # range | trend | transition | unclear
    trend_direction: str | None  # up | down when regime is trend
    regime_timeframe: str | None
    zones: tuple[StructureZone, ...]
    nearest_support: StructureZone | None
    nearest_resistance: StructureZone | None
    data_cutoffs: dict[str, int | None]
    bar_counts: dict[str, int]
    quality_issues: tuple[str, ...]

    def opposing_zone(self, direction: str, from_price: float) -> StructureZone | None:
        """Nearest known structural obstacle beyond ``from_price``.

        ``LONG`` seeks a swing-high resistance; ``SHORT`` seeks a swing-low
        support. A level already behind the reference price is not a target.
        """
        side = "resistance" if direction.upper() == "LONG" else "support"
        if direction.upper() not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        candidates = [z for z in self.zones if z.side == side and
                      (z.high > from_price if side == "resistance" else z.low < from_price)]
        if not candidates:
            return None
        return min(candidates, key=lambda z: (
            max(z.low - from_price, 0) if side == "resistance" else
            max(from_price - z.high, 0),
            -TIMEFRAMES[z.timeframe], -z.touches,
        ))

    def to_dict(self) -> dict:
        return asdict(self)


def _field(row: object, long: str, short: str, index: int):
    if isinstance(row, Mapping):
        return row[long] if long in row else row[short]
    if isinstance(row, (tuple, list)):
        return row[index]
    return getattr(row, long)


def _tail(rows: Sequence[object], limit: int) -> tuple[object, ...]:
    # Handles both lists and the deque used by the existing candle engine.
    return tuple(rows)[-limit:]


def _closed_flag(row: object) -> bool:
    if isinstance(row, Mapping):
        return bool(row.get("x", row.get("closed", True)))
    return bool(getattr(row, "closed", True))


def _coerce_bar(row: object, timeframe: str, minutes: int, as_of_ms: int) -> PriceBar | None:
    if not _closed_flag(row):
        return None
    try:
        ts = int(_field(row, "ts", "t", 0))
        close_ts = ts + minutes * MINUTE_MS - 1
        if ts < 0 or close_ts > as_of_ms or ts % (minutes * MINUTE_MS):
            return None
        o = float(_field(row, "open", "o", 1))
        h = float(_field(row, "high", "h", 2))
        l = float(_field(row, "low", "l", 3))
        c = float(_field(row, "close", "c", 4))
        v = float(_field(row, "volume", "v", 5))
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return None
    if (not all(math.isfinite(x) for x in (o, h, l, c, v)) or
            l <= 0 or v < 0 or l > min(o, c) or h < max(o, c)):
        return None
    return PriceBar(timeframe, ts, close_ts, o, h, l, c, v)


def _continuous_suffix(bars: Sequence[PriceBar], minutes: int) -> tuple[PriceBar, ...]:
    """A data gap invalidates older bars as neighbours of recent pivots."""
    if not bars:
        return ()
    step = minutes * MINUTE_MS
    start = len(bars) - 1
    while start and bars[start].ts - bars[start - 1].ts == step:
        start -= 1
    return tuple(bars[start:])


def _normalize_rows(rows: Sequence[object], timeframe: str, minutes: int,
                    as_of_ms: int) -> tuple[PriceBar, ...]:
    # Equal timestamps with different values are ambiguous. Drop both, then
    # use only the latest contiguous run; never manufacture a pivot over a gap.
    by_ts: dict[int, PriceBar | None] = {}
    input_limit = MAX_ONE_MINUTE if minutes == 1 else MAX_BARS_PER_FRAME * 2
    output_limit = MAX_ONE_MINUTE if minutes == 1 else MAX_BARS_PER_FRAME
    for row in _tail(rows, input_limit):
        bar = _coerce_bar(row, timeframe, minutes, as_of_ms)
        if bar is None:
            continue
        if bar.ts in by_ts and by_ts[bar.ts] != bar:
            by_ts[bar.ts] = None
        elif bar.ts not in by_ts:
            by_ts[bar.ts] = bar
    ordered = [bar for _, bar in sorted(by_ts.items()) if bar is not None]
    return _continuous_suffix(ordered[-output_limit:], minutes)


def aggregate_closed_bars(one_minute: Sequence[object], timeframe_minutes: int,
                          *, as_of_ms: int) -> tuple[PriceBar, ...]:
    """Aggregate only full, gap-free 1m buckets that had closed by ``as_of_ms``.

    The final partial higher-timeframe bucket is omitted. Invalid or duplicate
    source minutes make their entire bucket ineligible.
    """
    if timeframe_minutes < 1:
        raise ValueError("timeframe_minutes must be positive")
    minute_bars = _normalize_rows(_tail(one_minute, MAX_ONE_MINUTE), "1m", 1, as_of_ms)
    width = timeframe_minutes * MINUTE_MS
    buckets: dict[int, list[PriceBar]] = {}
    for bar in minute_bars:
        start = bar.ts // width * width
        buckets.setdefault(start, []).append(bar)
    result: list[PriceBar] = []
    for ts, members in sorted(buckets.items()):
        if (ts + width - 1 > as_of_ms or len(members) != timeframe_minutes or
                members[0].ts != ts or members[-1].ts != ts + width - MINUTE_MS):
            continue
        result.append(PriceBar(
            f"{timeframe_minutes}m", ts, ts + width - 1,
            members[0].open, max(b.high for b in members),
            min(b.low for b in members), members[-1].close,
            sum(b.volume for b in members),
        ))
    return _continuous_suffix(result[-MAX_BARS_PER_FRAME:], timeframe_minutes)


@dataclass(frozen=True)
class _Pivot:
    side: str
    level: float
    low: float
    high: float
    pivot_ts: int
    confirmed_ts: int


def _pivots(bars: Sequence[PriceBar]) -> list[_Pivot]:
    if len(bars) < 5:
        return []
    typical_range = statistics.median(max(b.high - b.low, b.close * .0001) for b in bars)
    found: list[_Pivot] = []
    for i in range(2, len(bars) - 2):
        bar = bars[i]
        left, right = bars[i - 2:i], bars[i + 1:i + 3]
        width = min(bar.close * .005, max(bar.close * .0005, typical_range * .25))
        if bar.high > max(x.high for x in left) and bar.high >= max(x.high for x in right):
            found.append(_Pivot("resistance", bar.high,
                                bar.high - width, bar.high,
                                bar.ts, right[-1].close_ts))
        if bar.low < min(x.low for x in left) and bar.low <= min(x.low for x in right):
            found.append(_Pivot("support", bar.low, bar.low,
                                bar.low + width,
                                bar.ts, right[-1].close_ts))
    return found


def _zones_for_frame(timeframe: str, bars: Sequence[PriceBar]) -> tuple[StructureZone, ...]:
    pivots = _pivots(bars)
    if not pivots:
        return ()
    clusters: list[list[_Pivot]] = []
    # Group only pivots of the same role and timeframe. The width caps prevent
    # a chain of gradually drifting highs from becoming one very broad zone.
    for pivot in pivots:
        matching = []
        for group in clusters:
            if group[0].side != pivot.side:
                continue
            centre = statistics.median(p.level for p in group)
            tolerance = max(pivot.level * .0008, pivot.high - pivot.low,
                            max(p.high - p.low for p in group))
            if abs(pivot.level - centre) <= tolerance:
                matching.append((abs(pivot.level - centre), group))
        if matching:
            min(matching, key=lambda item: item[0])[1].append(pivot)
        else:
            clusters.append([pivot])
    zones: list[StructureZone] = []
    for group in clusters:
        lo, hi = min(p.low for p in group), max(p.high for p in group)
        first_ts = min(p.pivot_ts for p in group)
        touch_times: list[int] = []
        visiting = False
        for bar in bars:
            if bar.ts < first_ts:
                continue
            intersects = bar.high >= lo and bar.low <= hi
            if intersects and not visiting:
                touch_times.append(bar.close_ts)
            visiting = intersects
        # The first pivot's confirming candle is the earliest time this zone
        # could be published. A later merged pivot updates its provenance.
        zones.append(StructureZone(
            side=group[0].side, low=lo, high=hi, timeframe=timeframe,
            origin_ts=first_ts,
            confirmed_ts=max(p.confirmed_ts for p in group),
            last_touch_ts=touch_times[-1] if touch_times else group[-1].confirmed_ts,
            touches=len(touch_times),
            source_pivot_ts=tuple(p.pivot_ts for p in group),
            source_confirmed_ts=tuple(p.confirmed_ts for p in group),
            touch_ts=tuple(touch_times[-24:]),
        ))
    selected: list[StructureZone] = []
    for side in ("support", "resistance"):
        choices = [z for z in zones if z.side == side]
        selected.extend(sorted(choices, key=lambda z: (
            -len(z.source_pivot_ts), -z.touches, -z.last_touch_ts,
        ))[:MAX_ZONES_PER_FRAME_SIDE])
    return tuple(selected)


def _frame_regime(bars: Sequence[PriceBar]) -> str:
    if len(bars) < 12:
        return "unclear"
    recent = bars[-24:]
    typical_range = statistics.median(max(b.high - b.low, b.close * .0001) for b in recent)
    net = recent[-1].close - recent[0].close
    path = sum(abs(b.close - a.close) for a, b in zip(recent, recent[1:]))
    efficiency = abs(net) / path if path else 0.0
    if abs(net) >= 3 * typical_range and efficiency >= .48:
        return "trend_up" if net > 0 else "trend_down"
    prior = recent[:-2]
    if (recent[-1].close > max(b.high for b in prior) or
            recent[-1].close < min(b.low for b in prior)):
        return "transition"
    if efficiency <= .33 and abs(net) <= 2 * typical_range:
        return "range"
    return "transition"


def _infer_as_of(one_minute: Sequence[object]) -> int:
    timestamps = []
    for row in _tail(one_minute, MAX_ONE_MINUTE):
        if not _closed_flag(row):
            continue
        try:
            timestamps.append(int(_field(row, "ts", "t", 0)) + MINUTE_MS - 1)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            continue
    if not timestamps:
        raise ValueError("as_of_ms is required when no closed 1m candle is supplied")
    return max(timestamps)


def build_market_map(symbol: str, one_minute: Sequence[object], *,
                     higher_timeframes: Mapping[str, Sequence[object]] | None = None,
                     as_of_ms: int | None = None) -> MarketMap:
    """Build a bounded 15m/1h/4h structure map from point-in-time OHLCV.

    Explicit higher-timeframe rows take precedence over 1m aggregation for
    that timeframe. They may include Binance's in-progress final kline; it is
    excluded using ``as_of_ms`` regardless of whether an ``x`` flag exists.
    Rows without ``x`` must otherwise represent closed candles. Missing 4h
    data does not prevent a 15m or 1h map.
    """
    if as_of_ms is None:
        as_of_ms = _infer_as_of(one_minute)
    as_of_ms = int(as_of_ms)
    minute_bars = _normalize_rows(_tail(one_minute, MAX_ONE_MINUTE), "1m", 1, as_of_ms)
    price = minute_bars[-1].close if minute_bars else None
    frames: dict[str, tuple[PriceBar, ...]] = {}
    data_cutoffs: dict[str, int | None] = {"1m": minute_bars[-1].close_ts if minute_bars else None}
    bar_counts: dict[str, int] = {"1m": len(minute_bars)}
    issues: list[str] = []
    if not minute_bars:
        issues.append("missing_1m")
    explicit = higher_timeframes or {}
    for timeframe, minutes in TIMEFRAMES.items():
        if timeframe in explicit:
            bars = _normalize_rows(explicit[timeframe], timeframe, minutes, as_of_ms)
        else:
            bars = aggregate_closed_bars(one_minute, minutes, as_of_ms=as_of_ms)
            bars = tuple(PriceBar(timeframe, b.ts, b.close_ts, b.open, b.high,
                                  b.low, b.close, b.volume) for b in bars)
        frames[timeframe] = bars
        data_cutoffs[timeframe] = bars[-1].close_ts if bars else None
        bar_counts[timeframe] = len(bars)
        if not bars:
            issues.append(f"missing_{timeframe}")
        elif bars[-1].close_ts < as_of_ms - 2 * minutes * MINUTE_MS:
            issues.append(f"stale_{timeframe}")
    if price is None:
        for timeframe in TIMEFRAMES:
            if frames[timeframe]:
                price = frames[timeframe][-1].close
                break
    zones = tuple(z for timeframe in TIMEFRAMES for z in _zones_for_frame(timeframe, frames[timeframe]))
    primary_frame = next((tf for tf in ("1h", "15m") if len(frames[tf]) >= 12), None)
    detailed_regime = _frame_regime(frames[primary_frame]) if primary_frame else "unclear"
    if primary_frame and len(frames["4h"]) >= 12:
        higher_regime = _frame_regime(frames["4h"])
        if (higher_regime != "unclear" and detailed_regime != "unclear" and
                higher_regime != detailed_regime and
                ("trend" in higher_regime or "trend" in detailed_regime)):
            detailed_regime = "transition"
    regime = "trend" if detailed_regime.startswith("trend_") else detailed_regime
    trend_direction = detailed_regime.removeprefix("trend_") if regime == "trend" else None
    nearest_support = None
    nearest_resistance = None
    if price is not None:
        supports = [z for z in zones if z.side == "support" and z.low <= price]
        resistances = [z for z in zones if z.side == "resistance" and z.high >= price]
        if supports:
            nearest_support = min(supports, key=lambda z: (
                max(price - z.high, 0), -TIMEFRAMES[z.timeframe], -z.touches))
        if resistances:
            nearest_resistance = min(resistances, key=lambda z: (
                max(z.low - price, 0), -TIMEFRAMES[z.timeframe], -z.touches))
    return MarketMap(
        symbol=symbol, as_of_ms=as_of_ms, price=price, regime=regime,
        trend_direction=trend_direction,
        regime_timeframe=primary_frame, zones=zones,
        nearest_support=nearest_support, nearest_resistance=nearest_resistance,
        data_cutoffs=data_cutoffs, bar_counts=bar_counts,
        quality_issues=tuple(issues),
    )
