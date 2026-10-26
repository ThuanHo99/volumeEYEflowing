# 📊 Báo Cáo Tổng Quan Hệ Thống Signal

> **Lưu trữ lịch sử:** báo cáo này mô tả bộ quy tắc legacy trước context v2.
> Luồng mặc định và giới hạn hiện tại nằm trong [docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md).

> Cập nhật: 2026-05-31 | Nguồn: `core/signals.py`, `VolumeSniper/analyzer.py`, `VolumeSniper/volume_detector.py`, `VolumeSniper/scanner.py`

---

## Phần 1 — Alerts & Signals (`core/signals.py`)

Theo dõi **các symbol cấu hình trong `.env`** (ví dụ: BTC, ETH). Dữ liệu lấy từ ccxt WebSocket → nhiều timeframe song song.

### 1.1 — Danh sách Signal Types

| # | Signal | Loại | Timeframe | Độ ưu tiên | Điều kiện kích hoạt |
|---|--------|------|-----------|-----------|----------------------|
| S1 | `RSI_OVERBOUGHT` | Momentum | **15m only** | ⚠️ WARNING | RSI 14 > **75** |
| S2 | `RSI_OVERSOLD` | Momentum | **15m only** | ⚠️ WARNING | RSI 14 < **25** |
| S3 | `VOLUME_SPIKE` | Volume | **1h only** | 🔔 ALERT | Volume 1h > **2.0x** avg 24h VÀ avg_vol_usdt >= **50,000 USDT** |
| S4 | `EMA_SUPPORT_TOUCH` | Structure | 1m, 5m, 15m, 1h, 4h | 🔔 ALERT | Close ≥ EMA200 (lệch ≤ **1.5%**) VÀ Low ≤ EMA200 + **0.1% buffer** |
| S5 | `EMA_RESISTANCE_TOUCH` | Structure | 1m, 5m, 15m, 1h, 4h | 🔔 ALERT | Close ≤ EMA200 (lệch ≤ **1.5%**) VÀ High ≥ EMA200 - **0.1% buffer** |
| S6 | `BULLISH_DIVERGENCE` | Divergence | **15m only** | 🔔 ALERT | So sánh **2 đáy Pivot Low** đã xác nhận (đáy sau thấp hơn, rsi đáy sau cao hơn) |
| S7 | `BEARISH_DIVERGENCE` | Divergence | **15m only** | 🔔 ALERT | So sánh **2 đỉnh Pivot High** đã xác nhận (đỉnh sau cao hơn, rsi đỉnh sau thấp hơn) |
| S8 | `CONFLUENCE_LONG` | Confluence | **MTF** | 🚨 HIGH PRIORITY | EMA Support Touch + RSI Oversold **trong 15 phút** |
| S9 | `CONFLUENCE_SHORT` | Confluence | **MTF** | 🚨 HIGH PRIORITY | EMA Resistance Touch + RSI Overbought **trong 15 phút** |

### 1.2 — Điều kiện chi tiết từng Signal

#### S1 / S2 — RSI Extremes (`check_rsi_extremes`)
```
Chỉ chạy khi: snapshot.tf == '15m'
RSI_OVERBOUGHT → RSI14 > 75   (cấu hình: RSI_OVERBOUGHT=75)
RSI_OVERSOLD   → RSI14 < 25   (cấu hình: RSI_OVERSOLD=25)
```
- Nếu S1/S2 kích hoạt → lưu vào `_last_rsi_extreme` (dùng cho S8/S9 cross-TF)
- `_last_rsi_extreme` tự xóa sau **15 phút** (`CONFLUENCE_MAX_AGE_SEC` = 900s) không có snapshot 15m mới

#### S3 — Volume Spike (`check_volume_spike`)
```
Chỉ chạy khi: snapshot.tf == '1h'
avg_24h_volume quy đổi theo USDT phải >= MIN_VOLUME_THRESHOLD_USDT = 50000.0
volume_1h > avg_24h_volume * VOLUME_SPIKE_MULTIPLIER = 2.0x
```
> ⚠️ Đây là spike đơn giản (2x), khác hoàn toàn với VolumeSniper (5x + nhiều filter)

