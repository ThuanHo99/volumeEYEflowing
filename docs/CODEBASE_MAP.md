# Codebase map — VolHunter

Read this file first when changing signal logic. The workspace has no `.codegraph/`
index and is not currently a Git checkout. Generated assets, `node_modules/`,
`.env`, and `data/` are not source code.

For the implemented price-first shadow research slice, see
[Chart reasoning v3](CHART_REASONING_V3.md).
For the frozen all-symbol 1m holdout replay and execution assumptions, see
[USD-M 15-day validation](USDM_15D_VALIDATION.md).
For the adaptive N100 Linux paper loop, see
[Adaptive shadow research](N100_ADAPTIVE_ARCHITECTURE.md).

## Runtime paths

```text
python main.py
  ├─ core/config.py: .env, symbols, timeframes, feature switches
  ├─ core/ingestion.py: ccxt REST warmup + one OHLCV WebSocket producer per core symbol
  │    → candle_queue → core/state_manager.consume_candles()
  │    → indicator snapshots → signal_queue → main.signal_consumer()
  │    → optional legacy TelegramNotifier; also → ui_queue
  ├─ ui/web_dashboard.py: aiohttp /ws, /dashboard, /health, /metrics
  │    ← ui_queue → WebSocket clients and persisted signal buffer
  └─ VolumeSniper/scanner.py (if VS_ENABLED)
       ← Binance Futures combined kline streams for top MAX_SYMBOLS
       → VolumeSniper/volume_detector.py: closed candle state + warmup
       → VolumeSniper/volume_context.py (default context_v2)
       → VolumeSniper/chart_reasoning_v3.py (1m, selected symbols, research only)
         ← VolumeSniper/market_structure.py + htf_feed.py (closed 15m/1h/4h)
       → optional VolumeSniper/adaptive_shadow.py (every selected closed 1m map)
         ← adaptive_observation.py (typed facts + structural paper plans)
         ← local_scenario_proposer.py (localhost Ollama, off candle path)
         → adaptive_scenarios.py (versioned ledger + delayed ranker)
         → adaptive_paper.py (next-open 1m paper TP/SL)
       → VolumeSniper/derivatives_context.py (on-demand Binance USD-M observations)
       → VolumeSniper/telegram_bot.py + ui_queue
```

`main.TaskSupervisor` restarts producers/consumers. `main.warmup_phase`
loads cached or fetched candles and feeds the same indicator state as live mode.
`VolumeSniper/main.py` is an optional standalone entry point via
`python -m VolumeSniper.main`.

## Where to edit

