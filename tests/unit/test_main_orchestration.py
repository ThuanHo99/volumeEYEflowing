"""Tests for main.py orchestration: warmup_phase and TaskSupervisor.

Covers:
- warmup_phase with mocked data fetching
- TaskSupervisor lifecycle
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_warmup_phase_feeds_state_and_ui(mocker, queues):
    """warmup_phase must process candles into state and push to UI queue."""
    from core.state_manager import SymbolStateManager
    from core.config import SYMBOLS
    _, _, ui_q = queues

    # Create SymbolStateManager with test symbol
    state_mgr = SymbolStateManager(['1m', '5m'])

    # Mock historical data: 2 closed candles per timeframe
    mock_candles = {}
    for i, tf in enumerate(['1m', '5m']):
        base_ts = 1700000000000 + i * 60000
        mock_candles[tf] = [
            {'symbol': SYMBOLS[0], 'tf': tf, 'start_ts': base_ts, 'open': 50000, 'high': 50100, 'low': 49900,
             'close': 50050, 'volume': 100, 'is_closed': True},
            {'symbol': SYMBOLS[0], 'tf': tf, 'start_ts': base_ts + 60000, 'open': 50050, 'high': 50200, 'low': 50000,
             'close': 50100, 'volume': 150, 'is_closed': True},
        ]

    test_tfs = ['1m', '5m']
    with patch('main.SYMBOLS', ['BTC/USDT:USDT']):
        with patch('main.TIMEFRAMES', test_tfs):
            with patch('main.fetch_historical_ohlcv', return_value=mock_candles):
                with patch('main.fetch_prev_day_levels', return_value={'high': 50500, 'low': 49500, 'start_ts': 1700000000000}):
                    with patch('main.save_ohlcv_cache'):
                        with patch('main.load_ohlcv_cache', return_value={}):
                            with patch('main.mark_ohlcv_dirty'):
                                from main import warmup_phase
                                total, historical = await warmup_phase(state_mgr, ui_q)

    assert total == 4  # 2 candles x 2 TFs
    assert SYMBOLS[0] in historical

    # UI queue should have received each candle as IndicatorSnapshot + status + day_levels
    ui_items = []
    while not ui_q.empty():
        ui_items.append(await ui_q.get())

    snapshots = [m for m in ui_items if not isinstance(m, dict)]
    status_msgs = [m for m in ui_items if isinstance(m, dict) and m.get('type') == 'status']
    day_levels = [m for m in ui_items if isinstance(m, dict) and m.get('type') == 'day_levels']
    assert len(snapshots) == 4  # 2 candles x 2 TFs
    assert len(status_msgs) >= 1
    assert len(day_levels) == 1
    assert day_levels[0]['high'] == 50500
    assert day_levels[0]['low'] == 49500

    # State should have processed candles
    test_state = state_mgr.get_state(SYMBOLS[0])
    assert len(test_state.ema['1m']) >= 2
    assert len(test_state.ema['5m']) >= 2


@pytest.mark.asyncio
async def test_warmup_phase_empty_data(mocker, queues):
    """warmup_phase must handle empty historical data gracefully."""
    from core.state_manager import SymbolStateManager
    _, _, ui_q = queues

    state_mgr = SymbolStateManager(['1m', '5m'])

    test_tfs = ['1m', '5m']
    with patch('main.SYMBOLS', ['BTC/USDT:USDT']):
        with patch('main.TIMEFRAMES', test_tfs):
            with patch('main.fetch_historical_ohlcv', return_value={}):
                with patch('main.fetch_prev_day_levels', return_value=None):
                    with patch('main.save_ohlcv_cache'):
                        with patch('main.load_ohlcv_cache', return_value={}):
                            with patch('main.mark_ohlcv_dirty'):
                                from main import warmup_phase
                                total, historical = await warmup_phase(state_mgr, ui_q)

    assert total == 0
    assert 'BTC/USDT:USDT' in historical
    assert historical['BTC/USDT:USDT'] == {'1m': [], '5m': []}


# ── T1: TaskSupervisor ───────────────────────────────────────────────────

class TestTaskSupervisor:
    """TaskSupervisor creates, monitors, and restarts critical asyncio tasks."""

    async def test_supervisor_creates_task(self, mocker):
        """supervisor.add('test', coro) must create an asyncio task."""
        from main import TaskSupervisor

        mock_logger = mocker.MagicMock()
        supervisor = TaskSupervisor(mock_logger)

        started = asyncio.Event()

        async def dummy_coro():
            started.set()
            await asyncio.Event().wait()  # hang forever

        supervisor.add('test_task', dummy_coro)
        assert 'test_task' in supervisor._tasks
        task = supervisor._tasks['test_task']
        assert isinstance(task, asyncio.Task)
        assert not task.done()

        # Let the coro actually start
        await asyncio.wait_for(started.wait(), timeout=2)

        # Cleanup
        await supervisor.shutdown()

    async def test_supervisor_restarts_on_failure(self, mocker):
        """A crashing task must be restarted by the supervisor."""
        from main import TaskSupervisor

        mock_logger = mocker.MagicMock()
        supervisor = TaskSupervisor(mock_logger)

        crash_count = 0
        ran_after_restart = asyncio.Event()

        async def crashing_coro():
            nonlocal crash_count
            crash_count += 1
            if crash_count == 1:
                raise ValueError("Simulated crash")
            # Second run succeeds
            ran_after_restart.set()
            await asyncio.Event().wait()

        supervisor.add('crashy', crashing_coro)

        # Wait for the restart to happen (5s sleep in _run, so use a generous timeout)
        await asyncio.wait_for(ran_after_restart.wait(), timeout=10)
        assert crash_count >= 2
        mock_logger.error.assert_called()
        mock_logger.info.assert_any_call(mocker.ANY)

        # Cleanup
        await supervisor.shutdown()

    async def test_supervisor_shutdown_cancels_tasks(self, mocker):
        """supervisor.shutdown() must cancel all managed tasks."""
        from main import TaskSupervisor

        mock_logger = mocker.MagicMock()
        supervisor = TaskSupervisor(mock_logger)

        async def dummy_coro():
            await asyncio.Event().wait()

        supervisor.add('task_a', dummy_coro)
        supervisor.add('task_b', dummy_coro)

        assert len(supervisor._tasks) == 2

        await supervisor.shutdown()

        # All tasks should be done (cancelled)
        for task in supervisor._tasks.values():
            assert task.done()
            assert task.cancelled()


# ── TaskSupervisor ─────────────────────────────────────────────────────────

class TestTaskSupervisor:
    """TaskSupervisor creates, monitors, and restarts critical asyncio tasks."""

    async def test_supervisor_creates_task(self, mocker):
        from main import TaskSupervisor
        mock_logger = mocker.MagicMock()
        supervisor = TaskSupervisor(mock_logger)
        started = asyncio.Event()
        async def dummy_coro():
            started.set()
            await asyncio.Event().wait()
        supervisor.add('test_task', dummy_coro)
        assert 'test_task' in supervisor._tasks
        task = supervisor._tasks['test_task']
        assert isinstance(task, asyncio.Task)
        assert not task.done()
        await asyncio.wait_for(started.wait(), timeout=2)
        await supervisor.shutdown()

    async def test_supervisor_restarts_on_failure(self, mocker):
        from main import TaskSupervisor
        mock_logger = mocker.MagicMock()
        supervisor = TaskSupervisor(mock_logger)
        crash_count = 0
        ran_after_restart = asyncio.Event()
        async def crashing_coro():
            nonlocal crash_count
            crash_count += 1
            if crash_count == 1:
                raise ValueError("Simulated crash")
            ran_after_restart.set()
            await asyncio.Event().wait()
        supervisor.add('crashy', crashing_coro)
        await asyncio.wait_for(ran_after_restart.wait(), timeout=10)
        assert crash_count >= 2
        mock_logger.error.assert_called()
        await supervisor.shutdown()

    async def test_supervisor_shutdown_cancels_tasks(self, mocker):
        from main import TaskSupervisor
        mock_logger = mocker.MagicMock()
        supervisor = TaskSupervisor(mock_logger)
        async def dummy_coro():
            await asyncio.Event().wait()
        supervisor.add('task_a', dummy_coro)
        supervisor.add('task_b', dummy_coro)
        assert len(supervisor._tasks) == 2
        await supervisor.shutdown()
        for task in supervisor._tasks.values():
            assert task.done()
            assert task.cancelled()
