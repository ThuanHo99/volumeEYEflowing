import json
from core.state_manager import IndicatorState

def main():
    # Load cache
    with open('data/ohlcv_cache.json', 'r') as f:
        cache = json.load(f)

    state = IndicatorState()
    candles = cache['BTC/USDT:USDT']['1m']
    print('Total cached candles:', len(candles))

    # Process all cached candles except the last 5
    for c in candles[:-5]:
        state.process_candle(c)

    print('EMA200 before real-time simulation:', state.ema['1m'][-1])
    print('RSI14 before real-time simulation:', state.rsi['1m'][-1])

    # Simulate processing the last 5 candles as real-time ticks
    for c in candles[-5:]:
        # First process as open tick (is_closed = False)
        c_open = dict(c, is_closed=False)
        state.process_candle(c_open)
        snap_open = state.get_snapshot(c['tf'], c_open)
        
        # Then process as closed tick (is_closed = True)
        snap_close = state.process_candle(c)
        
        print(f"TS={c['start_ts']} Close={c['close']} Open_EMA={snap_open.ema200} Close_EMA={snap_close.ema200}")

if __name__ == '__main__':
    main()
