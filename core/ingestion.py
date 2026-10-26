import asyncio
import aiohttp
import ccxt
import ccxt.pro as ccxtpro
import logging
import time

from core.config import GRACE_PERIOD_MS, HISTORICAL_CANDLES, WARMUP_CANDLES, SYMBOLS, SYMBOL, EXCHANGE_ID as EXCHANGE, TIMEFRAMES, FETCH_TIMEOUT_SECONDS

_TF_MS: dict[str, int] = {'1m': 60000, '5m': 300000, '15m': 900000, '1h': 3600000, '4h': 14400000}

logger = logging.getLogger(__name__)


def fetch_historical_ohlcv(symbol: str | None = None, timeframes: list[str] | None = None, limits: dict[str, int] | None = None) -> dict[str, list[dict]]:
    """
    Fetch historical OHLCV data from Binance REST API for each timeframe.
    Returns dict: { '1m': [...candles...], '5m': [...], ... }
    Each candle format matches IndicatorState.process_candle input.

    Args:
        symbol: Trading pair (e.g. 'BTC/USDT:USDT'). Defaults to first in SYMBOLS.
        timeframes: List of timeframes to fetch. Defaults to TIMEFRAMES.
        limits: Max candles per timeframe (defaults to HISTORICAL_CANDLES).
                Can pass WARMUP_CANDLES to fetch more for 1m/5m/15m.
    """
    if symbol is None:
        symbol = SYMBOL
    if timeframes is None:
        timeframes = TIMEFRAMES
    result: dict[str, list[dict]] = {}

    def _fetch_single(tf):
        exchange_rest = getattr(ccxt, EXCHANGE)({'enableRateLimit': True})
        target = limits.get(tf) if limits else HISTORICAL_CANDLES
        tf_ms = _TF_MS.get(tf, 60000)
        now_ms = int(time.time() * 1000)
        all_candles: list[dict] = []
        cursor = now_ms - (target * tf_ms)
        remaining = target
        while remaining > 0 and cursor < now_ms:
            limit = min(remaining, 1000)
            try:
                ohlcv = exchange_rest.fetch_ohlcv(symbol, tf, since=cursor, limit=limit)
            except Exception as e:
                logger.warning("Could not fetch historical data for %s: %s", tf, e, exc_info=True)
                break
            if not ohlcv:
                break
            batch = [{'symbol': symbol, 'tf': tf, 'start_ts': c[0], 'open': c[1], 'high': c[2], 'low': c[3], 'close': c[4], 'volume': c[5], 'is_closed': True} for c in ohlcv]
            all_candles.extend(batch)
            remaining -= len(ohlcv)
            if len(ohlcv) < limit:
                break
            cursor = ohlcv[-1][0] + 1
        all_candles.sort(key=lambda c: c['start_ts'])
        seen: set[int] = set()
        deduped: list[dict] = []
        for c in reversed(all_candles):
            if c['start_ts'] not in seen:
                seen.add(c['start_ts'])
                deduped.append(c)
                if len(deduped) >= target:
                    break
        deduped.reverse()
        logger.info(f"Fetched {len(deduped)} historical candles for {symbol} {tf}")
        return tf, deduped

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(timeframes)) as executor:
        futures = {executor.submit(_fetch_single, tf): tf for tf in timeframes}
        done, not_done = concurrent.futures.wait(futures, timeout=FETCH_TIMEOUT_SECONDS)
        for future in done:
            try:
                tf, candles = future.result()
                result[tf] = candles
            except Exception as e:
                logger.error("Fetch failed for %s: %s", futures[future], e, exc_info=True)
        for future in not_done:
            logger.error("Fetch timed out for %s — cancelling", futures[future])
            future.cancel()

    return result


def fetch_prev_day_levels(symbol: str | None = None, exchange_id: str | None = None) -> dict | None:
    """
    Fetch yesterday's High/Low (1d) for Support/Resistance levels.
    Returns: {'high': float, 'low': float, 'start_ts': int} or None on failure.
    """
    if symbol is None:
        symbol = SYMBOL
    if exchange_id is None:
        exchange_id = EXCHANGE
    exchange_rest = getattr(ccxt, exchange_id)({'enableRateLimit': True})
    try:
        ohlcv = exchange_rest.fetch_ohlcv(symbol, '1d', limit=2)
    except Exception as e:
        logger.warning("Could not fetch 1d candles: %s", e, exc_info=True)
        return None
    if not ohlcv or len(ohlcv) < 2:
        logger.warning("Insufficient 1d data (need at least 2 candles)")
        return None
    yesterday = ohlcv[-2]
    return {
        'high': float(yesterday[2]),
        'low': float(yesterday[3]),
        'start_ts': yesterday[0],
    }


def _fetch_gap_candles(tf: str, since_ts: int, symbol: str, exchange_id: str) -> list[dict]:
    """Fetch candles from since_ts+1 to now via REST API to fill reconnection gaps."""
    exchange_rest = getattr(ccxt, exchange_id)({'enableRateLimit': True})
    result: list[dict] = []
    cursor = since_ts + 1
    try:
        while True:
            ohlcv = exchange_rest.fetch_ohlcv(symbol, tf, since=cursor, limit=1000)
            if not ohlcv:
                break
            for c in ohlcv:
                if c[0] > since_ts:
                    result.append({
                        'tf': tf, 'start_ts': c[0],
                        'open': c[1], 'high': c[2], 'low': c[3],
                        'close': c[4], 'volume': c[5], 'is_closed': True,
                    })
            if len(ohlcv) < 1000:
                break
            cursor = ohlcv[-1][0] + 1
    except Exception as e:
        logger.warning("_fetch_gap_candles %s: %s — returning %d candles fetched so far", tf, e, len(result), exc_info=True)
    return result


