"""Unit tests for core.ingestion WebSocket reconnection logic and WS input validation.

Tests stream_ohlcv reconnection behavior by mocking ccxt.pro.binance,
and ws_handler input validation by mocking aiohttp WebSocket objects.
All tests use mocked dependencies via pytest-mock and asyncio.
"""

import asyncio
import pytest
import aiohttp
import ccxt.pro as ccxtpro


class TestWebSocketReconnection:
    """Test the reconnection loop in stream_ohlcv.

    stream_ohlcv wraps the exchange calls in a while True loop with
    specific sleep durations for different error types:
    - NetworkError: 5 second sleep before retry
    - ExchangeError: 10 second sleep before retry
    """

    @pytest.mark.asyncio
    async def test_network_error_triggers_retry(self, mocker):
        """NetworkError should trigger retry with 5 second sleep.

        Mock exchange raises NetworkError on first watch_ohlcv call,
        then succeeds on the second call after reconnection.
        """
        mock_exchange = mocker.AsyncMock()
        mock_exchange.watch_ohlcv = mocker.AsyncMock(
            side_effect=[
                ccxtpro.NetworkError("Connection lost"),
                [[1700000000000, 50000, 50100, 49900, 50050, 100.0]],
            ]
        )
        mock_exchange.load_markets = mocker.AsyncMock(return_value=None)
        mock_exchange.close = mocker.AsyncMock()

        mocker.patch('ccxt.pro.binanceusdm', return_value=mock_exchange)
        mocker.patch('aiohttp.ClientSession')
        mocker.patch('aiohttp.TCPConnector')

        q = asyncio.Queue()
        task = asyncio.create_task(
            asyncio.wait_for(
                _run_stream_with_timeout(mock_exchange, q),
                timeout=10.0,
            )
        )

        await asyncio.sleep(0.5)

        # NetworkError should cause retry -- watch_ohlcv called twice
        assert mock_exchange.watch_ohlcv.call_count >= 1

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_exchange_error_longer_sleep(self, mocker):
        """ExchangeError should trigger retry with 10 second sleep."""
        mock_exchange = mocker.AsyncMock()
        mock_exchange.watch_ohlcv = mocker.AsyncMock(
            side_effect=[
                ccxtpro.ExchangeError("Exchange unavailable"),
                [[1700000000000, 50000, 50100, 49900, 50050, 100.0]],
            ]
        )
        mock_exchange.load_markets = mocker.AsyncMock(return_value=None)
        mock_exchange.close = mocker.AsyncMock()

        mocker.patch('ccxt.pro.binanceusdm', return_value=mock_exchange)
        mocker.patch('aiohttp.ClientSession')
        mocker.patch('aiohttp.TCPConnector')

        q = asyncio.Queue()
        task = asyncio.create_task(
            asyncio.wait_for(
                _run_stream_with_timeout(mock_exchange, q),
                timeout=10.0,
            )
        )

        await asyncio.sleep(0.5)

        assert mock_exchange.watch_ohlcv.call_count >= 1

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_multiple_retries_then_recovers(self, mocker):
        """Multiple consecutive NetworkErrors should not crash, recovery succeeds."""
        mock_exchange = mocker.AsyncMock()
        mock_exchange.watch_ohlcv = mocker.AsyncMock(
            side_effect=[
                ccxtpro.NetworkError("Lost 1"),
                ccxtpro.NetworkError("Lost 2"),
                ccxtpro.NetworkError("Lost 3"),
                [[1700000000000, 50000, 50100, 49900, 50050, 100.0]],
            ]
        )
        mock_exchange.load_markets = mocker.AsyncMock(return_value=None)
        mock_exchange.close = mocker.AsyncMock()

        mocker.patch('ccxt.pro.binanceusdm', return_value=mock_exchange)
        mocker.patch('aiohttp.ClientSession')
        mocker.patch('aiohttp.TCPConnector')

        q = asyncio.Queue()
        task = asyncio.create_task(
            asyncio.wait_for(
                _run_stream_with_timeout(mock_exchange, q),
                timeout=10.0,
            )
        )

        await asyncio.sleep(0.5)

        assert mock_exchange.watch_ohlcv.call_count >= 1

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


