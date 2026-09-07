"""Fase 3.4.4 — a saúde shadow precisa ser legível TAMBÉM no multiativo.

Regressão de um defeito real, encontrado em produção na troca controlada
da 3.4.3: `routes_shadow.shadow_health` resolve o motor com
`getattr(orch, "shadow_engine", None)`, e o `MultiSymbolOrchestrator` não
expunha esse atributo. Resultado: na ÚNICA configuração que roda de
verdade (três símbolos), o endpoint respondia DESLIGADO enquanto a coleta
funcionava normalmente.

Os testes da 3.4.3 não pegaram isso porque usavam orquestradores de um
símbolo só.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes_shadow
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.orchestrator import MultiSymbolOrchestrator, Orchestrator
from app.persistence.db import session_scope
from app.persistence.models import (
    Execution,
    Order,
    Position,
    ShadowExperiment,
    ShadowOpportunity,
)
from app.shadow.engine import MODEL_BASELINE, MODEL_H2

TRES = "BTCUSDT,ETHUSDT,SOLUSDT"


def _cliente(tmp_path, nome, symbols):
    settings = Settings(mode=RunMode.REPLAY, symbols=symbols,
                        database_url=f"sqlite:///{tmp_path / nome}")
    orch = build_orchestrator(settings)
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_shadow.router, prefix="/api")
    return TestClient(app), orch


# =========================================================================
# A INSTÂNCIA COMPARTILHADA
# =========================================================================

def test_property_devolve_exatamente_a_instancia_injetada_nos_tres_filhos(tmp_path):
    _, orch = _cliente(tmp_path, "compartilhada.db", TRES)
    assert isinstance(orch, MultiSymbolOrchestrator)
    assert set(orch.orchestrators) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

    compartilhado = orch.orchestrators["BTCUSDT"].shadow_engine
    assert compartilhado is not None
    # identidade, não igualdade: um motor por instância, jamais um por símbolo
    for symbol, filho in orch.orchestrators.items():
        assert filho.shadow_engine is compartilhado, f"{symbol} tem outro motor"
    assert orch.shadow_engine is compartilhado

    # mesmo padrão das properties públicas que já existiam
    assert orch.execution_engine is orch.orchestrators["ETHUSDT"].execution_engine
    assert orch.risk_engine is orch.orchestrators["SOLUSDT"].risk_engine


def test_property_nao_cria_motor_novo_a_cada_acesso(tmp_path):
    _, orch = _cliente(tmp_path, "estavel.db", TRES)
    assert orch.shadow_engine is orch.shadow_engine
    primeiro = orch.shadow_engine
    orch.shadow_engine.record_failure(RuntimeError("marca"), "on_strategy_signal")
    # o estado sobrevive: é a MESMA instância, não uma cópia recriada
    assert orch.shadow_engine is primeiro
    assert orch.shadow_engine.failures == 1


# =========================================================================
# O ENDPOINT
# =========================================================================

def test_health_habilitado_no_monoativo(tmp_path):
    client, orch = _cliente(tmp_path, "mono.db", "BTCUSDT")
    assert type(orch) is Orchestrator
    corpo = client.get("/api/shadow/health").json()
    assert corpo["enabled"] is True
    assert corpo["status"] == "SAUDAVEL"
    assert corpo["blocks_operation"] is False


def test_health_habilitado_no_multiativo_com_tres_simbolos(tmp_path):
    """O caso que estava quebrado em produção."""
    client, orch = _cliente(tmp_path, "multi.db", TRES)
    assert isinstance(orch, MultiSymbolOrchestrator)

    corpo = client.get("/api/shadow/health").json()
    assert corpo["enabled"] is True, "o endpoint via DESLIGADO no multiativo"
    assert corpo["status"] == "SAUDAVEL"
    assert corpo["failures"] == 0
    assert corpo["consecutive_failures"] == 0
    assert corpo["last_error"] is None
    assert corpo["blocks_operation"] is False
    assert set(corpo["models"]) == {MODEL_BASELINE, MODEL_H2}
    assert corpo["h2_min_separation"] == 0.15
    assert "recovery_rule" in corpo


def test_health_degrada_e_recupera_por_operacao_no_multiativo(tmp_path):
    client, orch = _cliente(tmp_path, "degrada.db", TRES)
    motor = orch.shadow_engine

    motor.record_failure(ValueError("falha proposital"), "on_strategy_signal")
    corpo = client.get("/api/shadow/health").json()
    assert corpo["status"] == "DEGRADADO"
    assert corpo["failures"] == 1
    assert "falha proposital" in corpo["last_error"]
    assert corpo["last_failed_operation"] == "on_strategy_signal"
    assert corpo["consecutive_failures_by_operation"]["on_strategy_signal"] == 1
    assert corpo["blocks_operation"] is False, "saúde shadow nunca bloqueia"

    # sucesso de OUTRA operação não pode mascarar a que está quebrada
    motor.record_success("on_operational_candle")
    assert client.get("/api/shadow/health").json()["status"] == "DEGRADADO"

    # a operação certa recupera
    motor.record_success("on_strategy_signal")
    corpo = client.get("/api/shadow/health").json()
    assert corpo["status"] == "SAUDAVEL"
    assert corpo["failures"] == 1, "o cumulativo é histórico e não zera"
    assert corpo["last_error"] is not None


def test_sem_shadow_o_endpoint_responde_desligado_de_forma_coerente(tmp_path):
    """Configuração sem instrumentação: comportamento explícito e seguro,
    nunca uma exceção e nunca um motor inventado."""
    for nome, symbols in (("sem_mono.db", "BTCUSDT"), ("sem_multi.db", TRES)):
        client, orch = _cliente(tmp_path, nome, symbols)
        if isinstance(orch, MultiSymbolOrchestrator):
            for filho in orch.orchestrators.values():
                filho.shadow_engine = None
            assert orch.shadow_engine is None
        else:
            orch.shadow_engine = None

        corpo = client.get("/api/shadow/health").json()
        assert corpo["enabled"] is False
        assert corpo["status"] == "DESLIGADO"
        assert "detail" in corpo


# =========================================================================
# NADA MAIS PODE TER MUDADO
# =========================================================================

def test_demais_endpoints_shadow_continuam_funcionando_no_multiativo(tmp_path):
    client, _ = _cliente(tmp_path, "rotas.db", TRES)
    for rota in ("/api/shadow/opportunities", "/api/shadow/positions",
                 "/api/shadow/trades", "/api/shadow/experiments"):
        r = client.get(rota)
        assert r.status_code == 200, rota
        assert r.json() == [], rota

    m = client.get("/api/shadow/metrics")
    assert m.status_code == 200
    corpo = m.json()
    assert set(corpo["models"]) == {MODEL_BASELINE, MODEL_H2}
    for modelo in (MODEL_BASELINE, MODEL_H2):
        promocao = corpo["models"][modelo]["promotion"]
        assert promocao["promotion_eligible"] is False
        assert promocao["promotion_allowed_this_phase"] is False


def test_a_property_nao_altera_saude_operacional_nem_cria_ordens(tmp_path):
    """A correção é ADITIVA: ler a saúde shadow não pode mover uma vírgula
    do lado operacional."""
    client, orch = _cliente(tmp_path, "aditiva.db", TRES)

    for _ in range(30):
        orch.tick()
    degradado_antes = orch.engine_degraded

    for _ in range(5):
        assert client.get("/api/shadow/health").json()["enabled"] is True
        client.get("/api/shadow/metrics")

    assert orch.engine_degraded == degradado_antes

    with session_scope(orch.session_factory) as s:
        assert s.execute(select(Order)).scalars().all() == []
        assert s.execute(select(Execution)).scalars().all() == []
        assert s.execute(select(Position)).scalars().all() == []


def test_nenhuma_duplicacao_de_oportunidade_ou_experimento(tmp_path):
    """Um motor por instância, não um por símbolo: se a property tivesse
    criado motores separados, cada símbolo abriria o seu próprio
    experimento e a idempotência por candle deixaria de valer."""
    client, orch = _cliente(tmp_path, "sem_dup.db", TRES)
    for _ in range(60):
        orch.tick()
    for _ in range(3):
        client.get("/api/shadow/health")

    with session_scope(orch.session_factory) as s:
        exps = s.execute(select(ShadowExperiment)).scalars().all()
        # no máximo UM ativo por modelo, jamais um por símbolo
        for modelo in (MODEL_BASELINE, MODEL_H2):
            ativos = [e for e in exps if e.model == modelo and e.status == "ATIVO"]
            assert len(ativos) <= 1, f"{modelo} com {len(ativos)} experimentos ativos"

        ops = s.execute(select(ShadowOpportunity)).scalars().all()
        chaves = [(o.experiment_id, o.symbol, o.source_candle_open_time) for o in ops]
        assert len(chaves) == len(set(chaves)), "oportunidade duplicada"
