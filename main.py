"""
main.py — Main entry point of the system.
Connects all modules using Producer-Consumer architecture.

Run: python main.py
"""
import asyncio
import logging
import os
import signal
import structlog
import sys
import time
import uuid
import datetime
from typing import Callable, Coroutine

from core.ingestion      import stream_ohlcv, fetch_historical_ohlcv, fetch_prev_day_levels, fetch_gap_candles
from core.state_manager  import SymbolStateManager, consume_candles
from core.config         import WEB_HOST, WEB_PORT, TIMEFRAMES, SYMBOLS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, validate_startup_config, validate_required_timeframes, CACHE_RETENTION_DAYS, WARMUP_CANDLES, VS_ENABLED, EXCHANGE_ID, CORE_LEGACY_ALERTS_ENABLED
from core.signals        import (
    check_rsi_extremes, check_volume_spike,
    check_ema_touch, check_rsi_divergence, check_confluence,
    check_confluence_cross_tf,
    SignalCooldownManager,
    SignalType,
)
from notifiers.telegram_bot import TelegramNotifier
from ui.web_dashboard       import serve

if VS_ENABLED:
    from VolumeSniper import Scanner

from core.persistence import (
    save_indicator_state, load_indicator_state,
    save_rate_limits, load_rate_limits,
    save_ohlcv_cache, load_ohlcv_cache,
    prune_ohlcv_cache, mark_ohlcv_dirty, save_ohlcv_cache_if_dirty,
)
from core.metrics import QUEUE_SIZE, CANDLES_PROCESSED, SIGNAL_COUNT, WS_CONNECTIONS
from ui.web_dashboard import rebuild_candle_buffer
from core.market_sentiment import derive_trend_from_ohlcv, derive_sentiment_from_news

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
)

handler = logging.StreamHandler()
handler.setFormatter(
    structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
)

logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger('main')

# BUG-C1 FIX: Do NOT create asyncio.Event at module scope.
# It binds to the running loop at construction time; creating it before
# asyncio.run() raises RuntimeError on Python 3.12+.
# Declared here as None; set inside main() where the loop is guaranteed running.
shutdown_event: asyncio.Event | None = None


def _init_signal_handlers(loop, shutdown_ev: asyncio.Event):
    """Register signal handlers cross-platform.
    Unix: loop.add_signal_handler for SIGTERM + SIGINT.
    Windows: signal.signal(SIGINT) only, bridged via call_soon_threadsafe.
    """
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_ev.set)
        logger.info("Registered signal handler (Unix)")
    except (NotImplementedError, AttributeError):
        def _win_handler(signum, frame):
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(shutdown_ev.set)
            except RuntimeError:
                pass
        signal.signal(signal.SIGINT, _win_handler)
        logger.info("Registered SIGINT handler (Windows)")


