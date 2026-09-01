"""Fase 3 multiativo: `build_orchestrator` returns a plain `Orchestrator`
for monoativo (byte-for-byte backward compatible) and a
`MultiSymbolOrchestrator` for multiativo, both wired end-to-end through the
real startup path (session, reconciliation, DB) -- REPLAY only, no network.
"""
from __future__ import annotations

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.orchestrator import MultiSymbolOrchestrator, Orchestrator
from app.persistence.db import session_scope


def test_build_orchestrator_returns_plain_orchestrator_for_mono_symbol(tmp_path):
    settings = Settings(
        mode=RunMode.REPLAY, symbol="BTCUSDT", database_url=f"sqlite:///{tmp_path / 'mono.db'}",
        # Fase 3.2: um tick = uma decisão (compatibilidade explícita de 1
        # minuto); com o default de 5 minutos o primeiro tick devolveria
        # "aggregating", que é correto mas não é o que este teste verifica.
        strategy_timeframe_minutes=1,
    )
    orch = build_orchestrator(settings)
    assert type(orch) is Orchestrator
    assert orch.settings.symbol == "BTCUSDT"
    assert orch.settings.symbols == ["BTCUSDT"]

    result = orch.tick()
    assert result["status"] in ("hold", "rejected", "order_filled", "order_pending", "order_not_filled")


def test_build_orchestrator_returns_multi_symbol_orchestrator_for_multi_symbol(tmp_path):
    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT,ETHUSDT,SOLUSDT",
        database_url=f"sqlite:///{tmp_path / 'multi.db'}",
    )
    orch = build_orchestrator(settings)
    assert isinstance(orch, MultiSymbolOrchestrator)
    assert orch.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert set(orch.orchestrators) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    for symbol, sub_orch in orch.orchestrators.items():
        assert sub_orch.settings.symbol == symbol
        assert sub_orch.settings.symbols == [symbol]
        # Shared engine/session_factory across every symbol.
        assert sub_orch.execution_engine is orch.orchestrators["BTCUSDT"].execution_engine
        assert sub_orch.risk_engine is orch.orchestrators["BTCUSDT"].risk_engine
        assert sub_orch.session_factory is orch.orchestrators["BTCUSDT"].session_factory
        # NOT shared -- independent strategy state/provider per symbol.
        if symbol != "BTCUSDT":
            assert sub_orch.strategy_engine is not orch.orchestrators["BTCUSDT"].strategy_engine
            assert sub_orch.market_data_provider is not orch.orchestrators["BTCUSDT"].market_data_provider

    # A round of ticks round-robins across all three, each producing a
    # plausible per-tick status.
    seen = []
    for _ in range(3):
        result = orch.tick()
        seen.append(result["symbol"])
    assert seen == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_build_orchestrator_multi_symbol_shares_one_portfolio_session(tmp_path):
    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT,ETHUSDT",
        database_url=f"sqlite:///{tmp_path / 'session.db'}",
    )
    orch = build_orchestrator(settings)
    with session_scope(orch.session_factory) as session:
        from app.persistence import repo

        state = repo.get_or_create_system_state(session)
        assert state.active_session_id is not None
        from app.persistence.models import OperationalSession

        op_session = session.get(OperationalSession, state.active_session_id)
        assert op_session is not None
        import json

        assert json.loads(op_session.symbols) == ["BTCUSDT", "ETHUSDT"]
        # Genuinely multi-symbol -- legacy scalar column left NULL, never
        # lying with a single value (Fase 3 multiativo, item 2 do plano).
        assert op_session.symbol is None
