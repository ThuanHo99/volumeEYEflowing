/**
 * chart_tools.js — Interactive drawing & analysis tools for BTC Signal Tracker
 *
 * Dependencies: LightweightCharts 4.x (global)
 * Usage: Include after chart creation, then call ChartTools.init({...})
 *
 * Exports: window.ChartTools (single intentional global — singleton with .init(), .refreshVolumeProfile(), etc.)
 * Data access: uses cfg.getStore() callback instead of window._tfStore (no global data dependency)
 *
 * @global {object} ChartTools — drawing & analysis toolkit singleton
 */
(function () {
  'use strict';

  // ======================== ChartTools Singleton ========================
  var CT = {
    _mainChart: null,
    _candleSeries: null,
    _emaSeries: null,
    _rsiChart: null,
    _volChart: null,
    _mainEl: null,
    _rsiEl: null,
    _volEl: null,

    // Canvas overlay
    _canvas: null,
    _ctx: null,

    // Tool state
    _activeTool: null, // 'hline' | 'vline' | 'trend' | 'ruler' | 'fibo' | null
    _currentSymbol: null,
    _drawings: {},     // per-symbol: {symbol: [{type, points, priceLines, ...}]}
    _getSymbolDrawings: function () {
      if (!this._currentSymbol) return [];
      if (!this._drawings[this._currentSymbol]) this._drawings[this._currentSymbol] = [];
      return this._drawings[this._currentSymbol];
    },
    _fiboLines: [],    // active fibonacci price lines
    _rulerLabel: null, // DOM element for ruler measurement

    // Drawing transient state
    _isDrawing: false,
    _startPoint: null, // {x, y, time, price}
    _currentPoint: null,

    // Modifier keys
    _shiftKey: false,
    _altKey: false,
    _ctrlKey: false,

    // Volume profile
    _volProfile: { enabled: false, levels: [], lines: [] },
    _volData: [], // accumulated volume by price level

    // Indicator visibility refs
    _prevDayHighLine: null,
    _prevDayLowLine: null,

    // ======================== INIT ========================
    init: function (cfg) {
      this._mainChart = cfg.mainChart;
      this._candleSeries = cfg.candleSeries;
      this._emaSeries = cfg.emaSeries;
      this._rsiChart = cfg.rsiChart;
      this._volChart = cfg.volChart;
      this._mainEl = cfg.mainEl || document.getElementById('main-chart');
      this._rsiEl = cfg.rsiEl || document.getElementById('rsi-chart');
      this._volEl = cfg.volEl || document.getElementById('vol-chart');

      // External refs from the main script (for day levels, etc.)
      this._extPrevDayHighLine = cfg.prevDayHighLine;
      this._extPrevDayLowLine = cfg.prevDayLowLine;
      this._updateDayLevels = cfg.updateDayLevels; // fn(high, low, ts)
      this._getStore = cfg.getStore || function () { return {}; };

      this._createToolbar();
      this._createCanvasOverlay();
      this._createRulerLabel();
      this._bindEvents();
      this._bindKeyboard();
      this._syncCanvasSize();
      console.log('[ChartTools] Initialized — drawing tools ready');
    },

    // ======================== TOOLBAR ========================
    _createToolbar: function () {
      // Prefer vertical toolbar container if it exists (new layout)
      const vtc = document.getElementById('vtoolbar-container');
      const horizontal = !vtc;
      const container = vtc || (function () {
        const tb = document.getElementById('chart-toolbar');
        if (tb) tb.remove();
        return null;
      })();

      const bar = document.createElement('div');
      bar.id = 'chart-toolbar';

      if (horizontal) {
        // Legacy horizontal toolbar (fallback)
        bar.innerHTML =
          '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">' +
          '<span style="font-size:11px;color:#9598a1;margin-right:4px;">Tools:</span>' +
          '<button data-tool="hline" class="ct-btn" title="Horizontal (H)"><i class="ph ph-minus"></i> H-Line</button>' +
          '<button data-tool="vline" class="ct-btn" title="Vertical (V)"><i class="ph ph-line-vertical"></i> V-Line</button>' +
          '<button data-tool="trend" class="ct-btn" title="Trend (T)"><i class="ph ph-trend-up"></i> Trend</button>' +
          '<button data-tool="fibo" class="ct-btn" title="Fibonacci (Alt+Click)"><i class="ph ph-chart-bar"></i> Fib</button>' +
          '<button data-tool="ruler" class="ct-btn" title="ruler (Shift+Drag)"><i class="ph ph-ruler"></i> Ruler</button>' +
          '<span style="color:#2a2e39;margin:0 4px;">|</span>' +
          '<span style="font-size:11px;color:#9598a1;margin-right:4px;">Show/Hide:</span>' +
          '<button data-ind="ema" class="ct-btn ct-toggle on" title="EMA 200"><i class="ph ph-eye"></i> EMA</button>' +
          '<button data-ind="rsi" class="ct-btn ct-toggle on" title="RSI"><i class="ph ph-eye"></i> RSI</button>' +
          '<button data-ind="vol" class="ct-btn ct-toggle on" title="Volume"><i class="ph ph-eye"></i> Vol</button>' +
          '<button data-ind="dayLevels" class="ct-btn ct-toggle on" title="Support/Resistance"><i class="ph ph-eye"></i> S/R</button>' +
          '<button data-ind="volProfile" class="ct-btn ct-toggle" title="Volume Profile"><i class="ph ph-eye-slash"></i> V-Profile</button>' +
          '<span style="color:#2a2e39;margin:0 4px;">|</span>' +
          '<button data-action="clear" class="ct-btn ct-danger" title="Clear all tools"><i class="ph ph-trash"></i> Clear</button>' +
          '</div>';
        const chartContainer = document.getElementById('chart-container');
        const topBar = chartContainer.querySelector('div');
        if (topBar && topBar.nextSibling) {
          chartContainer.insertBefore(bar, topBar.nextSibling);
        } else {
          chartContainer.insertBefore(bar, chartContainer.firstChild);
        }
      } else {
        // Vertical toolbar — inject into #vtoolbar-container
        bar.className = 'vtoolbar';
        bar.innerHTML =
          '<div class="vt-section">' +
          '<div class="vt-section-title">Tools</div>' +
          '<button data-tool="hline" class="ct-btn-v" title="Horizontal (H)"><span class="ct-icon"><i class="ph ph-minus"></i></span><span>H-Line</span></button>' +
          '<button data-tool="vline" class="ct-btn-v" title="Vertical (V)"><span class="ct-icon"><i class="ph ph-line-vertical"></i></span><span>V-Line</span></button>' +
          '<button data-tool="trend" class="ct-btn-v" title="trend (T)"><span class="ct-icon"><i class="ph ph-trend-up"></i></span><span>Trend</span></button>' +
          '<button data-tool="fibo" class="ct-btn-v" title="Fibonacci (Alt+Click)"><span class="ct-icon"><i class="ph ph-chart-bar"></i></span><span>Fib</span></button>' +
          '<button data-tool="ruler" class="ct-btn-v" title="ruler (Shift+Drag)"><span class="ct-icon"><i class="ph ph-ruler"></i></span><span>Ruler</span></button>' +
          '</div>' +
          '<div class="vt-section">' +
          '<div class="vt-section-title">Show/Hide</div>' +
          '<button data-ind="ema" class="ct-btn-v ct-toggle on" title="EMA 200"><span class="ct-icon"><i class="ph ph-eye"></i></span><span>EMA</span></button>' +
          '<button data-ind="rsi" class="ct-btn-v ct-toggle on" title="RSI"><span class="ct-icon"><i class="ph ph-eye"></i></span><span>RSI</span></button>' +
          '<button data-ind="vol" class="ct-btn-v ct-toggle on" title="Volume"><span class="ct-icon"><i class="ph ph-eye"></i></span><span>Vol</span></button>' +
          '<button data-ind="dayLevels" class="ct-btn-v ct-toggle on" title="Support/Resistance"><span class="ct-icon"><i class="ph ph-eye"></i></span><span>S/R</span></button>' +
          '<button data-ind="volProfile" class="ct-btn-v ct-toggle" title="Volume Profile"><span class="ct-icon"><i class="ph ph-eye-slash"></i></span><span>V-Pr</span></button>' +
          '</div>' +
          '<div class="vt-section vt-bottom">' +
          '<button data-action="clear" class="ct-btn-v ct-danger" title="Clear all tools"><span class="ct-icon"><i class="ph ph-trash"></i></span><span>Clear</span></button>' +
          '</div>';
        container.appendChild(bar);
      }

      // Tool button clicks (shared handler)
      const self = this;
      bar.addEventListener('click', function (e) {
        const btn = e.target.closest('button');
        if (!btn) return;

        const tool = btn.dataset.tool;
        if (tool) {
          e.preventDefault();
          if (tool === 'ruler') {
            self._showHint('Shift + Keo chuot de do — % thay doi + so nen');
            return;
          }
          if (tool === 'fibo') {
            self._showHint('Alt + Click diem dau (dinh/day) → Click diem cuoi');
            return;
          }
          self._activateTool(tool, btn);
          return;
        }

        const ind = btn.dataset.ind;
        if (ind) {
          e.preventDefault();
          self._toggleIndicator(ind, btn);
          return;
        }

        const action = btn.dataset.action;
        if (action === 'clear') {
          e.preventDefault();
          self._clearAllDrawings();
        }
      });
    },

    _showHint: function (msg) {
      let el = document.getElementById('ct-hint');
      if (!el) {
        el = document.createElement('div');
        el.id = 'ct-hint';
        el.style.cssText =
          'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);' +
          'background:#1e222d;color:#f0b90b;padding:8px 16px;border-radius:4px;' +
          'font-size:12px;z-index:300;pointer-events:none;transition:opacity 300ms;' +
          'border:1px solid #f0b90b;font-family:monospace;';
        document.body.appendChild(el);
      }
      el.textContent = msg;
      el.style.opacity = '1';
      clearTimeout(this._hintTimer);
      this._hintTimer = setTimeout(function () {
        el.style.opacity = '0';
      }, 2500);
    },

    _activateTool: function (tool, btn) {
      const bar = document.getElementById('chart-toolbar');
      if (this._activeTool === tool) {
        // Deactivate
        this._activeTool = null;
        if (btn) btn.classList.remove('active');
        this._setCanvasActive(false);
        this._showHint('Da huy che do ve');
        return;
      }

      // Deactivate all tool buttons
      const buttons = bar.querySelectorAll('[data-tool]');
      for (let i = 0; i < buttons.length; i++) buttons[i].classList.remove('active');

      this._activeTool = tool;
      if (btn) btn.classList.add('active');
      this._setCanvasActive(true);
      this._isDrawing = false;
      this._startPoint = null;
      this._currentPoint = null;
      this._clearTemp();
    },

    _setCanvasActive: function (active) {
      if (!this._canvas) return;
      this._canvas.style.pointerEvents = active ? 'auto' : 'none';
      this._canvas.style.cursor = active ? 'crosshair' : 'default';
    },

    // ======================== CANVAS OVERLAY ========================
    _createCanvasOverlay: function () {
      const self = this;
      const c = document.createElement('canvas');
      c.id = 'ct-overlay';
      c.style.cssText =
        'position:absolute;top:0;left:0;width:100%;height:100%;' +
        'z-index:10;pointer-events:none;';
      this._mainEl.style.position = 'relative';
      this._mainEl.appendChild(c);
      this._canvas = c;
      this._ctx = c.getContext('2d');

      // Sync canvas resolution on resize — immediate first call, debounce subsequent
      const sync = function () { self._syncCanvasSize(); };
      sync();
      window.addEventListener('resize', sync);
      // Also observe main chart size changes
      if (window.ResizeObserver) {
        let resizePending = false;
        this._resizeObserver = new ResizeObserver(function () {
          if (!resizePending) {
            resizePending = true;
            requestAnimationFrame(function () {
              resizePending = false;
              self._syncCanvasSize();
            });
          }
        });
        this._resizeObserver.observe(this._mainEl);
      }
    },

    _syncCanvasSize: function () {
      const c = this._canvas;
      if (!c) return;
      const w = this._mainEl.clientWidth;
      const h = this._mainEl.clientHeight;
      if (w > 0 && h > 0 && (c.width !== w || c.height !== h)) {
        c.width = w;
        c.height = h;
        this._redrawAll();
      }
    },

    // ======================== RULER LABEL ========================
    _createRulerLabel: function () {
      const el = document.createElement('div');
      el.id = 'ct-ruler-label';
      el.style.cssText =
        'position:absolute;top:0;left:0;padding:4px 8px;' +
        'background:rgba(30,34,45,0.92);color:#f0b90b;font-size:11px;' +
        'border-radius:3px;pointer-events:none;z-index:20;display:none;' +
        'font-family:monospace;white-space:nowrap;border:1px solid #f0b90b;';
      this._mainEl.appendChild(el);
      this._rulerLabel = el;
    },

    // ======================== EVENTS ========================
    _bindEvents: function () {
      const self = this;
      const c = this._canvas;

      c.addEventListener('mousedown', function (e) { self._onMouseDown(e); });
      c.addEventListener('mousemove', function (e) { self._onMouseMove(e); });
      c.addEventListener('mouseup', function (e) { self._onMouseUp(e); });
      c.addEventListener('mouseleave', function (e) { self._onMouseUp(e); });
      // Prevent chart from capturing events
      c.addEventListener('click', function (e) { e.stopPropagation(); });
      c.addEventListener('dblclick', function (e) { e.stopPropagation(); });
    },

    _bindKeyboard: function () {
      const self = this;
      document.addEventListener('keydown', function (e) {
        self._shiftKey = e.shiftKey;
        self._altKey = e.altKey;
        self._ctrlKey = e.ctrlKey || e.metaKey;

        // Shift: activate ruler mode temporarily
        if (e.shiftKey && !self._isDrawing && self._activeTool !== 'fibo') {
          self._setCanvasActive(true);
          self._canvas.style.cursor = 'crosshair';
        }

        // Alt: activate fibo mode temporarily
        if (e.altKey && !self._isDrawing && self._activeTool !== 'ruler') {
          self._setCanvasActive(true);
          self._canvas.style.cursor = 'crosshair';
          self._showHint('Alt: Click diem dau (dinh/day) → Click diem cuoi');
        }

        // Keyboard shortcuts for tools
        if (!e.ctrlKey && !e.altKey && !e.shiftKey && !self._isDrawing) {
          const bar = document.getElementById('chart-toolbar');
          switch (e.key.toLowerCase()) {
            case 'h':
              if (bar) self._activateTool('hline', bar.querySelector('[data-tool="hline"]'));
              break;
            case 'v':
              if (bar) self._activateTool('vline', bar.querySelector('[data-tool="vline"]'));
              break;
            case 't':
              if (bar) self._activateTool('trend', bar.querySelector('[data-tool="trend"]'));
              break;
            case 'escape':
              self._activeTool = null;
              self._setCanvasActive(false);
              self._isDrawing = false;
              self._clearTemp();
              const btns = bar ? bar.querySelectorAll('[data-tool].active') : [];
              for (let i = 0; i < btns.length; i++) btns[i].classList.remove('active');
              self._showHint('Da huy');
              break;
          }
        }
      });

      document.addEventListener('keyup', function (e) {
        self._shiftKey = e.shiftKey;
        self._altKey = e.altKey;
        self._ctrlKey = e.ctrlKey || e.metaKey;

        // Restore canvas state when modifiers released
        if (!e.shiftKey && !e.altKey && !self._ctrlKey) {
          if (!self._activeTool) {
            self._setCanvasActive(false);
          }
          self._canvas.style.cursor = self._activeTool ? 'crosshair' : 'default';
        }

        // If ruler was in-progress via shift, finalize
        if (!e.shiftKey && self._isDrawing && self._activeTool === null && self._startPoint) {
          self._finalizeRuler();
        }
      });
    },

    // ======================== MOUSE HANDLERS ========================
    _getChartCoords: function (e) {
      const rect = this._canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const time = this._mainChart.timeScale().coordinateToTime(x);
      const price = this._candleSeries.coordinateToPrice(y);
      return { x: x, y: y, time: time, price: price };
    },

    _onMouseDown: function (e) {
      const pt = this._getChartCoords(e);
      if (pt.time == null) return; // outside chart area

      // --- Ruler mode (Shift key) ---
      if (this._shiftKey && !this._activeTool) {
        this._isDrawing = true;
        this._startPoint = pt;
        this._currentPoint = pt;
        this._canvas.style.cursor = 'crosshair';
        e.preventDefault();
        return;
      }

      // --- Fibonacci mode (Alt key) ---
      if (this._altKey && !this._activeTool) {
        this._handleFiboClick(pt);
        e.preventDefault();
        return;
      }

      // --- Explicit tool mode ---
      if (!this._activeTool) return;

      if (!this._isDrawing) {
        // First click
        this._isDrawing = true;
        this._startPoint = pt;
        this._currentPoint = pt;
      } else {
        // Second click — finalize drawing
        this._finalizeDrawing(pt);
        this._isDrawing = false;
        this._startPoint = null;
        this._currentPoint = null;
        this._clearTemp();
        // Keep tool active for next drawing
        if (this._activeTool === 'hline' || this._activeTool === 'vline' || this._activeTool === 'trend') {
          // Ready for next drawing
        }
      }
      e.preventDefault();
    },

    _onMouseMove: function (e) {
      if (!this._isDrawing) return;
      const pt = this._getChartCoords(e);
      this._currentPoint = pt;

      if (this._shiftKey && !this._activeTool) {
        // Ruler mode: show live measurement
        this._drawRulerPreview();
      } else if (this._activeTool) {
        // Drawing mode: show preview
        this._drawPreview();
      }
    },

    _onMouseUp: function (e) {
      if (!this._isDrawing) return;

      // Ruler finalization
      if (this._shiftKey && !this._activeTool && this._startPoint) {
        this._finalizeRuler();
        return;
      }

      // For trend/hline/vline, two-click mode — don't finalize on mouseup, wait for second click
    },

    // ======================== DRAWING FINALIZATION ========================
    _finalizeDrawing: function (endPt) {
      const drawing = {
        type: this._activeTool,
        priceLines: [],
      };

      switch (this._activeTool) {
        case 'hline':
          drawing.price = endPt.price;
          drawing.pl = this._candleSeries.createPriceLine({
            price: endPt.price,
            color: '#f0b90b',
            lineWidth: 1,
            lineStyle: 2, // dotted
            axisLabelVisible: true,
            title: '' + endPt.price.toFixed(2),
          });
          break;

        case 'vline':
          // Store time for persistent render (survives resize via timeToCoordinate)
          drawing.time = endPt.time;
          break;

        case 'trend':
          // Store chart coords (time, price) for persistent render (survives resize)
          drawing.startTime = this._startPoint.time;
          drawing.startPrice = this._startPoint.price;
          drawing.endTime = endPt.time;
          drawing.endPrice = endPt.price;
          break;
      }

      this._getSymbolDrawings().push(drawing);
      this._redrawAll();
    },

    _finalizeRuler: function () {
      if (!this._startPoint || !this._currentPoint) {
        this._isDrawing = false;
        this._startPoint = null;
        this._currentPoint = null;
        this._hideRulerLabel();
        this._clearTemp();
        return;
      }

      const start = this._startPoint;
      const end = this._currentPoint;

      // Calculate metrics
      const deltaPrice = end.price - start.price;
      const pctChange = ((deltaPrice / start.price) * 100);
      const timeDelta = Math.abs(end.time - start.time);
      // Estimate candle count based on timeframe
      const currentTf = document.getElementById('tf-selector');
      let tfMin = 1;
      if (currentTf) {
        const tfVal = currentTf.value;
        if (tfVal === '5m') tfMin = 5;
        else if (tfVal === '15m') tfMin = 15;
        else if (tfVal === '1h') tfMin = 60;
        else if (tfVal === '4h') tfMin = 240;
      }
      const candleCount = Math.round(timeDelta / (tfMin * 60));

      const isPositive = deltaPrice >= 0;
      const sign = isPositive ? '+' : '';

      this._showRulerLabel(
        this._startPoint,
        this._currentPoint,
        'Gia: ' + sign + deltaPrice.toFixed(2) + ' (' + sign + pctChange.toFixed(2) + '%)' +
        ' | ' + candleCount + ' nen (' + tfMin + 'm)'
      );

      // Keep ruler visible for a few seconds then fade
      const self = this;
      clearTimeout(this._rulerFadeTimer);
      this._rulerFadeTimer = setTimeout(function () {
        self._hideRulerLabel();
      }, 4000);

      // Draw selection rectangle (temporary)
      this._drawRulerRect(start, end);

      this._isDrawing = false;
      this._startPoint = null;
      this._currentPoint = null;
      this._setCanvasActive(false);

      // Clear the temp rectangle after a moment
      const self2 = this;
      setTimeout(function () {
        self2._clearTemp();
      }, 3000);
    },

    // ======================== FIBONACCI ========================
    _fiboClickCount: 0,
    _fiboPeak: null,

    _handleFiboClick: function (pt) {
      if (!this._candleSeries) {
        console.warn('[ChartTools] Cannot draw Fibo — no chart series');
        this._fiboClickCount = 0;
        this._fiboPeak = null;
        return;
      }
      if (this._fiboClickCount === 0) {
        this._fiboPeak = pt;
        this._fiboClickCount = 1;
        this._showHint('Da chon diem ' + (pt.price > 50000 ? 'dinh' : 'day') +
          ' tai ' + pt.price.toFixed(2) + ' — Click diem thu 2');
        // Draw a temporary marker
        this._drawFiboMarker(pt, '#f0b90b');
      } else {
        // Second click — calculate and draw fibo levels
        this._drawFibonacci(this._fiboPeak, pt);
        this._fiboClickCount = 0;
        this._fiboPeak = null;
        this._setCanvasActive(false);
        this._showHint('Da ve Fibonacci');
      }
    },

    _drawFibonacci: function (p1, p2) {
      if (!this._candleSeries) {
        console.warn('[ChartTools] Cannot draw Fibonacci — candleSeries not initialized');
        this._fiboClickCount = 0;
        this._fiboPeak = null;
        return;
      }
      // Clear previous fibo lines
      this._clearFiboLines();

      const highPrice = Math.max(p1.price, p2.price);
      const lowPrice = Math.min(p1.price, p2.price);
      const diff = highPrice - lowPrice;
      const isUptrend = p2.price > p1.price; // click sequence indicates direction

      // Fibonacci retracement levels (from top to bottom for uptrend retracement)
      const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
      const colors = [
        '#26a69a', // 0
        '#4caf50', // 0.236
        '#8bc34a', // 0.382
        '#ffeb3b', // 0.5
        '#ff9800', // 0.618
        '#f44336', // 0.786
        '#ef5350', // 1
      ];

      const self = this;
      this._fiboLines = [];
      levels.forEach(function (level, i) {
        let price;
        if (isUptrend) {
          // Retracement from high down
          price = highPrice - diff * level;
        } else {
          // Retracement from low up
          price = lowPrice + diff * level;
        }

        const pl = self._candleSeries.createPriceLine({
          price: price,
          color: colors[i],
          lineWidth: 1,
          lineStyle: i === 0 || i === 6 ? 0 : 2, // Solid for 0/1, dashed for others
          axisLabelVisible: true,
          title: (level * 100).toFixed(1) + '%',
        });
        self._fiboLines.push(pl);
      });

      // Store for persistence
      this._getSymbolDrawings().push({
        type: 'fibo',
        peak: p1,
        trough: p2,
        highPrice: highPrice,
        lowPrice: lowPrice,
        isUptrend: isUptrend,
        priceLines: this._fiboLines,
      });
    },

    _clearFiboLines: function () {
      for (let i = 0; i < this._fiboLines.length; i++) {
        try { this._candleSeries.removePriceLine(this._fiboLines[i]); } catch (e) { /* already removed */ }
      }
      this._fiboLines = [];
    },

    _drawFiboMarker: function (pt, color) {
      this._tempFiboMarker = { x: pt.x, y: pt.y, color: color };
      this._redrawAll();
      const self = this;
      setTimeout(function () {
        self._tempFiboMarker = null;
        self._redrawAll();
      }, 3000);
    },

    // ======================== RULER UI ========================
    _drawRulerPreview: function () {
      if (!this._startPoint || !this._currentPoint || !this._ctx) return;
      this._drawRulerRect(this._startPoint, this._currentPoint);

      const start = this._startPoint;
      const end = this._currentPoint;
      const deltaPrice = end.price - start.price;
      const pctChange = ((deltaPrice / start.price) * 100);
      const sign = deltaPrice >= 0 ? '+' : '';

      this._showRulerLabel(start, end,
        sign + deltaPrice.toFixed(2) + ' (' + sign + pctChange.toFixed(2) + '%)'
      );
    },

    _drawRulerRect: function (p1, p2) {
      const ctx = this._ctx;
      const x = Math.min(p1.x, p2.x);
      const y = Math.min(p1.y, p2.y);
      const w = Math.abs(p2.x - p1.x);
      const h = Math.abs(p2.y - p1.y);

      this._clearTemp();
      ctx.save();
      ctx.fillStyle = 'rgba(240,185,11,0.08)';
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = 'rgba(240,185,11,0.5)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    },

    _showRulerLabel: function (p1, p2, text) {
      const el = this._rulerLabel;
      if (!el) return;
      el.textContent = text;
      el.style.display = 'block';
      // Position near the selection
      const left = Math.min(p1.x, p2.x);
      let top = Math.min(p1.y, p2.y) - 24;
      if (top < 0) top = Math.max(p1.y, p2.y) + 4;
      el.style.left = left + 'px';
      el.style.top = top + 'px';
    },

    _hideRulerLabel: function () {
      if (this._rulerLabel) this._rulerLabel.style.display = 'none';
    },

    // ======================== PREVIEW RENDERING ========================
    _drawPreview: function () {
      if (!this._startPoint || !this._currentPoint) return;
      const ctx = this._ctx;
      this._clearTemp();

      ctx.save();
      ctx.strokeStyle = 'rgba(240,185,11,0.7)';
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 3]);

      const s = this._startPoint;
      const e = this._currentPoint;

      switch (this._activeTool) {
        case 'hline':
          ctx.beginPath();
          ctx.moveTo(0, e.y);
          ctx.lineTo(this._canvas.width, e.y);
          ctx.stroke();
          // Price label
          ctx.fillStyle = '#f0b90b';
          ctx.font = '11px monospace';
          ctx.fillText(e.price.toFixed(2), 4, e.y - 4);
          break;

        case 'vline':
          ctx.beginPath();
          ctx.moveTo(e.x, 0);
          ctx.lineTo(e.x, this._canvas.height);
          ctx.stroke();
          break;

        case 'trend':
          ctx.setLineDash([]);
          ctx.strokeStyle = 'rgba(240,185,11,0.8)';
          ctx.beginPath();
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(e.x, e.y);
          ctx.stroke();

          // Endpoint circles
          ctx.fillStyle = '#f0b90b';
          ctx.beginPath();
          ctx.arc(s.x, s.y, 4, 0, Math.PI * 2);
          ctx.fill();
          ctx.beginPath();
          ctx.arc(e.x, e.y, 4, 0, Math.PI * 2);
          ctx.fill();

          // Price labels
          ctx.font = '11px monospace';
          ctx.fillText(s.price.toFixed(2), s.x + 8, s.y - 4);
          ctx.fillText(e.price.toFixed(2), e.x + 8, e.y - 4);
          break;
      }
      ctx.restore();
    },

    _clearTemp: function () {
      if (!this._ctx || !this._canvas) return;
      this._ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
      this._redrawPersistent();
    },

    // ======================== PERSISTENT DRAWING RENDER ========================
    _redrawAll: function () {
      this._clearTemp();
    },

    _redrawPersistent: function () {
      const ctx = this._ctx;
      if (!ctx) return;

      const dwgs = this._getSymbolDrawings();
      for (let i = 0; i < dwgs.length; i++) {
        const d = dwgs[i];
        ctx.save();

        if (d.type === 'vline') {
          // Convert stored time to current pixel x (survives resize)
          const x = this._mainChart.timeScale().timeToCoordinate(d.time);
          if (x == null) { ctx.restore(); continue; }
          ctx.strokeStyle = '#f0b90b';
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(x, 0);
          ctx.lineTo(x, this._canvas.height);
          ctx.stroke();
        } else if (d.type === 'trend') {
          // Convert stored chart coords to current pixels (survives resize)
          const sx = this._mainChart.timeScale().timeToCoordinate(d.startTime);
          const sy = this._candleSeries.priceToCoordinate(d.startPrice);
          const ex = this._mainChart.timeScale().timeToCoordinate(d.endTime);
          const ey = this._candleSeries.priceToCoordinate(d.endPrice);
          if (sx == null || sy == null || ex == null || ey == null) { ctx.restore(); continue; }

          ctx.strokeStyle = 'rgba(240,185,11,0.6)';
          ctx.lineWidth = 1;
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(sx, sy);
          ctx.lineTo(ex, ey);
          ctx.stroke();
          // Endpoint circles
          ctx.fillStyle = '#f0b90b';
          ctx.beginPath();
          ctx.arc(sx, sy, 3, 0, Math.PI * 2);
          ctx.fill();
          ctx.beginPath();
          ctx.arc(ex, ey, 3, 0, Math.PI * 2);
          ctx.fill();
        }

        ctx.restore();
      }

      // Draw temp fibo marker
      if (this._tempFiboMarker) {
        const m = this._tempFiboMarker;
        ctx.save();
        ctx.fillStyle = m.color;
        ctx.beginPath();
        ctx.arc(m.x, m.y, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
      }

      // Draw volume profile if enabled
      if (this._volProfile.enabled) {
        this._drawVolumeProfile();
      }
    },

    // ======================== INDICATOR TOGGLE ========================
    _toggleIndicator: function (ind, btn) {
      switch (ind) {
        case 'ema':
          const vis = !this._emaSeries.options().visible;
          this._emaSeries.applyOptions({ visible: vis });
          btn.classList.toggle('on', vis);
          break;

        case 'rsi':
          const rsiVis = this._rsiEl.style.display !== 'none';
          this._rsiEl.style.display = rsiVis ? 'none' : '';
          this._rsiEl.style.flex = rsiVis ? '0' : '1.5';
          btn.classList.toggle('on', !rsiVis);
          // Wait for browser reflow before reading new clientHeight (Q1)
          requestAnimationFrame(function () {
            this._rsiChart.applyOptions({
              height: rsiVis ? 0 : (this._rsiEl.clientHeight || 100),
            });
            this._mainChart.applyOptions({ height: this._mainEl.clientHeight });
          }.bind(this));
          break;

        case 'vol':
          const volVis = this._volEl.style.display !== 'none';
          this._volEl.style.display = volVis ? 'none' : '';
          this._volEl.style.flex = volVis ? '0' : '1.5';
          btn.classList.toggle('on', !volVis);
          // Wait for browser reflow before reading new clientHeight (Q1)
          requestAnimationFrame(function () {
            this._volChart.applyOptions({
              height: volVis ? 0 : (this._volEl.clientHeight || 100),
            });
            this._mainChart.applyOptions({ height: this._mainEl.clientHeight });
          }.bind(this));
          break;

        case 'dayLevels':
          if (this._dayLevelsHidden) {
            // Restore: recreate price lines from stored values
            this._dayLevelsHidden = false;
            btn.classList.add('on');
            if (this._dayLevelsHigh != null && this._dayLevelsLow != null && this._updateDayLevels) {
              this._updateDayLevels(this._dayLevelsHigh, this._dayLevelsLow, this._dayLevelsTs || 0);
              this._showHint('S/R da hien lai');
            } else {
              this._showHint('S/R se hien lai khi co du lieu ngay moi');
            }
          } else {
            // Hide: remove existing lines
            this._dayLevelsHidden = true;
            btn.classList.remove('on');
            if (this._extPrevDayHighLine) {
              try { this._candleSeries.removePriceLine(this._extPrevDayHighLine); } catch (e) { }
            }
            if (this._extPrevDayLowLine) {
              try { this._candleSeries.removePriceLine(this._extPrevDayLowLine); } catch (e) { }
            }
          }
          break;

        case 'volProfile':
          this._volProfile.enabled = !this._volProfile.enabled;
          btn.classList.toggle('on', this._volProfile.enabled);
          if (this._volProfile.enabled) {
            this._computeVolumeProfile();
            this._showHint('Volume Profile da bat — phan phoi khoi luong theo gia');
          }
          this._redrawAll();
          break;
      }
    },

    // ======================== VOLUME PROFILE ========================
    _computeVolumeProfile: function () {
      // Get candle data from tfStore for current timeframe
      const currentTf = document.getElementById('tf-selector');
      const tf = currentTf ? currentTf.value : '1m';
      const store = this._getStore();
      if (!store) return;
      const candles = (store[tf] && store[tf].candles) ? store[tf].candles : [];
      if (candles.length === 0) return;

      // Derive price range from actual candle data
      let priceMax = -Infinity, priceMin = Infinity;
      for (let i = 0; i < candles.length; i++) {
        if (candles[i].high > priceMax) priceMax = candles[i].high;
        if (candles[i].low < priceMin) priceMin = candles[i].low;
      }
      if (!isFinite(priceMax) || !isFinite(priceMin) || priceMax === priceMin) return;

      const numBins = 50;
      const binSize = (priceMax - priceMin) / numBins;
      const bins = [];
      for (let i = 0; i < numBins; i++) {
        bins.push({ priceLow: priceMin + i * binSize, priceHigh: priceMin + (i + 1) * binSize, volume: 0 });
      }

      for (let j = 0; j < candles.length; j++) {
        const c = candles[j];
        const vol = c.volume || 0;
        const firstBin = Math.max(0, Math.floor((c.low - priceMin) / binSize));
        const lastBin = Math.min(numBins - 1, Math.floor((c.high - priceMin) / binSize));
        const span = Math.max(1, lastBin - firstBin + 1);
        const volPerBin = vol / span;
        for (let b = firstBin; b <= lastBin; b++) {
          bins[b].volume += volPerBin;
        }
      }

      this._volProfile.levels = bins;
    },

    _drawVolumeProfile: function () {
      const ctx = this._ctx;
      const bins = this._volProfile.levels;
      if (!bins || bins.length === 0) return;

      let maxVol = 0;
      for (let i = 0; i < bins.length; i++) {
        if (bins[i].volume > maxVol) maxVol = bins[i].volume;
      }
      if (maxVol === 0) return;

      const profileWidth = 120; // pixels from right edge
      const w = this._canvas.width;
      const h = this._canvas.height;

      ctx.save();
      for (let i = 0; i < bins.length; i++) {
        const b = bins[i];
        const barW = (b.volume / maxVol) * profileWidth;
        const y = this._candleSeries.priceToCoordinate(b.priceHigh);
        const y2 = this._candleSeries.priceToCoordinate(b.priceLow);
        if (y == null || y2 == null) continue;
        let barH = Math.abs(y2 - y);
        if (barH < 1) barH = 1;

        const alpha = 0.3 + 0.5 * (b.volume / maxVol);
        ctx.fillStyle = 'rgba(100,149,237,' + alpha.toFixed(2) + ')';
        ctx.fillRect(w - profileWidth - 4, y, barW, barH);
      }
      ctx.restore();
    },

    // ======================== CLEAR ========================
    _clearAllDrawings: function () {
      // Remove all price lines from drawings
      const dwgs = this._getSymbolDrawings();
      for (let i = 0; i < dwgs.length; i++) {
        const d = dwgs[i];
        if (d.pl) {
          try { this._candleSeries.removePriceLine(d.pl); } catch (e) { }
        }
        if (d.priceLines) {
          for (let j = 0; j < d.priceLines.length; j++) {
            try { this._candleSeries.removePriceLine(d.priceLines[j]); } catch (e) { }
          }
        }
      }
      if (this._currentSymbol) this._drawings[this._currentSymbol] = [];
      this._clearFiboLines();
      this._clearTemp();
      this._showHint('Da xoa tat ca ban ve');
    },

    // ======================== PUBLIC API ========================

    // Call from dashboard.js when symbol changes — redraws per-symbol drawings
    setSymbol: function (symbol) {
      this._currentSymbol = symbol;
      this._redrawAll();
    },

    // Call from index.html when day_levels are updated externally
    onDayLevelsUpdated: function (highLine, lowLine) {
      this._extPrevDayHighLine = highLine;
      this._extPrevDayLowLine = lowLine;
      // Store values for S/R toggle restore
      if (highLine && highLine.options) { this._dayLevelsHigh = highLine.options().price; }
      if (lowLine && lowLine.options) { this._dayLevelsLow = lowLine.options().price; }
      this._dayLevelsTs = Date.now();
    },

    // Recompute vol profile on data change
    refreshVolumeProfile: function () {
      if (this._volProfile.enabled) {
        this._computeVolumeProfile();
        this._redrawAll();
      }
    },

    // Destroy and clean up
    destroy: function () {
      if (this._resizeObserver) {
        this._resizeObserver.disconnect();
        this._resizeObserver = null;
      }
      this._clearAllDrawings();
      const tb = document.getElementById('chart-toolbar');
      if (tb) tb.remove();
      if (this._canvas) this._canvas.remove();
      const hint = document.getElementById('ct-hint');
      if (hint) hint.remove();
      const ruler = document.getElementById('ct-ruler-label');
      if (ruler) ruler.remove();
      this._activeTool = null;
      console.log('[ChartTools] Destroyed — resources cleaned up');
    },
  };

  // Expose
  window.ChartTools = CT;
})();