#### S4 / S5 — EMA Touch (`check_ema_touch`)
```
Chỉ chạy khi: tf trong EMA_TOUCH_TIMEFRAMES = ['1m','5m','15m','1h','4h']
Tolerance: |close - EMA200| / EMA200 <= 1.5% (SIGNAL_TOLERANCE_PCT)
EMA Touch Buffer: VS_TOUCH_BUFFER_PCT = 0.1%
Hỗ trợ Front-running & Liquidity Sweep:
  EMA_SUPPORT_TOUCH    : close >= EMA200 VÀ candle_low  <= EMA200 + buffer VÀ deviation <= 1.5%
  EMA_RESISTANCE_TOUCH : close <= EMA200 VÀ candle_high >= EMA200 - buffer VÀ deviation <= 1.5%
```

#### S6 / S7 — RSI Divergence (`check_rsi_divergence`)
```
Chỉ chạy khi: tf == '15m'
Cần ít nhất 14 bars lịch sử cho cả close_history và rsi_history
So sánh giữa hai Pivot swing đáy/đỉnh đã đóng/xác nhận hoàn toàn trong lịch sử
RSI threshold: chênh lệch RSI phải >= 3.0 điểm

BULLISH: price(Pivot 2) < price(Pivot 1) VÀ rsi(Pivot 2) > rsi(Pivot 1) + 3.0
BEARISH: price(Pivot 2) > price(Pivot 1) VÀ rsi(Pivot 2) < rsi(Pivot 1) - 3.0
(Tín hiệu chỉ phát khi Pivot 2 vừa được xác nhận ở nến đóng gần nhất - tránh repaint)
```

#### S8 / S9 — Confluence (`check_confluence` + `check_confluence_cross_tf`)
```
CONFLUENCE_LONG:
  → EMA_SUPPORT_TOUCH (bất kỳ TF) + RSI_OVERSOLD (15m, trong 15 phút)

CONFLUENCE_SHORT:
  → EMA_RESISTANCE_TOUCH (bất kỳ TF) + RSI_OVERBOUGHT (15m, trong 15 phút)

Cross-TF: RSI extreme từ snapshot 15m trước đó, EMA touch từ snapshot TF bất kỳ sau đó (giới hạn 15 phút)
```

### 1.3 — Cơ chế Anti-Spam (Alerts & Signals)

| Lớp | Vị trí | Key | Thời gian |
|-----|--------|-----|-----------|
| SignalCooldownManager | `core/signals.py` | `{symbol}_{type}_{tf}` | **300 giây (5 phút)** |
| TelegramNotifier rate limit | `notifiers/telegram_bot.py` | `{symbol}_{type}_{tf}` | **60 giây** (configurable `TELEGRAM_RATE_LIMIT`) |

> Hai lớp hoạt động độc lập — nếu signal pass lớp 1 vẫn có thể bị chặn ở lớp 2.

### 1.4 — Format thông báo Telegram

```
🚨 HIGH PRIORITY / 🔔 ALERT / ⚠️ WARNING  BTC — RSI_OVERSOLD

📊 Timeframe: 15m
💰 Gia: $65,432.00
📈 RSI 14: 23.45
〰️ EMA 200: $64,100.00
📦 Volume: 1234.5678

📝 RSI 15m = 23.45 (oversold threshold: 25)
🕐 14:30:00 UTC
📊 Mo Dashboard  [link]
```

---

## Phần 2 — Altcoin Volume Scanner (`VolumeSniper/`)

Theo dõi **Top 500 altcoin USDT Perp trên Binance Futures** real-time qua WebSocket kline stream. Tất cả xử lý trong RAM.

### 2.1 — Pipeline tổng quan

```
Binance WS kline stream (1m)
    ↓
VolumeDetector._check_spike()          ← Tầng 1: Volume + Price filter
    ↓ [spike detected]
Scanner._on_spike()                    ← Chạy Analyzer sớm xác định analysis_direction
    ↓
[Tầng 2: Confirm candle (chờ nến kế)]   ← Check PnL >= 0.3% theo analysis_direction
    ↓ [pnl >= 0.3% confirm]
[Tầng 3: BB Squeeze filter]            ← Check BB Width pre-spike
    ↓
AlertBatcher                           ← Tầng 4: Gom 3 giây chống spam
    ↓
Telegram (ảnh chart kèm theo)
```

### 2.2 — Tầng 1: Spike Detection Filters

