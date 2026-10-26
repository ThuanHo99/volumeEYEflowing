"""Measure local Ollama typed-proposal latency on the actual N100 host."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter

from VolumeSniper.adaptive_scenarios import FeatureSnapshot, FeatureValue
from VolumeSniper.local_scenario_proposer import DEFAULT_MODEL, LocalScenarioProposer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.runs <= 100:
        parser.error("--runs must be 1..100")
    proposer = LocalScenarioProposer(model=args.model)
    now = int(time.time() * 1000)
    sample = FeatureSnapshot(now, {
        "rvol": FeatureValue(2.1, now),
        "body_to_range": FeatureValue(0.25, now),
        "taker_delta_pct": FeatureValue(18, now),
        "trend_efficiency": FeatureValue(0.16, now),
        "long_room_r": FeatureValue(1.8, now),
        "short_room_r": FeatureValue(0.9, now),
        "regime_range": FeatureValue(1, now),
    })
    timings = []
    eval_rates = []
    statuses = Counter()
    for _ in range(args.runs):
        begin = time.perf_counter()
        result = proposer.propose(sample, created_at_ms=now)
        timings.append(time.perf_counter() - begin)
        statuses[result.status] += 1
        if result.eval_count is not None and result.eval_duration_ns:
            eval_rates.append(result.eval_count / (result.eval_duration_ns / 1e9))
    ordered = sorted(timings)
    report = {
        "model": args.model, "runs": args.runs, "status_counts": dict(statuses),
        "median_wall_seconds": statistics.median(timings),
        "p95_wall_seconds": ordered[max(0, int(0.95 * len(ordered) + 0.999999) - 1)],
        "median_eval_tokens_per_second": (statistics.median(eval_rates)
                                          if eval_rates else None),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
