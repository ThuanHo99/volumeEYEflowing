"""Read-only, timestamped Binance USD-M derivatives observations.

These observations describe positioning and liquidity around a price plan.
They do not vote on direction or turn a context candidate into a trade.
"""

import asyncio
import math
import time
from collections import deque
from typing import Any, Awaitable, Callable

import aiohttp

from . import config

Fetch = Callable[[str, dict[str, Any], bool], Awaitable[Any]]
PERIOD_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
             "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000}

# (path, cache TTL milliseconds, requires API key)
ENDPOINTS = {
    "open_interest": ("/fapi/v1/openInterest", 60_000, False),
    "open_interest_history": ("/futures/data/openInterestHist", 300_000, False),
    "top_trader_accounts": ("/futures/data/topLongShortAccountRatio", 300_000, True),
    "top_trader_positions": ("/futures/data/topLongShortPositionRatio", 300_000, True),
    "long_short_accounts": ("/futures/data/globalLongShortAccountRatio", 300_000, False),
    "taker_buy_sell": ("/futures/data/takerlongshortRatio", 300_000, False),
    "basis": ("/futures/data/basis", 300_000, False),
    "funding": ("/fapi/v1/premiumIndex", 30_000, False),
    "funding_history": ("/fapi/v1/fundingRate", 300_000, False),
    "order_book": ("/fapi/v1/depth", 10_000, False),
}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _latest_complete(rows: Any, now_ms: int, period_ms: int,
                     *, start_timestamp: bool = False) -> dict | None:
    if not isinstance(rows, list):
        return None
    complete = [r for r in rows if isinstance(r, dict) and
                isinstance(r.get("timestamp"), int) and
                r["timestamp"] + (period_ms if start_timestamp else 0) <= now_ms]
    return max(complete, key=lambda r: r["timestamp"]) if complete else None


