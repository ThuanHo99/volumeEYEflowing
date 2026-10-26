"""Integration tests for notifiers.telegram_bot.

These tests require a real Telegram bot token and chat ID configured via .env.
"""

import pytest
from core.signals import Signal, SignalType, SignalLevel
from notifiers.telegram_bot import TelegramNotifier


@pytest.mark.integration
class TestTelegramIntegration:
    def setup_method(self):
        self.notifier = TelegramNotifier(token=None, chat_id=None)

    def test_send_to_telegram(self):
        """Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."""
        pytest.skip("Real Telegram send requires valid credentials — run manually with python -m pytest -m integration -x")

    def test_rate_limit(self):
        """Rate-limit prevents sending same signal type within RATE_LIMIT_SECONDS."""
        pytest.skip("Real Telegram rate-limit test requires valid credentials — run manually")
