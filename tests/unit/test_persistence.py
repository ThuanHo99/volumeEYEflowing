"""
Round-trip persistence unit tests for all 4 state types.
Uses tmp_path for isolation -- never writes to real data/ directory.
"""

import json
import os
import pytest

from talipp.indicators import EMA, RSI

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


@pytest.fixture
def state_mgr(timeframes, state, volume_tracker):
    from core.state_manager import SymbolStateManager
    mgr = SymbolStateManager(timeframes)
    # Pre-populate with BTC/USDT:USDT state
    return mgr


def _make_closed_candle(tf: str, close: float, ts: int, volume: float = 1.0) -> dict:
    return {
        'tf': tf, 'start_ts': ts,
        'open': close, 'high': close, 'low': close,
        'close': close, 'volume': volume, 'is_closed': True,
    }


def _feed_candles(state, volume_tracker, n: int = 200):
    """Feed n closed candles to state and volume_tracker for each timeframe."""
    for i in range(n):
        close = 50000.0 + i * 10.0  # steadily increasing price
        for tf in state._timeframes:
            candle = _make_closed_candle(tf, close, ts=i * 60000, volume=1.0 + (i % 5))
            snap = state.process_candle(candle)
            if snap is not None:
                volume_tracker.update(tf, candle['volume'], is_closed=True)


