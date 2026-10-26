# V3 shadow replay — 2026-10-25

Command: `python -m scripts.replay_chart_v3 --cost-pct 0.2`.
Machine-readable output: `data/v3_replay_report.json`.

The cache contains 40,320 closed one-minute candles for BTCUSDT, ETHUSDT,
SOLUSDT, and XRPUSDT from 2026-05-24 13:43 through 2026-05-31 13:42 UTC.
Replay reveals candles chronologically. V3 builds its own completed higher
timeframes from those 1m candles and receives no future HTF cache rows.
All 5,942 v3 WATCH/candidate/confirmed observations passed the check that
their plan preceded the event candle and their observation time did not lie
in the future.

| Measure | Context v2 | Chart v3 |
|---|---:|---:|
| WATCH | 2,072 | 3,715 |
| Candidates | 1,104 | 2,049 |
| Confirmed | 491 | 178 |
| NO_TRADE | 0 explicit | 1,292 |
| 5-candle evaluated | 490 | 178 |
| 5-candle mean gross | +0.0054% | +0.0165% |
| 5-candle mean net at 0.2 percentage-point cost | **−0.1946%** | **−0.1835%** |
| 15-candle evaluated | 490 | 178 |
| 15-candle mean gross | +0.0106% | −0.0060% |
| 15-candle mean net at 0.2 percentage-point cost | **−0.1894%** | **−0.2060%** |

The cost value is an illustrative total round-trip fee, spread, and slippage
assumption. It is subtracted from a hypothetical next-candle-open entry with
an OHLC structural stop. Neither side is executable P&L. At five candles,
v3's gross advantage over v2 is only about 0.011 percentage points; at 15
candles it reverses. Every symbol's mean net result is negative at both
horizons. The zero-return no-signal baseline therefore beats both versions
on this sample under that cost assumption.

V3 abstained for reasons the old engine did not record: 708 candidates lacked
room after the risk-and-cost gate, 456 had no positive room to the next zone,
104 had no known next opposing zone, 16 were deep sweeps that reclaimed, and
8 crossed support and resistance in the same candle. These are useful
diagnostics, not proof that the remaining 178 decisions are good trades.

The replay has no historical OI, positioning ratios, depth changes, spread,
funding, or actual fills. The current cache is one seven-day period and four
symbols; it is neither a held-out test nor a portfolio simulation. Chart-image
vision and human discretionary interpretation are not implemented. Keep v3
in research mode and keep automated trade/Telegram publication disabled.

## Code and live data checks

`python -m pytest tests/unit -q --basetemp data/pytest-tmp/v3-complete`:
240 passed, 1 skipped. Python compilation and `node --check` for the dashboard
passed. A read-only live Binance USD-M BTCUSDT smoke check returned 19 closed
bars on each of 15m/1h/4h, plus `ok` for current/historical OI, all-account
ratio, taker flow, basis, latest/settled funding, depth, and the derived OI to
market-cap ratio. Top-trader account and position ratios returned
`api_key_required` because no Binance API key was configured. This confirms
data availability at one moment; it does not validate signal quality or
continuous WebSocket operation.
