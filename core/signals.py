import time
import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.config import MIN_VOLUME_THRESHOLD_USDT, CONFLUENCE_MAX_AGE_SEC, RSI_OVERSOLD, RSI_OVERBOUGHT, VOLUME_SPIKE_MULTIPLIER, EMA_TOUCH_TIMEFRAMES, SIGNAL_TOLERANCE_PCT, VS_TOUCH_BUFFER_PCT

logger = logging.getLogger(__name__)


class SignalType(Enum):
    RSI_OVERBOUGHT       = "RSI_OVERBOUGHT"
    RSI_OVERSOLD         = "RSI_OVERSOLD"
    VOLUME_SPIKE         = "VOLUME_SPIKE"
    EMA_SUPPORT_TOUCH    = "EMA_SUPPORT_TOUCH"
    EMA_RESISTANCE_TOUCH = "EMA_RESISTANCE_TOUCH"
    BULLISH_DIVERGENCE   = "BULLISH_DIVERGENCE"
    BEARISH_DIVERGENCE   = "BEARISH_DIVERGENCE"
    CONFLUENCE_LONG      = "CONFLUENCE_LONG"
    CONFLUENCE_SHORT     = "CONFLUENCE_SHORT"


class SignalLevel(Enum):
    WARNING       = "⚠️ WARNING"
    ALERT         = "\U0001f514 ALERT"
    HIGH_PRIORITY = "\U0001f6a8 HIGH PRIORITY"


@dataclass
class Signal:
    type:      SignalType
    level:     SignalLevel
    tf:        str
    close:     float
    timestamp: int
    symbol:    str = ''
    rsi14:     Optional[float] = None
    ema200:    Optional[float] = None
    volume:    Optional[float] = None
    detail:    str = ""


def check_rsi_extremes(snapshot) -> Optional[Signal]:
    if snapshot.tf != '15m' or snapshot.rsi14 is None:
        return None
    if snapshot.rsi14 > RSI_OVERBOUGHT:
        return Signal(
            type=SignalType.RSI_OVERBOUGHT, level=SignalLevel.WARNING,
            symbol=snapshot.symbol, tf='15m', close=snapshot.close, rsi14=snapshot.rsi14,
            ema200=snapshot.ema200, volume=snapshot.volume,
            timestamp=snapshot.timestamp,
            detail=f"RSI 15m = {snapshot.rsi14:.1f} (overbought threshold: {RSI_OVERBOUGHT})",
        )
    if snapshot.rsi14 < RSI_OVERSOLD:
        return Signal(
            type=SignalType.RSI_OVERSOLD, level=SignalLevel.WARNING,
            symbol=snapshot.symbol, tf='15m', close=snapshot.close, rsi14=snapshot.rsi14,
            ema200=snapshot.ema200, volume=snapshot.volume,
            timestamp=snapshot.timestamp,
            detail=f"RSI 15m = {snapshot.rsi14:.1f} (oversold threshold: {RSI_OVERSOLD})",
        )
    return None


def check_volume_spike(snapshot, avg_24h_volume: float) -> Optional[Signal]:
    if snapshot.tf != '1h' or avg_24h_volume <= 0:
        return None
    avg_vol_usdt = avg_24h_volume * snapshot.close
    if avg_vol_usdt < MIN_VOLUME_THRESHOLD_USDT:
        logger.debug(f"Volume 24h in USDT {avg_vol_usdt:.2f} < MIN_VOLUME_THRESHOLD_USDT ({MIN_VOLUME_THRESHOLD_USDT}), skipping spike check")
        return None
    if snapshot.volume > avg_24h_volume * VOLUME_SPIKE_MULTIPLIER:
        ratio = snapshot.volume / avg_24h_volume
        return Signal(
            type=SignalType.VOLUME_SPIKE, level=SignalLevel.ALERT,
            symbol=snapshot.symbol, tf='1h', close=snapshot.close, rsi14=snapshot.rsi14,
            ema200=snapshot.ema200, volume=snapshot.volume,
            timestamp=snapshot.timestamp,
            detail=f"Volume 1h = {snapshot.volume:.2f} ({ratio:.1f}x 24h avg)",
        )
    return None