async def warmup_phase(
    state_mgr: SymbolStateManager,
    ui_queue: asyncio.Queue,
) -> tuple[int, dict]:
    logger.info("=" * 60)
    logger.info("WARMUP: Loading historical data...")
    logger.info("=" * 60)

    loaded_cache = load_ohlcv_cache()
    ohlcv_cache: dict = {}
    total = 0

    async def warmup_single_symbol(symbol: str) -> tuple[str, dict, int]:
        sym_short = symbol.split('/')[0]
        logger.info(f"--- Warming up {symbol} ---")
        symbol_historical = {}
        sym_count = 0
        symbol_cache = loaded_cache.get(symbol, {})

        for tf in TIMEFRAMES:
            cached = symbol_cache.get(tf, [])
            need_full = False
            if not cached:
                need_full = True
            else:
                since_ts = cached[-1]['start_ts']
                now_ms = int(time.time() * 1000)
                gap_ms = now_ms - since_ts
                retention_days = CACHE_RETENTION_DAYS.get(tf, 7)
                if gap_ms < 0 or gap_ms > retention_days * 86400 * 1000:
                    logger.warning(f"Cache for {sym_short} {tf} too old or invalid. Gap={gap_ms/1000/60:.1f}m. Falling back to full fetch.")
                    need_full = True

            if need_full:
                logger.info(f"Fetching full history for {sym_short} {tf}...")
                tf_res = await asyncio.to_thread(fetch_historical_ohlcv, symbol, [tf], WARMUP_CANDLES)
                symbol_historical[tf] = tf_res.get(tf, [])
            else:
                since_ts = cached[-1]['start_ts']
                logger.info(f"Cache hit for {sym_short} {tf}. Fetching gap since {datetime.datetime.fromtimestamp(since_ts/1000).isoformat()}...")
                gap_candles = await asyncio.to_thread(fetch_gap_candles, tf, since_ts, symbol, EXCHANGE_ID)
                new_candles = [c for c in gap_candles if c['start_ts'] > since_ts]
                merged = cached + new_candles
                target_limit = WARMUP_CANDLES.get(tf, 500)
                if len(merged) > target_limit:
                    merged = merged[-target_limit:]
                symbol_historical[tf] = merged

        state = state_mgr.get_state(symbol)
        vol_tracker = state_mgr.get_volume_tracker(symbol)

        all_candles: list[dict] = []
        for tf, candles in symbol_historical.items():
            all_candles.extend(candles)
        all_candles.sort(key=lambda c: c['start_ts'])

        for candle in all_candles:
            snap = state.process_candle(candle)
            if snap is not None:
                sym_count += 1
                vol_tracker.update(candle['tf'], candle['volume'], is_closed=True)
                await ui_queue.put(snap)

        logger.info(f"  {sym_short}: {sym_count} closed candles processed")
        for tf in TIMEFRAMES:
            ema_val = state.ema[tf][-1] if len(state.ema[tf]) > 0 else None
            rsi_val = state.rsi[tf][-1] if len(state.rsi[tf]) > 0 else None
            avg_vol = vol_tracker.get_avg_24h(tf)
            logger.info(
                f"  {sym_short} {tf}: {len(state.ema[tf])} candles | "
                f"EMA200={f'{ema_val:.2f}' if ema_val else 'N/A'} | "
                f"RSI14={f'{rsi_val:.1f}' if rsi_val else 'N/A'} | "
                f"AvgVol24h={avg_vol:.2f}"
            )

        return symbol, symbol_historical, sym_count

    # Warm up all symbols concurrently
    results = await asyncio.gather(*[warmup_single_symbol(symbol) for symbol in SYMBOLS], return_exceptions=False)

    for symbol, symbol_historical, sym_count in results:
        ohlcv_cache[symbol] = symbol_historical
        total += sym_count

    await save_ohlcv_cache(ohlcv_cache)
    logger.info("Saved multi-symbol OHLCV cache")

    # After warmup, send a bulk status update
    await ui_queue.put({'type': 'status', 'text': f'Warmup complete - {total} candles across {len(SYMBOLS)} symbols'})

    # Fetch and push previous day High/Low concurrently
    async def fetch_levels_for_symbol(symbol):
        levels = await asyncio.to_thread(fetch_prev_day_levels, symbol)
        if levels:
            levels['symbol'] = symbol
            await ui_queue.put({'type': 'day_levels', **levels})
            logger.info(f"PrevDay levels for {symbol}: High={levels['high']:.2f} Low={levels['low']:.2f}")

    await asyncio.gather(*[fetch_levels_for_symbol(symbol) for symbol in SYMBOLS])

    # Signal frontend to reload chart data now that warmup is done
    import ui.web_dashboard
    ui.web_dashboard._IS_WARMING_UP = False

    await ui_queue.put({'type': 'warmup_done'})
    logger.info(f"Warmup complete: {total} closed candles processed across {len(SYMBOLS)} symbols")

    return total, ohlcv_cache


