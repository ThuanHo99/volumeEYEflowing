"""Chronological, read-only shadow comparison of chart v3 and context v2.

Run ``python -m scripts.replay_chart_v3 --cost-pct 0.2``. The input cache's
precomputed higher-timeframe bars are deliberately ignored: each engine sees
only one closed 1m candle at a time and must build its own *completed* higher
timeframe bars. Outcomes use a hypothetical next-candle-open entry, an
intrabar structural stop, and 5/15-candle exits. They are research statistics,
not executable fills, a portfolio simulation, or evidence of a trading edge.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from VolumeSniper.volume_context import SeasonalVolumeBaseline, VolumeContextEngine

MINUTE_MS = 60_000
HORIZONS = (5, 15)
COUNTED_KINDS = ("plan", "watch", "no_trade", "candidate", "confirmed",
                 "invalidated", "expired", "censored_gap", "outcome")


def _default_factories() -> dict[str, Callable]:
    from VolumeSniper.chart_reasoning_v3 import ChartReasoningEngine

    return {
        "v2": lambda: VolumeContextEngine(SeasonalVolumeBaseline(Path(os.devnull))),
        "v3": ChartReasoningEngine,
    }


def _symbol(raw: str) -> str:
    return raw.replace("/", "").replace(":USDT", "")


def _closed_minutes(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Normalize 1m cache rows without pulling in its future 5m/1h/4h data."""
    candles: list[dict] = []
    skipped = Counter()
    last_ts = -1
    for c in sorted(rows, key=lambda x: int(x["start_ts"])):
        if not c.get("is_closed", False):
            skipped["unclosed"] += 1
            continue
        ts = int(c["start_ts"])
        if ts <= last_ts:
            skipped["duplicate"] += 1
            continue
        try:
            o, h, l, close, volume = (float(c[key]) for key in
                                       ("open", "high", "low", "close", "volume"))
        except (KeyError, TypeError, ValueError):
            skipped["invalid"] += 1
            continue
        if (not all(math.isfinite(x) for x in (o, h, l, close, volume))
                or min(o, h, l, close) <= 0 or volume < 0
                or not l <= min(o, close) <= max(o, close) <= h):
            skipped["invalid"] += 1
            continue
        row = {
            "t": ts, "o": o, "h": h, "l": l, "c": close, "v": volume,
            "q": float(c.get("quote_volume", volume * close)), "x": True,
        }
        if not math.isfinite(row["q"]) or row["q"] < 0:
            skipped["invalid"] += 1
            continue
        if "taker_buy_volume" in c:
            row["V"] = float(c["taker_buy_volume"])
        candles.append(row)
        last_ts = ts
    return candles, dict(skipped)


def _drain_events(engine: object, returned: object) -> list[dict]:
    """The journal is the canonical event stream; returned events are fallback."""
    journal = getattr(engine, "records", None)
    if journal is not None:
        events = [row for row in journal if isinstance(row, dict)]
        journal.clear()  # bounded live journals must not discard early replay rows
        if events:
            return events
    if isinstance(returned, dict):
        return [returned]
    if isinstance(returned, list):
        return [row for row in returned if isinstance(row, dict)]
    return []


def _event_status(row: dict, candle: dict) -> str:
    """Require a plan from before its event candle and evidence available now."""
    if int(row.get("ts", candle["t"])) != candle["t"]:
        return "retrospective_event"
    event_ts = int(row.get("event_ts", row.get("ts", candle["t"])))
    plan_ts = row.get("plan_ts")
    if plan_ts is None:
        return "missing_plan_ts"
    if int(plan_ts) >= event_ts:
        return "late_plan"
    observed_at = int(row.get("observed_at", candle["t"] + MINUTE_MS - 1))
    if observed_at > candle["t"] + MINUTE_MS - 1:
        return "future_observation"
    return "valid"


