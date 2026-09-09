"""Fase 3.4.3 — motor shadow H2: isolamento, semântica e idempotência.

O ponto mais importante testado aqui não é o resultado econômico — é que a
instrumentação NÃO PODE tocar a operação. Vários testes provam ausência:
nenhuma Order, nenhuma Execution, nenhuma Position operacional, nenhuma
alteração de patrimônio, e falha shadow que não derruba o tick.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import RunMode, Settings
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import (
    Execution,
    Order,
    Position,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
)
from app.shadow.engine import (
    H2_MIN_SEPARATION,
    MODEL_BASELINE,
    MODEL_H2,
    MODELS,
    ShadowEngine,
    ShadowLimits,
)
from app.shadow.metrics import PROMOTION_GATE, comparative, model_metrics

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
PRECO, ATR = 100.0, 5.0


@pytest.fixture()
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'shadow.db'}")
    init_db(eng)
    return make_session_factory(eng)


def _engine(**kw):
    return ShadowEngine(limits=ShadowLimits(**kw), strategy_timeframe_minutes=15)


def _sinal(e, s, sym="BTCUSDT", direcao="BUY", sep=0.30, t=None, preco=PRECO, atr=ATR):
    """Emite um sinal com a separação normalizada pedida.
    separação = |fast - slow| / atr  ->  fast = slow + sep*atr."""
    slow = preco
    fast = slow + sep * atr if direcao == "BUY" else slow - sep * atr
    e.on_strategy_signal(s, sym, direcao, preco, atr, fast, slow, t or T0)


# --- 1 e 17: isolamento absoluto das tabelas operacionais -----------------

def test_shadow_nunca_cria_order_execution_ou_position(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s)
        e.on_operational_candle(s, "BTCUSDT", PRECO + 50, PRECO - 50, PRECO, T0 + timedelta(minutes=1))
    with session_scope(sf) as s:
        assert s.execute(select(Order)).scalars().all() == []
        assert s.execute(select(Execution)).scalars().all() == []
        assert s.execute(select(Position)).scalars().all() == []
        # ... e escreveu apenas nas tabelas shadow
        assert s.execute(select(ShadowOpportunity)).scalars().all() != []


def test_modulo_shadow_nao_importa_caminho_de_execucao():
    """Prova estrutural por AST, não por texto.

    Verificar com `in` no código-fonte seria frágil nos dois sentidos: pega
    a prova falsa de um comentário que apenas CITA o nome (foi o que
    aconteceu na primeira versão deste teste, com a própria docstring do
    módulo), e não pegaria um acesso construído dinamicamente. A árvore
    sintática mostra o que o módulo REALMENTE importa e chama."""
    import ast
    import pathlib

    arvore = ast.parse(pathlib.Path("app/shadow/engine.py").read_text(encoding="utf-8"))

    importados: set[str] = set()
    modulos: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.ImportFrom):
            modulos.add(no.module or "")
            importados.update(a.name for a in no.names)
        elif isinstance(no, ast.Import):
            for a in no.names:
                modulos.add(a.name)
                importados.add(a.name)

    PROIBIDOS = {"ExecutionEngine", "RiskEngine", "SystemState", "Order",
                 "Execution", "Position", "ApprovedOrder", "OrderStatus"}
    assert not (importados & PROIBIDOS), f"shadow importa {importados & PROIBIDOS}"

    MODULOS_PROIBIDOS = {"app.execution", "app.risk.engine", "requests",
                         "urllib", "urllib.request", "http", "httpx", "socket"}
    for m in modulos:
        assert not any(m == p or m.startswith(p + ".") for p in MODULOS_PROIBIDOS), \
            f"shadow importa módulo proibido: {m}"

    # nomes efetivamente USADOS no código (não em comentário/docstring)
    usados = {n.id for n in ast.walk(arvore) if isinstance(n, ast.Name)}
    usados |= {n.attr for n in ast.walk(arvore) if isinstance(n, ast.Attribute)}
    assert not (usados & PROIBIDOS), f"shadow usa {usados & PROIBIDOS}"
    for perigoso in ("submit", "poll_order", "request_cancel", "evaluate", "get_settings"):
        assert perigoso not in usados, f"shadow chama `{perigoso}`"


# --- 2: baseline e H2 independentes ---------------------------------------

def test_baseline_e_h2_sao_independentes(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.05)          # abaixo do limiar do H2
    with session_scope(sf) as s:
        ops = {o.model: o for o in s.execute(select(ShadowOpportunity)).scalars().all()}
        assert ops[MODEL_BASELINE].approved is True
        assert ops[MODEL_H2].approved is False
        pos = s.execute(select(ShadowPosition)).scalars().all()
        assert [p.model for p in pos] == [MODEL_BASELINE]


def test_portfolios_nao_compartilham_posicao(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30)          # ambos aprovam
    with session_scope(sf) as s:
        pos = s.execute(select(ShadowPosition)).scalars().all()
        assert {p.model for p in pos} == set(MODELS)
        assert len(pos) == 2, "cada modelo tem a sua própria posição"


# --- 3 e 4: limiar exatamente 0,15, sem varredura --------------------------

def test_limiar_h2_e_exatamente_015(sf):
    assert H2_MIN_SEPARATION == 0.15
    for sep, esperado in ((0.1499, False), (0.15, True), (0.1501, True)):
        e = _engine()
        eng = make_engine("sqlite:///:memory:")
        init_db(eng)
        f = make_session_factory(eng)
        with session_scope(f) as s:
            _sinal(e, s, sep=sep)
        with session_scope(f) as s:
            h2 = s.execute(select(ShadowOpportunity).where(
                ShadowOpportunity.model == MODEL_H2)).scalars().first()
            assert h2.approved is esperado, f"sep={sep}"


def test_nenhum_outro_limiar_e_procurado():
    """O limiar é uma constante única no código — não há lista, grade nem
    varredura de valores."""
    import pathlib

    fonte = pathlib.Path("app/shadow/engine.py").read_text(encoding="utf-8")
    assert fonte.count("H2_MIN_SEPARATION = ") == 1
    for suspeito in ("for limiar in", "for threshold in", "np.arange", "itertools.product"):
        assert suspeito not in fonte


# --- 5, 6, 7: ciclo completo BUY/SELL, saídas e P&L -----------------------

@pytest.mark.parametrize("direcao", ["BUY", "SELL"])
def test_ciclo_completo_com_alvo(sf, direcao):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, direcao=direcao, sep=0.30)
    with session_scope(sf) as s:
        p = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        alvo = p.take_profit
    with session_scope(sf) as s:
        # candle que toca SÓ o alvo
        if direcao == "BUY":
            e.on_operational_candle(s, "BTCUSDT", alvo + 0.01, alvo - 1, alvo, T0 + timedelta(minutes=5))
        else:
            e.on_operational_candle(s, "BTCUSDT", alvo + 1, alvo - 0.01, alvo, T0 + timedelta(minutes=5))
    with session_scope(sf) as s:
        t = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_H2)).scalars().first()
        assert t is not None and t.exit_reason == "take_profit"
        assert t.net_pnl_usd > 0
        assert t.fees_usd > 0 and t.slippage_usd > 0
        assert t.net_pnl_usd == pytest.approx(t.gross_pnl_usd - t.fees_usd - t.slippage_usd, abs=1e-9)


def test_stop_tem_prioridade_sobre_alvo_no_mesmo_candle(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30)
    with session_scope(sf) as s:
        p = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        stop, alvo = p.stop_loss, p.take_profit
    with session_scope(sf) as s:
        e.on_operational_candle(s, "BTCUSDT", alvo + 1, stop - 1, PRECO, T0 + timedelta(minutes=3))
    with session_scope(sf) as s:
        t = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_H2)).scalars().first()
        assert t.exit_reason == "stop_loss", "regra conservadora do motor"


def test_sinal_oposto_fecha_a_posicao(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, direcao="BUY", sep=0.30, t=T0)
    with session_scope(sf) as s:
        _sinal(e, s, direcao="SELL", sep=0.30, t=T0 + timedelta(minutes=15))
    with session_scope(sf) as s:
        t = s.execute(select(ShadowTrade)).scalars().all()
        assert t and all(x.exit_reason == "opposite_signal" for x in t)


# --- 8 e 9: exposição global e notional mínimo ----------------------------

def test_exposicao_global_impede_segunda_posicao(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sym="BTCUSDT", sep=0.30, t=T0)
    with session_scope(sf) as s:
        _sinal(e, s, sym="ETHUSDT", sep=0.30, t=T0 + timedelta(minutes=15))
    with session_scope(sf) as s:
        eth = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.symbol == "ETHUSDT",
            ShadowOpportunity.model == MODEL_H2)).scalars().first()
        assert eth.approved is False
        assert "exposição" in eth.reason


def test_notional_minimo_recusa_sobra_irrelevante(sf):
    e = _engine(max_position_usd=50.0, max_total_exposure_usd=50.02,
                min_order_notional_usd=5.0)
    with session_scope(sf) as s:
        _sinal(e, s, sym="BTCUSDT", sep=0.30, t=T0)
    with session_scope(sf) as s:
        _sinal(e, s, sym="ETHUSDT", sep=0.30, t=T0 + timedelta(minutes=15))
    with session_scope(sf) as s:
        eth = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.symbol == "ETHUSDT",
            ShadowOpportunity.model == MODEL_H2)).scalars().first()
        assert eth.approved is False
        assert "mínimo" in eth.reason


def test_notional_respeita_teto_com_slippage(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30)
    with session_scope(sf) as s:
        for p in s.execute(select(ShadowPosition)).scalars().all():
            assert p.notional_usd == pytest.approx(50.0, abs=1e-9)
            assert p.notional_usd <= 50.0 + 1e-9


# --- 10: cooldown em tempo de candle --------------------------------------

def test_cooldown_usa_tempo_do_candle(sf):
    e = _engine(cooldown_after_losses=1, cooldown_minutes=30)
    t = T0
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30, t=t)
    with session_scope(sf) as s:
        p = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        stop = p.stop_loss
    with session_scope(sf) as s:                      # perda -> cooldown
        e.on_operational_candle(s, "BTCUSDT", PRECO, stop - 1, stop, t + timedelta(minutes=5))
    with session_scope(sf) as s:                      # 10 min depois: bloqueado
        _sinal(e, s, sep=0.30, t=t + timedelta(minutes=15))
    with session_scope(sf) as s:
        o = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2,
            ShadowOpportunity.source_candle_open_time == t + timedelta(minutes=15))).scalars().first()
        assert o.approved is False and "cooldown" in o.reason
    with session_scope(sf) as s:                      # 45 min depois: liberado
        _sinal(e, s, sep=0.30, t=t + timedelta(minutes=45))
    with session_scope(sf) as s:
        o = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2,
            ShadowOpportunity.source_candle_open_time == t + timedelta(minutes=45))).scalars().first()
        assert o.approved is True, "cooldown deveria ter expirado em tempo de candle"


# --- 11, 12, 13: retomada, idempotência, reprocessamento ------------------

def test_reprocessar_o_mesmo_candle_nao_duplica(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30, t=T0)
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30, t=T0)      # exatamente o mesmo bucket
        _sinal(e, s, sep=0.30, t=T0)
    with session_scope(sf) as s:
        ops = s.execute(select(ShadowOpportunity)).scalars().all()
        assert len(ops) == len(MODELS), "uma oportunidade por modelo, sem duplicata"
        assert len(s.execute(select(ShadowPosition)).scalars().all()) == len(MODELS)


def test_retomada_apos_reinicio_le_posicao_do_banco(sf):
    e1 = _engine()
    with session_scope(sf) as s:
        _sinal(e1, s, sep=0.30, t=T0)
    # "reinício": motor novo, estado em memória zerado, mesmo banco
    e2 = _engine()
    with session_scope(sf) as s:
        p = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        alvo = p.take_profit
        e2.on_operational_candle(s, "BTCUSDT", alvo + 0.01, alvo - 1, alvo,
                                 T0 + timedelta(minutes=5))
    with session_scope(sf) as s:
        t = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_H2)).scalars().first()
        assert t is not None, "motor reiniciado deve enxergar a posição persistida"


# --- 14: falha shadow não afeta operação ----------------------------------

def test_falha_shadow_nao_interrompe_o_tick(tmp_path):
    """O tick operacional roda com um shadow que sempre explode. A operação
    tem de continuar, e a falha aparece só na saúde do shadow."""
    from tests.test_price_correctness import ListMarketDataProvider, make_candle
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.config import RiskLimits
    from app.risk.engine import RiskEngine
    from app.strategy.engine import StrategyEngine

    class ShadowQuebrado(ShadowEngine):
        def on_operational_candle(self, *a, **k):
            raise RuntimeError("falha proposital de instrumentação")

        def on_strategy_signal(self, *a, **k):
            raise RuntimeError("falha proposital de instrumentação")

    eng = make_engine(f"sqlite:///{tmp_path / 'falha.db'}")
    init_db(eng)
    f = make_session_factory(eng)
    st = Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                  database_url=f"sqlite:///{tmp_path / 'falha.db'}",
                  strategy_timeframe_minutes=1)
    shadow = ShadowQuebrado()
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=st, session_factory=f,
        market_data_provider=ListMarketDataProvider(
            [make_candle(i, 100.0 + (i % 5)) for i in range(40)]),
        strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0),
        price_state=price_state, shadow_engine=shadow,
    )
    for _ in range(40):
        orch.tick()                      # não pode levantar
    assert shadow.failures > 0, "a falha deveria ter sido capturada"
    assert shadow.health()["status"] == "DEGRADADO"
    with session_scope(f) as s:          # a operação seguiu gravando candles
        from app.persistence.models import Candle
        assert len(s.execute(select(Candle)).scalars().all()) > 0


# --- 15: API e métricas ---------------------------------------------------

def test_api_shadow_somente_leitura_e_isolada(sf):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_shadow

    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30)

    class _Orch:
        session_factory = sf
        shadow_engine = e

    app = FastAPI()
    app.state.orchestrator = _Orch()
    app.include_router(routes_shadow.router, prefix="/api")
    c = TestClient(app)

    assert c.get("/api/shadow/health").json()["enabled"] is True
    assert c.get("/api/shadow/opportunities").json()
    assert c.get("/api/shadow/positions").json()[0]["hypothetical"] is True
    m = c.get("/api/shadow/metrics").json()
    assert set(m["models"]) == set(MODELS)
    assert m["promotion_gate"]["promotion_allowed_this_phase"] is False
    assert m["promotion_gate"]["min_closed_trades_for_review"] == 60
    # modelo inexistente -> 404, e nenhuma rota de escrita existe
    assert c.get("/api/shadow/metrics?model=inventado").status_code == 404
    for rota in ("/api/shadow/health", "/api/shadow/trades"):
        assert c.post(rota).status_code in (404, 405), "shadow não expõe escrita"


def test_metricas_nao_se_misturam_com_patrimonio(sf):
    e = _engine()
    with session_scope(sf) as s:
        _sinal(e, s, sep=0.30)
    with session_scope(sf) as s:
        m = comparative(s)
        for modelo in MODELS:
            assert "equity" not in m["models"][modelo]
            assert "starting_balance" not in m["models"][modelo]
        assert PROMOTION_GATE["promotion_allowed_this_phase"] is False


# --- 16: migration sobre banco existente ----------------------------------

def test_migration_v9_sobre_banco_existente(tmp_path):
    """Um banco já em v8, sem as tabelas shadow, é migrado sem perder dado."""
    from sqlalchemy import text

    from app.persistence.migrations import CURRENT_SCHEMA_VERSION, run_migrations

    caminho = tmp_path / "legado.db"
    eng = make_engine(f"sqlite:///{caminho}")
    init_db(eng)
    with eng.begin() as conn:          # simula banco anterior à v9
        for t in ("shadow_trades", "shadow_positions", "shadow_opportunities"):
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        # v10 (Fase 3.5) vem depois de v9 -- pra simular "banco antes de
        # v9" a contiguidade exige remover tambem a linha de v10.
        conn.execute(text("DELETE FROM schema_migrations WHERE version IN (9, 10)"))
    run_migrations(eng)
    with eng.begin() as conn:
        nomes = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert {"shadow_opportunities", "shadow_positions", "shadow_trades"} <= nomes
        v = conn.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
        assert v == CURRENT_SCHEMA_VERSION == 10
    run_migrations(eng)                # idempotente: rodar de novo é no-op
    with eng.begin() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=9")).scalar() == 1


# --- 18: isolamento multiativo --------------------------------------------

def test_isolamento_multiativo(sf):
    """Cada símbolo tem posição própria; o que fecha um não fecha o outro."""
    e = _engine(max_total_exposure_usd=200.0)
    with session_scope(sf) as s:
        _sinal(e, s, sym="BTCUSDT", sep=0.30, t=T0)
        _sinal(e, s, sym="ETHUSDT", sep=0.30, t=T0)
    with session_scope(sf) as s:
        p = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2,
            ShadowPosition.symbol == "BTCUSDT")).scalars().first()
        alvo = p.take_profit
        e.on_operational_candle(s, "BTCUSDT", alvo + 0.01, alvo - 1, alvo, T0 + timedelta(minutes=5))
    with session_scope(sf) as s:
        abertas = s.execute(select(ShadowPosition).where(
            ShadowPosition.status == "OPEN")).scalars().all()
        assert {p.symbol for p in abertas} == {"ETHUSDT"}, "ETH não podia fechar junto"
