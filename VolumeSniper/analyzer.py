"""
analyzer.py - VolumeSniper

Phân tích xu hướng sau khi phát hiện volume spike.
Trả về nhận định: LONG / SHORT / NEUTRAL + lý do chi tiết.

--- Thay đổi so với bản gốc ---

P1 — RSI consistency fix:
  Bản gốc dùng `list(close_prices) + [current_price]` cho mọi trường hợp,
  khiến RSI "đi trước" 1 nến khi live candle chưa đóng.
  Giờ nhận thêm `is_closed` — nếu nến đã đóng (hoặc REQUIRE_CLOSED_CANDLE=True),
  current_price đã được append vào close_prices trước khi analyzer được gọi,
  nên không append lại. Giữ nhất quán giữa RSI baseline và volume baseline.

P3 — Asymmetric RSI thresholds (backtesting 478 signals):
  LONG safe zone : RSI 50–82
    - RSI < 50 → SKIP (LONG RSI 40-50: GOOD=8%, BAD=33% — worst bucket)
    - RSI > 82 → caution/reversal (không hard-reject vì GOOD=40% ở 70-80)
  SHORT safe zone: RSI 18–50
    - RSI > 50 → SKIP (SHORT RSI 60-70: BAD rate tăng mạnh không có edge)
    - SHORT + UPTREND tốt hơn DOWNTREND (counter-trend bounce)
  EMA NEUTRAL tốt nhất cho LONG (BAD=0%, n=53) → boost confidence.
  config.RSI_TREND_LIMIT đổi từ 65→50, RSI_EXTREME từ 80→82.
  CONFIRM_CANDLE_MIN_PCT đổi từ 0.2→0.3 (71% GOOD, BAD=5%).

P2 — Rename sideways semantic:
  `sideways_ok` → `in_accumulation` để phản ánh đúng ý nghĩa:
  True = giá đang trong vùng tích lũy (range nhỏ) = đủ điều kiện alert.
  Tránh nhầm lẫn khi đọc `if not analysis.sideways_ok: skip`.
"""

import logging
import numpy as np
from typing import Optional
from dataclasses import dataclass

from . import config

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    signal:     str            # 'LONG' | 'SHORT' | 'NEUTRAL' | 'SKIP'
    confidence: str            # 'HIGH' | 'MEDIUM' | 'LOW'
    zone:       str            # 'TREND' | 'REVERSAL' | 'RISKY' | 'SIDEWAYS'
    ema_trend:  str            # 'UPTREND' | 'DOWNTREND' | 'NEUTRAL'
    rsi:        Optional[float]
    ema_fast:   Optional[float]
    ema_slow:   Optional[float]
    price_change:    float     # % change in current candle
    spike_ratio:     float

    # P2: renamed từ sideways_ok → in_accumulation
    # True = giá đang tích lũy (BB Width <= BB_WIDTH_MAX_PCT) → ok để alert
    in_accumulation:       bool
    bb_width_pct:          float

    reasons: list[str]

    # --- Backward-compat property (xoá sau khi mày update Scanner + Telegram) ---
    @property
    def sideways_ok(self) -> bool:
        """Deprecated alias — dùng in_accumulation."""
        return self.in_accumulation

    @property
    def sideways_range_pct(self) -> float:
        """Deprecated alias — dùng bb_width_pct."""
        return self.bb_width_pct


