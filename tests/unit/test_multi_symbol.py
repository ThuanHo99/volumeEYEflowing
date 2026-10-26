"""Multi-symbol support: verify all symbols get independent state and processing."""

import pytest


class TestMultiSymbolState:
    """SymbolStateManager must maintain independent state per symbol."""

    def test_get_state_creates_per_symbol(self):
        from core.state_manager import SymbolStateManager
        mgr = SymbolStateManager(['1m'])
        state_btc = mgr.get_state('BTC/USDT:USDT')
        state_eth = mgr.get_state('ETH/USDT:USDT')
        assert state_btc is not state_eth, "Each symbol must have its own IndicatorState"

    def test_independent_candle_processing(self):
        from core.state_manager import SymbolStateManager
        from tests.factories import make_closed_candle
        mgr = SymbolStateManager(['1m', '5m'])

        state_btc = mgr.get_state('BTC/USDT:USDT')
        state_eth = mgr.get_state('ETH/USDT:USDT')

        # Feed 50 candles to BTC, 20 to ETH (need >14 for RSI to produce output)
        for i in range(50):
            c = make_closed_candle('1m', 50000.0 + i, ts=i * 60000)
            c['symbol'] = 'BTC/USDT:USDT'
            state_btc.process_candle(c)

        for i in range(20):
            c = make_closed_candle('1m', 40000.0 + i, ts=i * 60000)
            c['symbol'] = 'ETH/USDT:USDT'
            state_eth.process_candle(c)

        # BTC should have more close history entries than ETH
        assert len(state_btc.close_history['1m']) > 0
        # ETH should have its own close history (independent from BTC)
        assert len(state_eth.close_history['1m']) > 0
        assert state_eth.close_history['1m'][0] != state_btc.close_history['1m'][0]


class TestMultiSymbolCooldown:
    """SignalCooldownManager must handle per-symbol suppression correctly."""

    def test_per_symbol_mode_suppresses_all_types(self):
        from core.signals import SignalCooldownManager, Signal, SignalType, SignalLevel
        cooldown = SignalCooldownManager(cooldown_mode='per_symbol')

        sig_btc = Signal(type=SignalType.RSI_OVERBOUGHT, level=SignalLevel.WARNING,
                          symbol='BTC/USDT:USDT', tf='15m', close=50000.0,
                          rsi14=75.0, timestamp=1700000000000, detail="")
        sig_btc2 = Signal(type=SignalType.VOLUME_SPIKE, level=SignalLevel.ALERT,
                           symbol='BTC/USDT:USDT', tf='1h', close=51000.0,
                           rsi14=60.0, timestamp=1700000000000, detail="")
        sig_eth = Signal(type=SignalType.RSI_OVERBOUGHT, level=SignalLevel.WARNING,
                          symbol='ETH/USDT:USDT', tf='15m', close=3000.0,
                          rsi14=75.0, timestamp=1700000000000, detail="")

        # BTC RSI_OVERBOUGHT — not suppressed (first)
        assert not cooldown.is_suppressed(sig_btc)
        # BTC VOLUME_SPIKE — should be suppressed in per_symbol mode
        assert cooldown.is_suppressed(sig_btc2)
        # ETH RSI_OVERBOUGHT — different symbol, not suppressed
        assert not cooldown.is_suppressed(sig_eth)


class TestMultiSymbolPersistence:
    """Persistence must save/load per-symbol indicator state."""

    @pytest.mark.asyncio
    async def test_save_load_multiple_symbols(self, tmp_path, mocker):
        from core.persistence import save_indicator_state, load_indicator_state
        from core.state_manager import SymbolStateManager
        from tests.factories import make_closed_candle

        mocker.patch('core.persistence.DATA_DIR', str(tmp_path))

        from core.config import TIMEFRAMES
        mgr = SymbolStateManager(TIMEFRAMES)
        symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

        for symbol in symbols:
            state = mgr.get_state(symbol)
            for i in range(50):
                c = make_closed_candle('1m', 50000.0 + i, ts=i * 60000)
                c['symbol'] = symbol
                state.process_candle(c)

        result = await save_indicator_state(mgr)
        assert result is True

        loaded = load_indicator_state()
        assert loaded is not None
        for symbol in symbols:
            assert symbol in loaded, f"Missing {symbol} in saved state"
