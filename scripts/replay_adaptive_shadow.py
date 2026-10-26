"""Forward paper replay of generated scenarios on one frozen USD-M symbol.

This is an exploratory harness. It processes every closed 1m candle in order,
calls a local Ollama model at most once per UTC day if requested, then records
only future paper decisions and matured 1m TP/SL outcomes. It never sends
orders and cannot promote a policy.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter
from pathlib import Path

from VolumeSniper.adaptive_scenarios import (Condition, ScenarioLedger,
                                             ScenarioProposal)
from VolumeSniper.adaptive_shadow import AdaptiveShadowLoop
from VolumeSniper.chart_reasoning_v3 import ChartReasoningEngine
from VolumeSniper.local_scenario_proposer import LocalScenarioProposer
from scripts.replay_usdm_15d import read_symbol


def _load_seed(path: Path, start_ms: int) -> ScenarioProposal:
    """Load an explicit, time-zero hypothesis for testing the paper plumbing."""
    row = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(row, dict) or set(row) != {
            "scenario_id", "conditions", "action", "stop_mult", "target_r",
            "max_hold_minutes"}:
        raise ValueError("seed proposal must contain the six typed fields")
    return ScenarioProposal(
        row["scenario_id"], 1,
        tuple(Condition(**condition) for condition in row["conditions"]),
        row["action"], row["stop_mult"], row["target_r"],
        row["max_hold_minutes"], start_ms,
    )


def run(run_dir: Path, symbol: str, output_dir: Path, *,
        use_ollama: bool = False, model: str | None = None,
        max_model_calls: int = 15,
        seed_proposal: Path | None = None) -> dict:
    universe = json.loads((run_dir / "universe.json").read_text(encoding="utf-8"))
    meta = next((row for row in universe["symbols"] if row["symbol"] == symbol), None)
    if meta is None:
        raise ValueError(f"symbol not in frozen universe: {symbol}")
    tick_size = float(meta["tick_size"])
    if tick_size <= 0:
        raise ValueError("missing exchange tick size")
    bars, coverage = read_symbol(run_dir, universe, meta)
    if not bars:
        raise ValueError("no frozen 1m bars available")
    if output_dir.exists():
        raise FileExistsError(f"choose a fresh output directory: {output_dir}")
    output_dir.mkdir(parents=True)
    start_ms = universe["start_ms"]
    if seed_proposal is not None:
        seed = _load_seed(seed_proposal, start_ms)
    else:
        seed = None
    proposer = (LocalScenarioProposer(model=model) if model else
                LocalScenarioProposer() if use_ollama else None)
    ledger_path = output_dir / "ledger.jsonl"
    if seed:
        ScenarioLedger(ledger_path).append_proposal(seed)
    loop = AdaptiveShadowLoop(ledger_path, asof_ms=0, proposer=proposer)
    engine = ChartReasoningEngine(journal_path=Path(os.devnull))
    attempts = []
    attempted_days: set[int] = set()
    scored_bars = 0
    for bar in bars:
        engine.on_kline(symbol, bar)
        engine.records.clear()
        plan = engine.plans.get(symbol)
        if plan is None:
            continue
        history = list(engine.history[symbol])
        prior = history[-61:-1]
        result = loop.on_bar(symbol, bar, plan.market_map, prior,
                             tick_size=tick_size)
        if bar["t"] < start_ms:
            continue
        scored_bars += 1
        day = bar["t"] // 86_400_000
        if (proposer is not None and day not in attempted_days and
                len(attempted_days) < max_model_calls and result["plans"]):
            attempted_days.add(day)
            # A proposal may first match after the next candle has closed;
            # the replay makes no instant-response assumption about the N100.
            answer = loop.propose(result["snapshot"],
                                  created_at_ms=result["snapshot"].context_asof_ms + 60_000)
            attempts.append({"day": day, "status": answer.status,
                             "new_proposals": [item.key for item in answer.proposals],
                             "eval_count": answer.eval_count,
                             "eval_duration_ns": answer.eval_duration_ns,
                             "error": answer.error})
    rows = loop.ledger.read_rows()
    counts = Counter(row["kind"] for row in rows)
    outcomes = [row for row in rows if row["kind"] == "outcome"]
    nets = [row["net_pct"] for row in outcomes]
    report = {
        "purpose": "exploratory forward paper replay; not portfolio P&L or validated edge",
        "symbol": symbol, "start_ms": start_ms,
        "end_ms_exclusive": universe["end_ms_exclusive"],
        "tick_size": tick_size, "scored_bars": scored_bars,
        "coverage": coverage, "ledger_counts": dict(counts),
        "model_attempts": attempts,
        "paper_outcomes": {
            "count": len(nets),
            "mean_net_pct_per_trial": statistics.mean(nets) if nets else None,
            "median_net_pct_per_trial": statistics.median(nets) if nets else None,
            "positive_fraction": (sum(value > 0 for value in nets) / len(nets)
                                  if nets else None),
        },
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path,
                        default=Path("data/usdm_15d_20261025"))
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ollama", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-model-calls", type=int, default=15)
    parser.add_argument("--seed-proposal", type=Path, default=None,
                        help="Explicit time-zero control to verify paper execution")
    args = parser.parse_args()
    if not 0 <= args.max_model_calls <= 100:
        parser.error("--max-model-calls must be 0..100")
    report = run(args.run_dir, args.symbol.upper(), args.output_dir,
                 use_ollama=args.ollama, model=args.model,
                 max_model_calls=args.max_model_calls,
                 seed_proposal=args.seed_proposal)
    print(json.dumps({key: report[key] for key in
                      ("symbol", "scored_bars", "ledger_counts",
                       "paper_outcomes", "model_attempts")}, indent=2))


if __name__ == "__main__":
    main()
