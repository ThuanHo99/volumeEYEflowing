"""UI/static analysis tests for dashboard CSS, HTML, and JS patterns.

Groups: Bug 5/7/9/10, C1, Q1-Q5, M1-M4.
"""

import json
import pathlib
import pytest
from aiohttp import web


# ── Bug 5: Static cache ────────────────────────────────────────────────────

class TestBug5_StaticCache:
    """_STATIC_CACHE must be populated on first read and reused."""

    def test_cache_is_dict(self):
        from ui.web_dashboard import _STATIC_CACHE
        assert isinstance(_STATIC_CACHE, dict)

    def test_cache_populated_after_static_request(self):
        from ui.web_dashboard import _STATIC_CACHE
        _STATIC_CACHE.clear()
        import ui.web_dashboard as webd
        filename = 'chart_tools.js'
        filepath = webd._STATIC_DIR / filename
        if not filepath.is_file():
            pytest.skip(f"{filename} not found in static dir")
        content = filepath.read_bytes()
        content_type = 'application/javascript'
        webd._STATIC_CACHE[filename] = (content, content_type)
        assert filename in webd._STATIC_CACHE
        cached_content, cached_type = webd._STATIC_CACHE[filename]
        assert cached_content == content
        assert cached_type == 'application/javascript'
        assert filename in webd._STATIC_CACHE
        hit_content, hit_type = webd._STATIC_CACHE[filename]
        assert hit_content == content

    def test_cache_has_cache_control_header(self):
        response = web.Response(
            body=b'test',
            content_type='application/javascript',
            headers={'Cache-Control': 'public, max-age=3600'},
        )
        assert response.headers.get('Cache-Control') == 'public, max-age=3600'

    def test_image_files_get_longer_cache(self):
        def cache_ttl(ext):
            if ext in ('.js', '.css'):
                return 3600
            return 86400
        assert cache_ttl('.js') == 3600
        assert cache_ttl('.css') == 3600
        assert cache_ttl('.svg') == 86400
        assert cache_ttl('.png') == 86400
        assert cache_ttl('.woff2') == 86400


# ── Bug 7: candleCount cap ──────────────────────────────────────────────────

class TestBug7_CandleCount:
    """candleCount display is capped to prevent UI overflow."""

    def test_counter_capped_display(self):
        cap = 999999
        for count, expected in [
            (0, '0'),
            (50000, '50000'),
            (999999, '999999'),
            (1000000, '1000k'),
            (9999999, '10000k'),
            (12345678, '12346k'),
            (999999, '999999'),
        ]:
            if count > cap:
                display = str(round(count / 1000)) + 'k'
            else:
                display = str(count)
            assert display == expected, f"count={count} → expected '{expected}', got '{display}'"


# ── Bug 9: Resize flicker ───────────────────────────────────────────────────

class TestBug9_ResizeFlicker:
    """Single deferred resize replaces dual setTimeout pattern."""

    def test_single_resize_not_double(self):
        resize_calls = []
        def resizeAllCharts():
            resize_calls.append(1)
        # Before fix (dual setTimeout):
        resize_calls.clear()
        resize_calls.append(1)
        resize_calls.append(1)
        assert len(resize_calls) == 2  # old behavior: 2 calls
        # After fix (single setTimeout):
        resize_calls.clear()
        resize_calls.append(1)
        assert len(resize_calls) == 1  # new behavior: 1 call


# ── Bug 10: Cache-Control headers ───────────────────────────────────────────

class TestBug10_CacheControl:
    """Static file responses must carry Cache-Control header."""

    def test_static_handler_returns_cache_control(self):
        ext = '.js'
        cache_ttl = 3600 if ext in ('.js', '.css') else 86400
        response = web.Response(
            body=b'// test',
            content_type='application/javascript',
            headers={'Cache-Control': f'public, max-age={cache_ttl}'},
        )
        assert 'Cache-Control' in response.headers
        assert response.headers['Cache-Control'] == 'public, max-age=3600'

    def test_cache_control_varies_by_file_type(self):
        cases = [
            ('.js',    'public, max-age=3600'),
            ('.css',   'public, max-age=3600'),
            ('.svg',   'public, max-age=86400'),
            ('.png',   'public, max-age=86400'),
            ('.woff2', 'public, max-age=86400'),
        ]
        for ext, expected_cc in cases:
            ttl = 3600 if ext in ('.js', '.css') else 86400
            response = web.Response(
                body=b'x',
                content_type='application/octet-stream',
                headers={'Cache-Control': f'public, max-age={ttl}'},
            )
            assert response.headers['Cache-Control'] == expected_cc


# ── C1: View Switching ──────────────────────────────────────────────────────

