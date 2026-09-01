"""Fase 3.2 (decisão Q4 do PO): representação CANÔNICA única do timeframe.

Antes desta fase a mesma grandeza era gravada com duas strings: BYBIT_DEMO
persistia `"1"` (o formato de intervalo da corretora) e REPLAY persistia
`"1m"`, enquanto o painel consultava sempre `"1m"` -- e por isso
`/api/chart-data` devolvia lista vazia em BYBIT_DEMO. Defeito real,
invisível em REPLAY porque lá as duas pontas coincidiam.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.core.timeframe import (
    CANONICAL_OPERATIONAL_TIMEFRAME,
    bybit_interval,
    canonical_timeframe,
    minutes_to_canonical,
    timeframe_aliases,
    timeframe_minutes,
)
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Candle
from tests.factories import activate_operational_state
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import (
    _generate_kline_rows,
    _KlineSequenceTransport,
    make_bybit_demo_settings,
)


# --- a função canônica ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1", "1m"), ("1m", "1m"), ("1M", "1m"), (1, "1m"), (" 1m ", "1m"),
    ("5", "5m"), ("5m", "5m"), (5, "5m"),
    ("15", "15m"), ("15m", "15m"), (15, "15m"),
])
def test_every_accepted_alias_normalizes_to_the_canonical_form(raw, expected):
    assert canonical_timeframe(raw) == expected


@pytest.mark.parametrize("raw", ["", "3", "1h", "60", None, "abc"])
def test_unknown_timeframe_raises_instead_of_being_guessed(raw):
    """Nunca tratar silenciosamente um valor desconhecido como 1 minuto --
    seria decidir sobre a série errada sem nenhum aviso."""
    with pytest.raises(ValueError):
        canonical_timeframe(raw)


def test_aliases_always_list_the_canonical_form_first():
    assert timeframe_aliases("1") == ("1m", "1")
    assert timeframe_aliases("5m") == ("5m", "5")
    assert timeframe_minutes("1") == 1 and timeframe_minutes("15m") == 15
    assert minutes_to_canonical(5) == "5m"
    with pytest.raises(ValueError):
        minutes_to_canonical(7)


def test_bybit_interval_keeps_the_exchange_format_at_the_http_boundary_only():
    assert bybit_interval(CANONICAL_OPERATIONAL_TIMEFRAME) == "1"
    assert bybit_interval("5m") == "5"


# --- escrita sempre canônica ---------------------------------------------

def test_new_candles_are_always_written_canonically(db_session):
    open_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    row = repo.save_candle(db_session, "BTCUSDT", "1", open_time, 1, 2, 0, 1, 1, "bybit_demo")
    assert row.timeframe == "1m"


# --- leitura encontra o legado -------------------------------------------

def _insert_legacy(session, symbol, open_time, close, timeframe="1"):
    """Escreve DIRETAMENTE na tabela com a grafia legada, simulando um banco
    anterior à canonicalização (`repo.save_candle` já não permite isso)."""
    row = Candle(
        symbol=symbol, timeframe=timeframe, open_time=open_time,
        open=close, high=close, low=close, close=close, volume=1.0, source="bybit_demo",
    )
    session.add(row)
    session.flush()
    return row


def test_queries_still_find_legacy_candles_written_as_1(db_session):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        _insert_legacy(db_session, "BTCUSDT", base + timedelta(minutes=i), 100 + i)

    found = repo.recent_candles(db_session, "BTCUSDT", "1m", limit=10)
    assert [c.close for c in found] == [100, 101, 102]  # cronológico
    assert repo.get_last_candle_open_time(db_session, "BTCUSDT", "1m") == base + timedelta(minutes=2)


def test_legacy_and_canonical_rows_for_the_same_instant_are_deduplicated(db_session):
    """Convivência: se existirem "1" e "1m" para o mesmo símbolo/open_time,
    apenas UM candle é emitido -- e o preferido é o CANÔNICO. O banco
    legado nunca é reescrito."""
    open_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _insert_legacy(db_session, "BTCUSDT", open_time, close=100.0, timeframe="1")
    _insert_legacy(db_session, "BTCUSDT", open_time, close=200.0, timeframe="1m")

    found = repo.recent_candles(db_session, "BTCUSDT", "1m", limit=10)
    assert len(found) == 1
    assert found[0].timeframe == "1m"
    assert found[0].close == 200.0  # o registro canônico prevalece

    # As duas linhas continuam no banco -- nada foi apagado nem reescrito.
    from sqlalchemy import select
    rows = db_session.execute(
        select(Candle).where(Candle.symbol == "BTCUSDT", Candle.open_time == open_time)
    ).scalars().all()
    assert len(rows) == 2


def test_dedup_prefers_canonical_regardless_of_insertion_order(db_session):
    open_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _insert_legacy(db_session, "BTCUSDT", open_time, close=200.0, timeframe="1m")
    _insert_legacy(db_session, "BTCUSDT", open_time, close=100.0, timeframe="1")
    found = repo.recent_candles(db_session, "BTCUSDT", "1m", limit=10)
    assert len(found) == 1 and found[0].close == 200.0


def test_limit_is_respected_after_logical_deduplication(db_session):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(10):
        _insert_legacy(db_session, "BTCUSDT", base + timedelta(minutes=i), 100 + i)
    found = repo.recent_candles(db_session, "BTCUSDT", "1m", limit=3)
    assert [c.close for c in found] == [107, 108, 109]


# --- BYBIT_DEMO: o defeito que a canonicalização corrige -----------------

def test_bybit_demo_persists_canonically_and_the_chart_finds_the_candles(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'bybit_tf.db'}")
    rows = _generate_kline_rows(n_down=6, n_up=0)
    orch = build_orchestrator(
        settings, bybit_transport=_KlineSequenceTransport(FakeBybitTransport(), rows)
    )
    activate_operational_state(orch)
    for _ in range(len(rows) + 2):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        from sqlalchemy import select
        persisted = session.execute(select(Candle)).scalars().all()
    assert persisted, "o modo BYBIT_DEMO precisa ter persistido candles"
    assert {c.timeframe for c in persisted} == {"1m"}  # nunca mais "1"

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    body = TestClient(app).get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()

    assert body["timeframe"] == "1m"
    assert body["market_data_timeframe"] == "1m"
    # O defeito corrigido: antes desta fase esta lista vinha VAZIA em
    # BYBIT_DEMO, porque a rota consultava "1m" e o provider gravava "1".
    assert len(body["candles"]) == len(persisted)


def test_replay_persists_canonically_too(tmp_path):
    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / 'replay_tf.db'}",
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    for _ in range(10):
        orch.tick()
    with session_scope(orch.session_factory) as session:
        from sqlalchemy import select
        persisted = session.execute(select(Candle)).scalars().all()
    assert {c.timeframe for c in persisted} == {"1m"}


def test_the_bybit_provider_still_receives_the_exchange_interval_format(tmp_path):
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'interval.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    # O formato "1" da corretora continua confinado à fronteira HTTP.
    assert orch.market_data_provider.timeframe == "1"


def test_session_row_records_the_canonical_strategy_timeframe(tmp_path):
    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT", strategy_timeframe_minutes=15,
        database_url=f"sqlite:///{tmp_path / 'session_tf.db'}",
    )
    orch = build_orchestrator(settings)
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        op = repo.get_active_session(session, state)
        assert op.timeframe == "15m"
