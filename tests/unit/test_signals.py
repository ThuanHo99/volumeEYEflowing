"""Unit tests for core.signals."""

from unittest import mock

from core.signals import (
    check_rsi_extremes, check_volume_spike, check_ema_touch,
    check_confluence, check_confluence_cross_tf, check_rsi_divergence,
    SignalType, SignalLevel,
)
from tests.factories import make_snap


class TestRSIExtremes:
    def test_rsi_overbought(self):
        with mock.patch('core.signals.RSI_OVERBOUGHT', 75):
            s = check_rsi_extremes(make_snap('15m', 50000, rsi14=76.0))
        assert s is not None and s.type == SignalType.RSI_OVERBOUGHT

    def test_rsi_oversold(self):
        with mock.patch('core.signals.RSI_OVERSOLD', 25):
            s = check_rsi_extremes(make_snap('15m', 50000, rsi14=24.0))
        assert s is not None and s.type == SignalType.RSI_OVERSOLD

    def test_rsi_normal_no_signal(self):
        with mock.patch('core.signals.RSI_OVERBOUGHT', 75), \
             mock.patch('core.signals.RSI_OVERSOLD', 25):
            s = check_rsi_extremes(make_snap('15m', 50000, rsi14=50.0))
        assert s is None


class TestVolumeSpike:
    def test_volume_spike(self):
        snap = make_snap('1h', 50000, volume=300.0)
        with mock.patch('core.signals.VOLUME_SPIKE_MULTIPLIER', 2.0):
            s = check_volume_spike(snap, avg_24h_volume=100.0)
        assert s is not None and s.type == SignalType.VOLUME_SPIKE


class TestEMATouch:
    def test_ema_support_touch(self):
        """Close within 0.1% above EMA200 should trigger support touch."""
        snap = make_snap('4h', 50050.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=49900.0, candle_high=50100.0)
        assert s is not None and s.type == SignalType.EMA_SUPPORT_TOUCH

    def test_ema_resistance_touch(self):
        """Close within 0.1% below EMA200 should trigger resistance touch."""
        snap = make_snap('4h', 49950.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=49900.0, candle_high=50100.0)
        assert s is not None and s.type == SignalType.EMA_RESISTANCE_TOUCH

    def test_ema_far_from_tolerance_no_signal(self):
        """Close 2% away from EMA should NOT trigger."""
        snap = make_snap('4h', 49000.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=49500.0, candle_high=51000.0)
        assert s is None


class TestConfluence:
    def test_confluence_long(self):
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 0.1), \
             mock.patch('core.signals.RSI_OVERSOLD', 25):
            ema_sig = check_ema_touch(
                make_snap('4h', 50050.0, ema200=50000.0), 49900.0, 50100.0)
            rsi_sig = check_rsi_extremes(make_snap('15m', 49950.0, rsi14=22.0))
            c = check_confluence(ema_sig, rsi_sig)
        assert c is not None and c.type == SignalType.CONFLUENCE_LONG
        assert c.level == SignalLevel.HIGH_PRIORITY

    def test_confluence_requires_both(self):
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 0.1):
            ema_sig = check_ema_touch(
                make_snap('4h', 49950.0, ema200=50000.0), 49900.0, 50100.0)
            c = check_confluence(ema_sig, None)
        assert c is None


# ── Duplicate Signal Suppression ───────────────────────────────────────────

class TestSignalSuppression:
    """SignalCooldownManager must suppress duplicate signals within cooldown."""

    def setup_method(self):
        from core.signals import SignalCooldownManager
        self.cooldown = SignalCooldownManager()

    def test_is_signal_suppressed_new_signal(self):
        """First occurrence of a signal type must NOT be suppressed."""
        from core.signals import Signal, SignalType, SignalLevel
        signal = Signal(
            type=SignalType.RSI_OVERBOUGHT,
            level=SignalLevel.WARNING,
            tf='15m', close=50000.0, rsi14=75.0,
            ema200=None, volume=None, timestamp=1700000000000,
            detail="Test RSI overbought",
        )
        assert not self.cooldown.is_suppressed(signal)

    def test_is_signal_suppressed_duplicate(self):
        """Same signal type within cooldown window MUST be suppressed."""
        from core.signals import Signal, SignalType, SignalLevel
        signal = Signal(
            type=SignalType.RSI_OVERBOUGHT,
            level=SignalLevel.WARNING,
            tf='15m', close=50000.0, rsi14=75.0,
            ema200=None, volume=None, timestamp=1700000000000,
            detail="Test RSI overbought",
        )
        assert not self.cooldown.is_suppressed(signal), "First call must not be suppressed"
        assert self.cooldown.is_suppressed(signal), "Duplicate must be suppressed"


