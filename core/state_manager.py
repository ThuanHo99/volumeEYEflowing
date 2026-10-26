import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from talipp.indicators import EMA, RSI

from core.config import MAX_CANDLES_PER_TF as MAX_CANDLES, TIMEFRAMES

logger = logging.getLogger(__name__)


@dataclass
class IndicatorSnapshot:
    symbol:    str = ''
    tf:         str = ''
    timestamp:  int = 0
    open:       float = 0.0
    high:       float = 0.0
    low:        float = 0.0
    close:      float = 0.0
    ema200:     Optional[float] = None
    rsi14:      Optional[float] = None
    volume:     float = 0.0
    is_closed:  bool = False


class VolumeTracker:
    """Tracks 24h volume for Volume Spike signal."""
    def __init__(self, window: int = 24):
        self._window = window
        # BUG-M2 FIX: Use deque(maxlen=window) instead of list + pop(0).
        # list.pop(0) is O(n) because it shifts all remaining elements left.
        # deque with maxlen automatically evicts the oldest entry in O(1).
        self._volumes: dict[str, deque] = {}  # tf -> deque of closed candle volumes
        self._current: dict[str, float] = {}  # tf -> current live candle volume

    def _ensure_tf(self, tf: str) -> None:
        if tf not in self._volumes:
            self._volumes[tf] = deque(maxlen=self._window)
            self._current[tf] = 0.0

    def update(self, tf: str, volume: float, is_closed: bool) -> None:
        self._ensure_tf(tf)
        if is_closed:
            self._volumes[tf].append(volume)  # deque auto-evicts oldest at maxlen
            self._current[tf] = 0.0
        else:
            self._current[tf] = volume

    def get_avg_24h(self, tf: str) -> float:
        self._ensure_tf(tf)
        vols = self._volumes.get(tf, deque())
        if not vols:
            return 0.0
        return sum(vols) / len(vols)


class IndicatorState:
    def __init__(self, timeframes: list[str] | None = None):
        if timeframes is None:
            timeframes = TIMEFRAMES
        self._timeframes = timeframes
        self.ema: dict[str, EMA] = {tf: EMA(200) for tf in timeframes}
        self.rsi: dict[str, RSI] = {tf: RSI(14)  for tf in timeframes}
        self._last_volume: dict[str, float] = {tf: 0.0 for tf in timeframes}
        # BUG-M3 FIX: Use deque(maxlen=20) instead of list + pop(0) for O(1) eviction.
        # list.pop(0) shifts all elements — O(n) per closed candle on every TF per symbol.
        self.rsi_history:   dict[str, deque] = {tf: deque(maxlen=20) for tf in timeframes}
        self.close_history: dict[str, deque] = {tf: deque(maxlen=20) for tf in timeframes}
        self.volume_history: dict[str, deque] = {tf: deque(maxlen=20) for tf in timeframes}
        self.recent_signals:   list[dict] = []
        self._current_candle_ts: dict[str, int] = {tf: -1 for tf in timeframes}

    def process_candle(self, candle: dict) -> Optional[IndicatorSnapshot]:
        tf    = candle['tf']
        close = candle['close']
        vol   = candle['volume']
        ts    = candle['start_ts']

        self._last_volume[tf] = vol

        if ts != self._current_candle_ts[tf]:
            self.ema[tf].add(close)
            self.rsi[tf].add(close)
            self._current_candle_ts[tf] = ts
        else:
            self.ema[tf].update(close)
            self.rsi[tf].update(close)

        if not candle['is_closed']:
            return None

        rsi_val = self.rsi[tf][-1]
        if rsi_val is not None:
            # BUG-M3 FIX: deque(maxlen=20) auto-evicts oldest — no pop(0) needed.
            self.rsi_history[tf].append(float(rsi_val))
            self.close_history[tf].append(close)
            self.volume_history[tf].append(vol)

        # maxlen=20 on deque handles trimming automatically — removed manual pop(0) calls

        if len(self.ema[tf]) > MAX_CANDLES:
            n = 50
            self.ema[tf].purge_oldest(n)
            self.rsi[tf].purge_oldest(n)
            logger.debug(f"Purged {n} oldest entries for {tf}")

        return self.get_snapshot(tf, candle)

    def add_signal(self, signal_type: str, tf: str, detail: str = "") -> None:
        """Append a signal to recent_signals FIFO buffer (max 50)."""
        self.recent_signals.append({
            'type': signal_type,
            'tf': tf,
            'detail': detail,
            'timestamp': time.time(),
        })
        if len(self.recent_signals) > 50:
            self.recent_signals.pop(0)

    def get_snapshot(self, tf: str, candle: dict) -> IndicatorSnapshot:
        ema_val = self.ema[tf][-1]
        rsi_val = self.rsi[tf][-1]

        return IndicatorSnapshot(
            symbol    = candle.get('symbol', ''),
            tf        = tf,
            timestamp = candle['start_ts'],
            open      = candle['open'],
            high      = candle['high'],
            low       = candle['low'],
            close     = candle['close'],
            ema200    = float(ema_val) if ema_val is not None else None,
            rsi14     = float(rsi_val) if rsi_val is not None else None,
            volume    = candle['volume'],
            is_closed = candle['is_closed'],
        )


class SymbolStateManager:
    """Manages per-symbol IndicatorState and VolumeTracker instances.

    Auto-creates state on first access per symbol.
    """
    def __init__(self, timeframes: list[str] | None = None):
        self._timeframes = timeframes
        self._states: dict[str, IndicatorState] = {}
        self._volume_trackers: dict[str, VolumeTracker] = {}

    def get_state(self, symbol: str) -> IndicatorState:
        if symbol not in self._states:
            self._states[symbol] = IndicatorState(self._timeframes)
            logger.info(f"Created IndicatorState for {symbol}")
        return self._states[symbol]

    def get_volume_tracker(self, symbol: str) -> VolumeTracker:
        if symbol not in self._volume_trackers:
            self._volume_trackers[symbol] = VolumeTracker(window=24)
            logger.info(f"Created VolumeTracker for {symbol}")
        return self._volume_trackers[symbol]

    @property
    def symbols(self) -> list[str]:
        return list(self._states.keys())


async def consume_candles(
    queue:        asyncio.Queue,
    state_mgr:    SymbolStateManager,
    signal_queue: asyncio.Queue,
    ui_queue:     asyncio.Queue,
) -> None:
    while True:
        candle   = await queue.get()
        sym      = candle.get('symbol', '')
        state    = state_mgr.get_state(sym)
        snapshot = state.process_candle(candle)

        if snapshot is not None:
            await signal_queue.put(snapshot)
            await ui_queue.put(snapshot)
            logger.debug(f"[{sym}][{snapshot.tf}] close={snapshot.close:.2f} "
                         f"EMA200={snapshot.ema200} RSI14={snapshot.rsi14}")
        else:
            # Open candle: still push to UI for realtime chart display
            ui_snap = state.get_snapshot(candle['tf'], candle)
            await ui_queue.put(ui_snap)

        queue.task_done()
