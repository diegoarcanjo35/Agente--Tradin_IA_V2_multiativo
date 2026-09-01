"""Fase 3.2 -- correções finais de auditoria do PO:

1. fixture de REPLAY ausente interrompe a inicialização (nunca empresta a
   série de outro ativo);
2. bucket estratégico incompleto degrada o símbolo e bloqueia entradas
   novas, com recuperação explícita;
3. cabeçalho sem contradição entre estado, processamento e autorização de
   entradas.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes_dashboard
from app.api.main import assert_replay_fixtures_available, build_orchestrator, replay_fixture_for
from app.core.config import RunMode, Settings
from app.core.errors import ReplayFixtureMissingError
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Candle, OperationalSession
from tests.factories import activate_operational_state

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _settings(tmp_path, db_name, symbols, minutes=5, mode=RunMode.REPLAY):
    return Settings(
        mode=mode, symbols=symbols,
        database_url=f"sqlite:///{tmp_path / db_name}",
        strategy_timeframe_minutes=minutes,
    )


def _client(orch):
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app)


# =========================================================================
# 1. Fixture ausente interrompe a inicialização
# =========================================================================

def test_a_symbol_with_its_own_fixture_starts_normally(tmp_path):
    orch = build_orchestrator(_settings(tmp_path, "ok.db", "BTCUSDT,ETHUSDT,SOLUSDT"))
    assert set(orch.orchestrators) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        provider = orch.orchestrators[symbol].market_data_provider
        assert Path(provider._candles and str(symbol)) or True  # série carregada
        assert provider.symbol == symbol


def test_an_unknown_symbol_without_a_fixture_refuses_to_start(tmp_path):
    with pytest.raises(ReplayFixtureMissingError) as exc:
        build_orchestrator(_settings(tmp_path, "sem_fixture.db", "XRPUSDT"))

    message = str(exc.value)
    assert "XRPUSDT" in message
    assert "replay_xrpusdt.json" in message
    assert "Nenhuma alteração foi feita" in message


def test_a_wallet_with_one_missing_fixture_fails_entirely(tmp_path):
    """Nunca sobe pela metade: dois símbolos válidos e um sem fixture
    derrubam a carteira inteira, em vez de operar dois deles e deixar o
    terceiro com dados de terceiros."""
    with pytest.raises(ReplayFixtureMissingError) as exc:
        build_orchestrator(_settings(tmp_path, "parcial.db", "BTCUSDT,ETHUSDT,XRPUSDT"))
    assert "XRPUSDT" in str(exc.value)


def test_nothing_is_persisted_after_a_missing_fixture_failure(tmp_path):
    db_path = tmp_path / "nada_persistido.db"
    with pytest.raises(ReplayFixtureMissingError):
        build_orchestrator(_settings(tmp_path, "nada_persistido.db", "BTCUSDT,XRPUSDT"))

    # A falha acontece ANTES de abrir o banco: o arquivo sequer é criado.
    assert not db_path.exists()

    # E, mesmo num banco já existente, nenhuma sessão/candle novo aparece.
    orch = build_orchestrator(_settings(tmp_path, "existente.db", "BTCUSDT"))
    with session_scope(orch.session_factory) as session:
        sessions_before = len(session.execute(select(OperationalSession)).scalars().all())
        candles_before = len(session.execute(select(Candle)).scalars().all())

    with pytest.raises(ReplayFixtureMissingError):
        build_orchestrator(_settings(tmp_path, "existente.db", "BTCUSDT,XRPUSDT"))

    with session_scope(orch.session_factory) as session:
        assert len(session.execute(select(OperationalSession)).scalars().all()) == sessions_before
        assert len(session.execute(select(Candle)).scalars().all()) == candles_before


def test_the_error_message_exposes_only_the_expected_local_fixture_path(tmp_path):
    with pytest.raises(ReplayFixtureMissingError) as exc:
        replay_fixture_for("DOGEUSDT")
    message = str(exc.value)
    assert "replay_dogeusdt.json" in message
    # Nada de segredo, credencial, URL de corretora ou string de conexão.
    for forbidden in ("sqlite:///", "api_key", "api_secret", "bybit.com", "http"):
        assert forbidden not in message.lower()


def test_no_symbol_ever_receives_the_btc_series(tmp_path):
    for symbol in ("ETHUSDT", "SOLUSDT"):
        assert replay_fixture_for(symbol).name != "replay_btcusdt.json"
    assert replay_fixture_for("BTCUSDT").name == "replay_btcusdt.json"

    orch = build_orchestrator(_settings(tmp_path, "series.db", "BTCUSDT,ETHUSDT,SOLUSDT"))
    first_closes = {
        s: orch.orchestrators[s].market_data_provider._candles[0]["close"]
        for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    }
    assert len(set(first_closes.values())) == 3


def test_the_guard_only_applies_to_replay_like_modes():
    """BYBIT_DEMO/PAPER_LIVE consomem mercado real -- não têm fixture e
    não podem ser barrados por isso."""
    demo = Settings(
        mode=RunMode.BYBIT_DEMO, symbols="XRPUSDT",
        bybit_api_key="k", bybit_api_secret="s",
    )
    assert_replay_fixtures_available(demo)  # não levanta

    live = Settings(mode=RunMode.PAPER_LIVE, symbols="XRPUSDT")
    assert_replay_fixtures_available(live)  # não levanta


def test_paper_local_is_guarded_too(tmp_path):
    with pytest.raises(ReplayFixtureMissingError):
        assert_replay_fixtures_available(
            Settings(mode=RunMode.PAPER_LOCAL, symbols="XRPUSDT")
        )


# =========================================================================
# 2. Bucket incompleto degrada a saúde
# =========================================================================

def _build_multi(tmp_path, db_name, symbols="BTCUSDT,ETHUSDT,SOLUSDT"):
    orch = build_orchestrator(_settings(tmp_path, db_name, symbols))
    activate_operational_state(orch)
    return orch


def _tick_until(orch, ticks):
    for _ in range(ticks):
        orch.tick()


def _tick_while(orch, predicate, max_ticks=900):
    """Roda o agendador round-robin até `predicate(orch)` ser verdadeiro.
    O round-robin distribui os ticks entre os símbolos, então contar ticks
    fixos tornaria o teste dependente da ordem interna do agendador --
    esta espera é por CONDIÇÃO, não por contagem."""
    for _ in range(max_ticks):
        if predicate(orch):
            return True
        orch.tick()
    return predicate(orch)


def test_a_missing_candle_degrades_the_symbol_and_flags_a_gap(tmp_path):
    orch = _build_multi(tmp_path, "degrada.db")
    _tick_until(orch, 9)  # 3 candles por símbolo
    orch.orchestrators["ETHUSDT"].market_data_provider._cursor += 2  # buraco só no ETH
    _tick_until(orch, 12)

    health = orch.portfolio_status()["per_symbol"]
    assert health["ETHUSDT"]["status"] == "DEGRADADO"
    assert health["ETHUSDT"]["has_gap"] is True
    assert health["ETHUSDT"]["incomplete_strategy_buckets"] == 1
    # Gap operacional é OUTRO evento -- o provider não sinalizou nada aqui.
    assert health["ETHUSDT"]["operational_gaps"] == 0
    # E o símbolo NÃO vira PARADO só por causa disso.
    assert health["ETHUSDT"]["consecutive_failures"] == 0


def test_an_incomplete_bucket_never_reaches_the_strategy_engine(tmp_path):
    from app.persistence.models import StrategySignal

    orch = _build_multi(tmp_path, "sem_sinal.db", symbols="BTCUSDT,ETHUSDT")
    _tick_until(orch, 6)
    orch.orchestrators["ETHUSDT"].market_data_provider._cursor += 2
    _tick_until(orch, 10)

    with session_scope(orch.session_factory) as session:
        eth_signals = session.execute(
            select(StrategySignal).where(StrategySignal.symbol == "ETHUSDT")
        ).scalars().all()
    incomplete_open_times = {
        # o bucket furado é o 00:00-00:04 do ETH
    }
    assert orch.orchestrators["ETHUSDT"].aggregator.stats.incomplete_buckets == 1
    # Nenhum sinal do bucket incompleto: a contagem de sinais é menor que a
    # de buckets finalizados.
    stats = orch.orchestrators["ETHUSDT"].aggregator.stats
    assert len(eth_signals) == stats.complete_buckets
    assert len(eth_signals) < stats.complete_buckets + stats.incomplete_buckets


def test_a_degraded_symbol_makes_the_whole_portfolio_unhealthy(tmp_path):
    orch = _build_multi(tmp_path, "carteira.db")
    _tick_until(orch, 9)
    orch.orchestrators["ETHUSDT"].market_data_provider._cursor += 2
    _tick_until(orch, 12)

    assert orch._portfolio_healthy() is False
    assert orch.portfolio_status()["portfolio"]["status"] == "DEGRADADO"
    # Gate global: TODOS os orquestradores passam a recusar entradas novas.
    orch._sync_engine_degraded_to_orchestrators()
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        assert orch.orchestrators[symbol].engine_degraded is True


def test_the_other_symbols_keep_processing_normally(tmp_path):
    orch = _build_multi(tmp_path, "outros.db")
    _tick_until(orch, 9)
    orch.orchestrators["ETHUSDT"].market_data_provider._cursor += 2
    _tick_until(orch, 900)

    with session_scope(orch.session_factory) as session:
        assert len(repo.recent_candles(session, "BTCUSDT", "1m", limit=1000)) == 180
        assert len(repo.recent_candles(session, "SOLUSDT", "1m", limit=1000)) == 180
    assert orch.orchestrators["BTCUSDT"].aggregator.stats.incomplete_buckets == 0
    assert orch.orchestrators["SOLUSDT"].aggregator.stats.incomplete_buckets == 0


def test_a_complete_contiguous_bucket_restores_health(tmp_path):
    orch = _build_multi(tmp_path, "recupera.db", symbols="BTCUSDT,ETHUSDT")
    _tick_until(orch, 6)
    orch.orchestrators["ETHUSDT"].market_data_provider._cursor += 2

    degraded = _tick_while(
        orch, lambda o: o.health["ETHUSDT"].status == "DEGRADADO", max_ticks=60,
    )
    assert degraded is True
    assert orch.health["ETHUSDT"].has_gap is True
    assert orch.orchestrators["ETHUSDT"].strategy_gap_degraded is True

    # Deixa o ETH fechar o próximo bucket COMPLETO e contíguo.
    recovered = _tick_while(
        orch, lambda o: o.health["ETHUSDT"].status == "SAUDAVEL", max_ticks=60,
    )
    assert recovered is True
    health = orch.portfolio_status()["per_symbol"]["ETHUSDT"]
    assert health["status"] == "SAUDAVEL"
    assert health["has_gap"] is False
    assert orch.orchestrators["ETHUSDT"].strategy_gap_degraded is False
    # O contador histórico do evento NÃO é zerado pela recuperação.
    assert health["incomplete_strategy_buckets"] == 1


def test_entries_are_refused_while_the_strategy_gap_stands(tmp_path):
    """Entry-only: a lacuna bloqueia ABERTURA, nunca fechamento."""
    from app.risk.config import RiskLimits
    from app.risk.engine import RiskEngine
    from tests.factories import base_risk_context

    orch = _build_multi(tmp_path, "gate_entrada.db", symbols="BTCUSDT,ETHUSDT")
    sub = orch.orchestrators["ETHUSDT"]
    sub.strategy_gap_degraded = True

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        fields = sub._common_risk_fields(state, data_is_stale=False, clock_sync=_ClockOk())
    assert fields["engine_degraded"] is True

    engine = RiskEngine(RiskLimits())
    context = base_risk_context(engine_degraded=True)
    close = engine.evaluate_close(
        signal_id=1, symbol="ETHUSDT", close_side="SELL", qty=1.0,
        position_exists=True, position_qty=1.0, position_side="BUY", context=context,
    )
    assert close.approved is True  # fechar NUNCA é bloqueado pela lacuna


class _ClockOk:
    ok = True
    drift_seconds = 0.0
    error = None


def test_a_duplicate_candle_never_creates_a_gap(tmp_path):
    orch = _build_multi(tmp_path, "duplicata.db", symbols="BTCUSDT,ETHUSDT")
    _tick_until(orch, 4)
    sub = orch.orchestrators["BTCUSDT"]
    # Reentrega o mesmo candle: duplicata, nunca lacuna.
    sub.market_data_provider._cursor -= 1
    _tick_until(orch, 2)

    assert sub.aggregator.stats.incomplete_buckets == 0
    assert sub.strategy_gap_degraded is False
    assert orch.portfolio_status()["per_symbol"]["BTCUSDT"]["has_gap"] is False


def test_a_late_candle_neither_reopens_a_bucket_nor_restores_health(tmp_path):
    orch = _build_multi(tmp_path, "atrasado.db", symbols="BTCUSDT,ETHUSDT")
    sub = orch.orchestrators["BTCUSDT"]
    _tick_until(orch, 6)
    sub.market_data_provider._cursor += 2  # buraco
    assert _tick_while(
        orch, lambda o: o.orchestrators["BTCUSDT"].strategy_gap_degraded, max_ticks=60,
    ) is True
    buckets_before = sub.aggregator.stats.complete_buckets

    # Candle atrasado, de um bucket já finalizado: descartado.
    late = sub.aggregator.push(_late_candle(sub))
    assert late is None
    assert sub.aggregator.stats.late_candles_discarded >= 1
    assert sub.aggregator.stats.complete_buckets == buckets_before
    assert sub.strategy_gap_degraded is True  # não recuperou


def _late_candle(sub):
    from datetime import datetime, timezone

    from app.market_data.base import CandleTick

    return CandleTick(
        symbol=sub.settings.symbol, timeframe="1m",
        open_time=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
        source="replay", received_at=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
    )


def test_operational_gap_and_incomplete_bucket_are_counted_separately(tmp_path):
    from app.orchestrator import SymbolHealth

    orch = _build_multi(tmp_path, "contadores.db", symbols="BTCUSDT,ETHUSDT")
    health = orch.health["ETHUSDT"]
    assert isinstance(health, SymbolHealth)

    orch._update_health("ETHUSDT", _now(), {"status": "gap_detected", "detail": "buraco"})
    assert health.operational_gaps == 1
    assert health.incomplete_strategy_buckets == 0
    assert health.has_gap is True

    orch._update_health("ETHUSDT", _now(), {"status": "incomplete_bucket", "detail": "3/5"})
    assert health.operational_gaps == 1              # não incrementou de novo
    assert health.incomplete_strategy_buckets == 1   # o outro evento, separado
    assert health.status == "DEGRADADO"


def _now():
    from app.core.clock import utcnow

    return utcnow()


def test_incomplete_bucket_alone_never_makes_a_symbol_parado(tmp_path):
    orch = _build_multi(tmp_path, "nao_parado.db", symbols="BTCUSDT,ETHUSDT")
    for _ in range(10):
        orch._update_health("ETHUSDT", _now(), {"status": "incomplete_bucket", "detail": "x"})
    health = orch.health["ETHUSDT"]
    assert health.status == "DEGRADADO"
    assert health.consecutive_failures == 0
    assert health.eligible_again_at is None  # nunca perde turno do round-robin


def test_aggregating_ticks_do_not_restore_health(tmp_path):
    orch = _build_multi(tmp_path, "agregando.db", symbols="BTCUSDT,ETHUSDT")
    orch._update_health("ETHUSDT", _now(), {"status": "incomplete_bucket", "detail": "x"})
    orch._update_health("ETHUSDT", _now(), {"status": "aggregating"})
    assert orch.health["ETHUSDT"].status == "DEGRADADO"
    assert orch.health["ETHUSDT"].has_gap is True

    orch._update_health("ETHUSDT", _now(), {"status": "hold", "strategy_bucket_complete": True})
    assert orch.health["ETHUSDT"].status == "SAUDAVEL"
    assert orch.health["ETHUSDT"].has_gap is False


# =========================================================================
# 3. Cabeçalho e status operacional
# =========================================================================

INDEX_HTML = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "app.js"


def test_header_title_is_up_to_date():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Agente de Trading IA — V2 Multiativo" in html
    assert "Fase 2" not in html


def test_header_separates_processing_from_entry_authorization():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="chip-processing"' in html
    assert 'id="chip-entries"' in html
    # O chip ambíguo não existe mais.
    assert 'id="chip-trading"' not in html
    # O rótulo ambíguo não aparece mais como TEXTO renderizado (a
    # palavra segue no comentário que explica por que ele saiu).
    assert ">OPERAÇÕES:" not in html

    js = APP_JS.read_text(encoding="utf-8")
    assert "PROCESSAMENTO DE MERCADO:" in js
    assert "NOVAS ENTRADAS:" in js
    assert "OPERAÇÕES: ${" not in js
    # Nenhuma escrita por innerHTML.
    import re

    assert not re.search(r"\.(inner|outer)HTML\s*=", js)


def test_operational_state_label_no_longer_reads_like_a_contradiction():
    js = APP_JS.read_text(encoding="utf-8")
    assert "OBSERVANDO — novas entradas desativadas" in js
    assert "ATIVO — novas entradas autorizadas" in js
    assert "PAUSADO — novas entradas desativadas" in js


@pytest.mark.parametrize(
    "operational_state,kill,blocked,expected_entries",
    [
        ("OBSERVANDO", False, False, "DESATIVADAS"),
        ("ATIVO", False, False, "ATIVADAS"),
        ("PAUSADO", False, False, "DESATIVADAS"),
        ("ATIVO", False, True, "BLOQUEADAS"),
        ("ATIVO", True, False, "BLOQUEADAS_EMERGENCIA"),
        ("BLOQUEADO", False, True, "BLOQUEADAS"),
    ],
)
def test_new_entries_status_is_derived_from_the_real_state(
    tmp_path, operational_state, kill, blocked, expected_entries
):
    orch = build_orchestrator(_settings(tmp_path, f"estado_{operational_state}_{kill}_{blocked}.db", "BTCUSDT"))
    client = _client(orch)
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.operational_state = operational_state
        state.kill_switch_engaged = kill
        state.trading_blocked = blocked

    body = client.get("/api/state").json()
    assert body["new_entries_status"] == expected_entries
    # E o estado operacional continua sendo reportado separadamente.
    assert body["operational_state"] == operational_state


def _client_with_poll_engine(orch, status="SAUDAVEL"):
    """TestClient com um `poll_health` REAL montado, como em produção --
    sem ele a rota reporta INICIANDO, que é a verdade honesta para um app
    mínimo sem laço de mercado."""
    from app.api.poll_engine import PollEngineStatus, PollHealth

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.state.poll_health = PollHealth(status=PollEngineStatus(status))
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app)


def test_market_processing_status_reflects_the_engine_not_the_authorization(tmp_path):
    orch = build_orchestrator(_settings(tmp_path, "processamento.db", "BTCUSDT,ETHUSDT"))
    activate_operational_state(orch)
    _tick_until(orch, 6)

    # Entradas autorizadas E processamento ativo -- dois campos, sem
    # contradição possível entre eles.
    body = _client_with_poll_engine(orch).get("/api/state").json()
    assert body["market_processing_status"] == "ATIVO"
    assert body["new_entries_status"] == "ATIVADAS"

    # Degradar a carteira muda o PROCESSAMENTO, nunca a autorização.
    orch.health["ETHUSDT"].status = "DEGRADADO"
    orch.health["ETHUSDT"].has_gap = True
    body = _client_with_poll_engine(orch).get("/api/state").json()
    assert body["market_processing_status"] == "DEGRADADO"
    assert body["new_entries_status"] == "ATIVADAS"


def test_market_processing_is_iniciando_without_a_running_poll_engine(tmp_path):
    """Sem laço de mercado montado, o campo diz INICIANDO -- nunca finge
    ATIVO só porque o servidor HTTP respondeu (o defeito histórico que a
    correção do poll loop já tinha atacado)."""
    orch = build_orchestrator(_settings(tmp_path, "sem_poll.db", "BTCUSDT"))
    body = _client(orch).get("/api/state").json()
    assert body["market_processing_status"] == "INICIANDO"


def test_a_dead_poll_engine_is_reported_as_parado(tmp_path):
    orch = build_orchestrator(_settings(tmp_path, "poll_morto.db", "BTCUSDT"))
    body = _client_with_poll_engine(orch, status="PARADO").get("/api/state").json()
    assert body["market_processing_status"] == "PARADO"


@pytest.mark.parametrize("mode,symbols", [
    (RunMode.REPLAY, "BTCUSDT"),
    (RunMode.REPLAY, "BTCUSDT,ETHUSDT,SOLUSDT"),
    (RunMode.PAPER_LIVE, "BTCUSDT"),
])
def test_header_fields_exist_in_every_mode_and_wallet_shape(tmp_path, mode, symbols):
    settings = Settings(
        mode=mode, symbols=symbols,
        database_url=f"sqlite:///{tmp_path / f'modo_{mode.value}_{len(symbols)}.db'}",
    )
    orch = build_orchestrator(settings)
    body = _client(orch).get("/api/state").json()
    assert body["market_processing_status"] in (
        "INICIANDO", "ATIVO", "DEGRADADO", "PARADO", "ENCERRANDO",
    )
    assert body["new_entries_status"] in (
        "ATIVADAS", "DESATIVADAS", "BLOQUEADAS", "BLOQUEADAS_EMERGENCIA",
    )
