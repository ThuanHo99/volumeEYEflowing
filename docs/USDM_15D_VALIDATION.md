# Binance USD-M: 15-day, all-symbol, 1m execution replay

Frozen run: `data/usdm_15d_20261025/`. Source code:
`scripts/usdm_15d_data.py` and `scripts/replay_usdm_15d.py`.
Machine-readable results: `report.json`, `policy_selection.json`,
`phase1_results.json`, and `phase2_results.json` in the run directory.

## Scope and data

- Scored interval: **2026-10-10 15:43 UTC through 2026-10-25 15:43 UTC**, end
  exclusive. The preceding two days warm the engines. The window was fixed
  with Binance server time before collection.
- Universe: **798 USD-M contracts** returned by exchangeInfo: 795 trading at
  the initial freeze and three which settled inside the scored window
  (EPICUSDT, MITOUSDT, PTBUSDT). This includes USDT, USDC, other USD-M quote
  assets, quarterly contracts, and Binance's TradFi perps. Contracts removed
  from exchangeInfo before collection may be absent.
- **13,475** completed-day 1m ZIP packages and **795** current-day REST
  portions were cached. Archive ZIPs passed CRC validation. The replay parsed
  **17,132,458** scored one-minute candles; 786 symbols had all 21,600 minutes,
  and 12 were listed or settled during the window. No input source days,
  conflicting duplicate candles, or internal one-minute intervals were missing
  among included symbols. A zero-volume candle is not proof of an executable
  trade.
- Binance documents USD-M daily kline archives as being derived from
  `/fapi/v1/klines`; the archive for an entire UTC day normally appears the
  next day. See [Binance Public Data](https://github.com/binance/binance-public-data/blob/master/README.md).

## Prospective design

The first ten scored days (ending **2026-10-20 15:43 UTC**) select one global
execution policy for each engine from a fixed 27-point grid: structural-stop
multipliers `1, 1.25, 1.5`; targets `1, 1.5, 2R`; maximum holds `15, 30, 60`
minutes. The last five days are held out. The fixed reference policy is
`1 × stop, 1.5R target, 30-minute hold` for both engines. Selection maximizes
the pooled training mean net percentage per evaluated signal. It does not use
holdout outcomes.

Every signal is made from candles already closed. The simulated entry is the
**next 1m open**, provided the next minute is contiguous and has positive
volume. Its structural invalidation was known before entry, and an available
opposing zone caps the target. Stops and targets are rounded to the exchange
tick using decimal arithmetic. Each subsequent 1m
bar is tested in chronological order. If TP and SL fall within the same minute,
the **SL is assumed first** (241 v2 and 18 v3 selected-policy holdout cases).
An adverse stop gap fills at the worse open; a favorable target gap receives
only the target. A zero-volume entry is skipped, and an exit requiring a
zero-volume candle is censored. Signals crossing a data gap or period boundary
are censored. A **0.20 percentage-point** total round-trip fee, spread, and
slippage assumption is deducted per evaluated signal.

This is a conservative **1m OHLC approximation**, not exact historical
execution. A minute with both TP and SL cannot reveal which traded first;
trade-level data would be needed for that. The next open is not a guaranteed
fill, particularly for thin contracts. There is no order size, funding,
liquidation, or portfolio overlap model. The historical replay has kline taker
buy volume but no synchronized historical order-book depth, OI, trader ratios,
basis, or funding inputs. V2's same-weekday/hour seasonal baseline is not fully
mature for much of this run because only two warmup days were loaded; its
rolling fallback is used where needed.

## Held-out results

All 798 contracts were eligible; none had a phase error or missing source day.
The table uses **mean percent return per evaluated signal**, not compounded
account P&L. Net means subtract 0.20 percentage points from gross means.

| Engine and policy | Evaluated | Mean gross | Mean net | Net-positive fraction | TP / SL / timeout |
|---|---:|---:|---:|---:|---:|
| V2, train-selected `1.5 × stop, 2R, 60m` | 41,857 | −0.0255% | **−0.2255%** | 33.8% | 14,377 / 25,032 / 2,448 |
| V3, train-selected `1 × stop, 2R, 15m` | 24,336 | −0.0017% | **−0.2017%** | 27.7% | 2,165 / 6,975 / 15,196 |
| V2, fixed reference `1 × stop, 1.5R, 30m` | 41,223 | −0.0151% | **−0.2151%** | 31.2% | 15,989 / 23,521 / 1,713 |
| V3, fixed reference `1 × stop, 1.5R, 30m` | 24,428 | −0.0079% | **−0.2079%** | 33.2% | 5,482 / 9,640 / 9,306 |

V3 generated fewer confirmed signals overall (77,794 versus v2's 129,382)
and has a **0.0238 percentage-point** better selected-policy holdout mean per
evaluated signal. Both selected policies had negative gross returns, even
before the assumed execution cost. The no-trade baseline is zero and beats
both on this sample. The selection step also failed to improve v2's holdout
result relative to the fixed reference policy.

Broad segments show the same pattern:

| Contract group | Symbols | V2 selected: count / mean net | V3 selected: count / mean net |
|---|---:|---:|---:|
| Perpetual, USDT | 526 | 31,224 / −0.2276% | 18,776 / −0.2008% |
| Perpetual, USDC | 38 | 2,433 / −0.2447% | 1,208 / −0.2099% |
| TradFi perpetual, USDT | 224 | 8,012 / −0.2115% | 4,271 / −0.2026% |

At least 30 evaluated holdout signals occurred for 415 v2 symbols and 370 v3
symbols. Only 8 and 10 of those symbols, respectively, had a positive mean
after the assumed cost. Signals across symbols and minutes overlap, so these
counts are diagnostic rather than independent samples.

## Decision

The price-first v3 planner is more selective and loses less per hypothetical
signal here, but this replay does **not** show a tradeable edge. Keep it in
research/shadow mode. Do not treat the selected TP/SL pair as a profitable
configuration or enable automatic orders on the strength of this result.
The next meaningful validation would need point-in-time order-book/spread and
funding data, a stated order size, and additional forward periods. Avoid
selecting a new policy on these five holdout days and then reporting the same
days as an independent test.

## Reproduction and checks

```powershell
python -m scripts.usdm_15d_data --run-dir data/usdm_15d_20261025
python -m scripts.replay_usdm_15d --run-dir data/usdm_15d_20261025 --workers 10 --resume-signals --recompute-outcomes
python -m pytest tests/unit -q --basetemp data/pytest-tmp/usdm-complete
```

The complete unit suite passed: **248 passed, 1 skipped**. Compilation of the
new acquisition/replay scripts and modified chart modules passed. The replay
includes targeted tests for a TP and SL touched in the same minute, an adverse
stop gap, a pre-entry target cap, a missing minute, a period boundary, a
zero-volume exit, exact-tick stop, malformed candles, and a settled contract.
