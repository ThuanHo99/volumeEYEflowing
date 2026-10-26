"""Unit tests for core.market_sentiment.

Tests cover classify_trend, derive_sentiment_from_news, and derive_trend_from_ohlcv.
No external APIs — all tests are synchronous and deterministic.
"""

import pytest
from core.market_sentiment import (
    TrendLabel,
    FearGreedData,
    BinanceTickerData,
    classify_trend,
    derive_sentiment_from_news,
    derive_trend_from_ohlcv,
)


class TestClassifyTrend:
    """Tests for the classify_trend function with all 5 threshold ranges."""

    def test_strong_bullish(self):
        """>= +5.0% should return Strong Bullish."""
        assert classify_trend(5.0) == TrendLabel.STRONG_BULLISH
        assert classify_trend(10.0) == TrendLabel.STRONG_BULLISH
        assert classify_trend(100.0) == TrendLabel.STRONG_BULLISH

    def test_bullish(self):
        """+1.0% to +4.99% should return Bullish."""
        assert classify_trend(1.0) == TrendLabel.BULLISH
        assert classify_trend(2.5) == TrendLabel.BULLISH
        assert classify_trend(4.99) == TrendLabel.BULLISH

    def test_sideways(self):
        """>-1.0% to <+1.0% should return Sideways."""
        assert classify_trend(0.0) == TrendLabel.SIDEWAYS
        assert classify_trend(0.5) == TrendLabel.SIDEWAYS
        assert classify_trend(-0.99) == TrendLabel.SIDEWAYS

    def test_bearish(self):
        """>-5.0% to <=-1.0% should return Bearish."""
        assert classify_trend(-1.0) == TrendLabel.BEARISH
        assert classify_trend(-2.5) == TrendLabel.BEARISH
        assert classify_trend(-4.99) == TrendLabel.BEARISH

    def test_strong_bearish(self):
        """<= -5.0% should return Strong Bearish."""
        assert classify_trend(-5.0) == TrendLabel.STRONG_BEARISH
        assert classify_trend(-10.0) == TrendLabel.STRONG_BEARISH
        assert classify_trend(-100.0) == TrendLabel.STRONG_BEARISH

    def test_edge_case_boundaries(self):
        """Exact boundary values must map correctly."""
        assert classify_trend(5.0) == TrendLabel.STRONG_BULLISH
        assert classify_trend(4.9999) == TrendLabel.BULLISH
        assert classify_trend(1.0) == TrendLabel.BULLISH
        assert classify_trend(0.9999) == TrendLabel.SIDEWAYS
        assert classify_trend(-1.0) == TrendLabel.BEARISH
        assert classify_trend(-0.9999) == TrendLabel.SIDEWAYS
        assert classify_trend(-5.0) == TrendLabel.STRONG_BEARISH
        assert classify_trend(-4.9999) == TrendLabel.BEARISH


class TestDeriveSentimentFromNews:
    """Tests for news-based sentiment derivation."""

    def test_empty_news_returns_none(self):
        """Empty list should return None."""
        assert derive_sentiment_from_news([]) is None

    def test_no_keywords_returns_neutral(self):
        """Text with no sentiment keywords should return Neutral 50."""
        result = derive_sentiment_from_news(["Some random text about nothing"])
        assert result is not None
        assert result.value == 50
        assert result.classification == 'Neutral'

    def test_bullish_only_returns_greed(self):
        """Bullish-only keywords should map to high score."""
        result = derive_sentiment_from_news([
            "Bitcoin surges to new record high amid massive ETF inflow",
            "Bullish rally continues as institutional adoption grows",
        ])
        assert result is not None
        assert result.value >= 55
        assert result.classification in ('Greed', 'Extreme Greed')

    def test_bearish_only_returns_fear(self):
        """Bearish-only keywords should map to low score."""
        result = derive_sentiment_from_news([
            "Crypto market crashes as regulatory crackdown intensifies",
            "Panic sell-off continues, bearish sentiment dominates",
        ])
        assert result is not None
        assert result.value <= 45
        assert result.classification in ('Fear', 'Extreme Fear')

    def test_mixed_signals_returns_mid_range(self):
        """Equal bullish and bearish signals should be near 50."""
        result = derive_sentiment_from_news([
            "Bitcoin ETF approval drives surge in institutional adoption",
            "But SEC lawsuit and regulation fears trigger sell-off",
        ])
        assert result is not None
        # Mixed signals should be in the middle range
        assert 30 <= result.value <= 70


class TestDeriveTrendFromOhlcv:
    """Tests for OHLCV-based trend derivation."""

    def _make_state(self, closes_4h: list[float], closes_1h: list[float] = None):
        """Create a minimal mock state with close_history."""
        from unittest.mock import MagicMock
        state = MagicMock()
        state.close_history = {
            '4h': closes_4h,
            '1h': closes_1h if closes_1h is not None else [],
        }
        return state

    def test_bullish_trend(self):
        """+3% over window should return Bullish."""
        state = self._make_state(closes_4h=[100.0, 103.0])
        result = derive_trend_from_ohlcv(state, symbol='BTC/USDT:USDT')
        assert result is not None
        assert result.symbol == 'BTCUSDT'
        assert result.price_change_pct == 3.0
        assert result.trend == TrendLabel.BULLISH

    def test_bearish_trend(self):
        """-3% over window should return Bearish."""
        state = self._make_state(closes_4h=[100.0, 97.0])
        result = derive_trend_from_ohlcv(state)
        assert result is not None
        assert result.price_change_pct == -3.0
        assert result.trend == TrendLabel.BEARISH

    def test_sideways_trend(self):
        """Small change within +/-1% should return Sideways."""
        state = self._make_state(closes_4h=[100.0, 100.5])
        result = derive_trend_from_ohlcv(state)
        assert result is not None
        assert result.trend == TrendLabel.SIDEWAYS

    def test_falls_back_to_1h(self):
        """When 4h has insufficient data, should use 1h instead."""
        state = self._make_state(closes_4h=[], closes_1h=[50.0, 55.0])
        result = derive_trend_from_ohlcv(state)
        assert result is not None
        assert result.price_change_pct == 10.0
        assert result.trend == TrendLabel.STRONG_BULLISH

    def test_insufficient_data_returns_none(self):
        """When no timeframe has enough data, should return None."""
        state = self._make_state(closes_4h=[100.0], closes_1h=[])
        result = derive_trend_from_ohlcv(state)
        assert result is None

    def test_zero_old_price_returns_none(self):
        """Zero price should be treated as invalid."""
        state = self._make_state(closes_4h=[0.0, 100.0])
        result = derive_trend_from_ohlcv(state)
        assert result is None
