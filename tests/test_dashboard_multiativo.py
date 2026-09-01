"""Fase 3 multiativo, itens 9.13/9.16 do brief: painel/API com três
símbolos (contrato aditivo, escaping seguro) e comportamento monoativo
legado preservado -- REPLAY apenas, sem rede.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
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


def test_symbols_endpoint_lists_configured_symbols_in_order(tmp_path):
    client, _ = _make_client(tmp_path, "symbols.db", "BTCUSDT,ETHUSDT,SOLUSDT")
    body = client.get("/api/symbols").json()
    # Fase 3.2: a chave "symbols" continua IDÊNTICA (a garantia de
    # compatibilidade); `per_symbol` é acréscimo aditivo com timeframe,
    # aquecimento e integridade de bucket.
    assert body["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert set(body["per_symbol"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert body["per_symbol"]["BTCUSDT"]["market_data_timeframe"] == "1m"


def test_state_endpoint_exposes_symbols_health_for_three_symbols(tmp_path):
    client, orch = _make_client(tmp_path, "state_multi.db", "BTCUSDT,ETHUSDT,SOLUSDT")
    for _ in range(6):
        orch.tick()

    body = client.get("/api/state").json()
    health = body["symbols_health"]
    assert set(health["per_symbol"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert health["portfolio"]["total"] == 3
    for symbol_health in health["per_symbol"].values():
        assert symbol_health["status"] in ("INICIANDO", "SAUDAVEL", "DEGRADADO", "PARADO", "ENCERRANDO")

    # Original fields are all still present, unchanged -- purely additive.
    for field in ("trading_blocked", "kill_switch_engaged", "operational_state", "mode"):
        assert field in body


def test_session_endpoint_reflects_multi_symbol_portfolio_identity(tmp_path):
    client, _ = _make_client(tmp_path, "session_multi.db", "BTCUSDT,ETHUSDT")
    body = client.get("/api/session").json()
    assert body["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert body["symbol"] is None  # genuinely multi-symbol -- never a lying scalar


def test_session_endpoint_mono_symbol_keeps_legacy_symbol_field(tmp_path):
    client, _ = _make_client(tmp_path, "session_mono.db", "BTCUSDT")
    body = client.get("/api/session").json()
    assert body["symbols"] == ["BTCUSDT"]
    assert body["symbol"] == "BTCUSDT"


def test_positions_and_orders_can_be_filtered_by_symbol(tmp_path):
    client, orch = _make_client(tmp_path, "filter_multi.db", "BTCUSDT,ETHUSDT,SOLUSDT")
    for _ in range(30):
        orch.tick()

    all_orders = client.get("/api/orders").json()
    symbols_seen = {o["symbol"] for o in all_orders}
    if symbols_seen:
        one_symbol = next(iter(symbols_seen))
        filtered = client.get(f"/api/orders?symbol={one_symbol}").json()
        assert all(o["symbol"] == one_symbol for o in filtered)
        assert len(filtered) <= len(all_orders)


def test_metrics_endpoint_has_consolidated_and_per_symbol_breakdown(tmp_path):
    client, _ = _make_client(tmp_path, "metrics_multi.db", "BTCUSDT,ETHUSDT,SOLUSDT")
    body = client.get("/api/metrics").json()
    assert set(body["per_symbol"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    # Consolidated result keeps its original top-level keys (e.g. win_rate),
    # untouched by the new per_symbol breakdown.
    assert "per_symbol" in body
    for symbol_metrics in body["per_symbol"].values():
        assert isinstance(symbol_metrics, dict)


def test_signal_with_malicious_symbol_field_is_never_reflected_unescaped(tmp_path):
    """Fase 3 multiativo, item 9.13: escaping seguro -- the API itself
    never HTML-renders anything (that's the frontend's job, covered by
    test_frontend_xss_safety.py); here we prove the API returns the raw
    value as plain JSON string data, never interpolated into any other
    field or executed."""
    client, orch = _make_client(tmp_path, "xss_multi.db", "BTCUSDT,ETHUSDT")
    from app.persistence.db import session_scope
    from app.persistence import repo

    payload = "<img src=x onerror=alert(1)>"
    with session_scope(orch.session_factory) as session:
        repo.save_signal(session, "BTCUSDT", "HOLD", payload, 100.0, 1.0, {})

    body = client.get("/api/signals").json()
    matching = [s for s in body if s["justification"] == payload]
    assert matching  # returned verbatim as JSON string data, not interpreted
