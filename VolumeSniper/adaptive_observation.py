"""Point-in-time chart facts and structural paper plans for any closed minute.

This observes every eligible closed candle. It does not require one of v3's
four named setup families, assign direction, or submit an exchange order.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

from .adaptive_scenarios import FeatureSnapshot, FeatureValue
from .market_structure import MINUTE_MS, MarketMap, StructureZone


@dataclass(frozen=True, slots=True)
class StructuralPaperPlan:
    invalidation: float
    planned_target: float
    risk_pct: float
    room_r: float


@dataclass(frozen=True, slots=True)
class ChartObservation:
    snapshot: FeatureSnapshot
    plans: Mapping[str, StructuralPaperPlan]


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {name}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"invalid {name}")
    return number


def _plan(market_map: MarketMap, direction: str, price: float,
          tick_size: float) -> StructuralPaperPlan | None:
    anchor: StructureZone | None = (market_map.nearest_support if direction == "LONG"
                                    else market_map.nearest_resistance)
    if anchor is None:
        return None
    target_zone = market_map.opposing_zone(direction, price)
    if target_zone is None:
        return None
    buffer = max((anchor.high - anchor.low) * 0.1, price * 0.0002,
                 2 * tick_size)
    stop = anchor.low - buffer if direction == "LONG" else anchor.high + buffer
    target = target_zone.low if direction == "LONG" else target_zone.high
    risk = (price - stop) if direction == "LONG" else (stop - price)
    room = (target - price) if direction == "LONG" else (price - target)
    if stop <= 0 or target <= 0 or risk < 2 * tick_size or room <= 0:
        return None
    return StructuralPaperPlan(stop, target, risk / price * 100, room / risk)


def observe_chart(market_map: MarketMap, candle: Mapping[str, object],
                  prior_candles: Sequence[Mapping[str, object]], *,
                  tick_size: float,
                  derivatives: Mapping[str, object] | None = None) -> ChartObservation:
    """Make an action-independent snapshot plus predeclared directional plans.

    ``market_map`` is the plan computed after this candle closed. The replay
    clock and all higher-timeframe cutoffs must be no later than that close.
    ``prior_candles`` must end before the candle under test.
    """
    ts = candle.get("t")
    if type(ts) is not int or ts < 0 or ts % MINUTE_MS or not candle.get("x"):
        raise ValueError("expected a closed aligned 1m candle")
    asof = ts + MINUTE_MS - 1
    if market_map.as_of_ms != asof:
        raise ValueError("market map clock differs from closed candle")
    if market_map.data_cutoffs.get("1m") != asof or any(
            cutoff is not None and cutoff > asof
            for cutoff in market_map.data_cutoffs.values()):
        raise ValueError("market map contains a future or missing 1m cutoff")
    tick = _finite(tick_size, "tick_size")
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    o = _finite(candle.get("o"), "open")
    h = _finite(candle.get("h"), "high")
    l = _finite(candle.get("l"), "low")
    c = _finite(candle.get("c"), "close")
    v = _finite(candle.get("v"), "volume")
    if (l <= 0 or v < 0 or l > min(o, c) or h < max(o, c) or
            market_map.price is None or not math.isclose(c, market_map.price, rel_tol=1e-10)):
        raise ValueError("candle and market map price disagree")
    if any(type(bar.get("t")) is not int or bar["t"] >= ts for bar in prior_candles):
        raise ValueError("prior_candles include current or future data")
    features: dict[str, FeatureValue] = {}

    def add(name: str, value: float | int | None, clock: int = asof) -> None:
        if value is not None:
            features[name] = FeatureValue(value, clock)

    previous_volume = [_finite(bar.get("v"), "prior volume")
                       for bar in prior_candles[-60:]]
    positive_volume = [value for value in previous_volume if value > 0]
    if len(positive_volume) >= 10:
        add("rvol", v / statistics.median(positive_volume))
    add("body_to_range", abs(c - o) / (h - l) if h > l else 0)
    taker_buy = candle.get("V")
    if taker_buy is not None and v > 0:
        buy = _finite(taker_buy, "taker buy volume")
        if not 0 <= buy <= v:
            raise ValueError("invalid taker buy volume")
        add("taker_delta_pct", (2 * buy - v) / v * 100)
    closes = [_finite(bar.get("c"), "prior close")
              for bar in prior_candles[-19:]] + [c]
    if len(closes) >= 10:
        path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
        add("trend_efficiency", abs(closes[-1] - closes[0]) / path if path else 0)
    add("regime_trend", int(market_map.regime == "trend"))
    add("regime_range", int(market_map.regime == "range"))
    add("trend_up", int(market_map.trend_direction == "up"))
    add("trend_down", int(market_map.trend_direction == "down"))
    for name, zone in (("support", market_map.nearest_support),
                       ("resistance", market_map.nearest_resistance)):
        if zone is None:
            continue
        if zone.confirmed_ts > asof or zone.last_touch_ts > asof:
            raise ValueError("future structural zone")
        add(f"{name}_touches", zone.touches, zone.last_touch_ts)
        distance = (max(c - zone.high, 0) if name == "support" else
                    max(zone.low - c, 0))
        add(f"distance_to_{name}_pct", distance / c * 100)
    plans: dict[str, StructuralPaperPlan] = {}
    structure_ready = (market_map.bar_counts.get("15m", 0) >= 12 and
                       market_map.regime != "unclear" and
                       not any(issue.startswith(("stale_", "missing_"))
                               for issue in market_map.quality_issues))
    if structure_ready:
        for direction in ("LONG", "SHORT"):
            plan = _plan(market_map, direction, c, tick)
            if plan is not None:
                plans[direction] = plan
                prefix = direction.lower()
                add(f"{prefix}_risk_pct", plan.risk_pct)
                add(f"{prefix}_room_r", plan.room_r)
    if derivatives is not None:
        derivative_asof = derivatives.get("as_of")
        if type(derivative_asof) is not int or derivative_asof > asof:
            raise ValueError("derivatives snapshot is from the future")
        metrics = derivatives.get("metrics")
        if not isinstance(metrics, Mapping):
            raise ValueError("invalid derivatives metrics")

        def metric(name: str, key: str) -> tuple[float, int] | None:
            row = metrics.get(name)
            if not isinstance(row, Mapping) or row.get("status") != "ok":
                return None
            observed, fetched = row.get("observed_at"), row.get("fetched_at")
            if (type(observed) is not int or type(fetched) is not int or
                    observed > asof or fetched > asof or observed < 0 or fetched < 0):
                return None
            values = row.get("value")
            if not isinstance(values, Mapping) or values.get(key) is None:
                return None
            return _finite(values[key], f"{name}.{key}"), max(observed, fetched)

        names = {
            "oi_change_pct": ("open_interest_history", "notional_change_pct"),
            "top_trader_account_ratio": ("top_trader_accounts", "long_short_ratio"),
            "top_trader_position_ratio": ("top_trader_positions", "long_short_ratio"),
            "global_long_short_ratio": ("long_short_accounts", "long_short_ratio"),
            "taker_buy_sell_ratio": ("taker_buy_sell", "buy_sell_ratio"),
            "funding_rate_pct": ("funding", "latest_funding_rate"),
            "spread_bps": ("order_book", "spread_bps"),
            "depth_imbalance": ("order_book", "top20_imbalance"),
        }
        for feature, (source, key) in names.items():
            value = metric(source, key)
            if value is not None:
                amount, clock = value
                add(feature, amount * 100 if feature == "funding_rate_pct"
                    else amount, clock)
        basis = metric("basis", "basis")
        index = metric("basis", "index_price")
        if basis is not None and index is not None and index[0] > 0:
            add("basis_bps", basis[0] / index[0] * 10_000,
                max(basis[1], index[1]))
        oi_value = metric("open_interest_history", "notional_usd_approx")
        supply = metric("open_interest_history", "cmc_circulating_supply")
        mark_index = metric("funding", "index_price")
        if (oi_value is not None and supply is not None and
                mark_index is not None and supply[0] > 0 and mark_index[0] > 0):
            add("oi_to_market_cap_ratio",
                oi_value[0] / (supply[0] * mark_index[0]),
                max(oi_value[1], supply[1], mark_index[1]))
    return ChartObservation(FeatureSnapshot(asof, features), plans)
