"""Tests for signal_consumer error isolation in main.py.

Verifies that one failing signal check does not block other checks from running (STAB-03).
"""

import asyncio
import pytest
from unittest.mock import AsyncMock

from tests.factories import make_snap


class TestSignalConsumerIsolation:
    """Verify that one failing signal check does not block others."""

    @pytest.mark.asyncio
    async def test_one_check_fails_others_still_run(self, mocker, queues):
        """When check_rsi_extremes raises, check_volume_spike still executes."""
        from core.state_manager import SymbolStateManager
        signal_q, _, ui_q = queues

        state_mgr = SymbolStateManager(['15m', '1h'])
        # Feed some candles to warm up the state
        for i in range(20):
            for tf in ['15m', '1h']:
                state_mgr.get_state('BTC/USDT:USDT').process_candle({
                    'symbol': 'BTC/USDT:USDT', 'tf': tf, 'start_ts': 1700000000000 + i * 60000,
                    'open': 50000 + i * 10, 'high': 50100 + i * 10, 'low': 49900 + i * 10,
                    'close': 50050 + i * 10, 'volume': 100, 'is_closed': True,
                })

        snapshot = make_snap(tf='1h', close=50050, rsi14=55.0, ema200=49000, volume=100.0)
        snapshot.symbol = 'BTC/USDT:USDT'
        await signal_q.put(snapshot)

        mocker.patch('main.check_rsi_extremes', side_effect=ValueError("test error"))
        mocker.patch('main.CORE_LEGACY_ALERTS_ENABLED', True)
        vol_spike_mock = mocker.patch('main.check_volume_spike', return_value=None)
        ema_touch_mock = mocker.patch('main.check_ema_touch', return_value=None)
        rsi_div_mock = mocker.patch('main.check_rsi_divergence', return_value=None)
        confluence_mock = mocker.patch('main.check_confluence', return_value=None)

        notifier_mock = AsyncMock()
        notifier_mock._last_sent = {}

        from main import signal_consumer
        from core.signals import SignalCooldownManager

        cooldown = SignalCooldownManager()

        task = asyncio.create_task(
            signal_consumer(signal_q, ui_q, state_mgr, notifier_mock, cooldown)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        assert vol_spike_mock.called, "check_volume_spike should have been called despite check_rsi_extremes failing"
        assert ema_touch_mock.called, "check_ema_touch should have been called"
        assert confluence_mock.called, "check_confluence should have been called"

    @pytest.mark.asyncio
    async def test_all_checks_fail_pipeline_continues(self, mocker, queues):
        """When ALL checks raise, signal_consumer continues processing next candle."""
        from core.state_manager import SymbolStateManager
        signal_q, _, ui_q = queues

        state_mgr = SymbolStateManager(['15m', '1h'])
        for i in range(20):
            for tf in ['15m', '1h']:
                state_mgr.get_state('BTC/USDT:USDT').process_candle({
                    'symbol': 'BTC/USDT:USDT', 'tf': tf, 'start_ts': 1700000000000 + i * 60000,
                    'open': 50000 + i * 10, 'high': 50100 + i * 10, 'low': 49900 + i * 10,
                    'close': 50050 + i * 10, 'volume': 100, 'is_closed': True,
                })

        snapshot = make_snap(tf='15m', close=50050, rsi14=55.0, ema200=49000, volume=100.0)
        snapshot.symbol = 'BTC/USDT:USDT'
        await signal_q.put(snapshot)

        mocker.patch('main.check_rsi_extremes', side_effect=ValueError("all fail"))
        mocker.patch('main.CORE_LEGACY_ALERTS_ENABLED', True)
        mocker.patch('main.check_volume_spike', side_effect=ValueError("all fail"))
        mocker.patch('main.check_ema_touch', side_effect=ValueError("all fail"))
        mocker.patch('main.check_rsi_divergence', side_effect=ValueError("all fail"))
        mocker.patch('main.check_confluence', side_effect=ValueError("all fail"))

        notifier_mock = AsyncMock()
        notifier_mock._last_sent = {}

        from main import signal_consumer
        from core.signals import SignalCooldownManager

        cooldown = SignalCooldownManager()

        task = asyncio.create_task(
            signal_consumer(signal_q, ui_q, state_mgr, notifier_mock, cooldown)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        assert signal_q.empty(), "Queue should have been consumed despite all checks failing"
