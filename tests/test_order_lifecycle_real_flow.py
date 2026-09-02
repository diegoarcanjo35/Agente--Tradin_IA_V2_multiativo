"""Correção da Fase 2 v1.1 #1/#2: the real order lifecycle actually goes
through SUBMITTED/CANCEL_PENDING as persisted, visible states (not just
skipped straight to a terminal one), fills accumulate by delta across
multiple polls, a process restart resumes tracking via the periodic poller
without ever re-submitting, and /order/create is posted exactly once per
order no matter how many ticks/polls happen afterward.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.main import build_orchestrator
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order, OrderEvent
from tests.factories import activate_operational_state
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import (
    _generate_kline_rows,
    _KlineSequenceTransport,
    make_bybit_demo_settings,
)


def _order_event_sequence(session, order_id: int) -> list[str]:
    rows = session.execute(
        select(OrderEvent).where(OrderEvent.order_id == order_id).order_by(OrderEvent.id)
    ).scalars().all()
    return [r.to_status for r in rows]


def _drive_until(orch, statuses, max_ticks=40):
    result = None
    for _ in range(max_ticks):
        result = orch.tick()
        if result["status"] in statuses:
            return result
    return result


def test_real_flow_persists_submitted_as_an_intermediate_state_before_filled(tmp_path):
    """Correção #1: the audited gap was that SUBMITTED never got persisted
    -- the real flow jumped straight from PENDING_SUBMIT to a terminal
    status. This proves order_events actually records SUBMITTED as its own
    row, distinct from the eventual FILLED."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0,
        database_url=f"sqlite:///{tmp_path / 'submitted_state.db'}",
    )
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=10)
    transport = _KlineSequenceTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    activate_operational_state(orch)

    result = _drive_until(orch, {"order_filled"}, max_ticks=len(rows) + 2)
    assert result["status"] == "order_filled"

    with session_scope(orch.session_factory) as session:
        events = _order_event_sequence(session, result["order_id"])
        assert events == [OrderStatus.SUBMITTED.value, OrderStatus.FILLED.value]


