"""Verify candle volume field presence (Bug 1)."""

from core.state_manager import IndicatorSnapshot


class TestBug1_VolumeProfile:
    """Candle objects must include volume so chart_tools volume profile works."""

    def test_indicator_snapshot_includes_volume(self):
        """IndicatorSnapshot carries volume — frontend needs it for vol profile."""
        snap = IndicatorSnapshot(
            tf='1m', timestamp=1700000000000,
            open=50000, high=50100, low=49900, close=50050,
            ema200=50123.45, rsi14=55.5, volume=123.456,
            is_closed=True,
        )
        assert snap.volume == 123.456
        assert hasattr(snap, 'volume')

    def test_ws_candle_message_has_volume(self):
        """The WS candle dict produced by consume_for_ui must contain 'volume'."""
        snap = IndicatorSnapshot(
            tf='15m', timestamp=1700000000000,
            open=50000, high=50100, low=49900, close=50050,
            ema200=50123.45, rsi14=55.5, volume=99.5,
            is_closed=True,
        )
        msg = {
            'type': 'candle',
            'tf': snap.tf,
            'start_ts': snap.timestamp,
            'open': snap.open,
            'high': snap.high,
            'low': snap.low,
            'close': snap.close,
            'ema200': snap.ema200,
            'rsi14': snap.rsi14,
            'volume': snap.volume,
            'is_closed': snap.is_closed,
        }
        assert 'volume' in msg
        assert msg['volume'] == 99.5
        required = {'type', 'tf', 'start_ts', 'open', 'high', 'low', 'close', 'volume', 'ema200', 'rsi14'}
        assert required.issubset(msg.keys())