def _evaluate_one(row: dict, candles: list[dict], index: dict[int, int],
                  horizon: int, cost_pct: float) -> dict:
    """Evaluate after confirmation; never enter at the confirming close."""
    confirmed_ts = int(row["ts"])
    i = index.get(confirmed_ts)
    if i is None:
        return {"status": "missing_confirmation_candle"}
    entry_i = i + 1
    if entry_i >= len(candles):
        return {"status": "censored_end"}
    if candles[entry_i]["t"] != confirmed_ts + MINUTE_MS:
        return {"status": "censored_gap"}
    direction = row.get("direction")
    if direction not in ("LONG", "SHORT"):
        return {"status": "invalid_direction"}
    sign = 1 if direction == "LONG" else -1
    entry = candles[entry_i]["o"]
    try:
        stop = float(row["invalidation"])
    except (KeyError, TypeError, ValueError):
        return {"status": "missing_stop"}
    if not math.isfinite(stop) or stop <= 0:
        return {"status": "invalid_stop"}
    if (sign == 1 and entry <= stop) or (sign == -1 and entry >= stop):
        return {"status": "invalidated_before_entry"}

    for age in range(1, horizon + 1):
        j = entry_i + age - 1
        if j >= len(candles):
            return {"status": "censored_end"}
        if j > entry_i and candles[j]["t"] != candles[j - 1]["t"] + MINUTE_MS:
            return {"status": "censored_gap"}
        bar = candles[j]
        hit = bar["l"] <= stop if sign == 1 else bar["h"] >= stop
        if hit:
            # A gap through the stop fills at the adverse open, not the stop.
            exit_price = min(bar["o"], stop) if sign == 1 else max(bar["o"], stop)
            exit_reason = "stop"
            held = age
            break
    else:
        exit_price = candles[entry_i + horizon - 1]["c"]
        exit_reason = "horizon"
        held = horizon
    gross = sign * (exit_price - entry) / entry * 100
    return {
        "status": "evaluated", "entry_ts": candles[entry_i]["t"],
        "entry": entry, "exit": exit_price, "exit_reason": exit_reason,
        "held_candles": held, "gross_pct": gross, "net_pct_assumed": gross - cost_pct,
    }


def _summarize_outcomes(rows: list[dict], candles_by_symbol: dict[str, list[dict]],
                        cost_pct: float) -> dict:
    result = {}
    indexes = {s: {c["t"]: i for i, c in enumerate(cs)}
               for s, cs in candles_by_symbol.items()}
    for horizon in HORIZONS:
        evaluations = []
        for row in rows:
            symbol = row["symbol"]
            evaluations.append(_evaluate_one(row, candles_by_symbol[symbol],
                                             indexes[symbol], horizon, cost_pct))
        valid = [x for x in evaluations if x["status"] == "evaluated"]
        net = [x["net_pct_assumed"] for x in valid]
        gross = [x["gross_pct"] for x in valid]
        result[str(horizon)] = {
            "confirmed_events": len(rows), "evaluated": len(valid),
            "excluded_or_censored": dict(Counter(x["status"] for x in evaluations
                                                 if x["status"] != "evaluated")),
            "stopped": sum(x["exit_reason"] == "stop" for x in valid),
            "gross_mean_pct": sum(gross) / len(gross) if gross else None,
            "net_mean_pct_assumed": sum(net) / len(net) if net else None,
            "net_positive_fraction_assumed": (sum(x > 0 for x in net) / len(net)
                                              if net else None),
        }
    return result


def _unconditional_move(candles_by_symbol: dict[str, list[dict]]) -> dict:
    result = {}
    for horizon in HORIZONS:
        values = []
        for candles in candles_by_symbol.values():
            for i in range(len(candles) - horizon):
                if candles[i + 1]["t"] != candles[i]["t"] + MINUTE_MS:
                    continue
                window = candles[i + 1:i + horizon + 1]
                if any(window[j]["t"] != window[j - 1]["t"] + MINUTE_MS
                       for j in range(1, len(window))):
                    continue
                entry, exit_price = window[0]["o"], window[-1]["c"]
                values.append(abs(exit_price - entry) / entry * 100)
        result[str(horizon)] = {
            "windows": len(values),
            "mean_abs_move_pct": sum(values) / len(values) if values else None,
        }
    return result


