"""
telegram_bot.py - VolumeSniper

Gửi alert volume spike + xử lý Telegram commands.
Không có trading. Tất cả lệnh đặt thủ công trên sàn.
"""

import asyncio
import aiohttp
import html
import io
import logging
import time
import json
import os
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    from .scanner import Scanner, _PendingAlert
    from .analyzer import AnalysisResult
    from .volume_context import ContextSignal

logger = logging.getLogger(__name__)

# ── Emoji maps ──────────────────────────────────────────────────────────────
_SIG   = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "🟡", "SKIP": "⚪"}
_CONF  = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "💤"}
_ZONE  = {"TREND": "📈", "REVERSAL": "🔄", "RISKY": "⚠️", "SIDEWAYS": "↔️"}
_TREND = {"UPTREND": "⬆️", "DOWNTREND": "⬇️", "NEUTRAL": "➡️"}


class TelegramBot:

    def __init__(self):
        self._session:  Optional[aiohttp.ClientSession] = None
        self._base    = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        self._chat_id = str(config.TELEGRAM_CHAT_ID)
        self._offset  = 0
        self._scanner: Optional["Scanner"] = None

        # Auth Management
        self._auth_file = "authorized_users.json"
        self._admin_id = str(config.ADMIN_CHAT_ID)
        # Sử dụng set để đảm bảo không có duplicate ID
        self._authorized_users: set[str] = set(self._load_auth())

        # Nếu admin chưa có trong list thì tự thêm vào
        if self._admin_id not in self._authorized_users:
            self._authorized_users.add(self._admin_id)
            self._save_auth()

        # Stats
        self.alert_count = 0
        self.skip_count  = 0
        self._start_time = time.time()

    def _load_auth(self) -> list[str]:
        if os.path.exists(self._auth_file):
            try:
                with open(self._auth_file, "r") as f:
                    data = json.load(f)
                    return list(data) if isinstance(data, list) else []
            # BUG-M7 FIX: Replaced bare except: with specific exceptions.
            # A bare except: catches SystemExit, KeyboardInterrupt and CancelledError.
            # More critically, auth file corruption was silently discarded — all
            # authorized users would lose access with no log warning.
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Failed to load auth file '{self._auth_file}': {e} — starting with empty authorized list")
                return []
        return []

    def _save_auth(self):
        try:
            with open(self._auth_file, "w") as f:
                json.dump(list(self._authorized_users), f)
        except Exception as e:
            logger.error(f"Error saving auth: {e}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self, scanner=None):
        self._scanner = scanner
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        )
        me = await self._get_me()
        if me:
            logger.info(f"✅ Telegram @{me} connected")

        batch_info = (
            f"\n🗂️ Alert batching: <b>{'BẬT' if config.ALERT_BATCH_WINDOW_SEC > 0 else 'TẮT'}</b> "
            f"(window {config.ALERT_BATCH_WINDOW_SEC}s, max {config.ALERT_BATCH_MAX}/msg)"
            if config.ALERT_BATCH_WINDOW_SEC > 0 else ""
        )

        # Thông báo bot khởi động (chỉ gửi cho admin để tránh làm phiền member)
        await self._send(
            "🚀 <b>VolumeSniper đã khởi động!</b>\n\n"
            f"📡 Theo dõi <b>{config.MAX_SYMBOLS}</b> cặp Binance Futures\n"
            f"⚡ Ngưỡng: <b>{config.VOLUME_SPIKE_RATIO}x</b> volume "
            f"(avg {config.VOLUME_CANDLES_AVG} nến, khung {config.KLINE_INTERVAL})\n"
            f"📉 Min price change: <b>{config.PRICE_CHANGE_MIN_PCT}%</b>\n"
            f"↔️ Squeeze filter: <b>{'BẬT' if config.BB_SQUEEZE_ENABLED else 'TẮT'}</b> "
            f"(BBW max {config.BB_WIDTH_MAX_PCT}% / {config.BB_PERIOD} nến)"
            f"{batch_info}\n\n"
            "Dùng /help để xem lệnh điều khiển.",
            chat_id=self._admin_id
        )
        asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._session:
            await self._session.close()

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def send_context_alert(self, signal: "ContextSignal", chart_bytes: Optional[io.BytesIO] = None) -> bool:
        """v2: evidence and invalidation; no fixed TP/SL or implied win rate."""
        direction = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
        scenario = "Breakout held" if signal.scenario == "BREAKOUT" else "Breakout failed"
        when = datetime.fromtimestamp((signal.confirmed_close_ts or signal.confirmed_ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        plan_line = ""
        if signal.plan_ts is not None and signal.plan_lower is not None and signal.plan_upper is not None:
            plan_when = datetime.fromtimestamp(signal.plan_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            plan_line = (
                f"Vùng lập trước sự kiện ({plan_when}): "
                f"<code>{signal.plan_lower:.6g}–{signal.plan_upper:.6g}</code>\n"
            )
        delta = "N/A" if signal.delta_pct is None else f"{signal.delta_pct:+.1f}%"
        cvd = "N/A" if signal.cvd is None else f"{signal.cvd:+.6g} base units"
        text = (
            f"{direction} <b>{html.escape(signal.symbol)}</b> — {scenario}\n"
            f"Giá lúc xác nhận: <code>{signal.price:.6g}</code>\n"
            f"{plan_line}"
            f"Vùng kiểm tra: <code>{signal.level:.6g}</code>\n"
            f"Vô hiệu nếu giữ qua: <code>{signal.invalidation:.6g}</code>\n"
            f"Khoảng tới mức vô hiệu: {abs(signal.price - signal.invalidation) / signal.price * 100:.2f}% (chưa tính phí/trượt giá)\n"
            f"RVOL: <b>{signal.rvol:.1f}x</b> ({html.escape(signal.baseline)})\n"
            f"Taker delta nến sự kiện: {delta} | CVD gần đây: {cvd}\n"
            f"Nến xác nhận: {when}\n"
            "Đây là bối cảnh đã xác nhận, không phải dự đoán chắc chắn. Xét phí và trượt giá trước khi giao dịch."
        )
        self.alert_count += 1
        photo_data = None
        if chart_bytes:
            chart_bytes.seek(0)
            photo_data = chart_bytes.read()
        tasks = [
            self._send_photo(text, io.BytesIO(photo_data), chat_id=uid) if photo_data
            else self._send(text, chat_id=uid)
            for uid in self._authorized_users
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return bool(results) and all(r is True for r in results)

    async def send_alert(self, symbol: str, analysis: "AnalysisResult",
                         price: float, spike_ratio: float,
                         chart_bytes: Optional[io.BytesIO] = None) -> bool:
        if analysis.signal == "SKIP":
            self.skip_count += 1
            logger.info(f"⏭️ SKIP {symbol}: {'; '.join(analysis.reasons)}")
            return False

        self.alert_count += 1
        sig_e  = _SIG.get(analysis.signal, "⚪")
        conf_e = _CONF.get(analysis.confidence, "")
        zone_e = _ZONE.get(analysis.zone, "")
        trend_e = _TREND.get(analysis.ema_trend, "")

        rsi_str = f"{analysis.rsi:.1f}" if analysis.rsi is not None else "N/A"
        chg_ico = "🔺" if analysis.price_change >= 0 else "🔻"

        link = f"https://www.binance.com/en/futures/{symbol}"
        acc_ico  = "✅" if analysis.in_accumulation else "❌"
        acc_line = f"\n{acc_ico} <b>Tích lũy</b>: BBW <code>{analysis.bb_width_pct:.1f}%</code>"

        reasons_text = "\n".join(f" • {r}" for r in analysis.reasons) \
            if analysis.reasons else " • Không có cảnh báo đặc biệt"

        # --- Trading Suggestions (Backtest Optimized) ---
        tp_val = config.DEFAULT_TP_PCT
        sl_val = config.DEFAULT_SL_PCT
        
        if analysis.signal == 'LONG':
            tp_price = price * (1 + tp_val / 100)
            sl_price = price * (1 - sl_val / 100)
            tp_text = f"🎯 <b>TP</b>: <code>{tp_price:.6g}</code> (+{tp_val}%)"
            sl_text = f"🛑 <b>SL</b>: <code>{sl_price:.6g}</code> (-{sl_val}%)"
        else:
            tp_price = price * (1 - tp_val / 100)
            sl_price = price * (1 + sl_val / 100)
            tp_text = f"🎯 <b>TP</b>: <code>{tp_price:.6g}</code> (-{tp_val}%)"
            sl_text = f"🛑 <b>SL</b>: <code>{sl_price:.6g}</code> (+{sl_val}%)"

        trading_suggestion = (
            f"💡 <b>Gợi ý Trading:</b>\n"
            f"📥 <b>Entry</b>: <code>{price:.6g}</code>\n"
            f"{tp_text}\n"
            f"{sl_text}"
        )

        text = (
            f"{sig_e}{conf_e} <a href='{link}'><b>{symbol}</b></a> {analysis.signal} "
            f"({analysis.zone})\n\n"
            f"💰 Giá: <code>{price:.6g}</code> {chg_ico} <b>{analysis.price_change:+.1f}%</b>\n"
            f"📊 Volume Spike: <b>{spike_ratio:.1f}x</b>\n"
            f"📈 Trend: <b>{analysis.ema_trend}</b> {trend_e}\n"
            f"🌀 RSI: <b>{rsi_str}</b>\n"
            f"📍 Zone: <b>{analysis.zone}</b> {zone_e}"
            f"{acc_line}\n\n"
            f"📝 <b>Phân tích:</b>\n{reasons_text}\n\n"
            f"{trading_suggestion}\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')} | ⚠️ <i>Tự đặt lệnh!</i>"
        )

        try:
            photo_data = None
            if chart_bytes:
                chart_bytes.seek(0)
                photo_data = chart_bytes.read()

            tasks = []
            for uid in self._authorized_users:
                if photo_data:
                    tasks.append(self._send_photo(text, io.BytesIO(photo_data), chat_id=uid))
                else:
                    tasks.append(self._send(text, chat_id=uid))
            
            await asyncio.gather(*tasks)
            logger.info(f"📲 Alert #{self.alert_count} sent: {symbol} {analysis.signal} {spike_ratio:.1f}x (to {len(self._authorized_users)} users)")
            return True
        except Exception as e:
            logger.error(f"send_alert error: {e}")
            return False

    async def send_batch_alert(self, batch: list) -> bool:
        """Gửi 1 summary message cho nhiều spikes xảy ra cùng lúc."""
        alerts = [a for a in batch if a.analysis.signal != "SKIP"]
        skips  = len(batch) - len(alerts)
        self.skip_count += skips

        if not alerts:
            return True

        self.alert_count += len(alerts)
        now_str = datetime.now().strftime("%H:%M:%S")
        lines   = [
            f"⚡ <b>MARKET SPIKE × {len(alerts)} SYMBOLS</b>",
            f"━━━━━━━━━━━━━━━━━━━",
        ]

        for a in alerts:
            sig_e  = _SIG.get(a.analysis.signal, "⚪")
            conf_e = _CONF.get(a.analysis.confidence, "")
            chg_ico = "🔺" if a.analysis.price_change >= 0 else "🔻"
            rsi_str = f"{a.analysis.rsi:.0f}" if a.analysis.rsi is not None else "?"
            link    = f"https://www.binance.com/en/futures/{a.symbol}"

            lines.append(
                f"{sig_e}{conf_e} <a href='{link}'><b>{html.escape(a.symbol)}</b></a>  "
                f"<code>{a.spike_ratio:.1f}x</code>  "
                f"{chg_ico}<b>{a.analysis.price_change:+.1f}%</b>  "
                f"RSI <code>{rsi_str}</code>  "
                f"<i>{a.analysis.zone}</i>"
            )

        if skips:
            lines.append(f"━━━━━━━━━━━━━━━━━━━")
            lines.append(f"⏭️ <i>{skips} signal(s) bị SKIP</i>")

        lines += [
            f"━━━━━━━━━━━━━━━━━━━",
            f"⏰ {now_str} | ⚠️ <i>Tự đặt lệnh thủ công!</i>",
        ]
        
        summary = "\n".join(lines)
        tasks = [self._send(summary, chat_id=uid, disable_preview=True) for uid in self._authorized_users]
        await asyncio.gather(*tasks)
        logger.info(f"📲 Batch alert sent to {len(self._authorized_users)} users: {len(alerts)} symbols")
        return True

    async def send_photo(self, photo: io.BytesIO, caption: str):
        """Public method để gửi ảnh cho tất cả authorized users."""
        photo.seek(0)
        photo_data = photo.read()
        tasks = [self._send_photo(caption, io.BytesIO(photo_data), chat_id=uid) for uid in self._authorized_users]
        await asyncio.gather(*tasks)

    # ── Internal Sending ─────────────────────────────────────────────────────

    async def _send(self, text: str, disable_preview: bool = True, chat_id: str = None) -> bool:
        if not self._session: return False
        target = chat_id or self._admin_id
        try:
            async with self._session.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":                  target,
                    "text":                     text,
                    "parse_mode":               "HTML",
                    "disable_web_page_preview": disable_preview,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
                return data.get("ok", False)
        except Exception as e:
            logger.error(f"_send error: {e}")
            return False

    async def _send_photo(self, caption: str, photo: io.BytesIO, chat_id: str = None) -> bool:
        if not self._session: return False
        target = chat_id or self._admin_id
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", target)
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
            form.add_field("photo", photo, filename="chart.png", content_type="image/png")

            async with self._session.post(
                f"{self._base}/sendPhoto",
                data=form,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json()
                return data.get("ok", False)
        except Exception as e:
            logger.error(f"_send_photo error: {e}")
            return False

    async def _send_with_keyboard(self, chat_id: str, text: str, keyboard: dict):
        # BUG-L7 FIX: Added timeout to prevent indefinite block when Telegram API hangs.
        # Without a timeout, this coroutine could block the poll_loop and all keyboard
        # interactions if the Telegram API is slow or unresponsive.
        try:
            async with self._session.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":      chat_id,
                    "text":         text,
                    "parse_mode":   "HTML",
                    "reply_markup": keyboard
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return await r.json()
        except Exception as e:
            logger.error(f"Keyboard send error: {e}")

    # ── Polling & Dispatch ───────────────────────────────────────────────────

    async def _poll_loop(self):
        while True:
            try:
                async with self._session.get(
                    f"{self._base}/getUpdates",
                    params={
                        "offset":          self._offset,
                        "timeout":         30,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as r:
                    data = await r.json()
                    if data.get("ok"):
                        for upd in data.get("result", []):
                            self._offset = upd["update_id"] + 1
                            await self._dispatch(upd)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll error: {e}")
                await asyncio.sleep(5)

    async def _dispatch(self, update: dict):
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        msg     = update.get("message", {})
        text    = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        user    = msg.get("from", {})

        if not text:
            return

        # 1. Nếu là người lạ (chưa duyệt)
        if chat_id not in self._authorized_users:
            if text.startswith("/start"):
                await self._send_intro(chat_id)
            else:
                await self._send("⚠️ Bạn chưa được cấp quyền sử dụng bot. Vui lòng bấm /start để đăng ký.", chat_id=chat_id)
            return

        # 2. Nếu đã duyệt -> Xử lý lệnh
        parts = text.split()
        if not parts:
            return

        cmd = parts[0].lower().split("@")[0]
        handlers = {
            "/help":    self._cmd_help,
            "/status":  self._cmd_status,
            "/config":  self._cmd_config,
            "/stats":   self._cmd_stats,
            "/pause":   self._cmd_pause,
            "/resume":  self._cmd_resume,
            "/symbols": self._cmd_symbols,
            "/set":     self._cmd_set,
        }
        
        handler = handlers.get(cmd)
        if handler:
            # Các lệnh không cần tham số
            if cmd in ["/help", "/status", "/config", "/stats", "/pause", "/resume", "/symbols"]:
                await handler(chat_id)
            else:
                await handler(parts)

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _send_intro(self, chat_id: str):
        intro = (
            "🚀 <b>Chào mừng bạn đến với VolumeSniper!</b>\n\n"
            "Bot chuyên biệt săn tìm các điểm bùng nổ khối lượng (Volume Spike) trên thị trường Binance Futures.\n\n"
            "🎯 <b>Mục tiêu của chúng tôi:</b>\n"
            "• Phát hiện sớm các cú Breakout thực sự từ vùng tích lũy (Squeeze).\n"
            "• Loại bỏ nhiễu thị trường bằng thuật toán Bollinger Bands nâng cao.\n"
            "• Giúp bạn không bỏ lỡ những con sóng lớn từ Top 500 cặp giao dịch.\n\n"
            "🛠 <b>Cách hoạt động:</b>\n"
            "Bot quét liên tục dữ liệu Real-time. Khi hội đủ điều kiện khắt khe về Volume và Giá, bot sẽ gửi thông báo trực tiếp kèm biểu đồ phân tích.\n\n"
            "📝 <b>Yêu cầu:</b> Hiện bot đang ở chế độ giới hạn thành viên. Vui lòng bấm nút dưới đây để gửi yêu cầu cho Admin."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "📝 Đăng ký ngay", "callback_data": "register"}
            ]]
        }
        await self._send_with_keyboard(chat_id, intro, keyboard)

    async def _handle_callback(self, cb: dict):
        chat_id = str(cb["message"]["chat"]["id"])
        data    = cb["data"]
        user    = cb["from"]
        username = user.get("username") or user.get("first_name") or "User"
        uid     = str(user["id"])

        if data == "register":
            admin_msg = f"🔔 <b>Yêu cầu đăng ký mới</b>\n\nUser: @{username}\nID: <code>{uid}</code>"
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Duyệt", "callback_data": f"approve_{uid}"},
                    {"text": "❌ Từ chối", "callback_data": f"reject_{uid}"}
                ]]
            }
            await self._send_with_keyboard(self._admin_id, admin_msg, keyboard)
            await self._send("✅ Yêu cầu của bạn đã được gửi. Vui lòng đợi Admin phê duyệt.", chat_id=uid)
        
        elif data.startswith("approve_") and chat_id == self._admin_id:
            target_uid = data.replace("approve_", "")
            if target_uid not in self._authorized_users:
                self._authorized_users.add(target_uid)
                self._save_auth()
                await self._send("🎊 Chúc mừng! Bạn đã được Admin phê duyệt sử dụng bot VolumeSniper.", chat_id=target_uid)
                await self._send(f"✅ Đã duyệt User {target_uid}", chat_id=self._admin_id)
        
        elif data.startswith("reject_") and chat_id == self._admin_id:
            target_uid = data.replace("reject_", "")
            await self._send("❌ Yêu cầu tham gia của bạn đã bị từ chối.", chat_id=target_uid)
            await self._send(f"❌ Đã từ chối User {target_uid}", chat_id=self._admin_id)

    async def _cmd_help(self, chat_id: str):
        set_help = (" /set rvol 2.0\n /set cooldown 600\n"
                    if config.STRATEGY_MODE == "context_v2" else
                    " /set spike 6.0\n /set cooldown 600\n /set bb_squeeze on|off\n /set price_chg 1.0\n")
        await self._send(
            "📖 <b>VolumeSniper Commands</b>\n\n"
            "/status – Trạng thái & uptime\n"
            "/config – Xem cấu hình hiện tại\n"
            "/stats – Thống kê alert\n"
            "/symbols – Số symbols đang theo dõi\n"
            "/pause – Tạm dừng gửi alert\n"
            "/resume – Tiếp tục gửi alert\n"
            "/set &lt;key&gt; &lt;value&gt; – Thay đổi config\n\n"
            "<b>Ví dụ /set:</b>\n"
            f"{set_help}\n"
            "<i>Tự đặt lệnh thủ công trên Binance Futures.</i>",
            chat_id=chat_id
        )

    async def _cmd_status(self, chat_id: str):
        uptime   = int(time.time() - self._start_time)
        h, m, s  = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        paused   = "⏸️ PAUSED" if (self._scanner and self._scanner._paused) else "▶️ RUNNING"
        await self._send(
            f"✅ <b>Bot Status: {paused}</b>\n\n"
            f"⏱️ Uptime: <code>{h:02d}:{m:02d}:{s:02d}</code>\n"
            f"📋 Symbols: <code>{self._scanner.symbol_count if self._scanner else '?'}</code>\n"
            f"🔎 Context scenarios: <code>{self._scanner._context_confirmed if self._scanner and self._scanner.context else 'N/A'}</code>\n"
            f"🔔 Alerts sent: <code>{self.alert_count}</code>\n"
            f"⏭️ Alerts skipped: <code>{self.skip_count}</code>",
            chat_id=chat_id
        )

    async def _cmd_config(self, chat_id: str):
        if config.STRATEGY_MODE == "context_v2":
            await self._send(
                "⚙️ <b>Volume context v2</b>\n"
                f"RVOL min: <code>{config.CONTEXT_RVOL_MIN}x</code>\n"
                f"Range lookback: <code>{config.CONTEXT_LOOKBACK_CANDLES}</code> nến\n"
                f"Confirmation window: <code>{config.CONTEXT_CONFIRM_CANDLES}</code> nến\n"
                f"Level tolerance: <code>{config.CONTEXT_LEVEL_TOLERANCE_PCT}%</code>\n"
                f"Cooldown: <code>{config.ALERT_COOLDOWN_SEC}s</code>\n"
                f"Telegram context alerts: <code>{'ON' if config.CONTEXT_TELEGRAM_ALERTS_ENABLED else 'OFF (research mode)'}</code>\n"
                "Seasonal baseline cần ít nhất 2 tuần cùng giờ/ngày; trước đó dùng rolling median tạm thời.",
                chat_id=chat_id,
            )
            return
        sw = "✅ BẬT" if config.BB_SQUEEZE_ENABLED else "❌ TẮT"
        batch_info = (
            f"\n\n<b>Alert Batching</b>\n"
            f" • Window: <code>{config.ALERT_BATCH_WINDOW_SEC}s</code>\n"
            f" • Max/msg: <code>{config.ALERT_BATCH_MAX}</code>"
        ) if config.ALERT_BATCH_WINDOW_SEC > 0 else "\n\n<b>Alert Batching</b>: <code>TẮT</code>"

        await self._send(
            f"⚙️ <b>Cấu hình hiện tại</b>\n\n"
            f"<b>Volume Spike</b>\n"
            f" • Ngưỡng spike: <code>{config.VOLUME_SPIKE_RATIO}x</code>\n"
            f" • Avg candles: <code>{config.VOLUME_CANDLES_AVG}</code>\n"
            f" • Interval: <code>{config.KLINE_INTERVAL}</code>\n"
            f" • Min price Δ: <code>{config.PRICE_CHANGE_MIN_PCT}%</code>\n"
            f" • Closed candle only: <code>{'✅' if config.REQUIRE_CLOSED_CANDLE else '❌'}</code>\n\n"
            f"<b>Indicators</b>\n"
            f" • EMA fast/slow: <code>{config.EMA_FAST}/{config.EMA_SLOW}</code>\n"
            f" • RSI period: <code>{config.RSI_PERIOD}</code>\n"
            f" • RSI trend lim: <code>{config.RSI_TREND_LIMIT}</code>\n"
            f" • RSI OB/OS: <code>{config.RSI_OVERBOUGHT}/{config.RSI_OVERSOLD}</code>\n"
            f" • Wick reject: <code>{config.WICK_REJECT_PCT:.0%}</code>\n\n"
            f"<b>Trading Tips</b>\n"
            f" • Default TP: <code>{config.DEFAULT_TP_PCT}%</code>\n"
            f" • Default SL: <code>{config.DEFAULT_SL_PCT}%</code>\n\n"
            f"<b>Filters</b>\n"
            f" • Tích lũy: {sw} "
            f"(max {config.BB_WIDTH_MAX_PCT}% / {config.BB_PERIOD} nến)\n"
            f" • Cooldown: <code>{config.ALERT_COOLDOWN_SEC}s</code>\n"
            f" • Max symbols: <code>{config.MAX_SYMBOLS}</code>"
            f"{batch_info}",
            chat_id=chat_id
        )

    async def _cmd_stats(self, chat_id: str):
        total = self.alert_count + self.skip_count
        rate  = f"{self.alert_count/total*100:.0f}%" if total else "—"
        await self._send(
            f"📈 <b>Thống kê</b>\n\n"
            f"🔔 Alerts gửi: <code>{self.alert_count}</code>\n"
            f"⏭️ Alerts skip: <code>{self.skip_count}</code>\n"
            f"📊 Alert rate: <code>{rate}</code>",
            chat_id=chat_id
        )

    async def _cmd_pause(self, chat_id: str):
        if self._scanner: self._scanner.pause()
        await self._send("⏸️ <b>Bot đã tạm dừng.</b>\nDùng /resume để tiếp tục.", chat_id=chat_id)

    async def _cmd_resume(self, chat_id: str):
        if self._scanner: self._scanner.resume()
        await self._send("▶️ <b>Bot đã tiếp tục chạy.</b>", chat_id=chat_id)

    async def _cmd_symbols(self, chat_id: str):
        n = self._scanner.symbol_count if self._scanner else 0
        await self._send(f"📋 Đang theo dõi <b>{n}</b> symbols Binance Futures USDT Perp.", chat_id=chat_id)

    async def _cmd_set(self, parts: list[str]):
        if len(parts) < 3: return
        key, val = parts[1].lower(), parts[2].lower()
        # BUG-M6 FIX: Replaced bare except: with specific ValueError/TypeError.
        # A bare except: catches SystemExit, KeyboardInterrupt, and asyncio.CancelledError,
        # which must NOT be swallowed. It also masked network errors from _send().
        try:
            if key == "cooldown":
                value = int(val)
                if value < 0: raise ValueError("must be nonnegative")
                config.ALERT_COOLDOWN_SEC = value
            elif key == "rvol" and config.STRATEGY_MODE == "context_v2":
                value = float(val)
                if value <= 0: raise ValueError("must be positive")
                config.CONTEXT_RVOL_MIN = value
            elif key == "spike" and config.STRATEGY_MODE == "legacy": config.VOLUME_SPIKE_RATIO = float(val)
            elif key == "price_chg" and config.STRATEGY_MODE == "legacy": config.PRICE_CHANGE_MIN_PCT = float(val)
            elif key == "bb_squeeze" and config.STRATEGY_MODE == "legacy": config.BB_SQUEEZE_ENABLED = val in ("on", "true", "1", "yes")
            elif key == "bb_width" and config.STRATEGY_MODE == "legacy": config.BB_WIDTH_MAX_PCT = float(val)
            else:
                await self._send(f"❌ Key không hợp lệ: <code>{key}</code>")
                return
            await self._send(f"✅ Đã cập nhật <code>{key}</code> = <code>{val}</code>")
        except (ValueError, TypeError) as e:
            await self._send(f"❌ Giá trị không hợp lệ: <code>{val}</code> ({e})")

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _get_me(self) -> Optional[str]:
        try:
            async with self._session.get(f"{self._base}/getMe", timeout=10) as r:
                data = await r.json()
                return data["result"].get("username") if data.get("ok") else None
        except: return None