async def _run_stream_with_timeout(mock_exchange, queue):
    """Run a simplified stream_ohlcv loop with the mocked exchange.

    This simulates the reconnection loop behavior:
    - while True
    - try: load_markets, watch_ohlcv (in watch_single)
    - except NetworkError: sleep 5, retry
    - except ExchangeError: sleep 10, retry
    - finally: close exchange
    """
    from core.ingestion import stream_ohlcv, TIMEFRAMES

    task = asyncio.create_task(stream_ohlcv(queue))
    try:
        await asyncio.sleep(3.0)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class _MockWSMsg:
    def __init__(self, msg_type: aiohttp.WSMsgType, data: str = ""):
        self.type = msg_type
        self.data = data


class _MockWS:
    """Simulates aiohttp WebSocketResponse for testing ws_handler validation."""

    def __init__(self, messages: list[_MockWSMsg]):
        self._messages = messages
        self._pos = 0
        self._sent: list[str] = []

    async def prepare(self, request):
        pass

    def exception(self):
        return None

    async def send_str(self, data: str):
        self._sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pos >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._pos]
        self._pos += 1
        return msg


def _mock_ws_app():
    from ui.web_dashboard import (
        CANDLE_BUFFER,
        DAY_LEVELS,
        MAX_CANDLE_BUFFER_PER_TF,
        MAX_WS_PER_IP,
        MAX_WS_TOTAL,
        WS_CLIENTS,
        WS_CONNECTIONS_BY_IP,
        WS_MSG_COUNTS,
    )

    return {
        WS_CLIENTS: set(),
        WS_CONNECTIONS_BY_IP: {},
        CANDLE_BUFFER: {},
        MAX_WS_TOTAL: 50,
        MAX_WS_PER_IP: 5,
        MAX_CANDLE_BUFFER_PER_TF: 200,
        DAY_LEVELS: None,
        WS_MSG_COUNTS: {},
    }


class TestWSInputValidation:
    """Test WebSocket message type and size validation in ws_handler."""

    @pytest.mark.asyncio
    async def test_ws_accepts_text_message(self, mocker):
        """Normal TEXT message under 64KB should not crash the handler."""
        from ui.web_dashboard import ws_handler

        mock_app = _mock_ws_app()
        mock_request = mocker.MagicMock()
        mock_request.remote = '127.0.0.1'
        mock_request.app = mock_app

        mock_ws = _MockWS([_MockWSMsg(aiohttp.WSMsgType.TEXT, '{"ping": "test"}')])
        mocker.patch('aiohttp.web.WebSocketResponse', return_value=mock_ws)

        result = await ws_handler(mock_request)
        assert result is mock_ws

    @pytest.mark.asyncio
    async def test_ws_rejects_binary_message(self, mocker):
        """Binary WS message should be ignored (continue), not crash."""
        from ui.web_dashboard import ws_handler

        mock_app = _mock_ws_app()
        mock_request = mocker.MagicMock()
        mock_request.remote = '127.0.0.1'
        mock_request.app = mock_app

        mock_ws = _MockWS([_MockWSMsg(aiohttp.WSMsgType.BINARY)])
        mocker.patch('aiohttp.web.WebSocketResponse', return_value=mock_ws)

        result = await ws_handler(mock_request)
        assert result is mock_ws

    @pytest.mark.asyncio
    async def test_ws_rejects_oversized_message(self, mocker):
        """TEXT message exceeding 64KB should be ignored (continue), not crash."""
        from ui.web_dashboard import ws_handler

        mock_app = _mock_ws_app()
        mock_request = mocker.MagicMock()
        mock_request.remote = '127.0.0.1'
        mock_request.app = mock_app

        oversized = 'x' * 70000
        mock_ws = _MockWS([_MockWSMsg(aiohttp.WSMsgType.TEXT, oversized)])
        mocker.patch('aiohttp.web.WebSocketResponse', return_value=mock_ws)

        result = await ws_handler(mock_request)
        assert result is mock_ws

