import asyncio
import base64
import datetime
import json
import logging
import os
import pathlib
import structlog
import time
import uuid
from typing import Any, Awaitable, Callable

import hmac

from aiohttp import web
import aiohttp
from prometheus_client.aiohttp import make_aiohttp_handler

from core.state_manager import IndicatorSnapshot
from core.config import WEB_HOST, WEB_PORT, WEB_USER, WEB_PASS, WEB_SSL_CERT, WEB_SSL_KEY
from core.config import RSI_OVERSOLD, RSI_OVERBOUGHT, VOLUME_SPIKE_MULTIPLIER, EMA_TOUCH_TIMEFRAMES
from core.config import SIGNAL_BUFFER_SIZE, SYMBOLS
from core.metrics import WS_CONNECTIONS
from core.persistence import load_signal_buffer, save_signal_buffer

logger = logging.getLogger(__name__)

_HERE = pathlib.Path(__file__).resolve().parent

# Module-level app reference so main.py can sync candle buffer after warmup
_DASHBOARD_APP: web.Application | None = None
_LANDING_HTML = (_HERE.parent / 'templates' / 'landing.html').read_text(encoding='utf-8')
_INDEX_HTML = (_HERE.parent / 'templates' / 'index.html').read_text(encoding='utf-8')
_STATIC_DIR = _HERE.parent / 'static'
# BUG-L3 FIX: Cap the static file cache to prevent OOM.
# VolumeSniper writes a new chart PNG per alert — without a size cap this dict
# grows unboundedly until the process runs out of memory after days of uptime.
# 200 entries ≈ ~200 unique static files cached in RAM (JS/CSS/fonts are few).
# Chart files are large but short-lived — they should NOT be cached permanently.
_STATIC_CACHE: dict[str, tuple[bytes, str]] = {}  # path -> (content, content_type)
_MAX_STATIC_CACHE_ENTRIES = 200  # cap: evict oldest on overflow

WS_CLIENTS = web.AppKey("ws_clients", set[web.WebSocketResponse])
WS_CONNECTIONS_BY_IP = web.AppKey("ws_connections", dict[str, int])
CANDLE_BUFFER = web.AppKey("candle_buffer", dict[str, list[dict]])
MAX_CANDLE_BUFFER_PER_TF = web.AppKey("max_candle_buffer_per_tf", int)
MAX_WS_PER_IP = web.AppKey("max_ws_per_ip", int)
MAX_WS_TOTAL = web.AppKey("max_ws_total", int)
STARTED_AT = web.AppKey("started_at", float)
CANDLE_QUEUE = web.AppKey("candle_queue", asyncio.Queue)
SIGNAL_QUEUE = web.AppKey("signal_queue", asyncio.Queue)
UI_QUEUE = web.AppKey("ui_queue", asyncio.Queue)
DAY_LEVELS = web.AppKey("day_levels", dict | None)
STATE = web.AppKey("state", Any)
SCANNER = web.AppKey("scanner", Any)
SIGNAL_BUFFER_KEY = web.AppKey("signal_buffer", list)
CONTEXT_WATCH_BUFFER = web.AppKey("context_watch_buffer", list)
HEALTH_CACHE = web.AppKey("health_cache", dict | None)
HEALTH_CACHE_TS = web.AppKey("health_cache_ts", float)
WS_MSG_COUNTS = web.AppKey("ws_msg_counts", dict[str, list[float]])
_IS_WARMING_UP = True


