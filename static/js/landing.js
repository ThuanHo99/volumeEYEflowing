/**
 * BTC AI Signal Tracker - Landing Page Interactive Script
 * Connects directly to public Binance WebSocket to stream ticker and trades in the browser.
 */

(function () {
  const priceVal = document.getElementById('live-price-val');
  const pricePct = document.getElementById('live-price-pct');
  const terminal = document.getElementById('live-trade-logs');
  const canvas = document.getElementById('live-sparkline');
  
  if (!priceVal || !pricePct || !terminal || !canvas) {
    console.warn('[Landing] Missing DOM elements for live widget');
    return;
  }
  
  const ctx = canvas.getContext('2d');
  let prices = [];
  const MAX_PRICE_HISTORY = 40;
  let lastPrice = null;
  let ws = null;
  let reconnectTimeout = null;

  // TRANSLATIONS DICTIONARY
  const TRANSLATIONS = {
    vi: {
      'nav-features': 'Tính năng',
      'nav-workflow': 'Quy trình',
      'nav-tech': 'Công nghệ',
      'btn-launch-dashboard': 'Mở Dashboard',
      'hero-eyebrow': 'ĐỘNG CƠ THỜI GIAN THỰC PRO',
      'hero-title': 'Trí tuệ phi tập trung. Hành động tức thì.',
      'hero-subtitle': 'Theo dõi các bất thường về khối lượng giao dịch tổ chức và các chỉ báo thị trường dựa trên AI của hơn 500 altcoin theo thời gian thực với độ phân tích chính xác cao.',
      'widget-live-feed': 'Dữ liệu trực tiếp: BTC/USDT',
      'widget-connecting': 'Đang kết nối...',
      'widget-price-label': 'Giá trực tiếp (USD)',
      'widget-logs-title': 'DÒNG LỆNH THỜI GIAN THỰC',
      'widget-init-conn': 'Đang khởi tạo kết nối an toàn đến luồng dữ liệu...',
      'features-header': 'Thiết kế cho Phân tích Tốc độ cao',
      'features-subheader': 'Không dùng bảng điều khiển chung chung. Trải nghiệm hệ thống phân tích có cấu trúc tối ưu cho hiệu quả thị trường.',
      'bento-sniper-title': 'Volume Sniper Altcoin',
      'bento-sniper-desc': 'Quét liên tục hơn 500 cặp giao dịch giao ngay và tương lai để phát hiện các đợt bùng nổ khối lượng của các tổ chức. Phân loại bất thường theo thời gian thực vào các phân vùng cảnh báo Xu hướng, Đảo chiều và Rủi ro.',
      'zone-trend': 'XU HƯỚNG',
      'zone-reversal': 'ĐẢO CHIỀU',
      'zone-risky': 'RỦI RO',
      'bento-confluence-title': 'Hệ thống Hợp lưu',
      'bento-confluence-desc': 'Tham chiếu chéo các trạng thái quá mua/quá bán RSI, điểm chạm đường EMA200 và xu hướng phân kỳ RSI trên các khung thời gian 15m, 1h, và 4h để lọc ra các thiết lập giao dịch có xác suất thắng cao.',
      'ind-rsi-oversold': 'RSI Quá bán',
      'ind-ema-support': 'Hỗ trợ EMA200',
      'ind-long-trigger': 'Tín hiệu LONG',
      'bento-sentiment-title': 'Chỉ số Tâm lý AI',
      'bento-sentiment-desc': 'Giám sát nguồn cấp tin tức, hoạt động của nhà phát triển và khối lượng dòng lệnh để cung cấp hệ số tâm lý thị trường theo thời gian thực.',
      'ind-bullish': 'TĂNG GIÁ',
      'bento-telegram-title': 'Độ trễ dưới 1 giây',
      'bento-telegram-desc': 'Đường truyền thông báo tức thì trực tiếp đến các kênh Telegram của bạn. Nội dung tín hiệu có cấu trúc bao gồm các mức giá kích hoạt chính xác, chỉ báo kỹ thuật và liên kết biểu đồ tĩnh.',
      'tg-sig-long': '🔔 TÍN HIỆU LONG',
      'tg-detail-lbl': 'Chi tiết:',
      'tg-detail-val': 'Giá chạm đường hỗ trợ EMA200 khung 15m',
      'workflow-header': 'Quy trình Phía sau Hệ thống',
      'workflow-subheader': 'Từ việc thu thập dữ liệu đến khi gửi thông báo, quy trình được tối ưu hóa cho độ trễ nhỏ nhất.',
      'work-step1-title': 'Thu thập thời gian thực',
      'work-step1-desc': 'Nguồn cấp WebSocket liên tục xử lý giá và dữ liệu nến trực tiếp từ các cổng sàn giao dịch chính.',
      'work-step2-title': 'Xử lý trạng thái',
      'work-step2-desc': 'Động cơ Python quản lý các bộ đệm trạng thái cô lập và tính toán lại các chỉ báo kỹ thuật trên mỗi tick giá.',
      'work-step3-title': 'Phát hiện bất thường',
      'work-step3-desc': 'Các mô hình chấm điểm toán học lọc nhiễu và theo dõi sự đột biến biến động bên trong Altcoin Volume Sniper.',
      'work-step4-title': 'Phân phối hiệu quả',
      'work-step4-desc': 'Các luồng phân phối song song phát sóng dữ liệu đến Web UI và Telegram cùng một lúc.',
      'cta-title': 'Sẵn sàng tham gia vào thị trường?',
      'cta-desc': 'Truy cập động cơ giám sát thời gian thực và cấu trúc các quy tắc cảnh báo tự động của bạn.',
      'btn-enter-app': 'Truy cập Ứng dụng',
      'tech-logos-title': 'CÔNG NGHỆ & TÍCH HỢP HỆ THỐNG',
      'showcase-header': 'Thiết bị Đa Nền tảng',
      'showcase-subheader': 'Theo dõi các bất thường về khối lượng trên bất kỳ thiết bị nào. Giao diện bảng điều khiển tương thích hoàn toàn với máy tính và điện thoại.',
      'footer-copy': '© 2026 VolHunter. Bảo lưu mọi quyền.',
      'footer-status': 'Hệ thống hoạt động (v2.4.0)'
    },
    en: {
      'nav-features': 'Features',
      'nav-workflow': 'Workflow',
      'nav-tech': 'Technology',
      'btn-launch-dashboard': 'Launch Dashboard',
      'hero-eyebrow': 'PRO REAL-TIME ENGINE',
      'hero-title': 'Decentralized Intelligence. Immediate Action.',
      'hero-subtitle': 'Track institutional volume anomalies and AI-driven market indicators across 500+ altcoins in real time with high-fidelity analytics.',
      'widget-live-feed': 'Live Feed: BTC/USDT',
      'widget-connecting': 'Connecting...',
      'widget-price-label': 'Live Price (USD)',
      'widget-logs-title': 'REAL-TIME ORDER STREAM',
      'widget-init-conn': 'Initializing secure connection to stream...',
      'features-header': 'Built for High-Frequency Analysis',
      'features-subheader': 'No generic dashboards. Experience structured analytics engineered for market efficiency.',
      'bento-sniper-title': 'Altcoin Volume Sniper',
      'bento-sniper-desc': 'Scan 500+ spot and futures symbols continuously for institutional volume spikes. Classify anomalies in real time across custom Trend, Reversal, and Risky alert zones.',
      'zone-trend': 'TREND',
      'zone-reversal': 'REVERSAL',
      'zone-risky': 'RISKY',
      'bento-confluence-title': 'Confluence Engine',
      'bento-confluence-desc': 'Cross-references RSI extremums, EMA200 touches, and divergence trends across 15m, 1h, and 4h intervals to filter high-probability setups.',
      'ind-rsi-oversold': 'RSI Oversold',
      'ind-ema-support': 'EMA200 Support',
      'ind-long-trigger': 'LONG Trigger',
      'bento-sentiment-title': 'AI Sentiment Index',
      'bento-sentiment-desc': 'Monitors news feeds, developer activity, and order flow metrics to deliver a real-time sentiment coefficient index.',
      'ind-bullish': 'BULLISH',
      'bento-telegram-title': 'Under 1-Sec Latency',
      'bento-telegram-desc': 'Instant notification pipeline directly to your Telegram channels. Structured signal payloads include precise price entry triggers, indicator levels, and static chart links.',
      'tg-sig-long': '🔔 SIGNAL LONG',
      'tg-detail-lbl': 'Detail:',
      'tg-detail-val': 'Price touched 15m EMA200 support line',
      'workflow-header': 'Behind the Scenes',
      'workflow-subheader': 'From ingestion to delivery, the workflow is optimized for minimal latency.',
      'work-step1-title': 'Real-time Ingestion',
      'work-step1-desc': 'Continuous WebSocket feeds parse ticker and candle data directly from primary exchange gateways.',
      'work-step2-title': 'State Processing',
      'work-step2-desc': 'The Python processing engine manages isolated state buffers and recalculates indicators on every tick.',
      'work-step3-title': 'Anomaly Detection',
      'work-step3-desc': 'Mathematical scoring models filter noise and track volatility surges inside Altcoin Volume Sniper.',
      'work-step4-title': 'Actionable Dispatch',
      'work-step4-desc': 'Parallel dispatch pipes broadcast updates to connected Web UI clients and Telegram simultaneously.',
      'cta-title': 'Ready to step into the flow?',
      'cta-desc': 'Access the core real-time monitoring engine and configure your automated notification rules.',
      'btn-enter-app': 'Enter Application',
      'tech-logos-title': 'ENGINE STACK & INTEGRATIONS',
      'showcase-header': 'Cross-Platform Terminal',
      'showcase-subheader': 'Monitor volume anomalies on any device. Fully responsive desktop and mobile dashboards.',
      'footer-copy': '© 2026 VolHunter. All rights reserved.',
      'footer-status': 'Engine Active (v2.4.0)'
    }
  };

  let currentLang = localStorage.getItem('preferred-language') || 'vi';

  function applyLanguage(lang) {
    currentLang = lang;
    localStorage.setItem('preferred-language', lang);
    
    // Update active class on buttons
    document.querySelectorAll('.lang-btn').forEach(btn => {
      if (btn.getAttribute('data-lang') === lang) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    // Translate elements
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      if (TRANSLATIONS[lang] && TRANSLATIONS[lang][key]) {
        el.textContent = TRANSLATIONS[lang][key];
      }
    });
  }

  // Set up language event listeners
  document.querySelectorAll('.lang-btn').forEach(btn => {
    btn.addEventListener('click', function() {
      const selected = this.getAttribute('data-lang');
      applyLanguage(selected);
    });
  });

  // Apply default language at startup
  applyLanguage(currentLang);

  // Set canvas scale based on container size
  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = rect.height * window.devicePixelRatio;
    drawSparkline();
  }

  // Draw smooth sparkline on Canvas
  function drawSparkline() {
    if (prices.length < 2) return;
    
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    
    const minVal = Math.min(...prices);
    const maxVal = Math.max(...prices);
    const range = maxVal - minVal || 1;
    
    // Coordinates
    const points = prices.map((price, idx) => {
      const x = (idx / (prices.length - 1)) * w;
      const y = h - ((price - minVal) / range) * (h * 0.7) - (h * 0.15); // pad 15% top/bottom
      return { x, y };
    });
    
    // Draw neon area gradient
    ctx.beginPath();
    ctx.moveTo(0, h);
    points.forEach(p => ctx.lineTo(p.x, p.y));
    ctx.lineTo(w, h);
    ctx.closePath();
    
    const areaGrad = ctx.createLinearGradient(0, 0, 0, h);
    areaGrad.addColorStop(0, 'rgba(16, 185, 129, 0.2)');
    areaGrad.addColorStop(1, 'rgba(16, 185, 129, 0.0)');
    ctx.fillStyle = areaGrad;
    ctx.fill();

    // Draw glowing line
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
      // Curve smoothing
      const xc = (points[i].x + points[i - 1].x) / 2;
      const yc = (points[i].y + points[i - 1].y) / 2;
      ctx.quadraticCurveTo(points[i - 1].x, points[i - 1].y, xc, yc);
    }
    ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
    
    ctx.strokeStyle = '#10b981';
    ctx.lineWidth = 2.5 * window.devicePixelRatio;
    ctx.shadowColor = 'rgba(16, 185, 129, 0.6)';
    ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.shadowBlur = 0; // reset
  }

  // Add line to simulated terminal
  function addTerminalLine(type, text) {
    const line = document.createElement('div');
    line.className = `terminal-line ${type}`;
    line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
    terminal.appendChild(line);
    
    // Scroll to bottom
    terminal.scrollTop = terminal.scrollHeight;
    
    // Limit log rows
    while (terminal.childElementCount > 15) {
      terminal.removeChild(terminal.firstChild);
    }
  }

  // Connect to Binance Combined Stream
  function connectWebSocket() {
    if (ws) {
      ws.close();
    }
    
    // Combine btcusdt@ticker and btcusdt@trade streams
    const url = 'wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/btcusdt@trade';
    ws = new WebSocket(url);
    
    ws.onopen = () => {
      // Clear placeholder
      terminal.innerHTML = '';
      const text = currentLang === 'vi' 
        ? 'THÀNH CÔNG: Đã kết nối với luồng dữ liệu WebSocket của Binance' 
        : 'SUCCESS: Connected to Binance WebSocket stream';
      addTerminalLine('', text);
    };
    
    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        const stream = payload.stream;
        const data = payload.data;
        
        if (stream === 'btcusdt@ticker') {
          // Handle price tick
          const price = parseFloat(data.c);
          const pct = parseFloat(data.P);
          
          // Formatted price string
          const formattedPrice = price.toLocaleString('en-US', {
            style: 'currency',
            currency: 'USD',
            minimumFractionDigits: 2
          });
          
          // If priceVal is not overwritten by data-i18n on load or we update it dynamically
          priceVal.textContent = formattedPrice;
          
          // Format percent change
          const sign = pct >= 0 ? '+' : '';
          pricePct.textContent = `${sign}${pct.toFixed(2)}%`;
          pricePct.style.color = pct >= 0 ? '#10b981' : '#ef4444';
          
          // Price flash effect
          if (lastPrice !== null) {
            priceVal.classList.remove('up', 'down');
            if (price > lastPrice) {
              priceVal.classList.add('up');
            } else if (price < lastPrice) {
              priceVal.classList.add('down');
            }
          }
          
          lastPrice = price;
          
          // Add to price history & draw sparkline
          prices.push(price);
          if (prices.length > MAX_PRICE_HISTORY) {
            prices.shift();
          }
          drawSparkline();
        }
        
        else if (stream === 'btcusdt@trade') {
          // Handle trade transaction
          const qty = parseFloat(data.q).toFixed(4);
          const price = parseFloat(data.p).toFixed(2);
          const isBuyerMaker = data.m; // true = SELL (Maker), false = BUY (Taker)
          
          const typeClass = isBuyerMaker ? 'trade-sell' : 'trade-buy';
          const typeText = isBuyerMaker 
            ? (currentLang === 'vi' ? 'BÁN' : 'SELL')
            : (currentLang === 'vi' ? 'MUA' : 'BUY');
          const symbolText = `${qty} BTC @ $${parseFloat(price).toLocaleString()}`;
          
          addTerminalLine(typeClass, `${typeText}: ${symbolText}`);
        }
      } catch (err) {
        console.error('[Landing] Stream parsing error', err);
      }
    };
    
    ws.onclose = () => {
      const text = currentLang === 'vi' 
        ? 'MẤT KẾT NỐI: Đang kết nối lại sau 5 giây...' 
        : 'CONNECTION LOST: Reconnecting in 5 seconds...';
      addTerminalLine('placeholder', text);
      // Reconnect after delay
      reconnectTimeout = setTimeout(connectWebSocket, 5000);
    };
    
    ws.onerror = (err) => {
      console.error('[Landing] Stream socket error', err);
      ws.close();
    };
  }

  // Initial scaling & resize listeners
  window.addEventListener('resize', resizeCanvas);
  resizeCanvas();
  
  // Connect immediately
  connectWebSocket();
  
  // Smooth scroll transitions
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
      e.preventDefault();
      const target = document.querySelector(this.getAttribute('href'));
      if (target) {
        target.scrollIntoView({
          behavior: 'smooth',
          block: 'start'
        });
      }
    });
  });
})();
