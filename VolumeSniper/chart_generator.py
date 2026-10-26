"""
chart_generator.py - Tạo ảnh biểu đồ nến từ dữ liệu OHLCV.
"""
import io
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
from typing import List, Optional
from datetime import datetime, timedelta

def generate_candlestick_chart(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: Optional[List[float]] = None,
    title: str = "Volume Spike",
    interval_minutes: int = 5,
) -> io.BytesIO:
    """
    Vẽ biểu đồ nến và trả về BytesIO chứa ảnh PNG.
    """
    # Tạo DatetimeIndex giả định (mỗi nến cách nhau interval_minutes phút)
    end_time = datetime.now()
    start_time = end_time - timedelta(minutes=interval_minutes * (len(opens) - 1))
    dates = pd.date_range(start=start_time, end=end_time, periods=len(opens))

    df = pd.DataFrame({
        'Open': opens,
        'High': highs,
        'Low': lows,
        'Close': closes,
    }, index=dates)

    if volumes:
        df['Volume'] = volumes

    mc = mpf.make_marketcolors(
        up='#00e5a0', down='#ff6b35',
        edge='inherit', wick='inherit',
        volume='in'
    )
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=False)

    buf = io.BytesIO()
    kwargs = dict(
        type='candle',
        style=style,
        title=title,
        ylabel='Price',
        volume=bool(volumes),
        figsize=(10, 6),
        savefig=dict(fname=buf, bbox_inches='tight', dpi=100)
    )

    mpf.plot(df, **kwargs)
    buf.seek(0)
    return buf

async def async_generate_candlestick_chart(*args, **kwargs) -> io.BytesIO:
    import asyncio
    return await asyncio.to_thread(generate_candlestick_chart, *args, **kwargs)