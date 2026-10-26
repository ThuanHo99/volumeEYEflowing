"""
Shared test data factory functions.
Replaces inline duplicates across test files.
"""


def make_trade(price: float, amount: float, ts_ms: int) -> dict:
    return {'price': price, 'amount': amount, 'timestamp': ts_ms, 'id': str(ts_ms)}


def make_closed_candle(tf: str, close: float, ts: int = 0, volume: float = 1.0) -> dict:
    return {
        'tf': tf, 'start_ts': ts,
        'open': close, 'high': close, 'low': close,
        'close': close, 'volume': volume, 'is_closed': True,
    }


def make_open_candle(tf: str, close: float, volume: float = 1.0, ts: int = 0) -> dict:
    c = make_closed_candle(tf, close, ts=ts, volume=volume)
    c['is_closed'] = False
    return c


def make_candle(tf, start_ts, open_, high, low, close, volume=1.0, is_closed=True) -> dict:
    return {
        'tf': tf, 'start_ts': start_ts,
        'open': open_, 'high': high, 'low': low,
        'close': close, 'volume': volume, 'is_closed': is_closed,
    }


def make_snap(tf, close, rsi14=50.0, ema200=50000.0, volume=1.0):
    from core.state_manager import IndicatorSnapshot
    return IndicatorSnapshot(
        tf=tf, timestamp=0, open=close, high=close,
        low=close, close=close, ema200=ema200,
        rsi14=rsi14, volume=volume, is_closed=True,
    )
