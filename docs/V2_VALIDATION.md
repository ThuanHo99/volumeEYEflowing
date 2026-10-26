# Context v2 validation status

Context v2 is **research mode by default**. Dashboard events and the durable
candidate/outcome journal are enabled; Telegram context alerts require the
explicit `VS_CONTEXT_TELEGRAM_ALERTS_ENABLED=true` setting. Core RSI/EMA
Telegram alerts are off by default.

The revised engine prepares the next candle's range before that candle starts,
shows the plan on the scanner dashboard, records high-volume contests at the
range edge as non-directional WATCH events, and requires a held retest for a
breakout confirmation. Invalidation levels are displayed in the plan before
an event. OI and other derivatives observations now enrich the context, while
position sizing and any directional rule based on them remain unimplemented.

## Market-data source correction (2026-10-22)

The scanner's previous default mixed production REST warmup
(`fapi.binance.com`) with a testnet WebSocket host
(`fstream.binancefuture.com`). The production USD-M Kline feed is now the
`/market/stream?streams=` route on `fstream.binance.com`, and scanner startup
rejects known environment mismatches or the obsolete production route.
Binance documents the [production route and Kline mapping](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice)
and identifies the [former WebSocket host as testnet](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log).
Past live scanner signals with the former default cannot be treated as a
clean production sample. This is a plausible contributor to poor signals,
but there is no event-level evidence here to apportion causes. Existing
offline replay below used a separate core OHLCV cache, not the scanner feed.

## Offline replay available today

Command: `python -m scripts.replay_context --cost-pct 0.2`

The existing `data/ohlcv_cache.json` contains BTC, ETH, SOL and XRP 1m candles
from **2026-05-24 13:43 to 2026-05-31 13:42 UTC**. It is not a sample of the
scanner's top-500 altcoins. It has no taker-buy field and only one week of
history, so all RVOL values use the rolling provisional baseline.

With a 20-candle range and RVOL 2x, the revised chronological replay found
**2,072 non-directional edge watches**, **1,104 candidates** and **491
confirmed scenarios**. All watches and candidates refer to a plan timestamp
before their event candle. For the **490** confirmations with a complete
five-candle follow-up, mean directional close-to-close return was **+0.00462%
gross**, or **−0.19538%** after an illustrative **0.2 percentage point**
round-trip cost. The 15-candle mean was **−0.01126% gross**. These are
descriptive numbers, not a strategy estimate: no order fill, spread, funding,
intrabar stop, or selection effects
are modelled. They do not justify live directional push alerts.

The absolute five-candle move after a confirmed scenario averaged **0.122%**
versus **0.085%** over all five-candle windows in that cache; at 15 candles,
**0.213%** versus **0.148%**. This suggests these setups marked more active
periods in that sample, while their directional return was approximately zero.
Overlapping windows and the narrow historical sample prevent a significance
claim. The replay command reports both comparisons so future runs can check
whether the distinction persists.

## Required next evidence

Let v2 collect complete same-hour/same-weekday volume samples and the
`data/vs_context_events.jsonl` journal. Once the seasonal baseline is ready,
analyze watches, candidates and confirmed events in chronological, untouched periods.
Compare both the ability to identify unusually large moves and the ability
to identify direction. For hypothetical trades, deduct venue-specific fees,
spread, slippage and funding; test invalidation with candle OHLC rather than
assuming a favorable close. Count unconfirmed and censored scenarios too.
Do not tune a threshold from the one-week cache and report its in-sample
outcome as predictive performance.

## Derivatives enrichment check (2026-10-23)

The read-only BTCUSDT USD-M smoke request returned valid current OI, OI
history, all-account long/short, taker buy/sell, perpetual basis, mark/funding,
settled funding history, and top-20 depth. The two top-trader ratios were
reported `api_key_required` because no key was supplied; those paths were
verified against the current Binance API catalog but not exercised live.
This checks transport and parsing only. The older candle replay has no
historical derivatives snapshots, so it cannot measure whether these fields
improve signal outcomes.

