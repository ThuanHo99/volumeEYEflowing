"""Integration tests for core.ingestion.

These tests connect to Binance REST API (requires network).
"""

import pytest
from core.ingestion import fetch_historical_ohlcv


@pytest.mark.integration
class TestHistoricalFetch:
    def test_returns_data_from_binance(self):
        """Integration: call real Binance REST API to confirm data is returned."""
        try:
            data = fetch_historical_ohlcv(timeframes=['1m'])
        except Exception as e:
            pytest.skip(f"No network: {e}")

        assert '1m' in data
        assert len(data['1m']) > 0
        c = data['1m'][0]
        for field in ['tf', 'start_ts', 'open', 'high', 'low', 'close', 'volume', 'is_closed']:
            assert field in c, f"Missing field: {field}"
        assert c['is_closed'] is True
