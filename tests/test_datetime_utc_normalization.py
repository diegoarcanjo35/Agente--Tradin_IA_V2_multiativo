"""Correção de Datetimes v1.0 -- reprodução do incidente real de operação:
depois que o backlog alcançou o presente, o poll loop passou a falhar
intermitentemente com `TypeError: can't compare offset-naive and
offset-aware datetimes`. O heartbeat alternava SAUDAVEL/DEGRADADO e a
ativação foi corretamente recusada.

Causa raiz comprovada: `Position.closed_at`/`SystemState.cooldown_until`
(e outras colunas `DateTime(timezone=True)`) voltam do SQLite *naive*
mesmo tendo sido gravadas como UTC-aware -- comportamento conhecido do
driver `sqlite3`. `app/orchestrator.py::_today_start_utc`/`p.closed_at >=
today_start` (cálculo de perda diária) e `RiskEngine.evaluate` (gate de
`cooldown_until`) comparavam esse valor naive diretamente contra um
`datetime` aware (`utcnow()`), levantando o `TypeError` -- mas só quando
o tick chega a essas comparações, ou seja, só em sinais ACIONÁVEIS
(BUY/SELL), nunca em `HOLD`. Isso explica a intermitência observada.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.base import CandleFetchResult, CandleFetchStatus, CandleTick
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Position
from app.persistence.temporal import reattach_utc
from app.strategy.engine import StrategyConfig, StrategyEngine
from tests.factories import activate_operational_state
from tests.test_price_correctness import build_test_orchestrator, make_candle

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _persist_closed_losing_position(session_factory, closed_at: datetime) -> None:
    """Writes a CLOSED, losing position directly (bypassing the strategy/
    risk pipeline, which is irrelevant to this bug) and forces a real
    round trip through SQLite by closing the session afterwards -- the
    naive-on-read behaviour only reproduces once SQLAlchemy actually
    re-fetches the row from the driver instead of reusing its in-memory
    Python object."""
    with session_scope(session_factory) as session:
        position = Position(
            symbol="BTCUSDT", side="BUY", qty=0.001, avg_entry_price=100.0,
            stop_loss=90.0, take_profit=110.0, status="CLOSED",
            realized_pnl=-5.0, fees_paid=0.01,
            opened_at=closed_at - timedelta(minutes=5), closed_at=closed_at,
        )
        session.add(position)


def _sqlite_round_tripped_closed_at(session_factory) -> datetime:
    """Confirms (and documents) the actual raw driver behaviour this bug
    depends on: SQLite always hands back a naive datetime for a
    DateTime(timezone=True) column, never mind what was stored."""
    with session_scope(session_factory) as session:
        row = repo.closed_positions(session)[0]
        return row.closed_at


def _make_actionable_orchestrator(session_factory):
    """A crossover set up so the very next tick produces a BUY signal --
    i.e. reaches the daily-loss/cooldown risk-context comparisons that
    the bug lives in, never a HOLD tick."""
    prices = [100, 99, 98, 97, 96, 500]
    candles = [make_candle(i, p) for i, p in enumerate(prices)]
    orch = build_test_orchestrator(session_factory, candles)
    activate_operational_state(orch)
    return orch, candles


def test_closed_at_round_trips_utc_aware_through_the_orm_boundary(session_factory):
    """Depois da correção: a mesma leitura que antes reproduzia o driver
    devolvendo naive agora chega UTC-aware -- a fronteira (UTCDateTime)
    resolve isso sem que nenhum call-site precise lembrar de nada."""
    utcnow_now = datetime.now(timezone.utc)
    _persist_closed_losing_position(session_factory, utcnow_now)
    round_tripped = _sqlite_round_tripped_closed_at(session_factory)
    assert round_tripped.tzinfo == timezone.utc
    assert round_tripped == utcnow_now  # instante preservado, não só o tzinfo


def test_reproduces_the_incident_closed_position_plus_actionable_signal(session_factory):
    """Antes da correção: uma posição fechada com prejuízo hoje (persistida
    e re-lida do SQLite, portanto naive) somada a um sinal ACIONÁVEL no
    tick seguinte reproduz exatamente o TypeError do incidente real."""
    _persist_closed_losing_position(session_factory, datetime.now(timezone.utc))
    orch, candles = _make_actionable_orchestrator(session_factory)

    for _ in range(len(candles) + 1):
        result = orch.tick()
        if result["status"] == "no_data":
            break
        # must never raise TypeError partway through -- if it does, pytest
        # surfaces it as a test error, which is the reproduction itself.


def test_hold_tick_never_touches_the_buggy_comparison(session_factory):
    """Explica a intermitência: um tick HOLD nunca chega ao cálculo de
    perda diária nem ao gate de cooldown, então nunca falha -- mesmo com
    a mesma posição fechada naive presente."""
    _persist_closed_losing_position(session_factory, datetime.now(timezone.utc))
    settings_candles = [make_candle(i, 100.0) for i in range(3)]  # flat -> HOLD
    orch = build_test_orchestrator(session_factory, settings_candles)
    activate_operational_state(orch)

    for _ in range(len(settings_candles) + 1):
        result = orch.tick()
        if result["status"] == "no_data":
            break
        assert result["status"] in ("hold", "no_data")


# --- app.persistence.temporal.reattach_utc: unidade, isolada -------------

def test_reattach_utc_none_stays_none():
    assert reattach_utc(None) is None


def test_reattach_utc_attaches_utc_to_naive_without_changing_wall_clock():
    naive = datetime(2026, 8, 31, 3, 6, 0)
    result = reattach_utc(naive)
    assert result.tzinfo is timezone.utc
    assert (result.year, result.month, result.day, result.hour, result.minute, result.second) == (
        2026, 8, 31, 3, 6, 0,
    )


def test_reattach_utc_is_idempotent():
    naive = datetime(2026, 8, 31, 3, 6, 0)
    once = reattach_utc(naive)
    twice = reattach_utc(once)
    assert once == twice
    assert once.tzinfo == twice.tzinfo == timezone.utc


def test_reattach_utc_already_utc_aware_is_unchanged():
    aware = datetime(2026, 8, 31, 3, 6, 0, tzinfo=timezone.utc)
    assert reattach_utc(aware) == aware
    assert reattach_utc(aware).tzinfo == timezone.utc


def test_reattach_utc_converts_non_utc_offset_to_the_correct_instant():
    """Correção obrigatória item 5: um valor aware com offset -03:00 deve
    virar o MESMO instante em UTC (não apenas trocar o tzinfo, o que
    mudaria o significado temporal em 3 horas)."""
    minus_three = datetime(2026, 8, 31, 0, 6, 0, tzinfo=timezone(timedelta(hours=-3)))
    result = reattach_utc(minus_three)
    assert result.tzinfo == timezone.utc
    assert result == datetime(2026, 8, 31, 3, 6, 0, tzinfo=timezone.utc)
    assert result.hour == 3  # never just relabeled to hour == 0


# --- Regressão dos pontos auditados: perda diária, cooldown, ordenação de
#     múltiplas posições ao redor da meia-noite UTC -----------------------

def test_only_todays_losses_count_positions_before_and_after_utc_midnight(session_factory):
    midnight = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
    _persist_closed_losing_position(session_factory, midnight - timedelta(minutes=1))  # ontem
    _persist_closed_losing_position(session_factory, midnight + timedelta(minutes=1))  # hoje

    from app.orchestrator import _today_start_utc

    with session_scope(session_factory) as session:
        positions = repo.closed_positions(session)
        today_start = _today_start_utc(midnight + timedelta(hours=1))
        daily_loss = sum(
            -p.realized_pnl for p in positions
            if p.realized_pnl < 0 and p.closed_at and p.closed_at >= today_start
        )
    assert daily_loss == 5.0  # só a posição de "hoje", nunca a de "ontem"


def test_cooldown_until_naive_future_blocks_new_entries(session_factory):
    from app.persistence.models import SystemState

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)

    with session_scope(session_factory) as session:
        state = session.execute(
            __import__("sqlalchemy").select(SystemState)
        ).scalars().one()
        assert state.cooldown_until.tzinfo == timezone.utc  # já normalizado na leitura

    orch, candles = _make_actionable_orchestrator(session_factory)
    results = []
    for _ in range(len(candles) + 1):
        r = orch.tick()
        results.append(r)
        if r["status"] == "no_data":
            break
    assert any(r["status"] == "rejected" for r in results), results


def test_cooldown_until_naive_past_allows_new_entries(session_factory):
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=30)

    orch, candles = _make_actionable_orchestrator(session_factory)
    results = []
    for _ in range(len(candles) + 1):
        r = orch.tick()
        results.append(r)
        if r["status"] == "no_data":
            break
    assert any(r["status"] == "order_filled" for r in results), results


def test_several_ticks_near_the_present_do_not_flap_the_heartbeat(session_factory):
    """Reprodução direta do sintoma observado em produção: vários ticks
    seguidos, próximos ao presente, com uma posição fechada naive já
    persistida -- nenhum deve levantar exceção nem produzir um incidente
    de poll loop."""
    _persist_closed_losing_position(session_factory, datetime.now(timezone.utc))
    prices = [100, 100.5, 99.8, 100.2, 100.1, 100.6, 99.9, 100.3]
    candles = [make_candle(i, p) for i, p in enumerate(prices)]
    orch = build_test_orchestrator(session_factory, candles)
    activate_operational_state(orch)

    for _ in range(len(candles) + 1):
        result = orch.tick()
        if result["status"] == "no_data":
            break
        assert result["status"] != "error"