def parse_observation(name: str, payload: Any, now_ms: int,
                      period_ms: int) -> tuple[dict | None, int | None]:
    """Return compact metric values and the exchange observation time."""
    if name in {"open_interest_history", "top_trader_accounts",
                "top_trader_positions", "long_short_accounts", "taker_buy_sell", "basis"}:
        starts = name in {"taker_buy_sell", "basis"}
        row = _latest_complete(payload, now_ms, period_ms, start_timestamp=starts)
        if row is None:
            return None, None
        observed = row["timestamp"] + (period_ms if starts else 0)
        older = [r for r in payload if isinstance(r, dict) and
                 isinstance(r.get("timestamp"), int) and r["timestamp"] < row["timestamp"]]
        previous = max(older, key=lambda r: r["timestamp"]) if older else None
    elif name == "funding_history":
        if not isinstance(payload, list):
            return None, None
        rows = [r for r in payload if isinstance(r, dict) and
                isinstance(r.get("fundingTime"), int) and r["fundingTime"] <= now_ms]
        if not rows:
            return None, None
        row = max(rows, key=lambda r: r["fundingTime"])
        observed = row["fundingTime"]
    elif isinstance(payload, dict):
        row = payload
        observed = row.get("time") or row.get("T") or now_ms
        if not isinstance(observed, int) or observed > now_ms + 10_000:
            return None, None
    else:
        return None, None

    if name == "open_interest":
        value = {"base_units": _number(row.get("openInterest"))}
    elif name == "open_interest_history":
        value = {"base_units": _number(row.get("sumOpenInterest")),
                 "notional_usd_approx": _number(row.get("sumOpenInterestValue")),
                 "cmc_circulating_supply": _number(row.get("CMCCirculatingSupply"))}
        prev = _number(previous.get("sumOpenInterestValue")) if previous else None
        if prev and value["notional_usd_approx"] is not None:
            value["notional_change_pct"] = (value["notional_usd_approx"] / prev - 1) * 100
    elif name in {"top_trader_accounts", "top_trader_positions", "long_short_accounts"}:
        long_key, short_key = (("longPosition", "shortPosition") if name == "top_trader_positions"
                               else ("longAccount", "shortAccount"))
        value = {"long_short_ratio": _number(row.get("longShortRatio")),
                 "long_share": _number(row.get(long_key)), "short_share": _number(row.get(short_key))}
        prev = _number(previous.get("longShortRatio")) if previous else None
        if prev and value["long_short_ratio"] is not None:
            value["ratio_change_pct"] = (value["long_short_ratio"] / prev - 1) * 100
    elif name == "taker_buy_sell":
        buy, sell = _number(row.get("buyVol")), _number(row.get("sellVol"))
        value = {"buy_volume": buy, "sell_volume": sell,
                 "buy_sell_ratio": _number(row.get("buySellRatio")),
                 "delta": buy - sell if buy is not None and sell is not None else None}
        value["delta_previous"] = (lambda b, s: b - s if b is not None and s is not None else None)(
            _number(previous.get("buyVol")), _number(previous.get("sellVol"))) if previous else None
    elif name == "basis":
        value = {"index_price": _number(row.get("indexPrice")),
                 "futures_price": _number(row.get("futuresPrice")),
                 "basis": _number(row.get("basis")), "basis_rate": _number(row.get("basisRate")),
                 "contract_type": row.get("contractType")}
    elif name == "funding":
        value = {"mark_price": _number(row.get("markPrice")),
                 "index_price": _number(row.get("indexPrice")),
                 "latest_funding_rate": _number(row.get("lastFundingRate")),
                 "next_funding_time": row.get("nextFundingTime")}
    elif name == "funding_history":
        value = {"settled_funding_rate": _number(row.get("fundingRate")),
                 "funding_time": observed}
    elif name == "order_book":
        bids, asks = row.get("bids"), row.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            return None, None
        try:
            bid_price, ask_price = float(bids[0][0]), float(asks[0][0])
            bid_notional = sum(float(p) * float(q) for p, q in bids)
            ask_notional = sum(float(p) * float(q) for p, q in asks)
        except (TypeError, ValueError, IndexError):
            return None, None
        if bid_price <= 0 or ask_price <= bid_price or bid_notional + ask_notional <= 0:
            return None, None
        value = {"best_bid": bid_price, "best_ask": ask_price,
                 "spread_bps": (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 10_000,
                 "bid_notional": bid_notional, "ask_notional": ask_notional,
                 "top20_imbalance": (bid_notional - ask_notional) / (bid_notional + ask_notional),
                 "last_update_id": row.get("lastUpdateId"), "levels": min(len(bids), len(asks))}
    else:
        return None, None
    if all(v is None for v in value.values()):
        return None, None
    return value, observed


def explain_context(snapshot: dict | None, *, scenario: str,
                    event_delta_pct: float | None) -> list[str]:
    """Evidence notes, explicitly non-predictive and restricted to fresh fields."""
    if not snapshot:
        return ["Derivatives context unavailable at confirmation."]
    metrics = snapshot.get("metrics", {})
    def get(name: str, key: str) -> float | None:
        row = metrics.get(name, {})
        return row.get("value", {}).get(key) if row.get("status") == "ok" else None

    notes = []
    oi_change = get("open_interest_history", "notional_change_pct")
    if oi_change is not None:
        notes.append(f"Binance OI notional changed {oi_change:+.2f}% from the previous available completed {snapshot['period']} record; this does not identify the winning side.")
    account = get("top_trader_accounts", "long_short_ratio")
    position = get("top_trader_positions", "long_short_ratio")
    global_ratio = get("long_short_accounts", "long_short_ratio")
    if account is not None and position is not None:
        notes.append(f"Top-trader L/S: accounts {account:.2f}, positions {position:.2f}; compare account count with position size, not a directional vote.")
    if global_ratio is not None:
        notes.append(f"All-account L/S {global_ratio:.2f} on Binance; it is venue-specific positioning.")
    buy_sell = get("taker_buy_sell", "buy_sell_ratio")
    if buy_sell is not None:
        notes.append(f"Taker buy/sell volume ratio {buy_sell:.2f} over the latest completed {snapshot['period']} period; compare with subsequent price acceptance.")
    if scenario == "FAILURE" and event_delta_pct is not None and abs(event_delta_pct) >= 30:
        notes.append(f"Event taker delta {event_delta_pct:+.1f}% versus failed price acceptance: effort/result divergence, not proof of absorption.")
    basis = get("basis", "basis_rate")
    funding = get("funding", "latest_funding_rate")
    if basis is not None or funding is not None:
        b = "unavailable" if basis is None else f"{basis:+.5f}"
        f = "unavailable" if funding is None else f"{funding:+.6f}"
        notes.append(f"Perpetual basis rate {b}; latest funding rate {f}. They describe carry/crowding, not the next move.")
    spread = get("order_book", "spread_bps")
    imbalance = get("order_book", "top20_imbalance")
    if spread is not None and imbalance is not None:
        notes.append(f"Top-20 order book snapshot: spread {spread:.2f} bps, displayed imbalance {imbalance:+.2f}; orders can cancel and this is not executed flow.")
    ratio = get("oi_to_market_cap", "ratio_approx")
    if ratio is not None:
        notes.append(f"Binance OI / estimated market cap ≈ {ratio:.3f}; market cap uses CMC circulating supply supplied by Binance × index price.")
    return notes or ["Derivative fields are missing or stale; no positioning inference was made."]


class DerivativesContext:
    """Bounded on-demand cache; failures are visible and never become zeros."""

    def __init__(self, fetch: Fetch | None = None):
        if config.DERIVATIVES_PERIOD not in PERIOD_MS:
            raise ValueError("VS_DERIVATIVES_PERIOD must be 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h or 1d")
        self._fetch_override = fetch
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, dict[str, dict]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._requests: deque[int] = deque()
        self._semaphore = asyncio.Semaphore(4)

    async def close(self) -> None:
        for task in self._inflight.values():
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        self._inflight.clear()
        if self._session:
            await self._session.close()
            self._session = None

    async def _fetch(self, path: str, params: dict, keyed: bool) -> Any:
        if self._fetch_override:
            return await self._fetch_override(path, params, keyed)
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY} if keyed else None
        async with self._session.get(config.BINANCE_REST_URL.rstrip("/") + path,
                                     params=params, headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(f"Binance HTTP {response.status}")
            return await response.json()

    def snapshot(self, symbol: str, now_ms: int | None = None) -> dict:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cached = self._cache.get(symbol, {})
        metrics = {}
        for name, (_, ttl, keyed) in ENDPOINTS.items():
            item = dict(cached.get(name, {"status": "unavailable", "value": None,
                                          "observed_at": None, "fetched_at": None}))
            if keyed and not config.BINANCE_API_KEY:
                item = {"status": "api_key_required", "value": None,
                        "observed_at": None, "fetched_at": None}
            elif item["status"] in {"ok", "stale"}:
                max_observation_age = (36 * 3_600_000 if name == "funding_history" else
                                       2 * PERIOD_MS[config.DERIVATIVES_PERIOD] if name in {
                                           "open_interest_history", "top_trader_accounts",
                                           "top_trader_positions", "long_short_accounts",
                                           "taker_buy_sell", "basis"} else 2 * ttl)
                if (now_ms - item["fetched_at"] > ttl or
                        item["observed_at"] is None or
                        now_ms - item["observed_at"] > max_observation_age):
                    item["status"] = "stale"
            item["source"] = "Binance USD-M"
            metrics[name] = item

        oi, funding = metrics["open_interest_history"], metrics["funding"]
        ratio = None
        if oi["status"] == funding["status"] == "ok":
            value, mark = oi["value"], funding["value"]
            notional, supply, index = (value.get("notional_usd_approx"),
                                       value.get("cmc_circulating_supply"), mark.get("index_price"))
            if notional is not None and supply and index and supply > 0 and index > 0:
                ratio = notional / (supply * index)
        metrics["oi_to_market_cap"] = {
            "status": "ok" if ratio is not None else "unavailable",
            "value": {"ratio_approx": ratio} if ratio is not None else None,
            "observed_at": min(oi["observed_at"], funding["observed_at"]) if ratio is not None else None,
            "fetched_at": now_ms if ratio is not None else None,
            "source": "Binance OI notional / (CMC circulating supply via Binance × Binance index price)",
        }
        return {"symbol": symbol, "period": config.DERIVATIVES_PERIOD,
                "as_of": now_ms, "metrics": metrics,
                "interpretation": "Context only; no metric independently predicts direction."}

    async def refresh(self, symbol: str) -> dict:
        if not symbol.endswith("USDT") or not symbol.isalnum() or len(symbol) > 30:
            raise ValueError("Expected a Binance USD-M USDT symbol")
        task = self._inflight.get(symbol)
        if task is None:
            task = asyncio.create_task(self._refresh_symbol(symbol))
            self._inflight[symbol] = task
            task.add_done_callback(lambda finished: self._inflight.pop(symbol, None)
                                   if self._inflight.get(symbol) is finished else None)
        await task
        return self.snapshot(symbol)

    async def _refresh_symbol(self, symbol: str) -> None:
        now_ms = int(time.time() * 1000)
        cache = self._cache.setdefault(symbol, {})
        pending = []
        for name, (path, ttl, keyed) in ENDPOINTS.items():
            if keyed and not config.BINANCE_API_KEY:
                continue
            old = cache.get(name)
            if old and now_ms - old["fetched_at"] <= ttl:
                continue
            while self._requests and now_ms - self._requests[0] >= 300_000:
                self._requests.popleft()
            if len(self._requests) >= max(1, config.DERIVATIVES_MAX_REQUESTS_5M):
                cache[name] = {"status": "rate_limited", "value": None,
                               "observed_at": None, "fetched_at": now_ms}
                continue
            self._requests.append(now_ms)
            params: dict[str, Any] = {"symbol": symbol}
            if name in {"open_interest_history", "top_trader_accounts", "top_trader_positions",
                        "long_short_accounts", "taker_buy_sell", "basis"}:
                params.update(period=config.DERIVATIVES_PERIOD, limit=3)
            if name == "basis":
                params = {"pair": symbol, "contractType": "PERPETUAL",
                          "period": config.DERIVATIVES_PERIOD, "limit": 3}
            elif name == "funding_history":
                params["limit"] = 1
            elif name == "order_book":
                params["limit"] = 20
            pending.append(self._fetch_one(symbol, name, path, params, keyed, now_ms))
        if pending:
            await asyncio.gather(*pending)
        # Bound retained state to actively inspected symbols.
        if len(self._cache) > 200:
            oldest = min(self._cache, key=lambda s: max(
                (v["fetched_at"] or 0 for v in self._cache[s].values()), default=0))
            if oldest != symbol:
                self._cache.pop(oldest, None)

    async def _fetch_one(self, symbol: str, name: str, path: str, params: dict,
                         keyed: bool, now_ms: int) -> None:
        try:
            async with self._semaphore:
                raw = await self._fetch(path, params, keyed)
            value, observed = parse_observation(name, raw, now_ms,
                                                PERIOD_MS[config.DERIVATIVES_PERIOD])
            status = "ok" if value is not None else "unavailable"
            error = None
        except Exception as exc:
            old = self._cache.get(symbol, {}).get(name)
            value = old.get("value") if old else None
            observed = old.get("observed_at") if old else None
            status = "stale" if value is not None else "error"
            error = f"{type(exc).__name__}: {str(exc)[:100]}"
        self._cache.setdefault(symbol, {})[name] = {
            "status": status, "value": value,
            "observed_at": observed, "fetched_at": now_ms,
            **({"error": error} if error else {}),
        }