class TestVolumeSpikeUSDT:
    def test_volume_spike_usdt_low_price_coin_skipped(self):
        # average volume 10.0, price 1.0 (ADA) -> 24h average volume is only 10.0 USDT
        # This is below default MIN_VOLUME_THRESHOLD_USDT (50000.0) -> must be skipped
        snap = make_snap('1h', 1.0, volume=60.0) # 6x spike
        with mock.patch('core.signals.VOLUME_SPIKE_MULTIPLIER', 2.0), \
             mock.patch('core.signals.MIN_VOLUME_THRESHOLD_USDT', 50000.0):
            s = check_volume_spike(snap, avg_24h_volume=10.0)
        assert s is None

    def test_volume_spike_usdt_high_price_coin_triggered(self):
        # average volume 10.0, price 60000.0 (BTC) -> 24h average volume is 600,000 USDT >= 50,000 -> triggers
        snap = make_snap('1h', 60000.0, volume=30.0) # 3x spike
        with mock.patch('core.signals.VOLUME_SPIKE_MULTIPLIER', 2.0), \
             mock.patch('core.signals.MIN_VOLUME_THRESHOLD_USDT', 50000.0):
            s = check_volume_spike(snap, avg_24h_volume=10.0)
        assert s is not None and s.type == SignalType.VOLUME_SPIKE


