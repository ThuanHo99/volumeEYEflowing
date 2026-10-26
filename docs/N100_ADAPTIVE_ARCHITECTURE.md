# Adaptive shadow research on an Intel N100, 16 GB RAM, Linux

## Current result and model choice

The adaptive system is **paper research only**. It reads every selected closed
1m chart, can ask a localhost LLM for new numeric hypotheses, and evaluates
them using later 1m candles. It has no exchange order path or automatic
promotion. The prior 15-day all-symbol replay found negative average net
outcomes for v2/v3; see [USD-M validation](USDM_15D_VALIDATION.md). This code
does not establish a profitable strategy.

The target is the user's **N100 with 16 GB RAM on Linux**. Intel specifies four
cores/four threads, 6 W TDP, one memory channel, and up to 16 GB RAM for this
CPU. Model file size is not total process RAM; context and parallel requests
also consume memory. [Intel N100 specifications](https://www.intel.com/content/www/us/en/products/sku/231803/intel-processor-n100-6m-cache-up-to-3-40-ghz/specifications.html),
[Ollama memory notes](https://docs.ollama.com/faq).

| Local model | Official package | Use |
|---|---:|---|
| [Qwen3 0.6B Q4_K_M](https://ollama.com/library/qwen3/tags) | 523 MB | **First choice** for short JSON scenario proposals, one asynchronous call per UTC day |
| [Qwen3 1.7B Q4_K_M](https://ollama.com/library/qwen3/tags) | 1.4 GB | Optional quality comparison after N100 latency measurement |
| [Qwen3.5 0.8B](https://ollama.com/library/qwen3.5/tags) | About 1.2–1.3 GB | Separate future chart-image experiment; no image path is implemented here |

No small model has been verified as a ready-made profitable crypto trader.
[FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) is useful financial
research, but its published tasks do not validate USD-M 1m trade execution on
an N100. The LLM is a **proposal writer**, not the fast decision engine. A
frozen LLM does not update its weights because it saw one losing trade.

## Implemented loop

```text
closed 1m candle + point-in-time 15m/1h/4h structure
  → action-independent numeric snapshot and structural stop/target plans
  → match active, versioned LLM hypotheses to observable conditions
  → sample independent paper trials at the next 1m open
  → 1m stop-first / adverse-gap / tick-rounded / cost-adjusted outcome
  → append-only ledger; only mature outcomes update scenario estimates
  → default abstention until enough paper samples and positive lower bound
```

The adaptive path evaluates every eligible closed chart; it does **not** need
one of v3's four named setup signals. Structural support/resistance remains a
deterministic map used to define where a paper thesis fails and where the next
obstacle lies. A proposed condition may use only allowlisted numeric facts,
such as RVOL, taker delta, candle effort/result, trend efficiency, zone
distance, directional room/risk, and regime flags. The model cannot run code,
change leverage, or send orders. Unknown features make a hypothesis ineligible.

The scanner can add **previously fetched** Binance derivative observations:
OI change, account/position ratios, taker buy/sell ratio, funding, basis,
spread/depth, and estimated OI/market-cap ratio. A metric is available to a
decision only if both its source observation and REST fetch completed by that
candle close. Post-event enrichment cannot rewrite the decision. An
allowlisted field does not imply historical data exists: the frozen 15-day
replay has 1m klines but no historical derivative or order-book snapshots.

`AdaptiveShadowLoop` limits new proposals to two per UTC day and 32 active
versions. New conditions with the same scenario ID become a new immutable
version; when capacity is full, the lowest estimated noneligible hypothesis is
retired from future trials, while its old outcomes stay in the ledger. Each
proposal/symbol has a cooldown and at most one open trial. Paper sampling is
bounded to six decisions per symbol per UTC day, spaced at least four hours
apart, so the N100 ledger does not grow without a daily limit. The ranker uses a
minimum of 30 closed outcomes, shrinkage toward zero, and a conservative lower
bound. These are **research gates**, not statistical proof of a tradable edge:
overlapping trials and regime drift still need blocked forward validation.

Paper fills enter at the next contiguous 1m open. If a minute touches both
TP and SL, the stop wins. A stop gap exits at the worse open. Missing minutes,
zero-volume execution, and positions unobserved across restart are censored,
never called wins. The assumed round-trip fee/slippage cost is 0.2 percentage
points per trial; change this only after measuring the actual venue/account.

Relevant modules: `adaptive_observation.py`, `adaptive_scenarios.py`,
`local_scenario_proposer.py`, `adaptive_paper.py`, `adaptive_shadow.py`, and
`scripts/replay_adaptive_shadow.py`. The scanner integration is opt-in and
keeps the v2/v3 publication paths independent.

## Run on the N100 Linux box

Install Ollama using its [official Linux instructions](https://docs.ollama.com/linux)
and keep its API on localhost. Pull the exact model tag:

```bash
ollama pull qwen3:0.6b-q4_K_M
ollama list
python -m scripts.bench_local_proposer --model qwen3:0.6b-q4_K_M --runs 20
```

The benchmark measures total wall time, p95 wall time, valid-schema replies,
and generation tokens per second on the actual host. Nothing here has measured
the user's N100 yet. The code requests one short, non-thinking JSON reply with
a 2048-token context and validates the schema again in Python. See
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
and [thinking controls](https://docs.ollama.com/capabilities/thinking).

After the bot and Ollama run on that machine, enable both switches in its
`.env` and restart the scanner:

```dotenv
VS_KLINE_INTERVAL=1m
VS_V3_SHADOW_ENABLED=true
VS_ADAPTIVE_SHADOW_ENABLED=true
VS_ADAPTIVE_LOCAL_MODEL_ENABLED=true
VS_ADAPTIVE_MODEL=qwen3:0.6b-q4_K_M
```

The scanner obtains `PRICE_FILTER.tickSize` once from Binance USD-M
`/fapi/v1/exchangeInfo`, observes at most `VS_V3_MAX_SYMBOLS` selected symbols,
and asks Ollama once per UTC day using BTC if selected. If exchange metadata
is unavailable, paper evaluation is idle; if Ollama is unavailable, existing
proposals continue but no new hypothesis is created. The ledger is
`data/vs_adaptive_shadow.jsonl` unless `DATA_DIR` changes. No adaptive
Telegram alert or real trade is emitted.

To reproduce a frozen one-symbol research run with the locally installed
model (using a **fresh** output directory each time):

```bash
python -m scripts.replay_adaptive_shadow \
  --run-dir data/usdm_15d_20261025 --symbol BTCUSDT \
  --output-dir data/adaptive_btc_qwen_run1 --ollama
```

The replay delays each generated hypothesis until the following candle has
closed. It logs model status and generation timing. The generated scenarios
and the ranker's updates are adaptive through this exploratory run, so the
same period is not a pristine independent holdout for a final policy.

## Verification and remaining gates

On the current development host, a deliberately simple, predefined BTC
control hypothesis verified the paper wiring over all 21,600 scored minutes:
45 decisions, 45 mature outcomes, mean net **−0.2024% per trial** under the
0.2 percentage-point assumed cost. This is a bad control and is **not** an
AI result. See `data/adaptive_shadow_btc_smoke_20261026_c/report.json`.

The next proof requires running the local model on the actual N100, recording
its latency and proposal acceptance rate, then testing those versioned
proposals on later untouched periods across symbols and market regimes.
Compare with no trade and v3 using net expectancy, drawdown, turnover, and
concentration by symbol/day. Preserve an immutable policy cutoff and measure
live paper decisions before considering any order execution. The current
system cannot honestly be rated as a profitable or human-equivalent trader.