| Filter | Điều kiện | Ngưỡng mặc định | Config key |
|--------|-----------|-----------------|------------|
| Candle closed | Chỉ check khi nến đóng | `True` | `VS_REQUIRE_CLOSED_CANDLE` |
| Price change min | `\|close - open\| / open` | ≥ **0.8%** | `VS_PRICE_CHANGE_MIN_PCT` |
| Min liquidity | `avg_volume_usdt` | ≥ **$10,000 USDT/nến** | `VS_MIN_AVG_VOL_USDT` |
| Volume spike ratio | `volume / weighted_avg_20` | ≥ **5.0x** | `VS_VOLUME_SPIKE_RATIO` |
| Cooldown per symbol | Thời gian giữa 2 spike cùng coin | **300 giây** | `VS_ALERT_COOLDOWN_SEC` |
| Delta price max | Không alert nếu delta quá lớn (fomo) | ≤ **2.0%** | `VS_DELTA_PRICE_MAX_PCT` |

> **Weighted avg volume**: Dùng weighted moving average (nến gần nhất có weight cao hơn) + P10 floor để tránh bị kéo xuống bởi nến đêm volume thấp.

### 2.3 — Tầng 2: Confirm Candle Filter

```
Sau khi detect spike → Chạy Analyzer sớm để xác định direction thực tế (analysis_direction) và lưu vào _pending_spikes[symbol]

Khi nến KẾ TIẾP đóng:
  Tính PnL dựa trên analysis_direction thay vì hướng spike gốc:
    LONG:  pnl = (close_kế - spike_price) / spike_price * 100
    SHORT: pnl = (spike_price - close_kế) / spike_price * 100

  → Alert nếu: pnl >= 0.3%  (VS_CONFIRM_CANDLE_MIN_PCT)
  → Discard nếu: pnl < 0.3% hoặc chờ > 2 candles (timeout)

Tác động backtesting (460 signals):
  Precision trước filter: ~30%
  Precision sau filter:   57-68%
  80% BAD signals bị loại (pnl_5m < 0)
```

### 2.4 — Tầng 3: Bollinger Bands Squeeze Filter

```
BB Width = (4 × std_dev) / SMA × 100   (tính trên 20 nến gần nhất)

Chỉ alert nếu: BB_WIDTH <= 5.0%   (VS_BB_WIDTH_MAX_PCT)
→ True = giá đang nén (tích lũy) → breakout thật
→ False = giá đã bung, spike này là continuation noise

Có thể tắt: VS_BB_SQUEEZE_ENABLED=false
```

### 2.5 — Phân tích Signal (3-Zone Framework)

Sau khi qua 3 tầng lọc, `MarketAnalyzer` xác định direction + confidence:

#### Signal Direction Output

| Signal | Điều kiện | Zone | Confidence |
|--------|-----------|------|-----------|
| `LONG` TREND ⭐ | RSI 50–82, không wick rejection, giá tăng | TREND | **HIGH** (nếu EMA Neutral) / MEDIUM |
| `LONG` RISKY | RSI > 82 (overbought nhưng chưa reject) | RISKY | MEDIUM |
| `SHORT` TREND | RSI 18–50, không wick rejection, giá giảm | TREND | **HIGH** (nếu EMA Uptrend) / MEDIUM |
| `SHORT` RISKY | RSI < 18 (oversold nhưng chưa reject) | RISKY | MEDIUM |
| `SHORT` REVERSAL 🔄 | LONG spike nhưng RSI ≥ 82 + wick trên > 45% **HOẶC** spike ≥ 15x | REVERSAL | HIGH |
| `LONG` REVERSAL 🔄 | SHORT spike nhưng RSI ≤ 18 + wick dưới > 45% **HOẶC** spike ≥ 15x | REVERSAL | HIGH |
| `SKIP` | RSI < 50 (LONG weak) hoặc RSI > 50 (SHORT weak) hoặc wick rejection | RISKY | LOW |

#### Zone Semantics

| Zone | Ý nghĩa | Hành động gợi ý |
|------|---------|-----------------|
| **TREND** | Momentum rõ ràng, RSI hỗ trợ | Theo chiều spike |
| **REVERSAL** | Exhaustion / Climax volume → đảo chiều | Vào lệnh ngược chiều spike |
| **RISKY** | Momentum yếu hoặc RSI cực đoan | Thận trọng, size nhỏ |
| **SIDEWAYS** | (Legacy, hiện không dùng trong 3-zone) | — |