class TestEMATouchBuffer:
    def test_ema_support_exact_touch_triggered(self):
        # EMA = 50000. Low = 50000. Close = 50100 (0.2% deviation <= 1.5% tolerance)
        snap = make_snap('4h', 50100.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 1.5), \
             mock.patch('core.signals.VS_TOUCH_BUFFER_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=50000.0, candle_high=50200.0)
        assert s is not None and s.type == SignalType.EMA_SUPPORT_TOUCH

    def test_ema_support_front_running_within_buffer_triggered(self):
        # EMA = 50000. Low = 50020 (hụt 20 USD = 0.04% < 0.1% buffer). Close = 50100.
        snap = make_snap('4h', 50100.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 1.5), \
             mock.patch('core.signals.VS_TOUCH_BUFFER_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=50020.0, candle_high=50200.0)
        assert s is not None and s.type == SignalType.EMA_SUPPORT_TOUCH

    def test_ema_support_front_running_outside_buffer_skipped(self):
        # EMA = 50000. Low = 50060 (hụt 60 USD = 0.12% > 0.1% buffer). Close = 50100.
        snap = make_snap('4h', 50100.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 1.5), \
             mock.patch('core.signals.VS_TOUCH_BUFFER_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=50060.0, candle_high=50200.0)
        assert s is None

    def test_ema_support_liquidity_sweep_triggered(self):
        # EMA = 50000. Low = 49800 (đâm xuyên qua EMA). Close = 50200 (deviation = 0.4% <= 1.5%)
        snap = make_snap('4h', 50200.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 1.5), \
             mock.patch('core.signals.VS_TOUCH_BUFFER_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=49800.0, candle_high=50300.0)
        assert s is not None and s.type == SignalType.EMA_SUPPORT_TOUCH

    def test_ema_support_too_far_skipped(self):
        # EMA = 50000. Low = 49900. Close = 51000 (2.0% deviation > 1.5% tolerance)
        snap = make_snap('4h', 51000.0, ema200=50000.0)
        with mock.patch('core.signals.EMA_TOUCH_TIMEFRAMES', ['4h']), \
             mock.patch('core.signals.SIGNAL_TOLERANCE_PCT', 1.5), \
             mock.patch('core.signals.VS_TOUCH_BUFFER_PCT', 0.1):
            s = check_ema_touch(snap, candle_low=49900.0, candle_high=51100.0)
        assert s is None


class TestConfluenceCrossTFAge:
    def test_confluence_cross_tf_max_age_valid(self):
        from core.signals import Signal, SignalType, SignalLevel
        rsi_sig = Signal(
            type=SignalType.RSI_OVERSOLD, level=SignalLevel.WARNING,
            symbol="BTC", tf='15m', close=50000.0, rsi14=20.0, timestamp=0
        )
        ema_sig = Signal(
            type=SignalType.EMA_SUPPORT_TOUCH, level=SignalLevel.ALERT,
            symbol="BTC", tf='1m', close=50100.0, ema200=50000.0, timestamp=0
        )
        # Monotonic time of RSI is 100, now is 700 (600s age). Max age = 900s -> Valid!
        with mock.patch('core.signals.CONFLUENCE_MAX_AGE_SEC', 900), \
             mock.patch('time.monotonic', return_value=700.0):
            c = check_confluence_cross_tf(ema_sig, (SignalType.RSI_OVERSOLD.value, rsi_sig, 100.0))
        assert c is not None and c.type == SignalType.CONFLUENCE_LONG

    def test_confluence_cross_tf_max_age_expired(self):
        from core.signals import Signal, SignalType, SignalLevel
        rsi_sig = Signal(
            type=SignalType.RSI_OVERSOLD, level=SignalLevel.WARNING,
            symbol="BTC", tf='15m', close=50000.0, rsi14=20.0, timestamp=0
        )
        ema_sig = Signal(
            type=SignalType.EMA_SUPPORT_TOUCH, level=SignalLevel.ALERT,
            symbol="BTC", tf='1m', close=50100.0, ema200=50000.0, timestamp=0
        )
        # Monotonic time of RSI is 100, now is 1050 (950s age). Max age = 900s -> Expired!
        with mock.patch('core.signals.CONFLUENCE_MAX_AGE_SEC', 900), \
             mock.patch('time.monotonic', return_value=1050.0):
            c = check_confluence_cross_tf(ema_sig, (SignalType.RSI_OVERSOLD.value, rsi_sig, 100.0))
        assert c is None


class TestRSIDivergenceNew:
    def test_bullish_divergence_triggered_on_confirmation(self):
        from collections import deque
        # Construct close and rsi history of 20 elements
        close_history = deque(maxlen=20)
        rsi_history = deque(maxlen=20)
        
        # Populate base values
        for i in range(20):
            close_history.append(110.0)
            rsi_history.append(50.0)
            
        # Pivot 1 at index 5: low is 100.0, rsi is 30.0
        # Swing check range j in 1, 2, 3: indices 2, 3, 4 must be > 100.0; indices 6, 7, 8 must be > 100.0
        close_history[5] = 100.0
        rsi_history[5] = 30.0
        
        # Pivot 2 at index 16 (n-min_bars-1): low is 95.0, rsi is 35.0 (divergence!)
        # Swing check range j in 1, 2, 3: indices 13, 14, 15 must be > 95.0; indices 17, 18, 19 must be > 95.0
        close_history[16] = 95.0
        rsi_history[16] = 35.0
        
        # Call divergence detection
        s = check_rsi_divergence(close_history, rsi_history, timestamp=0, symbol="BTC")
        assert s is not None and s.type == SignalType.BULLISH_DIVERGENCE
        
    def test_bullish_divergence_not_triggered_again_after_confirmation(self):
        from collections import deque
        close_history = deque(maxlen=20)
        rsi_history = deque(maxlen=20)
        
        # Populate base values
        for i in range(20):
            close_history.append(110.0)
            rsi_history.append(50.0)
            
        # Pivot 1 at index 4 (shifted from 5 due to append)
        close_history[4] = 100.0
        rsi_history[4] = 30.0
        
        # Pivot 2 at index 15 (shifted from 16 due to append)
        close_history[15] = 95.0
        rsi_history[15] = 35.0
        
        # Current candle is index 19 (which is not 15 + 3 + 1 = 19, wait, index of pivot 2 is 15, n=20, min_bars=3)
        # Here idx2 = 15. n - min_bars - 1 = 20 - 3 - 1 = 16.
        # Since idx2 (15) != 16, it should not trigger!
        s = check_rsi_divergence(close_history, rsi_history, timestamp=0, symbol="BTC")
        assert s is None