def _within_tolerance(price: float, ema: float, tolerance_pct: float) -> bool:
    """Check if price is within ±tolerance_pct% of EMA."""
    if ema <= 0:
        return False
    deviation_pct = abs(price - ema) / ema * 100
    return deviation_pct <= tolerance_pct


def _build_ema_touch_signal(snapshot, ema: float, is_support: bool) -> Signal:
    deviation = (snapshot.close - ema) / ema * 100
    sig_type = SignalType.EMA_SUPPORT_TOUCH if is_support else SignalType.EMA_RESISTANCE_TOUCH
    return Signal(
        symbol=snapshot.symbol, type=sig_type, level=SignalLevel.ALERT,
        tf=snapshot.tf, close=snapshot.close, rsi14=snapshot.rsi14,
        ema200=ema, volume=snapshot.volume,
        timestamp=snapshot.timestamp,
        detail=f"Close {snapshot.tf} = {snapshot.close:.2f} ({deviation:+.2f}%) vs EMA200 = {ema:.2f}",
    )


def check_ema_touch(snapshot, candle_low: float, candle_high: float) -> Optional[Signal]:
    if snapshot.tf not in EMA_TOUCH_TIMEFRAMES or snapshot.ema200 is None:
        return None
    ema = snapshot.ema200
    if snapshot.close <= 0:
        return None

    # Close must be within SIGNAL_TOLERANCE_PCT% (e.g. 1.5%) of EMA to avoid entering too late
    deviation_pct = abs(snapshot.close - ema) / ema * 100
    if deviation_pct > SIGNAL_TOLERANCE_PCT:
        return None

    # Calculate EMA touch buffer zone
    touch_buffer = ema * (VS_TOUCH_BUFFER_PCT / 100)

    # If price closed above or on EMA, EMA acts as SUPPORT
    if snapshot.close >= ema:
        # Accept front-running (under ema + buffer) or liquidity sweep (deep penetration)
        if candle_low <= ema + touch_buffer:
            return _build_ema_touch_signal(snapshot, ema, is_support=True)
    # If price closed below EMA, EMA acts as RESISTANCE
    else:
        # Accept front-running (above ema - buffer) or liquidity sweep (deep penetration)
        if candle_high >= ema - touch_buffer:
            return _build_ema_touch_signal(snapshot, ema, is_support=False)

    return None


