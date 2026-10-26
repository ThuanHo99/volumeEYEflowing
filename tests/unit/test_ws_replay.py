"""Verify WebSocket buffer replay behavior (Bug 2)."""

import json
import pytest


class TestBug2_BufferReplay:
    """WS handshake must send replay_bulk messages, not individual ones."""

    @pytest.mark.asyncio
    async def test_ws_replay_is_bulk_message(self, mocker):
        """After buffer population, a new WS client gets replay_bulk messages."""
        from ui.web_dashboard import (
            CANDLE_BUFFER,
            DAY_LEVELS,
            MAX_CANDLE_BUFFER_PER_TF,
            MAX_WS_PER_IP,
            MAX_WS_TOTAL,
            WS_CLIENTS,
            WS_CONNECTIONS_BY_IP,
            ws_handler,
        )

        buf_candles_1m = [
            {'type': 'candle', 'tf': '1m', 'start_ts': 1700000000000,
             'open': 50000, 'high': 50050, 'low': 49950, 'close': 50025,
             'ema200': 50100, 'rsi14': 50, 'volume': 10, 'is_closed': True},
            {'type': 'candle', 'tf': '1m', 'start_ts': 1700000060000,
             'open': 50025, 'high': 50075, 'low': 50000, 'close': 50060,
             'ema200': 50101, 'rsi14': 52, 'volume': 15, 'is_closed': True},
        ]
        buf_candles_5m = [
            {'type': 'candle', 'tf': '5m', 'start_ts': 1700000000000,
             'open': 50000, 'high': 50200, 'low': 49800, 'close': 50100,
             'ema200': 50100, 'rsi14': 55, 'volume': 50, 'is_closed': True},
        ]

        mock_app = {
            WS_CLIENTS: set(),
            WS_CONNECTIONS_BY_IP: {},
            CANDLE_BUFFER: {'1m': buf_candles_1m, '5m': buf_candles_5m},
            MAX_WS_TOTAL: 50,
            MAX_WS_PER_IP: 5,
            MAX_CANDLE_BUFFER_PER_TF: 10080,
            DAY_LEVELS: None,
        }
        mock_request = mocker.MagicMock()
        mock_request.remote = '127.0.0.1'
        mock_request.app = mock_app

        sent_messages: list[str] = []

        class _CaptureWS:
            def __init__(self):
                self._msg_iter = iter([])
            async def prepare(self, request):
                pass
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
            async def send_str(self, data: str):
                sent_messages.append(data)

        mock_ws = _CaptureWS()
        mocker.patch('aiohttp.web.WebSocketResponse', return_value=mock_ws)

        await ws_handler(mock_request)

        bulk_msgs = [m for m in sent_messages if '"type":"replay_bulk"' in m
                     or '"type": "replay_bulk"' in m]
        assert len(bulk_msgs) >= 1, (
            f"Expected at least 1 replay_bulk message, got {len(bulk_msgs)}. "
            f"All messages: {sent_messages}"
        )

        total_candles = sum(len(json.loads(m)['candles']) for m in bulk_msgs)
        assert total_candles == 3  # 2 from 1m + 1 from 5m

    @pytest.mark.asyncio
    async def test_empty_buffer_sends_no_replay(self, mocker):
        """When no candles are buffered, replay_bulk is skipped entirely."""
        from ui.web_dashboard import (
            CANDLE_BUFFER,
            DAY_LEVELS,
            MAX_CANDLE_BUFFER_PER_TF,
            MAX_WS_PER_IP,
            MAX_WS_TOTAL,
            WS_CLIENTS,
            WS_CONNECTIONS_BY_IP,
            ws_handler,
        )

        mock_app = {
            WS_CLIENTS: set(),
            WS_CONNECTIONS_BY_IP: {},
            CANDLE_BUFFER: {},
            MAX_WS_TOTAL: 50,
            MAX_WS_PER_IP: 5,
            MAX_CANDLE_BUFFER_PER_TF: 10080,
            DAY_LEVELS: None,
        }
        mock_request = mocker.MagicMock()
        mock_request.remote = '127.0.0.1'
        mock_request.app = mock_app

        sent_messages: list[str] = []

        class _CaptureWS:
            def __init__(self):
                pass
            async def prepare(self, request):
                pass
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
            async def send_str(self, data: str):
                sent_messages.append(data)

        mock_ws = _CaptureWS()
        mocker.patch('aiohttp.web.WebSocketResponse', return_value=mock_ws)

        await ws_handler(mock_request)

        bulk_msgs = [m for m in sent_messages if 'replay_bulk' in m]
        assert len(bulk_msgs) == 0, "Empty buffer should not send replay_bulk"
