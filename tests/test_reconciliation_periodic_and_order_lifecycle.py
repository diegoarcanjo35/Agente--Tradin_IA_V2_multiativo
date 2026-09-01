"""Fase 2 v1.0 -- Estágio B: order state machine wiring through the
orchestrator, periodic reconciliation, reconciliation staleness (entry-only
gate), and kill-switch cancellation of pending orders.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from tests.factories import activate_operational_state, approved_close_order, base_risk_context
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import (
    _generate_kline_rows,
    _KlineSequenceTransport,
    make_bybit_demo_settings,
)


# --- Order state machine wiring through the orchestrator --------------------

def test_order_filled_via_orchestrator_transitions_state_machine_and_persists_totals():
    settings = make_bybit_demo_settings(risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0)
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=20)
    transport = _KlineSequenceTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    activate_operational_state(orch)

    result = None
    for _ in range(len(rows) + 2):
        result = orch.tick()
        if result["status"] == "order_filled":
            break
    assert result["status"] == "order_filled"

    with session_scope(orch.session_factory) as session:
        from app.persistence.models import Order, OrderEvent

        order = session.get(Order, result["order_id"])
        assert order.status == OrderStatus.FILLED.value
        assert order.filled_qty > 0
        assert order.avg_fill_price > 0
        assert order.exchange_order_id
        assert order.reference_price is not None  # item 7.6: needed for slippage tracking

        events = session.execute(
            __import__("sqlalchemy").select(OrderEvent).where(OrderEvent.order_id == order.id)
        ).scalars().all()
        assert events, "no order_events audit rows were written"
        assert events[-1].to_status == OrderStatus.FILLED.value


def test_order_ending_unknown_blocks_further_new_entries():
    """submit() that can never confirm a terminal status must land the
    order in UNKNOWN, flip SystemState.order_state_unknown, and block
    further NEW entries via trading_blocked (item 7.2's "nenhuma ordem
    UNKNOWN pode liberar nova exposição")."""
    settings = make_bybit_demo_settings(risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0)
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=20)

    class _NeverConfirmsTransport(_KlineSequenceTransport):
        def http_post(self, url, payload):
            resp = self._base.http_post(url, payload)
            # Deliberately never queue a status -- polling finds nothing,
            # forcing UNKNOWN, unlike the base fixture's auto-Filled.
            return resp

    transport = _NeverConfirmsTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    activate_operational_state(orch)

    result = None
    for _ in range(len(rows) + 2):
        result = orch.tick()
        if result["status"] == "order_not_filled":
            break
    assert result["status"] == "order_not_filled"
    assert result["order_status"] == OrderStatus.UNKNOWN.value

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.order_state_unknown is True
        assert state.trading_blocked is True
        assert "unknown" in state.block_reason.lower() or "desconhecido" in state.block_reason.lower()


# --- Periodic reconciliation --------------------------------------------

def test_periodic_reconciliation_runs_after_interval_elapses_and_updates_timestamp():
    settings = make_bybit_demo_settings(reconciliation_interval_seconds=0.0)
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)  # startup reconcile already ran once

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        first_stamp = state.last_reconciliation_at
        assert first_stamp is not None

    calls_before = len(transport.get_calls)
    orch.tick()  # interval=0 -> periodic reconciliation must fire again this tick
    calls_after = len(transport.get_calls)
    assert calls_after > calls_before, "periodic reconciliation never queried the exchange again"

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.last_reconciliation_at is not None


def test_reconciliation_diverged_flag_set_alongside_state_ambiguous():
    """The new, more specifically-named flag mirrors state_ambiguous for a
    reconciliation-caused block (Fase 1's state_ambiguous usage is left
    untouched for backward compatibility -- see app/orchestrator.py)."""
    settings = make_bybit_demo_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        # Simulate a local-only position the exchange doesn't know about.
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, 90.0, 110.0)
        orch.reconcile(session, state)
        assert state.state_ambiguous is True
        assert state.reconciliation_diverged is True
        assert state.trading_blocked is True


# --- Reconciliation staleness: entry-only gate ---------------------------

def test_reconciliation_stale_blocks_new_entries_but_evaluate_close_ignores_it():
    engine = RiskEngine(RiskLimits())

    open_context = base_risk_context(reconciliation_stale=True)
    from app.strategy.schemas import Signal

    signal = Signal(
        symbol="BTCUSDT", direction="BUY", justification="teste", created_at=open_context.now,
        observed_price=40000.0, atr=100.0, source_candle_open_time=open_context.now,
        stop_loss=39000.0, take_profit=41000.0, params={},
    )
    open_result = engine.evaluate(signal, signal_id=1, context=open_context)
    assert open_result.approved is False
    assert "atrasada" in open_result.reason.lower()

    close_context = base_risk_context(reconciliation_stale=True)
    close_result = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=0.001,
        position_exists=True, position_qty=0.001, position_side="BUY", context=close_context,
    )
    assert close_result.approved is True  # closes are never blocked by staleness


def test_orchestrator_flags_stale_reconciliation_into_risk_context():
    settings = make_bybit_demo_settings(reconciliation_interval_seconds=100000.0,
                                         reconciliation_max_delay_seconds=1.0)
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        # Force a stale-looking prior reconciliation, far enough in the past
        # to exceed max_delay but not force the (huge) periodic interval.
        state.last_reconciliation_at = datetime.now(timezone.utc) - timedelta(hours=1)

    result = orch.tick()
    assert result["status"] in ("no_new_candle", "no_data", "retryable_error", "fatal_error", "gap_detected")
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_stale is True


# --- Kill switch cancels pending orders -----------------------------------

def make_bybit_client(tmp_path, transport):
    db_path = tmp_path / "kill_switch_cancel_test.db"
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{db_path}")
    orch = build_orchestrator(settings, bybit_transport=transport)
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.include_router(routes_control.router, prefix="/api")
    return TestClient(app), orch


def test_kill_switch_engage_cancels_pending_bybit_order(tmp_path):
    transport = FakeBybitTransport()
    client, orch = make_bybit_client(tmp_path, transport)

    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key="pending-1", risk_evaluation_id=risk_eval.id, symbol="BTCUSDT",
            side="BUY", qty=0.001, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = "EX-PENDING-1"
        order_id = order.id

    transport.queue_status("EX-PENDING-1", [{"orderStatus": "Cancelled"}])

    resp = client.post("/api/kill-switch/engage")
    assert resp.status_code == 200
    assert order_id in resp.json()["ordens_canceladas"]

    with session_scope(orch.session_factory) as session:
        from app.persistence.models import Order

        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCELLED.value

    cancel_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/cancel")]
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1]["orderId"] == "EX-PENDING-1"
