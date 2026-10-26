/**
 * dashboard.js — Multi-Symbol Signal Tracker Main App
 * ES module: chart init, WebSocket client, UI handlers.
 */
import {
  TIMEFRAMES, MAX_CHART_POINTS, stores as _stores, currentSymbol as storeSymbol,
  processCandle, getCandles, getCount, getStore,
} from './store.js';

/* ---- Color constants — mirror CSS :root variables (Q2) ---- */
const C = {
  BG_PRIMARY: '#08090c',
  BG_TERTIARY: '#141926',
  TEXT_BODY: '#a3aed0',
  TEXT_MUTED: '#6e7b9e',
  ACCENT: '#f0b90b',
  GREEN: '#10b981',
  RED: '#f43f5e',
  PURPLE: '#8b5cf6',
  VOL_DEFAULT: '#2c374e',
  VOL_UP: 'rgba(16, 185, 129, 0.2)',
  VOL_DOWN: 'rgba(244, 63, 94, 0.2)',
};

console.log('[UI] dashboard.js loaded — multi-symbol version');

// Expose stores for debugging
window.__stores = _stores;

// Guard: wait for LightweightCharts (handles CDN fallback race)
if (typeof LightweightCharts === 'undefined') {
  const startupLang = localStorage.getItem('preferred-language') || 'vi';
  const loadingMsg = startupLang === 'vi' ? 'Đang tải thư viện biểu đồ...' : 'Loading chart library...';
  document.getElementById('ft-status-text').textContent = loadingMsg;
  var _lcTimer = setInterval(function () {
    if (typeof LightweightCharts !== 'undefined') {
      clearInterval(_lcTimer);
      _initApp();
    }
  }, 50);
  setTimeout(function () { clearInterval(_lcTimer); }, 15000);
} else {
  _initApp();
}

