"""
File-based state persistence for indicator state, seen hashes,
rate-limit cooldowns, and OHLCV cache.

Atomic write pattern: write to .tmp, os.replace to target.
"""

import asyncio
import json
import os
import logging
import time

from core.config import DATA_DIR, TIMEFRAMES, CACHE_RETENTION_DAYS

logger = logging.getLogger(__name__)

FILES = {
    'indicator': 'indicator_state.json',
    'hashes':    'seen_hashes.json',
    'ratelimit': 'rate_limits.json',
    'ohlcv':     'ohlcv_cache.json',
    'signals':   'signal_buffer.json',
}


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, FILES[name])


async def _atomic_save(filepath: str, data) -> bool:
    """Write data to file atomically: tmp -> replace. Runs I/O in executor."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp = filepath + '.tmp'
    loop = asyncio.get_running_loop()
    try:
        def _write():
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            # BUG-L4 NOTE (Windows): os.replace() can fail with PermissionError if the
            # target file is locked by antivirus or another process. The except block
            # below tries to remove the .tmp file on failure, but if the replace() call
            # itself raises BEFORE the except is reached (e.g., due to a process kill),
            # .tmp files can accumulate in the data/ directory. Low severity for
            # typical deployment, but worth monitoring with: dir data\\*.tmp
            os.replace(tmp, filepath)
        await loop.run_in_executor(None, _write)
        return True
    except (IOError, OSError, TypeError) as e:
        logger.error("_atomic_save failed: %s", e, exc_info=True)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _atomic_load(filepath: str):
    """Load data from file, return None if missing or corrupt."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning(f"Cannot read {filepath}, starting fresh")
        return None


# --- Indicator state save/restore (PERS-01) ---

async def save_indicator_state(state_mgr) -> bool:
    """Capture talipp EMA/RSI input values and VolumeTracker state per symbol."""
    data: dict = {}
    for symbol in state_mgr.symbols:
        state = state_mgr.get_state(symbol)
        vol_tracker = state_mgr.get_volume_tracker(symbol)
        data[symbol] = {
            'volume_tracker': {
                'window': vol_tracker._window,
                'volumes': {tf: list(v) for tf, v in vol_tracker._volumes.items()},
                'current': dict(vol_tracker._current),
            },
            'indicators': {
                tf: {
                    'ema_inputs': list(state.ema[tf].input_values),
                    'rsi_inputs': list(state.rsi[tf].input_values),
                }
                for tf in TIMEFRAMES
            },
        }
    return await _atomic_save(_path('indicator'), data)


def load_indicator_state():
    """Load saved indicator state, returns dict keyed by symbol or None."""
    return _atomic_load(_path('indicator'))


# --- Seen hashes save/restore (PERS-02) ---

async def save_seen_hashes(hashes: set) -> bool:
    return await _atomic_save(_path('hashes'), list(hashes))


def load_seen_hashes() -> set:
    data = _atomic_load(_path('hashes'))
    return set(data) if isinstance(data, list) else set()


# --- Rate-limit state save/restore (PERS-03) ---

async def save_rate_limits(last_sent: dict) -> bool:
    return await _atomic_save(_path('ratelimit'), last_sent)


def load_rate_limits() -> dict:
    data = _atomic_load(_path('ratelimit'))
    return data if isinstance(data, dict) else {}


# --- OHLCV cache save/restore (PERS-04) ---

async def save_ohlcv_cache(cache: dict) -> bool:
    return await _atomic_save(_path('ohlcv'), cache)


def load_ohlcv_cache() -> dict:
    data = _atomic_load(_path('ohlcv'))
    return data if isinstance(data, dict) else {}


# --- Signal buffer save/restore (PERS-05) ---


async def save_signal_buffer(buf: list) -> bool:
    return await _atomic_save(_path('signals'), buf)


def load_signal_buffer() -> list:
    data = _atomic_load(_path('signals'))
    return data if isinstance(data, list) else []


# --- OHLCV cache pruning (DATA-01) ---

# BUG-C1 FIX: Do NOT create asyncio primitives at module scope.
# asyncio.Event/Lock bind to the running event loop at construction time.
# Creating them before asyncio.run() starts (Python 3.12+) raises RuntimeError.
# Use lazy singleton factories instead.
_ohlcv_save_event: asyncio.Event | None = None
_ohlcv_save_lock: asyncio.Lock | None = None


def _get_ohlcv_event() -> asyncio.Event:
    global _ohlcv_save_event
    if _ohlcv_save_event is None:
        _ohlcv_save_event = asyncio.Event()
    return _ohlcv_save_event


def _get_ohlcv_lock() -> asyncio.Lock:
    global _ohlcv_save_lock
    if _ohlcv_save_lock is None:
        _ohlcv_save_lock = asyncio.Lock()
    return _ohlcv_save_lock


def mark_ohlcv_dirty() -> None:
    """Mark OHLCV cache as needing a save."""
    _get_ohlcv_event().set()


async def save_ohlcv_cache_if_dirty(cache: dict) -> bool:
    """Only save OHLCV cache if data has changed since last save."""
    async with _get_ohlcv_lock():
        if not _get_ohlcv_event().is_set():
            return True
        result = await save_ohlcv_cache(cache)
        if result:
            _get_ohlcv_event().clear()
        return result


def prune_ohlcv_cache(cache: dict, retention_days: dict[str, int]) -> None:
    now_ms = int(time.time() * 1000)
    pruned = False
    # cache format: {symbol: {tf: [candles]}}
    for symbol in list(cache.keys()):
        tf_dict = cache[symbol]
        if not isinstance(tf_dict, dict):
            continue
        for tf in list(tf_dict.keys()):
            days = retention_days.get(tf)
            if days is None:
                continue
            cutoff_ms = now_ms - (days * 86400 * 1000)
            candles = tf_dict[tf]
            old_len = len(candles)
            tf_dict[tf] = [c for c in candles if c['start_ts'] >= cutoff_ms]
            if len(tf_dict[tf]) < old_len:
                pruned = True
    if pruned:
        mark_ohlcv_dirty()