class TestPersistenceRoundTrip:
    """Round-trip save/load tests for all 4 persistence types."""

    @pytest.mark.asyncio
    async def test_indicator_state_save_load(self, mocker, state, volume_tracker, state_mgr, tmp_path):
        """Save indicator state and verify the JSON file structure."""
        from core.persistence import save_indicator_state, load_indicator_state

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        # Feed candles into SymbolStateManager
        for i in range(200):
            close = 50000.0 + i * 10.0
            for tf in state._timeframes:
                candle = _make_closed_candle(tf, close, ts=i * 60000, volume=1.0 + (i % 5))
                candle['symbol'] = 'BTC/USDT:USDT'
                snap = state_mgr.get_state('BTC/USDT:USDT').process_candle(candle)
                if snap is not None:
                    state_mgr.get_volume_tracker('BTC/USDT:USDT').update(tf, candle['volume'], is_closed=True)

        result = await save_indicator_state(state_mgr)
        assert result is True

        # Verify file exists
        indicator_file = tmp_path / 'indicator_state.json'
        assert indicator_file.exists(), "indicator_state.json should exist after save"

        # Reload and check structure
        loaded = load_indicator_state()
        assert loaded is not None
        assert 'BTC/USDT:USDT' in loaded
        assert 'volume_tracker' in loaded['BTC/USDT:USDT']
        assert 'indicators' in loaded['BTC/USDT:USDT']

        # Check indicators structure
        for tf in state._timeframes:
            assert tf in loaded['BTC/USDT:USDT']['indicators']
            assert 'ema_inputs' in loaded['BTC/USDT:USDT']['indicators'][tf]
            assert 'rsi_inputs' in loaded['BTC/USDT:USDT']['indicators'][tf]
            assert len(loaded['BTC/USDT:USDT']['indicators'][tf]['ema_inputs']) == 200
            assert len(loaded['BTC/USDT:USDT']['indicators'][tf]['rsi_inputs']) == 200

    @pytest.mark.asyncio
    async def test_indicator_state_restore_with_talipp(self, mocker, state, volume_tracker, state_mgr, tmp_path, timeframes):
        """Save indicator state, reload, and replay into talipp to verify identical last values."""
        from core.persistence import save_indicator_state, load_indicator_state

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        # Feed 200 candles into SymbolStateManager
        for i in range(200):
            close = 50000.0 + i * 10.0
            for tf in timeframes:
                candle = _make_closed_candle(tf, close, ts=i * 60000, volume=1.0 + (i % 5))
                candle['symbol'] = 'BTC/USDT:USDT'
                snap = state_mgr.get_state('BTC/USDT:USDT').process_candle(candle)
                if snap is not None:
                    state_mgr.get_volume_tracker('BTC/USDT:USDT').update(tf, candle['volume'], is_closed=True)

        # Record original last values from the first (and only) symbol
        sym_state = state_mgr.get_state('BTC/USDT:USDT')
        original_last = {}
        for tf in timeframes:
            original_last[tf] = {
                'ema': float(sym_state.ema[tf][-1]) if sym_state.ema[tf][-1] is not None else None,
                'rsi': float(sym_state.rsi[tf][-1]) if sym_state.rsi[tf][-1] is not None else None,
            }

        # Save
        await save_indicator_state(state_mgr)

        # Reload raw dict
        loaded = load_indicator_state()
        assert loaded is not None, "Should load saved state"

        # Replay into fresh talipp indicators
        for tf in timeframes:
            ema = EMA(200)
            rsi = RSI(14)
            for val in loaded['BTC/USDT:USDT']['indicators'][tf]['ema_inputs']:
                ema.add(val)
            for val in loaded['BTC/USDT:USDT']['indicators'][tf]['rsi_inputs']:
                rsi.add(val)

            # Verify last values match
            loaded_ema = float(ema[-1]) if ema[-1] is not None else None
            loaded_rsi = float(rsi[-1]) if rsi[-1] is not None else None

            assert loaded_ema == original_last[tf]['ema'], \
                f"EMA({tf}) mismatch: {loaded_ema} != {original_last[tf]['ema']}"
            assert loaded_rsi == original_last[tf]['rsi'], \
                f"RSI({tf}) mismatch: {loaded_rsi} != {original_last[tf]['rsi']}"

    @pytest.mark.asyncio
    async def test_seen_hashes_save_load(self, mocker, tmp_path):
        """Save and load seen hashes round-trip."""
        from core.persistence import save_seen_hashes, load_seen_hashes

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        test_hashes = {'abc123', 'def456', 'ghi789'}
        result = await save_seen_hashes(test_hashes)
        assert result is True

        loaded = load_seen_hashes()
        assert loaded == test_hashes, f"Loaded hashes mismatch: {loaded} != {test_hashes}"

    @pytest.mark.asyncio
    async def test_rate_limits_save_load(self, mocker, tmp_path):
        """Save and load rate-limit state round-trip."""
        from core.persistence import save_rate_limits, load_rate_limits

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        test_data = {
            'RSI_OVERBOUGHT_15m': 1234567890.0,
            'VOLUME_SPIKE_1h': 1234567895.0,
        }
        result = await save_rate_limits(test_data)
        assert result is True

        loaded = load_rate_limits()
        assert loaded == test_data, f"Loaded rate limits mismatch: {loaded} != {test_data}"

    @pytest.mark.asyncio
    async def test_ohlcv_cache_save_load(self, mocker, tmp_path):
        """Save and load OHLCV cache round-trip."""
        from core.persistence import save_ohlcv_cache, load_ohlcv_cache

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        test_cache = {
            '1m': [
                {
                    'tf': '1m', 'start_ts': 1000,
                    'open': 50000.0, 'high': 50100.0, 'low': 49900.0,
                    'close': 50050.0, 'volume': 10.0, 'is_closed': True,
                }
            ],
        }
        result = await save_ohlcv_cache(test_cache)
        assert result is True

        loaded = load_ohlcv_cache()
        assert loaded == test_cache, f"Loaded cache mismatch: {loaded} != {test_cache}"

    def test_load_missing_file_returns_default(self, mocker, tmp_path):
        """Each load function returns its default value when file doesn't exist."""
        from core.persistence import (
            load_indicator_state, load_seen_hashes,
            load_rate_limits, load_ohlcv_cache,
        )

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        assert load_indicator_state() is None, "Missing indicator file should return None"
        assert load_seen_hashes() == set(), "Missing hashes file should return empty set"
        assert load_rate_limits() == {}, "Missing rate-limit file should return empty dict"
        assert load_ohlcv_cache() == {}, "Missing OHLCV cache file should return empty dict"

    def test_load_corrupt_file_returns_default(self, mocker, tmp_path):
        """Corrupt JSON file should return the default value, not crash."""
        from core.persistence import (
            load_indicator_state, load_seen_hashes,
            load_rate_limits, load_ohlcv_cache,
            FILES,
        )

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        # Write corrupt JSON to the hashes file
        hashes_file = tmp_path / FILES['hashes']
        hashes_file.write_text('not json', encoding='utf-8')

        # Should not crash, return default
        assert load_seen_hashes() == set(), "Corrupt file should return empty set"

        # Also test indicator load on corrupt file
        indicator_file = tmp_path / FILES['indicator']
        indicator_file.write_text('also not json', encoding='utf-8')
        assert load_indicator_state() is None, "Corrupt indicator file should return None"

    @pytest.mark.asyncio
    async def test_atomic_write_creates_no_tmp_leftover(self, mocker, tmp_path):
        """After atomic save, .tmp file should not exist and target should have valid JSON."""
        from core.persistence import save_seen_hashes, FILES

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        test_data = {'test': 'data'}
        await save_seen_hashes({'abc'})

        # .tmp file should NOT exist
        for name, filename in FILES.items():
            tmp_file = tmp_path / (filename + '.tmp')
            target_file = tmp_path / filename
            if target_file.exists():
                # Verify no .tmp file
                assert not tmp_file.exists(), f"Orphan .tmp file exists for {name}"
                # Verify target is valid JSON
                with open(str(target_file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                assert data is not None
