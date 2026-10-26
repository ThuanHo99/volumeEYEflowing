import os
from unittest import mock

import core.config


class TestValidateStartupConfig:
    def test_missing_web_user(self):
        with (
            mock.patch.object(core.config, 'WEB_USER', ''),
            mock.patch.object(core.config, 'WEB_PASS', 'test'),
        ):
            errors = core.config.validate_startup_config()
            assert errors, f"Expected errors when WEB_USER empty, got: {errors}"
            assert any("WEB_USER" in e for e in errors)

    def test_missing_web_pass(self):
        with (
            mock.patch.object(core.config, 'WEB_USER', 'test'),
            mock.patch.object(core.config, 'WEB_PASS', ''),
        ):
            errors = core.config.validate_startup_config()
            assert errors, f"Expected errors when WEB_PASS empty, got: {errors}"

    def test_both_missing(self):
        with (
            mock.patch.object(core.config, 'WEB_USER', ''),
            mock.patch.object(core.config, 'WEB_PASS', ''),
        ):
            errors = core.config.validate_startup_config()
            assert errors, f"Expected errors when both unset, got: {errors}"
            assert "WEB_USER and WEB_PASS" in errors[0]

    def test_both_set(self):
        with (
            mock.patch.object(core.config, 'WEB_USER', 'u'),
            mock.patch.object(core.config, 'WEB_PASS', 'p'),
        ):
            errors = core.config.validate_startup_config()
            assert errors == [], f"Expected no errors when both set, got: {errors}"


class TestValidateRequiredTimeframes:
    def test_all_present(self):
        with mock.patch.object(core.config, 'TIMEFRAMES', ['1m', '5m', '15m', '1h', '4h']):
            warnings = core.config.validate_required_timeframes()
            assert warnings == [], f"Expected no warnings, got: {warnings}"

    def test_missing_15m(self):
        with mock.patch.object(core.config, 'TIMEFRAMES', ['1m', '5m', '1h', '4h']):
            warnings = core.config.validate_required_timeframes()
            assert warnings, f"Expected warnings when 15m missing, got: {warnings}"
            assert any("15m" in w for w in warnings)

    def test_missing_multiple(self):
        with mock.patch.object(core.config, 'TIMEFRAMES', ['1m', '5m']):
            warnings = core.config.validate_required_timeframes()
            assert len(warnings) >= 3, f"Expected >=3 warnings for all+ missing, got {len(warnings)}: {warnings}"
            assert any("15m" in w for w in warnings)
            assert any("1h" in w for w in warnings)
            assert any("4h" in w for w in warnings)
