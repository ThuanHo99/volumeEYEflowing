"""Freeze and cache 15 days of closed 1m data for USD-M symbols.

Completed UTC days come from Binance Public Data; the current partial day and
missing archive packages fall back to bounded /fapi/v1/klines requests. A run
directory contains its frozen universe, timestamps, sources and failures so a
replay never silently substitutes data collected at a later clock.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import json
import os
import time
import zipfile
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

REST = "https://fapi.binance.com"
ARCHIVE = "https://data.binance.vision/data/futures/um/daily/klines"
MINUTE_MS = 60_000
DAY_MS = 86_400_000
WINDOW_MS = 15 * DAY_MS
WARMUP_MS = 2 * DAY_MS  # closed 4h structure can exist before scored window
ARCHIVE_CONCURRENCY = 24
REST_CONCURRENCY = 6
REST_WEIGHT_PER_MINUTE = 700  # leave room under Binance's 2400/minute IP ceiling


def _json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _utc_day(ms: int) -> datetime:
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def archive_days(start_ms: int, end_ms: int) -> list[str]:
    day = _utc_day(start_ms)
    last = _utc_day(end_ms)
    result = []
    while day < last:
        result.append(day.date().isoformat())
        day += timedelta(days=1)
    return result


async def _get_json(session: aiohttp.ClientSession, url: str, **params) -> object:
    async with session.get(url, params=params or None) as response:
        response.raise_for_status()
        return await response.json()


async def freeze_universe(run_dir: Path) -> dict:
    manifest_path = run_dir / "universe.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if "warmup_start_ms" not in manifest:
            manifest["warmup_start_ms"] = manifest["start_ms"] - WARMUP_MS
            manifest["archive_days"] = archive_days(
                manifest["warmup_start_ms"], manifest["end_ms_exclusive"])
            _json_atomic(manifest_path, manifest)
        # The first snapshot listed only TRADING contracts. Include contracts
        # which were still active during the frozen window but have since
        # entered SETTLING. The end time itself never moves on a rerun.
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            exchange = await _get_json(session, REST + "/fapi/v1/exchangeInfo")
        known = {row["symbol"] for row in manifest["symbols"]}
        added = []
        for row in exchange["symbols"]:
            if (row.get("status") != "SETTLING" or row["symbol"] in known or
                    int(row.get("deliveryDate") or 0) <= manifest["start_ms"] or
                    int(row.get("onboardDate") or 0) >= manifest["end_ms_exclusive"]):
                continue
            added.append({
                "symbol": row["symbol"], "status_at_freeze": row.get("status"),
                "contract_type": row.get("contractType"),
                "quote_asset": row.get("quoteAsset"),
                "onboard_ms": row.get("onboardDate"),
                "delivery_ms": row.get("deliveryDate"),
                "tick_size": next((item.get("tickSize") for item in row.get("filters", [])
                                   if item.get("filterType") == "PRICE_FILTER"), None),
            })
        if added:
            manifest["symbols"].extend(added)
            manifest["symbols"].sort(key=lambda row: row["symbol"])
            manifest["universe_definition"] = (
                "TRADING at initial freeze plus SETTLING with delivery during scored window")
            _json_atomic(manifest_path, manifest)
        return manifest
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        server = await _get_json(session, REST + "/fapi/v1/time")
        exchange = await _get_json(session, REST + "/fapi/v1/exchangeInfo")
    end_ms = int(server["serverTime"]) // MINUTE_MS * MINUTE_MS
    start_ms = end_ms - WINDOW_MS
    symbols = [{
        "symbol": row["symbol"],
        "status_at_freeze": row.get("status"),
        "contract_type": row.get("contractType"),
        "quote_asset": row.get("quoteAsset"),
        "onboard_ms": row.get("onboardDate"),
        "delivery_ms": row.get("deliveryDate"),
        "tick_size": next((item.get("tickSize") for item in row.get("filters", [])
                           if item.get("filterType") == "PRICE_FILTER"), None),
    } for row in exchange["symbols"]
        if row.get("status") == "TRADING" or
        (row.get("status") == "SETTLING" and
         int(row.get("deliveryDate") or 0) > start_ms and
         int(row.get("onboardDate") or 0) < end_ms)]
    symbols.sort(key=lambda row: row["symbol"])
    manifest = {
        "source": "Binance USD-M /fapi/v1/exchangeInfo and /fapi/v1/time",
        "archive_source": ARCHIVE,
        "rest_source": REST + "/fapi/v1/klines",
        "frozen_at_server_ms": int(server["serverTime"]),
        "universe_definition": "TRADING at freeze plus SETTLING with delivery during scored window",
        "start_ms": start_ms,
        "warmup_start_ms": start_ms - WARMUP_MS,
        "end_ms_exclusive": end_ms,
        "interval": "1m",
        "symbols": symbols,
        "archive_days": archive_days(start_ms - WARMUP_MS, end_ms),
    }
    _json_atomic(manifest_path, manifest)
    return manifest


def _archive_path(run_dir: Path, symbol: str, day: str) -> Path:
    return run_dir / "archive" / symbol / f"{symbol}-1m-{day}.zip"


def _rest_path(run_dir: Path, symbol: str, day: str) -> Path:
    return run_dir / "rest" / symbol / f"{symbol}-1m-{day}.json.gz"


def _valid_zip(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return len(archive.namelist()) == 1 and archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


async def _download_archive(session: aiohttp.ClientSession, run_dir: Path,
                            symbol: str, day: str) -> dict:
    path = _archive_path(run_dir, symbol, day)
    if path.exists():
        return {"symbol": symbol, "day": day, "status": "cached",
                "bytes": path.stat().st_size}
    url = f"{ARCHIVE}/{symbol}/1m/{symbol}-1m-{day}.zip"
    for attempt in range(4):
        try:
            async with session.get(url) as response:
                if response.status == 404:
                    return {"symbol": symbol, "day": day, "status": "archive_missing"}
                if response.status in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {response.status}")
                response.raise_for_status()
                data = await response.read()
            if not _valid_zip(data):
                raise ValueError("ZIP/CRC validation failed")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".zip.part")
            temp.write_bytes(data)
            os.replace(temp, path)
            return {"symbol": symbol, "day": day, "status": "downloaded",
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            if attempt == 3:
                return {"symbol": symbol, "day": day, "status": "archive_error",
                        "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            await asyncio.sleep(min(0.5 * 2 ** attempt, 4.0))
    raise AssertionError("unreachable")


async def download_archives(run_dir: Path, manifest: dict) -> list[dict]:
    days = manifest["archive_days"]
    jobs = []
    for row in manifest["symbols"]:
        onboard = int(row.get("onboard_ms") or 0)
        delivery = int(row.get("delivery_ms") or 0)
        for day in days:
            day_start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
            if day_start + DAY_MS <= onboard or (delivery and day_start >= delivery):
                continue
            jobs.append((row["symbol"], day))
    queue: asyncio.Queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    results: list[dict] = []
    timeout = aiohttp.ClientTimeout(total=35)
    connector = aiohttp.TCPConnector(limit=ARCHIVE_CONCURRENCY,
                                     limit_per_host=ARCHIVE_CONCURRENCY)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def worker() -> None:
            while True:
                try:
                    symbol, day = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                result = await _download_archive(session, run_dir, symbol, day)
                results.append(result)
                if len(results) % 500 == 0:
                    print(f"archive {len(results)}/{len(jobs)}", flush=True)
                queue.task_done()

        await asyncio.gather(*(worker() for _ in range(ARCHIVE_CONCURRENCY)))
    results.sort(key=lambda row: (row["symbol"], row["day"]))
    _json_atomic(run_dir / "archive_manifest.json", {
        "jobs": len(jobs), "statuses": dict(Counter(row["status"] for row in results)),
        "results": results,
    })
    return results


class _WeightLimiter:
    def __init__(self, capacity: int = REST_WEIGHT_PER_MINUTE):
        self.capacity = capacity
        self.used: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self.used and now - self.used[0][0] >= 60:
                    self.used.popleft()
                if sum(item[1] for item in self.used) + weight <= self.capacity:
                    self.used.append((now, weight))
                    return
                await asyncio.sleep(min(1.0, max(0.05, 60 - (now - self.used[0][0]))))


async def _rest_day(session: aiohttp.ClientSession, limiter: _WeightLimiter,
                    symbol: str, day: str, start_ms: int, end_ms: int) -> tuple[list, str | None]:
    rows: list = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + 1_000 * MINUTE_MS, end_ms) - 1
        params = {"symbol": symbol, "interval": "1m", "startTime": cursor,
                  "endTime": chunk_end, "limit": 1000}
        for attempt in range(4):
            try:
                await limiter.acquire(5)
                async with session.get(REST + "/fapi/v1/klines", params=params) as response:
                    if response.status in (429, 418, 500, 502, 503, 504):
                        raise RuntimeError(f"HTTP {response.status}")
                    response.raise_for_status()
                    part = await response.json()
                if not isinstance(part, list):
                    raise ValueError("non-list kline response")
                rows.extend(part)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                if attempt == 3:
                    return rows, f"{type(exc).__name__}: {str(exc)[:120]}"
                await asyncio.sleep(min(1.0 * 2 ** attempt, 8.0))
        cursor = chunk_end + 1
    return rows, None


async def download_rest_days(run_dir: Path, manifest: dict,
                             archive_results: list[dict]) -> list[dict]:
    day_now = _utc_day(manifest["end_ms_exclusive"]).date().isoformat()
    current_start = int(_utc_day(manifest["end_ms_exclusive"]).timestamp() * 1000)
    jobs = [(row["symbol"], day_now, current_start, manifest["end_ms_exclusive"])
            for row in manifest["symbols"]
            if int(row.get("onboard_ms") or 0) < manifest["end_ms_exclusive"] and
            (not row.get("delivery_ms") or
             int(row["delivery_ms"]) > current_start)]
    for result in archive_results:
        if result["status"] not in ("archive_missing", "archive_error"):
            continue
        day = result["day"]
        start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        jobs.append((result["symbol"], day, start, start + DAY_MS))
    queue: asyncio.Queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    results = []
    limiter = _WeightLimiter()
    timeout = aiohttp.ClientTimeout(total=25)
    connector = aiohttp.TCPConnector(limit=REST_CONCURRENCY,
                                     limit_per_host=REST_CONCURRENCY)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def worker() -> None:
            while True:
                try:
                    symbol, day, start, end = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                path = _rest_path(run_dir, symbol, day)
                if path.exists():
                    outcome = {"symbol": symbol, "day": day, "status": "cached",
                               "rows": None}
                else:
                    rows, error = await _rest_day(session, limiter, symbol, day, start, end)
                    if error is None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temp = path.with_suffix(path.suffix + ".tmp")
                        with gzip.open(temp, "wt", encoding="utf-8") as handle:
                            json.dump(rows, handle, separators=(",", ":"))
                        os.replace(temp, path)
                    outcome = {"symbol": symbol, "day": day,
                               "status": "rest_ok" if error is None else "rest_error",
                               "rows": len(rows), **({"error": error} if error else {})}
                results.append(outcome)
                if len(results) % 100 == 0:
                    print(f"REST {len(results)}/{len(jobs)}", flush=True)
                queue.task_done()

        await asyncio.gather(*(worker() for _ in range(REST_CONCURRENCY)))
    results.sort(key=lambda row: (row["symbol"], row["day"]))
    _json_atomic(run_dir / "rest_manifest.json", {
        "jobs": len(jobs), "statuses": dict(Counter(row["status"] for row in results)),
        "results": results,
    })
    return results


async def acquire_data(run_dir: Path) -> dict:
    manifest = await freeze_universe(run_dir)
    print(f"frozen {len(manifest['symbols'])} window-active USD-M symbols; "
          f"{manifest['start_ms']}..{manifest['end_ms_exclusive']}", flush=True)
    archives = await download_archives(run_dir, manifest)
    print("archives", dict(Counter(row["status"] for row in archives)), flush=True)
    rest = await download_rest_days(run_dir, manifest, archives)
    print("REST", dict(Counter(row["status"] for row in rest)), flush=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/usdm_15d_latest"))
    args = parser.parse_args()
    asyncio.run(acquire_data(args.run_dir))


if __name__ == "__main__":
    main()
