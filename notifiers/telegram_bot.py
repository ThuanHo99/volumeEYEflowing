import asyncio
import logging
import time
from datetime import datetime, timezone

from telegram import Bot
from telegram.error import TelegramError

from core.signals import Signal, SignalLevel
from core.config import WEB_HOST, WEB_PORT, TELEGRAM_RATE_LIMIT

logger = logging.getLogger(__name__)

RATE_LIMIT_SECONDS = TELEGRAM_RATE_LIMIT


class TokenRedactionFilter(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self._token = token
        self._replacement = token[:4] + '...' + token[-4:] if len(token) > 8 else '***REDACTED***'

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token:
            record.msg = str(record.msg).replace(self._token, self._replacement)
            if record.args:
                record.args = tuple(
                    str(a).replace(self._token, self._replacement) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None):
        self._bot: Bot | None = None
        self._token = token
        self._chat_id = chat_id
        self._last_sent: dict[str, float] = {}
        self._send_lock = asyncio.Lock()

        if self._token:
            logger.addFilter(TokenRedactionFilter(self._token))

    def _get_bot(self) -> Bot | None:
        if self._bot is None and self._token:
            self._bot = Bot(token=self._token)
        return self._bot

    def _format_message(self, signal: Signal) -> str:
        dt = datetime.fromtimestamp(signal.timestamp / 1000, tz=timezone.utc)
        time_str = dt.strftime('%H:%M:%S UTC')

        sym_label = signal.symbol.replace('/USDT:USDT', '').replace('/USDT', '')
        lines = [
            f"{signal.level.value} <b>{sym_label} — {signal.type.value}</b>",
            "",
            f"\U0001f4ca Timeframe: <code>{signal.tf}</code>",
            f"\U0001f4b0 Gia: <code>${signal.close:,.2f}</code>",
        ]
        if signal.rsi14 is not None:
            lines.append(f"\U0001f4c8 RSI 14: <code>{signal.rsi14:.2f}</code>")
        if signal.ema200 is not None:
            lines.append(f"〰️ EMA 200: <code>${signal.ema200:,.2f}</code>")
        if signal.volume is not None:
            lines.append(f"\U0001f4e6 Volume: <code>{signal.volume:.4f}</code>")
        lines += [
            "",
            f"\U0001f4dd <i>{signal.detail}</i>",
            f"\U0001f550 {time_str}",
        ]
        lines.append(f"\U0001f4ca <a href=\"http://{WEB_HOST}:{WEB_PORT}\">Mo Dashboard</a>")
        return "\n".join(lines)

    async def send_signal(self, signal: Signal) -> None:
        bot = self._get_bot()
        if bot is None:
            logger.warning("Telegram not configured (missing BOT_TOKEN or CHAT_ID)")
            return

        if not self._chat_id:
            logger.warning("Telegram not configured (missing CHAT_ID)")
            return

        # BUG-M5 FIX: Include symbol in the rate-limit key.
        # The original key f"{signal.type.value}_{signal.tf}" meant an ETH/USDT signal
        # of the same type would suppress a BTC/USDT signal within the cooldown window.
        # Users silently missed legitimate alerts on other symbols.
        # SignalCooldownManager in signals.py already uses symbol — this must match.
        key = f"{signal.symbol}_{signal.type.value}_{signal.tf}"
        now = time.time()

        async with self._send_lock:
            if key in self._last_sent and (now - self._last_sent[key]) < RATE_LIMIT_SECONDS:
                logger.debug(f"Rate limit: skipping {key} (sent {now - self._last_sent[key]:.0f}s ago)")
                return
            self._last_sent[key] = now

        message = self._format_message(signal)

        try:
            await bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode='HTML',
            )
            logger.info(f"Telegram sent: {key}")
        except TelegramError as e:
            logger.warning(f"Telegram error (non-fatal): {e}")
        except Exception as e:
            logger.error("Unexpected error sending Telegram: %s", e, exc_info=True)

    async def close(self) -> None:
        if self._bot is not None:
            await self._bot.close()
            self._bot = None
