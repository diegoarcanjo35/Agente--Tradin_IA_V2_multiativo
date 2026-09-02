"""Fase 3.3.1 -- GATE TEMPORAL DE ATIVAÇÃO (bloco B da matriz do PO).

Antes desta fase, a ativação olhava heartbeat do motor, bloqueio de
operações e reconciliação inicial. Tudo isso reportava SAUDÁVEL enquanto
a carteira decidia sobre candles de horas atrás -- foi exatamente o que a
V2 fez em 01/09, com 185 minutos de defasagem e três símbolos verdes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control, routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import (
    _generate_kline_rows,
    _KlineSequenceTransport,
    make_bybit_demo_settings,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _client(orch, poll_status="SAUDAVEL"):
    from app.api.poll_engine import PollEngineStatus, PollHealth

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.state.poll_health = PollHealth(status=PollEngineStatus(poll_status))
    app.include_router(routes_dashboard.router, prefix="/api")
    app.include_router(routes_control.router, prefix="/api")
    return TestClient(app)


def _paper_live(tmp_path, db, symbols=SYMBOLS, **over):
    return Settings(
        mode=RunMode.PAPER_LIVE, symbols=",".join(symbols),
        database_url=f"sqlite:///{tmp_path / db}", strategy_timeframe_minutes=5, **over,
    )


def _seed(orch, symbols, minutos_atras: int, quantos: int = 130, preco=100.0):
    """Persiste candles de 1 minuto terminando `minutos_atras` do agora --
    é o que define a ATUALIDADE TEMPORAL da série."""
    agora = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    fim = agora - timedelta(minutes=minutos_atras)
    with session_scope(orch.session_factory) as session:
        for symbol in symbols:
            for i in range(quantos):
                t = fim - timedelta(minutes=quantos - 1 - i)
                p = preco + (i % 7) * 0.5
                repo.save_candle(session, symbol, "1m", t, p, p + 1, p - 1, p, 10.0, "paper_live")


def _hydrate(orch):
    """Coloca a carteira no estado de um sistema QUE JÁ ESTAVA RODANDO:

    - reexecuta a hidratação silenciosa para que o aquecimento reflita os
      candles recém-semeados (o boot rodou com o banco vazio);
    - marca cada símbolo como tendo completado um tick saudável, pelo
      MESMO caminho que o agendador usa (`_update_health`), nunca
      atribuindo `status` na mão.

    Sem isso a saúde fica em INICIANDO e o gate bloqueia -- corretamente:
    não se autoriza entrada antes de o motor ter processado cada símbolo
    ao menos uma vez."""
    from app.core.clock import utcnow

    for sub in getattr(orch, "orchestrators", {"x": orch}).values():
        sub._strategy_state_hydrated = False
    with session_scope(orch.session_factory) as session:
        orch.hydrate_strategy_state(session)
    if hasattr(orch, "health"):
        for symbol in orch.health:
            orch._update_health(symbol, utcnow(), {"status": "hold", "strategy_bucket_complete": True})


# --- caminho feliz -------------------------------------------------------

def test_all_symbols_current_allows_activation(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "ok.db"), bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=1)
    _hydrate(orch)
    body = _client(orch).post("/api/operational-state/activate").json()
    assert body["operational_state"] == "ATIVO"
    assert "ativadas" in body["mensagem"].lower()


# --- um único símbolo atrasado derruba a carteira inteira ---------------

def test_one_stale_symbol_blocks_the_entire_wallet(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "um_atrasado.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, ["BTCUSDT", "SOLUSDT"], minutos_atras=1)
    _seed(orch, ["ETHUSDT"], minutos_atras=180)          # só o ETH atrasado
    _hydrate(orch)

    body = _client(orch).post("/api/operational-state/activate").json()
    assert body["operational_state"] == "OBSERVANDO"     # ATÔMICO: ninguém ativa
    assert body["blocking_symbols"] == ["ETHUSDT"]
    assert "ETHUSDT" in body["mensagem"]
    assert "não está no presente" in body["mensagem"]
    assert "atômica" in body["mensagem"]

    with session_scope(orch.session_factory) as session:
        assert repo.get_or_create_system_state(session).operational_state == "OBSERVANDO"


def test_the_message_identifies_the_symbol_delay_and_limit(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "mensagem.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, ["BTCUSDT", "ETHUSDT"], minutos_atras=1)
    _seed(orch, ["SOLUSDT"], minutos_atras=120)
    _hydrate(orch)

    body = _client(orch).post("/api/operational-state/activate").json()
    msg = body["mensagem"]
    assert "SOLUSDT" in msg
    assert "atraso" in msg and "limite" in msg
    assert "último candle" in msg
    temporal = body["temporal_readiness"]["per_symbol"]["SOLUSDT"]
    assert temporal["current"] is False
    assert temporal["failure"] == "market_data_stale"
    assert temporal["delay_seconds"] > 300


# --- gap, aquecimento, falhas -------------------------------------------

def test_a_gap_blocks_activation(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "gap.db"), bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=1)
    _hydrate(orch)
    orch.health["ETHUSDT"].has_gap = True
    orch.health["ETHUSDT"].status = "DEGRADADO"

    body = _client(orch).post("/api/operational-state/activate").json()
    assert body["operational_state"] == "OBSERVANDO"
    assert "ETHUSDT" in body["blocking_symbols"]
    assert "lacuna de mercado" in body["mensagem"]


def test_incomplete_warmup_blocks_activation(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "aquecimento.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=1, quantos=20)   # < 22 buckets
    _hydrate(orch)

    body = _client(orch).post("/api/operational-state/activate").json()
    assert body["operational_state"] == "OBSERVANDO"
    assert set(body["blocking_symbols"]) == set(SYMBOLS)
    assert "aquecimento incompleto" in body["mensagem"]


def test_consecutive_failures_block_activation(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "falhas.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=1)
    _hydrate(orch)
    orch.health["BTCUSDT"].consecutive_failures = 2
    orch.health["BTCUSDT"].last_error = "erro sintético de teste"
    orch.health["BTCUSDT"].status = "DEGRADADO"

    body = _client(orch).post("/api/operational-state/activate").json()
    assert "BTCUSDT" in body["blocking_symbols"]
    assert "falha(s) consecutiva(s)" in body["mensagem"]


# --- o ponto central: recepção recente NÃO é dado atual -----------------

def test_recent_reception_with_old_candles_still_blocks(tmp_path):
    """O defeito exato da Fase 3.3: `data_reception_recent` verde, motor
    SAUDÁVEL, heartbeat vivo -- e a série com horas de atraso."""
    orch = build_orchestrator(_paper_live(tmp_path, "recepcao.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=185)
    _hydrate(orch)

    # Provider "recebendo agora": recepção recente é verdadeira.
    for sub in orch.orchestrators.values():
        sub.market_data_provider._last_received_at = datetime.now(timezone.utc)
        assert sub.market_data_provider.is_stale(30) is False

    client = _client(orch, poll_status="SAUDAVEL")   # motor saudável
    body = client.post("/api/operational-state/activate").json()
    assert body["operational_state"] == "OBSERVANDO"
    assert set(body["blocking_symbols"]) == set(SYMBOLS)


def test_current_candles_with_recent_reception_allow_activation(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "atual.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=2)
    _hydrate(orch)
    for sub in orch.orchestrators.values():
        sub.market_data_provider._last_received_at = datetime.now(timezone.utc)

    body = _client(orch).post("/api/operational-state/activate").json()
    assert body["operational_state"] == "ATIVO"


# --- reboot nunca restaura ATIVO ----------------------------------------

def test_reboot_never_restores_active_automatically(tmp_path):
    db = "reboot.db"
    orch = build_orchestrator(_paper_live(tmp_path, db), bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=1)
    _hydrate(orch)
    assert _client(orch).post("/api/operational-state/activate").json()["operational_state"] == "ATIVO"

    # "Reinício": novo processo, MESMO banco.
    orch2 = build_orchestrator(_paper_live(tmp_path, db), bybit_transport=FakeBybitTransport())
    with session_scope(orch2.session_factory) as session:
        assert repo.get_or_create_system_state(session).operational_state == "OBSERVANDO"


# --- estado exposto em /api/state ---------------------------------------

def test_state_exposes_temporal_readiness(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "estado.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, ["BTCUSDT", "ETHUSDT"], minutos_atras=1)
    _seed(orch, ["SOLUSDT"], minutos_atras=90)
    body = _client(orch).get("/api/state").json()

    tr = body["temporal_readiness"]
    assert tr["applicable"] is True
    assert tr["ready"] is False
    assert tr["blocking_symbols"] == ["SOLUSDT"]
    assert body["max_signal_delay_after_close_seconds"] == 300.0
    assert tr["per_symbol"]["BTCUSDT"]["current"] is True


# --- modos replayados ----------------------------------------------------

def test_replayed_modes_do_not_apply_the_temporal_gate(tmp_path):
    """A fixture de REPLAY é de 2024-01-01: medi-la contra o relógio de
    parede mediria a idade do arquivo, não risco."""
    settings = Settings(
        mode=RunMode.REPLAY, symbols=",".join(SYMBOLS),
        database_url=f"sqlite:///{tmp_path / 'replay.db'}", strategy_timeframe_minutes=5,
    )
    orch = build_orchestrator(settings)
    for _ in range(400):
        if orch.tick().get("status") == "no_data":
            break
    body = _client(orch).get("/api/state").json()
    assert body["temporal_readiness"]["applicable"] is False
    assert body["temporal_readiness"]["ready"] is True
    # E a ativação continua possível em REPLAY.
    assert _client(orch).post("/api/operational-state/activate").json()["operational_state"] == "ATIVO"


# --- o LIMITE: exatamente MAX_SIGNAL_DELAY_AFTER_CLOSE_SECONDS ----------
# Decisão do PO (Fase 3.3.1): o gate usa os mesmos 300 s do frescor do
# sinal, sem somar de novo a duração do bucket estratégico -- a idade já é
# contada a partir do FECHAMENTO do candle. Os três casos de fronteira são
# medidos na função pura, com `now` explícito: pelo endpoint o relógio
# real avança durante a própria chamada e 299,99 s viraria 300,05 s.

LIMITE = 300.0


def _atualidade(delay_segundos: float, limite: float = LIMITE):
    from app.core.freshness import market_data_temporally_current

    agora = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    fechamento = agora - timedelta(seconds=delay_segundos)
    return market_data_temporally_current(
        last_candle_open_time=fechamento - timedelta(minutes=1),   # candle de 1 min
        market_data_timeframe_minutes=1,
        now=agora,
        max_delay_seconds=limite,
    )


def test_delay_just_below_the_limit_is_current():
    r = _atualidade(299.99)
    assert r["current"] is True
    assert r["delay_seconds"] == pytest.approx(299.99)
    assert "failure" not in r


def test_delay_exactly_at_the_limit_is_current():
    """Limite INCLUSIVO: 300 s exatos ainda permite."""
    r = _atualidade(300.0)
    assert r["current"] is True
    assert r["delay_seconds"] == pytest.approx(300.0)


def test_delay_just_above_the_limit_blocks():
    r = _atualidade(300.01)
    assert r["current"] is False
    assert r["failure"] == "market_data_stale"
    assert r["delay_seconds"] == pytest.approx(300.01)


def test_the_limit_is_the_signal_freshness_window_without_adding_the_bucket(tmp_path):
    """Prova que o endpoint usa 300 s, e não o antigo derivado de 600 s:
    um atraso entre 7 e 8 minutos passaria sob 600 s e é BLOQUEADO."""
    orch = build_orchestrator(_paper_live(tmp_path, "limite.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=8)     # atraso medido entre 420 s e 480 s
    _hydrate(orch)

    body = _client(orch).post("/api/operational-state/activate").json()
    assert body["operational_state"] == "OBSERVANDO"
    assert set(body["blocking_symbols"]) == set(SYMBOLS)

    medido = body["temporal_readiness"]["per_symbol"]["BTCUSDT"]
    assert 420.0 <= medido["delay_seconds"] <= 480.0    # passaria sob 600 s
    assert medido["max_delay_seconds"] == LIMITE        # ... mas o limite é 300 s
    assert medido["failure"] == "market_data_stale"


def test_a_delay_comfortably_inside_the_window_still_activates(tmp_path):
    orch = build_orchestrator(_paper_live(tmp_path, "dentro.db"),
                               bybit_transport=FakeBybitTransport())
    _seed(orch, SYMBOLS, minutos_atras=3)     # atraso entre 120 s e 180 s
    _hydrate(orch)

    client = _client(orch)
    assert client.post("/api/operational-state/activate").json()["operational_state"] == "ATIVO"
    # E o limite publicado em /api/state é o mesmo que o gate aplicou.
    estado = client.get("/api/state").json()
    assert estado["max_signal_delay_after_close_seconds"] == LIMITE
    assert estado["temporal_readiness"]["per_symbol"]["BTCUSDT"]["max_delay_seconds"] == LIMITE