@web.middleware
async def basic_auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    # Bypass Basic Auth for public landing page, static assets, and favicon
    if request.path in ('/', '/favicon.ico') or request.path.startswith('/static/'):
        return await handler(request)

    # BUG-C3 FIX: Guard against None credentials BEFORE any comparison.
    # hmac.compare_digest raises TypeError if either arg is None.
    # This happens when WEB_USER/WEB_PASS are not set in .env.
    if not WEB_USER or not WEB_PASS:
        raise web.HTTPServiceUnavailable(
            text="Server mis-configured: auth credentials not set"
        )

    # BUG-C2 FIX: Do NOT trust X-Forwarded-For header unconditionally.
    # Any attacker can forge: "X-Forwarded-For: 127.0.0.1" to bypass auth.
    # Only trust X-Forwarded-For when the *actual* TCP peer is a known trusted proxy.
    # TRUSTED_PROXIES env var = comma-separated IPs of your reverse proxy (e.g. nginx).
    # If running directly (no proxy), only the real TCP remote address is checked.
    _trusted_proxies: set[str] = {
        ip.strip() for ip in os.getenv('TRUSTED_PROXIES', '').split(',') if ip.strip()
    }
    tcp_remote = request.remote or ''

    # Direct localhost access (no proxy): allow without auth
    if tcp_remote in ('127.0.0.1', '::1'):
        return await handler(request)

    # Behind a trusted reverse proxy: read the real IP from X-Forwarded-For
    if tcp_remote in _trusted_proxies:
        forwarded = request.headers.get('X-Forwarded-For', '')
        real_ip = forwarded.split(',')[0].strip() if forwarded else ''
        if real_ip in ('127.0.0.1', '::1'):
            return await handler(request)

    auth_header = request.headers.get('Authorization', '')
    try:
        scheme, creds = auth_header.split(' ', 1)
        if scheme.lower() != 'basic':
            raise ValueError("Not Basic")
        username, password = base64.b64decode(creds).decode('utf-8').split(':', 1)
    except Exception:
        raise web.HTTPUnauthorized(
            headers={'WWW-Authenticate': 'Basic realm="VolHunter"'}
        )

    if not hmac.compare_digest(username, WEB_USER) or not hmac.compare_digest(password, WEB_PASS):
        raise web.HTTPUnauthorized(
            headers={'WWW-Authenticate': 'Basic realm="VolHunter"'}
        )

    return await handler(request)


