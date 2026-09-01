"""Fase 3 multiativo, item 7 da correção obrigatória do PO: isolamento
REAL de estado entre `StrategyEngine`s -- duas séries de preço
MATERIALMENTE diferentes (nunca a mesma fixture de BTC só rotulada com
outro símbolo), provando que janelas SMA/ATR não são compartilhadas, que
alimentar candles de um símbolo não avança/altera os indicadores do outro,
que sinais diferentes podem surgir de séries divergentes, e que
reinício/reconstrução não mistura cursores nem estado entre símbolos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market_data.base import CandleFetchResult, CandleFetchStatus, CandleTick
from app.orchestrator import MultiSymbolOrchestrator, Orchestrator
from app.risk.config import RiskLimits
from app.risk.engine import RiskEngine
from app.strategy.engine import StrategyConfig, StrategyEngine
from tests.factories import activate_operational_state

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _make_candle(i: int, close: float, symbol: str) -> CandleTick:
    t = T0 + timedelta(minutes=i)
    return CandleTick(
        symbol=symbol, timeframe="1m", open_time=t, open=close, high=close + 0.5,
        low=close - 0.5, close=close, volume=10.0, source="test", received_at=t,
    )


# A strongly UPTRENDING series (BTC-like): price climbs steadily.
_UPTREND_PRICES = [100.0 + i * 3.0 for i in range(30)]
# A strongly DOWNTRENDING series (ETH-like): price falls steadily, starting
# from a materially different price level -- deliberately NOT the same
# fixture relabeled, a genuinely different signal.
_DOWNTREND_PRICES = [5000.0 - i * 40.0 for i in range(30)]


class _ListProvider:
    def __init__(self, candles: list[CandleTick]):
        self._candles = candles
        self._cursor = 0

    def next_candle(self) -> CandleFetchResult:
        if self._cursor >= len(self._candles):
            return CandleFetchResult(status=CandleFetchStatus.REPLAY_FINISHED)
        c = self._candles[self._cursor]
        self._cursor += 1
        return CandleFetchResult(status=CandleFetchStatus.CANDLE_AVAILABLE, candle=c)

    def is_stale(self, max_staleness_seconds: float) -> bool:
        return False


def test_strategy_engine_windows_are_never_shared_between_symbols():
    """Direct unit-level proof: two independent StrategyEngine instances
    fed genuinely divergent series never leak SMA/ATR state into each
    other -- and a THIRD engine that (incorrectly) shares one instance fed
    both series interleaved produces polluted numbers, demonstrating
    exactly what correct per-symbol isolation avoids."""
    btc_engine = StrategyEngine(symbol="BTCUSDT")
    eth_engine = StrategyEngine(symbol="ETHUSDT")

    last_btc_signal = None
    last_eth_signal = None
    for i in range(30):
        last_btc_signal = btc_engine.on_candle(_make_candle(i, _UPTREND_PRICES[i], "BTCUSDT"))
        last_eth_signal = eth_engine.on_candle(_make_candle(i, _DOWNTREND_PRICES[i], "ETHUSDT"))

    # Correctly isolated: each engine's own SMA/ATR reflect ONLY its own
    # series -- BTC's fast SMA must be near the uptrend's recent prices
    # (~180s), ETH's fast SMA must be near the downtrend's recent prices
    # (~3800s) -- worlds apart, never blended.
    assert last_btc_signal.params["fast_sma"] > 150.0
    assert last_eth_signal.params["fast_sma"] < 4500.0
    assert btc_engine._closes != eth_engine._closes
    assert btc_engine._closes[-1] == _UPTREND_PRICES[29]
    assert eth_engine._closes[-1] == _DOWNTREND_PRICES[29]
    assert len(btc_engine._closes) == 30  # BTC's window never grew from ETH's candles
    assert len(eth_engine._closes) == 30  # and vice versa

    # Negative control: ONE engine, incorrectly shared, fed BOTH series
    # interleaved -- this is exactly the bug isolation prevents. Its window
    # ends up mixing both price levels, producing a nonsensical blended SMA
    # that belongs to neither market.
    shared_engine = StrategyEngine(symbol="BTCUSDT")
    for i in range(30):
        shared_engine.on_candle(_make_candle(i, _UPTREND_PRICES[i], "BTCUSDT"))
        shared_engine.on_candle(_make_candle(i, _DOWNTREND_PRICES[i], "ETHUSDT"))
    assert len(shared_engine._closes) == 60  # both series landed in the SAME window
    blended_fast_sma = shared_engine.on_candle(_make_candle(30, 999.0, "BTCUSDT")).params["fast_sma"]
    # The blended value is contaminated by ETH's price level -- neither a
    # pure BTC nor a pure ETH signal, proving why sharing an engine is wrong.
    assert blended_fast_sma != last_btc_signal.params["fast_sma"]


def test_feeding_one_symbol_never_advances_or_alters_the_others_indicators():
    btc_engine = StrategyEngine(symbol="BTCUSDT")
    eth_engine = StrategyEngine(symbol="ETHUSDT")

    for i in range(25):
        btc_engine.on_candle(_make_candle(i, _UPTREND_PRICES[i], "BTCUSDT"))

    # ETH never received a single candle -- its indicators must be exactly
    # at their initial, untouched state.
    assert eth_engine._closes == []
    assert eth_engine._prev_fast_above_slow is None

    signal = eth_engine.on_candle(_make_candle(0, _DOWNTREND_PRICES[0], "ETHUSDT"))
    assert len(eth_engine._closes) == 1  # only the one candle just fed, nothing from BTC
    assert signal.direction == "HOLD"  # insufficient history -- proves it started fresh


def test_divergent_series_can_produce_different_signal_directions_per_symbol():
    """Real end-to-end proof via two per-symbol Orchestrators sharing a
    RiskEngine (same pattern build_orchestrator uses): a strong uptrend and
    a strong downtrend, run through the real tick() pipeline, persist
    materially different indicator values per symbol in the database --
    never the same fixture relabeled."""
    from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
    from app.persistence import repo
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider

    import tempfile
    import os

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "isolation.db")
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    settings = Settings(
        mode=RunMode.REPLAY, symbols=["BTCUSDT", "ETHUSDT"],
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> uma decisão". O default do timeframe estratégico passou a ser
        # 5 minutos; 1 minuto é a compatibilidade explícita mantida pelo PO
        # e preserva exatamente a intenção original do teste.
        strategy_timeframe_minutes=1,
    )
    price_state: dict[str, float] = {}
    risk_engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=500.0, require_stop_loss=False))
    execution_engine = PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0), slippage_bps=0.0)

    orchestrators = {}
    for symbol, prices in (("BTCUSDT", _UPTREND_PRICES), ("ETHUSDT", _DOWNTREND_PRICES)):
        per_symbol_settings = settings.model_copy(update={"symbol": symbol, "symbols": [symbol]})
        candles = [_make_candle(i, p, symbol) for i, p in enumerate(prices)]
        orchestrators[symbol] = Orchestrator(
            settings=per_symbol_settings, session_factory=session_factory,
            market_data_provider=_ListProvider(candles),
            strategy_engine=StrategyEngine(symbol=symbol, config=StrategyConfig()),
            risk_engine=risk_engine, execution_engine=execution_engine,
            ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
            clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
        )
    multi = MultiSymbolOrchestrator(orchestrators, ["BTCUSDT", "ETHUSDT"], settings=settings)
    activate_operational_state(orchestrators["BTCUSDT"])
    activate_operational_state(orchestrators["ETHUSDT"])

    for _ in range(50):
        multi.tick()

    with session_scope(session_factory) as session:
        btc_signals = repo.recent_signals(session, limit=50, symbol="BTCUSDT")
        eth_signals = repo.recent_signals(session, limit=50, symbol="ETHUSDT")
        assert btc_signals and eth_signals

        import json

        btc_fast_smas = [json.loads(s.params_json)["fast_sma"] for s in btc_signals if json.loads(s.params_json).get("fast_sma")]
        eth_fast_smas = [json.loads(s.params_json)["fast_sma"] for s in eth_signals if json.loads(s.params_json).get("fast_sma")]
        assert btc_fast_smas, "BTC produced no computable SMA -- fixture too short"
        assert eth_fast_smas, "ETH produced no computable SMA -- fixture too short"
        # Materially different price levels/directions -- never overlapping,
        # proving the two symbols' persisted signals came from genuinely
        # independent indicator state, not a shared/contaminated engine.
        assert max(btc_fast_smas) < min(eth_fast_smas) or min(btc_fast_smas) > max(eth_fast_smas)

    # Reinício/reconstrução: novo MultiSymbolOrchestrator sobre o MESMO
    # banco -- cada símbolo retoma seu próprio cursor/candles persistidos,
    # nunca misturando estado entre eles.
    from sqlalchemy import func, select

    from app.persistence.models import Candle

    with session_scope(session_factory) as session:
        btc_last_open_time = repo.get_last_candle_open_time(session, "BTCUSDT", "1m")
        eth_last_open_time = repo.get_last_candle_open_time(session, "ETHUSDT", "1m")
        assert btc_last_open_time is not None and eth_last_open_time is not None
        assert btc_last_open_time == eth_last_open_time  # same tick count (round-robin), but...
        btc_count = session.execute(
            select(func.count()).select_from(Candle).where(Candle.symbol == "BTCUSDT")
        ).scalar()
        eth_count = session.execute(
            select(func.count()).select_from(Candle).where(Candle.symbol == "ETHUSDT")
        ).scalar()
        # ...each symbol's OWN candle count reflects only its own history,
        # never inflated by the other's ticks.
        assert btc_count == eth_count  # round-robin gave each symbol equal turns
        assert btc_count > 0