def test_partial_fills_accumulate_by_delta_across_multiple_polls_via_orchestrator(tmp_path):
    """Correção #2 exact reproduction: cumExecQty going 0.004 -> 0.007 ->
    0.010 across successive polls must be applied by DELTA, never by
    re-summing the cumulative totals, and the position must reflect the
    correct running weighted-average price. Uses a directly-constructed
    SUBMITTED order (same pattern as the restart-recovery tests above) so
    the fill sequence is driven by hand, deterministically, through the
    real `Orchestrator._maybe_poll_open_orders` -> `fill_service` path --
    not through the strategy/candle pipeline, which would otherwise also
    generate a later opposing-signal close and confuse the exact deltas
    under test."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=1000.0, risk_max_total_exposure_usd=1000.0,
        database_url=f"sqlite:///{tmp_path / 'partial_deltas.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key="delta-1", risk_evaluation_id=risk_eval.id,
            symbol="BTCUSDT", side="BUY", qty=0.01, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = "EX-DELTA-1"
        order_id = order.id
    exchange_order_id = "EX-DELTA-1"

    # First partial: 0 -> 0.004
    transport.queue_status(exchange_order_id, [{"orderStatus": "PartiallyFilled"}])
    transport.queue_executions(exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.004", "execPrice": "100.0", "execFee": "0.001"},
    ])
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None  # force the periodic poller to run now
        orch._maybe_poll_open_orders(session, state)
    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.filled_qty == pytest.approx(0.004)
        assert order.avg_fill_price == pytest.approx(100.0)

    # Second partial: reports BOTH fills cumulatively (as the real
    # /v5/execution/list always would) -- 0.004 -> 0.007.
    transport.queue_status(exchange_order_id, [{"orderStatus": "PartiallyFilled"}])
    transport.queue_executions(exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.004", "execPrice": "100.0", "execFee": "0.001"},
        {"execId": "EXEC-2", "execQty": "0.003", "execPrice": "100.4", "execFee": "0.0007"},
    ])
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)
    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.filled_qty == pytest.approx(0.007)  # never 0.004+0.007
        expected_avg = (0.004 * 100.0 + 0.003 * 100.4) / 0.007
        assert order.avg_fill_price == pytest.approx(expected_avg)
        assert order.fees_total == pytest.approx(0.0017)

    # Completion: 0.007 -> 0.010.
    transport.queue_status(exchange_order_id, [{"orderStatus": "Filled"}])
    transport.queue_executions(exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.004", "execPrice": "100.0", "execFee": "0.001"},
        {"execId": "EXEC-2", "execQty": "0.003", "execPrice": "100.4", "execFee": "0.0007"},
        {"execId": "EXEC-3", "execQty": "0.003", "execPrice": "100.6", "execFee": "0.0006"},
    ])
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)
    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.filled_qty == pytest.approx(0.01)
        assert order.fees_total == pytest.approx(0.0023)
        events = _order_event_sequence(session, order_id)
        # Only 3 events, not 4: the second poll's status (PARTIALLY_FILLED)
        # was UNCHANGED from the first, so fill_service correctly does not
        # log a redundant self-transition event -- only a genuine status
        # CHANGE is ever recorded, while the fill delta itself is still
        # applied every time regardless.
        assert events == [
            OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value, OrderStatus.FILLED.value,
        ]


def test_submitted_to_cancel_pending_to_cancelled_via_kill_switch(tmp_path):
    """Correção #1: cancellation must persist CANCEL_PENDING BEFORE
    requesting it on the exchange -- never skip straight to CANCELLED."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_control

    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'cancel_pending.db'}")
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key="pending-cancel-1", risk_evaluation_id=risk_eval.id,
            symbol="BTCUSDT", side="BUY", qty=0.001, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = "EX-CANCEL-1"
        order_id = order.id

    transport.queue_status("EX-CANCEL-1", [{"orderStatus": "Cancelled"}])

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.include_router(routes_control.router, prefix="/api")
    client = TestClient(app)

    resp = client.post("/api/kill-switch/engage")
    assert resp.status_code == 200
    assert order_id in resp.json()["ordens_canceladas"]

    with session_scope(orch.session_factory) as session:
        events = _order_event_sequence(session, order_id)
        assert events == [OrderStatus.SUBMITTED.value, OrderStatus.CANCEL_PENDING.value, OrderStatus.CANCELLED.value]


def test_order_left_submitted_between_accept_and_first_poll_is_recovered_by_periodic_poller(tmp_path):
    """Correção #1 required test: a crash between the exchange accepting
    the order and the first status poll must be recoverable -- never
    hang, never re-submit."""
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'gap_before_first_poll.db'}")
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key="gap-1", risk_evaluation_id=risk_eval.id,
            symbol="BTCUSDT", side="BUY", qty=0.001, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        )
        # Simulates exactly the moment after submit() returned SUBMITTED but
        # BEFORE the orchestrator ever got to call poll_order() (e.g. the
        # process died right there).
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = "EX-GAP-1"
        order_id = order.id

    transport.queue_status("EX-GAP-1", [{"orderStatus": "Filled"}])
    transport.queue_executions("EX-GAP-1", [
        {"execId": "EXEC-GAP-1", "execQty": "0.001", "execPrice": "100.0", "execFee": "0.0001"},
    ])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.filled_qty == pytest.approx(0.001)

    create_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/create")]
    assert len(create_calls) == 0  # never re-submitted -- this order was created directly via repo, not submit()