#### RSI Thresholds (VolumeSniper)

```
RSI_OVERBOUGHT  = 82   (VS_RSI_OVERBOUGHT)   — cao hơn core (75) vì spike context
RSI_OVERSOLD    = 18   (VS_RSI_OVERSOLD)      — thấp hơn core (25)
RSI_TREND_LIMIT = 50   (VS_RSI_TREND_LIMIT)   — ngưỡng tối thiểu cho LONG
WICK_REJECT_PCT = 0.45 (VS_WICK_REJECT_PCT)   — wick > 45% → reversal / skip
```

### 2.6 — Tầng 4: Alert Batching

```
Cửa sổ gom: 3 giây  (VS_ALERT_BATCH_WINDOW_SEC)
Giới hạn:   10 symbols/message  (VS_ALERT_BATCH_MAX)

1 spike trong 3s  → gửi message CHI TIẾT + ảnh chart candlestick
2+ spike trong 3s → gửi 1 SUMMARY message (không kèm ảnh để tránh spam)

Tắt batching: VS_ALERT_BATCH_WINDOW_SEC=0 → gửi ngay từng alert
```

### 2.7 — Format thông báo Telegram (VolumeSniper)

```
🟢🔥 SOLUSDT LONG (TREND)

💰 Giá: 145.23 🔺 +1.2%
📊 Volume Spike: 7.3x
📈 Trend: UPTREND ⬆️
🌀 RSI: 64.5
📍 Zone: TREND 📈
✅ Tích lũy: BBW 3.2%

📝 Phân tích:
 • ✅ TREND: RSI=64 (EMA↑)

💡 Gợi ý Trading:
📥 Entry: 145.23
🎯 TP: 147.41 (+1.5%)
🛑 SL: 143.78 (-1.0%)

⏰ 14:32:10 | ⚠️ Tự đặt lệnh!
[kèm ảnh candlestick chart 50 nến]
```

### 2.8 — Quản lý người nhận (VolumeSniper)

```
Gửi đến: TẤT CẢ authorized_users (set, không trùng)
User mới: gõ /start → nút "Đăng ký" → Admin nhận thông báo → Duyệt/Từ chối
Lưu tại: authorized_users.json
Admin: ADMIN_CHAT_ID (mặc định = TELEGRAM_CHAT_ID)
```

---

## Phần 3 — So sánh tổng hợp

### 3.1 — Điểm khác biệt cốt lõi

| Tiêu chí | Alerts & Signals | VolumeSniper |
|----------|-----------------|--------------|
| **Phạm vi** | Vài symbol cấu hình | Top **500** altcoin USDT Perp |
| **Nguồn data** | ccxt (Binance / nhiều sàn) | Binance Futures WS trực tiếp |
| **Timeframe** | 1m, 5m, 15m, 1h, 4h đồng thời | 1 interval (`VS_KLINE_INTERVAL=1m`) |
| **Loại signal** | RSI, EMA touch, Divergence, Confluence | Volume spike + direction analysis |
| **Ngưỡng spike** | 2.0x avg (đơn giản) | **5.0x** weighted avg + 4 filter |
| **Confirm candle** | ❌ Không | ✅ Có (tăng precision 30%→68%) |
| **Ảnh chart** | ❌ Không | ✅ Có (candlestick 50 nến) |
| **TP/SL gợi ý** | ❌ Không | ✅ Có (+1.5% TP / -1.0% SL) |
| **Gửi đến** | 1 TELEGRAM_CHAT_ID | Nhiều authorized users |
| **Multi-user auth** | ❌ Không | ✅ Có (approve/reject flow) |

### 3.2 — Khi nào mỗi hệ thống thông báo?

