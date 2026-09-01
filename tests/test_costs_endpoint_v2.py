"""Fase 3.1.1 (correção final da auditoria do PO): `GET /api/costs` --
contrato refeito (item 6/9 da decisão do PO). Os campos antigos
`slippage_avg_usd`/`slippage_total_usd` (diferença unitária de preço, nunca
multiplicada por quantidade) são REMOVIDOS -- nunca reaproveitados com o
mesmo nome e semântica diferente.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.persistence.db import session_scope
from tests.factories import activate_operational_state


def _make_client(tmp_path, name, symbols):
    settings = Settings(mode=RunMode.REPLAY, symbols=symbols, database_url=f"sqlite:///{tmp_path / name}")
    orch = build_orchestrator(settings)
    activate_operational_state(orch)

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app), orch


def test_deprecated_slippage_fields_are_gone(tmp_path):
    client, orch = _make_client(tmp_path, "costs_v2_deprecated.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()
    body = client.get("/api/costs").json()
    assert "slippage_avg_usd" not in body
    assert "slippage_total_usd" not in body


def test_new_contract_fields_present(tmp_path):
    client, orch = _make_client(tmp_path, "costs_v2_new_fields.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()
    body = client.get("/api/costs").json()
    for field in (
        "fees_total", "adverse_slippage_cost_usd", "price_improvement_value_usd",
        "net_slippage_impact_usd", "avg_adverse_slippage_per_order_usd",
        "weighted_slippage_pct", "adverse_slippage_pct", "reference_notional_total_usd",
        "priced_orders_count", "unpriced_orders_count",
    ):
        assert field in body, f"campo ausente: {field}"


def test_symbol_filter_never_mixes_different_assets(tmp_path):
    """Item 8 da decisão do PO: /api/costs?symbol= nunca mistura notional
    de ativos diferentes na mesma soma."""
    client, orch = _make_client(tmp_path, "costs_v2_symbol_filter.db", "BTCUSDT,ETHUSDT")
    for _ in range(10):
        orch.tick()

    btc_only = client.get("/api/costs?symbol=BTCUSDT").json()
    eth_only = client.get("/api/costs?symbol=ETHUSDT").json()
    consolidated = client.get("/api/costs").json()

    assert btc_only["priced_orders_count"] + eth_only["priced_orders_count"] == consolidated["priced_orders_count"]
    # Notional de referência consolidado é a soma dos dois -- nunca um
    # valor arbitrário/menor que a soma das partes.
    if isinstance(btc_only["reference_notional_total_usd"], float) and isinstance(
        eth_only["reference_notional_total_usd"], float
    ):
        expected = btc_only["reference_notional_total_usd"] + eth_only["reference_notional_total_usd"]
        assert round(consolidated["reference_notional_total_usd"], 6) == round(expected, 6)


def test_unfiltered_costs_still_works_backward_compatible(tmp_path):
    client, orch = _make_client(tmp_path, "costs_v2_no_filter.db", "BTCUSDT")
    for _ in range(5):
        orch.tick()
    resp = client.get("/api/costs")
    assert resp.status_code == 200
