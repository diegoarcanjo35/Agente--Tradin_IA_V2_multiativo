"""Fase 2 v1.0 -- Estágio C: operational sessions (item 7.7) and the
INICIALIZANDO/OBSERVANDO/ATIVO/PAUSADO/BLOQUEADO/ENCERRANDO control states
(item 7.8) -- "process is running" kept separate from "strategy is
authorized to open new entries."
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control
from app.api.main import build_orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import OperationalSession
from app.risk.config import RiskLimits
from app.risk.engine import RiskEngine
from tests.factories import activate_operational_state, base_risk_context
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import (
    _generate_kline_rows,
    _KlineSequenceTransport,
    make_bybit_demo_settings,
)


def make_client(orch):
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.include_router(routes_control.router, prefix="/api")
    return TestClient(app)


# --- Item 7.8: never auto-activates -----------------------------------------

def test_fresh_startup_comes_up_observando_never_ativo():
    settings = make_bybit_demo_settings()
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.operational_state == "OBSERVANDO"


def test_activate_endpoint_moves_observando_to_ativo_and_allows_entry(tmp_path):
    # A real sqlite FILE (not ":memory:") is required whenever a TestClient
    # is involved -- it runs requests in FastAPI's threadpool, which opens a
    # fresh connection per call; an in-memory DB isn't shared across those.
    settings = make_bybit_demo_settings(
        risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0,
        database_url=f"sqlite:///{tmp_path / 'activate_entry.db'}",
    )
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=10)
    transport = _KlineSequenceTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    client = make_client(orch)

    # Fase 3.3.1: a ativação passou a exigir carteira pronta -- aquecimento
    # concluído e série no presente. Drenar a perna de baixa antes de ativar
    # é o que um operador realmente faria (e o que a V2 fez em produção):
    # deixar o motor aquecer e só então autorizar entradas. O cruzamento de
    # alta continua na perna seguinte, ainda por processar.
    # 32 candles: aquece o motor E deixa a série no presente (o último
    # candle drenado fecha ~3 min atrás, dentro da janela real de 300 s).
    # O cruzamento de alta está no candle 33 -- ainda por processar,
    # exatamente como antes.
    for _ in range(32):
        orch.tick()

    resp = client.post("/api/operational-state/activate")
    assert resp.status_code == 200
    assert resp.json()["operational_state"] == "ATIVO"

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.operational_state == "ATIVO"
        assert state.active_session_id is not None
        op_session = session.get(OperationalSession, state.active_session_id)
        assert op_session.status == "ATIVO"

    result = None
    for _ in range(len(rows) + 2):
        result = orch.tick()
        if result["status"] == "order_filled":
            break
    assert result["status"] == "order_filled"


def test_activate_refused_while_trading_blocked(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'blocked.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    client = make_client(orch)

    client.post("/api/kill-switch/engage")

    resp = client.post("/api/operational-state/activate")
    body = resp.json()
    assert body["operational_state"] != "ATIVO"
    assert "bloqueadas" in body["mensagem"].lower()

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.operational_state == "BLOQUEADO"


def test_activate_refused_before_initial_reconciliation_completes(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'not_reconciled.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        # Simulate "never actually reconciled" despite otherwise looking fine.
        state.initialization_not_reconciled = True

    client = make_client(orch)
    resp = client.post("/api/operational-state/activate")
    body = resp.json()
    assert body["operational_state"] != "ATIVO"
    assert "reconciliação inicial" in body["mensagem"].lower()


def test_pause_always_allowed_blocks_only_new_entries_not_closes(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'pause.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    client = make_client(orch)

    client.post("/api/operational-state/activate")
    resp = client.post("/api/operational-state/pause")  # no auth header, no local-only check bypass needed
    assert resp.status_code == 200
    assert resp.json()["operational_state"] == "PAUSADO"

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.operational_state == "PAUSADO"

    engine = RiskEngine(RiskLimits())
    entry_result = engine.evaluate(
        __import__("app.strategy.schemas", fromlist=["Signal"]).Signal(
            symbol="BTCUSDT", direction="BUY", justification="teste", created_at=base_risk_context().now,
            observed_price=100.0, atr=1.0, source_candle_open_time=base_risk_context().now,
            stop_loss=90.0, take_profit=110.0, params={},
        ),
        signal_id=1, context=base_risk_context(operational_state="PAUSADO"),
    )
    assert entry_result.approved is False

    close_result = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=0.001,
        position_exists=True, position_qty=0.001, position_side="BUY",
        context=base_risk_context(operational_state="PAUSADO"),
    )
    assert close_result.approved is True  # closes always continue while paused


# --- Item 7.7: session resumption -------------------------------------------

def test_session_created_at_startup_and_resumed_after_restart(tmp_path):
    db_path = tmp_path / "session_resume_test.db"
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{db_path}")

    orch_a = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    with session_scope(orch_a.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        first_session_id = state.active_session_id
        first_session_uid = session.get(OperationalSession, first_session_id).session_uid

    # "Restart": a brand-new build_orchestrator() call against the SAME db.
    orch_b = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    with session_scope(orch_b.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.active_session_id == first_session_id  # resumed, not a new row
        op_session = session.get(OperationalSession, state.active_session_id)
        assert op_session.session_uid == first_session_uid

        all_sessions = session.execute(
            __import__("sqlalchemy").select(OperationalSession)
        ).scalars().all()
        assert len(all_sessions) == 1  # never created a second row for the same mode+symbol


# --- Item 7.5: independent block causes (extended for the new Fase 2 flags) -

def test_session_counters_increment_as_a_real_tick_progresses():
    settings = make_bybit_demo_settings(risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0)
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=10)
    transport = _KlineSequenceTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    activate_operational_state(orch)

    result = None
    for _ in range(len(rows) + 2):
        result = orch.tick()
        if result["status"] == "order_filled":
            break
    assert result["status"] == "order_filled"

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        op_session = session.get(OperationalSession, state.active_session_id)
        assert op_session.candles_count > 0
        assert op_session.signals_count > 0
        assert op_session.approvals_count >= 1
        assert op_session.orders_count >= 1
        assert op_session.fills_count >= 1
        assert op_session.reconciliations_count >= 1  # at least the startup reconciliation


def test_all_new_block_causes_are_independent_of_each_other():
    from app.persistence.models import SystemState

    state = SystemState(id=1)
    state.kill_switch_engaged = False
    state.state_ambiguous = False
    state.clock_out_of_sync = False
    state.api_failure_count = 0
    state.reconciliation_diverged = True
    state.order_state_unknown = True

    repo.recompute_trading_blocked(state, max_api_failures=5)
    assert state.trading_blocked is True
    assert "divergência" in state.block_reason.lower()
    assert "unknown" in state.block_reason.lower()

    # Clearing ONE cause never clears the other.
    state.reconciliation_diverged = False
    repo.recompute_trading_blocked(state, max_api_failures=5)
    assert state.trading_blocked is True  # order_state_unknown still active
    assert "divergência" not in state.block_reason.lower()
    assert "unknown" in state.block_reason.lower()

    state.order_state_unknown = False
    repo.recompute_trading_blocked(state, max_api_failures=5)
    assert state.trading_blocked is False