async def signal_consumer(
    signal_queue: asyncio.Queue,
    ui_queue: asyncio.Queue,
    state_mgr: SymbolStateManager,
    notifier: TelegramNotifier,
    cooldown: SignalCooldownManager,
) -> None:
    processed = 0
    _last_rsi_extreme = None  # (type_value, signal, monotonic_time) cho cross-TF confluence
    # Fix #3: chỉ track snapshot 15m — snapshot 1m reset liên tục khiến dead code
    # Timeout 15 phút hợp lý vì nến 15m mới 15 phút/lần
    _last_15m_snapshot_time = 0.0
    while True:
        snapshot = await signal_queue.get()
        _now = time.monotonic()
        # Chỉ track và check expiry khi nhận snapshot 15m
        if snapshot.tf == '15m':
            if _last_15m_snapshot_time > 0 and (_now - _last_15m_snapshot_time) > 900:  # 15 phút
                _last_rsi_extreme = None
                logger.debug("Cleared stale _last_rsi_extreme after %.0fs without 15m snapshot", _now - _last_15m_snapshot_time)
            _last_15m_snapshot_time = _now
        trace_id = uuid.uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(trace_id=trace_id, symbol=snapshot.symbol, tf=snapshot.tf)
        try:
            volume_tracker = state_mgr.get_volume_tracker(snapshot.symbol)
            state = state_mgr.get_state(snapshot.symbol)

            CANDLES_PROCESSED.labels(snapshot.symbol, snapshot.tf).inc()
            # Volume Spike fix: tinh avg TRUOC khi update de tranh tu lam nhieu
            avg_vol_24h = volume_tracker.get_avg_24h(snapshot.tf)
            volume_tracker.update(snapshot.tf, snapshot.volume, snapshot.is_closed)

            # Keep chart/indicator state live, but retire the unvalidated RSI,
            # EMA-touch and raw volume alerts in the v2 default configuration.
            if not CORE_LEGACY_ALERTS_ENABLED:
                continue

            s1 = None
            try:
                s1 = check_rsi_extremes(snapshot)
            except Exception as e:
                logger.error(f"check_rsi_extremes failed: {e}", exc_info=True)

            s2 = None
            try:
                s2 = check_volume_spike(snapshot, avg_vol_24h)
            except Exception as e:
                logger.error(f"check_volume_spike failed: {e}", exc_info=True)

            s3 = None
            try:
                s3 = check_ema_touch(snapshot, snapshot.low, snapshot.high)
            except Exception as e:
                logger.error(f"check_ema_touch failed: {e}", exc_info=True)

            s4 = None
            if snapshot.tf == '15m':
                try:
                    s4 = check_rsi_divergence(
                        state.close_history['15m'],
                        state.rsi_history['15m'],
                        snapshot.timestamp,
                        symbol=snapshot.symbol,
                    )
                except Exception as e:
                    logger.error(f"check_rsi_divergence failed: {e}", exc_info=True)

            # Track RSI extreme for cross-TF confluence
            if s1 is not None:
                _last_rsi_extreme = (s1.type.value, s1, time.monotonic())

            s5 = None
            try:
                s5 = check_confluence(s3, s1)
            except Exception as e:
                logger.error(f"check_confluence failed: {e}", exc_info=True)
            # Cross-TF confluence: EMA touch on any TF + recent RSI extreme from 15m
            if s5 is None:
                try:
                    s5 = check_confluence_cross_tf(s3, _last_rsi_extreme)
                except Exception as e:
                    logger.error(f"check_confluence_cross_tf failed: {e}", exc_info=True)

            for sig in [s1, s2, s3, s4, s5]:
                if sig is None:
                    continue
                # Suppress duplicate signals within cooldown window
                if cooldown.is_suppressed(sig):
                    logger.debug(f"Signal suppressed (cooldown): {sig.symbol} {sig.type.value}/{sig.tf}")
                    continue
                try:
                    SIGNAL_COUNT.labels(sig.symbol, sig.type.value, sig.tf).inc()
                    await notifier.send_signal(sig)
                    await ui_queue.put({
                        'type':       'signal',
                        'symbol':     sig.symbol,
                        'signal_type': sig.type.value,
                        'level':      sig.level.name,
                        'tf':         sig.tf,
                        'close':      sig.close,
                        'rsi14':      sig.rsi14,
                        'ema200':     sig.ema200,
                        'timestamp':  sig.timestamp,
                        'detail':     sig.detail,
                    })
                    state.add_signal(sig.type.value, sig.tf, sig.detail)
                except Exception as e:
                    logger.error("Error processing signal %s/%s/%s: %s", sig.symbol, sig.type.value, sig.tf, e, exc_info=True)

            processed += 1
            if processed % 100 == 0:
                logger.info(f"Heartbeat: {processed} candles | {snapshot.symbol} RSI15m={snapshot.rsi14}")
        except Exception as e:
            logger.error(f"Error in signal_consumer: {e}", exc_info=True)
        finally:
            signal_queue.task_done()
            try:
                structlog.contextvars.unbind_contextvars('trace_id', 'symbol', 'tf')
            except Exception:
                pass


