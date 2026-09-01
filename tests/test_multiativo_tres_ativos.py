"""Fase 3.2 (ajuste obrigatório do PO): prova multiativo REALISTA com
três ativos -- BTCUSDT, ETHUSDT e SOLUSDT -- cada um com a SUA série
sintética, em escala e formato próprios.

Antes deste ajuste, todo símbolo em REPLAY lia `replay_btcusdt.json`
apenas trocando o rótulo: ETH aparecia cotado perto de US$ 40.000 e
produzia exatamente os mesmos sinais do BTC nos mesmos horários. Isso não
provava isolamento -- provava só que o mesmo arquivo tinha sido lido três
vezes.

BYBIT_DEMO permanece MONOATIVO nesta fase; nada aqui o amplia.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes_dashboard
from app.api.main import build_orchestrator, replay_fixture_for
from app.core.config import RunMode, Settings
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import StrategySignal
from tests.factories import activate_operational_state

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _build(tmp_path, db_name, symbols=None, minutes=5):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=",".join(symbols or SYMBOLS),
        database_url=f"sqlite:///{tmp_path / db_name}",
        strategy_timeframe_minutes=minutes,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    return orch


def _run(orch, max_ticks=1200):
    for _ in range(max_ticks):
        if orch.tick().get("status") == "no_data":
            break


def _client(orch):
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app)


# --- as fixtures em si ----------------------------------------------------

def test_each_symbol_has_its_own_synthetic_fixture():
    paths = {s: replay_fixture_for(s) for s in SYMBOLS}
    assert len({p.name for p in paths.values()}) == 3, "cada símbolo precisa de arquivo próprio"
    for symbol, path in paths.items():
        assert path.name == f"replay_{symbol.lower()}.json"
        assert path.exists()


def test_a_symbol_without_its_own_fixture_is_refused_never_borrowed():
    """Correção final da auditoria do PO: NÃO existe mais fallback. Um
    símbolo sem fixture própria interrompe a inicialização em vez de
    emprestar a série de outro ativo (o que produziria preço falso,
    sinais duplicados e métricas contaminadas). Cobertura completa em
    tests/test_fase32_correcoes_finais.py."""
    from app.core.errors import ReplayFixtureMissingError

    with pytest.raises(ReplayFixtureMissingError):
        replay_fixture_for("XRPUSDT")


def test_the_three_series_live_in_clearly_different_price_scales():
    ranges = {}
    for symbol in SYMBOLS:
        rows = json.loads(replay_fixture_for(symbol).read_text(encoding="utf-8"))
        closes = [r["close"] for r in rows]
        ranges[symbol] = (min(closes), max(closes))
        assert len(rows) == 180

    btc_lo, btc_hi = ranges["BTCUSDT"]
    eth_lo, eth_hi = ranges["ETHUSDT"]
    sol_lo, sol_hi = ranges["SOLUSDT"]

    # Faixas de preço DISJUNTAS -- nenhum preço de um ativo pode aparecer
    # no outro, nem por coincidência.
    assert eth_hi < btc_lo
    assert sol_hi < eth_lo
    # E em ordens de grandeza distintas, não apenas deslocadas.
    assert btc_lo / eth_hi > 10
    assert eth_lo / sol_hi > 10


def test_the_three_series_are_not_the_same_shape_relabelled():
    series = {}
    for symbol in SYMBOLS:
        rows = json.loads(replay_fixture_for(symbol).read_text(encoding="utf-8"))
        closes = [r["close"] for r in rows]
        base = closes[0]
        # Formato normalizado: se uma série fosse a outra apenas
        # reescalada, estes vetores seriam idênticos.
        series[symbol] = [round(c / base, 6) for c in closes]

    assert series["BTCUSDT"] != series["ETHUSDT"]
    assert series["BTCUSDT"] != series["SOLUSDT"]
    assert series["ETHUSDT"] != series["SOLUSDT"]
    # BTC sobe e depois cai; ETH faz o inverso -- direções opostas no fim.
    assert series["BTCUSDT"][-1] < series["BTCUSDT"][90]
    assert series["ETHUSDT"][-1] > series["ETHUSDT"][90]


# --- isolamento real em execução -----------------------------------------

def test_cursors_buckets_and_warmup_are_independent_per_symbol(tmp_path):
    orch = _build(tmp_path, "tres_isolamento.db")
    _run(orch)

    subs = {s: orch.orchestrators[s] for s in SYMBOLS}
    assert len({id(o.aggregator) for o in subs.values()}) == 3
    assert len({id(o.strategy_engine) for o in subs.values()}) == 3
    assert len({id(o.market_data_provider) for o in subs.values()}) == 3
    # Cursores próprios, cada um sobre o SEU arquivo.
    assert len({o.market_data_provider._candles[0]["close"] for o in subs.values()}) == 3

    for symbol, sub in subs.items():
        assert sub.aggregator.stats.complete_buckets == 36
        assert sub.strategy_engine.warmup_state()["ready"] is True
        with session_scope(orch.session_factory) as session:
            candles = repo.recent_candles(session, symbol, "1m", limit=1000)
        assert len(candles) == 180


def test_sma_and_atr_differ_between_the_three_symbols(tmp_path):
    orch = _build(tmp_path, "tres_indicadores.db")
    _run(orch)

    indicators = {s: orch.orchestrators[s].strategy_engine.current_indicators() for s in SYMBOLS}
    for field in ("fast_sma", "slow_sma", "atr_per_unit_usd", "last_close"):
        values = [indicators[s][field] for s in SYMBOLS]
        assert all(v is not None for v in values), field
        assert len({round(v, 6) for v in values}) == 3, f"{field} repetido entre símbolos"

    # ATR% também difere -- não é só a escala do preço que muda.
    pcts = [round(indicators[s]["atr_pct_of_price"], 8) for s in SYMBOLS]
    assert len(set(pcts)) == 3


def test_signals_never_repeat_prices_or_direction_patterns_across_symbols(tmp_path):
    orch = _build(tmp_path, "tres_sinais.db")
    _run(orch)

    with session_scope(orch.session_factory) as session:
        rows = session.execute(select(StrategySignal).order_by(StrategySignal.id)).scalars().all()
        by_symbol = {s: [(r.source_candle_open_time, r.observed_price, r.direction)
                          for r in rows if r.symbol == s] for s in SYMBOLS}

    for symbol in SYMBOLS:
        assert len(by_symbol[symbol]) == 36

    # Nenhum PREÇO observado se repete entre ativos (o defeito exato do
    # antes: ETH exibindo preços de BTC).
    prices = {s: {round(p, 6) for _t, p, _d in by_symbol[s]} for s in SYMBOLS}
    assert prices["BTCUSDT"].isdisjoint(prices["ETHUSDT"])
    assert prices["BTCUSDT"].isdisjoint(prices["SOLUSDT"])
    assert prices["ETHUSDT"].isdisjoint(prices["SOLUSDT"])

    # E a sequência de direções não é a mesma dos outros -- os
    # cruzamentos acontecem em buckets diferentes.
    patterns = {s: tuple(d for _t, _p, d in by_symbol[s]) for s in SYMBOLS}
    assert len(set(patterns.values())) == 3


def test_a_gap_in_one_symbol_never_leaks_into_the_others(tmp_path):
    orch = _build(tmp_path, "tres_gap.db")
    # Buraco APENAS na série do ETH.
    for _ in range(9):
        orch.tick()
    orch.orchestrators["ETHUSDT"].market_data_provider._cursor += 2
    _run(orch)

    stats = {s: orch.orchestrators[s].aggregator.stats for s in SYMBOLS}
    assert stats["ETHUSDT"].incomplete_buckets >= 1
    assert stats["BTCUSDT"].incomplete_buckets == 0
    assert stats["SOLUSDT"].incomplete_buckets == 0


def test_one_failing_symbol_never_stops_the_others(tmp_path):
    class _BrokenProvider:
        """Provider que sempre falha, mas com a MESMA superfície do real."""

        symbol = "ETHUSDT"
        timeframe = "1m"

        def next_candle(self):
            from app.market_data.base import CandleFetchResult, CandleFetchStatus

            return CandleFetchResult(
                status=CandleFetchStatus.RETRYABLE_ERROR, detail="falha sintética de teste",
            )

        def is_stale(self, max_staleness_seconds: float) -> bool:
            return True

    orch = _build(tmp_path, "tres_falha.db")
    orch.orchestrators["ETHUSDT"].market_data_provider = _BrokenProvider()
    _run(orch)

    health = orch.portfolio_status()["per_symbol"]
    assert health["ETHUSDT"]["status"] in ("DEGRADADO", "PARADO")
    assert health["BTCUSDT"]["status"] == "SAUDAVEL"
    assert health["SOLUSDT"]["status"] == "SAUDAVEL"

    # Os outros dois continuaram processando a série inteira.
    with session_scope(orch.session_factory) as session:
        assert len(repo.recent_candles(session, "BTCUSDT", "1m", limit=1000)) == 180
        assert len(repo.recent_candles(session, "SOLUSDT", "1m", limit=1000)) == 180
        assert repo.recent_candles(session, "ETHUSDT", "1m", limit=1000) == []


# --- API e painel com três ativos ----------------------------------------

def test_chart_data_returns_a_distinct_series_per_symbol(tmp_path):
    orch = _build(tmp_path, "tres_chart.db")
    _run(orch)
    client = _client(orch)

    bodies = {s: client.get("/api/chart-data", params={"symbol": s}).json() for s in SYMBOLS}
    for symbol, body in bodies.items():
        assert body["symbol"] == symbol
        assert len(body["candles"]) == 180
        assert len(body["strategy_candles"]) == 36
        assert body["strategy_timeframe"] == "5m"

    closes = {s: {round(c["close"], 6) for c in bodies[s]["candles"]} for s in SYMBOLS}
    assert closes["BTCUSDT"].isdisjoint(closes["ETHUSDT"])
    assert closes["BTCUSDT"].isdisjoint(closes["SOLUSDT"])
    assert closes["ETHUSDT"].isdisjoint(closes["SOLUSDT"])

    smas = {s: bodies[s]["strategy_indicators"]["fast_sma"] for s in SYMBOLS}
    assert len({round(v, 6) for v in smas.values()}) == 3


def test_symbols_endpoint_lists_all_three_with_their_own_state(tmp_path):
    orch = _build(tmp_path, "tres_symbols.db")
    _run(orch)
    body = _client(orch).get("/api/symbols").json()

    assert body["symbols"] == SYMBOLS  # ordem de configuração preservada
    assert set(body["per_symbol"]) == set(SYMBOLS)
    for symbol in SYMBOLS:
        entry = body["per_symbol"][symbol]
        assert entry["strategy_timeframe"] == "5m"
        assert entry["warmup"]["ready"] is True
        assert entry["bucket_integrity"]["complete_buckets"] == 36


def test_symbol_summary_table_data_covers_the_three_assets(tmp_path):
    """A tabela "Resumo por Símbolo" precisa mostrar TRÊS ativos com
    preços próprios. Antes deste ajuste ela exibia N/D em todos, porque só
    havia preço quando existia posição aberta -- escondendo exatamente a
    prova de que cada série tem preço próprio."""
    orch = _build(tmp_path, "tres_resumo.db")
    _run(orch)
    client = _client(orch)
    summary = client.get("/api/portfolio-summary").json()

    assert set(summary["per_symbol"]) == set(SYMBOLS)
    marks = {s: summary["per_symbol"][s]["mark_price"] for s in SYMBOLS}
    assert all(isinstance(v, float) for v in marks.values())
    assert len({round(v, 6) for v in marks.values()}) == 3
    assert marks["BTCUSDT"] > marks["ETHUSDT"] > marks["SOLUSDT"]
    for symbol in SYMBOLS:
        assert summary["per_symbol"][symbol]["mark_price_source"] == "last_closed_candle"

    # E coincidem com o preço que a rota do gráfico mostra para o mesmo
    # símbolo -- uma única fonte de marcação, nunca duas divergentes.
    for symbol in SYMBOLS:
        body = client.get("/api/chart-data", params={"symbol": symbol}).json()
        assert body["visual_price"] == pytest.approx(marks[symbol])


def test_global_metrics_consolidate_the_three_symbols(tmp_path):
    orch = _build(tmp_path, "tres_metricas.db")
    _run(orch)
    body = _client(orch).get("/api/metrics").json()

    assert set(body["per_symbol"]) == set(SYMBOLS)
    gate = body["cost_gate"]
    per_symbol_evaluated = sum(body["per_symbol"][s]["cost_gate"]["evaluated"] for s in SYMBOLS)
    per_symbol_blocked = sum(body["per_symbol"][s]["cost_gate"]["blocked_entries"] for s in SYMBOLS)
    # O consolidado global é a soma dos três -- nunca a média dos
    # percentuais nem o valor de um símbolo só.
    assert gate["evaluated"] == per_symbol_evaluated
    assert gate["blocked_entries"] == per_symbol_blocked
    assert body["incomplete_buckets"] == sum(
        body["per_symbol"][s]["bucket_integrity"]["incomplete_buckets"] for s in SYMBOLS
    )


# --- banner por modo ------------------------------------------------------

def test_replay_banner_never_claims_to_be_paper_live(tmp_path):
    orch = _build(tmp_path, "tres_banner.db")
    _run(orch)
    body = _client(orch).get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()

    assert body["chart_banner"] == (
        "REPLAY MULTIATIVO — DADOS HISTÓRICOS/SINTÉTICOS — SEM MERCADO REAL"
    )
    assert "PAPER LIVE" not in body["chart_banner"]
    assert body["data_disclaimer"] == "Dados REPLAY sintéticos — sem cotação real"


def test_replay_monoativo_banner_omits_the_multiativo_word(tmp_path):
    orch = _build(tmp_path, "mono_banner.db", symbols=["BTCUSDT"])
    for _ in range(10):
        orch.tick()
    body = _client(orch).get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()
    assert body["chart_banner"] == "REPLAY — DADOS HISTÓRICOS/SINTÉTICOS — SEM MERCADO REAL"


def test_paper_live_and_bybit_demo_banners_are_distinct_and_honest():
    class _FakeOrch:
        def __init__(self, mode, symbols):
            self.settings = Settings(
                mode=mode, symbols=",".join(symbols),
                bybit_api_key="k" if mode == RunMode.BYBIT_DEMO else "",
                bybit_api_secret="s" if mode == RunMode.BYBIT_DEMO else "",
            )

    paper = routes_dashboard._chart_banner(_FakeOrch(RunMode.PAPER_LIVE, ["BTCUSDT", "ETHUSDT"]))
    assert paper == "PAPER LIVE MULTIATIVO — SIMULAÇÃO LOCAL — SEM ORDEM NA CORRETORA"
    assert routes_dashboard._data_disclaimer(_FakeOrch(RunMode.PAPER_LIVE, ["BTCUSDT"])) is None

    demo = routes_dashboard._chart_banner(_FakeOrch(RunMode.BYBIT_DEMO, ["BTCUSDT"]))
    assert demo == "BYBIT DEMO — MONOATIVO — CONTA DEMO DA CORRETORA, SEM DINHEIRO REAL"
    assert "MULTIATIVO" not in demo  # BYBIT_DEMO segue monoativo nesta fase
    assert routes_dashboard._data_disclaimer(_FakeOrch(RunMode.BYBIT_DEMO, ["BTCUSDT"])) is None


def test_bybit_demo_remains_monoativo(monkeypatch):
    """Nada neste ajuste amplia BYBIT_DEMO para multiativo -- a guarda da
    Fase 3 (item 1) continua recusando a inicialização, no mesmo ponto de
    sempre (`get_settings`)."""
    from app.core.config import MultiSymbolNotSupportedError, get_settings

    for key, value in {
        "MODE": "BYBIT_DEMO", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
        "SYMBOLS": "BTCUSDT,ETHUSDT",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SYMBOL", raising=False)

    with pytest.raises(MultiSymbolNotSupportedError):
        get_settings()
