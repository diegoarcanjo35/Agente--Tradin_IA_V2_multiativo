"""Correção Operacional do Poll Loop v1.0: `POST /api/operational-state/activate`
deve recusar ativar novas entradas se o motor de mercado estiver
DEGRADADO/PARADO ou com heartbeat vencido -- nunca confiar só no fato de o
próprio endpoint HTTP ter respondido.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control
from app.api.poll_engine import PollEngineStatus, PollHealth
from app.core.clock import utcnow
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import make_bybit_demo_settings
from app.api.main import build_orchestrator


def _make_client(orch, poll_health: PollHealth | None):
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    if poll_health is not None:
        app.state.poll_health = poll_health
    app.include_router(routes_control.router, prefix="/api")
    return TestClient(app)


def test_activation_refused_when_engine_degraded(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'gate_degraded.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    health = PollHealth(status=PollEngineStatus.DEGRADADO)
    client = _make_client(orch, health)

    resp = client.post("/api/operational-state/activate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["operational_state"] != "ATIVO"
    assert "motor de mercado" in body["mensagem"].lower()

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.operational_state != "ATIVO"


def test_activation_refused_when_engine_parado(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'gate_parado.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    health = PollHealth(status=PollEngineStatus.PARADO)
    client = _make_client(orch, health)

    resp = client.post("/api/operational-state/activate")
    assert resp.status_code == 200
    assert resp.json()["operational_state"] != "ATIVO"


def test_activation_refused_when_heartbeat_expired_even_if_status_says_saudavel(tmp_path):
    """Mesmo que `status` ainda diga SAUDAVEL (uma checagem de heartbeat
    pode não ter rodado ainda), um heartbeat vencido por si só já deve
    bloquear a ativação."""
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'gate_heartbeat.db'}",
        poll_heartbeat_max_age_seconds=1.0,
    )
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    health = PollHealth(
        status=PollEngineStatus.SAUDAVEL,
        poll_last_success_at=utcnow() - timedelta(seconds=120),
    )
    client = _make_client(orch, health)

    resp = client.post("/api/operational-state/activate")
    assert resp.status_code == 200
    assert resp.json()["operational_state"] != "ATIVO"


def test_activation_allowed_when_engine_saudavel(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'gate_saudavel.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    health = PollHealth(status=PollEngineStatus.SAUDAVEL, poll_last_success_at=utcnow())
    client = _make_client(orch, health)

    resp = client.post("/api/operational-state/activate")
    assert resp.status_code == 200
    assert resp.json()["operational_state"] == "ATIVO"


def test_activation_allowed_when_no_poll_health_wired_at_all(tmp_path):
    """Regressão: apps de teste minimalistas que nunca definem
    `app.state.poll_health` (a maioria da suíte pré-existente) continuam
    funcionando exatamente como antes -- o gate é pulado, não um erro."""
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'gate_no_health.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    client = _make_client(orch, poll_health=None)

    resp = client.post("/api/operational-state/activate")
    assert resp.status_code == 200
    assert resp.json()["operational_state"] == "ATIVO"


def test_closing_a_position_is_never_blocked_by_a_degraded_engine(session_factory):
    """Correção item 2 (entry-only gate): RiskEngine.evaluate_close() nunca
    verifica engine_degraded -- reduzir/fechar exposição continua liberado
    mesmo com o motor degradado."""
    from app.risk.config import RiskLimits
    from app.risk.engine import RiskContext, RiskEngine

    engine = RiskEngine(RiskLimits())
    context = RiskContext(
        open_positions_count=1, open_exposure_usd=10.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        state_ambiguous=False, cooldown_until=None, now=utcnow(),
        engine_degraded=True,
    )
    result = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=0.01,
        position_exists=True, position_qty=0.01, position_side="BUY",
        context=context,
    )
    assert result.approved is True


def test_opening_a_new_position_is_rejected_when_engine_degraded(session_factory):
    from app.risk.config import RiskLimits
    from app.risk.engine import RiskContext, RiskEngine
    from app.strategy.engine import Signal
    from app.core.clock import utcnow as _utcnow

    engine = RiskEngine(RiskLimits(max_position_usd=100.0, max_total_exposure_usd=100.0, require_stop_loss=False))
    signal = Signal(
        symbol="BTCUSDT", direction="BUY", justification="teste",
        created_at=_utcnow(), observed_price=100.0, atr=1.0,
        source_candle_open_time=_utcnow(),
        stop_loss=90.0, take_profit=110.0, params={},
    )
    context = RiskContext(
        open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        state_ambiguous=False, cooldown_until=None, now=_utcnow(),
        operational_state="ATIVO", engine_degraded=True,
    )
    result = engine.evaluate(signal, signal_id=1, context=context)
    assert result.approved is False
    assert "motor de mercado" in result.reason.lower()
