"""
scanner.py - VolumeSniper

WebSocket scanner: kết nối Binance kline streams, điều phối toàn bộ pipeline.

Tại sao dùng @kline thay @aggTrade?
- @kline cung cấp OHLC + volume của nến hiện tại trong 1 message
- @aggTrade chỉ có price + qty từng lệnh → phải tự tích lũy volume
  và không có high/low thực → wick analysis không chính xác

Stream format: <symbol>@kline_<interval>
Binance limit: ~200 streams / connection → chia batch

--- Thay đổi so với bản gốc ---

P1 — Truyền is_closed xuống analyzer:
  _on_spike nhận thêm is_closed từ KlineState, truyền vào analyzer.analyze()
  để RSI không dùng current_price trùng lặp khi nến đã đóng.

P3 — Alert batching:
  AlertBatcher gom các spike xảy ra trong ALERT_BATCH_WINDOW_SEC giây
  vào 1 message duy nhất. Ngăn spam Telegram khi market-wide event.
  Tối đa ALERT_BATCH_MAX symbols/message. Nếu ALERT_BATCH_WINDOW_SEC=0,
  fallback về hành vi cũ (gửi ngay từng alert).

SIGNAL FILTER — Confirm candle:
  Sau khi detect spike, lưu vào _pending_spikes thay vì alert ngay.
  Khi candle 5m tiếp theo đóng (is_closed=True), tính pnl so với
  giá lúc spike. Chỉ alert nếu pnl >= CONFIRM_CANDLE_MIN_PCT (0.2%).
  Dựa trên backtesting 460 signals: 80% BAD signals có pnl_5m < 0,
  filter này nâng precision từ 30% → 57-68%.

CHART — Tạo ảnh chart từ ohlc_history và gửi kèm alert.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import websockets
import websockets.exceptions

from . import config
from .volume_detector import VolumeDetector
from .analyzer import MarketAnalyzer, AnalysisResult
from .volume_context import ContextSignal, VolumeContextEngine
from .derivatives_context import DerivativesContext, explain_context
from .telegram_bot import TelegramBot
from .chart_generator import generate_candlestick_chart
import io
import pathlib
from .utils import interval_minutes as _interval_minutes  # BUG-L6 FIX: shared util

logger = logging.getLogger(__name__)

# BUG-H6 FIX: Use absolute path so charts are written correctly regardless of CWD.
# os.path.join("static", "charts") resolves relative to CWD which differs when
# running as a systemd service (CWD = /). Derive from this file's location instead.
_SCANNER_DIR = pathlib.Path(__file__).resolve().parent  # VolumeSniper/
_PROJECT_ROOT = _SCANNER_DIR.parent                     # btc-ai-signal-tracker/
_STATIC_CHARTS_DIR = _PROJECT_ROOT / 'static' / 'charts'

_MAX_STREAMS_PER_WS = 80  # Giữ URL ngắn (<4096 chars) khi dùng combined stream URL
_MINUTE_MS = 60_000


# ------------------------------------------------------------------
# P3: Alert Batcher
# ------------------------------------------------------------------

@dataclass
class _PendingAlert:
    symbol:     str
    analysis:   "AnalysisResult"
    price:      float
    spike_ratio: float
    ts:         float
    chart_bytes: Optional[io.BytesIO] = None


class AlertBatcher:
    """
    Gom alerts trong cửa sổ ALERT_BATCH_WINDOW_SEC giây.
    Nếu chỉ có 1 alert → gửi đơn (hành vi cũ).
    Nếu có nhiều → gom thành 1 summary message.
    """

    def __init__(self, telegram: TelegramBot):
        self._telegram = telegram
        self._pending:  list[_PendingAlert] = []
        self._lock      = asyncio.Lock()
        self._task:     Optional[asyncio.Task] = None

    async def add(self, symbol: str, analysis: "AnalysisResult", price: float, spike_ratio: float, chart_bytes: Optional[io.BytesIO] = None):
        # Nếu batching bị tắt, gửi thẳng
        if config.ALERT_BATCH_WINDOW_SEC <= 0:
            await self._telegram.send_alert(
                symbol=symbol, analysis=analysis, price=price, spike_ratio=spike_ratio,
                chart_bytes=chart_bytes
            )
            return

        async with self._lock:
            self._pending.append(_PendingAlert(symbol, analysis, price, spike_ratio, time.time(), chart_bytes))

            # Nếu chưa có task flush đang chạy, tạo mới
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self):
        await asyncio.sleep(config.ALERT_BATCH_WINDOW_SEC)

        # BUG-H5 FIX: Drain the entire pending list in one atomic lock acquisition.
        # The original two-lock pattern had a race: new alerts added between the first
        # lock release and the second lock check were delayed by a full extra window.
        # By draining everything at once we guarantee all alerts that arrived during
        # the window are processed together with no re-entrancy gap.
        async with self._lock:
            if not self._pending:
                return
            # Drain up to ALERT_BATCH_MAX; remainder is left for the next task
            batch = self._pending[:config.ALERT_BATCH_MAX]
            self._pending = self._pending[config.ALERT_BATCH_MAX:]
            # Schedule continuation BEFORE releasing the lock so any alerts added
            # after this point are guaranteed to be picked up by the new task
            if self._pending:
                self._task = asyncio.create_task(self._flush_after_window())

        if not batch:
            return

        if len(batch) == 1:
            # Single alert → gửi chi tiết kèm ảnh nếu có
            a = batch[0]
            await self._telegram.send_alert(
                symbol=a.symbol, analysis=a.analysis, price=a.price, spike_ratio=a.spike_ratio,
                chart_bytes=a.chart_bytes
            )
        else:
            # Fix #6: Multiple alerts → gửi summary text + ảnh top 2 coins mạnh nhất
            # Khi thị trường biến động mạnh (toàn bộ altcoin spike cùng lúc) đây là lúc
            # trader cần biểu đồ nhất để tìm coin khỏe/yếu. Không bỏ ảnh hoàn toàn.
            await self._telegram.send_batch_alert(batch)

            # Gửi ảnh top 2 coins theo spike_ratio (chỉ những coin có chart_bytes)
            top_with_charts = sorted(
                [a for a in batch if a.chart_bytes is not None and a.analysis.signal != 'SKIP'],
                key=lambda x: x.spike_ratio,
                reverse=True
            )[:2]
            for a in top_with_charts:
                a.chart_bytes.seek(0)
                rsi_str = f"{a.analysis.rsi:.0f}" if a.analysis.rsi is not None else "N/A"
                caption = (
                    f"📊 <b>{a.symbol}</b> {a.spike_ratio:.1f}x | "
                    f"{a.analysis.signal} {a.analysis.zone} | "
                    f"RSI {rsi_str}"
                )
                await self._telegram.send_photo(a.chart_bytes, caption)


# ------------------------------------------------------------------
# Confirm candle pending state
# ------------------------------------------------------------------

@dataclass
class _PendingSpike:
    """Spike đang chờ confirm từ candle tiếp theo."""
    symbol:      str
    spike_price: float       # giá close lúc spike (baseline để tính pnl)
    spike_ratio: float
    direction:   str         # 'LONG' | 'SHORT' — direction của spike gốc
    analysis_direction: str  # Fix #1: direction sau khi analyzer xác định (có thể đảo chiều nếu REVERSAL)
    state_snapshot: object   # KlineState lúc spike để analyzer dùng
    ts:          float       # timestamp để detect timeout


# ------------------------------------------------------------------
# Scanner
# ------------------------------------------------------------------

class Scanner:

    def __init__(self, ui_queue=None):
        config.validate_market_data_endpoints()
        self.ui_queue = ui_queue
        self.detector = VolumeDetector(on_spike=self._on_spike)
        self.analyzer = MarketAnalyzer()
        self.telegram = TelegramBot()
        self.batcher  = AlertBatcher(self.telegram)
        if config.STRATEGY_MODE not in ("context_v2", "legacy"):
            raise ValueError("VS_STRATEGY_MODE must be context_v2 or legacy")
        if config.STRATEGY_MODE == "context_v2":
            if config.KLINE_INTERVAL not in ("1m", "3m", "5m", "15m", "30m"):
                raise ValueError("context_v2 requires a candle interval dividing one hour")
            if not 5 <= config.CONTEXT_LOOKBACK_CANDLES <= 80:
                raise ValueError("VS_CONTEXT_LOOKBACK_CANDLES must be 5..80")
            if config.CONTEXT_CONFIRM_CANDLES < 2:
                raise ValueError("VS_CONTEXT_CONFIRM_CANDLES must be at least 2")
        self.context = VolumeContextEngine() if config.STRATEGY_MODE == "context_v2" else None
        if config.V3_MAX_SYMBOLS < 0:
            raise ValueError("VS_V3_MAX_SYMBOLS must be non-negative")
        self.chart_v3 = None
        if config.V3_SHADOW_ENABLED and config.V3_MAX_SYMBOLS > 0:
            if config.KLINE_INTERVAL != "1m":
                logger.warning("v3 shadow requires 1m candles; disabled for %s", config.KLINE_INTERVAL)
            else:
                from .chart_reasoning_v3 import ChartReasoningEngine
                self.chart_v3 = ChartReasoningEngine()
        self.adaptive_shadow = None
        self._adaptive_ticks: dict[str, float] = {}
        self._adaptive_model_tasks: set[asyncio.Task] = set()
        self._adaptive_days_attempted: set[int] = set()
        self._adaptive_prompt_symbol: str | None = None
        if config.ADAPTIVE_SHADOW_ENABLED:
            if self.chart_v3 is None:
                logger.warning("adaptive shadow requires v3 closed 1m chart maps")
            else:
                from core.config import DATA_DIR
                from .adaptive_shadow import AdaptiveShadowLoop
                from .local_scenario_proposer import LocalScenarioProposer
                data_dir = pathlib.Path(DATA_DIR)
                if not data_dir.is_absolute():
                    data_dir = _PROJECT_ROOT / data_dir
                proposer = (LocalScenarioProposer(model=config.ADAPTIVE_MODEL)
                            if config.ADAPTIVE_LOCAL_MODEL_ENABLED else None)
                self.adaptive_shadow = AdaptiveShadowLoop(
                    data_dir / "vs_adaptive_shadow.jsonl",
                    asof_ms=int(time.time() * 1000), proposer=proposer)
        self._v3_symbols: set[str] = set()
        self._v3_warmup_task: asyncio.Task | None = None
        self._v3_confirmed = 0
        self.derivatives = (DerivativesContext() if (self.context or self.chart_v3) and
                            config.DERIVATIVES_CONTEXT_ENABLED else None)
        self._derivatives_tasks: set[asyncio.Task] = set()
        self._context_publish_tasks: set[asyncio.Task] = set()

        self._symbols:    list[str] = []
        self._running:    bool = False
        self._paused:     bool = False

        # SIGNAL FILTER: pending spikes chờ confirm candle
        # key = symbol, value = _PendingSpike
        # Chỉ 1 pending per symbol — spike mới ghi đè spike cũ nếu chưa confirm
        # BUG-L8 NOTE: _pending_spikes is bounded by MAX_SYMBOLS (max 500 entries — no OOM risk).
        # Stale entries (e.g. after a WS reconnect) are cleaned up by the age guard in
        # _process_kline: "if age > interval * 60 * 2: discard". This guard only fires when
        # the next candle arrives for that symbol. If WS is permanently dead for a symbol,
        # its entry stays until the next restart — acceptable behavior for this use case.
        self._pending_spikes: dict[str, _PendingSpike] = {}

        # Stats
        self._msg_count:  int   = 0
        self._start_time: float = time.time()
        self._confirm_pass: int = 0   # số spike pass confirm
        self._confirm_fail: int = 0   # số spike fail confirm
        self._context_confirmed: int = 0

    # ------------------------------------------------------------------
    # MAIN ENTRY
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True

        # 1. Telegram
        await self.telegram.start(scanner=self)

        # 2. Load symbols
        self._symbols = await self.detector.get_top_symbols()
        if not self._symbols:
            logger.error("❌ Không load được symbols. Bot dừng.")
            return

        # 3. Fetch history + khởi tạo states
        await self.detector.start(self._symbols)
        if self.context:
            for symbol, state in self.detector.states.items():
                self.context.seed(symbol, list(state.context_history))
        if self.chart_v3:
            self._start_v3_warmup()
        if self.adaptive_shadow:
            await self._load_adaptive_ticks()

        # 4. Mở WebSocket batches đồng thời
        batches = _chunk(self._symbols, _MAX_STREAMS_PER_WS)
        logger.info(
            f"📡 Opening {len(batches)} WebSocket connections "
            f"({_MAX_STREAMS_PER_WS} streams/conn)..."
        )
        ws_tasks = [self._run_ws_batch(batch, idx) for idx, batch in enumerate(batches)]
        await asyncio.gather(*ws_tasks, self._stats_printer())

    async def stop(self):
        """Dừng scanner và đóng tất cả connections."""
        self._running = False
        if self._v3_warmup_task:
            self._v3_warmup_task.cancel()
            await asyncio.gather(self._v3_warmup_task, return_exceptions=True)
        for task in self._adaptive_model_tasks:
            task.cancel()
        if self._adaptive_model_tasks:
            await asyncio.gather(*self._adaptive_model_tasks,
                                 return_exceptions=True)
        if self._context_publish_tasks:
            await asyncio.gather(*self._context_publish_tasks, return_exceptions=True)
        for task in self._derivatives_tasks:
            task.cancel()
        if self._derivatives_tasks:
            await asyncio.gather(*self._derivatives_tasks, return_exceptions=True)
        if self.derivatives:
            await self.derivatives.close()
        if self.context:
            await self.context.baseline.save()
            await self.context.flush_records()
        if self.chart_v3:
            await self.chart_v3.flush_records()
        if hasattr(self.detector, 'stop'):
            await self.detector.stop()
        if hasattr(self.telegram, 'stop'):
            await self.telegram.stop()

    def pause(self):
        self._paused = True
        logger.info("⏸️ Scanner paused")

    def resume(self):
        self._paused = False
        logger.info("▶️ Scanner resumed")

    def _start_v3_warmup(self) -> None:
        """Seed selected symbols from closed 1m history; fetch HTF in background.

        The REST cutoff precedes the first live candle that this scanner will
        process. A slow HTF response therefore cannot introduce a future bar
        into an already emitted chart decision.
        """
        selected = self._symbols[:config.V3_MAX_SYMBOLS]
        cutoffs: list[int] = []
        for symbol in selected:
            state = self.detector.states.get(symbol)
            history = list(state.context_history) if state else []
            if not history:
                continue
            self.chart_v3.seed(symbol, history)
            self._v3_symbols.add(symbol)
            cutoffs.append(int(history[-1]["t"]) + _MINUTE_MS - 1)
        if not self._v3_symbols:
            logger.warning("v3 shadow skipped: no closed 1m warmup history")
            return
        cutoff = min(cutoffs)
        self._v3_warmup_task = asyncio.create_task(
            self._seed_v3_higher_timeframes(sorted(self._v3_symbols), cutoff))
        logger.info("v3 shadow tracking %d symbols; HTF REST warmup running in background",
                    len(self._v3_symbols))

    async def _load_adaptive_ticks(self) -> None:
        """Read USD-M PRICE_FILTER tick sizes once for paper fill rounding."""
        selected = set(self._v3_symbols)
        if not selected:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                        f"{config.BINANCE_REST_URL}/fapi/v1/exchangeInfo") as response:
                    response.raise_for_status()
                    payload = await response.json()
            ticks = {}
            for row in payload.get("symbols", []):
                symbol = row.get("symbol")
                if symbol not in selected:
                    continue
                price_filter = next((item for item in row.get("filters", [])
                                     if item.get("filterType") == "PRICE_FILTER"), None)
                if price_filter is None:
                    continue
                tick = float(price_filter["tickSize"])
                if 0 < tick < float("inf"):
                    ticks[symbol] = tick
            self._adaptive_ticks = ticks
            self._adaptive_prompt_symbol = (
                "BTCUSDT" if "BTCUSDT" in ticks else next(iter(sorted(ticks)), None))
            logger.info("adaptive paper tracking %d symbols with exchange ticks",
                        len(ticks))
        except Exception:
            logger.exception("adaptive paper tick-size load failed; paper path idle")

    async def _seed_v3_higher_timeframes(self, symbols: list[str], cutoff: int) -> None:
        from .htf_feed import fetch_closed_higher_timeframes

        try:
            frames = await asyncio.wait_for(
                fetch_closed_higher_timeframes(
                    symbols, config.BINANCE_REST_URL, limit=100,
                    as_of_ms=cutoff, max_symbols=config.V3_MAX_SYMBOLS,
                ), timeout=45)
            for symbol in symbols:
                self.chart_v3.seed_higher_timeframes(symbol, frames.get(symbol, {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("v3 shadow HTF warmup failed; engine will use closed 1m aggregation")

    @property
    def symbol_count(self) -> int:
        return len(self._symbols)

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    # ------------------------------------------------------------------
    # WEBSOCKET BATCH
    # ------------------------------------------------------------------

    async def _run_ws_batch(self, symbols: list[str], batch_idx: int):
        """Chạy 1 WebSocket connection, reconnect tự động."""
        interval = config.KLINE_INTERVAL
        # USD-M kline streams use the /market combined-stream route.
        streams = "/".join(f"{s.lower()}@kline_{interval}" for s in symbols)
        url   = f"{config.BINANCE_WS_URL}{streams}"
        delay = config.WS_RECONNECT_DELAY_SEC

        while self._running:
            try:
                logger.info(f"🔌 WS[{batch_idx}] connecting ({len(symbols)} streams)...")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=5,
                    max_size=2 ** 22,  # 4 MB
                    open_timeout=30,
                ) as ws:
                    logger.info(f"✅ WS[{batch_idx}] connected ({len(symbols)} streams)")
                    delay = config.WS_RECONNECT_DELAY_SEC  # reset backoff

                    async for raw in ws:
                        if not self._running:
                            return
                        if self._paused:
                            continue
                        await self._handle_message(raw)

            except asyncio.CancelledError:
                return
            except websockets.exceptions.ConnectionClosedError as e:
                logger.warning(f"⚠️ WS[{batch_idx}] closed ({e.code}). Retry in {delay}s")
            except OSError as e:
                logger.warning(f"⚠️ WS[{batch_idx}] OS error: {e}. Retry in {delay}s")
            except Exception as e:
                logger.warning(f"⚠️ WS[{batch_idx}] error: {type(e).__name__}: {e}. Retry in {delay}s")

            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)  # exponential backoff, max 60s

    # ------------------------------------------------------------------
    # MESSAGE PROCESSING
    # ------------------------------------------------------------------

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
            # Subscribe method:    {"e": "kline", "s": "BTCUSDT", "k": {...}}
            # Combined stream URL: {"stream": "...", "data": {"e": "kline", ...}}
            # Subscription confirm: {"result": null, "id": 1}  <- bo qua
            if "result" in data and "e" not in data:
                return
            msg = data.get("data", data)
            if msg.get("e") != "kline":
                return

            symbol = msg["s"]
            k      = msg["k"]  # kline object

            self._msg_count += 1

            # SIGNAL FILTER: khi candle đóng, check pending spike trước
            if not self.context and bool(k["x"]) and symbol in self._pending_spikes:
                await self._check_confirm_candle(symbol, float(k["c"]))

            await self.detector.process_kline(symbol, k)
            if self.context and bool(k["x"]):
                had_candidate = symbol in self.context.pending
                signal = self.context.on_kline(symbol, k)
                if self.derivatives and not had_candidate and symbol in self.context.pending:
                    self._schedule_derivatives_refresh(
                        symbol, "candidate", self.context.pending[symbol].event_ts)
                for watch in self.context.drain_watches():
                    if self.derivatives:
                        self._schedule_derivatives_refresh(symbol, "watch", watch["ts"])
                    await self._publish_context_watch(watch)
                if signal:
                    # REST enrichment may take seconds; keep the kline stream flowing.
                    task = asyncio.create_task(self._publish_context_signal(signal))
                    self._context_publish_tasks.add(task)
                    def publication_done(finished: asyncio.Task,
                                         symbol_name: str = signal.symbol) -> None:
                        self._context_publish_tasks.discard(finished)
                        if not finished.cancelled():
                            try:
                                finished.result()
                            except Exception:
                                logger.exception("Could not publish context signal for %s", symbol_name)
                    task.add_done_callback(publication_done)

            # v3 observes the same closed candle independently of v2/legacy.
            # It cannot reach Telegram or the legacy signal publication path.
            if (getattr(self, "chart_v3", None) and bool(k["x"])
                    and symbol in getattr(self, "_v3_symbols", ())):
                try:
                    events = self.chart_v3.on_kline(symbol, k)
                    if (getattr(self, "adaptive_shadow", None) and
                            symbol in getattr(self, "_adaptive_ticks", {})):
                        try:
                            plan = self.chart_v3.plans.get(symbol)
                            if plan is not None:
                                history = list(self.chart_v3.history[symbol])
                                derivatives = (self.derivatives.snapshot(
                                    symbol, now_ms=int(k["t"]) + _MINUTE_MS - 1)
                                    if getattr(self, "derivatives", None) else None)
                                paper = self.adaptive_shadow.on_bar(
                                    symbol, k, plan.market_map, history[-61:-1],
                                    tick_size=self._adaptive_ticks[symbol],
                                    derivatives=derivatives)
                                if paper["paper_queued"] or paper["settled"]:
                                    logger.info(
                                        "adaptive paper %s: %d queued, %d settled",
                                        symbol, len(paper["paper_queued"]),
                                        len(paper["settled"]))
                                day = paper["snapshot"].context_asof_ms // 86_400_000
                                if (self.adaptive_shadow.proposer is not None and
                                        symbol == self._adaptive_prompt_symbol and
                                        paper["plans"] and
                                        day not in self._adaptive_days_attempted):
                                    self._adaptive_days_attempted.add(day)
                                    task = asyncio.create_task(
                                        self._propose_adaptive(paper["snapshot"]))
                                    self._adaptive_model_tasks.add(task)
                                    task.add_done_callback(
                                        self._adaptive_model_tasks.discard)
                        except Exception:
                            logger.exception("adaptive paper failed for %s", symbol)
                    for event in events:
                        if event.get("kind") == "confirmed":
                            self._v3_confirmed += 1
                        if getattr(self, "derivatives", None) and event.get("kind") in ("candidate", "confirmed"):
                            self._schedule_derivatives_refresh(
                                symbol, f"v3_{event['kind']}", event["ts"])
                        if self.ui_queue:
                            await self.ui_queue.put({
                                "type": "chart_v3_event", "symbol": symbol,
                                "kind": event.get("kind"),
                                "timestamp": event.get("observed_at", event.get("ts")),
                                "research_only": True, "event": event,
                            })
                except Exception:
                    logger.exception("v3 shadow failed for %s", symbol)

        except (json.JSONDecodeError, KeyError):
            pass
        except Exception as e:
            logger.debug(f"_handle_message: {e}")

    async def _propose_adaptive(self, snapshot) -> None:
        """Occasional N100 localhost model call outside candle processing."""
        try:
            result = await self.adaptive_shadow.propose_async(snapshot)
            logger.info("adaptive proposal: %s, accepted=%d, tokens=%s, eval_ns=%s",
                        result.status, len(result.proposals), result.eval_count,
                        result.eval_duration_ns)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("adaptive local proposal failed")

    def _schedule_derivatives_refresh(self, symbol: str, event_kind: str | None = None,
                                      event_ts: int | None = None) -> None:
        """Enrich asynchronously so REST latency cannot stall kline consumption."""
        if not self.derivatives:
            return
        async def refresh_and_record() -> None:
            snapshot = await self.derivatives.refresh(symbol)
            if event_kind and self.context and not event_kind.startswith("v3_"):
                self.context.records.append({
                    "kind": "derivatives_context", "event_kind": event_kind,
                    "symbol": symbol, "event_ts": event_ts, "snapshot": snapshot,
                })
            if event_kind and event_kind.startswith("v3_") and self.chart_v3:
                # REST enrichment arrives after the closed-candle decision.
                # Keep its observation clock separate; never rewrite the plan.
                self.chart_v3.records.append({
                    "kind": "derivatives_context", "event_kind": event_kind,
                    "symbol": symbol, "event_ts": event_ts,
                    "observed_at": int(time.time() * 1000),
                    "post_event": True, "snapshot": snapshot,
                    "research_only": True,
                })
        task = asyncio.create_task(refresh_and_record())
        self._derivatives_tasks.add(task)
        def done(finished: asyncio.Task) -> None:
            self._derivatives_tasks.discard(finished)
            if not finished.cancelled():
                try:
                    finished.result()
                except Exception as exc:
                    logger.debug("Derivatives refresh for %s failed: %s", symbol, exc)
        task.add_done_callback(done)

    async def _publish_context_signal(self, signal: ContextSignal) -> None:
        """Publish a confirmed v2 event using the actual confirmation close."""
        self._context_confirmed += 1
        service = getattr(self, "derivatives", None)
        if service:
            try:
                await asyncio.wait_for(service.refresh(signal.symbol), timeout=7)
            except Exception as exc:
                logger.debug("Context enrichment for %s incomplete: %s", signal.symbol, exc)
        derivatives = service.snapshot(signal.symbol) if service else None
        if service:
            self.context.records.append({
                "kind": "derivatives_context", "symbol": signal.symbol,
                "event_ts": signal.event_ts, "confirmed_ts": signal.confirmed_ts,
                "snapshot": derivatives,
            })
        chart_bytes = None
        chart_url = None
        state = self.detector.states.get(signal.symbol)
        if config.CONTEXT_TELEGRAM_ALERTS_ENABLED and state and len(state.ohlc_history) >= 5:
            try:
                from .chart_generator import async_generate_candlestick_chart
                opens, highs, lows, closes, volumes = zip(*state.ohlc_history)
                chart_bytes = await async_generate_candlestick_chart(
                    list(opens), list(highs), list(lows), list(closes), list(volumes),
                    title=f"{signal.symbol} {config.KLINE_INTERVAL} | {signal.scenario}",
                    interval_minutes=int(_interval_minutes(config.KLINE_INTERVAL)),
                )
                _STATIC_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
                chart_name = f"{signal.symbol}_{signal.confirmed_ts}.png"
                chart_path = _STATIC_CHARTS_DIR / chart_name
                chart_bytes.seek(0)
                chart_path.write_bytes(chart_bytes.read())
                chart_bytes.seek(0)
                chart_url = f"/static/charts/{chart_name}"
            except Exception:
                logger.exception("Could not build context chart for %s", signal.symbol)
                chart_bytes = None

        delta = "unavailable" if signal.delta_pct is None else f"{signal.delta_pct:+.1f}%"
        reasons = [
            f"{signal.scenario}: level {signal.level:.6g} held/failed on a later closed candle",
            f"Plan from prior closed candle {signal.plan_ts}: range {signal.plan_lower:.6g}–{signal.plan_upper:.6g}" if signal.plan_ts is not None else "Prior range was fixed before the event candle",
            f"RVOL {signal.rvol:.1f}x ({signal.baseline} baseline)",
            f"Taker delta at event: {delta}; CVD20: {signal.cvd if signal.cvd is not None else 'unavailable'}",
            f"Invalidation level: {signal.invalidation:.6g}",
            f"Distance to invalidation: {abs(signal.price - signal.invalidation) / signal.price * 100:.2f}% before fees/slippage",
            "Derivatives were captured after confirmation; displayed price is the confirmation close, not an executable quote",
        ]
        reasons.extend(explain_context(derivatives, scenario=signal.scenario,
                                       event_delta_pct=signal.delta_pct))
        if self.ui_queue:
            await self.ui_queue.put({
                "type": "volume_sniper_signal", "symbol": signal.symbol,
                "signal_type": "VOLUME_CONTEXT", "direction": signal.direction,
                "price": signal.price, "spike_ratio": signal.rvol,
                "confidence": None, "zone": signal.scenario,
                "rsi": None, "ema_trend": "N/A", "chart_url": chart_url,
                "reasons": reasons, "level": signal.level,
                "invalidation": signal.invalidation, "baseline": signal.baseline,
                "plan_ts": signal.plan_ts, "plan_upper": signal.plan_upper,
                "plan_lower": signal.plan_lower,
                "delta_pct": signal.delta_pct, "cvd": signal.cvd,
                "derivatives": derivatives,
                "published_at": int(time.time() * 1000),
                "timestamp": signal.confirmed_close_ts or signal.confirmed_ts,
            })
        if config.CONTEXT_TELEGRAM_ALERTS_ENABLED:
            await self.telegram.send_context_alert(signal, chart_bytes)

    async def _publish_context_watch(self, watch: dict) -> None:
        """Display contested preplanned levels without a trade direction or push alert."""
        if not self.ui_queue:
            return
        edge = "upper resistance" if watch["edge"] == "upper" else "lower support"
        await self.ui_queue.put({
            "type": "volume_context_watch", "symbol": watch["symbol"],
            "signal_type": "VOLUME_WATCH", "direction": "WATCH",
            "zone": "WATCH", "price": watch["price"],
            "spike_ratio": watch["rvol"], "baseline": watch["baseline"],
            "level": watch["plan_upper"] if watch["edge"] == "upper" else watch["plan_lower"],
            "plan_ts": watch["plan_ts"], "plan_upper": watch["plan_upper"],
            "plan_lower": watch["plan_lower"],
            "timestamp": watch.get("observed_at", watch["ts"]),
            "delta_pct": watch["delta_pct"], "cvd": watch["cvd"],
            "derivatives": (self.derivatives.snapshot(watch["symbol"])
                            if getattr(self, "derivatives", None) else None),
            "reasons": [
                f"Preplanned range: {watch['plan_lower']:.6g}–{watch['plan_upper']:.6g}",
                f"High volume at {edge}; {watch['repeated_tests']} recent test(s); direction unresolved",
                f"RVOL {watch['rvol']:.1f}x ({watch['baseline']} baseline)",
                "Prior volume and price range narrowed; this does not predict a breakout" if watch["quieting_before_event"] else "Wait for price to accept or reject the boundary",
            ],
        })

    # ------------------------------------------------------------------
    # CONFIRM CANDLE CHECK
    # ------------------------------------------------------------------

    async def _check_confirm_candle(self, symbol: str, close_price: float):
        """
        Được gọi khi candle tiếp theo đóng sau spike.
        Tính pnl so với giá lúc spike, chỉ alert nếu >= CONFIRM_CANDLE_MIN_PCT.
        """
        pending = self._pending_spikes.pop(symbol, None)
        if pending is None:
            return

        # Timeout guard: bỏ qua nếu pending quá lâu (> 2 candles = 10 phút)
        # Trường hợp này xảy ra khi bot restart hoặc WS reconnect
        age = time.time() - pending.ts
        if age > _interval_minutes(config.KLINE_INTERVAL) * 60 * 2:
            logger.debug(f"⏱️ {symbol}: pending spike timeout ({age:.0f}s), discarded")
            return

        spike_price = pending.spike_price
        if spike_price <= 0:
            return

        # Tính pnl theo analysis_direction (Fix #1: dùng analysis_direction thay vì direction)
        # Lý do: nếu spike LONG nhưng analyzer xác định REVERSAL (SHORT), nến N+1 sẽ giảm
        # Dùng spike direction thì pnl < 0 → DROP, dùng analysis_direction thì pass đúng
        if pending.analysis_direction == "LONG":
            pnl_pct = (close_price - spike_price) / spike_price * 100
        else:
            pnl_pct = (spike_price - close_price) / spike_price * 100

        min_pct = config.CONFIRM_CANDLE_MIN_PCT

        if pnl_pct < min_pct:
            self._confirm_fail += 1
            logger.info(
                f"⏭️ CONFIRM FAIL {symbol}: pnl={pnl_pct:+.2f}% < {min_pct}% "
                f"(spike={spike_price:.6g} → close={close_price:.6g})"
            )
            return

        # Pass confirm → tiếp tục pipeline alert
        self._confirm_pass += 1
        logger.info(
            f"✅ CONFIRM PASS {symbol}: pnl={pnl_pct:+.2f}% >= {min_pct}%"
        )
        await self._run_analysis_and_alert(symbol, pending.state_snapshot, pending.spike_ratio)

    # ------------------------------------------------------------------
    # SPIKE CALLBACK (từ VolumeDetector)
    # ------------------------------------------------------------------

    async def _on_spike(self, symbol: str, state, spike_ratio: float, delta_pct: float):
        """
        Được VolumeDetector gọi khi phát hiện spike.
        Nếu confirm candle filter bật (> 0), lưu vào _pending_spikes.
        Nếu tắt (= 0), chạy analysis + alert ngay lập tức.
        """
        try:
            # Nếu confirm candle filter TẮT → alert ngay, không đợi
            if config.CONFIRM_CANDLE_MIN_PCT <= 0:
                await self._run_analysis_and_alert(symbol, state, spike_ratio)
                return

            # Xác định direction spike gốc
            direction = "LONG" if delta_pct > 0 else "SHORT"

            # Snapshot OHLC + close_prices tại thời điểm spike
            # QUAN TRỌNG: KHÔNG lưu reference tới state object vì state
            # sẽ bị mutate bởi các candles tiếp theo (race condition).
            from dataclasses import replace as dc_replace
            import copy
            state_snap = dc_replace(
                state,
                close_prices=copy.copy(state.close_prices),
                ohlc_history=copy.copy(state.ohlc_history),
                # Fix #2: dùng bb_width_prev (trước spike) thay vì bb_width (sau spike)
                # bb_width sau spike luôn > 5% vì std_dev tăng mạnh → filter block hết breakout
                bb_width=state.bb_width_prev,
            )

            # Fix #1: Chạy analyzer sớm để xác định analysis_direction thực sự
            # Nếu REVERSAL (ví dụ LONG spike → SHORT REVERSAL), analysis_direction sẽ ngược với direction
            # Confirm candle dùng analysis_direction để tính pnl → cho phép REVERSAL pass filter
            closes_snap = list(state_snap.close_prices)
            if len(closes_snap) >= 10:
                quick_analysis = self.analyzer.analyze(
                    close_prices  = closes_snap,
                    open_price    = state_snap.open,
                    high_price    = state_snap.high,
                    low_price     = state_snap.low,
                    current_price = state_snap.close,
                    spike_ratio   = spike_ratio,
                    is_closed     = state_snap.is_closed,
                    bb_width      = state_snap.bb_width,
                )
                # analysis_direction: nếu SKIP hoặc NEUTRAL, fallback về spike direction
                if quick_analysis.signal in ('LONG', 'SHORT'):
                    analysis_direction = quick_analysis.signal
                else:
                    analysis_direction = direction
            else:
                analysis_direction = direction

            if analysis_direction != direction:
                logger.info(
                    f"⏳ PENDING {symbol}: REVERSAL detected at spike — "
                    f"spike={direction}, analysis={analysis_direction}, "
                    f"zone={quick_analysis.zone if len(closes_snap) >= 10 else 'N/A'}"
                )

            # Lưu pending — spike mới ghi đè spike cũ nếu cùng symbol chưa confirm
            self._pending_spikes[symbol] = _PendingSpike(
                symbol=symbol,
                spike_price=state.close,
                spike_ratio=spike_ratio,
                direction=direction,
                analysis_direction=analysis_direction,
                state_snapshot=state_snap,
                ts=time.time(),
            )
            logger.info(
                f"⏳ PENDING {symbol}: waiting confirm candle "
                f"(spike={spike_ratio:.1f}x, dir={direction}, analysis_dir={analysis_direction}, price={state.close:.6g})"
            )

        except Exception as e:
            logger.error(f"❌ _on_spike {symbol}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # ANALYSIS + ALERT (chạy sau khi confirm candle pass)
    # ------------------------------------------------------------------

    async def _run_analysis_and_alert(self, symbol: str, state, spike_ratio: float):
        """Chạy analyzer và gửi alert. Tách ra để _on_spike gọn hơn."""
        try:
            closes = list(state.close_prices)
            if len(closes) < 10:
                logger.debug(f"{symbol}: không đủ close_prices ({len(closes)})")
                return

            # P1: truyền is_closed để analyzer xử lý RSI đúng
            analysis = self.analyzer.analyze(
                close_prices  = closes,
                open_price    = state.open,
                high_price    = state.high,
                low_price     = state.low,
                current_price = state.close,
                spike_ratio   = spike_ratio,
                is_closed     = state.is_closed,
                bb_width      = state.bb_width,
            )

            # Accumulation filter (Bollinger Bands Squeeze)
            if config.BB_SQUEEZE_ENABLED and not analysis.in_accumulation:
                logger.info(
                    f"⏭️ SKIP {symbol}: {analysis.bb_width_pct:.1f}% > {config.BB_WIDTH_MAX_PCT}% (not in squeeze)"
                )
                return

            # CHART: Tạo ảnh chart từ lịch sử OHLC (cần ít nhất 5 nến)
            chart_bytes = None
            chart_url = None
            if hasattr(state, 'ohlc_history') and state.ohlc_history and len(state.ohlc_history) >= 5:
                opens, highs, lows, closes_ohlc, volumes = zip(*state.ohlc_history)
                from .chart_generator import async_generate_candlestick_chart
                chart_bytes = await async_generate_candlestick_chart(
                    list(opens), list(highs), list(lows), list(closes_ohlc),
                    list(volumes),
                    title=f"{symbol} {config.KLINE_INTERVAL} | Spike {spike_ratio:.1f}x",
                    interval_minutes=int(_interval_minutes(config.KLINE_INTERVAL)),
                )

                if chart_bytes:
                    # BUG-H6 FIX: Use the pre-computed absolute path _STATIC_CHARTS_DIR
                    # instead of a CWD-relative path that breaks under systemd.
                    _STATIC_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
                    chart_filename = f"{symbol.replace('/', '_').replace(':', '_')}_{int(time.time() * 1000)}.png"
                    chart_path = _STATIC_CHARTS_DIR / chart_filename
                    try:
                        with open(chart_path, 'wb') as f:
                            chart_bytes.seek(0)
                            f.write(chart_bytes.read())
                        chart_url = f"/static/charts/{chart_filename}"
                        logger.info(f"Saved static chart for UI: {chart_url}")
                    except Exception as ex:
                        logger.error(f"Failed to save static chart: {ex}")

            # Gửi alert qua Web UI
            if self.ui_queue and analysis.signal != "SKIP":
                await self.ui_queue.put({
                    'type': 'volume_sniper_signal',
                    'symbol': symbol,
                    'signal_type': 'VOLUME_SPIKE',
                    'direction': analysis.signal,
                    'price': state.close,
                    'spike_ratio': spike_ratio,
                    'confidence': analysis.confidence,
                    'zone': analysis.zone,
                    'rsi': analysis.rsi,
                    'ema_trend': analysis.ema_trend,
                    'chart_url': chart_url,
                    'reasons': analysis.reasons,
                    'timestamp': int(time.time() * 1000),
                })

            # Gửi alert qua batcher (đã bao gồm chart)
            await self.batcher.add(
                symbol=symbol,
                analysis=analysis,
                price=state.close,
                spike_ratio=spike_ratio,
                chart_bytes=chart_bytes,
            )

        except Exception as e:
            logger.error(f"❌ _run_analysis_and_alert {symbol}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # STATS
    # ------------------------------------------------------------------

    async def _stats_printer(self):
        """In stats định kỳ ra log."""
        while self._running:
            await asyncio.sleep(300)  # mỗi 5 phút
            uptime = int(time.time() - self._start_time)
            h, m   = uptime // 3600, (uptime % 3600) // 60
            pending = len(self._pending_spikes)
            if self.context:
                try:
                    await self.context.baseline.save()
                    await self.context.flush_records()
                except OSError:
                    logger.exception("Could not save seasonal volume baseline")
                pending = len(self.context.pending)
            if self.chart_v3:
                try:
                    await self.chart_v3.flush_records()
                except Exception:
                    logger.exception("Could not save v3 shadow journal")
            logger.info(
                f"📊 Stats | uptime={h}h{m:02d}m "
                f"| symbols={self.symbol_count} "
                f"| messages={self._msg_count:,} "
                f"| alerts={self.telegram.alert_count} "
                f"| context confirmed={self._context_confirmed} "
                f"| v3 shadow confirmed={self._v3_confirmed} "
                f"| confirm pass={self._confirm_pass} fail={self._confirm_fail} "
                f"| pending={pending}"
            )


# ------------------------------------------------------------------

def _chunk(lst: list, size: int) -> list[list]:
    return [lst[i: i + size] for i in range(0, len(lst), size)]

# BUG-L6 FIX: _interval_minutes() moved to VolumeSniper/utils.py to avoid duplication.
# Imported as _interval_minutes above for backward compatibility with existing call sites.