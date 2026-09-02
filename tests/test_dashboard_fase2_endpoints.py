"""Fase 2, item 7.9: new dashboard endpoints backing the operational
sections of the painel -- current session, orders + state machine, and
cost/slippage metrics.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from tests.factories import activate_operational_state
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import (
    _generate_kline_rows,
    _KlineSequenceTransport,
    make_bybit_demo_settings,
)


def make_client(tmp_path, name):
    settings = make_bybit_demo_settings(
        risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0,
        database_url=f"sqlite:///{tmp_path / name}",
    )
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=10)
    transport = _KlineSequenceTransport(base_transport, rows)
    orch = build_orchestrator(settings, bybit_transport=transport)
    activate_operational_state(orch)

    for _ in range(len(rows) + 2):
        result = orch.tick()
        if result["status"] == "order_filled":
            break

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app)


def test_state_endpoint_exposes_independent_block_causes(tmp_path):
    client = make_client(tmp_path, "state_block_causes.db")
    body = client.get("/api/state").json()
    for field in (
        "state_ambiguous", "clock_out_of_sync", "reconciliation_diverged",
        "reconciliation_stale", "order_state_unknown", "initialization_not_reconciled",
        "operational_state",
    ):
        assert field in body


def test_session_endpoint_reflects_real_activity(tmp_path):
    client = make_client(tmp_path, "session_endpoint.db")
    body = client.get("/api/session").json()
    assert body is not None
    assert body["status"] == "ATIVO"
    assert body["orders_count"] >= 1
    assert body["fills_count"] >= 1


def test_orders_endpoint_lists_filled_order_with_state_machine_fields(tmp_path):
    client = make_client(tmp_path, "orders_endpoint.db")
    body = client.get("/api/orders").json()
    assert body
    order = body[0]
    assert order["status"] == "FILLED"
    assert order["filled_qty"] > 0
    assert order["reference_price"] is not None


def test_costs_endpoint_computes_fees_and_slippage(tmp_path):
    client = make_client(tmp_path, "costs_endpoint.db")
    body = client.get("/api/costs").json()
    assert body["priced_orders_count"] >= 1
    assert isinstance(body["fees_total"], float)
