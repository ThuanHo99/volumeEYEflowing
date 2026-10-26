"""Tests for ingestion streaming: _fetch_gap_candles.

Covers:
- _fetch_gap_candles with mocked ccxt REST exchange
- Deduplication, boundary handling, error recovery, pagination

Note: stream_ohlcv is an infinite retry loop (while True with asyncio.gather)
that cannot be cleanly cancelled via task.cancel(). It is tested via integration
tests rather than unit tests.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestFetchGapCandles:
    """_fetch_gap_candles must fetch missed candles after reconnect."""

    def test_returns_closed_candles_since_timestamp(self):
        """_fetch_gap_candles must format candles correctly with is_closed=True."""
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.return_value = [
            [2000, 100.0, 101.0, 99.0, 100.5, 1000.0],
            [3000, 101.0, 102.0, 100.0, 101.5, 1100.0],
        ]

        with patch('ccxt.binance', return_value=mock_exchange):
            from core.ingestion import _fetch_gap_candles
            result = _fetch_gap_candles('5m', 1000, 'BTC/USDT', 'binance')

        assert len(result) == 2
        assert result[0] == {
            'tf': '5m', 'start_ts': 2000, 'open': 100.0, 'high': 101.0,
            'low': 99.0, 'close': 100.5, 'volume': 1000.0, 'is_closed': True,
        }
        assert result[1]['start_ts'] == 3000
        assert result[1]['is_closed'] is True

    def test_skips_candle_at_boundary(self):
        """Must skip candles with ts <= since_ts (only fetch strictly newer)."""
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.return_value = [
            [1000, 100.0, 101.0, 99.0, 100.5, 1000.0],
            [2000, 101.0, 102.0, 100.0, 101.5, 1100.0],
        ]

        with patch('ccxt.binance', return_value=mock_exchange):
            from core.ingestion import _fetch_gap_candles
            result = _fetch_gap_candles('5m', 1000, 'BTC/USDT', 'binance')

        assert len(result) == 1
        assert result[0]['start_ts'] == 2000

    def test_empty_when_no_data(self):
        """Must return empty list when exchange returns nothing."""
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.return_value = []

        with patch('ccxt.binance', return_value=mock_exchange):
            from core.ingestion import _fetch_gap_candles
            result = _fetch_gap_candles('5m', 1000, 'BTC/USDT', 'binance')

        assert result == []

    def test_handles_exchange_error(self):
        """Must return empty list on exchange error, not crash."""
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.side_effect = Exception("Connection reset")

        with patch('ccxt.binance', return_value=mock_exchange):
            from core.ingestion import _fetch_gap_candles
            result = _fetch_gap_candles('5m', 1000, 'BTC/USDT', 'binance')

        assert result == []

    def test_breaks_on_batch_under_1000(self):
        """Must stop pagination when exchange returns < 1000 candles."""
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.return_value = [
            [i * 60000, 100.0, 101.0, 99.0, 100.5, 1000.0] for i in range(999)
        ]

        with patch('ccxt.binance', return_value=mock_exchange):
            from core.ingestion import _fetch_gap_candles
            result = _fetch_gap_candles('5m', -1, 'BTC/USDT', 'binance')

        # 999 candles all pass the since_ts filter (since_ts=-1)
        assert len(result) == 999

    def test_pagination_continues_when_batch_is_1000(self):
        """Must paginate when exchange returns exactly 1000 candles."""
        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.side_effect = [
            [[i * 60000, 100.0, 101.0, 99.0, 100.5, 1000.0] for i in range(1000)],
            [[1000 * 60000, 101.0, 102.0, 100.0, 101.5, 1100.0]],
        ]

        with patch('ccxt.binance', return_value=mock_exchange):
            from core.ingestion import _fetch_gap_candles
            result = _fetch_gap_candles('5m', -1, 'BTC/USDT', 'binance')

        # 1000 from first batch + 1 from second = 1001
        assert len(result) == 1001
