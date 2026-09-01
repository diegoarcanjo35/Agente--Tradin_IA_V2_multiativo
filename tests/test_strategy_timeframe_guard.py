"""Fase 3.2 (item 10 da decisão do PO): mudar `STRATEGY_TIMEFRAME_MINUTES`
com qualquer posição aberta na carteira interrompe a inicialização.

Uma posição aberta sob uma cadência temporal não pode passar
silenciosamente a ser administrada por outra: stop e alvo foram
dimensionados pelo ATR daquele timeframe.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.core.errors import StrategyTimeframeChangeBlockedError
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import OperationalSession
from app.sessions import resolve_accounting_base
from tests.factories import activate_operational_state


def _build(tmp_path, db_name, minutes=5, symbols="BTCUSDT"):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols,
        database_url=f"sqlite:///{tmp_path / db_name}",
        strategy_timeframe_minutes=minutes,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    return orch


def _open_a_position(orch, symbol="BTCUSDT"):
    """Abre uma posição REAL pelo caminho de produção (fill_service), a
    partir da cadeia Signal -> RiskEvaluation -> Order."""
    from app.execution import fill_service
    from app.execution.base import FillEvent, OrderStatusSnapshot
    from app.execution.order_state import OrderStatus

    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, symbol, "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "ok", {})
        order = repo.save_order(
            session, idempotency_key=f"guard-{symbol}", risk_evaluation_id=risk_eval.id,
            symbol=symbol, side="BUY", qty=1.0, stop_loss=90.0, take_profit=130.0,
            mode="REPLAY", reference_price=100.0,
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = "EX-guard"
        state = repo.get_or_create_system_state(session)
        fill_service.apply_order_snapshot(
            session, state, None, order,
            OrderStatusSnapshot(
                exchange_order_id="EX-guard", status=OrderStatus.FILLED,
                fills=[FillEvent("F-guard", 1.0, 100.0, 0.0)],
            ),
            is_close=False, max_api_failures=5,
        )


def _sessions_of(orch):
    with session_scope(orch.session_factory) as session:
        rows = session.execute(
            select(OperationalSession).order_by(OperationalSession.started_at)
        ).scalars().all()
        return [(r.id, r.timeframe, r.ended_at) for r in rows]


def test_changing_the_timeframe_with_an_open_position_blocks_startup(tmp_path):
    db = "guard_blocked.db"
    orch = _build(tmp_path, db, minutes=5)
    _open_a_position(orch)
    before = _sessions_of(orch)

    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / db}", strategy_timeframe_minutes=15,
    )
    with pytest.raises(StrategyTimeframeChangeBlockedError) as exc:
        build_orchestrator(settings)

    message = str(exc.value)
    assert "STRATEGY_TIMEFRAME_MINUTES" in message
    assert "5m" in message and "15m" in message
    assert "BTCUSDT" in message
    assert "Nenhuma alteração foi feita" in message

    # Nenhum estado parcial: a sessão anterior segue aberta, nenhuma nova
    # foi criada.
    assert _sessions_of(orch) == before
    assert before[-1][2] is None  # ended_at continua None


def test_the_guard_checks_every_symbol_not_only_the_first(tmp_path):
    db = "guard_multi.db"
    orch = _build(tmp_path, db, minutes=5, symbols="BTCUSDT,ETHUSDT")
    _open_a_position(orch, "ETHUSDT")  # posição aberta apenas no SEGUNDO símbolo

    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT,ETHUSDT",
        database_url=f"sqlite:///{tmp_path / db}", strategy_timeframe_minutes=15,
    )
    with pytest.raises(StrategyTimeframeChangeBlockedError) as exc:
        build_orchestrator(settings)
    assert "ETHUSDT" in str(exc.value)


def test_changing_the_timeframe_without_open_positions_is_allowed(tmp_path):
    db = "guard_allowed.db"
    orch_a = _build(tmp_path, db, minutes=5)
    with session_scope(orch_a.session_factory) as session:
        assert repo.open_positions(session) == []

    orch_b = _build(tmp_path, db, minutes=15)
    sessions = _sessions_of(orch_b)
    assert len(sessions) == 2                 # sessão operacional NOVA
    assert sessions[0][2] is not None         # a anterior foi encerrada
    assert sessions[0][1] == "5m" and sessions[1][1] == "15m"


def test_a_timeframe_change_never_creates_a_new_accounting_base(tmp_path):
    """A base contábil só nasce quando `paper_starting_balance_usd` muda --
    trocar de timeframe é, para a contabilidade, igual a trocar de
    estratégia."""
    db = "guard_base.db"
    orch_a = _build(tmp_path, db, minutes=5)
    with session_scope(orch_a.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        base_a, _ = resolve_accounting_base(session, repo.get_active_session(session, state))

    orch_b = _build(tmp_path, db, minutes=15)
    with session_scope(orch_b.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active = repo.get_active_session(session, state)
        base_b, base_session = resolve_accounting_base(session, active)

    assert base_b == base_a                  # MESMA base contábil
    assert base_session.id != active.id      # embora a sessão seja outra


def test_equity_stays_continuous_across_a_timeframe_change(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_dashboard

    def _client(orch):
        app = FastAPI()
        app.state.orchestrator = orch
        app.state.settings = orch.settings
        app.state.replay_done = False
        app.include_router(routes_dashboard.router, prefix="/api")
        return TestClient(app)

    db = "guard_equity.db"
    orch_a = _build(tmp_path, db, minutes=5)
    before = _client(orch_a).get("/api/portfolio-summary").json()["portfolio"]

    orch_b = _build(tmp_path, db, minutes=15)
    after = _client(orch_b).get("/api/portfolio-summary").json()["portfolio"]

    assert after["equity"] == before["equity"]
    assert after["starting_balance"] == before["starting_balance"]
    assert after["accounting_base_started_at"] == before["accounting_base_started_at"]


def test_restarting_without_changing_the_timeframe_is_allowed_with_open_positions(tmp_path):
    db = "guard_restart.db"
    orch_a = _build(tmp_path, db, minutes=5)
    _open_a_position(orch_a)

    orch_b = _build(tmp_path, db, minutes=5)  # mesmo timeframe -- permitido
    with session_scope(orch_b.session_factory) as session:
        assert len(repo.open_positions(session)) == 1
    assert len(_sessions_of(orch_b)) == 1  # sessão RETOMADA, não recriada


def test_other_strategy_changes_are_not_guarded(tmp_path):
    """A guarda é deliberadamente restrita ao timeframe: o PO pediu
    explicitamente para NÃO ampliá-la para toda mudança de estratégia."""
    db = "guard_scope.db"
    orch_a = _build(tmp_path, db, minutes=5)
    _open_a_position(orch_a)

    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / db}", strategy_timeframe_minutes=5,
        strategy_fast_period=7,  # muda a estratégia, NÃO o timeframe
    )
    orch_b = build_orchestrator(settings)  # não levanta
    assert orch_b.strategy_engine.config.fast_period == 7
    assert len(_sessions_of(orch_b)) == 2  # sessão nova, sem bloqueio


def test_legacy_session_without_the_field_does_not_trigger_the_guard(tmp_path):
    """Sessão criada antes deste campo existir não tem valor anterior
    contra o qual comparar -- a guarda não dispara em vez de inventar uma
    comparação."""
    import json as json_module

    db = "guard_legacy.db"
    orch_a = _build(tmp_path, db, minutes=5)
    _open_a_position(orch_a)
    with session_scope(orch_a.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active = repo.get_active_session(session, state)
        snapshot = json_module.loads(active.config_snapshot_json)
        del snapshot["strategy_timeframe_minutes"]
        active.config_snapshot_json = json_module.dumps(snapshot)

    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / db}", strategy_timeframe_minutes=15,
    )
    build_orchestrator(settings)  # não levanta
