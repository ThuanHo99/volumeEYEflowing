"""Offline, chronological smoke replay of context v2 on the core 1m cache.

Example: python -m scripts.replay_context --cost-pct 0.2
This cache lacks taker-buy volume and is not the scanner's top-500 universe.
The output is descriptive, not a strategy backtest or realized P&L.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from VolumeSniper.volume_context import SeasonalVolumeBaseline, VolumeContextEngine


def replay(cache_path: Path, cost_pct: float) -> dict:
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    baseline = SeasonalVolumeBaseline(Path(os.devnull))
    engine = VolumeContextEngine(baseline)
    rows: list[dict] = []
    seen_times: list[int] = []
    unconditional_abs: dict[int, list[float]] = {5: [], 15: []}
    for symbol, by_tf in cache.items():
        klines = sorted(by_tf.get("1m", []), key=lambda x: x["start_ts"])
        if not klines:
            continue
        seen_times.extend([klines[0]["start_ts"], klines[-1]["start_ts"]])
        for horizon in (5, 15):
            for i in range(len(klines) - horizon):
                start_price = klines[i]["close"]
                if start_price > 0:
                    unconditional_abs[horizon].append(
                        abs(klines[i + horizon]["close"] - start_price) / start_price * 100
                    )
        for c in klines:
            k = {
                "t": c["start_ts"], "o": c["open"], "h": c["high"],
                "l": c["low"], "c": c["close"], "v": c["volume"],
                "q": c["volume"] * c["close"], "x": c["is_closed"],
            }
            engine.on_kline(symbol.replace("/", "").replace(":USDT", ""), k)

        # The live engine bounds its in-memory journal. Drain after each symbol
        # so a longer replay cannot silently drop early candidate/watch rows.
        rows.extend(engine.records)
        engine.records.clear()

    confirmed_by_key = {(r["symbol"], r["event_ts"]): r["scenario"]
                        for r in rows if r["kind"] == "confirmed"}
    result = {
        "cache": str(cache_path),
        "symbols": len(cache),
        "period_utc": [
            datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()
            for t in (min(seen_times), max(seen_times))
        ] if seen_times else [],
        "candidates": sum(r["kind"] == "candidate" for r in rows),
        "non_directional_watches": sum(r["kind"] == "watch" for r in rows),
        "confirmed": sum(r["kind"] == "confirmed" for r in rows),
        "expired": sum(r["kind"] == "expired" for r in rows),
        "events_without_prior_plan": sum(
            r["kind"] in ("candidate", "watch") and r.get("plan_ts", r["ts"]) >= r["ts"]
            for r in rows
        ),
        "limitations": "1m core cache only; no taker delta, seasonal history, intrabar fills, or execution costs beyond assumed round trip",
        "cost_pct_round_trip_assumed": cost_pct,
        "horizons": {},
    }
    for horizon in (5, 15):
        gross = [r["return_pct_gross"] for r in rows
                 if r["kind"] == "outcome" and r["horizon_candles"] == horizon]
        result["horizons"][str(horizon)] = {
            "observations": len(gross),
            "gross_mean_pct": sum(gross) / len(gross) if gross else None,
            "net_mean_pct_assumed": sum(x - cost_pct for x in gross) / len(gross) if gross else None,
            "net_positive_fraction_assumed": sum(x > cost_pct for x in gross) / len(gross) if gross else None,
            "mean_abs_move_pct": sum(abs(x) for x in gross) / len(gross) if gross else None,
            "unconditional_mean_abs_move_pct": (
                sum(unconditional_abs[horizon]) / len(unconditional_abs[horizon])
                if unconditional_abs[horizon] else None
            ),
        }
        for scenario in ("BREAKOUT", "FAILURE"):
            values = [r["return_pct_gross"] for r in rows
                      if r["kind"] == "outcome" and r["horizon_candles"] == horizon
                      and confirmed_by_key.get((r["symbol"], r["event_ts"])) == scenario]
            result["horizons"][str(horizon)][scenario.lower()] = {
                "observations": len(values),
                "gross_mean_pct": sum(values) / len(values) if values else None,
                "net_positive_fraction_assumed": sum(x > cost_pct for x in values) / len(values) if values else None,
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("data/ohlcv_cache.json"))
    parser.add_argument("--cost-pct", type=float, default=0.2,
                        help="Assumed round-trip fee + slippage in percentage points")
    args = parser.parse_args()
    print(json.dumps(replay(args.cache, args.cost_pct), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