```
Alerts & Signals gửi khi:
  1. RSI 15m vượt ngưỡng (>75 hoặc <25)
  2. Volume 1h bằng 2x trung bình 24h
  3. Giá chạm EMA200 trên bất kỳ TF nào (lệch ≤ 1.5%, đệm 0.1%)
  4. Xuất hiện RSI divergence trên 15m
  5. Confluence: EMA touch + RSI extreme cùng lúc (HIGH PRIORITY)
  → Sau đó chặn 5 phút (cooldown) cho cùng signal type

VolumeSniper gửi khi:
  1. Volume nến đóng ≥ 5x baseline weighted avg
  2. VÀ biến động giá 0.8% – 2.0%
  3. VÀ avg liquidity ≥ $10,000 USDT/nến
  4. VÀ nến KẾ TIẾP xác nhận PnL ≥ 0.3%
  5. VÀ BBW ≤ 5% (giá đang nén)
  → Sau đó chặn 300 giây cho cùng symbol
```

### 3.3 — Signal Priority Matrix

| Priority | Signal | Ý nghĩa thực tế |
|----------|--------|-----------------|
| 🚨 P1 | `CONFLUENCE_LONG/SHORT` | EMA + RSI cùng lúc → vào lệnh |
| 🔥 P2 | VolumeSniper `REVERSAL` HIGH | Climax volume đảo chiều → cơ hội cao |
| ⚡ P3 | VolumeSniper `TREND` HIGH (NEUTRAL EMA) | Breakout từ tích lũy → theo trend |
| 🔔 P4 | VolumeSniper `TREND` MEDIUM | Spike trend thông thường |
| 🔔 P5 | `EMA_SUPPORT/RESISTANCE_TOUCH` | Structure signal đơn |
| 🔔 P6 | `BULLISH/BEARISH_DIVERGENCE` | Early warning |
| ⚠️ P7 | `RSI_OVERBOUGHT/OVERSOLD` | Warning đơn thuần |
| ⚠️ P8 | VolumeSniper `RISKY` MEDIUM | Cần thêm xác nhận |
| ⚪ P9 | `VOLUME_SPIKE` (core, 2x) | Tín hiệu yếu, thường bị che bởi VolumeSniper |

---

## Phần 4 — Cấu hình thay đổi được (`.env`)

### Alerts & Signals

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `RSI_OVERBOUGHT` | `75` | Ngưỡng RSI overbought |
| `RSI_OVERSOLD` | `25` | Ngưỡng RSI oversold |
| `VOLUME_SPIKE_MULTIPLIER` | `2.0` | Bội số spike volume (1h) |
| `MIN_VOLUME_THRESHOLD_USDT` | `50000.0` | Volume trung bình tối thiểu (USDT) để check spike |
| `SIGNAL_TOLERANCE_PCT` | `1.5` | Giới hạn khoảng cách đóng cửa tối đa tới EMA cho EMA touch (%) |
| `VS_TOUCH_BUFFER_PCT` | `0.1` | Vùng đệm chạm EMA200 (%) |
| `CONFLUENCE_MAX_AGE_SEC` | `900` | Thời gian hiệu lực tối đa của RSI extreme cho Confluence (giây) |
| `TELEGRAM_RATE_LIMIT` | `60` | Rate limit Telegram (giây) |

### VolumeSniper

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `VS_VOLUME_SPIKE_RATIO` | `5.0` | Ngưỡng spike tối thiểu |
| `VS_PRICE_CHANGE_MIN_PCT` | `0.8` | Biến động giá tối thiểu (%) |
| `VS_DELTA_PRICE_MAX_PCT` | `2.0` | Biến động giá tối đa (%) |
| `VS_CONFIRM_CANDLE_MIN_PCT` | `0.3` | PnL tối thiểu để confirm (%) |
| `VS_BB_SQUEEZE_ENABLED` | `true` | Bật/tắt BB Squeeze filter |
| `VS_BB_WIDTH_MAX_PCT` | `5.0` | BBW tối đa để xem là tích lũy (%) |
| `VS_ALERT_COOLDOWN_SEC` | `300` | Cooldown giữa 2 spike cùng coin (giây) |
| `VS_ALERT_BATCH_WINDOW_SEC` | `3` | Cửa sổ gom alert (giây) |
| `VS_MAX_SYMBOLS` | `500` | Số coin tối đa theo dõi |
| `VS_MIN_AVG_VOL_USDT` | `10000` | Avg USDT vol tối thiểu/nến |
| `VS_KLINE_INTERVAL` | `1m` | Timeframe nến |
| `VS_DEFAULT_TP_PCT` | `1.5` | % TP mặc định gợi ý |
| `VS_DEFAULT_SL_PCT` | `1.0` | % SL mặc định gợi ý |