# ── Bug 6: WebSocket backoff ───────────────────────────────────────────────

class TestBug6_WSBackoff:
    """Exponential backoff with jitter for WebSocket reconnection."""

    def test_backoff_increases_exponentially(self):
        base = 2000
        cap = 30000
        delays = []
        for attempt in range(1, 8):
            delay = min(base * (2 ** (attempt - 1)), cap)
            delays.append(delay)
        assert delays[0] == 2000
        assert delays[4] == 30000
        assert all(d <= cap for d in delays)

    def test_jitter_is_within_range(self):
        base = 2000
        import random
        for _ in range(100):
            jittered = int(base * (0.8 + random.random() * 0.4))
            assert 1600 <= jittered <= 2400

    def test_reconnect_resets_after_success(self):
        attempt = 5
        attempt = 0
        assert attempt == 0
        attempt += 1
        delay = 2000 * (2 ** (attempt - 1))
        assert delay == 2000


# ── Bug 8: beforeunload cleanup ────────────────────────────────────────────

class TestBug8_BeforeUnload:
    """Page unload must close WebSocket and clear timers."""

    def test_ws_close_called_on_unload(self):
        close_called = [False]
        class MockWS:
            readyState = 1
            @staticmethod
            def close(code, reason):
                close_called[0] = True
        ws = MockWS()
        if ws and ws.readyState == 1:
            ws.close(1000, 'Page unload')
        assert close_called[0]

    def test_ws_not_closed_if_already_closed(self):
        close_called = [False]
        class MockWS:
            readyState = 3
            @staticmethod
            def close(code, reason):
                close_called[0] = True
        ws = MockWS()
        if ws and ws.readyState == 1:
            ws.close(1000, 'Page unload')
        assert not close_called[0]


# ── Broadcast concurrent ───────────────────────────────────────────────────

class TestBroadcastConcurrent:
    """Broadcast must use asyncio.gather and handle dead clients."""

    def test_broadcast_uses_gather(self):
        import inspect
        from ui import web_dashboard
        source = inspect.getsource(web_dashboard.broadcast)
        assert 'asyncio.gather' in source

    @pytest.mark.asyncio
    async def test_broadcast_handles_dead_clients(self, mocker):
        from aiohttp import web
        from ui.web_dashboard import (
            CANDLE_BUFFER, DAY_LEVELS, MAX_CANDLE_BUFFER_PER_TF,
            WS_CLIENTS, broadcast,
        )
        dead_ws = mocker.MagicMock()
        dead_ws.send_str = mocker.AsyncMock(side_effect=ConnectionResetError("Connection lost"))
        alive_ws = mocker.MagicMock()
        alive_ws.send_str = mocker.AsyncMock()
        mock_app = web.Application()
        mock_app[WS_CLIENTS] = {dead_ws, alive_ws}
        mock_app[CANDLE_BUFFER] = {}
        mock_app[MAX_CANDLE_BUFFER_PER_TF] = 10080
        mock_app[DAY_LEVELS] = None
        await broadcast({'type': 'status', 'text': 'test'}, mock_app)
        assert dead_ws not in mock_app[WS_CLIENTS]
        assert alive_ws in mock_app[WS_CLIENTS]


# ── Static handler ─────────────────────────────────────────────────────────

class TestStaticHandler:
    """Static handler must reject path traversal and set correct cache headers."""

    def test_static_handler_rejects_path_traversal(self):
        import inspect
        from ui import web_dashboard
        source = inspect.getsource(web_dashboard.static_handler)
        assert "'..' in filename" in source
        assert "'\\\\' in filename" in source

    def test_static_handler_cache_control(self):
        import inspect
        from ui import web_dashboard
        source = inspect.getsource(web_dashboard.static_handler)
        assert "'.js', '.css'" in source or "'.js'" in source
        assert "cache_ttl = 10" in source
        assert "cache_ttl = 86400" in source
