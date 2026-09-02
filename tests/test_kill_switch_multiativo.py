"""Fase 3.1.1 (correção final da auditoria do PO, item 2): `POST
/api/kill-switch/engage` acessava `orch._active_session`, um método
PRIVADO que só existe em `Orchestrator` -- `AttributeError` real sob
`MultiSymbolOrchestrator` (multiativo), encontrado durante a validação
visual manual da Fase 3.1.1. Corrigido com uma interface pública comum
(`repo.get_active_session`) e uma nova property `execution_engine` em
`MultiSymbolOrchestrator` (mesmo padrão de `session_factory`/
`funding_provider`/`price_state`, já que o motor de execução é uma
instância ÚNICA compartilhada entre todos os símbolos -- ver
app/api/main.py::build_orchestrator).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control, routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.orchestrator import MultiSymbolOrchestrator, Orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from tests.factories import activate_operational_state, make_portfolio_temporally_ready


def _make_client(tmp_path, name, symbols):
    settings = Settings(mode=RunMode.REPLAY, symbols=symbols, database_url=f"sqlite:///{tmp_path / name}")
    orch = build_orchestrator(settings)
    activate_operational_state(orch)

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_control.router, prefix="/api")
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app), orch


def test_engage_monoativo_still_works(tmp_path):
    """Regressão: o comportamento monoativo (já existente) nunca muda."""
    client, orch = _make_client(tmp_path, "kill_mono.db", "BTCUSDT")
    assert isinstance(orch, Orchestrator)

    resp = client.post("/api/kill-switch/engage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kill_switch_engaged"] is True
    assert body["trading_blocked"] is True


def test_engage_multiativo_no_longer_raises_attribute_error(tmp_path):
    """O teste central desta correção: multiativo genuíno (>1 símbolo),
    nunca mais um AttributeError."""
    client, orch = _make_client(tmp_path, "kill_multi.db", "BTCUSDT,ETHUSDT")
    assert isinstance(orch, MultiSymbolOrchestrator)

    resp = client.post("/api/kill-switch/engage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kill_switch_engaged"] is True
    assert body["trading_blocked"] is True


def test_multiativo_execution_engine_is_the_shared_instance(tmp_path):
    client, orch = _make_client(tmp_path, "kill_exec_engine.db", "BTCUSDT,ETHUSDT")
    assert orch.execution_engine is next(iter(orch.orchestrators.values())).execution_engine
    # A mesma instância para TODOS os símbolos -- nunca uma cópia por símbolo.
    engines = {o.execution_engine for o in orch.orchestrators.values()}
    assert len(engines) == 1


def test_engage_blocks_new_entries_for_every_symbol_in_multiativo(tmp_path):
    """O bloqueio é de estado global (SystemState.trading_blocked) -- nunca
    por símbolo -- então engajar o kill switch bloqueia igualmente
    BTCUSDT e ETHUSDT."""
    client, orch = _make_client(tmp_path, "kill_multi_blocks_all.db", "BTCUSDT,ETHUSDT")
    client.post("/api/kill-switch/engage")

    for _ in range(5):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        from app.persistence.models import Position

        assert repo.open_positions(session, "BTCUSDT") == []
        assert repo.open_positions(session, "ETHUSDT") == []


def test_engage_persists_global_state(tmp_path):
    client, orch = _make_client(tmp_path, "kill_persist.db", "BTCUSDT,ETHUSDT")
    client.post("/api/kill-switch/engage")

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.kill_switch_engaged is True
        assert state.trading_blocked is True


def test_engage_called_twice_is_idempotent(tmp_path):
    """Chamada repetida: sempre reengajado, sempre 200, nunca um erro por
    já estar engajado."""
    client, orch = _make_client(tmp_path, "kill_idempotent.db", "BTCUSDT,ETHUSDT")
    r1 = client.post("/api/kill-switch/engage")
    r2 = client.post("/api/kill-switch/engage")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["trading_blocked"] is True
    assert r2.json()["trading_blocked"] is True

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.kill_switch_engaged is True


def test_disengage_after_multiativo_engage_releases_trading(tmp_path):
    client, orch = _make_client(tmp_path, "kill_multi_disengage.db", "BTCUSDT,ETHUSDT")
    client.post("/api/kill-switch/engage")

    resp = client.post("/api/kill-switch/disengage")
    body = resp.json()
    assert body["kill_switch_engaged"] is False
    assert body["trading_blocked"] is False


def test_operational_state_activate_pause_unaffected_by_multiativo(tmp_path):
    """Regressão: as outras rotas de controle (nunca tocadas nesta
    correção) continuam funcionando normalmente sob multiativo -- elas já
    usavam apenas o SystemState/OperationalSession globais, nunca um
    atributo privado do orquestrador."""
    client, orch = _make_client(tmp_path, "kill_other_routes.db", "BTCUSDT,ETHUSDT")
    # Fase 3.3.1: ativar exige agora carteira pronta -- aquecimento
    # concluído, saúde SAUDÁVEL, sem gap (e, em modos com dado ao vivo,
    # série no presente). Um orquestrador que nunca ticou não atende
    # nenhum desses, corretamente. O helper representa um sistema que
    # já estava rodando; nenhuma asserção do teste muda.
    make_portfolio_temporally_ready(orch)

    pause_resp = client.post("/api/operational-state/pause")
    assert pause_resp.status_code == 200
    assert pause_resp.json()["operational_state"] == "PAUSADO"

    activate_resp = client.post("/api/operational-state/activate")
    assert activate_resp.status_code == 200
    assert activate_resp.json()["operational_state"] == "ATIVO"


def test_portfolio_summary_still_works_after_engage_in_multiativo(tmp_path):
    """Garantia adicional de não-regressão: as rotas novas da Fase 3.1.1
    (portfolio-summary) continuam respondendo normalmente depois do
    kill-switch ser engajado sob multiativo."""
    client, orch = _make_client(tmp_path, "kill_portfolio_summary.db", "BTCUSDT,ETHUSDT")
    client.post("/api/kill-switch/engage")
    resp = client.get("/api/portfolio-summary")
    assert resp.status_code == 200