class MarketAnalyzer:
    """
    Phân tích thị trường sau khi volume spike được phát hiện.

    Input: close prices + OHLC của nến hiện tại + spike_ratio + is_closed flag
    """

    def analyze(
        self,
        close_prices:  list[float],
        open_price:    float,
        high_price:    float,
        low_price:     float,
        current_price: float,
        spike_ratio:   float,
        is_closed:     bool = True,
        bb_width:      float = 0.0,
    ) -> AnalysisResult:
        reasons = []

        # P1: Nếu nến đã đóng, current_price đã nằm trong close_prices
        # (được append bởi process_kline trước khi gọi analyzer).
        # Nếu live candle (REQUIRE_CLOSED_CANDLE=False), thêm vào để RSI phản chiếu
        # giá hiện tại — nhưng chỉ khi chưa có trong deque.
        if is_closed:
            # Nến đã đóng: close_prices đã có current_price ở cuối deque
            prices = list(close_prices)
        else:
            # Live candle: append current_price tạm thời cho tính toán RSI
            prices = list(close_prices) + [current_price]

        # --- Indicators ---
        rsi      = _calc_rsi(prices, config.RSI_PERIOD)
        ema_fast = _calc_ema(prices, config.EMA_FAST)
        ema_slow = _calc_ema(prices, config.EMA_SLOW)

        # --- EMA Trend ---
        ema_trend = _ema_trend(ema_fast, ema_slow)

        # --- Price change % trong nến ---
        price_chg = ((current_price - open_price) / open_price * 100) if open_price > 0 else 0.0
        raw_signal = 'LONG' if price_chg > 0 else 'SHORT'

        # --- P2: Accumulation check (Bollinger Bands Squeeze) ---
        in_accumulation = True
        bb_width_pct    = bb_width
        if config.BB_SQUEEZE_ENABLED and bb_width_pct > 0:
            in_accumulation = bb_width_pct <= config.BB_WIDTH_MAX_PCT
            if not in_accumulation:
                reasons.append(
                    f"⚠️ Chưa đủ nén (BBW {bb_width_pct:.1f}% > {config.BB_WIDTH_MAX_PCT}%)"
                )

        # --- Wick analysis ---
        candle_range    = high_price - low_price
        upper_wick_pct  = ((high_price - max(current_price, open_price)) / candle_range) if candle_range > 0 else 0
        lower_wick_pct  = ((min(current_price, open_price) - low_price)  / candle_range) if candle_range > 0 else 0

        # --- 3-Zone Decision ---
        signal, zone, confidence = self._three_zone_decision(
            raw_signal, rsi, ema_trend, spike_ratio,
            upper_wick_pct, lower_wick_pct, reasons
        )

        return AnalysisResult(
            signal=signal,
            confidence=confidence,
            zone=zone,
            ema_trend=ema_trend,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            price_change=price_chg,
            spike_ratio=spike_ratio,
            in_accumulation=in_accumulation,
            bb_width_pct=bb_width_pct,
            reasons=reasons
        )

    # ------------------------------------------------------------------
    # 3-ZONE DECISION FRAMEWORK
    # ------------------------------------------------------------------

    def _three_zone_decision(
        self,
        raw_signal:     str,
        rsi:            Optional[float],
        ema_trend:      str,
        spike_ratio:    float,
        upper_wick_pct: float,
        lower_wick_pct: float,
        reasons:        list,
    ) -> tuple[str, str, str]:
        """
        Zone 1 - TREND    : RSI trong vùng an toàn, wick nhỏ → theo trend
        Zone 2 - RISKY    : RSI ngoài safe zone nhưng chưa cực đoan → SKIP/CAUTION
        Zone 3 - REVERSAL : RSI cực đoan + dấu hiệu đảo chiều (wick/climax) → ngược trend
        """
        R   = rsi if rsi is not None else 50.0
        OB  = config.RSI_OVERBOUGHT  # 75-80
        OS  = config.RSI_OVERSOLD    # 20-25
        WR  = config.WICK_REJECT_PCT # 0.45
        
        # Ngưỡng tối thiểu để đánh Trend
        LONG_RSI_MIN  = config.RSI_TREND_LIMIT      # 50
        SHORT_RSI_MAX = 100 - config.RSI_TREND_LIMIT # 50

        if raw_signal == 'LONG':
            # Các điều kiện cho LONG
            cond_ob      = R >= OB
            cond_weak    = R < LONG_RSI_MIN
            cond_wick    = upper_wick_pct > WR
            cond_climax  = spike_ratio >= 15.0 # Climax volume

            # --- Zone 3: REVERSAL (LONG Spike at Overbought + Rejection) ---
            # Nếu giá đang quá mua mà có râu trên dài hoặc vol cực đại -> Sợ đảo chiều
            if cond_ob and (cond_wick or cond_climax or R > 90):
                reasons.append(f"🔄 REVERSAL: RSI={R:.0f} (Overbought) + Rejection/Climax")
                return 'SHORT', 'REVERSAL', 'HIGH'

            # --- Zone 2: RISKY ---
            if cond_weak:
                reasons.append(f"🛑 SKIP: RSI={R:.0f} < {LONG_RSI_MIN} (momentum yếu)")
                return 'SKIP', 'RISKY', 'LOW'
            
            if cond_ob:
                reasons.append(f"⚠️ RISKY: RSI={R:.0f} (Overbought) - Trend climax?")
                return 'LONG', 'RISKY', 'MEDIUM'

            # --- Zone 1: TREND ---
            if cond_wick:
                reasons.append(f"🛑 SKIP: Wick rejection {upper_wick_pct:.1%}")
                return 'SKIP', 'RISKY', 'LOW'

            reasons.append(f"✅ TREND: RSI={R:.0f} ({_ema_label(ema_trend)})")
            # BUG-L5 NOTE: Intentional asymmetry — do NOT change without re-running backtest.
            # Backtest result (478 signals): EMA NEUTRAL = best zone for LONG (BAD=0%, n=53).
            # NEUTRAL means price is between EMA_FAST and EMA_SLOW — classic accumulation.
            # UPTREND/DOWNTREND EMAs have slightly higher BAD rates for LONG signals.
            conf = 'HIGH' if ema_trend == 'NEUTRAL' else 'MEDIUM'
            return 'LONG', 'TREND', conf

        else:  # SHORT Spike
            cond_os      = R <= OS
            cond_weak    = R > SHORT_RSI_MAX
            cond_wick    = lower_wick_pct > WR
            cond_climax  = spike_ratio >= 15.0

            # --- Zone 3: REVERSAL (SHORT Spike at Oversold + Rejection) ---
            if cond_os and (cond_wick or cond_climax or R < 10):
                reasons.append(f"🔄 REVERSAL: RSI={R:.0f} (Oversold) + Rejection/Climax")
                return 'LONG', 'REVERSAL', 'HIGH'

            # --- Zone 2: RISKY ---
            if cond_weak:
                reasons.append(f"🛑 SKIP: RSI={R:.0f} > {SHORT_RSI_MAX} (momentum yếu)")
                return 'SKIP', 'RISKY', 'LOW'
            
            if cond_os:
                reasons.append(f"⚠️ RISKY: RSI={R:.0f} (Oversold) - Price exhaustion?")
                return 'SHORT', 'RISKY', 'MEDIUM'

            # --- Zone 1: TREND ---
            if cond_wick:
                reasons.append(f"🛑 SKIP: Lower wick rejection {lower_wick_pct:.1%}")
                return 'SKIP', 'RISKY', 'LOW'

            reasons.append(f"✅ TREND: RSI={R:.0f} ({_ema_label(ema_trend)})")
            # BUG-L5 NOTE: Intentional asymmetry — SHORT has best results when market is
            # in UPTREND (counter-trend bounce). This is the opposite of LONG logic above.
            # Backtest (478 signals): SHORT + UPTREND showed better win rate than SHORT + NEUTRAL.
            conf = 'HIGH' if ema_trend == 'UPTREND' else 'MEDIUM'
            return 'SHORT', 'TREND', conf