def replay_data(cache: dict, cost_pct: float = 0.2,
                engine_factories: dict[str, Callable] | None = None) -> dict:
    if not math.isfinite(cost_pct) or cost_pct < 0:
        raise ValueError("cost_pct must be a finite non-negative percentage-point value")
    factories = engine_factories if engine_factories is not None else _default_factories()
    candles_by_symbol = {}
    skipped = Counter()
    for raw_symbol, by_tf in cache.items():
        candles, bad = _closed_minutes(by_tf.get("1m", []))
        skipped.update(bad)
        if candles:
            symbol = _symbol(raw_symbol)
            if symbol in candles_by_symbol:
                raise ValueError(f"duplicate normalized symbol {symbol}")
            candles_by_symbol[symbol] = candles

    all_ts = [c["t"] for candles in candles_by_symbol.values() for c in candles]
    engines = {}
    for name, factory in factories.items():
        counts = Counter()
        prospective = Counter()
        confirmations = []
        duplicate_confirmations = set()
        by_symbol = {}
        no_trade_reasons = Counter()
        for symbol, candles in candles_by_symbol.items():
            engine = factory()
            symbol_counts = Counter()
            symbol_confirmations = []
            for candle in candles:
                returned = engine.on_kline(symbol, candle)
                for row in _drain_events(engine, returned):
                    kind = str(row.get("kind", "unknown"))
                    counts[kind] += 1
                    symbol_counts[kind] += 1
                    if kind == "no_trade":
                        no_trade_reasons[str(row.get("reason", "unspecified"))] += 1
                    if kind in ("candidate", "watch", "confirmed"):
                        status = _event_status(row, candle)
                        prospective[status] += 1
                    if kind != "confirmed":
                        continue
                    if status != "valid":
                        continue
                    normalized = dict(row)
                    normalized.setdefault("symbol", symbol)
                    key = (symbol, normalized.get("ts"), normalized.get("scenario"),
                           normalized.get("direction"))
                    if key in duplicate_confirmations:
                        counts["duplicate_confirmed"] += 1
                        continue
                    duplicate_confirmations.add(key)
                    confirmations.append(normalized)
                    symbol_confirmations.append(normalized)
            by_symbol[symbol] = {
                "event_counts": {key: symbol_counts.get(key, 0) for key in COUNTED_KINDS},
                "horizons": _summarize_outcomes(
                    symbol_confirmations, {symbol: candles}, cost_pct),
            }
        engines[name] = {
            "event_counts": {key: counts.get(key, 0) for key in COUNTED_KINDS},
            "other_event_counts": {key: value for key, value in counts.items()
                                   if key not in COUNTED_KINDS},
            "prospective_checks": dict(prospective),
            "no_trade_reasons": dict(no_trade_reasons),
            "horizons": _summarize_outcomes(confirmations, candles_by_symbol, cost_pct),
            "by_symbol": by_symbol,
        }

    return {
        "symbols": len(candles_by_symbol),
        "closed_1m_candles": sum(map(len, candles_by_symbol.values())),
        "skipped_input_rows": dict(skipped),
        "period_utc": ([datetime.fromtimestamp(min(all_ts) / 1000, timezone.utc).isoformat(),
                        datetime.fromtimestamp(max(all_ts) / 1000, timezone.utc).isoformat()]
                       if all_ts else []),
        "cost_pct_round_trip_assumed": cost_pct,
        "execution_model": "next 1m open after closed confirmation; structural stop using OHLC, adverse open on a gap; 5/15 bars including entry bar",
        "limitations": "OHLCV-only four-coin cache; no historical derivatives, taker flow, depth, spread, funding, actual fills, portfolio sizing, or out-of-sample evidence",
        "no_signal_baseline_net_pct": 0.0,
        "unconditional_absolute_move": _unconditional_move(candles_by_symbol),
        "engines": engines,
    }


def replay(cache_path: Path, cost_pct: float = 0.2) -> dict:
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    result = replay_data(cache, cost_pct)
    result["cache"] = str(cache_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("data/ohlcv_cache.json"))
    parser.add_argument("--cost-pct", type=float, default=0.2,
                        help="Assumed total round-trip costs in percentage points")
    args = parser.parse_args()
    print(json.dumps(replay(args.cache, args.cost_pct), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
