"""Fase 3.5 — testes direcionados do módulo de diagnóstico do funil e do
painel Shadow/Diagnóstico: reconciliação de contagens, distribuição de
cobertura, categorização de rejeições, MFE/MAE, marcos de progresso,
comparação baseline/H2, significância estatística, isolamento e
segurança do HTML novo.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_diagnostics
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import (
    Candle,
    Execution,
    Order,
    Position,
    RiskEvaluation,
    ShadowExperiment,
    ShadowOpportunity,
    ShadowTrade,
    StrategySignal,
)
from app.shadow import diagnostics
from app.shadow.engine import MODEL_BASELINE, MODEL_H2, ShadowEngine
from app.strategy.engine import StrategyConfig

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _experimento_ativo(session, model, exp_id=1, started_at=T0):
    """Fixture mínima: um `ShadowExperiment` ATIVO real -- necessário desde
    a correção da auditoria, que escopa comparison/significance/progress/
    trade_quality pelo experimento ATIVO, nunca por `model` sozinho."""
    session.add(ShadowExperiment(
        id=exp_id, experiment_uid=f"uid-teste-{exp_id}", model=model,
        hypothesis_version="h2-v1", strategy_timeframe_minutes=15, strategy_version="v1",
        fast_period=9, slow_period=21, atr_period=14, fee_rate=0.0006, slippage_bps=5.0,
        stop_loss_atr_multiple=2.0, take_profit_atr_multiple=3.0, max_position_usd=50.0,
        max_total_exposure_usd=50.0, min_order_notional_usd=5.0, max_daily_loss_usd=25.0,
        cooldown_after_losses=3, cooldown_minutes=30, config_fingerprint=f"fp-{exp_id}",
        config_snapshot_json="{}", started_at=started_at, status="ATIVO",
    ))
    session.flush()


@pytest.fixture()
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'diag.db'}")
    init_db(eng)
    return make_session_factory(eng)


def _sinal(session, symbol, direction, candle_time, atr=1.0, price=100.0):
    s = StrategySignal(symbol=symbol, direction=direction,
                       justification="teste", observed_price=price, atr=atr,
                       params_json="{}", created_at=candle_time + timedelta(minutes=15),
                       source_candle_open_time=candle_time)
    session.add(s)
    session.flush()
    return s


def _avaliacao(session, signal_id, approved, coverage, required=3.0, reason="teste"):
    checks = {"cost_gate": {"achieved_coverage_ratio": coverage, "required_coverage_ratio": required,
                            "cost_coverage_ok": coverage >= required}}
    session.add(RiskEvaluation(signal_id=signal_id, approved=approved, reason=reason,
                               checks_json=json.dumps(checks)))


def _candle(session, symbol, open_time, o, h, l, cl, v=100.0):
    session.add(Candle(symbol=symbol, timeframe="1m", open_time=open_time,
                       open=o, high=h, low=l, close=cl, volume=v, source="replay"))


# =========================================================================
# FUNIL -- reconciliação de contagens
# =========================================================================

def test_funil_reconcilia_hold_acionaveis_e_avaliacoes(sf):
    with session_scope(sf) as s:
        for i in range(5):
            _candle(s, "BTCUSDT", T0 + timedelta(minutes=i), 100, 101, 99, 100)
        _sinal(s, "BTCUSDT", "HOLD", T0)
        _sinal(s, "BTCUSDT", "HOLD", T0 + timedelta(minutes=15))
        sig = _sinal(s, "BTCUSDT", "BUY", T0 + timedelta(minutes=30))
        _avaliacao(s, sig.id, approved=False, coverage=1.5)

    with session_scope(sf) as s:
        f = diagnostics.funnel(s, since=T0.isoformat(), until=(T0 + timedelta(hours=1)).isoformat())
        etapas = {e["etapa"]: e for e in f["etapas"]}
        assert etapas["candles_1m"]["total"] == 5
        assert etapas["sinais_gerados"]["total"] == 3
        assert etapas["sinais_acionaveis"]["total"] == 1
        assert etapas["avaliados_risk_engine"]["total"] == 1, "1:1 com acionaveis"
        assert etapas["aprovados_gate_operacional_real"]["total"] == 0
        assert etapas["ordens_criadas"]["total"] == 0
        assert etapas["posicoes_operacionais"]["total"] == 0


def test_funil_nunca_mistura_janela_com_lifetime(sf):
    """Um sinal FORA da janela nao pode contaminar a contagem."""
    with session_scope(sf) as s:
        _sinal(s, "BTCUSDT", "BUY", T0 - timedelta(days=10))   # fora da janela
        _sinal(s, "BTCUSDT", "BUY", T0)                         # dentro

    with session_scope(sf) as s:
        f = diagnostics.funnel(s, since=T0.isoformat(), until=(T0 + timedelta(hours=1)).isoformat())
        acionaveis = next(e for e in f["etapas"] if e["etapa"] == "sinais_acionaveis")
        assert acionaveis["total"] == 1, "o sinal antigo vazou pra dentro da janela"


# =========================================================================
# GATE DE CUSTOS -- distribuicao
# =========================================================================

def test_distribuicao_de_cobertura_percentis_e_faixas(sf):
    coberturas = [0.5, 0.8, 1.2, 1.4, 1.6, 1.9, 2.1, 2.5, 3.5, 4.0]
    with session_scope(sf) as s:
        for i, cov in enumerate(coberturas):
            sig = _sinal(s, "BTCUSDT", "BUY", T0 + timedelta(minutes=15 * i))
            _avaliacao(s, sig.id, approved=(cov >= 3.0), coverage=cov)

    with session_scope(sf) as s:
        cg = diagnostics.cost_gate_distribution(s, since=T0.isoformat(),
                                                until=(T0 + timedelta(days=1)).isoformat())
        assert cg["n"] == 10
        assert cg["minimo"] == 0.5
        assert cg["maximo"] == 4.0
        assert cg["distribuicao_por_faixa"]["abaixo_1_0x"] == 2       # 0.5, 0.8
        assert cg["distribuicao_por_faixa"]["1_0_a_1_5x"] == 2        # 1.2, 1.4
        assert cg["distribuicao_por_faixa"]["1_5_a_2_0x"] == 2        # 1.6, 1.9
        assert cg["distribuicao_por_faixa"]["2_0_a_3_0x"] == 2        # 2.1, 2.5
        assert cg["distribuicao_por_faixa"]["acima_3_0x"] == 2        # 3.5, 4.0
        assert sum(cg["distribuicao_por_faixa"].values()) == 10
        assert cg["acima_do_limiar_sem_ordem"] == 0, "os dois >=3.0x foram aprovados neste teste"


def test_cobertura_acima_do_limiar_mas_sem_ordem_e_detectada(sf):
    """Se outro gate bloqueasse apesar do custo aprovar, o diagnostico
    precisa acusar -- prova de que a deteccao funciona quando o caso existe."""
    with session_scope(sf) as s:
        sig = _sinal(s, "BTCUSDT", "BUY", T0)
        _avaliacao(s, sig.id, approved=False, coverage=3.5,
                  reason="bloqueado por outro motivo, nao custo")

    with session_scope(sf) as s:
        cg = diagnostics.cost_gate_distribution(s, since=T0.isoformat(),
                                                until=(T0 + timedelta(hours=1)).isoformat())
        assert cg["acima_do_limiar_sem_ordem"] == 1


# =========================================================================
# MOTIVOS DE REJEICAO -- categorizacao estavel
# =========================================================================

def test_categorizacao_de_motivos_de_rejeicao(sf):
    motor = ShadowEngine(strategy_config=StrategyConfig(timeframe_minutes=15),
                         strategy_timeframe_minutes=15)
    with session_scope(sf) as s:
        # aprovado (baseline) + rejeitado por separacao (H2)
        motor.on_strategy_signal(s, "BTCUSDT", "BUY", 100.0, 5.0, 101.0, 100.0, T0)
        # exposicao esgotada nos dois: outro simbolo, exposicao ja tomada pela baseline
        motor.on_strategy_signal(s, "ETHUSDT", "BUY", 100.0, 5.0, 100.3, 100.0,
                                 T0 + timedelta(minutes=15))

    with session_scope(sf) as s:
        r = diagnostics.rejection_reasons(s, since="2020-01-01T00:00:00Z")
        categorias = {item["categoria"] for item in r["ranking"]}
        assert "aprovado" in categorias
        assert "separacao_h2_insuficiente" in categorias
        assert "exposicao_esgotada" in categorias
        total_pct = sum(item["percentual"] for item in r["ranking"])
        assert abs(total_pct - 100.0) < 1e-6, "percentuais devem somar 100%"


def test_filtro_por_modelo_simbolo_e_direcao(sf):
    motor = ShadowEngine(strategy_config=StrategyConfig(timeframe_minutes=15),
                         strategy_timeframe_minutes=15)
    with session_scope(sf) as s:
        motor.on_strategy_signal(s, "BTCUSDT", "BUY", 100.0, 5.0, 101.0, 100.0, T0)

    with session_scope(sf) as s:
        so = diagnostics.rejection_reasons(s, model=MODEL_BASELINE, since="2020-01-01T00:00:00Z")
        assert so["total"] == 1
        errado = diagnostics.rejection_reasons(s, symbol="ETHUSDT", since="2020-01-01T00:00:00Z")
        assert errado["total"] == 0
        so_dir = diagnostics.rejection_reasons(s, direction="SELL", since="2020-01-01T00:00:00Z")
        assert so_dir["total"] == 0


# =========================================================================
# QUALIDADE -- MFE/MAE calculado corretamente contra candles conhecidos
# =========================================================================

def test_mfe_mae_calculado_contra_candles_conhecidos(sf):
    entrada = 100.0
    with session_scope(sf) as s:
        _experimento_ativo(s, MODEL_BASELINE, exp_id=1)
        # candle 1: sobe ate 103 (favoravel para BUY) | candle 2: cai ate 97 (adverso)
        _candle(s, "BTCUSDT", T0 + timedelta(minutes=1), 100, 103, 99, 101)
        _candle(s, "BTCUSDT", T0 + timedelta(minutes=2), 101, 102, 97, 98)
        s.add(ShadowTrade(
            experiment_id=1, model=MODEL_BASELINE, hypothesis_version="h2-v1",
            symbol="BTCUSDT", side="BUY", qty=1.0, entry_fill_price=entrada,
            exit_fill_price=98.0, notional_usd=100.0, stop_loss=95.0, take_profit=110.0,
            exit_reason="stop_loss", opened_candle_time=T0,
            closed_candle_time=T0 + timedelta(minutes=2), duration_minutes=2,
            gross_pnl_usd=-2.0, fees_usd=0.1, slippage_usd=0.05, net_pnl_usd=-2.15,
            normalized_separation=0.2,
        ))

    with session_scope(sf) as s:
        tq = diagnostics.trade_quality(s, model=MODEL_BASELINE)
        t = tq["trades"][0]
        # MFE: (103-100)/100 = 3.0% | MAE: (97-100)/100 = -3.0%
        assert t["mfe_pct"] == pytest.approx(3.0, abs=1e-6)
        assert t["mae_pct"] == pytest.approx(-3.0, abs=1e-6)


def test_trade_quality_limite_respeitado(sf):
    with session_scope(sf) as s:
        _experimento_ativo(s, MODEL_BASELINE, exp_id=1)
        for i in range(5):
            s.add(ShadowTrade(
                experiment_id=1, model=MODEL_BASELINE, hypothesis_version="h2-v1",
                symbol="BTCUSDT", side="BUY", qty=1.0, entry_fill_price=100.0,
                exit_fill_price=101.0, notional_usd=100.0, stop_loss=95.0, take_profit=110.0,
                exit_reason="take_profit", opened_candle_time=T0 + timedelta(minutes=i),
                closed_candle_time=T0 + timedelta(minutes=i + 1), duration_minutes=1,
                gross_pnl_usd=1.0, fees_usd=0.1, slippage_usd=0.05, net_pnl_usd=0.85,
                normalized_separation=0.2,
            ))
    with session_scope(sf) as s:
        tq = diagnostics.trade_quality(s, limit=2)
        assert len(tq["trades"]) == 2
        assert tq["limit"] == 2
        # limite fora do range e' clampado, nunca ilimitado
        tq2 = diagnostics.trade_quality(s, limit=10_000)
        assert tq2["limit"] <= 500


# =========================================================================
# PROGRESSO -- marcos
# =========================================================================

def test_marcos_de_progresso(sf):
    with session_scope(sf) as s:
        _experimento_ativo(s, MODEL_H2, exp_id=2)
        for i in range(20):
            s.add(ShadowTrade(
                experiment_id=2, model=MODEL_H2, hypothesis_version="h2-v1",
                symbol="BTCUSDT", side="BUY", qty=1.0, entry_fill_price=100.0,
                exit_fill_price=101.0, notional_usd=100.0, stop_loss=95.0, take_profit=110.0,
                exit_reason="take_profit", opened_candle_time=T0 + timedelta(minutes=i),
                closed_candle_time=T0 + timedelta(minutes=i + 1), duration_minutes=1,
                gross_pnl_usd=1.0, fees_usd=0.1, slippage_usd=0.05, net_pnl_usd=0.85,
                normalized_separation=0.2,
            ))
    with session_scope(sf) as s:
        p = diagnostics.progress(s)
        h2 = p[MODEL_H2]
        assert h2["trades_fechados"] == 20
        marcos = {m["alvo"]: m for m in h2["marcos"]}
        assert marcos[15]["atingido"] is True
        assert marcos[30]["atingido"] is False
        assert marcos[30]["faltam"] == 10
        assert p[MODEL_BASELINE]["trades_fechados"] == 0


# =========================================================================
# COMPARACAO -- sequencia de perdas e duracao media
# =========================================================================

def test_comparacao_sequencia_de_perdas_e_duracao(sf):
    resultados = [1.0, -1.0, -1.0, -1.0, 2.0, -1.0]   # streak max = 3
    with session_scope(sf) as s:
        _experimento_ativo(s, MODEL_BASELINE, exp_id=1)
        for i, net in enumerate(resultados):
            s.add(ShadowTrade(
                experiment_id=1, model=MODEL_BASELINE, hypothesis_version="h2-v1",
                symbol="BTCUSDT", side="BUY", qty=1.0, entry_fill_price=100.0,
                exit_fill_price=101.0, notional_usd=100.0, stop_loss=95.0, take_profit=110.0,
                exit_reason="take_profit" if net > 0 else "stop_loss",
                opened_candle_time=T0 + timedelta(minutes=i * 10),
                closed_candle_time=T0 + timedelta(minutes=i * 10 + 5), duration_minutes=5,
                gross_pnl_usd=net, fees_usd=0.0, slippage_usd=0.0, net_pnl_usd=net,
                normalized_separation=0.2,
            ))
    with session_scope(sf) as s:
        comp = diagnostics.comparison(s)
        b = comp[MODEL_BASELINE]
        assert b["max_losing_streak"] == 3
        assert b["avg_duration_minutes"] == pytest.approx(5.0)
        assert b["current_position"] is None


# =========================================================================
# SIGNIFICANCIA -- nunca declara conclusao formal com amostra pequena
# =========================================================================

def test_significancia_marca_amostra_insuficiente_abaixo_de_60(sf):
    with session_scope(sf) as s:
        _experimento_ativo(s, MODEL_BASELINE, exp_id=1)
        for i in range(12):
            s.add(ShadowTrade(
                experiment_id=1, model=MODEL_BASELINE, hypothesis_version="h2-v1",
                symbol="BTCUSDT", side="BUY", qty=1.0, entry_fill_price=100.0,
                exit_fill_price=101.0, notional_usd=100.0, stop_loss=95.0, take_profit=110.0,
                exit_reason="take_profit", opened_candle_time=T0 + timedelta(minutes=i),
                closed_candle_time=T0 + timedelta(minutes=i + 1), duration_minutes=1,
                gross_pnl_usd=1.0, fees_usd=0.0, slippage_usd=0.0, net_pnl_usd=1.0,
                normalized_separation=0.2,
            ))
    with session_scope(sf) as s:
        sig = diagnostics.significance(s)
        b = sig[MODEL_BASELINE]
        assert b["n_trades"] == 12
        assert b["amostra_suficiente_para_conclusao_formal"] is False
        assert 0.0 <= b["ic95_win_rate"][0] <= b["ic95_win_rate"][1] <= 1.0


# =========================================================================
# ISOLAMENTO -- diagnostico nunca cria Order/Execution/Position, nunca escreve
# =========================================================================

def test_diagnostico_e_puramente_leitura(sf):
    motor = ShadowEngine(strategy_config=StrategyConfig(timeframe_minutes=15),
                         strategy_timeframe_minutes=15)
    with session_scope(sf) as s:
        motor.on_strategy_signal(s, "BTCUSDT", "BUY", 100.0, 5.0, 101.0, 100.0, T0)

    with session_scope(sf) as s:
        diagnostics.funnel(s, since=T0.isoformat(), until=(T0 + timedelta(hours=1)).isoformat())
        diagnostics.cost_gate_distribution(s, since=T0.isoformat())
        diagnostics.rejection_reasons(s, since="2020-01-01T00:00:00Z")
        diagnostics.progress(s)
        diagnostics.comparison(s)
        diagnostics.significance(s)
        diagnostics.trade_quality(s)
        diagnostics.recent_events(s)

    with session_scope(sf) as s:
        assert s.query(Order).count() == 0
        assert s.query(Execution).count() == 0
        assert s.query(Position).count() == 0
        # nao duplicou nem alterou as oportunidades originais
        assert s.query(ShadowOpportunity).count() == 2   # baseline + H2 do unico sinal


# =========================================================================
# ENDPOINTS -- contrato HTTP (limite, filtro, 404 de modelo invalido)
# =========================================================================

def _cliente(sf):
    app = FastAPI()

    class _Orch:
        session_factory = sf

        @staticmethod
        def portfolio_status():
            # necessário para /diagnostics/dashboard (via _symbols_health_dict) --
            # sem isso, cairia no fallback monoativo que exige orch.settings.
            return {"portfolio": {"status": "SAUDAVEL", "healthy_count": 3, "total": 3},
                    "per_symbol": {}}

    app.state.orchestrator = _Orch()
    app.include_router(routes_diagnostics.router, prefix="/api")
    return TestClient(app)


def test_endpoints_respondem_200_e_modelo_invalido_da_404(sf):
    client = _cliente(sf)
    for rota in ("/api/diagnostics/funnel", "/api/diagnostics/cost-gate",
                 "/api/diagnostics/rejections", "/api/diagnostics/trade-quality",
                 "/api/diagnostics/progress", "/api/diagnostics/comparison",
                 "/api/diagnostics/significance", "/api/diagnostics/events"):
        r = client.get(rota)
        assert r.status_code == 200, rota

    r = client.get("/api/diagnostics/rejections", params={"model": "modelo-inexistente"})
    assert r.status_code == 404


def test_endpoint_trade_quality_aceita_limite(sf):
    client = _cliente(sf)
    r = client.get("/api/diagnostics/trade-quality", params={"limit": 3})
    assert r.status_code == 200
    assert r.json()["limit"] == 3


# =========================================================================
# ENDPOINT CONSOLIDADO -- /api/diagnostics/dashboard, escopo custom
# =========================================================================

def test_dashboard_consolidado_responde_200_com_todas_as_secoes(sf):
    client = _cliente(sf)
    r = client.get("/api/diagnostics/dashboard")
    assert r.status_code == 200
    corpo = r.json()
    for chave in ("generated_at", "scope", "window", "experiments", "funnel",
                  "cost_gate", "rejections", "comparison", "trade_quality",
                  "progress", "significance", "events", "economic_verdict",
                  "operational", "shadow_health"):
        assert chave in corpo, f"secao ausente na resposta consolidada: {chave}"
    assert corpo["scope"] == "experiment"


def test_dashboard_escopo_custom_usa_since_until_exatos(sf):
    client = _cliente(sf)
    since = "2026-09-01T00:00:00+00:00"
    until = "2026-09-02T00:00:00+00:00"
    r = client.get("/api/diagnostics/dashboard",
                    params={"scope": "custom", "since": since, "until": until})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["scope"] == "custom"
    assert corpo["window"]["desde"] == since
    assert corpo["window"]["ate"] == until
    assert corpo["window"]["rotulo"] == "personalizado"


def test_dashboard_escopo_invalido_da_422(sf):
    client = _cliente(sf)
    r = client.get("/api/diagnostics/dashboard", params={"scope": "mes-inteiro"})
    assert r.status_code == 422



# =========================================================================
# SEGURANCA DO NOVO HTML -- nenhum innerHTML com dado dinamico, zero CDN
# =========================================================================

SHADOW_HTML = Path(__file__).resolve().parents[1] / "frontend" / "shadow.html"


def test_novo_painel_nao_usa_innerhtml_com_dado_dinamico():
    """Aceita innerHTML apenas com string LITERAL fixa (mensagens de estado
    vazio/erro escritas por nos, nunca com valor vindo da API)."""
    if not SHADOW_HTML.exists():
        pytest.skip("frontend/shadow.html ainda nao existe nesta worktree")
    texto = SHADOW_HTML.read_text(encoding="utf-8")
    for m in re.finditer(r"\.innerHTML\s*=\s*(.+?);", texto):
        expressao = m.group(1).strip()
        # permite apenas string literal (aspas simples/duplas/template sem ${)
        literal = re.fullmatch(r"""(['"]).*\1""", expressao) or (
            expressao.startswith("`") and expressao.endswith("`") and "${" not in expressao)
        assert literal, f"innerHTML com expressao dinamica suspeita: {expressao[:80]}"


def test_novo_painel_sem_cdn_nem_requisicao_externa():
    if not SHADOW_HTML.exists():
        pytest.skip("frontend/shadow.html ainda nao existe nesta worktree")
    texto = SHADOW_HTML.read_text(encoding="utf-8")
    proibidos = ("cdn.", "unpkg.", "jsdelivr.", "http://", "https://")
    for termo in proibidos:
        # permite mencao em comentario/texto explicativo, proibe em src=/href=/fetch(
        for m in re.finditer(r'(?:src|href)\s*=\s*["\'][^"\']*' + re.escape(termo), texto):
            pytest.fail(f"referencia externa via src/href: {m.group(0)}")
        for m in re.finditer(r'fetch\(\s*["\'][^"\']*' + re.escape(termo), texto):
            pytest.fail(f"fetch para URL externa: {m.group(0)}")


def test_novo_painel_nunca_faz_post():
    if not SHADOW_HTML.exists():
        pytest.skip("frontend/shadow.html ainda nao existe nesta worktree")
    texto = SHADOW_HTML.read_text(encoding="utf-8")
    assert "method:" not in texto or "POST" not in texto.upper(), \
        "painel shadow deve ser estritamente somente-leitura"
