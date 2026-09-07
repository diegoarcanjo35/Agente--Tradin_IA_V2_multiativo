"""Fase 3.4.3 (auditoria) — isolamento transacional por SAVEPOINT,
identidade imutável do experimento e diferencial baseline x motor real.

O teste central aqui é adversarial: uma violação REAL de constraint
durante o `flush` shadow não pode abortar a persistência operacional do
mesmo tick.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.orchestrator import Orchestrator
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import (
    Candle,
    Order,
    Position,
    ShadowExperiment,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
    StrategySignal,
)
from app.risk.config import RiskLimits
from app.risk.engine import RiskEngine
from app.shadow.engine import (
    MODEL_BASELINE,
    MODEL_H2,
    MODELS,
    ShadowEngine,
    ShadowLimits,
)
from app.shadow.metrics import comparative, model_metrics, promotion_status
from app.strategy.engine import StrategyEngine
from tests.test_price_correctness import ListMarketDataProvider, make_candle

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
PRECO, ATR = 100.0, 5.0


@pytest.fixture()
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'iso.db'}")
    init_db(eng)
    return make_session_factory(eng)


def _sinal(e, s, sym="BTCUSDT", direcao="BUY", sep=0.30, t=None, **kw):
    slow = PRECO
    fast = slow + sep * ATR if direcao == "BUY" else slow - sep * ATR
    e.on_strategy_signal(s, sym, direcao, PRECO, ATR, fast, slow, t or T0, **kw)


def _orquestrador(tmp_path, shadow, nome="iso"):
    db = f"sqlite:///{tmp_path / (nome + '.db')}"
    eng = make_engine(db)
    init_db(eng)
    f = make_session_factory(eng)
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=Settings(mode=RunMode.REPLAY, symbols="BTCUSDT", database_url=db,
                          strategy_timeframe_minutes=1),
        session_factory=f,
        market_data_provider=ListMarketDataProvider(
            [make_candle(i, 100.0 + (i % 7)) for i in range(60)]),
        strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(
            price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0),
        price_state=price_state, shadow_engine=shadow,
    )
    return orch, f


# =========================================================================
# 1. ISOLAMENTO TRANSACIONAL POR SAVEPOINT
# =========================================================================

class _ShadowViolaConstraint(ShadowEngine):
    """Viola uma constraint REAL no flush: grava duas oportunidades com a
    mesma chave única (model, symbol, source_candle_open_time)."""

    def on_strategy_signal(self, session, symbol, direction, reference_price,
                           atr, fast_sma, slow_sma, source_candle_open_time, **kw):
        exp = self.experiment_id(session, MODEL_BASELINE, source_candle_open_time)
        for _ in range(2):
            session.add(ShadowOpportunity(
                experiment_id=exp, model=MODEL_BASELINE, hypothesis_version="x",
                symbol=symbol, source_candle_open_time=source_candle_open_time,
                strategy_timeframe_minutes=1, direction=direction,
                reference_price=reference_price, fast_sma=1.0, slow_sma=1.0,
                atr=max(atr or 1.0, 1e-9), normalized_separation=0.0,
                approved=False, reason="colisao proposital",
            ))
        session.flush()          # <- IntegrityError REAL acontece aqui


class _ShadowQuebraAntesDoFlush(ShadowEngine):
    """Falha de cálculo ANTES de qualquer escrita."""

    def on_strategy_signal(self, *a, **k):
        raise ZeroDivisionError("erro de cálculo proposital antes do flush")

    def on_operational_candle(self, *a, **k):
        raise ZeroDivisionError("erro de cálculo proposital antes do flush")


@pytest.mark.parametrize("classe,rotulo", [
    (_ShadowViolaConstraint, "IntegrityError no flush"),
    (_ShadowQuebraAntesDoFlush, "exceção antes do flush"),
])
def test_falha_shadow_nao_aborta_persistencia_operacional(tmp_path, classe, rotulo):
    shadow = classe()
    orch, f = _orquestrador(tmp_path, shadow, nome=classe.__name__)

    for _ in range(60):
        orch.tick()              # nenhum tick pode levantar

    with session_scope(f) as s:
        candles = s.execute(select(Candle)).scalars().all()
        sinais = s.execute(select(StrategySignal)).scalars().all()
        assert candles, f"{rotulo}: candles operacionais deveriam ter sido persistidos"
        assert sinais, f"{rotulo}: sinais operacionais deveriam ter sido persistidos"
        # a escrita shadow que falhou não deixou lixo meio gravado
        assert s.execute(select(ShadowTrade)).scalars().all() == []

    assert shadow.failures > 0, f"{rotulo}: a falha deveria estar registrada"
    assert shadow.health()["status"] == "DEGRADADO"
    assert shadow.last_error is not None

    # a sessão continua utilizável e o próximo tick funciona
    antes = len(candles)
    orch.market_data_provider = ListMarketDataProvider(
        [make_candle(200 + i, 120.0 + i) for i in range(5)])
    for _ in range(5):
        orch.tick()
    with session_scope(f) as s:
        assert len(s.execute(select(Candle)).scalars().all()) > antes, \
            "a sessão ficou inutilizável após a falha shadow"


def test_saude_operacional_nao_degrada_por_falha_shadow(tmp_path):
    shadow = _ShadowQuebraAntesDoFlush()
    orch, f = _orquestrador(tmp_path, shadow, nome="saude")
    for _ in range(40):
        orch.tick()
    # a saúde de mercado do orquestrador não conhece o shadow
    assert getattr(orch, "strategy_gap_degraded", False) is False
    assert shadow.health()["status"] == "DEGRADADO"


def test_savepoint_nao_faz_rollback_da_transacao_operacional(tmp_path):
    """Prova direta: o método de isolamento usa begin_nested e nunca chama
    session.rollback()."""
    import ast
    import pathlib

    fonte = pathlib.Path("app/orchestrator.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    metodo = next(n for n in ast.walk(arvore)
                  if isinstance(n, ast.FunctionDef) and n.name == "_run_shadow")
    chamadas = {n.func.attr for n in ast.walk(metodo)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "begin_nested" in chamadas, "isolamento deve usar savepoint"

    # `session.rollback()` precisa ser procurado na ÁRVORE, não no texto: a
    # docstring do método cita a expressão justamente para dizer que nunca a
    # usa, e uma busca textual daria falso positivo (mesma armadilha que já
    # apareceu no teste de isolamento por importação).
    for n in ast.walk(metodo):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "rollback"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "session"):
            pytest.fail("falha shadow nunca pode reverter a transação operacional")


# =========================================================================
# 2. IDENTIDADE IMUTÁVEL DO EXPERIMENTO
# =========================================================================

def test_experimento_criado_com_fingerprint_deterministico(sf):
    e = ShadowEngine(limits=ShadowLimits())
    with session_scope(sf) as s:
        _sinal(e, s)
    with session_scope(sf) as s:
        exps = s.execute(select(ShadowExperiment)).scalars().all()
        assert {x.model for x in exps} == set(MODELS)
        for x in exps:
            assert x.status == "ATIVO" and x.ended_at is None
            assert len(x.config_fingerprint) == 64
        h2 = next(x for x in exps if x.model == MODEL_H2)
        base = next(x for x in exps if x.model == MODEL_BASELINE)
        assert h2.threshold == 0.15
        assert base.threshold is None
        assert h2.config_fingerprint != base.config_fingerprint

    # mesmo config -> mesmo fingerprint, em outro motor
    outro = ShadowEngine(limits=ShadowLimits())
    assert outro.fingerprint(outro._config_snapshot(MODEL_H2)) == h2.config_fingerprint


def test_mudanca_de_configuracao_cria_experimento_novo(sf):
    e1 = ShadowEngine(limits=ShadowLimits(), strategy_timeframe_minutes=15)
    with session_scope(sf) as s:
        _sinal(e1, s, t=T0)
    # timeframe diferente => experimento novo, o antigo ENCERRADO
    e2 = ShadowEngine(limits=ShadowLimits(), strategy_timeframe_minutes=30)
    with session_scope(sf) as s:
        _sinal(e2, s, t=T0 + timedelta(hours=1))
    with session_scope(sf) as s:
        h2 = s.execute(select(ShadowExperiment).where(
            ShadowExperiment.model == MODEL_H2)).scalars().all()
        assert len(h2) == 2
        antigos = [x for x in h2 if x.status == "ENCERRADO"]
        ativos = [x for x in h2 if x.status == "ATIVO"]
        assert len(antigos) == 1 and len(ativos) == 1
        assert antigos[0].ended_at is not None
        assert antigos[0].strategy_timeframe_minutes == 15
        assert ativos[0].strategy_timeframe_minutes == 30


@pytest.mark.parametrize("mudanca", [
    {"fee_rate": 0.0009}, {"slippage_bps": 9.0},
    {"stop_loss_atr_multiple": 3.0}, {"max_position_usd": 80.0},
    {"min_order_notional_usd": 10.0}, {"cooldown_minutes": 45},
])
def test_qualquer_parametro_relevante_muda_o_fingerprint(mudanca):
    a = ShadowEngine(limits=ShadowLimits())
    b = ShadowEngine(limits=ShadowLimits(**mudanca))
    fa = a.fingerprint(a._config_snapshot(MODEL_H2))
    fb = b.fingerprint(b._config_snapshot(MODEL_H2))
    assert fa != fb, f"{mudanca} deveria criar experimento novo"


def test_metricas_nao_agregam_experimentos_diferentes(sf):
    e1 = ShadowEngine(limits=ShadowLimits(), strategy_timeframe_minutes=15)
    with session_scope(sf) as s:
        _sinal(e1, s, sym="BTCUSDT", t=T0)
    e2 = ShadowEngine(limits=ShadowLimits(), strategy_timeframe_minutes=30)
    with session_scope(sf) as s:
        _sinal(e2, s, sym="ETHUSDT", t=T0 + timedelta(hours=1))
    with session_scope(sf) as s:
        m = model_metrics(s, MODEL_H2)          # só o ativo
        assert m["opportunities"] == 1, "o experimento antigo não pode entrar"
        assert m["experiment_id"] is not None
        antigo = s.execute(select(ShadowExperiment).where(
            ShadowExperiment.model == MODEL_H2,
            ShadowExperiment.status == "ENCERRADO")).scalars().first()
        hist = model_metrics(s, MODEL_H2, experiment_id=antigo.id)
        assert hist["opportunities"] == 1
        assert hist["experiment_id"] == antigo.id


def test_posicao_aberta_pertence_ao_experimento_que_a_criou(sf):
    e1 = ShadowEngine(limits=ShadowLimits(), strategy_timeframe_minutes=15)
    with session_scope(sf) as s:
        _sinal(e1, s, t=T0)
    with session_scope(sf) as s:
        pos = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        exp_original = pos.experiment_id
    e2 = ShadowEngine(limits=ShadowLimits(), strategy_timeframe_minutes=30)
    with session_scope(sf) as s:
        _sinal(e2, s, t=T0 + timedelta(hours=1))   # cria experimento novo
    with session_scope(sf) as s:
        pos = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2,
            ShadowPosition.opened_candle_time == T0)).scalars().first()
        assert pos.experiment_id == exp_original, \
            "a posição não pode migrar de experimento"


# =========================================================================
# 3. GATE DE PROMOÇÃO — CONTRATO REAL
# =========================================================================

def test_gate_distingue_declarado_calculavel_atendido_pendente():
    r = promotion_status({"closed_trades": 500, "net_pnl_usd": 999.0,
                          "per_symbol": {"A": {"closed_trades": 250},
                                         "B": {"closed_trades": 250}}})
    assert set(r["declared"]) >= {"min_closed_trades", "positive_net_result",
                                  "confidence_interval_above_zero",
                                  "out_of_sample_validation"}
    assert "min_closed_trades" in r["met"]
    assert set(r["not_yet_computable"]) == {"confidence_interval_above_zero",
                                            "out_of_sample_validation"}
    # nenhuma quantidade de trades libera promoção
    assert r["promotion_eligible"] is False
    assert r["promotion_allowed_this_phase"] is False


def test_gate_bloqueia_mesmo_com_amostra_enorme():
    for n in (60, 230, 10_000):
        r = promotion_status({"closed_trades": n, "net_pnl_usd": 1e6,
                              "per_symbol": {"A": {"closed_trades": n // 2},
                                             "B": {"closed_trades": n // 2}}})
        assert r["promotion_eligible"] is False


def test_concentracao_em_um_simbolo_reprova(sf):
    r = promotion_status({"closed_trades": 100, "net_pnl_usd": 10.0,
                          "per_symbol": {"A": {"closed_trades": 95},
                                         "B": {"closed_trades": 5}}})
    assert r["criteria"]["not_concentrated_in_one_symbol"]["met"] is False


# =========================================================================
# 4. SEMÂNTICA DO SINAL OPOSTO E PRIORIDADE DE SAÍDA
# =========================================================================

def test_sinal_oposto_fecha_e_NAO_reverte_no_mesmo_evento(sf):
    """Fidelidade a orchestrator.py::_maybe_close_opposing_position, que
    fecha e RETORNA -- a reentrada fica para uma oportunidade posterior."""
    e = ShadowEngine(limits=ShadowLimits())
    with session_scope(sf) as s:
        _sinal(e, s, direcao="BUY", t=T0)
    with session_scope(sf) as s:
        _sinal(e, s, direcao="SELL", t=T0 + timedelta(minutes=15))
    with session_scope(sf) as s:
        trades = s.execute(select(ShadowTrade)).scalars().all()
        assert trades and all(t.exit_reason == "opposite_signal" for t in trades)
        abertas = s.execute(select(ShadowPosition).where(
            ShadowPosition.status == "OPEN")).scalars().all()
        assert abertas == [], "o motor não abre posição nova no mesmo evento"
        op = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2,
            ShadowOpportunity.source_candle_open_time == T0 + timedelta(minutes=15))
        ).scalars().first()
        assert op.approved is False and "não reverte" in op.reason


def test_saida_por_stop_no_mesmo_tick_bloqueia_entrada_nova(sf):
    """Fidelidade a `if stop_take_result is not None: ... return`."""
    e = ShadowEngine(limits=ShadowLimits())
    with session_scope(sf) as s:
        _sinal(e, s, direcao="BUY", t=T0)
    with session_scope(sf) as s:
        pos = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        stop = pos.stop_loss
    tick = T0 + timedelta(minutes=14)
    with session_scope(sf) as s:
        e.on_operational_candle(s, "BTCUSDT", PRECO, stop - 1, stop, tick)
    with session_scope(sf) as s:
        # sinal do MESMO tick não pode virar entrada
        _sinal(e, s, direcao="BUY", t=T0 + timedelta(minutes=15), tick_candle_time=tick)
    with session_scope(sf) as s:
        op = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2,
            ShadowOpportunity.source_candle_open_time == T0 + timedelta(minutes=15))
        ).scalars().first()
        assert op.approved is False and "prioridade" in op.reason
        assert s.execute(select(ShadowPosition).where(
            ShadowPosition.status == "OPEN")).scalars().all() == []


# =========================================================================
# 5. NENHUMA ESCRITA OPERACIONAL, EM NENHUM CENÁRIO
# =========================================================================

def test_nenhuma_tabela_operacional_e_tocada_nem_sob_falha(tmp_path):
    shadow = _ShadowViolaConstraint()
    orch, f = _orquestrador(tmp_path, shadow, nome="sem_ordem")
    for _ in range(60):
        orch.tick()
    with session_scope(f) as s:
        # o shadow nunca cria ordem/posição operacional; o que existir aí
        # veio do motor real, jamais do shadow
        assert s.execute(select(ShadowTrade)).scalars().all() == []
        for p in s.execute(select(Position)).scalars().all():
            assert p.symbol == "BTCUSDT"     # criadas pelo motor, não pelo shadow
        for o in s.execute(select(Order)).scalars().all():
            assert o.symbol == "BTCUSDT"
