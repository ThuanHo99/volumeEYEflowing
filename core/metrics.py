"""Prometheus metrics definitions for the entire system."""
import logging

from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

QUEUE_SIZE = Gauge(
    'btc_signal_queue_size',
    'Current number of items in queue',
    labelnames=['queue'],
    namespace='btc_signal',
)

WS_CONNECTIONS = Gauge(
    'btc_signal_ws_connections',
    'Active WebSocket connections',
    namespace='btc_signal',
)

CANDLES_PROCESSED = Counter(
    'btc_signal_candles_processed_total',
    'Total closed candles processed',
    labelnames=['symbol', 'timeframe'],
    namespace='btc_signal',
)

SIGNAL_COUNT = Counter(
    'btc_signal_signals_total',
    'Signals generated',
    labelnames=['symbol', 'signal_type', 'timeframe'],
    namespace='btc_signal',
)