function _initApp() {
  try {
    let currentTf = '15m';
    let currentSymbol = null;
    let lastPlottedTime = 0;
    window.__currentSymbol = () => currentSymbol;
    window.__currentTf = () => currentTf;
    let initialLoad = true;
    let symbolsList = [];

    // TRANSLATIONS DICTIONARY FOR DASHBOARD
    const TRANSLATIONS = {
      vi: {
        'nav-market': 'Thị trường',
        'nav-alerts': 'Cảnh báo',
        'nav-scanner': 'Quét Volume',
        'dash-alerts-title': 'Cảnh báo & Tín hiệu',
        'dash-alerts-subtitle': 'Luồng tín hiệu giao dịch thời gian thực',
        'dash-filter-symbol': 'Cặp giao dịch:',
        'dash-filter-tf': 'Khung thời gian:',
        'dash-filter-tf-short': 'Khung:',
        'dash-filter-type': 'Loại chỉ báo:',
        'dash-filter-all': 'Tất cả',
        'dash-scanner-title': 'Máy quét Khối lượng Altcoin',
        'dash-scanner-subtitle': 'Theo dõi đột biến volume trên 500+ altcoin thời gian thực',
        'dash-scanner-search-lbl': 'Tìm cặp giao dịch:',
        'dash-scanner-search-placeholder': 'Tìm symbol...',
        'dash-scanner-zone-lbl': 'Phân vùng:',
        'dash-btn-clear': 'Xóa',
        'dash-config-title': 'Cấu hình Tín hiệu',
        'dash-log-title': 'Nhật ký Tín hiệu',
        'loading-text': 'Đang đồng bộ hóa dữ liệu thị trường...',
        'zone-trend': 'Xu hướng',
        'zone-reversal': 'Đảo chiều',
        'zone-risky': 'Rủi ro',
        'zone-breakout': 'Giữ được vùng mới',
        'zone-failure': 'Phá vỡ thất bại',
        'zone-watch': 'Chờ / tranh chấp',
        'vs-plan-search': 'Nhập mã ở trên để xem vùng giá đã lập trước sự kiện.',
        'vs-plan-warmup': 'Chưa đủ nến đóng để lập vùng giá cho mã này.',
        'vs-plan-error': 'Không tải được kế hoạch vùng giá.',
        'vs-plan-upper': 'Cản trên',
        'vs-plan-lower': 'Hỗ trợ dưới',
        'vs-plan-ready': 'Lập lúc',
        'vs-plan-quiet': 'Volume và biên độ đang co lại; chưa suy ra hướng.',
        'vs-plan-active': 'Chờ phản ứng giá tại mép vùng; chưa suy ra hướng.',
        'vs-plan-scenarios': 'Đóng vượt cản rồi retest giữ được: xét lên. Vượt rồi quay lại và không lấy lại: xét xuống. Tại hỗ trợ, áp dụng đối xứng.',
        'vs-plan-pending': 'Đã có nến thử phá vùng; đang chờ retest hoặc thất bại.',
        'vs-plan-invalidations': 'Sai kịch bản: vượt cản lên nhưng mất {upLong}; thất bại ở cản nhưng lấy lại {upShort}. Hỗ trợ: thủng xuống nhưng lấy lại {downShort}; thất bại thủng nhưng mất {downLong}.',
        'vs-chart-title': 'Bản đồ giá v3',
        'vs-chart-research': 'Chỉ nghiên cứu · không phát lệnh',
        'vs-chart-search': 'Nhập mã ở trên để xem bản đồ giá và các kịch bản.',
        'vs-chart-warmup': 'Chưa có kế hoạch v3 cho mã này; đang khởi tạo hoặc ngoài nhóm nghiên cứu.',
        'vs-chart-error': 'Không tải được kế hoạch v3.',
        'vs-chart-price': 'Giá gần nhất',
        'vs-chart-regime': 'Trạng thái',
        'vs-chart-support': 'Hỗ trợ gần nhất',
        'vs-chart-resistance': 'Kháng cự gần nhất',
        'vs-chart-scenarios': 'Kịch bản lập trước',
        'vs-chart-no-zone': 'Chưa xác định được vùng giá',
        'vs-chart-no-scenarios': 'Chưa có kịch bản',
        'vs-chart-reason-structure': 'Chưa đủ nến đa khung đã đóng để lập vùng tin cậy',
        'vs-chart-reason-zone': 'Chưa có vùng hỗ trợ hoặc kháng cự gần giá',
        'vs-chart-trigger': 'Điều kiện',
        'vs-chart-invalidation': 'Sai khi',
        'vs-chart-expiry': 'Hết hạn sau',
        'vs-chart-candles': 'nến',
        'vs-chart-cutoffs': 'Dữ liệu đến',
        'vs-chart-quality': 'Thiếu hoặc cũ',
        'vs-chart-zones': 'Vùng giá đa khung',
        'vs-chart-touches': 'lần chạm',
        'vs-chart-confirmed': 'xác nhận',
        'vs-chart-event': 'Sự kiện nghiên cứu',
        'vs-chart-regime-range': 'đi ngang',
        'vs-chart-regime-trend': 'xu hướng',
        'vs-chart-regime-transition': 'chuyển trạng thái',
        'vs-chart-regime-unclear': 'chưa rõ',
        'vs-chart-up': 'tăng',
        'vs-chart-down': 'giảm',
        'ind-rsi-oversold': 'RSI Quá bán',
        'ind-ema-support': 'Hỗ trợ EMA200',
        'ind-long-trigger': 'Tín hiệu LONG',

        // Statuses
        'status-connecting': 'Đang kết nối...',
        'status-realtime': 'Đã kích hoạt chế độ thời gian thực',
        'status-warmup': 'Đã hoàn thành làm nóng - {count} nến trên {symbols} cặp',
        'status-disconnect-retry': 'Mất kết nối - đang thử lại sau {delay}s (lần {attempt}/{max})',
        'status-disconnected-max': 'Mất kết nối - vượt quá số lần thử lại tối đa',
        'status-rate-limited': 'Mất kết nối — bị giới hạn tần suất. Vui lòng tải lại trang để thử lại.',
        'status-connected-ok': 'Kết nối thành công',
        'status-loading-library': 'Đang tải thư viện biểu đồ...',

        // Toasts
        'toast-websocket-error': 'Lỗi kết nối WebSocket',
        'toast-reconnecting': 'Đang kết nối lại sau {delay}s',
        'toast-ws-connected': 'Đã kết nối WebSocket',
        'toast-rate-limited': 'Bị giới hạn tần suất. Vui lòng tải lại trang để thử lại.',
        'toast-log-cleared': 'Nhật ký đã xóa',
        'toast-volume-spike': '{symbol} Đột biến Khối lượng {ratio}x',
        'toast-signal': '{symbol} {type} [{tf}]',

        // Config panel
        'config-rsi-lbl': 'RSI: Quá bán < {os} | Quá mua > {ob}',
        'config-vol-lbl': 'Khối lượng: Hệ số đột biến = {mult}x',
        'config-ema-lbl': 'EMA: Khung thời gian hoạt động: {tfs}',

        // Dynamic signal types
        'RSI_OVERSOLD': 'RSI Quá bán',
        'RSI_OVERBOUGHT': 'RSI Quá mua',
        'RSI_DIVERGENCE': 'Phân kỳ RSI',
        'EMA_SUPPORT_TOUCH': 'Chạm Hỗ trợ EMA',
        'EMA_RESISTANCE_TOUCH': 'Chạm Kháng cự EMA',
        'VOLUME_SPIKE': 'Đột biến Khối lượng',
        'VOLUME_CONTEXT': 'Volume và phản ứng giá',
        'BULLISH_DIVERGENCE': 'Phân kỳ Tăng giá',
        'BEARISH_DIVERGENCE': 'Phân kỳ Giảm giá',
        'CONFLUENCE_LONG': 'Hợp lưu LONG',
        'CONFLUENCE_SHORT': 'Hợp lưu SHORT',

        // Scanner card details
        'vs-card-price': 'Giá',
        'vs-card-spike': 'Đột biến',
        'vs-card-trend': 'Xu hướng',
        'vs-modal-title-suffix': 'Đột biến Khối lượng ({ratio}x)',
        'vs-modal-analysis-title': 'Chi tiết phân tích',
        'vs-modal-no-warnings': 'Không có cảnh báo',
        'scanner-alerts-count-lbl': 'Hiển thị {visible} sự kiện',
        'LONG': 'LONG',
        'SHORT': 'SHORT',
        'NEUTRAL': 'NEUTRAL'
      },
      en: {
        'nav-market': 'Market',
        'nav-alerts': 'Alerts',
        'nav-scanner': 'Scanner',
        'dash-alerts-title': 'Alerts & Signals',
        'dash-alerts-subtitle': 'Real-time trading signals feed',
        'dash-filter-symbol': 'Symbol:',
        'dash-filter-tf': 'Timeframe:',
        'dash-filter-tf-short': 'TF:',
        'dash-filter-type': 'Type:',
        'dash-filter-all': 'All',
        'dash-scanner-title': 'Altcoin Volume Scanner',
        'dash-scanner-subtitle': 'Real-time volume spikes on 500+ symbols',
        'dash-scanner-search-lbl': 'Symbol Search:',
        'dash-scanner-search-placeholder': 'Type symbol...',
        'dash-scanner-zone-lbl': 'Zone:',
        'dash-btn-clear': 'Clear',
        'dash-config-title': 'Signal Configuration',
        'dash-log-title': 'Signals Log',
        'loading-text': 'Synchronizing market data...',
        'zone-trend': 'Trend',
        'zone-reversal': 'Reversal',
        'zone-risky': 'Risky',
        'zone-breakout': 'Breakout held',
        'zone-failure': 'Breakout failed',
        'zone-watch': 'Watch / contested',
        'vs-plan-search': 'Type a symbol above to see its pre-event range plan.',
        'vs-plan-warmup': 'Not enough closed candles to plan a range for this symbol.',
        'vs-plan-error': 'Could not load the range plan.',
        'vs-plan-upper': 'Upper boundary',
        'vs-plan-lower': 'Lower boundary',
        'vs-plan-ready': 'Planned at',
        'vs-plan-quiet': 'Volume and price range narrowed; direction remains unknown.',
        'vs-plan-active': 'Wait for price response at either boundary; direction remains unknown.',
        'vs-plan-scenarios': 'Close above then hold a retest: examine upside. Break above then fail to reclaim: examine downside. Reverse the logic at support.',
        'vs-plan-pending': 'A boundary test has occurred; waiting for a retest or failure.',
        'vs-plan-invalidations': 'Invalidation: upside breakout loses {upLong}; failed resistance break reclaims {upShort}. At support: downside break reclaims {downShort}; failed downside break loses {downLong}.',
        'vs-chart-title': 'Price map v3',
        'vs-chart-research': 'Research only · no trade action',
        'vs-chart-search': 'Type a symbol above to see its price map and scenarios.',
        'vs-chart-warmup': 'No v3 plan for this symbol yet; warming up or outside the research universe.',
        'vs-chart-error': 'Could not load the v3 plan.',
        'vs-chart-price': 'Latest price',
        'vs-chart-regime': 'Regime',
        'vs-chart-support': 'Nearest support',
        'vs-chart-resistance': 'Nearest resistance',
        'vs-chart-scenarios': 'Prospective scenarios',
        'vs-chart-no-zone': 'No price zone yet',
        'vs-chart-no-scenarios': 'No scenarios yet',
        'vs-chart-reason-structure': 'Not enough closed higher-timeframe candles for a usable map',
        'vs-chart-reason-zone': 'No nearby support or resistance zone',
        'vs-chart-trigger': 'Trigger',
        'vs-chart-invalidation': 'Invalid if',
        'vs-chart-expiry': 'Expires after',
        'vs-chart-candles': 'candles',
        'vs-chart-cutoffs': 'Data through',
        'vs-chart-quality': 'Missing or stale',
        'vs-chart-zones': 'Multi-timeframe zones',
        'vs-chart-touches': 'touches',
        'vs-chart-confirmed': 'confirmed',
        'vs-chart-event': 'Research event',
        'vs-chart-regime-range': 'range',
        'vs-chart-regime-trend': 'trend',
        'vs-chart-regime-transition': 'transition',
        'vs-chart-regime-unclear': 'unclear',
        'vs-chart-up': 'up',
        'vs-chart-down': 'down',
        'ind-rsi-oversold': 'RSI Oversold',
        'ind-ema-support': 'EMA200 Support',
        'ind-long-trigger': 'LONG Trigger',

        // Statuses
        'status-connecting': 'Connecting...',
        'status-realtime': 'Real-time mode active',
        'status-warmup': 'Warmup complete - {count} candles across {symbols} symbols',
        'status-disconnect-retry': 'Disconnected - retrying in {delay}s (attempt {attempt}/{max})',
        'status-disconnected-max': 'Disconnected — max retries exceeded',
        'status-rate-limited': 'Disconnected — rate limited. Refresh page to retry.',
        'status-connected-ok': 'Connected successfully',
        'status-loading-library': 'Loading chart library...',

        // Toasts
        'toast-websocket-error': 'WebSocket connection error',
        'toast-reconnecting': 'Reconnecting in {delay}s',
        'toast-ws-connected': 'WebSocket connected',
        'toast-rate-limited': 'Rate limited. Refresh page to retry.',
        'toast-log-cleared': 'Signal log cleared',
        'toast-volume-spike': '{symbol} Volume Spike {ratio}x',
        'toast-signal': '{symbol} {type} [{tf}]',

        // Config panel
        'config-rsi-lbl': 'RSI: Oversold < {os} | Overbought > {ob}',
        'config-vol-lbl': 'Volume: Spike multiplier = {mult}x',
        'config-ema-lbl': 'EMA: Active TFs: {tfs}',

        // Dynamic signal types
        'RSI_OVERSOLD': 'RSI Oversold',
        'RSI_OVERBOUGHT': 'RSI Overbought',
        'RSI_DIVERGENCE': 'RSI Divergence',
        'EMA_SUPPORT_TOUCH': 'EMA Support Touch',
        'EMA_RESISTANCE_TOUCH': 'EMA Resistance Touch',
        'VOLUME_SPIKE': 'Volume Spike',
        'VOLUME_CONTEXT': 'Volume and price response',
        'BULLISH_DIVERGENCE': 'Bullish Divergence',
        'BEARISH_DIVERGENCE': 'Bearish Divergence',
        'CONFLUENCE_LONG': 'Confluence LONG',
        'CONFLUENCE_SHORT': 'Confluence SHORT',

        // Scanner card details
        'vs-card-price': 'Price',
        'vs-card-spike': 'Spike',
        'vs-card-trend': 'Trend',
        'vs-modal-title-suffix': 'Volume Spike ({ratio}x)',
        'vs-modal-analysis-title': 'Analysis Details',
        'vs-modal-no-warnings': 'No warnings',
        'scanner-alerts-count-lbl': '{visible} event(s) shown',
        'LONG': 'LONG',
        'SHORT': 'SHORT',
        'NEUTRAL': 'NEUTRAL'
      }
    };

    let currentLang = localStorage.getItem('preferred-language') || 'vi';

    function t(key, replacements = {}) {
      let text = TRANSLATIONS[currentLang] && TRANSLATIONS[currentLang][key]
        ? TRANSLATIONS[currentLang][key]
        : key;
      for (const [k, v] of Object.entries(replacements)) {
        text = text.replace(`{${k}}`, v);
      }
      return text;
    }

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem('preferred-language', lang);

      // Translate static DOM elements
      document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (TRANSLATIONS[lang] && TRANSLATIONS[lang][key]) {
          el.textContent = TRANSLATIONS[lang][key];
        }
      });

      // Translate inputs placeholders
      document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (TRANSLATIONS[lang] && TRANSLATIONS[lang][key]) {
          el.placeholder = TRANSLATIONS[lang][key];
        }
      });

      // Update page title
      document.title = currentLang === 'vi' ? 'Theo dõi đa tín hiệu' : 'Multi Signal Tracker';
    }

    /* ---- HTML sanitizer for safe innerHTML usage (prevents XSS) ---- */
    function escapeHtml(str) {
      if (str == null) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    // View name → nav item index mapping
    const VIEW_TO_NAV = { charts: 0, alerts: 1, scanner: 2 };
    const NAV_TO_VIEW = ['charts', 'alerts', 'scanner'];

    /* ---- Chart dimensions ---- */
    function getChartHeight(id) {
      const el = document.getElementById(id);
      return el && el.clientHeight > 0 ? el.clientHeight : (id === 'main-chart' ? 400 : 100);
    }

    /* ---- Main chart (candles + EMA) ---- */
    const mainEl = document.getElementById('main-chart');
    if (!mainEl) throw new Error('main-chart element not found');
    const mainChart = LightweightCharts.createChart(mainEl, {
      layout: {
        background: { color: C.BG_PRIMARY },
        textColor: C.TEXT_BODY,
        fontFamily: "'JetBrains Mono', monospace",
      },
      grid: { vertLines: { color: C.BG_TERTIARY }, horzLines: { color: C.BG_TERTIARY } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { minimumWidth: 80 },
      height: getChartHeight('main-chart'),
    });

    const candleSeries = mainChart.addCandlestickSeries({
      upColor: C.GREEN, downColor: C.RED,
      borderUpColor: C.GREEN, borderDownColor: C.RED,
      wickUpColor: C.GREEN, wickDownColor: C.RED,
    });

    const emaSeries = mainChart.addLineSeries({
      color: C.ACCENT, lineWidth: 2, title: 'EMA 200',
    });

    /* ---- Previous Day Support / Resistance price lines ---- */
    let prevDayHighLine = null;
    let prevDayLowLine = null;
    const dayLevelsBySymbol = {};
    window.dayLevelsBySymbol = dayLevelsBySymbol;

    function updateDayLevels(high, low, _startTs, _symbol) {
      // Store per-symbol for switchSymbol restore (always, regardless of currentSymbol)
      if (_symbol) {
        dayLevelsBySymbol[_symbol] = { high: high, low: low, startTs: _startTs };
      }
      // Only create price lines for the currently displayed symbol
      if (_symbol && _symbol !== currentSymbol) return;

      if (prevDayHighLine) { candleSeries.removePriceLine(prevDayHighLine); prevDayHighLine = null; }
      if (prevDayLowLine) { candleSeries.removePriceLine(prevDayLowLine); prevDayLowLine = null; }

      prevDayHighLine = candleSeries.createPriceLine({
        price: high, color: C.RED, lineWidth: 2, lineStyle: 0,
        axisLabelVisible: true, title: 'H',
      });
      prevDayLowLine = candleSeries.createPriceLine({
        price: low, color: C.GREEN, lineWidth: 2, lineStyle: 0,
        axisLabelVisible: true, title: 'L',
      });

      if (typeof ChartTools !== 'undefined' && ChartTools.onDayLevelsUpdated) {
        ChartTools.onDayLevelsUpdated(prevDayHighLine, prevDayLowLine);
      }
    }

    /* ---- RSI chart ---- */
    const rsiEl = document.getElementById('rsi-chart');
    if (!rsiEl) throw new Error('rsi-chart element not found');
    const rsiChart = LightweightCharts.createChart(rsiEl, {
      layout: {
        background: { color: C.BG_PRIMARY },
        textColor: C.TEXT_MUTED,
        fontFamily: "'JetBrains Mono', monospace",
      },
      grid: { vertLines: { color: C.BG_TERTIARY }, horzLines: { color: C.BG_TERTIARY } },
      timeScale: { timeVisible: true },
      rightPriceScale: { minimumWidth: 80 },
      height: getChartHeight('rsi-chart'),
    });
    const rsiSeries = rsiChart.addLineSeries({ color: C.PURPLE, lineWidth: 1, title: 'RSI 14' });
    rsiSeries.createPriceLine({ price: 70, color: C.RED, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '70' });
    rsiSeries.createPriceLine({ price: 30, color: C.GREEN, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '30' });

    const rsiDummySeries = rsiChart.addLineSeries({
      color: 'rgba(0, 0, 0, 0)',
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => null,
    });
    window.__rsiSeries = rsiSeries;
    window.__rsiDummySeries = rsiDummySeries;

    /* ---- Volume chart ---- */
    const volEl = document.getElementById('vol-chart');
    if (!volEl) throw new Error('vol-chart element not found');
    const volChart = LightweightCharts.createChart(volEl, {
      layout: {
        background: { color: C.BG_PRIMARY },
        textColor: C.TEXT_MUTED,
        fontFamily: "'JetBrains Mono', monospace",
      },
      timeScale: { timeVisible: true },
      rightPriceScale: { minimumWidth: 80 },
      height: getChartHeight('vol-chart'),
    });
    const volSeries = volChart.addHistogramSeries({
      color: C.VOL_DEFAULT, priceFormat: { type: 'volume' },
    });

    /* ---- Initialize ChartTools ---- */
    if (typeof ChartTools !== 'undefined') {
      ChartTools.init({
        mainChart: mainChart,
        candleSeries: candleSeries,
        emaSeries: emaSeries,
        rsiChart: rsiChart,
        volChart: volChart,
        mainEl: mainEl,
        rsiEl: rsiEl,
        volEl: volEl,
        prevDayHighLine: prevDayHighLine,
        prevDayLowLine: prevDayLowLine,
        getStore: function () {
          const s = getStore(currentSymbol);
          return s || getStore(currentSymbol);
        },
      });
    }

    /* ---- Sync crosshair + time scale across all 3 charts ---- */
    let _syncing = false;

    function syncTimeScale(sourceChart, targetCharts) {
      if (_syncing) return;
      _syncing = true;
      try {
        const range = sourceChart.timeScale().getVisibleLogicalRange();
        if (range) {
          targetCharts.forEach(function (target) {
            target.timeScale().setVisibleLogicalRange(range);
          });
        }
      } finally {
        _syncing = false;
      }
    }

    mainChart.timeScale().subscribeVisibleLogicalRangeChange(function (_range) {
      syncTimeScale(mainChart, [rsiChart, volChart]);
    });
    rsiChart.timeScale().subscribeVisibleLogicalRangeChange(function (_range) {
      syncTimeScale(rsiChart, [mainChart, volChart]);
    });
    volChart.timeScale().subscribeVisibleLogicalRangeChange(function (_range) {
      syncTimeScale(volChart, [mainChart, rsiChart]);
    });

    function findValueAtTime(arr, time, key = 'value') {
      if (!arr || arr.length === 0) return null;
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i].time === time) {
          return arr[i][key];
        }
        if (arr[i].time < time) {
          break;
        }
      }
      return null;
    }

    let _syncingCrosshair = false;
    function syncCrosshairs(sourceChart, param, targetChartsConfigs) {
      if (_syncingCrosshair) return;
      _syncingCrosshair = true;
      try {
        const time = param ? param.time : null;
        const point = param ? param.point : null;

        if (!time || !point) {
          targetChartsConfigs.forEach(cfg => {
            cfg.chart.clearCrosshairPosition();
          });
          return;
        }

        const store = getStore(currentSymbol, currentTf);
        if (!store) return;

        targetChartsConfigs.forEach(cfg => {
          let val = null;
          if (cfg.seriesType === 'candle') {
            val = findValueAtTime(store.candles, time, 'close');
          } else if (cfg.seriesType === 'rsi') {
            val = findValueAtTime(store.rsi, time, 'value');
          } else if (cfg.seriesType === 'volume') {
            val = findValueAtTime(store.vols, time, 'value');
          }

          if (val !== null) {
            cfg.chart.setCrosshairPosition(val, time, cfg.series);
          } else {
            cfg.chart.clearCrosshairPosition();
          }
        });
      } finally {
        _syncingCrosshair = false;
      }
    }

    mainChart.subscribeCrosshairMove(function (param) {
      syncCrosshairs(mainChart, param, [
        { chart: rsiChart, series: rsiSeries, seriesType: 'rsi' },
        { chart: volChart, series: volSeries, seriesType: 'volume' }
      ]);
    });

    rsiChart.subscribeCrosshairMove(function (param) {
      syncCrosshairs(rsiChart, param, [
        { chart: mainChart, series: candleSeries, seriesType: 'candle' },
        { chart: volChart, series: volSeries, seriesType: 'volume' }
      ]);
    });

    volChart.subscribeCrosshairMove(function (param) {
      syncCrosshairs(volChart, param, [
        { chart: mainChart, series: candleSeries, seriesType: 'candle' },
        { chart: rsiChart, series: rsiSeries, seriesType: 'rsi' }
      ]);
    });

    /* ---- Signal markers ---- */
    const markers = {};
    let pendingReplaySignals = [];
    function addMarker(signal) {
      const sym = signal.symbol || currentSymbol;
      if (!markers[sym]) markers[sym] = [];
      const ts = Math.floor(signal.timestamp / 1000);
      const sigType = signal.signal_type || signal.type;
      const isLong = ['RSI_OVERSOLD', 'EMA_SUPPORT_TOUCH', 'BULLISH_DIVERGENCE', 'CONFLUENCE_LONG'].includes(sigType);
      markers[sym].push({
        time: ts,
        position: isLong ? 'belowBar' : 'aboveBar',
        color: isLong ? C.GREEN : C.RED,
        shape: isLong ? 'arrowUp' : 'arrowDown',
        text: sigType.replace(/_/g, ' '),
      });
      if (markers[sym].length > 200) markers[sym].shift();
      if (sym === currentSymbol) {
        candleSeries.setMarkers(markers[sym]);
      }
    }

    function rebuildMarkersForSymbol(sym) {
      const ms = markers[sym] || [];
      candleSeries.setMarkers(ms);
    }

    /* ---- Symbol selector ---- */
    function populateSymbolSelect(symbols) {
      var sel = document.getElementById('symbol-selector');
      if (!sel) return;
      // Clone to strip accumulated event listeners (reconnection safety)
      sel.replaceWith(sel.cloneNode(false));
      sel = document.getElementById('symbol-selector');
      sel.innerHTML = '';
      symbolsList = symbols;
      for (let i = 0; i < symbols.length; i++) {
        const opt = document.createElement('option');
        const sym = symbols[i];
        opt.value = sym;
        opt.textContent = sym.replace('/USDT:USDT', '').replace('/USDT', '');
        sel.appendChild(opt);
      }
      sel.value = currentSymbol;
      sel.addEventListener('change', function (e) {
        if (e.target.value) switchSymbol(e.target.value);
      });
    }

    function switchSymbol(symbol) {
      if (symbol === currentSymbol) return;
      currentSymbol = symbol;
      console.log('[UI] Switching to symbol:', symbol);

      // Sync dropdown UI
      const sel = document.getElementById('symbol-selector');
      if (sel) sel.value = symbol;

      // Reload chart data for this symbol
      loadTimeframe(currentTf);

      // Rebuild markers for this symbol
      rebuildMarkersForSymbol(symbol);

      // Switch ChartTools drawings to this symbol
      if (typeof ChartTools !== 'undefined' && ChartTools.setSymbol) {
        ChartTools.setSymbol(symbol);
      }

      // Restore S/R lines for this symbol
      const dl = dayLevelsBySymbol[symbol];
      if (dl) {
        updateDayLevels(dl.high, dl.low, dl.startTs, symbol);
      } else {
        // Clear S/R lines if no data for this symbol
        if (prevDayHighLine) { candleSeries.removePriceLine(prevDayHighLine); prevDayHighLine = null; }
        if (prevDayLowLine) { candleSeries.removePriceLine(prevDayLowLine); prevDayLowLine = null; }
      }

      // Update signal logs to show only this symbol's signals
      filterSignalsBySymbol(symbol);

      savePrefs();
    }

    function filterSignalsBySymbol(symbol) {
      if (!symbol) return;
      const symLabel = symbol.replace('/USDT:USDT', '').replace('/USDT', '');
      // Filter signal log
      const log = document.getElementById('signal-log');
      if (log) {
        for (let i = 0; i < log.children.length; i++) {
          const item = log.children[i];
          if (item.className.indexOf('signal-item') === -1) continue;
          const itemSym = item.dataset.symbol || '';
          item.style.display = itemSym === symbol ? '' : 'none';
        }
      }
      // Filter alerts view
      const fullLog = document.getElementById('signal-log-full');
      if (fullLog) {
        for (let i = 0; i < fullLog.children.length; i++) {
          const item = fullLog.children[i];
          if (item.className.indexOf('signal-item') === -1) continue;
          const itemSym = item.dataset.symbol || '';
          item.style.display = itemSym === symbol ? '' : 'none';
        }
      }
    }

    /* ---- Timeframe switching ---- */
    function loadTimeframe(tf) {
      currentTf = tf;
      const store = getStore(currentSymbol);
      if (!store || !store[tf]) return;
      const data = store[tf];
      const count = data.candles.length;
      document.getElementById('tf-candle-count').textContent = count + ' candles';

      candleSeries.setData(data.candles.slice(-MAX_CHART_POINTS));
      emaSeries.setData(data.ema.slice(-MAX_CHART_POINTS));
      rsiSeries.setData(data.rsi.slice(-MAX_CHART_POINTS));
      rsiDummySeries.setData(data.candles.slice(-MAX_CHART_POINTS).map(c => ({ time: c.time, value: 50 })));
      volSeries.setData(data.vols.slice(-MAX_CHART_POINTS));

      const lastCandle = data.candles[data.candles.length - 1];
      lastPlottedTime = lastCandle ? lastCandle.time : 0;

      // Scroll chart to the latest candle (real-time position)
      if (data.candles.length > 0) {
        mainChart.timeScale().scrollToRealTime();
      }
    }

    /* ---- Apply candle to live chart (called after processCandle during live feed) ---- */
    function applyLiveCandle(msg, bar) {
      if (initialLoad) return;
      if (msg.tf !== currentTf || msg.symbol !== currentSymbol) return;
      if (bar.time < lastPlottedTime) return;
      lastPlottedTime = Math.max(lastPlottedTime, bar.time);

      candleSeries.update(bar);
      if (msg.ema200 != null) emaSeries.update({ time: bar.time, value: msg.ema200 });
      if (msg.rsi14 != null) rsiSeries.update({ time: bar.time, value: msg.rsi14 });
      rsiDummySeries.update({ time: bar.time, value: 50 });
      volSeries.update({
        time: bar.time,
        value: msg.volume,
        color: msg.close >= msg.open ? C.VOL_UP : C.VOL_DOWN,
      });
      const store = getStore(currentSymbol, msg.tf);
      const count = store ? store.candles.length : 0;
      document.getElementById('tf-candle-count').textContent = count + ' candles';
    }

    /* ---- WebSocket client ---- */
    let ws;
    let reconnectTimer;
    let heartbeatTimer;
    let lastMsgTime = 0;
    let reconnectAttempt = 0;
    const RECONNECT_BASE_MS = 2000;
    const RECONNECT_MAX_MS = 30000;
    const RECONNECT_MAX_ATTEMPTS = 20;

    function updateHeartbeat() {
      const el = document.getElementById('ft-heartbeat');
      const timeEl = document.getElementById('ft-time');
      if (timeEl) {
        const now = new Date();
        timeEl.textContent = now.toISOString().substring(11, 16) + ' UTC';
      }
      if (!el) return;
      const ago = lastMsgTime ? Math.floor((Date.now() - lastMsgTime) / 1000) : 999;
      const cnt = getCount();
      const display = cnt > 999999 ? (cnt / 1000).toFixed(0) + 'k' : cnt;
      el.textContent = 'Data: ' + (lastMsgTime ? ago + 's ago' : 'never') + ' | Candles: ' + display;
    }

    function connect() {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      var wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      console.log('[UI] Connecting WebSocket via ' + wsProto + '//' + location.host + '/ws ...');
      ws = new WebSocket(wsProto + '//' + location.host + '/ws');

      ws.onopen = function () {
        console.log('[UI] WebSocket connected');
        reconnectAttempt = 0;
        const dot = document.getElementById('ft-dot');
        if (dot) dot.classList.add('connected');
        document.getElementById('ft-status-text').textContent = t('status-connected-ok');
        showToast(t('toast-ws-connected'), 'success', 4000);
      };

      ws.onclose = function (event) {
        console.log('[UI] WebSocket disconnected', event.code);
        if (event.code === 1008) {
          document.getElementById('ft-status-text').textContent = t('status-rate-limited');
          showToast(t('toast-rate-limited'), 'error', 12000);
          return;
        }
        const dot = document.getElementById('ft-dot');
        if (dot) dot.classList.remove('connected');
        reconnectAttempt++;
        if (reconnectAttempt > RECONNECT_MAX_ATTEMPTS) {
          document.getElementById('ft-status-text').textContent = t('status-disconnected-max');
          showToast(t('toast-reconnect-max'), 'error', 8000);
          return;
        }
        let delay = Math.min(RECONNECT_BASE_MS * Math.pow(2, reconnectAttempt - 1), RECONNECT_MAX_MS);
        delay = Math.floor(delay * (0.8 + Math.random() * 0.4));
        document.getElementById('ft-status-text').textContent =
          t('status-disconnect-retry', {
            delay: (delay / 1000).toFixed(1),
            attempt: reconnectAttempt,
            max: RECONNECT_MAX_ATTEMPTS
          });
        showToast(t('toast-reconnecting', { delay: (delay / 1000).toFixed(1) }), 'warning', 5000);
        reconnectTimer = setTimeout(connect, delay);
      };

      ws.onerror = function (e) {
        console.error('[UI] WebSocket error', e);
        showToast(t('toast-websocket-error'), 'error', 5000);
      };

      ws.onmessage = function (event) {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch (e) {
          console.error('[UI] Failed to parse WS message:', e, 'raw:', event.data.substring(0, 200));
          return;
        }
        lastMsgTime = Date.now();

        if (msg.type === 'candle') {
          const result = processCandle(msg);
          applyLiveCandle(msg, result.bar);
        }

        if (msg.type === 'replay_bulk') {
          const candles = msg.candles || [];
          for (let i = 0; i < candles.length; i++) {
            processCandle(candles[i]);
          }
        }

        if (msg.type === 'replay_signals') {
          const signals = msg.signals || [];
          pendingReplaySignals = [];

          const log = document.getElementById('signal-log');
          const fullLog = document.getElementById('signal-log-full');
          const vsLog = document.getElementById('vs-signal-log-full');
          if (log) log.innerHTML = '';
          if (fullLog) fullLog.innerHTML = '';
          if (vsLog) vsLog.innerHTML = '';

          for (let i = 0; i < signals.length; i++) {
            const s = signals[i];
            if (s.type === 'volume_sniper_signal') {
              addVolumeSniperSignal(s);
            } else {
              pendingReplaySignals.push(s);
              if (!log) continue;
              const sym = s.symbol || '';
              const item = document.createElement('div');
              item.className = 'signal-item ' + (s.level || '');
              item.dataset.tf = s.tf || 'unknown';
              item.dataset.type = s.signal_type || s.type || 'unknown';
              item.dataset.symbol = sym;
              const dt = new Date(s.timestamp);
              const symLabel = sym.replace('/USDT:USDT', '').replace('/USDT', '');
              const translatedType = t(s.signal_type || s.type || '');
              item.textContent = dt.toLocaleTimeString() + ' - ' + symLabel + ' ' + translatedType + ' [' + s.tf + ']';
              item.style.display = sym === currentSymbol ? '' : 'none';
              log.insertBefore(item, log.firstChild);
            }
          }
          if (log) {
            while (log.children.length > 100) log.removeChild(log.lastChild);
          }

          if (fullLog && log) {
            fullLog.innerHTML = log.innerHTML;
            applyAlertsViewFilters();
          }
        }

        if (msg.type === 'replay_context_watches') {
          for (const watch of msg.watches || []) addVolumeSniperSignal(watch);
        }

        if (msg.type === 'volume_sniper_signal') {
          addVolumeSniperSignal(msg);
          const symLabel = (msg.symbol || '').replace('/USDT:USDT', '').replace('/USDT', '');
          showToast(t('toast-volume-spike', { symbol: symLabel, ratio: msg.spike_ratio ? msg.spike_ratio.toFixed(1) : '' }), 'signal', 6000);
        }

        if (msg.type === 'volume_context_watch') {
          addVolumeSniperSignal(msg);
        }

        if (msg.type === 'chart_v3_event' && msg.research_only === true) {
          // V3 observations refresh the research plan only. They are not alerts.
          document.dispatchEvent(new CustomEvent('chart-v3-event', { detail: msg }));
        }

        if (msg.type === 'signal') {
          const sym = msg.symbol || '';
          console.log('[UI] Signal:', sym, msg.signal_type, msg.level);
          addMarker(msg);
          const log = document.getElementById('signal-log');
          if (!log) return;
          const item = document.createElement('div');
          item.className = 'signal-item ' + msg.level;
          item.dataset.tf = msg.tf || 'unknown';
          item.dataset.type = msg.signal_type || msg.type || 'unknown';
          item.dataset.symbol = sym;
          const dt = new Date(msg.timestamp);
          const symLabel = sym.replace('/USDT:USDT', '').replace('/USDT', '');
          const translatedType = t(msg.signal_type || msg.type);
          item.textContent = dt.toLocaleTimeString() + ' - ' + symLabel + ' ' + translatedType + ' [' + msg.tf + ']';
          item.style.display = sym === currentSymbol ? '' : 'none';
          log.insertBefore(item, log.firstChild);
          while (log.children.length > 100) log.removeChild(log.lastChild);

          const fullLog = document.getElementById('signal-log-full');
          if (fullLog) {
            const fullItem = item.cloneNode(true);
            fullItem.style.display = sym === currentSymbol ? '' : 'none';
            fullLog.insertBefore(fullItem, fullLog.firstChild);
            while (fullLog.children.length > 200) fullLog.removeChild(fullLog.lastChild);
            applyAlertsViewFilters();
          }

          showToast(t('toast-signal', { symbol: symLabel, type: translatedType, tf: msg.tf }), 'signal', 6000);
        }

        if (msg.type === 'day_levels') {
          updateDayLevels(msg.high, msg.low, msg.start_ts, msg.symbol);
        }

        if (msg.type === 'warmup_done') {
          // Warmup completed — reload chart data from store
          // Guard: config may not have arrived yet (currentSymbol still null)
          if (currentSymbol) {
            loadTimeframe(currentTf);
            console.log('[UI] Warmup done, chart reloaded with candle data');
          }
        }

        if (msg.type === 'status') {
          let statusText = msg.text;
          if (statusText === 'Real-time mode active') {
            statusText = t('status-realtime');
          } else if (statusText && statusText.startsWith('Warmup complete')) {
            const parts = statusText.match(/Warmup complete - (\d+) candles across (\d+) symbols/);
            if (parts) {
              statusText = t('status-warmup', { count: parts[1], symbols: parts[2] });
            }
          }
          document.getElementById('ft-status-text').textContent = statusText;
        }

        if (msg.type === 'config') {
          window.__lastConfigMsg = msg; // Cache config message for language switch refresh
          document.getElementById('config-rsi').textContent = t('config-rsi-lbl', {
            os: msg.rsi_oversold,
            ob: msg.rsi_overbought
          });
          document.getElementById('config-volume').textContent = t('config-vol-lbl', {
            mult: msg.volume_multiplier
          });
          document.getElementById('config-ema').textContent = t('config-ema-lbl', {
            tfs: (msg.ema_touch_timeframes || []).join(', ')
          });

          // Init symbol selector from config symbols list
          if (msg.symbols && msg.symbols.length > 0) {
            initSymbolSelector(msg.symbols);
            populateSymbolFilter(msg.symbols);
            populateSymbolFilterAlerts(msg.symbols);
            // Restore saved symbol preference
            if (prefs.currentSymbol && msg.symbols.indexOf(prefs.currentSymbol) !== -1) {
              switchSymbol(prefs.currentSymbol);
            }
          }
        }

        if (msg.type === 'ready') {
          loadTimeframe(currentTf);

          for (let i = 0; i < pendingReplaySignals.length; i++) {
            addMarker(pendingReplaySignals[i]);
          }
          pendingReplaySignals = [];

          initialLoad = false;
          const overlay = document.getElementById('loading-overlay');
          if (overlay) {
            overlay.classList.add('fade-out');
            setTimeout(function () { overlay.remove(); }, 500);
          }
        }
      };
    }

    /* ---- Symbol selector init from config message ---- */
    // The config message now includes symbols list from the server
    // We'll detect symbols from replay signals or a dedicated symbols message
    function initSymbolSelector(symbols) {
      if (!symbols || symbols.length === 0) return;
      currentSymbol = symbols[0];
      populateSymbolSelect(symbols);
      // Chart render happens in 'ready' handler after config+replay_bulk complete
    }

    /* ---- Timeframe selector ---- */
    document.getElementById('tf-selector').addEventListener('change', function (e) {
      loadTimeframe(e.target.value);
      savePrefs();
    });

    /* ---- Expand/collapse right panel ---- */
    const btnExpand = document.getElementById('btn-expand-panel');
    const rightPanel = document.getElementById('right-panel');

    if (btnExpand) btnExpand.addEventListener('click', function () {
      if (rightPanel.classList.contains('open')) {
        closeRightPanel();
      } else {
        openRightPanel();
      }
    });

    /* ---- Signal log filters ---- */
    const tfFilter = document.getElementById('tf-filter');
    const typeFilter = document.getElementById('type-filter');
    const symbolFilter = document.getElementById('symbol-filter');
    const signalLog = document.getElementById('signal-log');

    function applyFilters() {
      const tfVal = tfFilter ? tfFilter.value : 'ALL';
      const typeVal = typeFilter ? typeFilter.value : 'ALL';
      const symVal = symbolFilter ? symbolFilter.value : 'ALL';
      const children = signalLog.children;
      for (let i = 0; i < children.length; i++) {
        const item = children[i];
        if (item.className.indexOf('signal-item') === -1) continue;
        const matchTf = tfVal === 'ALL' || item.dataset.tf === tfVal;
        const matchType = typeVal === 'ALL' || item.dataset.type === typeVal;
        const itemSym = item.dataset.symbol || '';
        const matchSym = symVal === 'ALL' || (itemSym && itemSym === symVal);
        item.style.display = (matchTf && matchType && matchSym) ? '' : 'none';
      }
    }

    if (tfFilter) tfFilter.addEventListener('change', function () { applyFilters(); savePrefs(); });
    if (typeFilter) typeFilter.addEventListener('change', function () { applyFilters(); savePrefs(); });
    if (symbolFilter) symbolFilter.addEventListener('change', function () { applyFilters(); savePrefs(); });

    /* ---- Populate symbol filter from symbols list ---- */
    function populateSymbolFilter(symbols) {
      const sel = document.getElementById('symbol-filter');
      if (!sel) return;
      sel.innerHTML = '';
      const allOpt = document.createElement('option');
      allOpt.value = 'ALL';
      allOpt.textContent = 'All';
      sel.appendChild(allOpt);
      for (let i = 0; i < symbols.length; i++) {
        const opt = document.createElement('option');
        opt.value = symbols[i];
        opt.textContent = symbols[i].replace('/USDT:USDT', '').replace('/USDT', '');
        sel.appendChild(opt);
      }
    }

    function populateSymbolFilterAlerts(symbols) {
      var alertsFilter = document.getElementById('symbol-filter-alerts');
      if (!alertsFilter) return;
      // Clone to strip accumulated listeners before re-populating
      alertsFilter.replaceWith(alertsFilter.cloneNode(false));
      var sel = document.getElementById('symbol-filter-alerts');
      sel.innerHTML = '';
      var allOpt = document.createElement('option');
      allOpt.value = 'ALL';
      allOpt.textContent = 'All';
      sel.appendChild(allOpt);
      for (let i = 0; i < symbols.length; i++) {
        const opt = document.createElement('option');
        opt.value = symbols[i];
        opt.textContent = symbols[i].replace('/USDT:USDT', '').replace('/USDT', '');
        sel.appendChild(opt);
      }
      sel.addEventListener('change', function () { applyAlertsViewFilters(); });
    }

    /* ---- Start ---- */
    connect();
    heartbeatTimer = setInterval(updateHeartbeat, 5000);

    /* ---- Toolbar toggle ---- */
    const vtoolbarToggle = document.getElementById('btn-toggle-vtoolbar');
    if (vtoolbarToggle) {
      vtoolbarToggle.addEventListener('click', function () {
        const vt = document.getElementById('vtoolbar-container');
        if (vt) {
          const isHidden = vt.style.display === 'none';
          vt.style.display = isHidden ? '' : 'none';
          this.innerHTML = isHidden ? '<i class="ph ph-caret-left"></i>' : '<i class="ph ph-caret-right"></i>';
          this.title = isHidden ? 'Hide toolbar' : 'Show toolbar';
          setTimeout(resizeAllCharts, 100);
        }
      });
    }

    /* ---- Clear signals button ---- */
    document.getElementById('btn-clear-signals')?.addEventListener('click', function () {
      var log = document.getElementById('signal-log');
      var fullLog = document.getElementById('signal-log-full');
      if (log) log.innerHTML = '';
      if (fullLog) fullLog.innerHTML = '';
      showToast(t('toast-log-cleared'), 'info', 2000);
    });



    /* ---- Periodic refresh for active views + vol profile ---- */
    setInterval(function () {
      if (document.hidden) return;
      if (currentView === 'charts' && typeof ChartTools !== 'undefined' && ChartTools.refreshVolumeProfile) {
        ChartTools.refreshVolumeProfile();
        return;
      }
      if (currentView === 'alerts') { refreshAlertsView(); return; }
    }, 10000);

    /* ---- Responsive resize ---- */
    function resizeAllCharts() {
      const w = mainEl.clientWidth;
      if (w > 0) {
        mainChart.applyOptions({ width: w, height: getChartHeight('main-chart') });
        rsiChart.applyOptions({ width: rsiEl.clientWidth, height: getChartHeight('rsi-chart') });
        volChart.applyOptions({ width: volEl.clientWidth, height: getChartHeight('vol-chart') });
      }
    }
    window.addEventListener('resize', resizeAllCharts);
    setTimeout(resizeAllCharts, 300);

    /* ---- Page unload cleanup ---- */
    window.addEventListener('beforeunload', function () {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close(1000, 'Page unload');
      }
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (heartbeatTimer) clearInterval(heartbeatTimer);
    });

    /* ---- Collapsible sections ---- */
    const headings = document.querySelectorAll('.section-heading');
    for (let i = 0; i < headings.length; i++) {
      headings[i].addEventListener('click', function () {
        const targetId = this.getAttribute('data-target');
        const body = document.getElementById(targetId);
        if (!body) return;
        const isCollapsed = this.classList.toggle('collapsed');
        body.style.display = isCollapsed ? 'none' : '';
        savePrefs();
      });
    }

    /* ---- View switching ---- */
    let currentView = 'charts';

    function switchView(viewName, _pushState) {
      if (currentView === viewName) return;
      currentView = viewName;

      if (location.hash !== '#' + viewName) {
        history.replaceState(null, '', '#' + viewName);
      }
      savePrefs();

      const iconBtns = document.querySelectorAll('#icon-sidebar .is-btn[data-view]');
      for (let i = 0; i < iconBtns.length; i++) {
        iconBtns[i].classList.toggle('active', iconBtns[i].getAttribute('data-view') === viewName);
      }

      const navItems = document.querySelectorAll('#top-nav .nav-item');
      const navIdx = VIEW_TO_NAV[viewName];
      for (let i = 0; i < navItems.length; i++) {
        navItems[i].classList.toggle('active', i === navIdx);
      }

      const panels = document.querySelectorAll('.view-panel');
      for (let j = 0; j < panels.length; j++) {
        panels[j].style.display = 'none';
        panels[j].classList.remove('active');
      }

      const chartContainer = document.getElementById('chart-container');
      const viewAlerts = document.getElementById('view-alerts');
      const viewScanner = document.getElementById('view-scanner');

      switch (viewName) {
        case 'charts':
          if (chartContainer) { chartContainer.style.display = ''; chartContainer.classList.add('active'); }
          closeRightPanel();
          resizeOnLayout(chartContainer);
          break;
        case 'alerts':
          if (viewAlerts) { viewAlerts.style.display = ''; viewAlerts.classList.add('active'); }
          closeRightPanel();
          resizeOnLayout(viewAlerts);
          refreshAlertsView();
          break;
        case 'scanner':
          if (viewScanner) { viewScanner.style.display = ''; viewScanner.classList.add('active'); }
          closeRightPanel();
          resizeOnLayout(viewScanner);
          break;
        default:
          break;
      }
      const vtoolbar = document.getElementById('vtoolbar-container');
      if (vtoolbar) {
        vtoolbar.style.display = (viewName === 'charts') ? '' : 'none';
      }
    }

    function resizeOnLayout(panelEl) {
      if (panelEl && window.ResizeObserver) {
        const ro = new ResizeObserver(function () {
          resizeAllCharts();
          ro.disconnect();
        });
        ro.observe(panelEl);
      } else {
        setTimeout(resizeAllCharts, 350);
      }
    }

    function openRightPanel() {
      if (!rightPanel.classList.contains('open')) {
        rightPanel.classList.add('open');
        btnExpand.classList.add('open');
        btnExpand.innerHTML = '<i class="ph ph-sidebar-simple ph-bold" style="transform: scaleX(-1); display: inline-block;"></i>';
        savePrefs();
        resizeOnLayout(document.getElementById('chart-container'));
      }
    }

    function closeRightPanel() {
      if (rightPanel.classList.contains('open')) {
        rightPanel.classList.remove('open');
        btnExpand.classList.remove('open');
        btnExpand.innerHTML = '<i class="ph ph-sidebar-simple"></i>';
        savePrefs();
        resizeOnLayout(document.getElementById('chart-container'));
      }
    }

    // Sidebar icon click
    const iconBtns = document.querySelectorAll('#icon-sidebar .is-btn[data-view]');
    for (let i = 0; i < iconBtns.length; i++) {
      const btn = iconBtns[i];
      btn.addEventListener('click', function () {
        const view = this.getAttribute('data-view');
        if (view) switchView(view);
      });
    }

    // Top nav click
    const navItems = document.querySelectorAll('#top-nav .nav-item');
    for (let i = 0; i < navItems.length; i++) {
      const view = NAV_TO_VIEW[i];
      navItems[i].addEventListener('click', function () {
        if (view) switchView(view);
      });
    }

    // Restore view from URL hash on load
    window.addEventListener('hashchange', function () {
      const view = location.hash.replace('#', '');
      if (Object.hasOwn(VIEW_TO_NAV, view)) {
        switchView(view);
      }
    });

    /* ---- View refresh helpers ---- */

    function refreshAlertsView() {
      const src = document.getElementById('signal-log');
      const dst = document.getElementById('signal-log-full');
      if (!src || !dst) return;
      dst.innerHTML = src.innerHTML;
      applyAlertsViewFilters();
    }


    function applyAlertsViewFilters() {
      const tfFilterEl = document.getElementById('tf-filter-alerts');
      const typeFilterEl = document.getElementById('type-filter-alerts');
      const symbolFilterEl = document.getElementById('symbol-filter-alerts');
      const log = document.getElementById('signal-log-full');
      if (!log) return;
      const tfVal = tfFilterEl ? tfFilterEl.value : 'ALL';
      const typeVal = typeFilterEl ? typeFilterEl.value : 'ALL';
      const symVal = symbolFilterEl ? symbolFilterEl.value : 'ALL';
      var children = log.children;
      for (var i = 0; i < children.length; i++) {
        var item = children[i];
        if (item.className.indexOf('signal-item') === -1) continue;
        var matchTf = tfVal === 'ALL' || item.dataset.tf === tfVal;
        var matchType = typeVal === 'ALL' || item.dataset.type === typeVal;
        var itemSym = item.dataset.symbol || '';
        var matchSym = symVal === 'ALL' || (itemSym && itemSym === symVal);
        item.style.display = (matchTf && matchType && matchSym) ? '' : 'none';
      }
    }


    /* ---- localStorage persistence ---- */
    const PREFS_KEY = 'btc_tracker_prefs';
    let prefs = {};

    function loadPrefs() {
      try {
        prefs = JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
      } catch (e) {
        prefs = {};
      }
    }

    function savePrefs() {
      prefs.currentTf = currentTf;
      prefs.currentView = currentView;
      prefs.currentSymbol = currentSymbol;
      prefs.panelOpen = rightPanel && rightPanel.classList.contains('open');
      if (tfFilter) prefs.tfFilter = tfFilter.value;
      if (typeFilter) prefs.typeFilter = typeFilter.value;
      if (symbolFilter) prefs.symbolFilter = symbolFilter.value;

      var sections = {};
      var headings = document.querySelectorAll('#right-panel .section-heading');
      for (var i = 0; i < headings.length; i++) {
        if (headings[i].classList.contains('collapsed')) {
          var target = headings[i].getAttribute('data-target');
          if (target) sections[target] = true;
        }
      }
      prefs.collapsedSections = sections;

      try {
        localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      } catch (e) { /* storage full */ }
    }

    function applyPrefs() {
      if (!prefs) return;

      if (prefs.currentTf && ['1m', '5m', '15m', '1h', '4h'].indexOf(prefs.currentTf) !== -1) {
        var tfEl = document.getElementById('tf-selector');
        if (tfEl && tfEl.value !== prefs.currentTf) {
          tfEl.value = prefs.currentTf;
          loadTimeframe(prefs.currentTf);
        }
      }

      if (prefs.currentSymbol && symbolsList.indexOf(prefs.currentSymbol) !== -1) {
        currentSymbol = prefs.currentSymbol;
        var symEl = document.getElementById('symbol-selector');
        if (symEl) symEl.value = currentSymbol;
      }

      if (prefs.panelOpen) openRightPanel();

      if (prefs.tfFilter && tfFilter) tfFilter.value = prefs.tfFilter;
      if (prefs.typeFilter && typeFilter) typeFilter.value = prefs.typeFilter;
      if (prefs.symbolFilter && symbolFilter) symbolFilter.value = prefs.symbolFilter;
      applyFilters();

      var tfSelectorData = document.getElementById('tf-selector-data');
      if (prefs.dataTf && tfSelectorData) tfSelectorData.value = prefs.dataTf;

      if (prefs.collapsedSections) {
        var headings = document.querySelectorAll('#right-panel .section-heading');
        for (var i = 0; i < headings.length; i++) {
          var target = headings[i].getAttribute('data-target');
          if (target && prefs.collapsedSections[target]) {
            headings[i].classList.add('collapsed');
            var body = document.getElementById(target);
            if (body) body.style.display = 'none';
          }
        }
      }
    }

    /* ---- Toast notification system ---- */
    var toastContainer = null;

    function createToastContainer() {
      if (toastContainer) return;
      toastContainer = document.createElement('div');
      toastContainer.id = 'toast-container';
      document.body.appendChild(toastContainer);
    }

    function showToast(message, type, duration) {
      if (!toastContainer) createToastContainer();
      type = type || 'info';
      duration = duration || 4000;

      var toast = document.createElement('div');
      toast.className = 'toast toast-' + type;
      toast.textContent = message;

      toastContainer.appendChild(toast);
      toast.offsetHeight;
      toast.classList.add('show');

      setTimeout(function () {
        toast.classList.remove('show');
        var removeTimer = setTimeout(function () {
          if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 500);
        toast.addEventListener('transitionend', function () {
          clearTimeout(removeTimer);
          if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, { once: true });
      }, duration);
    }

    /* ---- Events View stub (placeholder) ---- */
    function eventAnalyzeAll() { console.log('[UI] eventAnalyzeAll'); }
    function eventTestAlert() { console.log('[UI] eventTestAlert'); }
    function eventRunAnalysis() { console.log('[UI] eventRunAnalysis'); }

    /* ---- Volume Scanner integration (P3) ---- */
    function derivativesSummary(snapshot) {
      if (!snapshot || !snapshot.metrics) return 'Derivatives: unavailable';
      const m = snapshot.metrics;
      const metric = (key, field, digits = 3) => {
        const item = m[key];
        if (!item || item.status !== 'ok' || !item.value || item.value[field] == null || !Number.isFinite(Number(item.value[field]))) {
          return item ? item.status : 'unavailable';
        }
        const age = Number.isFinite(item.observed_at) ? Math.max(0, Math.round((snapshot.as_of - item.observed_at) / 1000)) : null;
        return Number(item.value[field]).toFixed(digits) + (age == null ? '' : ` (${age}s old)`);
      };
      const lines = [
        `OI (base): ${metric('open_interest', 'base_units', 2)} · OI/market cap approx: ${metric('oi_to_market_cap', 'ratio_approx', 4)}`,
        `Top accounts L/S: ${metric('top_trader_accounts', 'long_short_ratio')} · Top positions L/S: ${metric('top_trader_positions', 'long_short_ratio')} · All accounts L/S: ${metric('long_short_accounts', 'long_short_ratio')}`,
        `Taker buy/sell: ${metric('taker_buy_sell', 'buy_sell_ratio')} · Basis rate: ${metric('basis', 'basis_rate', 5)} · Funding: ${metric('funding', 'latest_funding_rate', 6)}`,
        `Order book top 20 imbalance: ${metric('order_book', 'top20_imbalance')} · Spread (bps): ${metric('order_book', 'spread_bps', 2)}`,
      ];
      const stamp = snapshot.as_of ? new Date(snapshot.as_of).toLocaleTimeString() : '?';
      return `Binance USD-M context (${stamp}):\n${lines.join('\n')}\nValues marked stale/unavailable are not used as evidence.`;
    }

    function addVolumeSniperSignal(msg) {
      const vsLog = document.getElementById('vs-signal-log-full');
      if (!vsLog) return;

      const card = document.createElement('div');
      card.className = `vs-signal-card ${msg.direction || 'NEUTRAL'}`;
      card.dataset.symbol = (msg.symbol || '').replace('/USDT:USDT', '').replace('/USDT', '');
      card.dataset.zone = msg.zone || '';
      card.dataset.direction = msg.direction || '';

      const dt = new Date(msg.timestamp);
      const timeStr = dt.toLocaleTimeString();

      const symLabel = (msg.symbol || '').replace('/USDT:USDT', '').replace('/USDT', '');
      const isWatch = msg.type === 'volume_context_watch';
      const dirText = isWatch ? t('zone-watch') : t(msg.direction || 'NEUTRAL');
      const zoneText = isWatch ? '' : t(msg.zone ? 'zone-' + msg.zone.toLowerCase() : '');
      card.innerHTML = `
    <div class="vs-signal-header">
      <span class="vs-signal-symbol">${escapeHtml(symLabel)}</span>
      <span class="vs-signal-type" style="color: ${msg.direction === 'LONG' ? C.GREEN : (msg.direction === 'SHORT' ? C.RED : C.PURPLE)};">${escapeHtml(dirText)}${isWatch ? '' : ` (${escapeHtml(zoneText)})`}</span>
    </div>
    <div class="vs-signal-info">
      <span>${t('vs-card-price')}: <b>${msg.price}</b></span>
      <span>${msg.baseline ? 'RVOL' : t('vs-card-spike')}: <b>${msg.spike_ratio ? msg.spike_ratio.toFixed(1) : '?'}x</b></span>
      ${isWatch ? `<span>Range: <b>${msg.plan_lower}–${msg.plan_upper}</b></span><span>Direction: <b>unresolved</b></span>` : (msg.baseline ? `<span>Level: <b>${msg.level}</b></span><span>Invalidation: <b>${msg.invalidation}</b></span>` : `<span>RSI: <b>${msg.rsi ? msg.rsi.toFixed(1) : 'N/A'}</b></span><span>${t('vs-card-trend')}: <b>${escapeHtml(msg.ema_trend || 'N/A')}</b></span>`)}
      <span style="margin-left: auto;">${timeStr}</span>
    </div>
  `;

      // On card click: open modal with static chart PNG
      card.addEventListener('click', function () {
        const modal = document.getElementById('chart-modal');
        const modalImg = document.getElementById('chart-modal-img');
        const modalTitle = document.getElementById('chart-modal-title');
        const modalDetails = document.getElementById('chart-modal-details');

        if (modal && modalImg) {
          modalTitle.textContent = `${symLabel} - ${dirText} ${t('vs-modal-title-suffix', { ratio: msg.spike_ratio ? msg.spike_ratio.toFixed(1) : '?' })}`;
          modalImg.style.display = msg.chart_url ? '' : 'none';
          modalImg.src = msg.chart_url || '';

          const reasonsList = (msg.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('');
          const derivativesText = msg.derivatives ? `<p style="white-space:pre-line">${escapeHtml(derivativesSummary(msg.derivatives))}</p>` : '';
          modalDetails.innerHTML = `
        <div style="font-weight: bold; margin-bottom: 4px; color: #f0b90b;">${t('vs-modal-analysis-title')}:</div>
        <ul style="margin: 0; padding-left: 20px;">
          ${reasonsList || `<li>${t('vs-modal-no-warnings')}</li>`}
        </ul>
        ${derivativesText}
      `;
          modal.style.display = 'flex';
        }
      });

      vsLog.insertBefore(card, vsLog.firstChild);
      while (vsLog.children.length > 100) {
        vsLog.removeChild(vsLog.lastChild);
      }

      updateScannerCount();
    }

    function updateScannerCount() {
      const vsLog = document.getElementById('vs-signal-log-full');
      const countEl = document.getElementById('vs-alerts-count');
      if (vsLog && countEl) {
        let visible = 0;
        for (let i = 0; i < vsLog.children.length; i++) {
          if (vsLog.children[i].style.display !== 'none') {
            visible++;
          }
        }
        countEl.textContent = t('scanner-alerts-count-lbl', { visible: visible });
      }
    }

    function initVolumeScanner() {
      const searchInput = document.getElementById('vs-symbol-search');
      const zoneFilter = document.getElementById('vs-zone-filter');
      const clearBtn = document.getElementById('btn-clear-vs-signals');
      const vsLog = document.getElementById('vs-signal-log-full');
      const planPanel = document.getElementById('vs-context-plan');
      const chartPlanBody = document.getElementById('vs-chart-plan-body');
      const chartEvent = document.getElementById('vs-chart-last-event');
      let planTimer = null;

      const chartNumber = value => {
        const n = Number(value);
        return value != null && Number.isFinite(n)
          ? n.toLocaleString(undefined, { maximumFractionDigits: 8 }) : '—';
      };
      const chartTime = value => {
        const n = Number(value);
        return value != null && Number.isFinite(n) && n > 0
          ? new Date(n).toLocaleString() : '—';
      };
      const chartText = value => escapeHtml(value == null ? '—'
        : (typeof value === 'object' ? JSON.stringify(value) : String(value)));
      const normalizedSearch = value => {
        const normalized = String(value || '').trim().toUpperCase()
          .replace('/USDT:USDT', 'USDT').replace('/USDT', 'USDT');
        return normalized && !normalized.endsWith('USDT') ? normalized + 'USDT' : normalized;
      };

      function chartZone(zone) {
        if (!zone || typeof zone !== 'object') return escapeHtml(t('vs-chart-no-zone'));
        return `<b>${chartNumber(zone.low)}–${chartNumber(zone.high)}</b> ` +
          `(${chartText(zone.timeframe)}, ${chartNumber(zone.touches)} ${t('vs-chart-touches')}, ` +
          `${t('vs-chart-confirmed')}: ${chartTime(zone.confirmed_ts)})`;
      }

      function renderChartPlan(plan) {
        if (!chartPlanBody) return;
        const zonesWereOpen = Boolean(chartPlanBody.querySelector('details')?.open);
        const map = plan.market_map || {};
        const regime = t('vs-chart-regime-' + (map.regime || 'unclear'));
        const direction = map.trend_direction ? ` (${t('vs-chart-' + map.trend_direction)})` : '';
        const price = Number(map.price);
        const zones = Array.isArray(map.zones) ? map.zones.slice() : [];
        if (Number.isFinite(price)) {
          zones.sort((a, b) => {
            const centre = z => (Number(z.low) + Number(z.high)) / 2;
            return Math.abs(centre(a) - price) - Math.abs(centre(b) - price);
          });
        }
        const zoneRows = zones.slice(0, 6).map(zone =>
          `<li>${chartText(zone.side)} · ${chartZone(zone)}</li>`).join('');
        const scenarios = Array.isArray(plan.scenarios) ? plan.scenarios : [];
        const scenarioRows = scenarios.map(s =>
          `<li style="margin:3px 0;"><b>${chartText(s.scenario)}</b> · ` +
          `${t('vs-chart-trigger')}: ${chartText(s.trigger)} · ` +
          `${t('vs-chart-invalidation')}: ${chartText(s.invalidation)} · ` +
          `${t('vs-chart-expiry')}: ${chartNumber(s.expiry_candles)} ${t('vs-chart-candles')}</li>`
        ).join('');
        const cutoffs = Object.entries(map.data_cutoffs || {}).map(([tf, ts]) =>
          `${chartText(tf)} ${chartTime(ts)}`).join(' · ');
        const quality = Array.isArray(map.quality_issues) ? map.quality_issues : [];
        const reasonLabels = {
          insufficient_closed_structure: t('vs-chart-reason-structure'),
          no_nearby_structural_zone: t('vs-chart-reason-zone'),
        };
        const decisionReason = plan.reason ? (reasonLabels[plan.reason] || plan.reason) : '';
        chartPlanBody.innerHTML =
          `<div><b>${chartText(plan.symbol)}</b> · ${chartText(plan.decision || 'WAIT')} · ` +
          `${t('vs-chart-price')}: <b>${chartNumber(map.price)}</b> · ` +
          `${t('vs-chart-regime')}: <b>${escapeHtml(regime + direction)}</b> · ` +
          `${chartTime(plan.as_of_ms)}</div>` +
          (decisionReason ? `<div style="color:#f0b90b;">${chartText(decisionReason)}</div>` : '') +
          `<div>${t('vs-chart-support')}: ${chartZone(map.nearest_support)}</div>` +
          `<div>${t('vs-chart-resistance')}: ${chartZone(map.nearest_resistance)}</div>` +
          `<details><summary>${t('vs-chart-zones')} (${zones.length})</summary>` +
          `<ul style="margin:3px 0 5px 16px;padding:0;">${zoneRows || `<li>${t('vs-chart-no-zone')}</li>`}</ul></details>` +
          `<div><b>${t('vs-chart-scenarios')}</b><ul style="margin:3px 0 5px 16px;padding:0;">` +
          `${scenarioRows || `<li>${t('vs-chart-no-scenarios')}</li>`}</ul></div>` +
          `<div style="color:#71819f;">${t('vs-chart-cutoffs')}: ${cutoffs || '—'}</div>` +
          (quality.length ? `<div style="color:#f0b90b;">${t('vs-chart-quality')}: ` +
            `${quality.map(chartText).join(', ')}</div>` : '');
        const details = chartPlanBody.querySelector('details');
        if (details) details.open = zonesWereOpen;
      }

      async function refreshChartPlan() {
        if (!chartPlanBody || !searchInput) return;
        const query = searchInput.value.trim().toUpperCase();
        if (!query) {
          chartPlanBody.textContent = t('vs-chart-search');
          if (chartEvent) chartEvent.textContent = '';
          return;
        }
        try {
          const response = await fetch('/api/chart-plan?symbol=' + encodeURIComponent(query),
            { cache: 'no-store' });
          const plan = await response.json();
          if (query !== searchInput.value.trim().toUpperCase()) return;
          if (!response.ok) {
            chartPlanBody.textContent = response.status === 404
              ? t('vs-chart-warmup') : t('vs-chart-error');
            return;
          }
          renderChartPlan(plan);
        } catch (_) {
          if (query === searchInput.value.trim().toUpperCase()) {
            chartPlanBody.textContent = t('vs-chart-error');
          }
        }
      }

      async function refreshContextPlan() {
        if (!planPanel || !searchInput) return;
        const query = searchInput.value.trim().toUpperCase();
        if (!query) {
          planPanel.textContent = t('vs-plan-search');
          return;
        }
        try {
          const response = await fetch('/api/context-plan?symbol=' + encodeURIComponent(query));
          const plan = await response.json();
          if (query !== searchInput.value.trim().toUpperCase()) return;
          if (!response.ok) {
            planPanel.textContent = response.status === 404 ? t('vs-plan-warmup') : t('vs-plan-error');
            return;
          }
          const price = value => Number(value).toLocaleString(undefined, { maximumFractionDigits: 8 });
          planPanel.innerHTML = `<strong>${escapeHtml(plan.symbol)} — ${escapeHtml(plan.status)}</strong> · ` +
            `${t('vs-plan-lower')}: <b>${price(plan.lower)}</b> · ` +
            `${t('vs-plan-upper')}: <b>${price(plan.upper)}</b> · ` +
            `${t('vs-plan-ready')}: ${new Date(plan.ready_ts).toLocaleTimeString()}<br>` +
            `${plan.status === 'PENDING' ? t('vs-plan-pending') : (plan.quieting ? t('vs-plan-quiet') : t('vs-plan-active'))}<br>` +
            `${t('vs-plan-scenarios')}<br>` +
            `${t('vs-plan-invalidations', {
              upLong: price(plan.upper_breakout_invalidation),
              upShort: price(plan.upper_failure_invalidation),
              downShort: price(plan.lower_breakout_invalidation),
              downLong: price(plan.lower_failure_invalidation)
            })}`;
          try {
            const contextResponse = await fetch('/api/derivatives-context?symbol=' + encodeURIComponent(query));
            const derivatives = await contextResponse.json();
            if (query === searchInput.value.trim().toUpperCase()) {
              planPanel.innerHTML += `<br><span style="white-space:pre-line">${escapeHtml(derivativesSummary(derivatives))}</span>`;
            }
          } catch (_) {
            if (query === searchInput.value.trim().toUpperCase()) {
              planPanel.innerHTML += '<br>Derivatives: unavailable';
            }
          }
        } catch (_) {
          planPanel.textContent = t('vs-plan-error');
        }
      }

      function applyVsFilters() {
        if (!vsLog) return;
        const query = searchInput ? searchInput.value.trim().toUpperCase() : '';
        const zoneVal = zoneFilter ? zoneFilter.value : 'ALL';
        const children = vsLog.children;
        for (let i = 0; i < children.length; i++) {
          const card = children[i];
          const symbol = card.dataset.symbol || '';
          const zone = card.dataset.zone || '';
          const matchQuery = query === '' || symbol.includes(query);
          const matchZone = zoneVal === 'ALL' || zone === zoneVal;
          card.style.display = (matchQuery && matchZone) ? '' : 'none';
        }
        updateScannerCount();
      }

      if (searchInput) {
        searchInput.addEventListener('keyup', applyVsFilters);
        searchInput.addEventListener('input', function () {
          applyVsFilters();
          clearTimeout(planTimer);
          if (chartEvent) chartEvent.textContent = '';
          planTimer = setTimeout(() => {
            refreshContextPlan();
            refreshChartPlan();
          }, 250);
        });
      }
      if (planPanel) {
        planPanel.textContent = t('vs-plan-search');
        setInterval(refreshContextPlan, 15000);
      }
      if (chartPlanBody) {
        chartPlanBody.textContent = t('vs-chart-search');
        setInterval(refreshChartPlan, 15000);
      }
      document.addEventListener('chart-v3-event', function (event) {
        const item = event.detail || {};
        if (!searchInput || !item.research_only ||
            normalizedSearch(searchInput.value) !== normalizedSearch(item.symbol)) return;
        if (chartEvent) {
          chartEvent.textContent = `${t('vs-chart-event')}: ${item.kind || '?'} · ` +
            `${item.event || ''} · ${chartTime(item.timestamp)}`;
        }
        refreshChartPlan();
      });
      if (zoneFilter) zoneFilter.addEventListener('change', applyVsFilters);
      if (clearBtn) {
        clearBtn.addEventListener('click', function () {
          if (vsLog) vsLog.innerHTML = '';
          updateScannerCount();
          showToast(t('toast-log-cleared'), 'info', 2000);
        });
      }

      // Modal close handlers
      const modal = document.getElementById('chart-modal');
      const closeBtn = document.getElementById('chart-modal-close');
      if (closeBtn && modal) {
        closeBtn.addEventListener('click', function () {
          modal.style.display = 'none';
        });
        window.addEventListener('click', function (event) {
          if (event.target === modal) {
            modal.style.display = 'none';
          }
        });
      }
    }

    /* ---- Init ---- */
    initVolumeScanner();
    loadPrefs();
    applyPrefs();

    // Language switcher click listener
    const btnLang = document.getElementById('btn-lang-toggle');
    if (btnLang) {
      btnLang.addEventListener('click', () => {
        const nextLang = currentLang === 'vi' ? 'en' : 'vi';
        applyLanguage(nextLang);

        // Update config panel texts dynamically if we have cached config
        if (window.__lastConfigMsg) {
          document.getElementById('config-rsi').textContent = t('config-rsi-lbl', {
            os: window.__lastConfigMsg.rsi_oversold,
            ob: window.__lastConfigMsg.rsi_overbought
          });
          document.getElementById('config-volume').textContent = t('config-vol-lbl', {
            mult: window.__lastConfigMsg.volume_multiplier
          });
          document.getElementById('config-ema').textContent = t('config-ema-lbl', {
            tfs: (window.__lastConfigMsg.ema_touch_timeframes || []).join(', ')
          });
        }

        // Re-trigger dynamic texts refresh
        updateHeartbeat();
        updateScannerCount();
        const scannerSearch = document.getElementById('vs-symbol-search');
        if (scannerSearch) scannerSearch.dispatchEvent(new Event('input'));

        // Translate existing signals in logs immediately
        document.querySelectorAll('.signal-item').forEach(item => {
          const type = item.dataset.type;
          const tf = item.dataset.tf;
          const sym = item.dataset.symbol || '';
          const symLabel = sym.replace('/USDT:USDT', '').replace('/USDT', '');
          const timePart = item.textContent.split(' - ')[0];
          item.textContent = timePart + ' - ' + symLabel + ' ' + t(type) + ' [' + tf + ']';
        });
      });
    }

    // Initial language application
    applyLanguage(currentLang);

    (function () {
      var hashView = location.hash.replace('#', '');
      if (hashView && Object.hasOwn(VIEW_TO_NAV, hashView)) {
        switchView(hashView);
      } else if (prefs.currentView && Object.hasOwn(VIEW_TO_NAV, prefs.currentView)) {
        switchView(prefs.currentView);
      }
    })();

    console.log('[UI] Charts initialized, waiting for data...');
    window.eventAnalyzeAll = eventAnalyzeAll;
    window.eventTestAlert = eventTestAlert;
    window.eventRunAnalysis = eventRunAnalysis;
  } catch (e) { console.error('[UI] _initApp error:', e); }
} // end _initApp