fetch_gap_candles = _fetch_gap_candles



async def stream_ohlcv(queue: asyncio.Queue, symbol: str | None = None) -> None:
    """Stream OHLCV candles from exchange WebSocket for all timeframes.
    Each candle from the exchange is a complete kline (open/high/low/close/volume),
    no CandleAggregator needed to assemble from trades.

    Args:
        queue: asyncio.Queue for candle output.
        symbol: Trading pair (e.g. 'ETH/USDT:USDT'). Defaults to first in SYMBOLS.
    """
    if symbol is None:
        symbol = SYMBOL
    last_seen: dict[str, int] = {tf: 0 for tf in TIMEFRAMES}
    while True:
        exchange = None
        session = None
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            session = aiohttp.ClientSession(connector=connector)
            exchange = getattr(ccxtpro, EXCHANGE)({
                'enableRateLimit': True,
                'session': session,
            })

            logger.info(f"Loading markets from {EXCHANGE}...")
            await exchange.load_markets()
            logger.info(f"Connecting OHLCV WebSocket to {EXCHANGE} — {symbol}")

            for tf in TIMEFRAMES:
                if last_seen[tf] > 0:
                    gap = await asyncio.to_thread(_fetch_gap_candles, tf, last_seen[tf], symbol, EXCHANGE)
                    pushed = 0
                    for candle in gap:
                        if candle['start_ts'] > last_seen[tf]:
                            try:
                                queue.put_nowait(candle)
                            except asyncio.QueueFull:
                                logger.warning(f"[{symbol}][{tf}] Queue full during gap fill, discarding remaining")
                                break
                            last_seen[tf] = candle['start_ts']
                            pushed += 1
                    if pushed > 0:
                        logger.info(f"[{symbol}][{tf}] Fetched {pushed} missed candles after reconnect")

            async def watch_single(tf: str):
                initialized = False
                _tpl = {'symbol': symbol, 'tf': tf, 'start_ts': 0, 'open': 0.0, 'high': 0.0, 'low': 0.0, 'close': 0.0, 'volume': 0.0, 'is_closed': False}
                while True:
                    ohlcv = await exchange.watch_ohlcv(symbol, tf)
                    if not ohlcv:
                        continue

                    if not initialized:
                        c = ohlcv[-1]
                        last_seen[tf] = c[0]
                        _tpl['start_ts'] = c[0]
                        _tpl['open'] = c[1]
                        _tpl['high'] = c[2]
                        _tpl['low'] = c[3]
                        _tpl['close'] = c[4]
                        _tpl['volume'] = c[5]
                        _tpl['is_closed'] = False
                        if not queue.full():
                            await queue.put(_tpl.copy())
                        logger.info(f"[{symbol}][{tf}] WebSocket ready — current candle ts={c[0]}")
                        initialized = True
                        continue

                    MAX_BATCH_PER_CYCLE = 10
                    pushed = 0
                    for i, c in enumerate(ohlcv):
                        if pushed >= MAX_BATCH_PER_CYCLE:
                            logger.warning(f"[{symbol}][{tf}] Batch limit ({MAX_BATCH_PER_CYCLE}) reached, discarding {len(ohlcv) - i} candles")
                            break
                        ts = c[0]
                        if ts < last_seen[tf]:
                            continue
                        if ts > last_seen[tf]:
                            await asyncio.sleep(GRACE_PERIOD_MS / 1000.0)
                            last_seen[tf] = ts

                        is_closed = (i < len(ohlcv) - 1)
                        _tpl['start_ts'] = ts
                        _tpl['open'] = c[1]
                        _tpl['high'] = c[2]
                        _tpl['low'] = c[3]
                        _tpl['close'] = c[4]
                        _tpl['volume'] = c[5]
                        _tpl['is_closed'] = is_closed
                        candle = _tpl.copy()
                        try:
                            queue.put_nowait(candle)
                        except asyncio.QueueFull:
                            logger.warning(f"[{symbol}][{tf}] Queue full, discarding candle ts={ts}")
                            break
                        pushed += 1
                    if pushed > 0:
                        logger.debug(f"[{symbol}][{tf}] pushed {pushed} candle(s) to queue")

            await asyncio.gather(*[watch_single(tf) for tf in TIMEFRAMES])

        except ccxtpro.NetworkError as e:
            logger.error(f"[{symbol}] WebSocket connection lost: {e} — Retrying in 5 seconds")
            await asyncio.sleep(5)

        except ccxtpro.ExchangeError as e:
            logger.error(f"[{symbol}] Exchange error: {e} — Retrying in 10 seconds")
            await asyncio.sleep(10)

        except Exception as e:
            logger.exception(f"[{symbol}] Unknown error: {e} — Retrying in 10 seconds")
            await asyncio.sleep(10)

        finally:
            if exchange:
                await exchange.close()
            if session:
                await session.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    async def _debug():
        q = asyncio.Queue(maxsize=10000)
        count = 0

        async def printer():
            nonlocal count
            while count < 5:
                try:
                    candle = q.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.1)
                    continue
                count += 1
                state = "DONG" if candle['is_closed'] else "MO"
                print(f"[{state}] tf={candle['tf']} o={candle['open']:.1f} c={candle['close']:.1f}")

        producer_task = asyncio.create_task(stream_ohlcv(q))
        consumer_task = asyncio.create_task(printer())

        await consumer_task
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_debug())