def test_restart_resumes_tracking_every_non_terminal_status_without_resubmitting(tmp_path):
    """Correção #1 required test: after a simulated restart (a brand-new
    Orchestrator/execution engine instance, sharing nothing in memory),
    orders left SUBMITTED / PARTIALLY_FILLED / CANCEL_PENDING / UNKNOWN are
    all picked back up by the periodic poller, never re-submitted."""
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'restart_resume.db'}")
    transport = FakeBybitTransport()
    orch_before = build_orchestrator(settings, bybit_transport=transport)

    statuses_and_ids = [
        (OrderStatus.SUBMITTED, "EX-R-SUBMITTED"),
        (OrderStatus.PARTIALLY_FILLED, "EX-R-PARTIAL"),
        (OrderStatus.CANCEL_PENDING, "EX-R-CANCELPENDING"),
        (OrderStatus.UNKNOWN, "EX-R-UNKNOWN"),
    ]
    order_ids = {}
    with session_scope(orch_before.session_factory) as session:
        for i, (status, exchange_id) in enumerate(statuses_and_ids):
            signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
            risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
            order = repo.save_order(
                session, idempotency_key=f"restart-{i}", risk_evaluation_id=risk_eval.id,
                symbol="BTCUSDT", side="BUY", qty=0.001, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
            )
            repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
            order.exchange_order_id = exchange_id
            if status != OrderStatus.SUBMITTED:
                repo.transition_order_status(session, order, status)
            order_ids[status] = order.id

    # Queue terminal resolutions for each on the SAME fake transport --
    # simulating a real exchange that continued existing independently of
    # our process across the "restart".
    transport.queue_status("EX-R-SUBMITTED", [{"orderStatus": "Filled"}])
    transport.queue_executions("EX-R-SUBMITTED", [
        {"execId": "EXEC-R1", "execQty": "0.001", "execPrice": "100.0", "execFee": "0.0001"},
    ])
    transport.queue_status("EX-R-PARTIAL", [{"orderStatus": "Filled"}])
    transport.queue_executions("EX-R-PARTIAL", [
        {"execId": "EXEC-R2", "execQty": "0.001", "execPrice": "100.0", "execFee": "0.0001"},
    ])
    transport.queue_status("EX-R-CANCELPENDING", [{"orderStatus": "Cancelled"}])
    transport.queue_status("EX-R-UNKNOWN", [{"orderStatus": "Cancelled"}])

    # "Restart": a brand-new Orchestrator built the same way, sharing
    # nothing in memory (a fresh execution engine instance too), against
    # the SAME database and the SAME fake transport (simulating the same
    # real exchange).
    orch_after = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch_after.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch_after._last_open_order_poll_at = None
        orch_after._maybe_poll_open_orders(session, state)

    with session_scope(orch_after.session_factory) as session:
        assert session.get(Order, order_ids[OrderStatus.SUBMITTED]).status == OrderStatus.FILLED.value
        assert session.get(Order, order_ids[OrderStatus.PARTIALLY_FILLED]).status == OrderStatus.FILLED.value
        assert session.get(Order, order_ids[OrderStatus.CANCEL_PENDING]).status == OrderStatus.CANCELLED.value
        assert session.get(Order, order_ids[OrderStatus.UNKNOWN]).status == OrderStatus.CANCELLED.value

    create_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/create")]
    assert len(create_calls) == 0  # none of these were ever (re)submitted


def test_create_is_posted_exactly_once_across_many_ticks_and_periodic_polls(tmp_path):
    settings = make_bybit_demo_settings(
        risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0,
        open_order_poll_interval_seconds=0.0,
        database_url=f"sqlite:///{tmp_path / 'create_once.db'}",
    )
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=10)
    transport = _KlineSequenceTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    activate_operational_state(orch)

    result = _drive_until(orch, {"order_filled"}, max_ticks=len(rows) + 2)
    assert result["status"] == "order_filled"

    with session_scope(orch.session_factory) as session:
        idempotency_key = session.get(Order, result["order_id"]).idempotency_key

    # Several more ticks (periodic poller runs every time, interval=0) must
    # never post ANOTHER order/create for the SAME already-filled order --
    # a later opposing-signal close naturally posts its own, different
    # order/create, which is correct and must not be confused with a
    # duplicate resubmission of the original entry.
    for _ in range(5):
        orch.tick()

    create_calls_for_this_order = [
        c for c in base_transport.post_calls
        if c[0].endswith("/v5/order/create") and c[1].get("orderLinkId") == idempotency_key
    ]
    assert len(create_calls_for_this_order) == 1