| Concern | Source of truth | Important callers / consumers |
|---|---|---|
| Exchange and core settings | `core/config.py` | `main.py`, `core/*`, `ui/web_dashboard.py` |
| Core OHLCV transport | `core/ingestion.py` | `main.py`, `core/state_manager.py` |
| EMA/RSI state and candle contract | `core/state_manager.py` | `main.signal_consumer`, dashboard broadcast |
| Old RSI/EMA/divergence checks | `core/signals.py` | `main.signal_consumer`; alerts disabled by default |
| Scanner settings | `VolumeSniper/config.py` | detector, context engine, scanner, Telegram |
| Scanner transport and publication | `VolumeSniper/scanner.py` | `main.py`; `ui_queue`, Telegram |
| Scanner kline warmup/state | `VolumeSniper/volume_detector.py` | scanner, chart generator, legacy pipeline |
| v2 event/confirmation logic | `VolumeSniper/volume_context.py` | scanner; writes seasonal baseline + event journal |
| v3 price zones and regime | `VolumeSniper/market_structure.py` | `chart_reasoning_v3.py`; closed 15m/1h/4h only |
| v3 higher-timeframe warmup | `VolumeSniper/htf_feed.py` | scanner background warmup, capped symbol set and HTTP concurrency |
| v3 prospective scenarios | `VolumeSniper/chart_reasoning_v3.py` | scanner shadow path; `data/vs_chart_v3_events.jsonl` |
| Adaptive chart facts and action-specific risk/room | `VolumeSniper/adaptive_observation.py` | adaptive shadow on every closed selected 1m candle; no v3 confirmation trigger |
| Adaptive proposal schema, ledger, ranker | `VolumeSniper/adaptive_scenarios.py` | generated hypotheses and mature paper outcomes; `data/vs_adaptive_shadow.jsonl` |
| Local Qwen3 structured proposals | `VolumeSniper/local_scenario_proposer.py` | localhost Ollama; at most one off-path request per UTC day from scanner |
| Conservative forward paper fills | `VolumeSniper/adaptive_paper.py` | next contiguous 1m open, exchange tick, stop-first tie, adverse gap, censor |
| Adaptive scanner research orchestration | `VolumeSniper/adaptive_shadow.py` | `VolumeSniper/scanner.py` when `VS_ADAPTIVE_SHADOW_ENABLED=true` |
| One-symbol frozen adaptive research replay | `scripts/replay_adaptive_shadow.py` | 15-day USD-M cache; optional local Ollama |
| N100 local model benchmark | `scripts/bench_local_proposer.py` | run on target Linux host after model install |
| v2/v3 chronological comparison | `scripts/replay_chart_v3.py` | `data/ohlcv_cache.json`, next-open outcome model |
| Frozen 15-day all-symbol USD-M 1m cache | `scripts/usdm_15d_data.py` | `data/usdm_15d_20261025/universe.json`, archive ZIPs, REST JSON, source manifests |
| Walk-forward USD-M TP/SL replay | `scripts/replay_usdm_15d.py` | v2/v3 engines, frozen cache, 10-day training policy selection, 5-day holdout |
| OI, trader ratios, taker flow, basis, funding, depth | `VolumeSniper/derivatives_context.py` | scanner background enrichment; `/api/derivatives-context` |
| Old RSI/EMA 3-zone model | `VolumeSniper/analyzer.py` | scanner only in `VS_STRATEGY_MODE=legacy` |
| Telegram authorization/commands | `VolumeSniper/telegram_bot.py` | scanner; `authorized_users.json` |
| Chart PNG | `VolumeSniper/chart_generator.py` | scanner; served through `ui/web_dashboard.py` |
| WebSocket/UI state | `ui/web_dashboard.py` | `static/js/dashboard.js`, `static/js/store.js` |
| Read-only pre-event plan API | `ui/web_dashboard.py` (`/api/context-plan?symbol=BTC`) | Scanner context plans; scanner search panel |
| Read-only v3 chart plan API | `ui/web_dashboard.py` (`/api/chart-plan?symbol=BTC`) | Scanner v3 plans; separate research panel |
| Read-only derivatives snapshot API | `ui/web_dashboard.py` (`/api/derivatives-context?symbol=BTC`) | Scanner search panel; one scanned symbol per request |
| Web chart interactions | `static/js/chart_tools.js` | `static/js/dashboard.js` |
| Dashboard HTML/CSS | `templates/index.html`, `static/css/dashboard.css` | browser |
| Landing page | `templates/landing.html`, `static/js/landing.js`, `static/css/landing*.css` | `/` |
| Persistence | `core/persistence.py` | main warmup/shutdown, dashboard signal replay |
| Sentiment widgets | `core/market_sentiment.py`, `core/fear_greed_cache.py` | dashboard/main |
| Monitoring | `core/metrics.py`, `docs/prometheus-config.yml` | `/metrics` |

## Data contracts and state

- Core candles: dict with `symbol`, `tf`, `start_ts` (milliseconds), OHLC,
  `volume`, `is_closed`. `IndicatorSnapshot` has the same market fields plus
  `ema200` and `rsi14`. `consume_candles` sends closed candles to signal and UI
  queues, open candles only to UI.
- Scanner candles: Binance Futures kline payload (`t`, `o`, `h`, `l`, `c`, `v`,
  `q`, `V`, `x`). `V` is taker-buy *base* volume; taker-sell base volume is
  `v - V`. Missing `V` means delta is unavailable, never zero.
- v2 UI alerts retain `type=volume_sniper_signal` so WebSocket replay and the
  scanner card work. New fields: `signal_type=VOLUME_CONTEXT`, `zone`
  `BREAKOUT|FAILURE`, `baseline`, `level`, `invalidation`, `delta_pct`, `cvd`,
  plus `plan_ts` and the upper/lower levels fixed before the event candle.
  Exchange Kline `t` is the candle **open** time; UI and Telegram display the
  close time at which the confirmation could actually be observed.
  Non-directional `volume_context_watch` events appear in the scanner view;
  the latest 100 watches are replayed to a reconnecting browser in memory.
