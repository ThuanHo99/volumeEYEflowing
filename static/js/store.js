/**
 * store.js — Data layer for Multi-Symbol Signal Tracker
 * Pure data management: candle/line/volume storage per symbol per timeframe.
 * No DOM or chart dependencies.
 */

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'];
const MAX_CHART_POINTS = 5000;

// Per-symbol store: { symbol: { tf: { candles: [], ema: [], rsi: [], vols: [] } } }
const stores = {};
let currentSymbol = null;

let candleCount = 0;
const CANDLE_COUNT_MAX = 999999;

function ensureStore(symbol) {
  if (!symbol) return null;
  if (!stores[symbol]) {
    stores[symbol] = {};
    TIMEFRAMES.forEach(function (tf) {
      stores[symbol][tf] = { candles: [], ema: [], rsi: [], vols: [] };
    });
  }
  return stores[symbol];
}

/**
 * Binary search insertion to maintain sorted order (O(log n) instead of O(n log n)).
 * Inserts item at correct position in a time-sorted array.
 */
function binaryInsert(arr, item) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (arr[mid].time < item.time) lo = mid + 1;
    else hi = mid;
  }
  arr.splice(lo, 0, item);
  if (arr.length > MAX_CHART_POINTS) arr.shift();
}

function upsertCandle(symbol, tf, bar) {
  if (!symbol) return;
  const store = ensureStore(symbol);
  const arr = store[tf].candles;
  if (arr.length > 0) {
    const last = arr[arr.length - 1];
    if (last.time === bar.time) { arr[arr.length - 1] = bar; return; }
    if (bar.time > last.time) { arr.push(bar); if (arr.length > MAX_CHART_POINTS) arr.shift(); return; }
  } else {
    arr.push(bar); return;
  }
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i].time === bar.time) { arr[i] = bar; return; }
  }
  binaryInsert(arr, bar);
}

function upsertLine(symbol, tf, key, point) {
  if (!symbol) return;
  const store = ensureStore(symbol);
  const arr = store[tf][key];
  if (arr.length > 0) {
    const last = arr[arr.length - 1];
    if (last.time === point.time) { arr[arr.length - 1] = point; return; }
    if (point.time > last.time) { arr.push(point); if (arr.length > MAX_CHART_POINTS) arr.shift(); return; }
  } else {
    arr.push(point); return;
  }
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i].time === point.time) { arr[i] = point; return; }
  }
  binaryInsert(arr, point);
}

function upsertVol(symbol, tf, point) {
  if (!symbol) return;
  const store = ensureStore(symbol);
  const arr = store[tf].vols;
  if (arr.length > 0) {
    const last = arr[arr.length - 1];
    if (last.time === point.time) { arr[arr.length - 1] = point; return; }
    if (point.time > last.time) { arr.push(point); if (arr.length > MAX_CHART_POINTS) arr.shift(); return; }
  } else {
    arr.push(point); return;
  }
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i].time === point.time) { arr[i] = point; return; }
  }
  binaryInsert(arr, point);
}

/**
 * Process an incoming candle message — store data only.
 * Returns { symbol, tf, bar, hasEma, hasRsi } for UI layer to act on.
 */
function processCandle(msg) {
  const symbol = msg.symbol || currentSymbol || '';
  const tf = msg.tf;
  const bar = {
    time: Math.floor(msg.start_ts / 1000),
    open: msg.open,
    high: msg.high,
    low: msg.low,
    close: msg.close,
    volume: msg.volume,
  };

  upsertCandle(symbol, tf, bar);

  const hasEma = msg.ema200 != null;
  const hasRsi = msg.rsi14 != null;

  if (hasEma) {
    upsertLine(symbol, tf, 'ema', { time: bar.time, value: msg.ema200 });
  }
  if (hasRsi) {
    upsertLine(symbol, tf, 'rsi', { time: bar.time, value: msg.rsi14 });
  }
  if (msg.volume != null) {
    upsertVol(symbol, tf, {
      time: bar.time,
      value: msg.volume,
      color: msg.close >= msg.open ? '#1c4a3c' : '#4a1c1c',
    });
  }

  if (candleCount < CANDLE_COUNT_MAX) {
    candleCount++;
  }

  return { symbol, tf, bar, hasEma, hasRsi };
}

function getStore(symbol, tf) {
  if (!symbol) symbol = currentSymbol || '';
  if (!symbol) return tf ? undefined : undefined;
  if (!stores[symbol]) ensureStore(symbol);
  if (tf) return stores[symbol][tf];
  return stores[symbol];
}

function getCandles(symbol, tf) {
  const s = getStore(symbol, tf);
  return s ? s.candles : [];
}

function getCount() {
  return candleCount;
}

export {
  TIMEFRAMES,
  MAX_CHART_POINTS,
  stores,
  currentSymbol,
  candleCount,
  ensureStore,
  upsertCandle,
  upsertLine,
  upsertVol,
  processCandle,
  getCandles,
  getCount,
  getStore,
};