def _add_security_headers(response: web.StreamResponse) -> None:
    csp = (
        "default-src 'self'; "
        "script-src 'self' https://unpkg.com 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
        "img-src 'self' data: blob: https://cdn.simpleicons.org; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' data: https://unpkg.com https://fonts.gstatic.com;"
    )
    response.headers['Content-Security-Policy'] = csp
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    peer = request.remote
    if peer is None:
        peer = request.headers.get('X-Forwarded-For', 'unknown')

    if len(app[WS_CLIENTS]) >= app[MAX_WS_TOTAL]:
        raise web.HTTPTooManyRequests(text="Max connections reached")

    ip_count = app[WS_CONNECTIONS_BY_IP].get(peer, 0)
    if ip_count >= app[MAX_WS_PER_IP]:
        raise web.HTTPTooManyRequests(text=f"Max {app[MAX_WS_PER_IP]} connections per IP")

    app[WS_CONNECTIONS_BY_IP][peer] = ip_count + 1

    trace_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(trace_id=trace_id, peer=peer)

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Replay buffered candles in chunks to avoid large single-message payloads
    all_buffered: list[dict] = []
    for buf in app[CANDLE_BUFFER].values():
        all_buffered.extend(buf)
    all_buffered.sort(key=lambda m: m.get('start_ts', 0))
    if all_buffered:
        _CHUNK_SIZE = 500
        for chunk_start in range(0, len(all_buffered), _CHUNK_SIZE):
            chunk = all_buffered[chunk_start:chunk_start + _CHUNK_SIZE]
            try:
                await ws.send_str(json.dumps({'type': 'replay_bulk', 'candles': chunk}))
            except Exception as e:
                logger.warning("WS bulk replay chunk %d failed: %s",
                               chunk_start // _CHUNK_SIZE, e, exc_info=True)
                break
        logger.info("Replayed %d candles in chunks to new WS client", len(all_buffered))

    # Replay buffered signals so late-connecting clients see full history
    if app.get(SIGNAL_BUFFER_KEY):
        try:
            await ws.send_str(json.dumps({
                'type': 'replay_signals',
                'signals': app[SIGNAL_BUFFER_KEY],
            }))
        except Exception as e:
            logger.warning("WS signal replay failed during handshake: %s", e, exc_info=True)

    if app.get(CONTEXT_WATCH_BUFFER):
        try:
            await ws.send_str(json.dumps({
                'type': 'replay_context_watches',
                'watches': app[CONTEXT_WATCH_BUFFER],
            }))
        except Exception as e:
            logger.warning("WS context watch replay failed: %s", e, exc_info=True)

    try:
        config_msg = {
            'type': 'config',
            'symbols': SYMBOLS,
            'rsi_oversold': RSI_OVERSOLD,
            'rsi_overbought': RSI_OVERBOUGHT,
            'volume_multiplier': VOLUME_SPIKE_MULTIPLIER,
            'ema_touch_timeframes': EMA_TOUCH_TIMEFRAMES,
            'max_chart_candles': int(os.getenv('MAX_CHART_CANDLES', '10080')),
        }
        await ws.send_str(json.dumps(config_msg))

        day_levels = app.get(DAY_LEVELS)
        if isinstance(day_levels, dict):
            for sym_levels in day_levels.values():
                if isinstance(sym_levels, dict):
                    try:
                        await ws.send_str(json.dumps(sym_levels))
                    except (TypeError, ValueError) as e:
                        logger.warning("WS day_levels replay failed: %s", e)

        # Signal frontend that initial data load is complete
        try:
            await ws.send_str(json.dumps({'type': 'ready'}))
        except Exception as e:
            logger.warning("WS ready signal failed: %s", e, exc_info=True)
    except Exception as e:
        logger.warning("WS config message send failed: %s", e, exc_info=True)

    app[WS_CLIENTS].add(ws)
    logger.info(f"WebSocket client connected. Total: {len(app[WS_CLIENTS])}")
    try:
        MAX_WS_MSG_SIZE = 65536  # 64KB
        async for msg in ws:
            # Per-IP rate limiting (max 10 msgs/sec)
            now = time.time()
            peer_ts = app[WS_MSG_COUNTS].setdefault(peer, [])
            cutoff = now - 1
            app[WS_MSG_COUNTS][peer] = [t for t in peer_ts if t > cutoff]
            app[WS_MSG_COUNTS][peer].append(now)
            if len(app[WS_MSG_COUNTS][peer]) > 10:
                logger.warning("WS rate limit exceeded for %s", peer)
                await ws.close(code=1008, message=b'Rate limited')
                break

            if msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning(f"WS error: {ws.exception()}")
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                logger.warning(f"WS msg type {msg.type} invalid — rejecting")
                continue
            if len(msg.data) > MAX_WS_MSG_SIZE:
                logger.warning(f"WS msg size {len(msg.data)} exceeds {MAX_WS_MSG_SIZE} — rejecting")
                continue
    finally:
        app[WS_CLIENTS].discard(ws)
        app[WS_CONNECTIONS_BY_IP][peer] = max(0, app[WS_CONNECTIONS_BY_IP].get(peer, 1) - 1)
        logger.info(f"WebSocket client disconnected. Total: {len(app[WS_CLIENTS])}")
        try:
            structlog.contextvars.unbind_contextvars('trace_id', 'peer')
        except Exception:
            pass
    return ws


async def buffer_debug_handler(request: web.Request) -> web.Response:
    """Debug endpoint: show CANDLE_BUFFER contents."""
    app = request.app
    buf = app.get(CANDLE_BUFFER, {})
    symbols_seen = {}
    total = 0
    for tf, candles in buf.items():
        total += len(candles)
        for c in candles:
            sym = c.get('symbol', 'UNKNOWN')
            key = f"{sym}/{tf}"
            symbols_seen[key] = symbols_seen.get(key, 0) + 1

    # Show first 5 1m candles to verify symbol field
    first_5 = []
    tf_1m = buf.get('1m', [])
    for i in range(min(5, len(tf_1m))):
        c = tf_1m[i]
        first_5.append({
            'idx': i,
            'symbol': c.get('symbol', 'MISSING'),
            'tf': c.get('tf'),
            'start_ts': c.get('start_ts'),
        })
    # Also last 5
    last_5 = []
    for i in range(max(0, len(tf_1m) - 5), len(tf_1m)):
        c = tf_1m[i]
        last_5.append({
            'idx': i,
            'symbol': c.get('symbol', 'MISSING'),
            'tf': c.get('tf'),
            'start_ts': c.get('start_ts'),
        })

    return web.json_response({
        'total_candles': total,
        'tf_count': len(buf),
        'tf_sizes': {tf: len(v) for tf, v in buf.items()},
        'symbols_by_tf': symbols_seen,
        'first_5_1m': first_5,
        'last_5_1m': last_5,
    })


async def landing_handler(request: web.Request) -> web.Response:
    resp = web.Response(text=_LANDING_HTML, content_type='text/html')
    _add_security_headers(resp)
    return resp


async def context_plan_handler(request: web.Request) -> web.Response:
    """Read the latest level plan made from already closed scanner candles."""
    raw = request.query.get('symbol', '').upper().strip()
    symbol = raw.replace('/USDT:USDT', 'USDT').replace('/USDT', 'USDT')
    if not symbol or len(symbol) > 30 or not all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' for c in symbol):
        return web.json_response({'error': 'symbol must be alphanumeric'}, status=400)
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
    scanner = request.app.get(SCANNER)
    context = getattr(scanner, 'context', None)
    plan = context.plans.get(symbol) if context else None
    if plan is None:
        return web.json_response({'symbol': symbol, 'status': 'warming_up'}, status=404)
    pending = context.pending.get(symbol)
    upper = pending.plan_upper if pending else plan.upper
    lower = pending.plan_lower if pending else plan.lower
    ready_ts = pending.plan_ts if pending else plan.ready_ts
    quieting = pending.plan_quieting if pending else plan.quieting
    from VolumeSniper import config as vs_config
    tolerance = vs_config.CONTEXT_LEVEL_TOLERANCE_PCT / 100
    return web.json_response({
        'symbol': symbol, 'status': 'PENDING' if pending else 'WAIT',
        'ready_ts': ready_ts, 'upper': upper, 'lower': lower,
        'quieting': quieting,
        'upper_breakout_invalidation': upper * (1 - tolerance),
        'upper_failure_invalidation': upper * (1 + tolerance),
        'lower_breakout_invalidation': lower * (1 + tolerance),
        'lower_failure_invalidation': lower * (1 - tolerance),
        'pending_setup': pending.kind if pending else None,
        'pending_side': pending.side if pending else None,
        'note': 'Levels are observational; wait for closed-candle price acceptance or rejection.',
    })


async def chart_plan_handler(request: web.Request) -> web.Response:
    """Read the latest prospective v3 research plan for one scanner symbol."""
    raw = request.query.get('symbol', '').upper().strip()
    symbol = raw.replace('/USDT:USDT', 'USDT').replace('/USDT', 'USDT')
    if not symbol or len(symbol) > 30 or not symbol.isalnum():
        return web.json_response({'error': 'symbol must be alphanumeric'}, status=400)
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
    scanner = request.app.get(SCANNER)
    engine = getattr(scanner, 'chart_v3', None)
    if engine is None:
        return web.json_response({'symbol': symbol, 'status': 'disabled',
                                  'research_only': True}, status=503)
    plan = engine.plans.get(symbol)
    if plan is None:
        return web.json_response({'symbol': symbol, 'status': 'warming_up',
                                  'research_only': True}, status=404)
    payload = plan.to_dict() if hasattr(plan, 'to_dict') else dict(plan)
    payload = dict(payload)
    payload['symbol'] = symbol
    payload['research_only'] = True
    response = web.json_response(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response


async def derivatives_context_handler(request: web.Request) -> web.Response:
    """On-demand, read-only positioning and depth for one USD-M symbol."""
    raw = request.query.get('symbol', '').upper().strip()
    symbol = raw.replace('/USDT:USDT', 'USDT').replace('/USDT', 'USDT')
    if not symbol or len(symbol) > 30 or not symbol.isalnum():
        return web.json_response({'error': 'symbol must be alphanumeric'}, status=400)
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
    scanner = request.app.get(SCANNER)
    service = getattr(scanner, 'derivatives', None)
    if service is None:
        return web.json_response({'symbol': symbol, 'status': 'disabled'}, status=503)
    if symbol not in getattr(scanner, '_symbols', []):
        return web.json_response({'symbol': symbol, 'status': 'not_scanned'}, status=404)
    try:
        result = await asyncio.wait_for(service.refresh(symbol), timeout=7)
    except asyncio.TimeoutError:
        result = service.snapshot(symbol)
    return web.json_response(result)


async def favicon_handler(request: web.Request) -> web.Response:
    filepath = _STATIC_DIR / 'images' / 'logo_icon_trans.png'
    if not filepath.is_file():
        raise web.HTTPNotFound()
    try:
        content = filepath.read_bytes()
    except Exception:
        raise web.HTTPNotFound()
    resp = web.Response(body=content, content_type='image/png')
    _add_security_headers(resp)
    return resp


async def dashboard_handler(request: web.Request) -> web.Response:
    resp = web.Response(text=_INDEX_HTML, content_type='text/html')
    _add_security_headers(resp)
    return resp


async def static_handler(request: web.Request) -> web.Response:
    filename = request.match_info.get('filename', '')
    # Prevent path traversal — reject parent-dir patterns
    if '..' in filename or '\\' in filename:
        raise web.HTTPNotFound()
    filepath = (_STATIC_DIR / filename).resolve()
    # Ensure resolved path is still under _STATIC_DIR
    if not str(filepath).startswith(str(_STATIC_DIR.resolve())):
        raise web.HTTPNotFound()
    if not filepath.is_file():
        raise web.HTTPNotFound()

    ext = filepath.suffix.lower()

    # BUG-L3 FIX: Do NOT cache dynamically-generated chart files.
    # VolumeSniper creates a new PNG per alert — caching every one would cause OOM.
    # charts/ subdirectory is excluded from the in-memory cache.
    is_dynamic = filename.startswith('charts/')

    cache_key = filename
    if not is_dynamic and cache_key in _STATIC_CACHE:
        content, content_type = _STATIC_CACHE[cache_key]
    else:
        try:
            content = filepath.read_bytes()
        except FileNotFoundError:
            raise web.HTTPNotFound()
        except OSError as e:
            logger.error("Failed to read static file %s: %s", filepath, e)
            raise web.HTTPInternalServerError()
        content_type = {
            '.js': 'application/javascript',
            '.css': 'text/css',
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.woff2': 'font/woff2',
        }.get(ext, 'application/octet-stream')
        if not is_dynamic:
            # Enforce cache size cap — evict oldest entry (FIFO) on overflow
            if len(_STATIC_CACHE) >= _MAX_STATIC_CACHE_ENTRIES:
                oldest_key = next(iter(_STATIC_CACHE))
                del _STATIC_CACHE[oldest_key]
            _STATIC_CACHE[cache_key] = (content, content_type)

    # Cache-Control: 10s for JS/CSS during dev, 0 for dynamic charts, 24h for fonts
    if is_dynamic:
        cache_ttl = 0  # no browser cache for chart files (they are unique per alert)
    elif ext in ('.js', '.css'):
        cache_ttl = 10
    else:
        cache_ttl = 86400
    resp = web.Response(
        body=content, content_type=content_type,
        headers={'Cache-Control': f'public, max-age={cache_ttl}'},
    )
    _add_security_headers(resp)
    return resp



async def health_handler(request: web.Request) -> web.Response:
    trace_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    app = request.app
    now = time.time()
    if app[HEALTH_CACHE] is not None and (now - app[HEALTH_CACHE_TS]) < 30:
        resp = web.json_response(app[HEALTH_CACHE])
        _add_security_headers(resp)
        return resp
    app[HEALTH_CACHE] = {
        'status': 'running',
        'uptime_seconds': int(now - app[STARTED_AT]),
        'ws_clients': len(app[WS_CLIENTS]),
        'queues': {
            'candles': app[CANDLE_QUEUE].qsize(),
            'signals': app[SIGNAL_QUEUE].qsize(),
            'ui': app[UI_QUEUE].qsize(),
        },
    }
    app[HEALTH_CACHE_TS] = now
    resp = web.json_response(app[HEALTH_CACHE])
    _add_security_headers(resp)
    return resp


async def broadcast(data: dict, app: web.Application) -> None:
    # Store day_levels per-symbol for late-connecting clients
    if data.get('type') == 'day_levels':
        sym = data.get('symbol', '')
        if sym:
            if app[DAY_LEVELS] is None:
                app[DAY_LEVELS] = {}
            app[DAY_LEVELS][sym] = data

    # Buffer candle data per timeframe so late-connecting clients get full chart history
    if data.get('type') == 'candle':
        tf = data.get('tf', '1m')
        if tf not in app[CANDLE_BUFFER]:
            app[CANDLE_BUFFER][tf] = []
        buf = app[CANDLE_BUFFER][tf]
        if _IS_WARMING_UP:
            buf.append(data)
            if len(buf) > app[MAX_CANDLE_BUFFER_PER_TF]:
                buf.pop(0)
        else:
            for i, existing in enumerate(buf):
                if (existing.get('start_ts') == data.get('start_ts')
                        and existing.get('symbol') == data.get('symbol')):
                    buf[i] = data
                    break
            else:
                buf.append(data)
                if len(buf) > app[MAX_CANDLE_BUFFER_PER_TF]:
                    buf.pop(0)

    # Buffer live signals so reconnecting clients see current session
    if data.get('type') in ('signal', 'volume_sniper_signal'):
        buf = app.get(SIGNAL_BUFFER_KEY)
        if buf is not None:
            buf.append(data)
            if len(buf) > SIGNAL_BUFFER_SIZE:
                buf.pop(0)

    if data.get('type') == 'volume_context_watch':
        buf = app.get(CONTEXT_WATCH_BUFFER)
        if buf is not None:
            buf.append(data)
            if len(buf) > 100:
                buf.pop(0)

    clients = list(app[WS_CLIENTS])
    if not clients:
        return
    message = json.dumps(data)
    dead = set()

    async def _send(ws):
        try:
            await asyncio.wait_for(ws.send_str(message), timeout=2.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("WS broadcast send failed: %s", e, exc_info=True)
            dead.add(ws)

    _sem = asyncio.Semaphore(10)

    async def _bounded_send(ws):
        async with _sem:
            await _send(ws)

    await asyncio.gather(*[_bounded_send(ws) for ws in clients], return_exceptions=True)
    if dead:
        app[WS_CLIENTS].difference_update(dead)


# --- Dispatch table for consume_for_ui ---
_UI_DISPATCH_TABLE: dict[str, callable] = {
    'signal': lambda item, app: broadcast(item, app),
    'volume_sniper_signal': lambda item, app: broadcast(item, app),
    'volume_context_watch': lambda item, app: broadcast(item, app),
    'chart_v3_event': lambda item, app: broadcast(item, app),
    'status': lambda item, app: broadcast(item, app),
    'day_levels': lambda item, app: broadcast(item, app),
    'warmup_done': lambda item, app: broadcast(item, app),
    'ai_analysis': lambda item, app: broadcast(item, app),
}


async def consume_for_ui(ui_queue: asyncio.Queue, app: web.Application) -> None:
    while True:
        item = await ui_queue.get()
        try:
            if isinstance(item, IndicatorSnapshot):
                await broadcast({
                    'type': 'candle',
                    'symbol': item.symbol,
                    'tf': item.tf, 'start_ts': item.timestamp,
                    'open': item.open, 'high': item.high, 'low': item.low, 'close': item.close,
                    'ema200': item.ema200, 'rsi14': item.rsi14,
                    'volume': item.volume, 'is_closed': item.is_closed,
                }, app)
            elif isinstance(item, dict):
                msg_type = item.get('type')
                handler = _UI_DISPATCH_TABLE.get(msg_type)
                if handler:
                    await handler(item, app)
                else:
                    logger.warning("consume_for_ui: unknown message type '%s'", msg_type)
            else:
                logger.warning("consume_for_ui: unexpected item type %s", type(item).__name__)
        finally:
            ui_queue.task_done()


async def serve(ui_queue: asyncio.Queue, candle_queue: asyncio.Queue, signal_queue: asyncio.Queue, host: str = None, port: int = None, state=None, scanner=None) -> None:
    if host is None:
        host = WEB_HOST
    if port is None:
        port = WEB_PORT

    app = web.Application(middlewares=[basic_auth_middleware])

    # Initialize WS state on app
    app[WS_CLIENTS] = set()
    app[WS_CONNECTIONS_BY_IP] = {}
    app[CANDLE_BUFFER] = {}
    global _IS_WARMING_UP
    _IS_WARMING_UP = True
    # Scale buffer to hold all symbols' data — avoids rebuild_candle_buffer overwrite
    from core.config import SYMBOLS
    _base_max = int(os.getenv('MAX_CHART_CANDLES', '10080'))
    app[MAX_CANDLE_BUFFER_PER_TF] = _base_max * max(len(SYMBOLS), 1)
    app[MAX_WS_PER_IP] = 10
    app[MAX_WS_TOTAL] = 50
    app[STARTED_AT] = time.time()
    app[CANDLE_QUEUE] = candle_queue
    app[SIGNAL_QUEUE] = signal_queue
    app[UI_QUEUE] = ui_queue
    app[DAY_LEVELS] = None
    app[STATE] = state
    app[SCANNER] = scanner
    app[WS_MSG_COUNTS] = {}

    # Load persisted signal buffers from disk
    persisted_signals = load_signal_buffer()
    app[SIGNAL_BUFFER_KEY] = persisted_signals if isinstance(persisted_signals, list) else []
    app[CONTEXT_WATCH_BUFFER] = []

    app[HEALTH_CACHE] = None
    app[HEALTH_CACHE_TS] = 0

    app.router.add_get('/',         landing_handler)
    app.router.add_get('/dashboard', dashboard_handler)
    app.router.add_get('/api/context-plan', context_plan_handler)
    app.router.add_get('/api/chart-plan', chart_plan_handler)
    app.router.add_get('/api/derivatives-context', derivatives_context_handler)
    app.router.add_get('/ws',        ws_handler)
    app.router.add_get('/health',    health_handler)
    app.router.add_get('/metrics',   make_aiohttp_handler())
    app.router.add_get('/favicon.ico', favicon_handler)
    app.router.add_get('/static/{filename:.*}', static_handler)
    if os.getenv('ENABLE_DEBUG_ENDPOINTS', '').lower() in ('1', 'true', 'yes'):
        app.router.add_get('/debug/buffer', buffer_debug_handler)
        logger.warning("Debug endpoints ENABLED — do not use in production")

    WS_CONNECTIONS.set_function(lambda: len(app[WS_CLIENTS]))

    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx = None
    if WEB_SSL_CERT and WEB_SSL_KEY:
        import ssl
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(WEB_SSL_CERT, WEB_SSL_KEY)
        scheme = 'https'
        logger.info(f"TLS enabled: cert={WEB_SSL_CERT}")
    else:
        scheme = 'http'

    global _DASHBOARD_APP
    _DASHBOARD_APP = app

    site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
    await site.start()
    logger.info(f"Web Dashboard: {scheme}://{host}:{port}")

    async def periodic_save_buffers():
        """Auto-save signal buffer to disk every 60s."""
        while True:
            await asyncio.sleep(60)
            try:
                if app.get(SIGNAL_BUFFER_KEY) is not None:
                    await save_signal_buffer(app[SIGNAL_BUFFER_KEY])
            except Exception:
                logger.error("periodic_save_buffers failed", exc_info=True)

    async def ws_heartbeat():
        """Send periodic ping to all connected WebSocket clients (every 30s)."""
        while True:
            await asyncio.sleep(30)
            dead = set()
            for ws in app[WS_CLIENTS]:
                try:
                    await ws.ping()
                except Exception:
                    dead.add(ws)
            if dead:
                app[WS_CLIENTS].difference_update(dead)

    save_task = asyncio.create_task(periodic_save_buffers())
    heartbeat_task = asyncio.create_task(ws_heartbeat())

    try:
        await consume_for_ui(ui_queue, app)
    finally:
        save_task.cancel()
        heartbeat_task.cancel()
        try:
            await save_task
        except asyncio.CancelledError:
            pass
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        # Final save on shutdown
        try:
            if app.get(SIGNAL_BUFFER_KEY) is not None:
                await save_signal_buffer(app[SIGNAL_BUFFER_KEY])
            logger.info("Saved signal buffer before shutdown")
        except Exception:
            logger.warning("Final buffer save failed", exc_info=True)


def rebuild_candle_buffer(ohlcv_cache: dict[str, dict[str, list[dict]]]) -> None:
    """Rebuild the CANDLE_BUFFER from a complete OHLCV cache after warmup.

    Called from main.py after warmup_phase completes to ensure the WS
    replay_bulk contains ALL candles across ALL symbols, not just the
    last symbol's data (which happens when max_per_tf < symbol * tf_candles).

    Also merges indicator data (ema200, rsi14) from the existing buffer
    that was populated by warmup broadcasts with full IndicatorSnapshots.

    Args:
        ohlcv_cache: {symbol: {tf: [candles]}} as returned by warmup_phase
    """
    app = _DASHBOARD_APP
    if app is None:
        logger.warning("rebuild_candle_buffer: dashboard not ready yet")
        return

    # Save existing indicator data before overwriting buffer
    old_buffer = dict(app.get(CANDLE_BUFFER, {}))

    buffer: dict[str, list[dict]] = {}
    max_per_tf = app[MAX_CANDLE_BUFFER_PER_TF]

    # Scale buffer to fit all symbols (each symbol has ~WARMUP_CANDLES[tf] candles)
    # Use max_per_tf per symbol so no symbol's data is truncated
    symbols = list(ohlcv_cache.keys())
    scaled_max = max_per_tf * max(len(symbols), 1)
    max_per_symbol = scaled_max  # Allow all candles per symbol per TF

    for symbol in symbols:
        tf_dict = ohlcv_cache[symbol]
        for tf, candles in tf_dict.items():
            if tf not in buffer:
                buffer[tf] = []
            # Tag each candle with symbol
            tagged = [dict(c, symbol=symbol) for c in candles]
            # Take only the most recent candles within the per-symbol quota
            if len(tagged) > max_per_symbol:
                tagged = tagged[-max_per_symbol:]
            buffer[tf].extend(tagged)

    # Merge indicator data from old buffer into rebuilt buffer
    for tf, old_candles in old_buffer.items():
        if tf not in buffer:
            continue
        for old_c in old_candles:
            if 'ema200' not in old_c and 'rsi14' not in old_c:
                continue
            # Find matching candle in new buffer by symbol + start_ts
            for new_c in buffer[tf]:
                if (new_c.get('start_ts') == old_c.get('start_ts')
                        and new_c.get('symbol') == old_c.get('symbol')):
                    if 'ema200' in old_c:
                        new_c['ema200'] = old_c['ema200']
                    if 'rsi14' in old_c:
                        new_c['rsi14'] = old_c['rsi14']
                    break

    app[CANDLE_BUFFER] = buffer
    total = sum(len(v) for v in buffer.values())
    tf_counts = {tf: len(v) for tf, v in buffer.items()}
    logger.info(f"Rebuilt CANDLE_BUFFER: {total} candles across {len(buffer)} TFs: {tf_counts}")