- A v2 alert includes `derivatives.metrics` with `value`, `status`, `observed_at`,
  `fetched_at`, and `source`. Status may be `ok`, `stale`, `unavailable`,
  `api_key_required`, `rate_limited`, or `error`. The context journal gets a
  separate `derivatives_context` rows tied to candidate, watch, and confirmed
  events. Every snapshot records its fetch time, which may be seconds after the
  candle close. The signal's
  direction and confirmation rules do not change based on these metrics.
- `data/ohlcv_cache.json`, `indicator_state.json`, `rate_limits.json`, and
  `signal_buffer.json` belong to the core/dashboard. `data/vs_seasonal_volume.json`
  and `data/vs_context_events.jsonl` belong to v2. The latter is append-only,
  with watch, candidate, confirmation, expiration, and 5/15-candle gross outcome rows.
  `static/charts/*.png` are generated per alert.
- Adaptive paper research has a separate append-only
  `data/vs_adaptive_shadow.jsonl`: immutable versioned proposals, timestamped
  feature/plan decisions, mature net 1m outcomes, censors and retirement rows.
  It never reaches Telegram or an exchange order endpoint. Existing v2/v3
  publication remains separate. The adaptive snapshot has chart and kline
  taker-flow features and may include cached derivatives only when both their
  source and REST fetch timestamps precede the candle close. Post-event
  derivatives are not reused as pre-decision facts.

## v2 decision sequence

1. At every **closed** candle, prepare the next candle's range plan from the
   previous 20 closed candles. The scanner search panel and read-only API
   expose upper/lower levels, plan time, quieting observation and predefined
   invalidation levels. A candidate retains the exact plan that preceded it.
   Quieting is descriptive; it does not predict a breakout.
2. Compare a **closed** event candle's volume with the median from the same UTC
   weekday/hour in at least two earlier weeks. Until enough live history has
   accumulated, use the median of the previous 20 closed candles and label it
   `rolling (provisional)`. The candle under test is excluded.
3. A high RVOL candle near a preplanned boundary that fails to cross becomes
   a non-directional `WATCH` observation. A high RVOL close outside or wick
   through that boundary opens a candidate. Taker delta and CVD provide
   context only; neither casts a direction vote.
4. Emit `BREAKOUT` only when a later closed candle retests and holds the
   planned level. A run that never retests expires without a directional alert.
   Emit `FAILURE` only after price returns inside and fails to reclaim.
   Candidates expire after three closed candles; a WebSocket gap resets stale
   levels and plans.
5. Publish the *confirmation* close as the observed price and the nearby level
   as thesis invalidation. No fixed TP/SL or claimed confidence is generated.
   The journal records outcomes at 5 and 15 candles without assuming a fill.
   Dashboard publication is on; Telegram publication is off in the default
   research mode. See [replay results](V2_VALIDATION.md).
6. On candidate creation, start a bounded background fetch for USD-M OI,
   positioning, taker flow, basis, funding, and top-20 order book. Refresh at
   confirmation before publication without blocking the kline WebSocket.
   Interpret only fresh fields in the alert text; data gaps remain visible.
   The order book is a single REST snapshot, not a synchronized depth stream.

## Derivatives context contract

Endpoint paths and current key requirements were checked against the
[official Binance USD-M market-data API catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).

| Field | Binance USD-M path | Meaning / limitation |
|---|---|---|
| Current OI | `/fapi/v1/openInterest` | Base units; no side information |
| 5m OI history | `/futures/data/openInterestHist` | Notional and prior completed-period change |
| Top trader accounts / positions | `/futures/data/topLongShortAccountRatio`, `/futures/data/topLongShortPositionRatio` | Separate account count and position size; require `BINANCE_API_KEY` per current docs |
| All-account long/short | `/futures/data/globalLongShortAccountRatio` | Binance account positioning, not market-wide |
| Taker buy/sell | `/futures/data/takerlongshortRatio` | Completed-period aggressive volume; 1m kline `V` remains the event-level delta |
| Perpetual basis | `/futures/data/basis` | `PERPETUAL` versus Binance index, not an exchange-agnostic spot basis |
| Funding | `/fapi/v1/premiumIndex`, `/fapi/v1/fundingRate` | Latest quoted rate and most recent settled rate are separate |
| Depth | `/fapi/v1/depth?limit=20` | Displayed top-20 imbalance/spread at one point in time; orders can cancel, RPI orders are excluded |
| OI / market cap | Derived from `sumOpenInterestValue / (CMCCirculatingSupply × indexPrice)` | Approximate Binance OI/estimated cap, not global OI; missing supply stays unavailable |

