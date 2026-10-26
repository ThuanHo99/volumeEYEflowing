import asyncio
import pytest
import pytest_asyncio

from core.state_manager import IndicatorState, VolumeTracker


@pytest.fixture
def timeframes():
    return ['1m', '5m', '15m', '1h', '4h']


@pytest.fixture
def state(timeframes):
    return IndicatorState(timeframes)


@pytest.fixture
def volume_tracker():
    return VolumeTracker(window=24)


@pytest_asyncio.fixture
async def queues():
    candle_q = asyncio.Queue(maxsize=10000)
    signal_q = asyncio.Queue(maxsize=500)
    ui_q = asyncio.Queue(maxsize=20000)
    return candle_q, signal_q, ui_q
