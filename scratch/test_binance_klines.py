import asyncio
import aiohttp
import time
import datetime

async def main():
    async with aiohttp.ClientSession() as session:
        symbol = "BTCUSDT"
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": "1m", "limit": 2}
        async with session.get(url, params=params) as r:
            klines = await r.json()
            for idx, k in enumerate(klines):
                ts = k[0]
                dt = datetime.datetime.fromtimestamp(ts/1000)
                vol = float(k[5])
                print(f"Candle {idx}: TS={ts} ({dt.isoformat()}), Volume={vol}")

if __name__ == "__main__":
    asyncio.run(main())