# ------------------------------------------------------------------
# INDICATOR FUNCTIONS (tối ưu chính xác)
# ------------------------------------------------------------------

def _calc_rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """
    RSI theo phương pháp Wilder (Smoothed Moving Average).
    Chính xác hơn simple rolling mean vì đây là chuẩn gốc của Wilder.
    """
    if len(prices) < period + 1:
        return None
    try:
        arr    = np.array(prices[-min(len(prices), period * 3):], dtype=float)
        deltas = np.diff(arr)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Seed với simple average cho period đầu
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        # Wilder smoothing cho phần còn lại
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 50.0 if avg_gain == 0 else 100.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 2)
    except Exception:
        return None


def _calc_ema(prices: list[float], period: int) -> Optional[float]:
    """EMA với adjust=False (exponential weighting chuẩn)."""
    if len(prices) < period:
        return None
    try:
        k    = 2 / (period + 1)
        data = prices[-period * 2:]  # đủ dữ liệu để ema ổn định
        ema  = data[0]
        for p in data[1:]:
            ema = p * k + ema * (1 - k)
        return round(ema, 8)
    except Exception:
        return None


def _ema_trend(ema_fast: Optional[float], ema_slow: Optional[float]) -> str:
    if ema_fast is None or ema_slow is None:
        return 'NEUTRAL'
    if ema_fast > ema_slow * 1.002:
        return 'UPTREND'
    if ema_fast < ema_slow * 0.998:
        return 'DOWNTREND'
    return 'NEUTRAL'


def _ema_label(trend: str) -> str:
    return {'UPTREND': 'EMA↑', 'DOWNTREND': 'EMA↓', 'NEUTRAL': 'EMA≈'}.get(trend, '')