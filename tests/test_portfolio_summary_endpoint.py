"""Fase 3.1.1 (decisão final do PO sobre escopos e saldo inicial):
`GET /api/portfolio-summary` -- `portfolio` (equity, SEMPRE lifetime,
idêntica em qualquer `?scope=`) e `period_performance` (recorte de
DESEMPENHO pelo `scope` pedido, nunca chamado de equity, nunca soma
`starting_balance` de novo). Patrimônio calculado sob demanda, nunca
persistido; saldo inicial CONGELADO no snapshot da sessão ativa.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.core.errors import StartingBalanceResetBlockedError
from app.execution import fill_service
from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order
from app.sessions import start_or_resume_session
from tests.factories import activate_operational_state


def _make_order(session, symbol: str, side: str, idempotency_key: str, is_close: bool = False, qty: float = 0.01) -> Order:
    """Mesmo padrão de tests/test_late_opposite_fill.py -- cria a cadeia
    real signal -> risk_evaluation -> order exigida pelo schema, para que
    fills aplicados via fill_service produzam linhas `Execution` reais
    (a fonte canônica de taxas -- ver repo.execution_fees)."""
    signal = repo.save_signal(session, symbol, side, "teste", 100.0, 1.0, {})
    risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
    order = repo.save_order(
        session, idempotency_key=idempotency_key, risk_evaluation_id=risk_eval.id,
        symbol=symbol, side=side, qty=qty, stop_loss=90.0, take_profit=110.0, mode="REPLAY",
        is_close=is_close,
    )
    repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
    order.exchange_order_id = f"EX-{idempotency_key}"
    return order


def _apply_fill(session, order, exchange_fill_id, qty, price, fee, is_close=False):
    state = repo.get_or_create_system_state(session)
    snapshot = OrderStatusSnapshot(
        exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
        fills=[FillEvent(exchange_fill_id, qty, price, fee)],
    )
    return fill_service.apply_order_snapshot(
        session, state, None, order, snapshot, is_close=is_close, max_api_failures=5,
    )


def _extend_base_into_the_past(orch, old_started_at, new_strategy_version="v2-same-base"):
    """Fase 3.1.1 (último gate contábil): simula uma BASE CONTÁBIL que
    começou há tempos (sessão A) e continuou através de uma mudança de
    configuração puramente OPERACIONAL (ex.: versão de estratégia --
    nunca `paper_starting_balance_usd`) que abriu uma sessão B nova
    "agora", com o MESMO saldo congelado -- portanto a MESMA base. Depois
    disso: `base_started_at` = início de A (antigo); `session_started_at`
    = início de B (recente) -- exatamente o cenário que distingue
    `portfolio` (sempre a base inteira) de `period_performance` com
    scope=session/daily (janela mais estreita, dentro da mesma base)."""
    import uuid as uuid_module

    from app.persistence.models import OperationalSession

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        session_a = repo.get_active_session(session, state)
        session_a.started_at = old_started_at
        session_a.ended_at = old_started_at + timedelta(minutes=1)
        session_a.status = "ENCERRANDO"

        session_b = OperationalSession(
            session_uid=str(uuid_module.uuid4()),
            mode=session_a.mode, symbol=session_a.symbol, symbols=session_a.symbols,
            timeframe=session_a.timeframe, strategy_version=new_strategy_version,
            risk_config_json=session_a.risk_config_json,
            config_snapshot_json=session_a.config_snapshot_json,  # mesmo saldo -- mesma base
            config_fingerprint=f"fingerprint-{new_strategy_version}",
            status="ATIVO",
        )
        session.add(session_b)
        session.flush()
        state.active_session_id = session_b.id


def _make_client(tmp_path, name, symbols, starting_balance=1000.0):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols, database_url=f"sqlite:///{tmp_path / name}",
        paper_starting_balance_usd=starting_balance,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app), orch


# --- portfolio (equity): sempre lifetime, nunca varia com scope ---------

def test_equity_with_no_positions_equals_configured_starting_balance(tmp_path):
    client, orch = _make_client(tmp_path, "eq_no_positions.db", "BTCUSDT", starting_balance=1000.0)
    body = client.get("/api/portfolio-summary").json()
    portfolio = body["portfolio"]
    assert portfolio["starting_balance"] == 1000.0
    assert portfolio["equity"] == 1000.0
    assert portfolio["equity_complete"] is True
    assert portfolio["open_positions_count"] == 0


def test_custom_starting_balance_is_used_never_the_old_hardcode(tmp_path):
    client, orch = _make_client(tmp_path, "eq_custom_balance.db", "BTCUSDT", starting_balance=7777.0)
    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["starting_balance"] == 7777.0
    assert body["equity"] == 7777.0


def test_equity_with_open_position_includes_unrealized_pnl(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_open_position.db", "BTCUSDT")
    for _ in range(3):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="OPEN", fees_paid=0.0,
        ))

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["open_positions_count"] == 1
    assert isinstance(body["unrealized_pnl"], (int, float))
    # mark price real (do fixture REPLAY) != avg_entry_price=100 -- unrealized != 0.
    assert body["equity"] != body["starting_balance"]


def test_profitable_closed_trade_increases_equity(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_profitable_trade.db", "BTCUSDT", starting_balance=1000.0)
    now = datetime.now(timezone.utc)
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED", realized_pnl=25.0,
            fees_paid=0.0, opened_at=now, closed_at=now,
        ))

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["equity"] == 1025.0


def test_losing_closed_trade_decreases_equity(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_losing_trade.db", "BTCUSDT", starting_balance=1000.0)
    now = datetime.now(timezone.utc)
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED", realized_pnl=-15.0,
            fees_paid=0.0, opened_at=now, closed_at=now,
        ))

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["equity"] == 985.0


def test_current_equity_identical_across_all_three_scopes(tmp_path):
    """Decisão definitiva do PO: equity NÃO TEM escopo -- consultar com
    scope=lifetime, session ou daily deve retornar exatamente o mesmo
    `portfolio.equity`, mesmo havendo histórico realizado fora da janela
    de session/daily -- CONTANTO que esteja dentro da mesma base contábil
    (nunca de uma base anterior a um reset, ver os testes de reset
    abaixo)."""
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_scope_identical.db", "BTCUSDT", starting_balance=1000.0)
    old_time = datetime.now(timezone.utc) - timedelta(days=30)
    _extend_base_into_the_past(orch, old_time - timedelta(hours=1))
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED", realized_pnl=999.0,
            fees_paid=0.0, opened_at=old_time, closed_at=old_time,
        ))

    lifetime = client.get("/api/portfolio-summary?scope=lifetime").json()["portfolio"]
    session_scope_body = client.get("/api/portfolio-summary?scope=session").json()["portfolio"]
    daily = client.get("/api/portfolio-summary?scope=daily").json()["portfolio"]

    assert lifetime == session_scope_body == daily
    assert lifetime["equity"] == 1999.0  # 1000 + 999, mesmo com o trade fora da janela de session/daily


# --- taxas: fonte canônica, sem dupla contagem ---------------------------

def test_entry_fee_of_open_position_reduces_equity(tmp_path):
    """Regra central da decisão do PO: a taxa de entrada de uma posição
    AINDA ABERTA já reduz o patrimônio, sem esperar o fechamento. Fill
    aplicado via fill_service (caminho real) -- gera a linha `Execution`
    real que é a fonte canônica de taxas."""
    client, orch = _make_client(tmp_path, "eq_open_fee.db", "BTCUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        order = _make_order(session, "BTCUSDT", "BUY", "open-fee-1")
        _apply_fill(session, order, "EXEC-open-fee-1", 0.01, 100.0, 0.5, is_close=False)

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["fees_paid"] == pytest.approx(0.5)
    assert body["equity"] == pytest.approx(1000.0 - 0.5 + body["unrealized_pnl"])


def test_entry_fee_before_period_and_exit_fee_now_both_in_equity(tmp_path):
    """Item 7 da decisão do PO: taxa de entrada ANTERIOR (ontem) e taxa de
    saída ATUAL (hoje) entram AMBAS na equity lifetime -- mas apenas a de
    saída aparece em `period_performance` com scope=daily."""
    client, orch = _make_client(tmp_path, "eq_entry_before_exit_now.db", "BTCUSDT", starting_balance=1000.0)
    _extend_base_into_the_past(orch, datetime.now(timezone.utc) - timedelta(days=2))
    with session_scope(orch.session_factory) as session:
        from sqlalchemy import select

        from app.persistence.models import Execution

        open_order = _make_order(session, "BTCUSDT", "BUY", "entry-before-1")
        _apply_fill(session, open_order, "EXEC-entry-before-1", 0.01, 100.0, 0.3, is_close=False)
        exec_row = session.execute(
            select(Execution).where(Execution.order_id == open_order.id)
        ).scalar_one()
        exec_row.executed_at = datetime.now(timezone.utc) - timedelta(days=1, hours=1)

        close_order = _make_order(session, "BTCUSDT", "SELL", "exit-now-1", is_close=True)
        _apply_fill(session, close_order, "EXEC-exit-now-1", 0.01, 110.0, 0.2, is_close=True)

    body = client.get("/api/portfolio-summary?scope=daily").json()
    portfolio, period = body["portfolio"], body["period_performance"]
    # Equity lifetime: as DUAS taxas (0.3 + 0.2 = 0.5) entram.
    assert portfolio["fees_paid"] == pytest.approx(0.5)
    assert portfolio["equity"] == pytest.approx(1000.0 + 0.10 - 0.5)
    # period_performance (scope=daily): só a taxa de SAÍDA (fill de hoje).
    assert period["fees_paid"] == pytest.approx(0.2)


def test_exit_fee_is_not_double_counted(tmp_path):
    """Fee de entrada (0.3) + fee de saída (0.2) -- cada uma aparece
    exatamente uma vez em `fees_paid` (0.5 total), nunca duplicada por
    somar Position.fees_paid junto com Order.fees_total/Execution."""
    client, orch = _make_client(tmp_path, "eq_exit_fee.db", "BTCUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        open_order = _make_order(session, "BTCUSDT", "BUY", "exit-fee-open")
        _apply_fill(session, open_order, "EXEC-exit-fee-open", 0.01, 100.0, 0.3, is_close=False)
        close_order = _make_order(session, "BTCUSDT", "SELL", "exit-fee-close", is_close=True)
        _apply_fill(session, close_order, "EXEC-exit-fee-close", 0.01, 110.0, 0.2, is_close=True)

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["fees_paid"] == pytest.approx(0.5)
    assert body["realized_price_pnl"] == pytest.approx(0.10)
    assert body["equity"] == pytest.approx(1000.0 + 0.10 - 0.5)


def test_partial_close_fee_is_not_double_counted(tmp_path):
    """Fechamento parcial (metade da posição) -- a taxa do fill parcial
    entra em `fees_paid` uma única vez, a posição continua aberta com o
    restante da quantidade."""
    client, orch = _make_client(tmp_path, "eq_partial_close_fee.db", "BTCUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        open_order = _make_order(session, "BTCUSDT", "BUY", "partial-open", qty=0.02)
        _apply_fill(session, open_order, "EXEC-partial-open", 0.02, 100.0, 0.4, is_close=False)
        close_order = _make_order(session, "BTCUSDT", "SELL", "partial-close", is_close=True, qty=0.01)
        _apply_fill(session, close_order, "EXEC-partial-close", 0.01, 110.0, 0.1, is_close=True)

    with session_scope(orch.session_factory) as session:
        open_positions = repo.open_positions(session, "BTCUSDT")
        assert len(open_positions) == 1
        assert open_positions[0].qty == pytest.approx(0.01)

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["fees_paid"] == pytest.approx(0.5)
    assert body["realized_price_pnl"] == pytest.approx((110.0 - 100.0) * 0.01)


def test_open_position_with_prior_partial_close_never_leaks_into_a_new_day(tmp_path):
    """Item 7 da decisão do PO: uma posição com fechamento PARCIAL
    anterior, mas ainda ABERTA, não pode ter seu P&L histórico realizado
    "vazado" para dentro de um `period_performance` de um dia novo -- a
    posição nunca aparece em `closed_positions` (ainda está OPEN), então
    seu `realized_pnl` acumulado simplesmente não é contado em nenhum
    escopo diário -- só na equity lifetime (sempre, via posições
    abertas)."""
    client, orch = _make_client(tmp_path, "eq_open_partial_no_leak.db", "BTCUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        open_order = _make_order(session, "BTCUSDT", "BUY", "leak-open", qty=0.02)
        _apply_fill(session, open_order, "EXEC-leak-open", 0.02, 100.0, 0.0, is_close=False)
        close_order = _make_order(session, "BTCUSDT", "SELL", "leak-partial-close", is_close=True, qty=0.01)
        _apply_fill(session, close_order, "EXEC-leak-partial-close", 0.01, 110.0, 0.0, is_close=True)

        from sqlalchemy import select

        from app.persistence.models import Execution

        # A taxa/realização do fechamento parcial aconteceu "ontem".
        for exec_row in session.execute(select(Execution)).scalars().all():
            exec_row.executed_at = datetime.now(timezone.utc) - timedelta(days=1, hours=1)

    body = client.get("/api/portfolio-summary?scope=daily").json()
    portfolio, period = body["portfolio"], body["period_performance"]
    # Equity lifetime SEMPRE reflete o P&L parcial já realizado (posição
    # aberta nunca é filtrada por tempo).
    assert portfolio["realized_price_pnl"] == pytest.approx((110.0 - 100.0) * 0.01)
    # period_performance (scope=daily, "hoje"): a posição continua OPEN,
    # nunca aparece em closed_positions -- nada vaza para o recorte diário.
    assert period["realized_price_pnl"] == pytest.approx(0.0)
    assert period["closed_trades_count"] == 0


def test_late_opposite_fill_blocked_fee_still_reduces_equity(tmp_path):
    """Correção do item 1 da auditoria: um fill bloqueado por
    LATE_OPPOSITE_FILL_BLOCKED (nunca aplicado a nenhuma Position) tem uma
    taxa REAL, genuinamente incorrida -- precisa reduzir a equity mesmo
    assim. Fonte canônica (Execution.fee) captura isso; Position.fees_paid
    sozinho NUNCA capturaria (é exatamente o bug relatado)."""
    client, orch = _make_client(tmp_path, "eq_late_opposite_fee.db", "BTCUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, 90.0, 110.0)
        order = _make_order(session, "BTCUSDT", "SELL", "late-opposite-1")
        state = repo.get_or_create_system_state(session)
        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
            fills=[FillEvent("EXEC-late-opposite-1", 0.01, 100.0, 0.15)],
        )
        fill_service.apply_order_snapshot(
            session, state, None, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert state.state_ambiguous is True

    with session_scope(orch.session_factory) as session:
        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].fees_paid == pytest.approx(0.0)

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["fees_paid"] == pytest.approx(0.15)
    assert body["equity"] == pytest.approx(1000.0 - 0.15 + body["unrealized_pnl"])


def test_rejected_or_unfilled_order_never_reduces_equity(tmp_path):
    """Uma ordem sem NENHUM fill real (rejeitada, ou ainda pendente) nunca
    gera uma linha Execution -- fees_paid permanece 0."""
    client, orch = _make_client(tmp_path, "eq_rejected_order.db", "BTCUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        order = _make_order(session, "BTCUSDT", "BUY", "rejected-1")
        repo.transition_order_status(session, order, OrderStatus.REJECTED)

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["fees_paid"] == pytest.approx(0.0)
    assert body["equity"] == pytest.approx(1000.0)


def test_fees_consolidated_equals_sum_of_per_symbol(tmp_path):
    client, orch = _make_client(tmp_path, "eq_fees_per_symbol.db", "BTCUSDT,ETHUSDT", starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        btc_order = _make_order(session, "BTCUSDT", "BUY", "fees-btc-1")
        _apply_fill(session, btc_order, "EXEC-fees-btc-1", 0.01, 100.0, 0.3, is_close=False)
        eth_order = _make_order(session, "ETHUSDT", "BUY", "fees-eth-1")
        _apply_fill(session, eth_order, "EXEC-fees-eth-1", 0.1, 2000.0, 0.7, is_close=False)

    body = client.get("/api/portfolio-summary").json()
    portfolio, per_symbol = body["portfolio"], body["per_symbol"]
    per_symbol_sum = sum(s["fees_paid"] for s in per_symbol.values())
    assert portfolio["fees_paid"] == pytest.approx(1.0)
    assert per_symbol_sum == pytest.approx(portfolio["fees_paid"])
    assert per_symbol["BTCUSDT"]["fees_paid"] == pytest.approx(0.3)
    assert per_symbol["ETHUSDT"]["fees_paid"] == pytest.approx(0.7)


def test_only_fee_whose_fill_occurred_in_period_appears_in_period_performance(tmp_path):
    """Item 7: apenas a taxa cujo fill ocorreu no período aparece em
    `period_performance` (filtro real por `Execution.executed_at`)."""
    client, orch = _make_client(tmp_path, "eq_fees_scope.db", "BTCUSDT", starting_balance=1000.0)
    _extend_base_into_the_past(orch, datetime.now(timezone.utc) - timedelta(days=2))
    with session_scope(orch.session_factory) as session:
        order = _make_order(session, "BTCUSDT", "BUY", "fees-scope-1")
        _apply_fill(session, order, "EXEC-fees-scope-1", 0.01, 100.0, 0.4, is_close=False)

        from sqlalchemy import select

        from app.persistence.models import Execution

        yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
        exec_row = session.execute(
            select(Execution).where(Execution.order_id == order.id)
        ).scalar_one()
        exec_row.executed_at = yesterday

    lifetime_body = client.get("/api/portfolio-summary?scope=lifetime").json()
    daily_body = client.get("/api/portfolio-summary?scope=daily").json()
    # Equity lifetime sempre inclui a taxa (independe de escopo) -- a base
    # contábil começou antes de "ontem", então a taxa está dentro dela.
    assert lifetime_body["portfolio"]["fees_paid"] == pytest.approx(0.4)
    assert daily_body["portfolio"]["fees_paid"] == pytest.approx(0.4)  # equity NUNCA varia com scope
    # period_performance respeita o corte: taxa de ontem nunca aparece em "hoje".
    assert lifetime_body["period_performance"]["fees_paid"] == pytest.approx(0.4)  # since=base_started_at
    assert daily_body["period_performance"]["fees_paid"] == pytest.approx(0.0)


# --- funding: lifetime na equity, filtrado no período --------------------

def test_funding_received_increases_equity(tmp_path):
    client, orch = _make_client(tmp_path, "eq_funding_received.db", "BTCUSDT", starting_balance=1000.0)
    orch.funding_provider = object()
    with session_scope(orch.session_factory) as session:
        from app.persistence.models import FundingEvent

        session.add(FundingEvent(
            funding_id="f-1", symbol="BTCUSDT", amount=5.0, occurred_at=datetime.now(timezone.utc),
        ))

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["funding_received"] == 5.0
    assert body["funding_paid"] == 0.0
    assert body["funding_net"] == 5.0
    assert body["equity"] == 1005.0


def test_funding_paid_decreases_equity(tmp_path):
    client, orch = _make_client(tmp_path, "eq_funding_paid.db", "BTCUSDT", starting_balance=1000.0)
    orch.funding_provider = object()
    with session_scope(orch.session_factory) as session:
        from app.persistence.models import FundingEvent

        session.add(FundingEvent(
            funding_id="f-2", symbol="BTCUSDT", amount=-3.0, occurred_at=datetime.now(timezone.utc),
        ))

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["funding_paid"] == 3.0
    assert body["funding_received"] == 0.0
    assert body["equity"] == 997.0


def test_funding_before_period_still_in_equity_but_not_in_period(tmp_path):
    client, orch = _make_client(tmp_path, "eq_funding_scope.db", "BTCUSDT", starting_balance=1000.0)
    orch.funding_provider = object()
    _extend_base_into_the_past(orch, datetime.now(timezone.utc) - timedelta(days=2))
    with session_scope(orch.session_factory) as session:
        from app.persistence.models import FundingEvent

        session.add(FundingEvent(
            funding_id="f-old", symbol="BTCUSDT", amount=8.0,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=1, hours=1),
        ))

    body = client.get("/api/portfolio-summary?scope=daily").json()
    assert body["portfolio"]["funding_net"] == 8.0  # equity lifetime -- sempre inclui
    assert body["period_performance"]["funding_net"] == 0.0  # fora do dia UTC corrente


def test_no_mark_available_marks_equity_incomplete(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_no_mark.db", "BTCUSDT")
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="OPEN", fees_paid=0.0,
        ))

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["equity_complete"] is False


def test_invalid_scope_is_rejected(tmp_path):
    client, orch = _make_client(tmp_path, "eq_invalid_scope.db", "BTCUSDT")
    resp = client.get("/api/portfolio-summary?scope=century")
    assert resp.status_code == 400


# --- period_performance: recorte de desempenho, nunca chamado de equity --

def test_period_performance_never_includes_starting_balance_or_equity(tmp_path):
    client, orch = _make_client(tmp_path, "eq_period_shape.db", "BTCUSDT")
    period = client.get("/api/portfolio-summary?scope=daily").json()["period_performance"]
    assert "starting_balance" not in period
    assert "equity" not in period
    assert "unrealized_pnl" not in period


def test_period_performance_realized_pnl_attribution_is_position_close(tmp_path):
    """Não existe ledger de P&L por fill -- a atribuição é sempre pelo
    fechamento da posição, exposta explicitamente."""
    client, orch = _make_client(tmp_path, "eq_attribution.db", "BTCUSDT")
    period = client.get("/api/portfolio-summary?scope=lifetime").json()["period_performance"]
    assert period["realized_pnl_attribution"] == "position_close"


def test_position_closed_within_period_follows_documented_attribution(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_period_closed_trade.db", "BTCUSDT", starting_balance=1000.0)
    now = datetime.now(timezone.utc)
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED", realized_pnl=7.0,
            fees_paid=0.0, opened_at=now, closed_at=now,
        ))

    period = client.get("/api/portfolio-summary?scope=daily").json()["period_performance"]
    assert period["realized_price_pnl"] == pytest.approx(7.0)
    assert period["closed_trades_count"] == 1


def test_session_scope_excludes_trades_closed_before_session_started(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_session_window.db", "BTCUSDT", starting_balance=1000.0)
    old_time = datetime.now(timezone.utc) - timedelta(days=30)
    _extend_base_into_the_past(orch, old_time - timedelta(hours=1))
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED", realized_pnl=999.0,
            fees_paid=0.0, opened_at=old_time, closed_at=old_time,
        ))

    lifetime_body = client.get("/api/portfolio-summary?scope=lifetime").json()
    session_body = client.get("/api/portfolio-summary?scope=session").json()
    assert lifetime_body["period_performance"]["realized_price_pnl"] == 999.0
    assert session_body["period_performance"]["realized_price_pnl"] == 0.0
    # Mas a equity (portfolio) é idêntica nos dois -- decisão central do PO.
    assert lifetime_body["portfolio"]["equity"] == session_body["portfolio"]["equity"]


def test_daily_scope_uses_real_utc_day_filter(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_daily_scope.db", "BTCUSDT", starting_balance=1000.0)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
    _extend_base_into_the_past(orch, yesterday - timedelta(hours=1))
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED", realized_pnl=42.0,
            fees_paid=0.0, opened_at=yesterday, closed_at=yesterday,
        ))

    daily_body = client.get("/api/portfolio-summary?scope=daily").json()
    lifetime_body = client.get("/api/portfolio-summary?scope=lifetime").json()
    assert lifetime_body["period_performance"]["realized_price_pnl"] == 42.0
    assert daily_body["period_performance"]["realized_price_pnl"] == 0.0
    assert lifetime_body["portfolio"]["equity"] == daily_body["portfolio"]["equity"]


# --- per_symbol -----------------------------------------------------------

def test_per_symbol_never_mixes_prices_or_percentages(tmp_path):
    client, orch = _make_client(tmp_path, "eq_per_symbol.db", "BTCUSDT,ETHUSDT")
    for _ in range(6):
        orch.tick()

    body = client.get("/api/portfolio-summary").json()
    assert set(body["per_symbol"]) == {"BTCUSDT", "ETHUSDT"}
    for symbol, data in body["per_symbol"].items():
        assert "starting_balance" not in data
        assert "equity" not in data


def test_consolidated_realized_pnl_equals_sum_of_per_symbol(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "eq_consolidated_sum.db", "BTCUSDT,ETHUSDT", starting_balance=1000.0)
    now = datetime.now(timezone.utc)
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="CLOSED",
            realized_pnl=10.0, fees_paid=0.0, opened_at=now, closed_at=now,
        ))
        session.add(Position(
            symbol="ETHUSDT", side="BUY", qty=0.1, avg_entry_price=2000.0,
            stop_loss=1900.0, take_profit=2200.0, status="CLOSED",
            realized_pnl=-4.0, fees_paid=0.0, opened_at=now, closed_at=now,
        ))

    body = client.get("/api/portfolio-summary").json()
    portfolio, per_symbol = body["portfolio"], body["per_symbol"]
    per_symbol_sum = sum(s["realized_price_pnl"] for s in per_symbol.values())
    assert portfolio["realized_price_pnl"] == per_symbol_sum == 6.0


# --- saldo inicial congelado (item 4 da decisão do PO) --------------------

def test_starting_balance_resolved_from_active_session_snapshot(tmp_path):
    """O valor vem do `config_snapshot_json` da sessão ATIVA, nunca de
    `Settings` lido ao vivo -- prova indireta: alterar `orch.settings`
    depois de o processo já ter subido não muda `starting_balance`."""
    client, orch = _make_client(tmp_path, "eq_frozen_balance.db", "BTCUSDT", starting_balance=1234.0)
    body_before = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body_before["starting_balance"] == 1234.0
    assert body_before["starting_balance_source"] == "session_snapshot"

    # Simula o Settings do processo mudando SEM criar uma sessão nova
    # (ex.: alguém mutou o objeto em memória por engano) -- a API deve
    # continuar honrando o valor CONGELADO na sessão, nunca o novo valor.
    orch.settings.paper_starting_balance_usd = 9999.0
    body_after = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body_after["starting_balance"] == 1234.0


def test_changing_settings_without_new_session_never_reinterprets_history(tmp_path):
    """Item 4: alterar o Settings ao vivo (sem passar por
    start_or_resume_session -- ex. mutação direta em memória) nunca
    reinterpreta a sessão já persistida."""
    client, orch = _make_client(tmp_path, "eq_no_silent_reinterpret.db", "BTCUSDT", starting_balance=500.0)
    orch.settings.paper_starting_balance_usd = 50000.0
    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["starting_balance"] == 500.0
    assert body["equity"] == 500.0


def test_legacy_session_without_the_field_falls_back_to_1000(tmp_path):
    """Sessão "legada" simulada: snapshot sem a chave
    `paper_starting_balance_usd` -- fallback explícito de 1000.0, nunca o
    Settings atual do processo."""
    import json as json_module

    client, orch = _make_client(tmp_path, "eq_legacy_fallback.db", "BTCUSDT", starting_balance=42.0)
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active = repo.get_active_session(session, state)
        snapshot = json_module.loads(active.config_snapshot_json)
        del snapshot["paper_starting_balance_usd"]
        active.config_snapshot_json = json_module.dumps(snapshot)

    body = client.get("/api/portfolio-summary").json()["portfolio"]
    assert body["starting_balance"] == 1000.0
    assert body["starting_balance_source"] == "legacy_fallback_missing_field"


def test_equity_curve_and_portfolio_summary_use_the_same_starting_balance(tmp_path):
    """Item 4: as duas rotas usam a MESMA resolução canônica."""
    client, orch = _make_client(tmp_path, "eq_curve_same_source.db", "BTCUSDT", starting_balance=3000.0)
    points = client.get("/api/equity-curve").json()
    summary = client.get("/api/portfolio-summary").json()["portfolio"]
    assert points[0]["equity"] == summary["starting_balance"] == 3000.0


def test_no_independent_hardcode_remains(tmp_path):
    """Confirma que os três hardcodes históricos (backend `_metrics_for_trades`,
    `/api/equity-curve`, frontend `"1000.00"`) continuam corrigidos --
    valores customizados se propagam para AMBAS as rotas."""
    client, orch = _make_client(tmp_path, "eq_no_hardcode.db", "BTCUSDT", starting_balance=8500.0)
    curve = client.get("/api/equity-curve").json()
    summary = client.get("/api/portfolio-summary").json()["portfolio"]
    assert curve[0]["equity"] == 8500.0
    assert summary["starting_balance"] == 8500.0
    assert summary["equity"] == 8500.0


# --- mudança de saldo com posição aberta: regra de reset seguro ----------

def test_starting_balance_change_with_open_position_is_refused(tmp_path):
    """Item 5 da decisão do PO: sem ledger de capital, mudar
    PAPER_STARTING_BALANCE_USD com posição aberta é recusado -- nunca
    reinterpreta silenciosamente uma posição sob uma nova âncora."""
    from app.persistence.db import init_db, make_engine, make_session_factory
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyConfig

    engine = make_engine(f"sqlite:///{tmp_path / 'eq_reset_guard.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    limits = RiskLimits(
        max_position_usd=50.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
        max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
        max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
    )
    settings_a = Settings(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:", paper_starting_balance_usd=1000.0)
    settings_b = Settings(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:", paper_starting_balance_usd=5000.0)

    with session_scope(session_factory) as session:
        start_or_resume_session(session, settings_a, "v1", limits, StrategyConfig())
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, 90.0, 110.0)

    with pytest.raises(StartingBalanceResetBlockedError):
        with session_scope(session_factory) as session:
            start_or_resume_session(session, settings_b, "v1", limits, StrategyConfig())


def test_starting_balance_change_without_open_position_is_allowed(tmp_path):
    from app.persistence.db import init_db, make_engine, make_session_factory
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyConfig

    engine = make_engine(f"sqlite:///{tmp_path / 'eq_reset_allowed.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    limits = RiskLimits(
        max_position_usd=50.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
        max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
        max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
    )
    settings_a = Settings(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:", paper_starting_balance_usd=1000.0)
    settings_b = Settings(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:", paper_starting_balance_usd=5000.0)

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings_a, "v1", limits, StrategyConfig())
        old_id = old.id
        # Nenhuma posição aberta -- reset seguro.

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings_b, "v1", limits, StrategyConfig())
        assert new.id != old_id


# --- integridade geral ------------------------------------------------

def test_equity_is_never_persisted_to_account_snapshots(tmp_path):
    client, orch = _make_client(tmp_path, "eq_no_snapshot_writes.db", "BTCUSDT")
    client.get("/api/portfolio-summary")
    client.get("/api/portfolio-summary")
    with session_scope(orch.session_factory) as session:
        assert repo.latest_account_snapshot(session) is None