The 5m (or configured) statistical series accepts only completed periods.
The cache uses 10 seconds for depth, 30 seconds for mark/funding, 60 seconds
for current OI, and 5 minutes for historical ratios and funding history.
It limits concurrent HTTP calls to 4 and defaults to at most 240 requests per
rolling 5 minutes across symbols. It polls only symbols with a candidate,
watch, confirmation, or explicit dashboard request. `BINANCE_API_KEY` is sent
only to the two documented top-trader ratio endpoints; no API secret is read.
Do not call `refresh()` over the full 500-symbol scan on every candle.

The current API is designed for live observation. Historical candle replay
does not backfill these metrics and therefore cannot validate any incremental
edge from them. Order book snapshots are not persistent liquidity and cannot
be treated as executed buying or selling.

## Comparison with the supplied volume note

| Note's question | Old implementation | v2 |
|---|---|---|
| Is volume unusual at this time? | 20-candle weighted mean (scanner), 24-hour simple mean (core) | Same weekday/hour median after two weeks; explicit provisional rolling median before then |
| Which side initiated trades? | Candle color treated as direction | Binance `V` gives taker delta; missing values stay unknown |
| How far did price move for that activity? | Minimum candle change and RSI/EMA/wick heuristics | Preplan prior range, then record price response and CVD context; stalled high-volume edge becomes WATCH |
| Did price hold the new level? | One directional confirmation candle measured from old spike close | Explicit multi-candle pending state; breakout requires a later held retest |
| Can the rule be checked prospectively? | No durable candidate/outcome record | Append-only event log and 5/15-candle gross outcomes |

## Known limits and validation

- The scanner REST warmup and WebSocket must use the same Binance environment.
  `VolumeSniper/config.py` validates this at scanner startup. Production USD-M
  klines use `wss://fstream.binance.com/market/stream?streams=`; the previous
  default used the testnet WebSocket host while REST used production. Historical
  live scanner observations made with that default have uncertain provenance
  and should not be pooled with new production observations. See the
  [Binance endpoint mapping](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice)
  and [change log naming the former host as testnet](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log).
- Same weekday/hour baseline starts as provisional after an empty install; no
  fabricated backfill or cross-venue coverage. It needs at least two earlier
  weeks of complete hourly observations for that symbol/slot. A restart loses
  the incomplete current hour but keeps completed hours on disk.
- Binance kline volume/delta and derivatives metrics are venue-specific. Delta
  does not identify who opened/closed a position. OI does not say which side
  will win; market-cap ratio is an estimate using supplied circulating supply.
- A predefined invalidation level is a thesis boundary, not an exchange stop
  order or a position-size rule. Execution costs and account risk remain to be
  evaluated before any trade.
- The `data/vs_context_events.jsonl` outcome uses confirmation-close to later
  close, before fees/slippage. It is **not** realized P&L or a validated edge.
  Compare candidate frequency, confirmed direction, gross return, and net
  return under realistic costs on unseen future data before changing thresholds.
- `README.MD` below its v2 note describes the legacy rules. Legacy can be
  restored with `VS_STRATEGY_MODE=legacy` and
  `CORE_LEGACY_ALERTS_ENABLED=true`.
- `VolumeSniper` currently has no full gap backfill after a WebSocket reconnect;
  v2 deliberately clears stale levels and warms up from new closed candles.

## Fast verification

```powershell
python -m compileall -q main.py core VolumeSniper ui notifiers
python -m pytest tests/unit/test_volume_context.py tests/unit/test_signal_main.py -q
python -m pytest tests/unit/test_derivatives_context.py -q
python -m pytest tests/unit -q
python -m scripts.replay_context --cost-pct 0.2
```

The integration tests may call external services. Unit tests and synthetic
context scenarios do not prove trading performance.
