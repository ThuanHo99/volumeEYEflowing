"""Unit tests for core.state_manager."""

import pytest
from core.state_manager import IndicatorState, VolumeTracker
from tests.factories import make_closed_candle, make_open_candle


class TestIndicatorState:
    def setup_method(self):
        self.state = IndicatorState(['1m'])

    def test_returns_none_for_open_candle(self):
        result = self.state.process_candle(make_open_candle('1m', 50000.0))
        assert result is None

    def test_returns_snapshot_for_closed_candle(self):
        result = self.state.process_candle(make_closed_candle('1m', 50000.0))
        assert result is not None
        assert result.tf == '1m'
        assert result.close == 50000.0

    def test_ema_becomes_valid_after_200_candles(self):
        snapshot = None
        for i in range(200):
            snapshot = self.state.process_candle(make_closed_candle('1m', 50000.0 + i, ts=i * 60000))
        assert snapshot is not None
        assert snapshot.ema200 is not None
        assert isinstance(snapshot.ema200, float)

    def test_rsi_becomes_valid_after_init(self):
        snapshot = None
        for i in range(15):
            snapshot = self.state.process_candle(make_closed_candle('1m', 50000.0 + i * 100, ts=i * 60000))
        assert snapshot is not None
        assert snapshot.rsi14 is not None
        assert 0 <= snapshot.rsi14 <= 100

    def test_update_does_not_change_ema_length(self):
        for i in range(50):
            self.state.process_candle(make_closed_candle('1m', 50000.0 + i, ts=i * 60000))
        len_before = len(self.state.ema['1m'])
        for _ in range(10):
            self.state.process_candle(make_open_candle('1m', 51000.0, ts=49 * 60000))
        len_after = len(self.state.ema['1m'])
        assert len_before == len_after

    def test_purge_does_not_break_indicator(self):
        for i in range(520):
            self.state.process_candle(make_closed_candle('1m', 50000.0 + i % 100, ts=i * 60000))
        snapshot = self.state.process_candle(make_closed_candle('1m', 55000.0, ts=520 * 60000))
        assert snapshot is not None
        assert snapshot.ema200 is not None

    def test_snapshot_has_all_fields(self):
        for i in range(200):
            self.state.process_candle(make_closed_candle('1m', 50000.0 + i, ts=i * 60000))
        result = self.state.process_candle(make_closed_candle('1m', 51000.0, ts=200 * 60000))
        assert result.ema200 is not None
        assert result.rsi14 is not None
        assert result.volume == 1.0
        assert result.is_closed is True

    def test_multi_timeframe(self):
        state = IndicatorState(['1m', '5m', '15m'])
        for tf in ['1m', '5m', '15m']:
            for i in range(15):
                state.process_candle(make_closed_candle(tf, 50000.0 + i, ts=i * 60000))
        for tf in ['1m', '5m', '15m']:
            snap = state.process_candle(make_closed_candle(tf, 50100.0, ts=15 * 60000))
            assert snap.rsi14 is not None


class TestVolumeTracker:
    def setup_method(self):
        self.tracker = VolumeTracker(window=24)

    def test_empty_tracker_returns_zero(self):
        assert self.tracker.get_avg_24h('1m') == 0.0

    def test_single_entry_returns_itself(self):
        self.tracker.update('1m', 100.0, is_closed=True)
        assert self.tracker.get_avg_24h('1m') == 100.0

    def test_overflow_pops_oldest(self):
        for i in range(25):
            self.tracker.update('1m', float(i), is_closed=True)
        assert self.tracker.get_avg_24h('1m') == sum(range(1, 25)) / 24

    def test_open_candle_does_not_add_to_history(self):
        self.tracker.update('1m', 100.0, is_closed=False)
        assert self.tracker.get_avg_24h('1m') == 0.0

    def test_multi_timeframe_independence(self):
        self.tracker.update('1m', 100.0, is_closed=True)
        self.tracker.update('5m', 200.0, is_closed=True)
        self.tracker.update('1m', 50.0, is_closed=True)
        assert self.tracker.get_avg_24h('1m') == 75.0
        assert self.tracker.get_avg_24h('5m') == 200.0

    def test_get_avg_24h_unknown_tf(self):
        assert self.tracker.get_avg_24h('unknown') == 0.0


# ── Bug 3: Candle sort ─────────────────────────────────────────────────────

class TestBug3_CandleSort:
    """Data always arrives sorted by start_ts, making O(n) insert possible."""

    def test_indicator_snapshots_ordered_by_timestamp(self):
        """process_candle should emit snapshots in monotonic timestamp order."""
        state = IndicatorState(timeframes=['1m'])
        results = []
        base_ts = 1700000000000
        for i in range(10):
            candle = {
                'tf': '1m', 'start_ts': base_ts + i * 60000,
                'open': 50000 + i, 'high': 50010 + i,
                'low': 49990 + i, 'close': 50005 + i,
                'volume': 100 + i, 'is_closed': True,
            }
            snap = state.process_candle(candle)
            if snap:
                results.append(snap)
        for i in range(len(results) - 1):
            assert results[i].timestamp <= results[i + 1].timestamp

    def test_interleaved_timeframe_inserts_dont_break_order(self):
        """Simulate 3 TFs: each TF's candles arrive in order but interleaved."""
        state = IndicatorState(timeframes=['1m', '5m', '15m'])
        tfs = ['1m', '5m', '15m']
        base_ts = 1700000000000
        snapshots: dict[str, list] = {tf: [] for tf in tfs}
        for step in range(5):
            for tf in tfs:
                candle = {
                    'tf': tf, 'start_ts': base_ts + step * 60000,
                    'open': 50000, 'high': 50100, 'low': 49900, 'close': 50050,
                    'volume': 100, 'is_closed': True,
                }
                snap = state.process_candle(candle)
                if snap:
                    snapshots[tf].append(snap)
        for tf in tfs:
            stamps = [s.timestamp for s in snapshots[tf]]
            assert stamps == sorted(stamps), f"TF {tf} timestamps not sorted: {stamps}"
