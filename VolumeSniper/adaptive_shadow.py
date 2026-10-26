"""Research-only loop connecting generated chart hypotheses and paper feedback.

Call ``on_bar`` after the chart engine builds a map for each closed 1m candle.
No v3 named signal is required. This loop never places exchange orders.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from .adaptive_observation import observe_chart
from .adaptive_paper import AdaptivePaperBook
from .adaptive_scenarios import (Condition, FeatureSnapshot, ScenarioLedger,
                                 ScenarioProposal, ScenarioRanker, evaluate,
                                 )
from .local_scenario_proposer import LocalScenarioProposer, ProposalResult
from .market_structure import MarketMap


def _restore_proposal(row: dict) -> ScenarioProposal:
    return ScenarioProposal(
        row["scenario_id"], row["version"],
        tuple(Condition(**condition) for condition in row["conditions"]),
        row["action"], row["stop_mult"], row["target_r"],
        row["max_hold_minutes"], row["created_at_ms"],
        row.get("rationale", ""),
    )


class AdaptiveShadowLoop:
    """Forward paper evaluation for up to a bounded number of proposals."""

    def __init__(self, ledger_path: Path, *, asof_ms: int,
                 proposer: LocalScenarioProposer | None = None,
                 max_proposals: int = 32, min_samples: int = 30,
                 max_paper_per_symbol_day: int = 6,
                 min_symbol_spacing_minutes: int = 240):
        if type(asof_ms) is not int or asof_ms < 0:
            raise ValueError("asof_ms must be a nonnegative timestamp")
        if not 1 <= max_proposals <= 128:
            raise ValueError("max_proposals must be 1..128")
        if not 1 <= max_paper_per_symbol_day <= 100:
            raise ValueError("max_paper_per_symbol_day must be 1..100")
        if not 1 <= min_symbol_spacing_minutes <= 1_440:
            raise ValueError("min_symbol_spacing_minutes must be 1..1440")
        self.ledger = ScenarioLedger(ledger_path)
        self.paper = AdaptivePaperBook()
        self.ranker = ScenarioRanker(min_samples=min_samples)
        self.proposer = proposer
        self.max_proposals = max_proposals
        self.max_paper_per_symbol_day = max_paper_per_symbol_day
        self.min_symbol_spacing_ms = min_symbol_spacing_minutes * 60_000
        self.proposals: dict[tuple[str, int], ScenarioProposal] = {}
        self.active: set[tuple[str, int]] = set()
        self._last_queued: dict[tuple[str, str, int], int] = {}
        self._last_symbol_queued: dict[str, int] = {}
        self._symbol_day_counts: Counter[tuple[str, int]] = Counter()
        rows = self.ledger.read_rows()
        resolved = {row["decision_id"] for row in rows
                    if row["kind"] in {"outcome", "censored"}}
        for row in rows:
            if row["kind"] == "proposal":
                item = _restore_proposal(row)
                self.proposals[item.key] = item
                self.active.add(item.key)
            elif row["kind"] == "retired":
                self.active.discard((row["scenario_id"], row["version"]))
            elif row["kind"] == "outcome":
                self.ranker.update(row, asof_ms=asof_ms)
            elif row["kind"] == "decision":
                self._last_queued[(row["symbol"], row["scenario_id"],
                                   row["version"])] = row["decision_at_ms"]
                self._last_symbol_queued[row["symbol"]] = row["decision_at_ms"]
                self._symbol_day_counts[(row["symbol"],
                                         row["decision_at_ms"] // 86_400_000)] += 1
        # The in-memory paper book cannot know a fill that occurred while the
        # process was down. Resolve such decisions as censored, never as wins.
        for row in rows:
            if (row["kind"] == "decision" and row["decision_id"] not in resolved and
                    row["decision_at_ms"] < asof_ms):
                self.ledger.append_censor(row["decision_id"], asof_ms,
                                          "restart_unobserved")

    def propose(self, snapshot: FeatureSnapshot, *,
                created_at_ms: int) -> ProposalResult:
        """Ask Ollama off-path; accept at most two new paper-only proposals/day."""
        if self.proposer is None:
            return ProposalResult("unavailable", error="local model not configured")
        rows = self.ledger.read_rows()
        day = created_at_ms // 86_400_000
        today = sum(row["kind"] == "proposal" and
                    row["created_at_ms"] // 86_400_000 == day for row in rows)
        if today >= 2:
            return ProposalResult("invalid_input", error="daily proposal cap reached")
        closed = self._recent_feedback(rows, created_at_ms)
        result = self.proposer.propose(
            snapshot, created_at_ms=created_at_ms, recent_outcomes=closed)
        return self.accept_proposals(result, created_at_ms=created_at_ms)

    def _recent_feedback(self, rows: list[dict], asof_ms: int) -> list[dict]:
        """Join only mature outcomes to the conditions and facts that preceded them."""
        decisions = {row["decision_id"]: row for row in rows
                     if row["kind"] == "decision" and row["decision_at_ms"] <= asof_ms}
        feedback = []
        for row in rows:
            if row["kind"] != "outcome" or row["matured_at_ms"] > asof_ms:
                continue
            decision = decisions.get(row["decision_id"])
            proposal = self.proposals.get((row["scenario_id"], row["version"]))
            if decision is None or proposal is None:
                continue
            execution = row.get("execution") or {}
            feedback.append({**row,
                             "conditions": [asdict(c) for c in proposal.conditions],
                             "entry_features": {name: value["value"]
                                                for name, value in decision["features"].items()},
                             "exit_reason": execution.get("reason")})
        return feedback[-5:]

    def accept_proposals(self, result: ProposalResult, *,
                         created_at_ms: int) -> ProposalResult:
        """Commit validated LLM output at its real completion time."""
        if result.status != "ok":
            return result
        rows = self.ledger.read_rows()
        day = created_at_ms // 86_400_000
        today = sum(row["kind"] == "proposal" and
                    row["created_at_ms"] // 86_400_000 == day for row in rows)
        accepted = []
        rationales = []
        for item, rationale in zip(result.proposals, result.rationales):
            if len(accepted) + today >= 2:
                continue
            old_versions = [existing for existing in self.proposals.values()
                            if existing.scenario_id == item.scenario_id]
            if old_versions:
                latest = max(old_versions, key=lambda existing: existing.version)
                if (latest.conditions, latest.action, latest.stop_mult,
                        latest.target_r, latest.max_hold_minutes) == (
                        item.conditions, item.action, item.stop_mult,
                        item.target_r, item.max_hold_minutes):
                    continue
                version = latest.version + 1
                if created_at_ms <= latest.created_at_ms:
                    continue
            else:
                version = 1
            item = ScenarioProposal(
                item.scenario_id, version, item.conditions, item.action,
                item.stop_mult, item.target_r, item.max_hold_minutes,
                created_at_ms, rationale)
            if len(self.active) >= self.max_proposals:
                inactive_candidates = [self.proposals[key] for key in self.active
                                       if not self.ranker.score(self.proposals[key]).eligible]
                pool = inactive_candidates or [self.proposals[key]
                                               for key in self.active]
                retired = min(pool, key=lambda existing: (
                    self.ranker.score(existing).estimated_net_pct,
                    existing.created_at_ms, existing.scenario_id))
                self.ledger.append_retire(retired, created_at_ms,
                                          "paper_capacity_rotation")
                self.active.remove(retired.key)
            self.ledger.append_proposal(item)
            self.proposals[item.key] = item
            self.active.add(item.key)
            accepted.append(item)
            rationales.append(rationale)
        return ProposalResult("ok", tuple(accepted), tuple(rationales),
                              result.eval_count, result.eval_duration_ns)

    async def propose_async(self, snapshot: FeatureSnapshot) -> ProposalResult:
        """Run the slow localhost request in a thread, then commit on loop."""
        if self.proposer is None:
            return ProposalResult("unavailable", error="local model not configured")
        started = int(time.time() * 1000)
        if snapshot.context_asof_ms > started:
            return ProposalResult("invalid_input", error="snapshot is in the future")
        rows = self.ledger.read_rows()
        if sum(row["kind"] == "proposal" and
               row["created_at_ms"] // 86_400_000 == started // 86_400_000
               for row in rows) >= 2:
            return ProposalResult("invalid_input", error="daily proposal cap reached")
        recent = self._recent_feedback(rows, started)
        result = await asyncio.to_thread(
            self.proposer.propose, snapshot, created_at_ms=started,
            recent_outcomes=recent)
        return self.accept_proposals(result, created_at_ms=int(time.time() * 1000))

    def on_bar(self, symbol: str, candle: dict, market_map: MarketMap,
               prior_candles: Sequence[Mapping[str, object]], *,
               tick_size: float,
               derivatives: Mapping[str, object] | None = None) -> dict:
        """Settle old positions, then sample matching hypotheses on this chart.

        Each proposal is evaluated independently, including proposals the
        current ranker would reject. That supplies unbiased shadow feedback;
        the returned recommendation still defaults to no trade.
        """
        settled = self.paper.on_closed_bar(symbol, candle)
        for result in settled:
            if result.status == "evaluated":
                row = self.ledger.append_outcome(
                    result.decision_id, result.matured_at_ms, result.net_pct,
                    execution=asdict(result))
                self.ranker.update(row, asof_ms=result.matured_at_ms)
            else:
                self.ledger.append_censor(result.decision_id,
                                          result.matured_at_ms, result.reason)
        observation = observe_chart(market_map, candle, prior_candles,
                                    tick_size=tick_size, derivatives=derivatives)
        snapshot = observation.snapshot
        matching = [item for key in self.active
                    for item in (self.proposals[key],)
                    if item.created_at_ms <= snapshot.context_asof_ms and
                    (item.action in observation.plans or item.action == "NO_TRADE") and
                    evaluate(item, snapshot, snapshot.context_asof_ms)]
        matching.sort(key=lambda item: (item.scenario_id, item.version))
        chosen = self.ranker.choose(matching, snapshot, snapshot.context_asof_ms)
        recommendation = {
            "symbol": symbol, "observed_at": snapshot.context_asof_ms,
            "paper_only": True, "scenario": chosen.key if chosen else None,
            "estimated_net_pct": (self.ranker.score(chosen).estimated_net_pct
                                  if chosen else None),
        }
        directional = [item for item in matching
                       if item.action in observation.plans]
        if directional:
            offset = (snapshot.context_asof_ms // 60_000) % len(directional)
            rotating = directional[offset:] + directional[:offset]
            candidates = ([chosen] if chosen else []) + [
                item for item in rotating if item != chosen]
        else:
            candidates = []
        paper_queued = []
        day_key = (symbol, snapshot.context_asof_ms // 86_400_000)
        last_symbol = self._last_symbol_queued.get(symbol)
        if (self._symbol_day_counts[day_key] >= self.max_paper_per_symbol_day or
                (last_symbol is not None and
                 snapshot.context_asof_ms - last_symbol < self.min_symbol_spacing_ms)):
            candidates = []
        for item in candidates:
            if (len(paper_queued) >= 2 or
                    self._symbol_day_counts[day_key] >= self.max_paper_per_symbol_day):
                break
            key = (symbol, item.scenario_id, item.version)
            last = self._last_queued.get(key)
            cooldown_ms = max(5, item.max_hold_minutes // 2) * 60_000
            if ((last is not None and snapshot.context_asof_ms - last < cooldown_ms) or
                    self.paper.has_exposure(*key)):
                continue
            structural = observation.plans[item.action]
            decision_id = (f"{symbol}:{candle['t']}:"
                           f"{item.scenario_id}:{item.version}")
            plan = {
                "invalidation": structural.invalidation,
                "planned_target": structural.planned_target,
                "tick_size": tick_size, "cost_pct": 0.2,
            }
            self.ledger.append_decision(
                decision_id, symbol, item, snapshot,
                snapshot.context_asof_ms, execution_plan=plan)
            try:
                self.paper.queue(
                    decision_id, symbol, item, snapshot,
                    invalidation=plan["invalidation"],
                    planned_target=plan["planned_target"],
                    tick_size=tick_size, cost_pct=plan["cost_pct"])
            except Exception:
                self.ledger.append_censor(
                    decision_id, snapshot.context_asof_ms + 1,
                    "paper_admission_failed")
                raise
            self._last_queued[key] = snapshot.context_asof_ms
            self._last_symbol_queued[symbol] = snapshot.context_asof_ms
            self._symbol_day_counts[day_key] += 1
            paper_queued.append(decision_id)
        return {"settled": settled, "paper_queued": paper_queued,
                "recommendation": recommendation,
                "snapshot": snapshot, "plans": observation.plans}
