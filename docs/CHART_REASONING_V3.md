# Chart reasoning v3 — implemented shadow research slice

V3 makes prospective, timestamped price plans. It does not claim to read a
chart like an experienced trader or produce profitable entries. The existing
`context_v2` pipeline remains the default alert path; v3 has no Telegram or
trade-execution output. See [the codebase map](CODEBASE_MAP.md) for wiring.

## Runtime scope

- `VS_V3_SHADOW_ENABLED=true` runs the shadow engine alongside v2 when the
  scanner uses `VS_KLINE_INTERVAL=1m`; `VS_V3_MAX_SYMBOLS=32` selects the first
  32 symbols from the scanner's ranked universe by default. Setting the flag
  false or the count to zero disables it. It does not scan all 500 symbols.
- `VolumeSniper/htf_feed.py` fetches up to 100 Binance USD-M 15m, 1h, and 4h
  klines per selected symbol in the background. A captured cutoff excludes
  bars that had not closed before the scanner's 1m seed. Live 1m candles then
  complete later higher-timeframe bars locally. Failed HTF reads stay missing;
  they are not fabricated.
- `VolumeSniper/market_structure.py` builds zones from confirmed swing
  highs/lows with two bars on each side, groups nearby pivots, and counts
  distinct visits. Each zone carries timeframe, bounds, pivot times,
  confirmation times, and touch times. It labels the market `range`, `trend`,
  `transition`, or `unclear`, with trend direction separately. A recent 15m
  or 1h structure can be used when 4h is unavailable. Data gaps discard the
  older contiguous run. These zone widths and regime rules are hypotheses,
  not established market laws.
- `VolumeSniper/chart_reasoning_v3.py` stores a plan before testing the next
  closed 1m candle. Its four prospective scenarios are break up/down and
  failed break up/down at the nearest structural zones. An ordinary-volume
  touch can be a directionless WATCH. Candidates need later closed-candle
  price response; a shallow held retest is distinct from a deep sweep and
  reclaim. Pending candidates expire after 12 candles. A WebSocket gap
  censors the pending thesis and later outcome.
- Confirmation requires a structural invalidation on the correct side and a
  known opposing zone with at least `1.5 × risk + 0.2% of price` of room.
  Otherwise the journal records `no_trade` with a reason. Those numbers are
  research assumptions, not optimized thresholds, actual fees, a stop order,
  or a position-sizing rule. Rolling RVOL and taker delta are recorded as
  observations, not direction votes.
- A v3 candidate or confirmation can trigger the existing bounded derivatives
  collector. Its OI, positioning, basis, funding, and depth snapshot is logged
  with its own later observation time as post-event context. It never rewrites
  the earlier plan or becomes evidence for that earlier decision.

## Plan, events, and UI

`ChartPlan.to_dict()` contains `symbol`, `as_of_ms`, `decision=WAIT|NO_TRADE`
and an explicit reason when structure is insufficient,
`market_map` (price, regime, zones, nearest support/resistance, data cutoffs,
bar counts, missing/stale issues), four or fewer scenarios with triggers,
invalidations, and expiry, and `research_only=true`. The prior plan timestamp
is included with each candidate, WATCH, confirmation, expiration, and
`no_trade` event. Plan changes and events are appended to
`data/vs_chart_v3_events.jsonl`; completed observations also receive 5/15
candle gross outcomes. The journal is observational, not realized P&L.

The scanner dashboard's symbol search retains its v2 plan and adds a separate
v3 panel. `GET /api/chart-plan?symbol=BTC` serves the latest v3 plan with
`Cache-Control: no-store`. Live `chart_v3_event` messages refresh that panel;
they do not become signal cards, Telegram messages, or trade instructions.
An absent plan returns a warming-up response, which can also mean the symbol
is outside the selected 32-symbol research universe.

## Replay and limits

Run `python -m scripts.replay_chart_v3 --cost-pct 0.2` against
`data/ohlcv_cache.json` (or pass `--cache PATH`). It advances v2 and v3 one
closed 1m candle at a time, ignores precomputed higher-timeframe cache bars,
checks that a plan predates each event, and reports WATCH, candidate,
confirmation, no-trade, and censored counts. Its hypothetical entry is the
next 1m open; a structural stop is checked against subsequent OHLC, with an
adverse open on a gap; 5/15-candle results subtract the stated round-trip
cost in percentage points. It also reports a zero-return no-signal baseline
and unconditional absolute moves. This is a research comparison, not an
executable fill or portfolio simulation.

The current 40,320-candle comparison and its negative assumed net results
are recorded in [V3 validation](V3_VALIDATION.md).

The current historical cache covers only four coins over roughly one week.
It has no historical derivatives, order book, spread, slippage, funding,
portfolio sizing, or reliable taker-flow series for this replay. Live v2
derivatives snapshots are not retroactively inserted into v3 decisions.
Chart-image vision, a synchronized depth stream, discretionary pattern
recognition, and account-risk sizing are not implemented. Neither v2 nor v3
has demonstrated a net edge on unseen data.

Before any alert-path change, replay on untouched time blocks and symbols;
vary plausible fees, spread, slippage, funding, and stop assumptions; compare
net expectancy, drawdown, missed opportunities, and abstention with v2 and
no-signal baselines. A plausible explanation of a chart is not evidence of a
tradable edge.
