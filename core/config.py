"""Centralized configuration with validation. Imported by all modules."""
import os
import sys
import logging

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()


# --- Validation helpers ---
def _int_env(key: str, default: str, name_en: str) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        logger.warning(f"{key} ({name_en}) invalid, using default {default}")
        return int(default)


def _float_env(key: str, default: str, name_en: str) -> float:
    try:
        return float(os.getenv(key, default))
    except ValueError:
        logger.warning(f"{key} ({name_en}) invalid, using default {default}")
        return float(default)


# --- String config (free-form, no validation needed) ---
EXCHANGE_ID: str = os.getenv('EXCHANGE_ID', 'binanceusdm')
_SYMBOL_ENV: str = os.getenv('SYMBOL', 'BTC/USDT:USDT')
_SYMBOLS_ENV: str = os.getenv('SYMBOLS', _SYMBOL_ENV)
SYMBOLS: list[str] = [s.strip() for s in _SYMBOLS_ENV.split(',') if s.strip()]
SYMBOL: str = SYMBOLS[0]  # backward compat: first symbol
TELEGRAM_BOT_TOKEN: str | None = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID: str | None = os.getenv('TELEGRAM_CHAT_ID')
WEB_HOST: str = os.getenv('WEB_HOST', '127.0.0.1')
WEB_USER: str | None = os.getenv('WEB_USER')
WEB_PASS: str | None = os.getenv('WEB_PASS')
WEB_SSL_CERT: str | None = os.getenv('WEB_SSL_CERT')
WEB_SSL_KEY: str | None = os.getenv('WEB_SSL_KEY')

# --- Integer config (validated) ---
HISTORICAL_CANDLES: int = _int_env('HISTORICAL_CANDLES', '1000', 'Historical candle count')
MAX_CANDLES_PER_TF: int = _int_env('MAX_CANDLES_PER_TF', '500', 'Max candles per TF')
FEAR_GREED_CACHE_TTL: int = _int_env('FEAR_GREED_CACHE_TTL', '60', 'F&G cache TTL (seconds)')
WEB_PORT: int = _int_env('WEB_PORT', '8080', 'Web UI port')
GRACE_PERIOD_MS: int = _int_env('GRACE_PERIOD_MS', '100', 'Candle transition grace period (ms)')
RSI_OVERSOLD: int = _int_env('RSI_OVERSOLD', '25', 'RSI oversold threshold')
RSI_OVERBOUGHT: int = _int_env('RSI_OVERBOUGHT', '75', 'RSI overbought threshold')
SIGNAL_TOLERANCE_PCT: float = _float_env('SIGNAL_TOLERANCE_PCT', '1.5', 'Price-EMA tolerance for signals (%)')
SIGNAL_BUFFER_SIZE: int = _int_env('SIGNAL_BUFFER_SIZE', '200', 'Max signals in WS buffer')
CONFLUENCE_MAX_AGE_SEC: int = _int_env('CONFLUENCE_MAX_AGE_SEC', '900', 'Max age for confluence RSI extreme (seconds)')
VS_TOUCH_BUFFER_PCT: float = _float_env('VS_TOUCH_BUFFER_PCT', '0.1', 'EMA touch buffer zone (%)')

# --- Float config (validated) ---
FETCH_TIMEOUT_SECONDS: int = _int_env('FETCH_TIMEOUT_SECONDS', '60', 'Historical data fetch timeout (seconds)')
MARKET_FETCH_TIMEOUT: int = _int_env('MARKET_FETCH_TIMEOUT', '10', 'Market data fetch timeout (seconds)')
TELEGRAM_RATE_LIMIT: int = _int_env('TELEGRAM_RATE_LIMIT', '60', 'Telegram message rate limit (seconds)')
MIN_VOLUME_THRESHOLD_USDT: float = _float_env('MIN_VOLUME_THRESHOLD_USDT', '50000.0', 'Min average volume in USDT for spike detection')
VOLUME_SPIKE_MULTIPLIER: float = _float_env('VOLUME_SPIKE_MULTIPLIER', '2.0', 'Volume spike multiplier')

# --- Parsed string config ---
TIMEFRAMES: list[str] = os.getenv('TIMEFRAMES', '1m,5m,15m,1h,4h').split(',')
EMA_TOUCH_TIMEFRAMES: list[str] = os.getenv('EMA_TOUCH_TIMEFRAMES', '1m,5m,15m,1h,4h').split(',')

REQUIRED_SIGNAL_TFS: list[str] = ['15m', '1h', '4h']


def validate_startup_config() -> list[str]:
    errors: list[str] = []
    if not WEB_USER or not WEB_PASS:
        errors.append("WEB_USER and WEB_PASS must be configured via env")
    if not SYMBOLS:
        errors.append("SYMBOLS is empty — at least one trading pair required (e.g. BTC/USDT:USDT)")
    for sym in SYMBOLS:
        if '/' not in sym:
            errors.append(f"Invalid symbol '{sym}' — must contain '/' (e.g. BTC/USDT:USDT)")
    return errors


def validate_required_timeframes() -> list[str]:
    warnings: list[str] = []
    for tf in REQUIRED_SIGNAL_TFS:
        if tf not in TIMEFRAMES:
                warnings.append(f"Missing timeframe '{tf}' — signal checks using this TF will not work")
    return warnings

# --- Cache retention ---
CACHE_RETENTION_DAYS: dict[str, int] = {
    '1m': 7,
    '5m': 14,
    '15m': 21,
    '1h': 30,
    '4h': 30,
}

# --- Warmup fetch counts (per-TF candles to fetch on cold start) ---
WARMUP_CANDLES: dict[str, int] = {
    '1m': 10080,   # 7 days * 24h * 60m
    '5m': 2016,    # 7 days * 24h * 12
    '15m': 672,    # 7 days * 24h * 4
    '1h': 500,
    '4h': 500,
}

# --- File paths ---
DATA_DIR: str = os.getenv('DATA_DIR', 'data')

# --- VolumeSniper Integration ---
VS_ENABLED: bool = os.getenv('VS_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
CORE_LEGACY_ALERTS_ENABLED: bool = os.getenv('CORE_LEGACY_ALERTS_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')

