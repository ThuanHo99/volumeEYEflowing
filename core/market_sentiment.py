"""Market sentiment data — news-based sentiment + OHLCV-based trend (no external API dependency)."""
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class TrendLabel(str, Enum):
    """Five-level market trend classification for 24h price movement."""
    STRONG_BULLISH = "Strong Bullish"
    BULLISH = "Bullish"
    SIDEWAYS = "Sideways"
    BEARISH = "Bearish"
    STRONG_BEARISH = "Strong Bearish"


@dataclass
class FearGreedData:
    """Market sentiment data — derived from news or external API."""
    value: int
    classification: str
    timestamp: int
    time_until_update: int


@dataclass
class BinanceTickerData:
    """24hr price trend data — derived from OHLCV or external API."""
    symbol: str
    price_change_pct: float
    quote_volume: float
    trend: TrendLabel


def classify_trend(price_change_pct: float, quote_volume: Optional[float] = None) -> TrendLabel:
    """Classify 24h price change into 5 trend levels.

    Thresholds:
      >= +5.0%: Strong Bullish
      >= +1.0%: Bullish
      >  -1.0%: Sideways (noise range)
      >  -5.0%: Bearish
      <= -5.0%: Strong Bearish
    """
    if price_change_pct >= 5.0:
        return TrendLabel.STRONG_BULLISH
    elif price_change_pct >= 1.0:
        return TrendLabel.BULLISH
    elif price_change_pct > -1.0:
        return TrendLabel.SIDEWAYS
    elif price_change_pct > -5.0:
        return TrendLabel.BEARISH
    else:
        return TrendLabel.STRONG_BEARISH


# =============================================================================
# News-based sentiment (replaces Alternative.me F&G API)
# =============================================================================

BULLISH_KEYWORDS: list[str] = [
    'bullish', 'surge', 'rally', 'breakout', 'green', 'gain',
    'record high', 'all-time high', 'ath', 'moon', 'pump',
    'etf inflow', 'accumulation', 'adoption', 'partnership',
    'mainstream', 'institutional', 'approval', 'approved',
    'rate cut', 'easing', 'dovish', 'stimulus', 'qe',
    'halving', 'upgrade', 'outperform', 'overweight',
]

BEARISH_KEYWORDS: list[str] = [
    'bearish', 'crash', 'dump', 'correction', 'red', 'loss',
    'record low', 'panic', 'fear', 'sell-off', 'selloff',
    'crackdown', 'ban', 'banned', 'lawsuit', 'regulation',
    'sec', 'cftc', 'doj', 'investigation', 'hack', 'exploit',
    'rate hike', 'hawkish', 'tightening', 'qt', 'recession',
    'downgrade', 'underperform', 'underweight', 'delist',
]


def _count_keyword_hits(text: str, keywords: list[str]) -> int:
    """Count how many keywords from the list appear in the text (case-insensitive)."""
    text_lower = text.lower()
    return sum(1 for kw in keywords if kw in text_lower)


def derive_sentiment_from_news(news_texts: list[str]) -> Optional[FearGreedData]:
    """Derive a Fear & Greed-like score (0-100) from news sentiment analysis.

    Counts bullish vs bearish keywords across all news texts.
    Maps the ratio to a 0-100 scale:
      0-25: Extreme Fear    55-75: Greed
      25-45: Fear           75-100: Extreme Greed
      45-55: Neutral

    Returns None if news_texts is empty, Neutral (50) if no keywords matched.
    """
    if not news_texts or len(news_texts) == 0:
        logger.debug("No news texts for sentiment analysis")
        return None

    total_bullish = 0
    total_bearish = 0

    for text in news_texts:
        total_bullish += _count_keyword_hits(text, BULLISH_KEYWORDS)
        total_bearish += _count_keyword_hits(text, BEARISH_KEYWORDS)

    total_signals = total_bullish + total_bearish

    if total_signals == 0:
        logger.debug("No sentiment keywords found in news — returning neutral")
        return FearGreedData(
            value=50, classification='Neutral',
            timestamp=int(time.time()), time_until_update=0,
        )

    # Map bullish ratio to 0-100: base 20 + ratio * 60 gives 20-80 range
    bullish_ratio = total_bullish / total_signals
    score = int(20 + (bullish_ratio * 60))
    score = max(0, min(100, score))

    if score >= 75:
        classification = 'Extreme Greed'
    elif score >= 55:
        classification = 'Greed'
    elif score >= 45:
        classification = 'Neutral'
    elif score >= 25:
        classification = 'Fear'
    else:
        classification = 'Extreme Fear'

    logger.info(
        f"News sentiment: {score}/100 ({classification}) "
        f"from {total_bullish}B/{total_bearish}S hits across {len(news_texts)} articles"
    )

    return FearGreedData(
        value=score, classification=classification,
        timestamp=int(time.time()), time_until_update=0,
    )


# =============================================================================
# OHLCV-based trend (replaces Binance 24hr ticker API)
# =============================================================================

def derive_trend_from_ohlcv(state, symbol: str = '') -> Optional[BinanceTickerData]:
    """Calculate 24h trend from local OHLCV close_history data.

    Uses 4h candles for the broadest 24h view, falling back to 1h.
    No external API call — pure local computation.

    Args:
        state: IndicatorState with close_history dict (tf -> list[float])
        symbol: Trading pair for the result (e.g. 'BTC/USDT:USDT')
    """
    # BUG-M4 FIX: Replaced for-else with explicit variable assignment.
    # The original for-else set 'closes' and 'tf' as implicit loop variables.
    # If refactored without understanding for-else semantics, 'closes' could be
    # undefined at the lines below, causing UnboundLocalError.
    # Now uses a clear None-check guard that is self-documenting.
    sym_clean = symbol.replace('/', '').split(':')[0]  # 'BTC/USDT:USDT' -> 'BTCUSDT'
    closes: list[float] | None = None
    tf: str | None = None
    for candidate_tf in ['4h', '1h']:
        c = state.close_history.get(candidate_tf, [])
        if len(c) >= 2:
            closes = list(c)  # convert deque to list for safe indexing
            tf = candidate_tf
            break

    if closes is None or tf is None:
        logger.warning("Not enough OHLCV data for trend calculation")
        return None

    old_price = closes[0]
    new_price = closes[-1]

    if old_price <= 0:
        return None

    price_change_pct = ((new_price - old_price) / old_price) * 100
    trend = classify_trend(price_change_pct)

    logger.debug(
        f"OHLCV trend ({symbol} {tf}): {old_price:.2f} -> {new_price:.2f} "
        f"({price_change_pct:+.2f}%) = {trend.value}"
    )

    return BinanceTickerData(
        symbol=sym_clean,
        price_change_pct=round(price_change_pct, 4),
        quote_volume=0.0,
        trend=trend,
    )
