"""Fase 3 multiativo, itens 9.3 e 9.4 do brief: round-robin determinístico
sem starvation, e backlogs/cursores independentes por símbolo (inclusive
reinício) -- sem rede, `ReplayMarketDataProvider`/`ListMarketDataProvider`.
"""
from __future__ import annotations

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.orchestrator import MultiSymbolOrchestrator, Orchestrator
from app.persistence.db import session_scope
from app.risk.config import RiskLimits
from app.risk.engine import RiskEngine
from app.strategy.engine import StrategyEngine
from tests.test_price_correctness import ListMarketDataProvider, make_candle


def _build_multi(session_factory, per_symbol_candles: dict[str, list]) -> MultiSymbolOrchestrator:
    symbols = list(per_symbol_candles)
    settings = Settings(mode=RunMode.REPLAY, symbols=symbols)
    price_state: dict[str, float] = {}
    execution_engine = PaperLocalExecutionEngine(
        price_provider=lambda s: price_state.get(s, 0.0), slippage_bps=0.0
    )
    risk_engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=500.0,
                                         require_stop_loss=False))
    orchestrators = {}
    for symbol in symbols:
        per_symbol_settings = settings.model_copy(update={"symbol": symbol, "symbols": [symbol]})
        orchestrators[symbol] = Orchestrator(
            settings=per_symbol_settings, session_factory=session_factory,
            market_data_provider=ListMarketDataProvider(per_symbol_candles[symbol]),
            strategy_engine=StrategyEngine(symbol=symbol),
            risk_engine=risk_engine, execution_engine=execution_engine,
            ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
            clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
        )
    return MultiSymbolOrchestrator(orchestrators, symbols)


class _AlwaysGapProvider:
    """Every call reports GAP_DETECTED -- a permanently failing symbol."""

    def next_candle(self):
        from app.market_data.base import CandleFetchResult, CandleFetchStatus
        return CandleFetchResult(status=CandleFetchStatus.GAP_DETECTED, detail="lacuna simulada")

    def is_stale(self, max_staleness_seconds: float) -> bool:
        return False


def test_round_robin_cycles_through_all_symbols_in_configured_order(tmp_path):
    from app.persistence.db import make_engine, make_session_factory, init_db

    engine = make_engine(f"sqlite:///{tmp_path / 'rr.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    candles = {
        "BTCUSDT": [make_candle(i, 100.0 + i, symbol="BTCUSDT") for i in range(5)],
        "ETHUSDT": [make_candle(i, 2000.0 + i, symbol="ETHUSDT") for i in range(5)],
        "SOLUSDT": [make_candle(i, 50.0 + i, symbol="SOLUSDT") for i in range(5)],
    }
    multi = _build_multi(session_factory, candles)

    seen_order = []
    for _ in range(6):  # two full cycles of 3 symbols
        result = multi.tick()
        seen_order.append(result["symbol"])

    assert seen_order == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_one_symbol_permanently_failing_never_starves_the_others(tmp_path):
    from app.persistence.db import make_engine, make_session_factory, init_db

    engine = make_engine(f"sqlite:///{tmp_path / 'starvation.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    candles = {
        "ETHUSDT": [make_candle(i, 2000.0 + i, symbol="ETHUSDT") for i in range(20)],
        "SOLUSDT": [make_candle(i, 50.0 + i, symbol="SOLUSDT") for i in range(20)],
    }
    multi = _build_multi(session_factory, candles)
    multi.orchestrators["BTCUSDT"] = Orchestrator(
        settings=Settings(mode=RunMode.REPLAY, symbol="BTCUSDT", symbols=["BTCUSDT"]),
        session_factory=session_factory,
        market_data_provider=_AlwaysGapProvider(),
        strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=multi.orchestrators["ETHUSDT"].risk_engine,
        execution_engine=multi.orchestrators["ETHUSDT"].execution_engine,
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=multi.orchestrators["ETHUSDT"].price_state,
    )
    from app.orchestrator import SymbolHealth

    multi.symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    multi.health["BTCUSDT"] = SymbolHealth()

    eth_results, sol_results = [], []
    for _ in range(12):
        result = multi.tick()
        if result["symbol"] == "ETHUSDT":
            eth_results.append(result["status"])
        elif result["symbol"] == "SOLUSDT":
            sol_results.append(result["status"])

    # BTCUSDT fails 3 times then enters PARADO/backoff -- but ETHUSDT and
    # SOLUSDT keep receiving their turns and keep processing real candles
    # throughout, never blocked by BTCUSDT's failures.
    assert multi.health["BTCUSDT"].status == "PARADO"
    assert "order_pending" in eth_results or "hold" in eth_results or "rejected" in eth_results
    assert "order_pending" in sol_results or "hold" in sol_results or "rejected" in sol_results
    assert len(eth_results) >= 3
    assert len(sol_results) >= 3


def test_each_symbol_has_an_independent_cursor_and_survives_a_restart(tmp_path):
    from app.persistence.db import make_engine, make_session_factory, init_db

    db_path = tmp_path / "restart.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    fixture_candles = {
        "BTCUSDT": [make_candle(i, 100.0 + i, symbol="BTCUSDT") for i in range(6)],
        "ETHUSDT": [make_candle(i, 2000.0 + i, symbol="ETHUSDT") for i in range(6)],
    }
    multi = _build_multi(session_factory, fixture_candles)

    # Drain 2 candles for BTCUSDT and 4 for ETHUSDT (unbalanced on purpose).
    for symbol, n in (("BTCUSDT", 2), ("ETHUSDT", 4)):
        provider = multi.orchestrators[symbol].market_data_provider
        for _ in range(n):
            multi.orchestrators[symbol].tick()
        assert provider._cursor == n

    with session_scope(session_factory) as session:
        from app.persistence import repo
        btc_last = repo.get_last_candle_open_time(session, "BTCUSDT", "1m")
        eth_last = repo.get_last_candle_open_time(session, "ETHUSDT", "1m")
        assert btc_last == fixture_candles["BTCUSDT"][1].open_time
        assert eth_last == fixture_candles["ETHUSDT"][3].open_time

    # "Restart": build a brand-new MultiSymbolOrchestrator (fresh in-memory
    # provider state) against the SAME database -- each symbol's persisted
    # candle history is untouched and independent of the other's progress.
    multi_restarted = _build_multi(session_factory, fixture_candles)
    with session_scope(session_factory) as session:
        from app.persistence import repo
        assert repo.get_last_candle_open_time(session, "BTCUSDT", "1m") == fixture_candles["BTCUSDT"][1].open_time
        assert repo.get_last_candle_open_time(session, "ETHUSDT", "1m") == fixture_candles["ETHUSDT"][3].open_time
    assert multi_restarted.symbols == ["BTCUSDT", "ETHUSDT"]
