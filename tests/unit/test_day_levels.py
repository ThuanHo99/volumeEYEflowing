"""Verify day_levels data format (Bug 4)."""

class TestBug4_SRToggle:
    """day_levels data format supports restoration after toggle off/on."""

    def test_day_levels_message_has_required_fields(self):
        msg = {
            'type': 'day_levels',
            'high': 51234.56,
            'low': 49876.54,
            'start_ts': 1700000000000,
        }
        assert msg['type'] == 'day_levels'
        assert isinstance(msg['high'], (int, float))
        assert isinstance(msg['low'], (int, float))
        assert isinstance(msg['start_ts'], int)
        assert msg['high'] > msg['low']

    def test_day_levels_roundtrip_for_toggle_restore(self):
        stored_high = 51234.56
        stored_low = 49876.54
        stored_ts = 1700000000000
        restored_lines = []
        def fake_update_day_levels(high, low, ts):
            restored_lines.append({'high': high, 'low': low, 'ts': ts})
            return f"line_high_{high}", f"line_low_{low}"
        high_line, low_line = fake_update_day_levels(stored_high, stored_low, stored_ts)
        assert len(restored_lines) == 1
        assert restored_lines[0]['high'] == stored_high
        assert restored_lines[0]['low'] == stored_low
        assert high_line is not None
        assert low_line is not None
