# VolHunter — Crypto Signal Tracker

> **Current default (context v2):** VolumeSniper alerts wait for a later closed
> candle to confirm a breakout hold or failure at the prior range boundary.
> The range and invalidation levels are prepared from earlier closed candles;
> search a symbol in the scanner dashboard to see the current WAIT/PENDING plan.
> High volume stuck at a boundary is shown as a directionless WATCH event.
> RVOL uses a matching UTC weekday/hour median when at least two earlier weeks
> are available; until then the alert says `rolling (provisional)`. Taker delta
> is context, not a directional vote. Fixed TP/SL and legacy RSI/EMA alerts are
> off by default. See [codebase map](docs/CODEBASE_MAP.md) for the actual
> runtime flow, settings, data contracts, and validation limits. The older
> signal/filter descriptions below document `VS_STRATEGY_MODE=legacy` and
> `CORE_LEGACY_ALERTS_ENABLED=true`.
> v2 starts in **research mode**: dashboard events and outcome logging run,
> Telegram context alerts stay off until explicitly enabled with
> `VS_CONTEXT_TELEGRAM_ALERTS_ENABLED=true`. The available one-week replay
> did not show a net directional edge; see [validation](docs/V2_VALIDATION.md).

> **Derivatives context:** When a v2 setup or v3 research candidate appears, the scanner reads Binance
> USD-M open interest, long/short positioning, taker buy/sell, perpetual basis,
> funding, and a top-20 order book snapshot. Search a symbol in the dashboard
> to inspect each field's value, age, and availability. The top-trader account
> and position ratios require `BINANCE_API_KEY` under the current Binance API
> documentation; all other fields can run without it. OI/market cap is an
> estimate using the circulating supply returned by Binance. These fields add
> context to the plan and alert; they do not change the confirmation direction
> or establish a profitable strategy. See [data contract](docs/CODEBASE_MAP.md#derivatives-context-contract).

> **Chart reasoning v3 (shadow research):** With 1m scanner candles, v3 is
> enabled by default for at most 32 ranked symbols (`VS_V3_SHADOW_ENABLED=true`,
> `VS_V3_MAX_SYMBOLS=32`). It plans from closed 15m/1h/4h swing zones, watches
> a zone even at normal volume, and records break, failed break, retest,
> invalidation, and NO_TRADE decisions before any later outcome. The dashboard
> symbol search shows its separate read-only panel; `/api/chart-plan?symbol=BTC`
> exposes the current plan. V3 never sends Telegram alerts or places orders.
> Run `python -m scripts.replay_chart_v3 --cost-pct 0.2` for a chronological
> v2/v3 comparison. The [current replay](docs/V3_VALIDATION.md) is negative
> after assumed costs for both versions; see [v3 design and limits](docs/CHART_REASONING_V3.md).

> **Signal-only bot. Không tự động đặt lệnh.**  
> Hệ thống theo dõi thị trường crypto real-time, phát hiện tín hiệu kỹ thuật và volume spike, gửi cảnh báo qua Telegram + Web Dashboard.

---

## Tính năng chính

| Module | Mô tả |
|--------|-------|
| **Alerts & Signals** | Theo dõi các symbol cấu hình (BTC, ETH, SOL...) đa khung thời gian. Phát hiện RSI extremes, EMA200 touch, Volume spike, RSI Divergence, Confluence. |
| **Altcoin Volume Scanner** | Quét real-time top 500 altcoin USDT Perp trên Binance Futures. Phát hiện volume spike ≥ 5x với 4 tầng lọc + gửi kèm ảnh candlestick chart. |
| **Web Dashboard** | Giao diện TradingView-style (Lightweight Charts) với WebSocket real-time, hỗ trợ multi-symbol. |
| **Telegram Alerts** | Gửi thông báo tức thì với đầy đủ chi tiết signal, chart ảnh, gợi ý TP/SL (VolumeSniper). |
| **Landing Page** | Trang giới thiệu hệ thống tại `/`, dashboard tại `/dashboard`. |

---

## Kiến trúc hệ thống

```
Binance / ccxt WebSocket
        │
        ▼
  candle_queue  ──►  consume_candles()  ──►  signal_queue
                            │                       │
                            ▼                       ▼
                      ui_queue            signal_consumer()
                            │                       │
                            ▼                       ▼
                    Web Dashboard           TelegramNotifier
                    (WebSocket broadcast)

VolumeSniper Scanner (parallel, top 500 altcoins)
        │
        ▼
  VolumeDetector → AlertBatcher → TelegramBot (multi-user)
        │
        ▼
    ui_queue  ──►  Web Dashboard (volume_sniper_signal events)
```

**Thiết kế Producer-Consumer** với `asyncio.Queue`. Tất cả tasks được giám sát bởi `TaskSupervisor` — tự restart khi crash.

---

## Cấu trúc thư mục

```
btc-ai-signal-tracker/
│
├── main.py                     # Entry point — khởi động toàn bộ hệ thống
│
├── core/
│   ├── config.py               # Tất cả config đọc từ .env
│   ├── ingestion.py            # ccxt WebSocket streams (OHLCV)
│   ├── state_manager.py        # Quản lý state EMA/RSI/Volume per symbol/TF
│   ├── signals.py              # Logic phát hiện signal (RSI, EMA, Divergence, Confluence)
│   ├── persistence.py          # Cache OHLCV và indicator state ra JSON
│   ├── market_sentiment.py     # Fear & Greed index
│   ├── fear_greed_cache.py     # Cache F&G
│   └── metrics.py              # Prometheus metrics
│
├── VolumeSniper/
│   ├── scanner.py              # WebSocket scanner + Alert pipeline (4 tầng lọc)
│   ├── volume_detector.py      # Phát hiện volume spike + KlineState
│   ├── analyzer.py             # 3-Zone decision (LONG/SHORT/REVERSAL/SKIP)
│   ├── telegram_bot.py         # TelegramBot với multi-user auth
│   ├── chart_generator.py      # Tạo candlestick chart PNG
│   ├── config.py               # Config VolumeSniper (đọc từ .env, prefix VS_)
│   └── utils.py                # Shared utilities
│
├── notifiers/
│   └── telegram_bot.py         # TelegramNotifier cho Alerts & Signals
│
├── ui/
│   └── web_dashboard.py        # aiohttp server + WebSocket broadcast + routing
│
├── templates/
│   ├── index.html              # Dashboard UI (Lightweight Charts)
│   └── landing.html            # Landing page
│
├── static/
│   ├── css/
│   │   ├── dashboard.css
│   │   └── landing.css
│   ├── js/
│   │   ├── dashboard.js
│   │   ├── chart_tools.js
│   │   ├── landing.js
│   │   ├── store.js
│   │   └── lightweight-charts.standalone.production.js
│   ├── images/                 # Logo assets
│   └── charts/                 # Auto-generated chart PNGs (VolumeSniper)
│
├── data/
│   ├── ohlcv_cache.json        # Cache lịch sử OHLCV (auto-generated)
│   ├── indicator_state.json    # Cache trạng thái chỉ báo (auto-generated)
│   └── rate_limits.json        # Rate limit state Telegram
│
├── tests/
│   ├── unit/                   # Unit tests (pytest)
│   └── integration/            # Integration tests
│
├── docs/
│   └── prometheus-config.yml   # Cấu hình Prometheus monitoring
│
├── authorized_users.json       # Danh sách user được phép nhận alert VolumeSniper
├── .env                        # Config bí mật (không commit)
├── .env.example                # Template config
├── requirements.txt
└── pyproject.toml
```

---

## Cài đặt

### Yêu cầu

- Python 3.11+
- pip

### 1. Clone và cài dependencies

```bash
git clone <repo-url>
cd btc-ai-signal-tracker
pip install -r requirements.txt
```

### 2. Tạo file `.env`

```bash
cp .env.example .env
```

Chỉnh sửa `.env` với các giá trị thực:

```env
# === SYMBOLS & EXCHANGE ===
EXCHANGE_ID=binanceusdm
SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT
TIMEFRAMES=1m,5m,15m,1h,4h

# === TELEGRAM ===
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# === WEB DASHBOARD ===
WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_USER=your_username
WEB_PASS=your_password

# === VOLUME SNIPER ===
VS_ENABLED=true
VS_VOLUME_SPIKE_RATIO=5.0
VS_KLINE_INTERVAL=1m
VS_MAX_SYMBOLS=500
ADMIN_CHAT_ID=your_admin_chat_id

# === SIGNAL THRESHOLDS ===
RSI_OVERBOUGHT=75
RSI_OVERSOLD=25
VOLUME_SPIKE_MULTIPLIER=2.0
```

### 3. Chạy hệ thống

```bash
python main.py
```

Mở trình duyệt: `http://127.0.0.1:8080`

---

## Cấu hình chi tiết

### Alerts & Signals (`core/config.py`)

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `SYMBOLS` | `BTC/USDT:USDT` | Danh sách symbol, phân cách bằng dấu phẩy |
| `TIMEFRAMES` | `1m,5m,15m,1h,4h` | Các khung thời gian theo dõi |
| `RSI_OVERBOUGHT` | `75` | Ngưỡng RSI overbought |
| `RSI_OVERSOLD` | `25` | Ngưỡng RSI oversold |
| `VOLUME_SPIKE_MULTIPLIER` | `2.0` | Bội số spike volume (1h) |
| `SIGNAL_TOLERANCE_PCT` | `0.1` | Tolerance giá-EMA200 để trigger EMA touch (%) |
| `TELEGRAM_RATE_LIMIT` | `60` | Cooldown Telegram per signal key (giây) |
| `WEB_PORT` | `8080` | Port web dashboard |

### Altcoin Volume Scanner (`VS_*` prefix)

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `VS_ENABLED` | `true` | Bật/tắt VolumeSniper |
| `VS_VOLUME_SPIKE_RATIO` | `5.0` | Ngưỡng spike tối thiểu (x lần avg) |
| `VS_PRICE_CHANGE_MIN_PCT` | `0.8` | Biến động giá tối thiểu (%) |
| `VS_DELTA_PRICE_MAX_PCT` | `2.0` | Biến động giá tối đa — lọc fomo (%) |
| `VS_CONFIRM_CANDLE_MIN_PCT` | `0.3` | PnL tối thiểu nến xác nhận (%) |
| `VS_BB_SQUEEZE_ENABLED` | `true` | Bật/tắt filter BB Squeeze |
| `VS_BB_WIDTH_MAX_PCT` | `5.0` | BBW tối đa để xem là đang tích lũy (%) |
| `VS_ALERT_COOLDOWN_SEC` | `300` | Cooldown giữa 2 spike cùng coin (giây) |
| `VS_ALERT_BATCH_WINDOW_SEC` | `3` | Cửa sổ gom alert anti-spam (giây) |
| `VS_MAX_SYMBOLS` | `500` | Số altcoin tối đa theo dõi |
| `VS_KLINE_INTERVAL` | `1m` | Timeframe nến VolumeSniper |
| `VS_DEFAULT_TP_PCT` | `1.5` | TP gợi ý (%) |
| `VS_DEFAULT_SL_PCT` | `1.0` | SL gợi ý (%) |
| `ADMIN_CHAT_ID` | (= TELEGRAM_CHAT_ID) | Chat ID nhận notification admin VolumeSniper |

---

## Các loại signal

### Alerts & Signals

| Signal | Timeframe | Điều kiện |
|--------|-----------|-----------|
| `RSI_OVERBOUGHT` | 15m | RSI14 > 75 |
| `RSI_OVERSOLD` | 15m | RSI14 < 25 |
| `VOLUME_SPIKE` | 1h | Volume > 2x avg 24h |
| `EMA_SUPPORT_TOUCH` | 1m–4h | Giá chạm EMA200 từ trên xuống |
| `EMA_RESISTANCE_TOUCH` | 1m–4h | Giá chạm EMA200 từ dưới lên |
| `BULLISH_DIVERGENCE` | 15m | Giá lower low, RSI higher low |
| `BEARISH_DIVERGENCE` | 15m | Giá higher high, RSI lower high |
| `CONFLUENCE_LONG` ⭐ | MTF | EMA Support + RSI Oversold (trong 5 phút) |
| `CONFLUENCE_SHORT` ⭐ | MTF | EMA Resistance + RSI Overbought (trong 5 phút) |

### Altcoin Volume Scanner — Output Signal

| Output | Điều kiện | Confidence |
|--------|-----------|-----------|
| `LONG TREND` | Spike + RSI 50–82 + không wick rejection | HIGH (EMA Neutral) / MEDIUM |
| `SHORT TREND` | Spike + RSI 18–50 + không wick rejection | HIGH (EMA Uptrend) / MEDIUM |
| `LONG REVERSAL` | SHORT spike + RSI ≤ 18 + wick dưới > 45% hoặc ≥ 15x | HIGH |
| `SHORT REVERSAL` | LONG spike + RSI ≥ 82 + wick trên > 45% hoặc ≥ 15x | HIGH |
| `SKIP` | RSI yếu (LONG < 50 / SHORT > 50) hoặc delta > 2% | — |

---

## Web Dashboard

- **`/`** — Landing page (public, không cần auth)
- **`/dashboard`** — Dashboard chính (Basic Auth)
- **`/ws`** — WebSocket endpoint
- **`/health`** — Health check JSON
- **`/metrics`** — Prometheus metrics
- **`/static/...`** — Static assets

**Basic Auth:** Set `WEB_USER` và `WEB_PASS` trong `.env`. Truy cập từ localhost không cần auth.

---

## Quản lý user VolumeSniper (Telegram)

VolumeSniper hỗ trợ multi-user. Người dùng mới phải đăng ký:

1. Gõ `/start` trong Telegram bot
2. Bấm nút **Đăng ký ngay**
3. Admin nhận thông báo → bấm **Duyệt** để cấp quyền

**Lệnh Telegram:**

| Lệnh | Mô tả |
|------|-------|
| `/help` | Xem danh sách lệnh |
| `/status` | Trạng thái và uptime bot |
| `/config` | Cấu hình hiện tại |
| `/stats` | Thống kê số alert đã gửi |
| `/pause` | Tạm dừng alert |
| `/resume` | Tiếp tục alert |
| `/set spike 6.0` | Đổi ngưỡng spike ratio |
| `/set cooldown 600` | Đổi cooldown (giây) |
| `/set bb_squeeze on\|off` | Bật/tắt BB Squeeze filter |
| `/set price_chg 1.0` | Đổi min price change (%) |

---

## Monitoring (Prometheus)

Hệ thống expose metrics tại `/metrics`. Cấu hình Prometheus:

```yaml
# docs/prometheus-config.yml
scrape_configs:
  - job_name: 'volhunter'
    static_configs:
      - targets: ['localhost:8080']
    metrics_path: /metrics
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Async runtime | `asyncio` |
| Exchange data | `ccxt` (REST + WebSocket) |
| Binance Futures WS | `websockets` (VolumeSniper) |
| HTTP server | `aiohttp` |
| Charting | TradingView Lightweight Charts v5 |
| Telegram | `python-telegram-bot` (core) / `aiohttp` (VolumeSniper) |
| Chart image | `matplotlib` / `mplfinance` |
| Math / indicators | `numpy`, `ta-lib` / manual Wilder RSI + EMA |
| Monitoring | `prometheus-client` |
| Structured logging | `structlog` |
| Config | `python-dotenv` |
| Tests | `pytest`, `pytest-asyncio` |

---

## Chạy tests

```bash
pytest tests/unit/
pytest tests/integration/
```

---

## Lưu ý quan trọng

- **Bot tín hiệu, không tự đặt lệnh.** Mọi quyết định giao dịch là của người dùng.
- File `data/*.json` được tạo tự động, không commit lên git.
- File `static/charts/*.png` được tạo tự động bởi VolumeSniper mỗi khi có alert.
- `authorized_users.json` lưu danh sách user được duyệt — backup file này nếu cần.
- Khi deploy server mới, xóa `data/*.json` để tránh cache cũ không phù hợp.
