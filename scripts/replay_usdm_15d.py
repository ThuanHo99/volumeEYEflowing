"""Walk-forward 1m TP/SL replay for a frozen all-symbol Binance USD-M cache.

Run data collection first with ``python -m scripts.usdm_15d_data``. This
script uses the first ten scored days to choose one global, predeclared
stop/target/expiry policy per engine, writes that selection, and only then
scores the final five days. Stops and targets use information known before
the next-candle-open entry. In a 1m candle touching both, the stop wins.
This is a conservative OHLC research approximation, not actual fills.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import statistics
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from VolumeSniper.chart_reasoning_v3 import ChartReasoningEngine
from VolumeSniper.volume_context import SeasonalVolumeBaseline, VolumeContextEngine
from scripts.replay_chart_v3 import _event_status
from scripts.usdm_15d_data import (DAY_MS, MINUTE_MS, _archive_path, _rest_path,
                                   archive_days)

TRAIN_DAYS = 10
COST_PCT = 0.2
STOP_MULTIPLIERS = (1.0, 1.25, 1.5)
TARGET_R = (1.0, 1.5, 2.0)
HOLD_MINUTES = (15, 30, 60)
POLICIES = tuple((stop, target, hold)
                 for stop in STOP_MULTIPLIERS
                 for target in TARGET_R for hold in HOLD_MINUTES)
REFERENCE = (1.0, 1.5, 30)


def _policy_key(policy: tuple[float, float, int]) -> str:
    stop, target, hold = policy
    return f"stop_x{stop:g}_tp_{target:g}R_max_{hold}m"


def _parse_row(raw: list, start_ms: int, end_ms: int) -> dict | None:
    try:
        ts = int(raw[0])
        if ts < start_ms or ts >= end_ms or ts % MINUTE_MS:
            return None
        o, h, l, c, v, q = (float(raw[i]) for i in (1, 2, 3, 4, 5, 7))
        close_ts = int(raw[6])
        buy = float(raw[9])
    except (IndexError, TypeError, ValueError, OverflowError):
        return None
    if (close_ts != ts + MINUTE_MS - 1 or
            not all(math.isfinite(x) for x in (o, h, l, c, v, q, buy)) or
            l <= 0 or v < 0 or q < 0 or l > min(o, c) or h < max(o, c)):
        return None
    bar = {"t": ts, "o": o, "h": h, "l": l, "c": c, "v": v,
           "q": q, "x": True}
    if 0 <= buy <= v:
        bar["V"] = buy
    return bar


def read_symbol(run_dir: Path, manifest: dict, meta: dict) -> tuple[list[dict], dict]:
    symbol = meta["symbol"]
    lower = manifest.get("warmup_start_ms", manifest["start_ms"])
    upper = manifest["end_ms_exclusive"]
    days = archive_days(lower, upper) + [
        datetime.fromtimestamp(upper / 1000, timezone.utc).date().isoformat()]
    by_ts: dict[int, dict] = {}
    missing_sources = []
    source_days = Counter()
    malformed = 0
    conflicts = 0
    for day in days:
        archive = _archive_path(run_dir, symbol, day)
        rest = _rest_path(run_dir, symbol, day)
        if archive.exists():
            source_days["archive"] += 1
            try:
                with zipfile.ZipFile(archive) as package:
                    with package.open(package.namelist()[0]) as handle:
                        reader = csv.reader(io.TextIOWrapper(handle))
                        for raw in reader:
                            bar = _parse_row(raw, lower, upper)
                            if bar is None:
                                # Header and out-of-window rows are expected.
                                continue
                            old = by_ts.get(bar["t"])
                            if old and old != bar:
                                conflicts += 1
                            by_ts[bar["t"]] = bar
            except (OSError, zipfile.BadZipFile, IndexError):
                malformed += 1
                missing_sources.append(day)
        elif rest.exists():
            source_days["rest"] += 1
            try:
                with gzip.open(rest, "rt", encoding="utf-8") as handle:
                    rows = json.load(handle)
                for raw in rows:
                    bar = _parse_row(raw, lower, upper)
                    if bar is None:
                        continue
                    old = by_ts.get(bar["t"])
                    if old and old != bar:
                        conflicts += 1
                    by_ts[bar["t"]] = bar
            except (OSError, ValueError):
                malformed += 1
                missing_sources.append(day)
        else:
            day_ms = int(datetime.fromisoformat(day)
                         .replace(tzinfo=timezone.utc).timestamp() * 1000)
            delivery = int(meta.get("delivery_ms") or 0)
            if (day_ms + DAY_MS > int(meta.get("onboard_ms") or 0) and
                    (not delivery or day_ms < delivery)):
                missing_sources.append(day)
    bars = [by_ts[ts] for ts in sorted(by_ts)]
    gaps = sum(b["t"] != a["t"] + MINUTE_MS for a, b in zip(bars, bars[1:]))
    scored = [bar for bar in bars if bar["t"] >= manifest["start_ms"]]
    coverage = {
        "bars_total_with_warmup": len(bars), "bars_scored": len(scored),
        "first_bar_ts": bars[0]["t"] if bars else None,
        "last_bar_ts": bars[-1]["t"] if bars else None,
        "gap_count": gaps, "missing_source_days": missing_sources,
        "source_days": dict(source_days), "malformed_packages": malformed,
        "conflicting_duplicate_bars": conflicts,
        "median_quote_volume_per_scored_minute":
            statistics.median(bar["q"] for bar in scored) if scored else None,
    }
    return bars, coverage


def _drain(engine: object, returned: object) -> list[dict]:
    records = getattr(engine, "records", None)
    if records is not None:
        events = [row for row in records if isinstance(row, dict)]
        records.clear()
        if events:
            return events
    return [returned] if isinstance(returned, dict) else []


def _planned_target(engine: str, row: dict) -> float | None:
    if engine == "v3":
        value = row.get("feasibility", {}).get("target_near_edge")
    elif row.get("scenario") == "FAILURE":
        value = row.get("plan_upper") if row.get("direction") == "LONG" else row.get("plan_lower")
    else:
        value = None
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def _compact_signal(engine: str, row: dict) -> dict:
    return {
        "engine": engine, "confirmed_ts": int(row["ts"]),
        "observed_at": int(row.get("observed_at", row["ts"] + MINUTE_MS - 1)),
        "plan_ts": int(row["plan_ts"]), "event_ts": int(row.get("event_ts", row["ts"])),
        "direction": row["direction"], "scenario": row.get("scenario"),
        "confirmation_close": float(row["price"]),
        "invalidation": float(row["invalidation"]),
        "planned_target": _planned_target(engine, row),
    }


def _round_down(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    # Binance prices are decimal ticks. Float multiplication can otherwise
    # place a rounded stop just beyond an exact-tick 1m high or low.
    amount = Decimal(str(round(value, 12)))
    quantum = Decimal(str(tick))
    return float((amount / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum)


def _round_up(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    amount = Decimal(str(round(value, 12)))
    quantum = Decimal(str(tick))
    return float((amount / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum)


def simulate(bars: list[dict], index: dict[int, int], signal: dict,
             policy: tuple[float, float, int], tick: float,
             *, period_end_ms: int) -> dict:
    stop_mult, target_r, max_hold = policy
    i = index.get(signal["confirmed_ts"])
    if i is None or i + 1 >= len(bars):
        return {"status": "censored_end"}
    entry_bar = bars[i + 1]
    if entry_bar["t"] != signal["confirmed_ts"] + MINUTE_MS:
        return {"status": "censored_gap"}
    if entry_bar["t"] >= period_end_ms:
        return {"status": "censored_boundary"}
    if entry_bar["v"] <= 0:
        return {"status": "no_trade_zero_volume_entry"}
    side = 1 if signal["direction"] == "LONG" else -1
    entry = entry_bar["o"]
    raw_stop = signal["invalidation"]
    base_risk = side * (entry - raw_stop)
    if not math.isfinite(base_risk) or base_risk <= 0:
        return {"status": "invalidated_before_entry"}
    stop = entry - side * stop_mult * base_risk
    stop = _round_down(stop, tick) if side == 1 else _round_up(stop, tick)
    risk = side * (entry - stop)
    if risk <= 0 or stop <= 0 or (tick > 0 and risk < 2 * tick):
        return {"status": "invalid_stop"}
    target = entry + side * target_r * risk
    planned = signal.get("planned_target")
    if planned is not None:
        target = min(target, planned) if side == 1 else max(target, planned)
    target = _round_down(target, tick) if side == 1 else _round_up(target, tick)
    if side * (target - entry) <= 0:
        return {"status": "target_passed_before_entry"}

    for age in range(1, max_hold + 1):
        j = i + age
        if j >= len(bars):
            return {"status": "censored_end"}
        bar = bars[j]
        if bar["t"] >= period_end_ms:
            return {"status": "censored_boundary"}
        if age > 1 and bar["t"] != bars[j - 1]["t"] + MINUTE_MS:
            return {"status": "censored_gap"}
        stop_hit = bar["l"] <= stop if side == 1 else bar["h"] >= stop
        target_hit = bar["h"] >= target if side == 1 else bar["l"] <= target
        if bar["v"] <= 0 and (stop_hit or target_hit or age == max_hold):
            return {"status": "censored_no_liquidity_exit"}
        if stop_hit:
            exit_price = min(bar["o"], stop) if side == 1 else max(bar["o"], stop)
            reason = "stop"
            ambiguous = target_hit
            break
        if target_hit:
            exit_price = target  # never credit an unverified favorable gap improvement
            reason = "target"
            ambiguous = False
            break
    else:
        exit_price = bars[i + max_hold]["c"]
        reason = "timeout"
        ambiguous = False
        age = max_hold
    gross_pct = side * (exit_price - entry) / entry * 100
    return {
        "status": "evaluated", "entry_ts": entry_bar["t"],
        "exit_ts": bars[i + age]["t"], "entry": entry,
        "stop": stop, "target": target, "exit": exit_price,
        "exit_reason": reason, "ambiguous_same_minute": ambiguous,
        "held_minutes": age, "gross_pct": gross_pct,
        "net_pct_assumed": gross_pct - COST_PCT,
    }


def _add_result(summary: dict, outcome: dict) -> None:
    status = outcome["status"]
    summary["statuses"][status] += 1
    if status != "evaluated":
        return
    summary["gross_sum"] += outcome["gross_pct"]
    summary["net_sum"] += outcome["net_pct_assumed"]
    summary["net_positive"] += outcome["net_pct_assumed"] > 0
    summary["exit_reasons"][outcome["exit_reason"]] += 1
    summary["ambiguous_same_minute"] += outcome["ambiguous_same_minute"]


def _empty_summary() -> dict:
    return {"statuses": Counter(), "gross_sum": 0.0, "net_sum": 0.0,
            "net_positive": 0, "exit_reasons": Counter(),
            "ambiguous_same_minute": 0}


def _phase1_symbol(run_dir_str: str, manifest: dict, meta: dict,
                   reuse_signals: bool = False) -> dict:
    run_dir = Path(run_dir_str)
    symbol = meta["symbol"]
    bars, coverage = read_symbol(run_dir, manifest, meta)
    path = run_dir / "signals" / f"{symbol}.json.gz"
    cached = reuse_signals and path.exists()
    event_counts = {name: Counter() for name in ("v2", "v3")}
    prospective = {name: Counter() for name in ("v2", "v3")}
    signals: list[dict] = []
    if cached:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                signals = json.load(handle)
            if not isinstance(signals, list) or any(
                    row.get("engine") not in event_counts or
                    row.get("confirmed_ts", -1) < manifest["start_ms"] or
                    row.get("confirmed_ts", manifest["end_ms_exclusive"]) >=
                    manifest["end_ms_exclusive"] or
                    row.get("plan_ts", 0) >= row.get("event_ts", 0) or
                    row.get("observed_at", 0) >
                    row.get("confirmed_ts", 0) + MINUTE_MS - 1
                    for row in signals):
                raise ValueError("invalid cached signal timing or engine")
            for row in signals:
                name = row["engine"]
                event_counts[name]["confirmed"] += 1
                prospective[name]["valid"] += 1
        except (OSError, EOFError, ValueError, TypeError, KeyError):
            cached = False
            signals = []
    if not cached:
        engines = {
            "v2": VolumeContextEngine(SeasonalVolumeBaseline(Path(os.devnull))),
            "v3": ChartReasoningEngine(journal_path=Path(os.devnull)),
        }
        for bar in bars:
            for name, engine in engines.items():
                returned = engine.on_kline(symbol, bar)
                for row in _drain(engine, returned):
                    if bar["t"] < manifest["start_ms"]:
                        continue
                    kind = row.get("kind", "unknown")
                    event_counts[name][kind] += 1
                    if kind in ("watch", "candidate", "confirmed"):
                        validity = _event_status(row, bar)
                        prospective[name][validity] += 1
                        if validity != "valid":
                            continue
                    if kind == "confirmed":
                        signals.append(_compact_signal(name, row))
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(signals, handle, separators=(",", ":"))

    index = {bar["t"]: i for i, bar in enumerate(bars)}
    tick = float(meta.get("tick_size") or 0)
    split_ms = manifest["start_ms"] + TRAIN_DAYS * DAY_MS
    train = {name: {_policy_key(policy): _empty_summary() for policy in POLICIES}
             for name in ("v2", "v3")}
    for signal in signals:
        if signal["confirmed_ts"] >= split_ms:
            continue
        for policy in POLICIES:
            result = simulate(bars, index, signal, policy, tick,
                              period_end_ms=split_ms)
            _add_result(train[signal["engine"]][_policy_key(policy)], result)
    return {
        "symbol": symbol, "meta": meta, "coverage": coverage,
        "events": {name: dict(counts) for name, counts in event_counts.items()},
        "prospective": {name: dict(counts) for name, counts in prospective.items()},
        "signals": dict(Counter(signal["engine"] for signal in signals)),
        "signal_cache_reused": cached,
        "train": train,
    }


def _merge_summary(target: dict, source: dict) -> None:
    target["statuses"].update(source["statuses"])
    target["gross_sum"] += source["gross_sum"]
    target["net_sum"] += source["net_sum"]
    target["net_positive"] += source["net_positive"]
    target["exit_reasons"].update(source["exit_reasons"])
    target["ambiguous_same_minute"] += source["ambiguous_same_minute"]


def _public_summary(summary: dict) -> dict:
    n = summary["statuses"].get("evaluated", 0)
    return {
        "statuses": dict(summary["statuses"]), "evaluated": n,
        "gross_mean_pct": summary["gross_sum"] / n if n else None,
        "net_mean_pct_assumed": summary["net_sum"] / n if n else None,
        "net_positive_fraction_assumed": summary["net_positive"] / n if n else None,
        "exit_reasons": dict(summary["exit_reasons"]),
        "ambiguous_same_minute_stop_first": summary["ambiguous_same_minute"],
    }


def _select_policy(train: dict[str, dict]) -> tuple[str, dict]:
    ranked = []
    for key, summary in train.items():
        n = summary["statuses"].get("evaluated", 0)
        mean = summary["net_sum"] / n if n else -math.inf
        ranked.append((mean, n, key))
    if not any(n for _, n, _ in ranked):
        return _policy_key(REFERENCE), {
            "train_evaluated": 0, "train_net_mean_pct_assumed": None,
            "selection_reason": "no_train_trades; fixed_reference_fallback",
        }
    mean, n, key = max(ranked, key=lambda row: (row[0], row[1], row[2]))
    return key, {"train_evaluated": n, "train_net_mean_pct_assumed": mean}


def _phase2_symbol(run_dir_str: str, manifest: dict, meta: dict,
                   selected: dict[str, str]) -> dict:
    run_dir = Path(run_dir_str)
    symbol = meta["symbol"]
    bars, coverage = read_symbol(run_dir, manifest, meta)
    index = {bar["t"]: i for i, bar in enumerate(bars)}
    tick = float(meta.get("tick_size") or 0)
    with gzip.open(run_dir / "signals" / f"{symbol}.json.gz", "rt", encoding="utf-8") as handle:
        signals = json.load(handle)
    split_ms = manifest["start_ms"] + TRAIN_DAYS * DAY_MS
    lookup = {_policy_key(p): p for p in POLICIES}
    policies = {name: {"selected": lookup[selected[name]], "reference": REFERENCE}
                for name in selected}
    result = {name: {label: _empty_summary() for label in modes}
              for name, modes in policies.items()}
    for signal in signals:
        if signal["confirmed_ts"] < split_ms:
            continue
        name = signal["engine"]
        for label, policy in policies[name].items():
            outcome = simulate(bars, index, signal, policy, tick,
                               period_end_ms=manifest["end_ms_exclusive"])
            _add_result(result[name][label], outcome)
    return {"symbol": symbol, "coverage": coverage, "holdout": result}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               default=lambda obj: dict(obj) if isinstance(obj, Counter) else obj),
                    encoding="utf-8")
    os.replace(temp, path)


def run_replay(run_dir: Path, workers: int = 8,
               *, resume_signals: bool = False,
               recompute_outcomes: bool = False) -> dict:
    manifest = json.loads((run_dir / "universe.json").read_text(encoding="utf-8"))
    metas = manifest["symbols"]
    checkpoint = run_dir / "phase1_results.partial.json"
    phase1: list[dict] = []
    if recompute_outcomes:
        _write_json(checkpoint, [])
    elif resume_signals and checkpoint.exists():
        phase1 = [row for row in json.loads(checkpoint.read_text(encoding="utf-8"))
                  if "error" not in row]
    completed = {row["symbol"] for row in phase1}
    print(f"phase 1: {len(metas)} symbols, {workers} workers; "
          f"{len(completed)} checkpointed", flush=True)
    pending_metas = [meta for meta in metas if meta["symbol"] not in completed]
    if resume_signals:
        pending_metas.sort(key=lambda meta: (
            (run_dir / "signals" / f"{meta['symbol']}.json.gz").exists(),
            meta["symbol"]))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_phase1_symbol, str(run_dir), manifest, meta,
                            resume_signals): meta["symbol"]
                for meta in pending_metas}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                phase1.append(future.result())
            except Exception as exc:
                phase1.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            if len(phase1) % 25 == 0:
                print(f"phase 1 {len(phase1)}/{len(metas)}", flush=True)
                _write_json(checkpoint, phase1)
    phase1.sort(key=lambda row: row["symbol"])
    _write_json(run_dir / "phase1_results.json", phase1)
    phase1_errors = [row for row in phase1 if "error" in row]
    if phase1_errors:
        raise RuntimeError(
            f"phase 1 failed for {len(phase1_errors)} symbols; "
            f"first failures: {phase1_errors[:3]}")
    usable = [row for row in phase1 if "error" not in row and
              not row["coverage"]["missing_source_days"] and
              not row["coverage"]["conflicting_duplicate_bars"]]
    eligible_symbols = {row["symbol"] for row in usable}
    train = {name: {_policy_key(policy): _empty_summary() for policy in POLICIES}
             for name in ("v2", "v3")}
    for row in usable:
        for name in train:
            for key, summary in row["train"][name].items():
                _merge_summary(train[name][key], summary)
    selected = {}
    selection_details = {}
    for name in train:
        selected[name], selection_details[name] = _select_policy(train[name])
    selection = {
        "train_start_ms": manifest["start_ms"],
        "train_end_ms_exclusive": manifest["start_ms"] + TRAIN_DAYS * DAY_MS,
        "test_end_ms_exclusive": manifest["end_ms_exclusive"],
        "cost_pct_assumed": COST_PCT,
        "selection_rule": "maximum pooled mean net percent on first 10 days only",
        "eligible_symbols": len(usable), "selected": selected,
        "training_details": selection_details,
        "policy_grid": {name: {key: _public_summary(value)
                               for key, value in policies.items()}
                        for name, policies in train.items()},
    }
    _write_json(run_dir / "policy_selection.json", selection)
    print("selected from training only", selected, flush=True)

    print(f"phase 2: held-out five days, {len(usable)} eligible symbols", flush=True)
    phase2: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_phase2_symbol, str(run_dir), manifest, meta, selected): meta["symbol"]
                for meta in metas if meta["symbol"] in eligible_symbols}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                phase2.append(future.result())
            except Exception as exc:
                phase2.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            if len(phase2) % 25 == 0:
                print(f"phase 2 {len(phase2)}/{len(usable)}", flush=True)
    phase2.sort(key=lambda row: row["symbol"])
    _write_json(run_dir / "phase2_results.json", phase2)
    phase2_errors = [row for row in phase2 if "error" in row]
    if phase2_errors:
        raise RuntimeError(
            f"phase 2 failed for {len(phase2_errors)} symbols; "
            f"first failures: {phase2_errors[:3]}")
    holdout = {name: {label: _empty_summary() for label in ("selected", "reference")}
               for name in ("v2", "v3")}
    for row in phase2:
        if "error" in row:
            continue
        for name in holdout:
            for label in holdout[name]:
                _merge_summary(holdout[name][label], row["holdout"][name][label])
    confirmed_counts = {name: sum(row["signals"].get(name, 0) for row in usable)
                        for name in ("v2", "v3")}
    metadata = {row["symbol"]: row for row in metas}
    by_segment: dict[str, dict] = {}
    for row in phase2:
        if "error" in row:
            continue
        meta = metadata[row["symbol"]]
        segment = f"{meta.get('contract_type')}|{meta.get('quote_asset')}"
        group = by_segment.setdefault(segment, {
            "symbols": 0,
            "v2": _empty_summary(), "v3": _empty_summary(),
        })
        group["symbols"] += 1
        for name in ("v2", "v3"):
            _merge_summary(group[name], row["holdout"][name]["selected"])
    report = {
        "universe_symbols": len(metas),
        "eligible_symbols": len(usable),
        "excluded_symbols": [{"symbol": row["symbol"],
                              "reason": row.get("error") or row["coverage"]["missing_source_days"]
                              or "conflicting_duplicate_bars"}
                             for row in phase1 if row["symbol"] not in eligible_symbols],
        "start_ms": manifest["start_ms"],
        "split_ms": manifest["start_ms"] + TRAIN_DAYS * DAY_MS,
        "end_ms_exclusive": manifest["end_ms_exclusive"],
        "training_selection": {name: {"policy": selected[name],
                                      **selection_details[name]}
                               for name in selected},
        "holdout": {name: {label: _public_summary(summary)
                           for label, summary in modes.items()}
                    for name, modes in holdout.items()},
        "holdout_by_contract_and_quote": {
            key: {"symbols": value["symbols"],
                  "v2": _public_summary(value["v2"]),
                  "v3": _public_summary(value["v3"])}
            for key, value in sorted(by_segment.items())
        },
        "confirmed_signal_counts": confirmed_counts,
        "signal_caches_reused": sum(bool(row.get("signal_cache_reused")) for row in usable),
        "execution_rule": "next 1m open; structural stop widened by trained multiplier; target limited by trained R and prior opposing zone; tick-rounded; same-minute TP+SL assumes SL; adverse gap open; max hold 15/30/60m; 0.2pp cost",
        "limitations": "Current trading contracts plus those entering SETTLING during the frozen window; contracts delisted before collection may be absent. OHLC has no intraminute path or real bid/ask/fills; no historical derivatives/depth/funding; independent signals, not a portfolio.",
    }
    _write_json(run_dir / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/usdm_15d_latest"))
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--resume-signals", action="store_true",
                        help="Reuse valid signal files and phase-1 checkpoints from this frozen run")
    parser.add_argument("--recompute-outcomes", action="store_true",
                        help="Reuse signals but discard phase-1 outcome checkpoints")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    report = run_replay(args.run_dir, args.workers,
                        resume_signals=args.resume_signals,
                        recompute_outcomes=args.recompute_outcomes)
    print(json.dumps({"universe": report["universe_symbols"],
                      "eligible": report["eligible_symbols"],
                      "selected": report["training_selection"],
                      "holdout": report["holdout"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
