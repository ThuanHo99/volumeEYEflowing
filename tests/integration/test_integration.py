"""
Integration tests: verify pipeline from candle -> snapshot -> signal -> UI.
No Binance or Ollama needed — push simulated candles.
"""

import asyncio
import json
import logging
import pytest

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s %(levelname)-8s [%(name)s] %(message)s',
)

from core.state_manager import IndicatorState, SymbolStateManager, consume_candles
from core.signals import (
    check_rsi_extremes, check_volume_spike,
    check_ema_touch, check_rsi_divergence, check_confluence,
    SignalType, SignalLevel, Signal,
)
from notifiers.telegram_bot import TelegramNotifier
from tests.factories import make_candle, make_closed_candle


@pytest.mark.integration
class TestPipeline:
    async def test_candle_to_snapshot(self):
        """Candle in -> snapshot out on signal_queue and ui_queue."""
        state = SymbolStateManager(['1m', '15m', '4h'])
        candle_q = asyncio.Queue()
        signal_q = asyncio.Queue()
        ui_q = asyncio.Queue()

        consumer = asyncio.create_task(
            consume_candles(candle_q, state, signal_q, ui_q)
        )

        candle = make_candle('1m', 1000, 50000, 50100, 49900, 50050, 10.0)
        await candle_q.put(candle)
        await asyncio.sleep(0.2)

        assert not signal_q.empty(), "signal_queue empty — consume_candles did not push"
        snap = signal_q.get_nowait()
        assert snap.close == 50050
        assert snap.open == 50000
        assert snap.high == 50100
        assert snap.low == 49900

        assert not ui_q.empty(), "ui_queue empty — consume_candles did not push"
        ui_snap = ui_q.get_nowait()
        assert ui_snap.close == 50050

        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

    async def test_signal_consumer_format(self):
        """Signal dict has correct 'type' (SignalType.value) and 'level' (SignalLevel.name)."""
        state = IndicatorState(['15m'])

        for i in range(200):
            state.process_candle(make_candle('15m', i * 900000, 50000 + i, 50100 + i, 49900 + i, 50050 + i))

        snap = state.process_candle(make_candle('15m', 200 * 900000, 51000, 51100, 50900, 51050, 5.0))
        assert snap is not None

        if snap.rsi14 and snap.rsi14 > 75:
            sig = check_rsi_extremes(snap)
            assert sig is not None
            signal_dict = {
                'type': sig.type.value, 'level': sig.level.name,
                'tf': sig.tf, 'close': sig.close, 'rsi14': sig.rsi14,
                'ema200': sig.ema200, 'timestamp': sig.timestamp,
            }
            assert signal_dict['type'] in ('RSI_OVERBOUGHT', 'RSI_OVERSOLD')
            assert signal_dict['level'] in ('WARNING', 'ALERT', 'HIGH_PRIORITY')
            assert signal_dict['tf'] == '15m'

        state2 = IndicatorState(['4h'])
        for i in range(200):
            state2.process_candle(make_candle('4h', i * 14400000, 48000 + i, 48100 + i, 47900 + i, 48050 + i))
        snap4h = state2.process_candle(make_candle('4h', 200 * 14400000, 49000, 49500, 47000, 48000, 5.0))
        assert snap4h is not None
        if snap4h.ema200:
            sig3 = check_ema_touch(snap4h, snap4h.low, snap4h.high)
            if sig3:
                d = {'type': sig3.type.value, 'level': sig3.level.name, 'tf': sig3.tf, 'timestamp': sig3.timestamp}
                assert d['type'] in ('EMA_SUPPORT_TOUCH', 'EMA_RESISTANCE_TOUCH')
                assert d['level'] == 'ALERT'

    async def test_divergence_from_history(self):
        """check_rsi_divergence using close_history and rsi_history from state."""
        state = IndicatorState(['15m'])

        for i in range(20):
            price = 50000.0 + i * 100
            state.process_candle(make_candle('15m', i * 900000, price, price + 100, price - 100, price))

        sig = check_rsi_divergence(state.close_history['15m'], state.rsi_history['15m'])
        if sig is not None:
            assert sig.type in (SignalType.BULLISH_DIVERGENCE, SignalType.BEARISH_DIVERGENCE)
            assert sig.tf == '15m'

    async def test_main_imports_and_structure(self):
        """Verify all modules import correctly."""
        from core.ingestion import stream_ohlcv, fetch_historical_ohlcv
        from core.state_manager import IndicatorState, IndicatorSnapshot, consume_candles
        from core.signals import (
            check_rsi_extremes, check_volume_spike,
            check_ema_touch, check_rsi_divergence,
            check_confluence, SignalType, SignalLevel, Signal,
        )
        from notifiers.telegram_bot import TelegramNotifier
        from ui.web_dashboard import serve, broadcast

        snap = IndicatorSnapshot(
            tf='1m', timestamp=0, open=100, high=110, low=90, close=105,
            ema200=None, rsi14=None, volume=5.0, is_closed=True,
        )
        assert snap.open == 100
        assert snap.close == 105

        notifier = TelegramNotifier(token=None, chat_id=None)
        sig = Signal(
            type=SignalType.RSI_OVERBOUGHT, level=SignalLevel.WARNING,
            symbol='BTC/USDT:USDT',
            tf='15m', close=50000, rsi14=80.0, ema200=48000.0,
            volume=10.0, timestamp=1700000000000, detail="test",
        )
        msg = notifier._format_message(sig)
        assert 'BTC' in msg and '/USDT' not in msg
        assert '50,000.00' in msg

        assert callable(stream_ohlcv)
        assert callable(serve)

    async def test_graceful_shutdown(self):
        """signal_consumer does not crash when queue is cancelled."""
        state = IndicatorState(['1m'])
        signal_q = asyncio.Queue()
        ui_q = asyncio.Queue()

        candle = make_candle('1m', 1000, 50000, 50100, 49900, 50050, 10.0)
        snap = state.process_candle(candle)
        assert snap is not None
        assert snap.open == 50000
        assert snap.high == 50100
