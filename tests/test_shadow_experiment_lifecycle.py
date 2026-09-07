"""Fase 3.4.3 (auditoria final) — CICLO TEMPORAL dos experimentos shadow.

O fingerprint identifica uma CONFIGURAÇÃO; o experimento identifica uma
EXECUÇÃO. Voltar a uma configuração antiga cria execução nova (A -> B ->
A2), e as duas nunca podem ser agregadas em silêncio.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.migrations import (
    CURRENT_SCHEMA_VERSION,
    current_schema_version,
    run_migrations,
)
from app.persistence.models import (
    ShadowExperiment,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
)
from app.shadow.engine import MODEL_BASELINE, MODEL_H2, ShadowEngine, ShadowLimits
from app.shadow.metrics import PROMOTION_ALLOWED_THIS_PHASE, model_metrics, promotion_status
from app.strategy.engine import StrategyConfig

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
PRECO, ATR = 100.0, 5.0


@pytest.fixture()
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'ciclo.db'}")
    init_db(eng)
    return make_session_factory(eng)


def _motor(tf=15, **lim):
    return ShadowEngine(limits=ShadowLimits(**lim), strategy_timeframe_minutes=tf,
                        strategy_config=StrategyConfig(timeframe_minutes=tf))


def _sinal(e, s, sym="BTCUSDT", direcao="BUY", sep=0.30, t=None, **kw):
    slow = PRECO
    fast = slow + sep * ATR if direcao == "BUY" else slow - sep * ATR
    e.on_strategy_signal(s, sym, direcao, PRECO, ATR, fast, slow, t or T0, **kw)


# =========================================================================
# 1. A -> B -> A2
# =========================================================================

def test_voltar_a_configuracao_antiga_cria_execucao_nova(sf):
    a = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a, s, t=T0)
    b = _motor(tf=5)                       # configuração B
    with session_scope(sf) as s:
        _sinal(b, s, t=T0 + timedelta(hours=1))
    a2 = _motor(tf=15)                     # volta EXATAMENTE para A
    with session_scope(sf) as s:
        _sinal(a2, s, t=T0 + timedelta(hours=2))

    with session_scope(sf) as s:
        exps = s.execute(select(ShadowExperiment).where(
            ShadowExperiment.model == MODEL_H2).order_by(ShadowExperiment.id)).scalars().all()
        assert len(exps) == 3, "A, B e A2 são três execuções distintas"
        A, B, A2 = exps
        # A e A2 têm a MESMA configuração...
        assert A.config_fingerprint == A2.config_fingerprint
        assert B.config_fingerprint != A.config_fingerprint
        # ...mas são execuções diferentes
        assert A.experiment_uid != A2.experiment_uid
        assert A2.started_at >= A.started_at
        assert A.status == "ENCERRADO" and B.status == "ENCERRADO"
        assert A2.status == "ATIVO"
        assert A.ended_at is not None and B.ended_at is not None


def test_A_e_A2_nunca_sao_agregados_silenciosamente(sf):
    a = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a, s, sym="BTCUSDT", t=T0)
    b = _motor(tf=5)
    with session_scope(sf) as s:
        _sinal(b, s, sym="BTCUSDT", t=T0 + timedelta(hours=1))
    a2 = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a2, s, sym="BTCUSDT", t=T0 + timedelta(hours=2))

    with session_scope(sf) as s:
        ativo = model_metrics(s, MODEL_H2)
        assert ativo["opportunities"] == 1, "só a execução ativa (A2)"
        exps = s.execute(select(ShadowExperiment).where(
            ShadowExperiment.model == MODEL_H2).order_by(ShadowExperiment.id)).scalars().all()
        # cada execução histórica é consultável isoladamente
        for e in exps:
            assert model_metrics(s, MODEL_H2, experiment_id=e.id)["opportunities"] == 1


# =========================================================================
# 2. IDEMPOTÊNCIA POR EXPERIMENTO
# =========================================================================

def test_mesmo_candle_em_experimentos_diferentes_nao_colide(sf):
    """O mesmo símbolo/candle pode aparecer em A e em A2 -- experimentos
    diferentes, sem violação de unicidade."""
    a = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a, s, sym="BTCUSDT", t=T0)
    b = _motor(tf=5)
    with session_scope(sf) as s:
        _sinal(b, s, sym="ETHUSDT", t=T0 + timedelta(minutes=1))
    a2 = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a2, s, sym="BTCUSDT", t=T0)      # MESMÍSSIMO candle de A

    with session_scope(sf) as s:
        linhas = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2,
            ShadowOpportunity.symbol == "BTCUSDT",
            ShadowOpportunity.source_candle_open_time == T0)).scalars().all()
        assert len(linhas) == 2, "uma em A, outra em A2"
        assert len({x.experiment_id for x in linhas}) == 2


def test_reprocessar_no_mesmo_experimento_continua_idempotente(sf):
    a = _motor(tf=15)
    for _ in range(3):
        with session_scope(sf) as s:
            _sinal(a, s, t=T0)
    with session_scope(sf) as s:
        assert len(s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2)).scalars().all()) == 1
        assert len(s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().all()) == 1


# =========================================================================
# 3. POSIÇÃO ABERTA DURANTE TROCA DE EXPERIMENTO
# =========================================================================

def test_posicao_de_A_continua_de_A_e_bloqueia_exposicao_de_B(sf):
    a = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a, s, sym="BTCUSDT", direcao="BUY", t=T0)
    with session_scope(sf) as s:
        pos = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        exp_A, stop = pos.experiment_id, pos.stop_loss

    b = _motor(tf=5)                       # troca de configuração
    with session_scope(sf) as s:
        _sinal(b, s, sym="ETHUSDT", direcao="BUY", t=T0 + timedelta(hours=1))

    with session_scope(sf) as s:
        # B não pode abrir: a exposição olha TODOS os experimentos do modelo
        op = s.execute(select(ShadowOpportunity).where(
            ShadowOpportunity.model == MODEL_H2,
            ShadowOpportunity.symbol == "ETHUSDT")).scalars().first()
        assert op.approved is False
        assert "exposição" in op.reason
        abertas = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2,
            ShadowPosition.status == "OPEN")).scalars().all()
        assert len(abertas) == 1 and abertas[0].experiment_id == exp_A

    # a posição de A continua administrada e fecha com as proteções de A
    with session_scope(sf) as s:
        b.on_operational_candle(s, "BTCUSDT", PRECO, stop - 1, stop,
                                T0 + timedelta(hours=2))
    with session_scope(sf) as s:
        t = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_H2)).scalars().first()
        assert t is not None and t.exit_reason == "stop_loss"
        assert t.experiment_id == exp_A, "o fechamento pertence a A"
        # métricas: A recebe o trade, o ativo (B) não
        assert model_metrics(s, MODEL_H2, experiment_id=exp_A)["closed_trades"] == 1
        ativo = model_metrics(s, MODEL_H2)
        assert ativo["experiment_id"] != exp_A
        assert ativo["closed_trades"] == 0


def test_troca_A_B_A2_com_posicao_antiga_ainda_aberta(sf):
    a = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a, s, sym="BTCUSDT", direcao="BUY", t=T0)
    with session_scope(sf) as s:
        pos = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2)).scalars().first()
        exp_A, stop = pos.experiment_id, pos.stop_loss

    b = _motor(tf=5)
    with session_scope(sf) as s:
        _sinal(b, s, sym="ETHUSDT", t=T0 + timedelta(hours=1))
    a2 = _motor(tf=15)
    with session_scope(sf) as s:
        _sinal(a2, s, sym="SOLUSDT", t=T0 + timedelta(hours=2))

    with session_scope(sf) as s:
        # nem B nem A2 abriram: a posição de A ainda ocupa a exposição
        abertas = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_H2,
            ShadowPosition.status == "OPEN")).scalars().all()
        assert len(abertas) == 1 and abertas[0].experiment_id == exp_A
        for sym in ("ETHUSDT", "SOLUSDT"):
            op = s.execute(select(ShadowOpportunity).where(
                ShadowOpportunity.model == MODEL_H2,
                ShadowOpportunity.symbol == sym)).scalars().first()
            assert op.approved is False

    # fecha a posição de A já sob A2 ativo -- o trade continua sendo de A
    with session_scope(sf) as s:
        a2.on_operational_candle(s, "BTCUSDT", PRECO, stop - 1, stop,
                                 T0 + timedelta(hours=3))
    with session_scope(sf) as s:
        t = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_H2)).scalars().first()
        assert t.experiment_id == exp_A
        assert model_metrics(s, MODEL_H2)["closed_trades"] == 0, "A2 não herda o trade"


# =========================================================================
# 4. FINGERPRINT CANÔNICO COMPLETO
# =========================================================================

@pytest.mark.parametrize("campo,valor", [
    ("fast_period", 8), ("slow_period", 22), ("atr_period", 20),
    ("min_atr_pct_of_price", 0.001), ("max_atr_pct_of_price", 0.04),
    ("stop_loss_atr_multiple", 2.5), ("take_profit_atr_multiple", 4.0),
])
def test_qualquer_campo_da_strategy_config_muda_o_experimento(campo, valor):
    base = ShadowEngine(strategy_config=StrategyConfig(), strategy_timeframe_minutes=15)
    outro = ShadowEngine(strategy_config=StrategyConfig(**{campo: valor}),
                         strategy_timeframe_minutes=15)
    assert (base.fingerprint(base._config_snapshot(MODEL_H2))
            != outro.fingerprint(outro._config_snapshot(MODEL_H2))), \
        f"{campo} deveria criar experimento novo"


@pytest.mark.parametrize("campo,valor", [
    ("fee_rate", 0.0009), ("slippage_bps", 9.0), ("max_position_usd", 80.0),
    ("min_order_notional_usd", 10.0), ("max_daily_loss_usd", 40.0),
    ("cooldown_after_losses", 5), ("cooldown_minutes", 45),
    ("minimum_cost_coverage_ratio", 2.0), ("expected_move_atr_multiple", 2.0),
])
def test_qualquer_campo_de_limites_ou_custo_muda_o_experimento(campo, valor):
    base = _motor()
    outro = _motor(**{campo: valor})
    assert (base.fingerprint(base._config_snapshot(MODEL_H2))
            != outro.fingerprint(outro._config_snapshot(MODEL_H2)))


def test_cadencia_e_log_nao_entram_no_fingerprint():
    """Parâmetros de cadência/log não alteram o significado econômico e não
    podem encerrar um experimento em andamento. Prova estrutural: eles nem
    chegam ao snapshot."""
    e = _motor()
    import json

    texto = json.dumps(e._config_snapshot(MODEL_H2), sort_keys=True)
    for irrelevante in ("reconciliation", "log_level", "poll_interval",
                        "api_port", "database_url", "api_key", "secret"):
        assert irrelevante not in texto, f"{irrelevante} não pode entrar no fingerprint"


def test_fingerprint_e_deterministico_entre_instancias():
    a, b = _motor(), _motor()
    assert (a.fingerprint(a._config_snapshot(MODEL_H2))
            == b.fingerprint(b._config_snapshot(MODEL_H2)))
    assert (a.fingerprint(a._config_snapshot(MODEL_BASELINE))
            != a.fingerprint(a._config_snapshot(MODEL_H2)))


# =========================================================================
# 5. TRAVA DEFINITIVA DE PROMOÇÃO
# =========================================================================

def test_trava_de_fase_vence_mesmo_com_todos_os_criterios_atendidos(monkeypatch):
    perfeito = {"closed_trades": 10_000, "net_pnl_usd": 1e6,
                "per_symbol": {"A": {"closed_trades": 5000},
                               "B": {"closed_trades": 5000}}}
    assert promotion_status(perfeito)["promotion_eligible"] is False
    assert PROMOTION_ALLOWED_THIS_PHASE is False

    # simula o futuro: TODOS calculáveis e atendidos, mas fase ainda fechada
    import app.shadow.metrics as M

    original = M.promotion_status

    def tudo_ok(m):
        r = original(m)
        for c in r["criteria"].values():
            c["computable"], c["met"] = True, True
        r["pending"], r["not_yet_computable"] = [], []
        r["promotion_eligible"] = bool(
            M.PROMOTION_ALLOWED_THIS_PHASE and not r["pending"]
            and not r["not_yet_computable"])
        return r

    assert tudo_ok(perfeito)["promotion_eligible"] is False, \
        "a trava de fase precisa vencer mesmo com tudo aprovado"


# =========================================================================
# 6. SAÚDE SHADOW
# =========================================================================

def test_saude_degrada_e_recupera_com_regra_explicita():
    e = _motor()
    assert e.health()["status"] == "SAUDAVEL"

    e.record_failure(RuntimeError("falha 1"))
    e.record_failure(RuntimeError("falha 2"))
    h = e.health()
    assert h["status"] == "DEGRADADO"
    assert h["failures"] == 2 and h["consecutive_failures"] == 2
    assert "falha 2" in h["last_error"]

    e.record_success()
    h = e.health()
    assert h["status"] == "SAUDAVEL", "recupera no primeiro ciclo bem-sucedido"
    assert h["failures"] == 2, "o cumulativo é histórico e não zera"
    assert h["consecutive_failures"] == 0
    assert h["last_error"] is not None, "último erro fica registrado"
    assert h["blocks_operation"] is False
    assert "SAUDAVEL" in h["recovery_rule"]


def test_hook_saudavel_nao_mascara_hook_permanentemente_quebrado():
    """Regressão: a checagem de stop/alvo roda em todo candle e quase nunca
    falha. Com um contador único, ela zeraria o contador da avaliação de
    sinal a cada tick, e a saúde reportaria SAUDAVEL com um defeito
    permanente embaixo."""
    e = _motor()
    for _ in range(50):
        e.record_success("on_operational_candle")
        e.record_failure(RuntimeError("sinal quebrado"), "on_strategy_signal")

    h = e.health()
    assert h["status"] == "DEGRADADO", "o hook quebrado não pode ser mascarado"
    assert h["consecutive_failures_by_operation"]["on_strategy_signal"] == 50
    assert h["consecutive_failures_by_operation"]["on_operational_candle"] == 0
    assert h["last_failed_operation"] == "on_strategy_signal"

    e.record_success("on_strategy_signal")     # a operação certa recupera
    assert e.health()["status"] == "SAUDAVEL"
    assert e.health()["failures"] == 50, "o cumulativo continua histórico"


def test_reinicio_nao_transforma_falha_historica_em_bloqueio():
    velho = _motor()
    velho.record_failure(RuntimeError("incidente antigo"))
    assert velho.health()["status"] == "DEGRADADO"
    novo = _motor()          # "reinício": saúde vive em memória
    assert novo.health()["status"] == "SAUDAVEL"
    assert novo.health()["blocks_operation"] is False


# =========================================================================
# 7. MIGRATION V9
# =========================================================================

def test_migration_v9_banco_novo_e_idempotente(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'novo.db'}")
    init_db(eng)
    run_migrations(eng)
    assert current_schema_version(eng) == CURRENT_SCHEMA_VERSION == 9
    r = run_migrations(eng)
    assert r.applied == [], "reexecutar é no-op"


def test_migration_v9_upgrade_real_de_v8(tmp_path):
    caminho = tmp_path / "de_v8.db"
    eng = make_engine(f"sqlite:///{caminho}")
    init_db(eng)
    with eng.begin() as c:                     # rebaixa para v8
        for t in ("shadow_trades", "shadow_positions",
                  "shadow_opportunities", "shadow_experiments"):
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
        c.execute(text("DELETE FROM schema_migrations WHERE version = 9"))
    assert current_schema_version(eng) == 8

    r = run_migrations(eng)
    assert 9 in r.applied
    with eng.begin() as c:
        nomes = {x[0] for x in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert {"shadow_experiments", "shadow_opportunities",
                "shadow_positions", "shadow_trades"} <= nomes
    assert run_migrations(eng).applied == []


def test_banco_recusa_dois_experimentos_ativos_do_mesmo_modelo(tmp_path):
    """A garantia é do BANCO (índice único parcial), não da aplicação."""
    eng = make_engine(f"sqlite:///{tmp_path / 'dois.db'}")
    init_db(eng)
    run_migrations(eng)
    with eng.begin() as c:
        base = ("INSERT INTO shadow_experiments (experiment_uid, model, "
                "hypothesis_version, strategy_timeframe_minutes, strategy_version, "
                "fast_period, slow_period, atr_period, fee_rate, slippage_bps, "
                "stop_loss_atr_multiple, take_profit_atr_multiple, max_position_usd, "
                "max_total_exposure_usd, min_order_notional_usd, max_daily_loss_usd, "
                "cooldown_after_losses, cooldown_minutes, config_fingerprint, "
                "config_snapshot_json, started_at, status) VALUES "
                "(:uid,'m','h',15,'v1',9,21,14,0.0006,5.0,2.0,3.0,50,50,5,25,3,30,"
                ":fp,'{}','2026-01-01','ATIVO')")
        c.execute(text(base), {"uid": "u1", "fp": "fp1"})
        with pytest.raises(Exception):
            c.execute(text(base), {"uid": "u2", "fp": "fp2"})


def test_indice_parcial_permite_varios_encerrados(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'encerrados.db'}")
    init_db(eng)
    run_migrations(eng)
    with eng.begin() as c:
        base = ("INSERT INTO shadow_experiments (experiment_uid, model, "
                "hypothesis_version, strategy_timeframe_minutes, strategy_version, "
                "fast_period, slow_period, atr_period, fee_rate, slippage_bps, "
                "stop_loss_atr_multiple, take_profit_atr_multiple, max_position_usd, "
                "max_total_exposure_usd, min_order_notional_usd, max_daily_loss_usd, "
                "cooldown_after_losses, cooldown_minutes, config_fingerprint, "
                "config_snapshot_json, started_at, status) VALUES "
                "(:uid,'m','h',15,'v1',9,21,14,0.0006,5.0,2.0,3.0,50,50,5,25,3,30,"
                ":fp,'{}','2026-01-01',:st)")
        for i in range(3):     # três ENCERRADOS com o mesmo fingerprint
            c.execute(text(base), {"uid": f"u{i}", "fp": "mesmo", "st": "ENCERRADO"})
        c.execute(text(base), {"uid": "ativo", "fp": "mesmo", "st": "ATIVO"})
        n = c.execute(text("SELECT COUNT(*) FROM shadow_experiments")).scalar()
        assert n == 4, "vários encerrados com o mesmo fingerprint são legítimos"