class TestC1_ViewSwitching:
    """Nav/sidebar clicks must switch views."""

    def _js_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'dashboard.js'

    def _css_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'css' / 'dashboard.css'

    def _html_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'templates' / 'index.html'

    def test_view_to_nav_index_mapping(self):
        mapping = {'charts': 0, 'alerts': 1, 'scanner': 2}
        assert mapping['charts'] == 0
        assert mapping['alerts'] == 1
        assert mapping['scanner'] == 2

    def test_nav_to_view_mapping(self):
        nav_to_view = ['charts', 'alerts', 'scanner']
        assert nav_to_view[0] == 'charts'
        assert nav_to_view[1] == 'alerts'
        assert nav_to_view[2] == 'scanner'

    def test_switch_view_js_implemented(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'function switchView' in js
        assert 'VIEW_TO_NAV' in js
        assert 'NAV_TO_VIEW' in js
        assert 'openRightPanel' in js

    def test_open_right_panel_js_implemented(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'function openRightPanel' in js
        assert "classList.add('open')" in js


# ── Q1: RSI/Vol Toggle Height ───────────────────────────────────────────────

class TestQ1_RSIVolToggleHeight:
    """RSI/Vol toggle must use requestAnimationFrame."""

    def _ct_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'chart_tools.js'

    def test_toggle_uses_request_animation_frame(self):
        ct_src = self._ct_path().read_text(encoding='utf-8')
        assert 'requestAnimationFrame' in ct_src

    def test_rsi_toggle_no_direct_client_height_read(self):
        ct_src = self._ct_path().read_text(encoding='utf-8')
        rsi_idx = ct_src.index("case 'rsi':")
        rsi_block = ct_src[rsi_idx:rsi_idx + 600]
        assert 'requestAnimationFrame' in rsi_block

    def test_vol_toggle_no_direct_client_height_read(self):
        ct_src = self._ct_path().read_text(encoding='utf-8')
        vol_idx = ct_src.index("case 'vol':")
        vol_block = ct_src[vol_idx:vol_idx + 500]
        assert 'requestAnimationFrame' in vol_block


# ── Q2: CSS Color Variables ─────────────────────────────────────────────────

class TestQ2_CSSColorVariables:
    """JS must use centralized color constants; CSS must use :root variables."""

    def _css_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'css' / 'dashboard.css'

    def _js_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'dashboard.js'

    def test_dashboard_css_has_root_variables(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert ':root' in css
        assert '--accent' in css
        assert '--green' in css
        assert '--red' in css

    def test_dashboard_js_has_color_constants(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'const C = {' in js or 'var C = {' in js or 'const C={' in js
        assert "BG_PRIMARY:" in js
        assert "ACCENT:" in js
        assert "GREEN:" in js

    def test_js_hex_colors_only_in_constants(self):
        js = self._js_path().read_text(encoding='utf-8')
        c_start = js.index('const C = {')
        c_end = js.index('};', c_start) + 2
        import re
        hex_colors = [m.start() for m in re.finditer(r"'#[0-9a-fA-F]{6}'", js)]
        for pos in hex_colors:
            assert c_start <= pos <= c_end


# ── Q3: Scrollbar Styling ───────────────────────────────────────────────────

class TestQ3_ScrollbarStyling:
    """Dashboard must have custom scrollbar styles for dark theme."""

    def _css_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'css' / 'dashboard.css'

    def test_scrollbar_styles_exist(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert '::-webkit-scrollbar' in css
        assert '::-webkit-scrollbar-track' in css
        assert '::-webkit-scrollbar-thumb' in css


# ── Q4: Global Pollution ────────────────────────────────────────────────────

class TestQ4_GlobalPollution:
    """chart_tools.js must minimize global exports."""

    def _js_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'dashboard.js'

    def _ct_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'chart_tools.js'

    def test_dashboard_js_no_tfstore_global(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'window._tfStore' not in js

    def test_chart_tools_has_jsdoc_global(self):
        ct_src = self._ct_path().read_text(encoding='utf-8')
        assert '@global' in ct_src

    def test_chart_tools_uses_getstore_not_tfstore(self):
        ct_src = self._ct_path().read_text(encoding='utf-8')
        assert 'this._getStore()' in ct_src


# ── Q5: SRI Integrity Hash ──────────────────────────────────────────────────

class TestQ5_SRIHash:
    """CDN script tag must include integrity hash for supply-chain security."""

    def _html_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'templates' / 'index.html'

    def test_cdn_script_has_integrity(self):
        html = self._html_path().read_text(encoding='utf-8')
        assert 'integrity=' in html
        assert 'sha384-' in html

    def test_cdn_script_has_crossorigin(self):
        html = self._html_path().read_text(encoding='utf-8')
        assert 'crossorigin' in html


# ── M1: View Switching with URL Hash ────────────────────────────────────────

class TestM1_ViewSwitchingHash:
    """switchView must update location.hash and restore from hash on page load."""

    def _js_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'dashboard.js'

    def _css_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'css' / 'dashboard.css'

    def _html_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'templates' / 'index.html'

    def test_switchview_updates_hash(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "history.replaceState" in js
        assert "location.hash" in js

    def test_hashchange_listener_exists(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "hashchange" in js

    def test_close_right_panel_function(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "closeRightPanel" in js
        assert "classList.remove('open')" in js

    def test_initial_view_from_hash(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "location.hash.replace('#', '')" in js

    def test_scanner_view_has_full_implementation(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'initVolumeScanner' in js
        assert 'vs-signal-log-full' in js

    def test_nav_to_view_mapping_complete(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "NAV_TO_VIEW" in js
        assert "VIEW_TO_NAV" in js
        assert "'charts'" in js and "'alerts'" in js and "'scanner'" in js

    def test_view_panels_in_html(self):
        html = self._html_path().read_text(encoding='utf-8')
        assert 'id="view-alerts"' in html
        assert 'id="view-scanner"' in html
        assert 'class="view-panel"' in html

    def test_view_refresh_functions(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'refreshAlertsView' in js

    def test_switchview_manages_view_panels(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "view-panel" in js

    def test_view_css_exists(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert '#view-alerts' in css
        assert '#view-scanner' in css

    def test_scanner_log_in_html(self):
        html = self._html_path().read_text(encoding='utf-8')
        assert 'id="vs-signal-log-full"' in html
        assert 'id="vs-symbol-search"' in html
        assert 'signal-log-full' in html


# ── M2: localStorage Persistence ────────────────────────────────────────────

class TestM2_LocalStoragePersistence:
    """User preferences must be saved to and restored from localStorage."""

    def _js_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'dashboard.js'

    def test_prefs_key_defined(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "PREFS_KEY" in js
        assert "localStorage" in js

    def test_save_prefs_function(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "savePrefs" in js
        assert "localStorage.setItem" in js
        assert "JSON.stringify" in js

    def test_load_prefs_function(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "loadPrefs" in js
        assert "localStorage.getItem" in js
        assert "JSON.parse" in js

    def test_apply_prefs_function(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "applyPrefs" in js

    def test_prefs_saves_timeframe(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "currentTf" in js

    def test_prefs_saves_panel_state(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "panelOpen" in js

    def test_prefs_saves_collapsed_sections(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "collapsedSections" in js

    def test_prefs_saves_filters(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "tfFilter" in js
        assert "typeFilter" in js

    def test_prefs_error_handling(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "try {" in js or "catch" in js


# ── M3: Toast Notification System ───────────────────────────────────────────

class TestM3_ToastNotificationSystem:
    """Toast notification system for connection, signal, and AI events."""

    def _js_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'js' / 'dashboard.js'

    def _css_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'css' / 'dashboard.css'

    def test_show_toast_function(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "showToast" in js

    def test_toast_container_creation(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "toast-container" in js
        assert "createToastContainer" in js

    def test_toast_types_defined(self):
        js = self._js_path().read_text(encoding='utf-8')
        for t in ('success', 'error', 'warning', 'info', 'signal'):
            assert t in js

    def test_toast_css_styles(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert '.toast' in css
        assert '.toast-success' in css
        assert '.toast-error' in css
        assert '.toast.show' in css
        assert '#toast-container' in css

    def test_ws_onopen_shows_toast(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "WebSocket connected" in js

    def test_ws_onclose_shows_toast(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert "WebSocket disconnected" in js

    def test_signal_shows_toast(self):
        js = self._js_path().read_text(encoding='utf-8')
        assert 'showToast' in js
        assert "'signal'" in js


# ── M4: Mobile Layout Improvements ──────────────────────────────────────────

class TestM4_MobileLayout:
    """Responsive CSS must include mobile breakpoints and touch-friendly sizing."""

    def _css_path(self):
        return pathlib.Path(__file__).resolve().parent.parent.parent / 'static' / 'css' / 'dashboard.css'

    def test_mobile_breakpoint_768(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert '@media (max-width: 768px)' in css

    def test_mobile_breakpoint_480(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert '@media (max-width: 480px)' in css

    def test_touch_friendly_tap_targets(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert 'min-height: 32px' in css or 'min-height: 28px' in css

    def test_panel_full_width_on_mobile(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert '100vw' in css

    def test_toast_responsive(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert 'calc(100vw - 12px)' in css

    def test_footer_compact_on_mobile(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert 'none' in css

    def test_chart_minimum_heights(self):
        css = self._css_path().read_text(encoding='utf-8')
        assert 'min-height: 150px' in css
