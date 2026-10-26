"""
volume_detector.py - VolumeSniper

Core logic: phát hiện volume spike dựa trên Kline WebSocket stream.

Dùng @kline stream thay vì @aggTrade để có đầy đủ OHLC + volume
của nến hiện tại → phân tích wick rejection chính xác.

Công thức Weighted Average Volume:
  w_i = i+1 (nến mới nhất = weight cao nhất)
  avg = sum(w_i * vol_i) / sum(w_i)
→ Phản ánh baseline thực tế hơn simple average.

--- Thay đổi so với bản gốc ---

P0 — Candle-completion guard:
  Khi REQUIRE_CLOSED_CANDLE=True (default), spike check chỉ chạy khi
  is_closed=True. Loại bỏ false positive do volume đầu nến chưa hoàn chỉnh
  bị so với avg của các nến đã đóng.
  Khi REQUIRE_CLOSED_CANDLE=False, áp dụng LIVE_CANDLE_SPIKE_MULTIPLIER
  để tăng ngưỡng, hành vi tương tự bản gốc nhưng ít noise hơn.

P1 — Refresh loop không xoá close_prices:
  _refresh_loop chỉ cập nhật avg_volume và sideways bands từ REST API.
  close_prices được duy trì liên tục từ stream, không bị clear mỗi 5 phút.
  Tránh blind spot khi refresh trùng với spike event.

SIGNAL FILTER — Delta price cap:
  Bỏ qua spike có delta_price_pct > DELTA_PRICE_MAX_PCT (default 2.0%).
  Dựa trên backtesting 460 signals: delta > 2% có BAD rate 43%,
  cao hơn cả GOOD rate (39%) → không có edge.
  Truyền delta_pct vào callback để scanner dùng cho confirm candle check.

CHART — Thêm ohlc_history để lưu lịch sử OHLCV phục vụ vẽ chart.
"""

import asyncio
import aiohttp
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
import numpy as np

from . import config
from .utils import interval_minutes as _interval_minutes  # BUG-L6 FIX: shared util

logger = logging.getLogger(__name__)


@dataclass
class KlineState:
    """State đầy đủ của 1 symbol, được cập nhật từ kline stream."""

    symbol: str

    # Nến hiện tại (live, có thể chưa đóng)
    open:      float = 0.0
    high:      float = 0.0
    low:       float = 0.0
    close:     float = 0.0
    volume:    float = 0.0
    is_closed: bool  = False

    # Baseline — chỉ được cập nhật từ closed candles
    avg_volume:      float = 0.0
    avg_volume_usdt: float = 0.0  # USDT-denominated avg, dùng lọc min liquidity
    last_avg_update: float = 0.0
    last_rest_vol_fetch: float = 0.0  # MOCK SERVER BUG FIX

    # Alert control
    last_alert_ts: float = 0.0
    alert_count:   int   = 0

    # Technical indicator inputs — duy trì liên tục từ stream (P1)
    close_prices: deque = field(default_factory=lambda: deque(maxlen=250))

    # Lịch sử OHLCV của các nến đã đóng (để vẽ chart)
    ohlc_history: deque = field(default_factory=lambda: deque(maxlen=50))

    # Lịch sử quote volume (USDT) của các nến đã đóng
    quote_volume_history: deque = field(default_factory=lambda: deque(maxlen=50))

    # Raw closed klines for context v2 warmup (includes UTC time and taker buy).
    context_history: deque = field(default_factory=lambda: deque(maxlen=80))

    # BB Squeeze (Tích lũy)
    bb_width: float = 0.0
    bb_width_prev: float = 0.0  # Fix #2: bb_width của nến TRƯỚC khi spike đóng

    # Internal
    _last_spike_check: float = field(default=0.0, repr=False)


# Callback nhận thêm delta_pct để scanner dùng cho confirm candle
SpikeCallback = Callable[[str, "KlineState", float, float], Awaitable[None]]


