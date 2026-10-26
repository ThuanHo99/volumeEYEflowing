"""Unit tests for notifiers.telegram_bot.

Note: TelegramNotifier class is the only interface after Phase 2 refactor.
The old module-level send_signal and _format_message no longer exist.
"""

from core.signals import Signal, SignalType, SignalLevel
from notifiers.telegram_bot import TelegramNotifier


class TestTelegramFormatting:
    def setup_method(self):
        self.notifier = TelegramNotifier(token=None, chat_id=None)

    def test_format_message_has_all_fields(self):
        sig = Signal(
            type=SignalType.RSI_OVERBOUGHT,
            level=SignalLevel.WARNING,
            symbol='BTC/USDT:USDT',
            tf='15m', close=50000.0, rsi14=76.5, ema200=48000.0,
            volume=10.5, timestamp=1700000000000,
            detail="Test detail",
        )
        msg = self.notifier._format_message(sig)
        assert 'BTC' in msg and '/USDT' not in msg
        assert '15m' in msg
        assert '50,000.00' in msg
        assert '76.50' in msg
        assert '48,000.00' in msg
        assert '10.5000' in msg