def check_rsi_divergence(
    close_history: 'list[float] | deque',
    rsi_history: 'list[float] | deque',
    timestamp: int | None = None,
    symbol: str = '',
) -> Optional[Signal]:
    if timestamp is None:
        timestamp = int(time.time() * 1000)
    if len(close_history) < 14 or len(rsi_history) < 14:
        return None

    n = len(close_history)
    min_bars = min(3, n // 4)

    # 1. Tìm tất cả các swing low (đáy pivot) đã xác nhận
    swing_low_indices = []
    for i in range(n - min_bars - 1, min_bars - 1, -1):
        left_ok = all(close_history[i] < close_history[i - j] for j in range(1, min_bars + 1))
        right_ok = all(close_history[i] < close_history[i + j] for j in range(1, min_bars + 1))
        if left_ok and right_ok:
            swing_low_indices.append(i)

    # 2. Tìm tất cả các swing high (đỉnh pivot) đã xác nhận
    swing_high_indices = []
    for i in range(n - min_bars - 1, min_bars - 1, -1):
        left_ok = all(close_history[i] > close_history[i - j] for j in range(1, min_bars + 1))
        right_ok = all(close_history[i] > close_history[i + j] for j in range(1, min_bars + 1))
        if left_ok and right_ok:
            swing_high_indices.append(i)

    rsi_threshold = 3.0

    # 3. Check Bullish Divergence (so sánh 2 swing lows gần nhất)
    if len(swing_low_indices) >= 2:
        idx2 = swing_low_indices[0]  # Pivot low mới hơn
        idx1 = swing_low_indices[1]  # Pivot low cũ hơn
        
        # Chỉ kích hoạt tại thời điểm pivot mới được xác nhận đầy đủ
        if idx2 == n - min_bars - 1:
            price2 = close_history[idx2]
            rsi2 = rsi_history[idx2]
            price1 = close_history[idx1]
            rsi1 = rsi_history[idx1]
            
            if price2 < price1 and rsi2 > rsi1 + rsi_threshold:
                return Signal(
                    symbol=symbol, type=SignalType.BULLISH_DIVERGENCE, level=SignalLevel.ALERT,
                    tf='15m', close=close_history[-1], rsi14=rsi_history[-1],
                    ema200=None, volume=None,
                    timestamp=timestamp,
                    detail=f"Bullish Divergence: Price Lows ↓ ({price1:.1f}→{price2:.1f}) but RSI Lows ↑ ({rsi1:.1f}→{rsi2:.1f})",
                )

    # 4. Check Bearish Divergence (so sánh 2 swing highs gần nhất)
    if len(swing_high_indices) >= 2:
        idx2 = swing_high_indices[0]  # Pivot high mới hơn
        idx1 = swing_high_indices[1]  # Pivot high cũ hơn
        
        # Chỉ kích hoạt tại thời điểm pivot mới được xác nhận đầy đủ
        if idx2 == n - min_bars - 1:
            price2 = close_history[idx2]
            rsi2 = rsi_history[idx2]
            price1 = close_history[idx1]
            rsi1 = rsi_history[idx1]
            
            if price2 > price1 and rsi2 < rsi1 - rsi_threshold:
                return Signal(
                    symbol=symbol, type=SignalType.BEARISH_DIVERGENCE, level=SignalLevel.ALERT,
                    tf='15m', close=close_history[-1], rsi14=rsi_history[-1],
                    ema200=None, volume=None,
                    timestamp=timestamp,
                    detail=f"Bearish Divergence: Price Highs ↑ ({price1:.1f}→{price2:.1f}) but RSI Highs ↓ ({rsi1:.1f}→{rsi2:.1f})",
                )

    return None


def check_confluence(
    signal_ema: Optional[Signal],
    signal_rsi: Optional[Signal],
) -> Optional[Signal]:
    if signal_ema is None or signal_rsi is None:
        return None

    is_long = (
        signal_ema.type == SignalType.EMA_SUPPORT_TOUCH and
        signal_rsi.type == SignalType.RSI_OVERSOLD
    )
    is_short = (
        signal_ema.type == SignalType.EMA_RESISTANCE_TOUCH and
        signal_rsi.type == SignalType.RSI_OVERBOUGHT
    )

    sym = signal_rsi.symbol or signal_ema.symbol
    if is_long:
        return Signal(
            symbol=sym, type=SignalType.CONFLUENCE_LONG, level=SignalLevel.HIGH_PRIORITY,
            tf='MTF', close=signal_rsi.close, rsi14=signal_rsi.rsi14,
            ema200=signal_ema.ema200, volume=None,
            timestamp=signal_rsi.timestamp,
            detail=f"{signal_ema.tf} touched EMA200 ({signal_ema.ema200:.0f}) + RSI 15m oversold ({signal_rsi.rsi14:.1f})",
        )
    if is_short:
        return Signal(
            symbol=sym, type=SignalType.CONFLUENCE_SHORT, level=SignalLevel.HIGH_PRIORITY,
            tf='MTF', close=signal_rsi.close, rsi14=signal_rsi.rsi14,
            ema200=signal_ema.ema200, volume=None,
            timestamp=signal_rsi.timestamp,
            detail=f"{signal_ema.tf} touched EMA200 ({signal_ema.ema200:.0f}) + RSI 15m overbought ({signal_rsi.rsi14:.1f})",
        )
    return None


def check_confluence_cross_tf(
    signal_ema: Optional[Signal],
    last_rsi_extreme: Optional[tuple[str, Signal, float]],
) -> Optional[Signal]:
    """Cross-TF confluence: EMA touch on any TF + recent RSI extreme on 15m.

    Khắc phục lỗi same-snapshot coupling: thay vì yêu cầu cả EMA touch và RSI
    extreme cùng xuất hiện trong một vòng lặp snapshot, hàm này dùng RSI extreme
    đã được lưu từ snapshot 15m trước đó để kiểm tra chéo khung thời gian.

    Args:
        signal_ema: EMA touch signal từ bất kỳ TF nào
        last_rsi_extreme: Tuple (type_value, Signal, monotonic_time) từ RSI extreme gần nhất
    """
    if signal_ema is None or last_rsi_extreme is None:
        return None

    rsi_type_str, rsi_signal, rsi_time = last_rsi_extreme
    now = time.monotonic()
    max_age = CONFLUENCE_MAX_AGE_SEC  # cấu hình từ config (.env)

    if now - rsi_time > max_age:
        return None

    is_long = (
        signal_ema.type == SignalType.EMA_SUPPORT_TOUCH and
        rsi_type_str == SignalType.RSI_OVERSOLD.value
    )
    is_short = (
        signal_ema.type == SignalType.EMA_RESISTANCE_TOUCH and
        rsi_type_str == SignalType.RSI_OVERBOUGHT.value
    )

    sym = rsi_signal.symbol or signal_ema.symbol
    if is_long:
        return Signal(
            symbol=sym, type=SignalType.CONFLUENCE_LONG, level=SignalLevel.HIGH_PRIORITY,
            tf='MTF', close=rsi_signal.close, rsi14=rsi_signal.rsi14,
            ema200=signal_ema.ema200, volume=None,
            timestamp=rsi_signal.timestamp,
            detail=f"{signal_ema.tf} touched EMA200 ({signal_ema.ema200:.0f}) + RSI 15m oversold ({rsi_signal.rsi14:.1f})",
        )
    if is_short:
        return Signal(
            symbol=sym, type=SignalType.CONFLUENCE_SHORT, level=SignalLevel.HIGH_PRIORITY,
            tf='MTF', close=rsi_signal.close, rsi14=rsi_signal.rsi14,
            ema200=signal_ema.ema200, volume=None,
            timestamp=rsi_signal.timestamp,
            detail=f"{signal_ema.tf} touched EMA200 ({signal_ema.ema200:.0f}) + RSI 15m overbought ({rsi_signal.rsi14:.1f})",
        )
    return None


# --- Duplicate signal suppression (cooldown) ---
SIGNAL_COOLDOWN_SECONDS = 300  # 5 minutes


class SignalCooldownManager:
    """Encapsulates signal cooldown state to avoid global mutable dict.

    Tracks recently-fired signals by type+timeframe key and suppresses
    duplicates within the configured cooldown window. Prunes stale
    entries when the dict exceeds 100 keys.
    """

    def __init__(self, cooldown_seconds: int = SIGNAL_COOLDOWN_SECONDS,
                 cooldown_mode: str = 'per_type') -> None:
        self._cooldowns: dict[str, float] = {}
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_mode = cooldown_mode  # 'per_type' or 'per_symbol'

    def is_suppressed(self, signal: Signal) -> bool:
        """Check if a signal should be suppressed due to recent duplicate."""
        if self._cooldown_mode == 'per_symbol':
            key = f"{signal.symbol}"
        else:
            key = f"{signal.symbol}_{signal.type.value}_{signal.tf}"
        now = time.monotonic()
        last = self._cooldowns.get(key, 0)
        if now - last < self._cooldown_seconds:
            return True
        self._cooldowns[key] = now
        # Prune old entries selectively — remove only expired entries
        if len(self._cooldowns) > 100:
            cutoff = now - self._cooldown_seconds - 60
            for stale_key in list(self._cooldowns):
                if self._cooldowns[stale_key] < cutoff:
                    del self._cooldowns[stale_key]
        return False