class VolumeDetector:
    """
    Nhận kline events từ scanner, tính spike ratio, gọi callback khi phát hiện.
    """

    def __init__(self, on_spike: SpikeCallback):
        self._on_spike  = on_spike
        self.states:    dict[str, KlineState]  = {}
        self._session:  Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self, symbols: list[str]):
        logger.info(f"🔍 VolumeDetector: initialising {len(symbols)} symbols...")
        if self._session:
            await self._session.close()
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=300, ttl_dns_cache=300, resolver=aiohttp.ThreadedResolver())
        )
        for sym in symbols:
            self.states[sym] = KlineState(symbol=sym)

        await self._bulk_fetch_history(symbols)
        # Polling disabled in Phase 1: all calculations are now in RAM
        # asyncio.create_task(self._refresh_loop())
        logger.info("✅ VolumeDetector ready")

    async def stop(self):
        if self._session:
            await self._session.close()

    def _calculate_weighted_avg_vol(self, vols: list[float]) -> float:
        if not vols:
            return 0.0
        import statistics
        n = len(vols)
        weights = list(range(1, n + 1))
        w_avg = sum(w * v for w, v in zip(weights, vols)) / sum(weights)

        # P10 floor
        vol_median = statistics.median(vols)
        vol_p10 = sorted(vols)[max(0, n // 10)]
        vols_filtered = [v for v in vols if v >= vol_p10]
        weights_f = list(range(1, len(vols_filtered) + 1))
        w_avg_filtered = (sum(w * v for w, v in zip(weights_f, vols_filtered))
                          / sum(weights_f)) if vols_filtered else w_avg
        return max(w_avg, w_avg_filtered)

    def _calculate_weighted_avg_quote_vol(self, vols_q: list[float]) -> float:
        if not vols_q:
            return 0.0
        n = len(vols_q)
        weights_q = list(range(1, n + 1))
        return sum(w * v for w, v in zip(weights_q, vols_q)) / sum(weights_q)

    # ------------------------------------------------------------------ kline event

    async def process_kline(self, symbol: str, k: dict):
        """
        Nhận raw kline dict từ Binance WebSocket.
        k keys: o, h, l, c, v, x (is_closed), t (open_time)
        """
        if symbol not in self.states:
            self.states[symbol] = KlineState(symbol=symbol)

        state = self.states[symbol]

        state.open      = float(k["o"])
        state.high      = float(k["h"])
        state.low       = float(k["l"])
        state.close     = float(k["c"])
        state.volume    = float(k["v"])
        state.is_closed = bool(k["x"])

        # Khi nến đóng → append close vào lịch sử stream (P1: close_prices
        # được duy trì liên tục, không phụ thuộc vào _refresh_loop)
        if state.is_closed:
            state.close_prices.append(state.close)
            state.context_history.append(dict(k))
            # Lưu OHLCV để vẽ chart
            state.ohlc_history.append((
                state.open,
                state.high,
                state.low,
                state.close,
                state.volume
            ))
            # Lưu quote volume
            state.quote_volume_history.append(float(k.get("q", 0.0)))

            # --- Recalculate indicators in RAM dynamically ---
            n = config.VOLUME_CANDLES_AVG
            if len(state.ohlc_history) > n:
                # Exclude the candle under test. Including it compresses every
                # spike ratio and makes large-climax thresholds unreachable.
                vols = [item[4] for item in list(state.ohlc_history)[-n-1:-1]]
                state.avg_volume = self._calculate_weighted_avg_vol(vols)

            if len(state.quote_volume_history) > n:
                vols_q = list(state.quote_volume_history)[-n-1:-1]
                state.avg_volume_usdt = self._calculate_weighted_avg_quote_vol(vols_q)
                state.last_avg_update = time.time()

            # Bollinger Bands Squeeze
            # Fix #2: lưu bb_width_prev (của nến N-1) TRƯỚC khi update
            # Dùng bb_width_prev khi check spike vì bb_width hiện tại đã bao gồm nến spike
            state.bb_width_prev = state.bb_width
            bb_min = config.BB_PERIOD
            if len(state.close_prices) >= bb_min:
                closes_arr = np.array(list(state.close_prices)[-bb_min:])
                sma = np.mean(closes_arr)
                std_dev = np.std(closes_arr)
                if sma > 0:
                    state.bb_width = (4 * std_dev) / sma * 100

        # --- P0: Candle-completion guard ---
        if config.STRATEGY_MODE == "context_v2":
            return  # Context engine handles the closed candle in Scanner.
        if config.REQUIRE_CLOSED_CANDLE:
            # Chỉ check spike khi nến vừa đóng
            if not state.is_closed:
                return
            await self._check_spike(state, is_live=False)
        else:
            # Hành vi cũ: check mỗi 2 giây, nhưng dùng higher multiplier cho live candle
            now = time.time()
            if now - state._last_spike_check < 2.0:
                return
            state._last_spike_check = now
            await self._check_spike(state, is_live=not state.is_closed)

    # ------------------------------------------------------------------ symbol list

    async def get_top_symbols(self) -> list[str]:
        """USDT Perp symbols sort by 24h quote volume, top MAX_SYMBOLS."""
        if not self._session:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            )
        try:
            url = f"{config.BINANCE_REST_URL}/fapi/v1/ticker/24hr"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    logger.error(f"ticker/24hr HTTP {r.status}")
                    raise RuntimeError(f"HTTP status {r.status}")
                data = await r.json()

            usdt = [d for d in data
                    if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) > 0]
            usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
            symbols = [d["symbol"] for d in usdt[: config.MAX_SYMBOLS]]
            logger.info(f"📋 {len(symbols)} symbols (top by 24h vol)")
            return symbols
        except Exception as e:
            logger.warning(f"⚠️ get_top_symbols failed: {e}. Falling back to popular static symbols list.")
            fallback = [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", 
                "ADAUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT", 
                "BCHUSDT", "ETCUSDT", "XLMUSDT", "TRXUSDT", "AVAXUSDT", 
                "ATOMUSDT", "SHIBUSDT", "UNIUSDT", "APTUSDT", "SUIUSDT", 
                "NEARUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "TIAUSDT", 
                "FTMUSDT", "GALAUSDT", "DYDXUSDT", "ORDIUSDT", "PEPEUSDT"
            ]
            return fallback[: config.MAX_SYMBOLS]

    # ------------------------------------------------------------------ spike check

    async def _fetch_live_volume_rest(self, symbol: str) -> float:
        """Fetch real volume from REST API (bypass mock server WS bug)."""
        try:
            url    = f"{config.BINANCE_REST_URL}/fapi/v1/klines"
            params = {"symbol": symbol, "interval": config.KLINE_INTERVAL, "limit": 1}
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return 0.0
                klines = await r.json()
                if not klines:
                    return 0.0
                return float(klines[0][5])
        except Exception as e:
            logger.debug(f"_fetch_live_volume_rest error for {symbol}: {e}")
            return 0.0

    async def _check_spike(self, state: KlineState, is_live: bool = False):
        if state.avg_volume <= 0:
            return

        # 1. Price change phải đủ lớn để xác nhận có momentum (Lọc 99% nến không quan trọng)
        if state.open <= 0:
            return
        price_chg_pct = (state.close - state.open) / state.open * 100
        abs_chg = abs(price_chg_pct)
        if abs_chg < config.PRICE_CHANGE_MIN_PCT:
            return

        # 1b. Minimum liquidity guard: bỏ qua cặp có avg vol quá nhỏ (micro-volume)
        # Tránh false spike khi $200/nến bị tính là spike vì baseline off-hours thấp
        min_usdt = config.MIN_AVG_VOL_USDT
        if min_usdt > 0 and 0 < state.avg_volume_usdt < min_usdt:
            logger.debug(
                f"⏭️ SKIP {state.symbol}: avg_vol_usdt={state.avg_volume_usdt:.0f} < {min_usdt:.0f} min"
            )
            return

        # 2. Skip REST API override (WS volume is accurate on the real Binance network).
        # Fetching REST with limit=1 returns the new open candle's volume (~0), wiping out the spike data.
        pass

        if state.volume <= 0:
            return

        # 3. Spike ratio check
        threshold = config.VOLUME_SPIKE_RATIO
        if is_live:
            threshold *= config.LIVE_CANDLE_SPIKE_MULTIPLIER

        spike_ratio = state.volume / state.avg_volume
        if spike_ratio < threshold:
            return

        # Cooldown check (TRƯỚC khi await)
        now = time.time()
        if now - state.last_alert_ts < config.ALERT_COOLDOWN_SEC:
            return

        # Cập nhật ngay để khóa symbol này lại
        state.last_alert_ts = now
        state.alert_count  += 1

        # Re-calculate ratio with current volume (already fetched above — no 2nd API call)
        spike_ratio = state.volume / state.avg_volume
        if spike_ratio < threshold:
            # Volume không đủ sau khi cập nhật — rollback
            state.last_alert_ts = 0
            state.alert_count -= 1
            return

        # SIGNAL FILTER: danger zone check
        if abs_chg > config.DELTA_PRICE_MAX_PCT:
            logger.info(f"⏭️ SKIP {state.symbol}: delta {abs_chg:.2f}% too high")
            state.last_alert_ts = 0
            return

        candle_label = "CLOSED" if not is_live else "LIVE"
        logger.info(
            f"🚨 SPIKE {state.symbol} [{candle_label}]: {spike_ratio:.1f}x "
            f"| Δprice={price_chg_pct:+.2f}% "
            f"| vol={state.volume:.0f} / avg={state.avg_volume:.0f}"
        )
        # Truyền thêm price_chg_pct để scanner lưu làm baseline cho confirm candle
        await self._on_spike(state.symbol, state, spike_ratio, price_chg_pct)

    # ------------------------------------------------------------------ history fetch

    async def _bulk_fetch_history(self, symbols: list[str]):
        sem = asyncio.Semaphore(150)

        async def fetch(sym):
            async with sem:
                await self._fetch_history(sym)

        await asyncio.gather(*[fetch(s) for s in symbols], return_exceptions=True)
        ready = sum(1 for s in self.states.values() if s.avg_volume > 0)
        logger.info(f"📊 History loaded: {ready}/{len(symbols)} symbols")

    async def _fetch_history(self, symbol: str):
        """
        Fetch đủ nến để tính:
        - weighted avg volume (N nến gần nhất)
        - bb_width (Bollinger Bands Width)
        - close_prices seed (chỉ khi deque còn rỗng — P1)
        - ohlc_history seed (lấy đủ để vẽ chart ngay sau khi khởi động)

        P1: close_prices KHÔNG bị clear trong refresh. Chỉ seed lần đầu
        (deque rỗng). Sau đó stream realtime tự append khi nến đóng.
        avg_volume và bb_width vẫn được refresh định kỳ từ REST.
        """
        n      = config.VOLUME_CANDLES_AVG
        bb_min = config.BB_PERIOD
        limit  = max(n, bb_min, config.EMA_SLOW + 5) + 1  # +1 cho nến đang chạy

        try:
            url    = f"{config.BINANCE_REST_URL}/fapi/v1/klines"
            params = {"symbol": symbol, "interval": config.KLINE_INTERVAL, "limit": limit}
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return
                klines = await r.json()

            if len(klines) < max(5, n):
                return

            closed = klines[:-1]  # bỏ nến đang chạy

            # Weighted avg volume — resistant to off-hours low-volume candles
            # Problem: nếu bot khởi động lúc đêm, 20 nến baseline có thể bị
            # dominated bởi các nến volume cực thấp (ví dụ 168K MEME vs 44M MEME ban ngày)
            # → tạo ra false spike khi market hoạt động bình thường trở lại.
            #
            # Fix: dùng P10 floor — nếu wavg < median*0.3 → thay bằng trung vị
            # Tránh trường hợp cực đoan mà không bỏ outlier hợp lệ
            import statistics
            vols    = [float(k[5]) for k in closed[-n:]]
            weights = list(range(1, len(vols) + 1))
            w_avg   = sum(w * v for w, v in zip(weights, vols)) / sum(weights)

            # P10 floor: nếu các nến có volume < p10 kéo avg xuống quá thấp → dùng median
            vol_median = statistics.median(vols)
            vol_p10    = sorted(vols)[max(0, len(vols)//10)]  # 10th percentile
            # Baseline an toàn: weighted avg nhưng loại trừ nến < p10
            vols_filtered  = [v for v in vols if v >= vol_p10]
            weights_f      = list(range(1, len(vols_filtered) + 1))
            w_avg_filtered = (sum(w * v for w, v in zip(weights_f, vols_filtered))
                              / sum(weights_f)) if vols_filtered else w_avg
            # Dùng max để luôn lấy baseline cao hơn
            w_avg = max(w_avg, w_avg_filtered)

            # Quote volume avg để check minimum liquidity
            vols_q  = [float(k[7]) for k in closed[-n:]]
            weights_q = list(range(1, len(vols_q) + 1))
            w_avg_q = sum(w * v for w, v in zip(weights_q, vols_q)) / sum(weights_q)

            # Bollinger Bands Squeeze
            bb_candles = closed[-bb_min:]
            bb_width = 0.0
            if len(bb_candles) == bb_min:
                closes_arr = np.array([float(k[4]) for k in bb_candles])
                sma = np.mean(closes_arr)
                std_dev = np.std(closes_arr)
                if sma > 0:
                    bb_width = (4 * std_dev) / sma * 100

            state = self.states.get(symbol)
            if not state:
                return

            # Luôn update avg và bb_width (cần refresh định kỳ)
            state.avg_volume      = w_avg
            state.avg_volume_usdt = w_avg_q   # dùng để check min liquidity
            state.last_avg_update = time.time()
            state.bb_width        = bb_width

            # P1: chỉ seed close_prices nếu deque còn rỗng (lần đầu khởi động)
            # Sau đó stream tự duy trì — không bao giờ clear trong refresh
            if not state.close_prices:
                closes = [float(k[4]) for k in closed]
                state.close_prices.extend(closes)

            # CHART: seed ohlc_history nếu rỗng, lấy tối đa 50 nến gần nhất
            if not state.ohlc_history:
                for k in closed[-50:]:
                    state.ohlc_history.append((
                        float(k[1]),  # open
                        float(k[2]),  # high
                        float(k[3]),  # low
                        float(k[4]),  # close
                        float(k[5])   # volume
                    ))

            # Seed quote_volume_history if empty, max 50 candles
            if not state.quote_volume_history:
                for k in closed[-50:]:
                    state.quote_volume_history.append(float(k[7]))

            if not state.context_history:
                for k in closed[-80:]:
                    state.context_history.append({
                        "t": k[0], "o": k[1], "h": k[2], "l": k[3],
                        "c": k[4], "v": k[5], "q": k[7], "V": k[9], "x": True,
                    })

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.debug(f"_fetch_history {symbol}: {e}")

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(config.VOLUME_UPDATE_INTERVAL)
            symbols = list(self.states.keys())
            if symbols:
                logger.debug(f"🔄 Refreshing avg_volume + sideways for {len(symbols)} symbols")
                await self._bulk_fetch_history(symbols)


# ------------------------------------------------------------------

# BUG-L6 FIX: _interval_minutes() moved to VolumeSniper/utils.py to avoid duplication.
# Imported as _interval_minutes above for backward compatibility with existing call sites.