async def update_metrics(candle_queue, signal_queue, ui_queue):
    """Update Prometheus queue-size gauges every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        QUEUE_SIZE.labels('candles').set(candle_queue.qsize())
        QUEUE_SIZE.labels('signals').set(signal_queue.qsize())
        QUEUE_SIZE.labels('ui').set(ui_queue.qsize())


async def periodic_save_indicators(state_mgr):
    """Save indicator state every 60 seconds (D-08)."""
    while True:
        await asyncio.sleep(60)
        if not await save_indicator_state(state_mgr):
            logger.error("Failed to save indicator state")


async def periodic_save_ohlcv(cache):
    """Save OHLCV cache every 300 seconds (D-08)."""
    while True:
        await asyncio.sleep(300)
        prune_ohlcv_cache(cache, CACHE_RETENTION_DAYS)
        if not await save_ohlcv_cache_if_dirty(cache):
            logger.error("Failed to save OHLCV cache")


async def periodic_day_levels(ui_queue):
    """Check every 60s if day changed; push new prev-day High/Low to UI."""
    last_start_ts: dict[str, int] = {}
    while True:
        await asyncio.sleep(60)
        try:
            for symbol in SYMBOLS:
                levels = await asyncio.to_thread(fetch_prev_day_levels, symbol)
                if levels and levels.get('start_ts', 0) != last_start_ts.get(symbol, 0):
                    last_start_ts[symbol] = levels['start_ts']
                    levels['symbol'] = symbol
                    await ui_queue.put({'type': 'day_levels', **levels})
                    logger.info(f"Day levels updated for {symbol}: High={levels['high']:.2f} Low={levels['low']:.2f}")
        except Exception as e:
            logger.error("periodic_day_levels error: %s", e, exc_info=True)


async def shutdown_save_all(state_mgr, notifier, ohlcv_cache):
    """Save all state on graceful shutdown."""
    logger.info("Saving state...")
    if not await save_indicator_state(state_mgr):
        logger.error("Luu indicator state that bai")
    if not await save_rate_limits(notifier._last_sent):
        logger.error("Luu rate limits that bai")
    if not await save_ohlcv_cache_if_dirty(ohlcv_cache):
        logger.error("Luu OHLCV cache that bai")
    logger.info("All state saved")


class TaskSupervisor:
    """Monitors and restarts critical asyncio tasks on failure."""
    def __init__(self, logger):
        self._tasks: dict[str, asyncio.Task] = {}
        self._factories: dict[str, callable] = {}
        self._logger = logger

    def add(self, name: str, factory) -> None:
        self._factories[name] = factory
        self._start(name)

    def _start(self, name: str) -> None:
        factory = self._factories[name]
        self._tasks[name] = asyncio.create_task(self._run(name, factory), name=name)

    async def _run(self, name: str, factory) -> None:
        while True:
            try:
                await factory()
                self._logger.warning(f"Task '{name}' finished normally. Restarting in 5s...")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"Task '{name}' crashed: {e}", exc_info=True)
                self._logger.info(f"Restarting task '{name}' in 5s...")
                await asyncio.sleep(5)

    async def shutdown(self) -> None:
        for name, task in self._tasks.items():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)


async def main() -> None:
    global shutdown_event
    # BUG-C1 FIX: Create asyncio.Event inside the running loop (Python 3.12+ safe)
    shutdown_event = asyncio.Event()

    candle_queue = asyncio.Queue(maxsize=10000)
    signal_queue = asyncio.Queue(maxsize=500)
    ui_queue     = asyncio.Queue(maxsize=20000)

    _init_signal_handlers(asyncio.get_running_loop(), shutdown_event)

    errors = validate_startup_config()
    if errors:
        for err in errors:
            logger.critical(err)
        sys.exit(1)

    warnings = validate_required_timeframes()
    if warnings:
        for warn in warnings:
            logger.warning(warn)

    cooldown = SignalCooldownManager()
    state_mgr = SymbolStateManager()
    notifier = TelegramNotifier(token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID)
    ohlcv_cache: dict = {}

    vs_scanner = None
    if VS_ENABLED:
        vs_scanner = Scanner(ui_queue=ui_queue)

    logger.info("=" * 60)
    logger.info("Crypto Signal Tracker — Starting up")
    logger.info(f"Symbols: {SYMBOLS}")
    logger.info(f"Timeframes: {TIMEFRAMES}")
    logger.info(f"Telegram: {'configured' if TELEGRAM_BOT_TOKEN else 'NOT configured'}")
    logger.info(f"Web UI: http://{WEB_HOST}:{WEB_PORT}")
    logger.info("=" * 60)

    server_task = asyncio.create_task(serve(ui_queue, candle_queue, signal_queue, state=state_mgr, scanner=vs_scanner))
    await asyncio.sleep(0.5)

    logger.info("Starting warmup — loading historical data...")
    _, ohlcv_cache = await warmup_phase(state_mgr, ui_queue)

    # Note: CANDLE_BUFFER is already scaled to hold all symbols' data
    # (set in serve() as MAX_CANDLE_BUFFER_PER_TF * len(SYMBOLS))
    # Warmup broadcasts already stored full IndicatorSnapshot data (ema200, rsi14)
    # No rebuild needed — data is preserved with indicators

    logger.info("=" * 60)
    logger.info("Switching to real-time mode (WebSocket)")
    logger.info(f"Bot running — visit http://{WEB_HOST}:{WEB_PORT} to view")
    logger.info(f"Tracking {len(SYMBOLS)} symbols: {', '.join(SYMBOLS)}")
    logger.info("=" * 60)

    await ui_queue.put({'type': 'status', 'text': 'Real-time mode active'})

    supervisor = TaskSupervisor(logger)
    # Spawn one ingestion stream per symbol
    for symbol in SYMBOLS:
        sym_safe = symbol.replace('/', '_').replace(':', '_')
        supervisor.add(f'stream_ohlcv_{sym_safe}', lambda sym=symbol: stream_ohlcv(candle_queue, symbol=sym))
    supervisor.add('consume_candles', lambda: consume_candles(candle_queue, state_mgr, signal_queue, ui_queue))
    supervisor.add('signal_consumer', lambda: signal_consumer(signal_queue, ui_queue, state_mgr, notifier, cooldown))
    supervisor.add('periodic_save_indicators', lambda: periodic_save_indicators(state_mgr))
    supervisor.add('periodic_save_ohlcv', lambda: periodic_save_ohlcv(ohlcv_cache))
    supervisor.add('update_metrics', lambda: update_metrics(candle_queue, signal_queue, ui_queue))
    supervisor.add('periodic_day_levels', lambda: periodic_day_levels(ui_queue))

    if VS_ENABLED and vs_scanner:
        supervisor.add('volume_sniper_scanner', lambda: vs_scanner.run())

    try:
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [shutdown_task, server_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task in done:
            logger.info("Received shutdown signal -- starting shutdown...")
        else:
            exc = server_task.exception()
            if exc:
                logger.exception(f"Server task crashed: {exc}")
    finally:
        if VS_ENABLED and vs_scanner:
            try:
                await vs_scanner.stop()
            except Exception as e:
                logger.error(f"Error stopping VolumeSniper scanner: {e}")
        await supervisor.shutdown()
        await shutdown_save_all(state_mgr, notifier, ohlcv_cache)
        logger.info("Shutting down...")
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)
        try:
            await notifier.close()
        except Exception:
            logger.debug("Telegram bot close error during shutdown (harmless)")
        logger.info("System shut down safely")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Dừng hệ thống theo yêu cầu người dùng")
