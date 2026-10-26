"""Integration tests for ui.web_dashboard.

These tests start a real aiohttp server on an ephemeral port and connect via WebSocket.
"""

import asyncio
import json
import socket

import pytest
from core.state_manager import IndicatorSnapshot


def _find_free_port():
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.mark.integration
class TestWebSocketBroadcast:
    """Integration tests for the WebSocket broadcast pipeline."""

    @pytest.fixture
    async def app_and_queue(self, tmp_path, monkeypatch):
        from ui.web_dashboard import serve
        # Use an isolated data directory; never remove a user's persisted signals.
        monkeypatch.setattr('core.persistence.DATA_DIR', str(tmp_path))

        port = _find_free_port()
        ui_queue = asyncio.Queue(maxsize=10000)
        candle_queue = asyncio.Queue(maxsize=10000)
        signal_queue = asyncio.Queue(maxsize=500)
        server_task = asyncio.create_task(
            serve(ui_queue, candle_queue, signal_queue, host='127.0.0.1', port=port)
        )
        await asyncio.sleep(0.5)
        # Verify server started successfully
        if server_task.done():
            exc = server_task.exception()
            if exc:
                raise RuntimeError(f"Server failed to start: {exc}")
        yield ui_queue, server_task, port
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_server_serves_html(self, app_and_queue):
        import aiohttp
        ui_queue, server_task, port = app_and_queue
        async with aiohttp.ClientSession() as session:
            # Test landing page at /
            async with session.get(f'http://localhost:{port}') as resp:
                assert resp.status == 200
                html = await resp.text()
                assert 'VolHunter' in html
            
            # Test dashboard page at /dashboard
            async with session.get(f'http://localhost:{port}/dashboard') as resp:
                assert resp.status == 200
                html = await resp.text()
                assert 'VolHunter' in html
                assert 'lightweight-charts' in html

    async def test_websocket_connect(self, app_and_queue):
        import aiohttp
        ui_queue, server_task, port = app_and_queue
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f'http://localhost:{port}/ws') as ws:
                await ws.send_str('ping')

    HANDSHAKE_TYPES = frozenset({
        'config', 'ai_status', 'ready', 'status',
        'replay_bulk', 'replay_signals', 'replay_ai_analysis', 'day_levels',
    })

    @staticmethod
    async def _drain_handshake(ws, timeout=3):
        """Drain handshake messages (config, ready, replays, etc.) until 'ready'."""
        drained = []
        try:
            while True:
                msg = await ws.receive(timeout=timeout)
                data = json.loads(msg.data)
                if data.get('type') in TestWebSocketBroadcast.HANDSHAKE_TYPES:
                    drained.append(data['type'])
                    if data['type'] == 'ready':
                        break
                else:
                    drained.append(data)
                    break
        except Exception:
            pass
        return drained

    async def test_broadcast_candle(self, app_and_queue):
        import aiohttp
        ui_queue, server_task, port = app_and_queue
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f'http://localhost:{port}/ws') as ws:
                await self._drain_handshake(ws)
                snap = IndicatorSnapshot(
                    tf='1m', timestamp=1700000000000, open=50000.0, high=50100.0,
                    low=49900.0, close=50050.0,
                    ema200=49000.0, rsi14=55.0, volume=10.0, is_closed=True,
                )
                await ui_queue.put(snap)
                msg = await ws.receive(timeout=5)
                data = json.loads(msg.data)
                assert data['type'] == 'candle'
                assert data['close'] == 50050.0
                assert data['ema200'] == 49000.0
                assert data['is_closed'] is True

    async def test_broadcast_signal(self, app_and_queue):
        import aiohttp
        ui_queue, server_task, port = app_and_queue
        signal = {
            'type': 'signal',
            'signal_type': 'RSI_OVERBOUGHT',
            'level': 'WARNING',
            'tf': '15m',
            'close': 51000.0,
            'rsi14': 78.0,
            'ema200': 49000.0,
            'volume': 5.0,
            'timestamp': 1700000120000,
            'detail': 'RSI 15m = 78.0 (nguong qua mua: 75)',
        }
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f'http://localhost:{port}/ws') as ws:
                await self._drain_handshake(ws)
                await ui_queue.put(signal)
                msg = await ws.receive(timeout=5)
                data = json.loads(msg.data)
                assert data['type'] == 'signal'
                assert data['signal_type'] == 'RSI_OVERBOUGHT'

    async def test_broadcast_ai(self, app_and_queue):
        import aiohttp
        ui_queue, server_task, port = app_and_queue
        ai = {
            'type': 'ai_analysis',
            'news_sentiment': 'Bullish',
            'impact_level': 7,
            'reasoning': 'Bitcoin ETF inflows surge as institutional demand grows. The macro environment remains favorable with dovish Fed signals.',
            'technical_alignment': 'RSI 15m at 55 confirms bullish momentum. EMA200 at 49000 is holding as support.',
            'timestamp': 1700000180000,
        }
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f'http://localhost:{port}/ws') as ws:
                await self._drain_handshake(ws)
                await ui_queue.put(ai)
                msg = await ws.receive(timeout=5)
                data = json.loads(msg.data)
                assert data['type'] == 'ai_analysis'
                assert data['news_sentiment'] == 'Bullish'
                assert data['impact_level'] == 7
