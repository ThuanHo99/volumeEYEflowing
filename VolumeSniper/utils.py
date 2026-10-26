"""
utils.py - VolumeSniper shared utilities.

BUG-L6 FIX: _interval_minutes() was duplicated in both scanner.py and
volume_detector.py. If one copy was updated (e.g. adding 'w' for weekly)
the other would silently remain stale. Now in a single canonical location.
"""


def interval_minutes(interval: str) -> int:
    """Convert kline interval string to minutes.

    Examples:
        '1m'  ->   1
        '3m'  ->   3
        '5m'  ->   5
        '15m' ->  15
        '30m' ->  30
        '1h'  ->  60
        '2h'  -> 120
        '4h'  -> 240
        '6h'  -> 360
        '8h'  -> 480
        '12h' -> 720
        '1d'  -> 1440
        '3d'  -> 4320
        '1w'  -> 10080
    """
    _MAP = {
        '1m':   1,
        '3m':   3,
        '5m':   5,
        '15m':  15,
        '30m':  30,
        '1h':   60,
        '2h':   120,
        '4h':   240,
        '6h':   360,
        '8h':   480,
        '12h':  720,
        '1d':   1440,
        '3d':   4320,
        '1w':   10080,
    }
    return _MAP.get(interval, 1)


__all__ = ['interval_minutes']
