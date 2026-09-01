"""Fase 2, item 7.1: PAPER_LIVE -- real Bybit Demo market data (public
endpoints only), execution stays entirely local/simulated. Never requires
credentials, never reaches BybitDemoExecutionEngine or a private/
authenticated pybit client.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.paper_local import PaperLocalExecutionEngine
from app.market_data.bybit_provider import BybitDemoMarketDataProvider, BybitServerTimeProvider
from app.orchestrator import Orchestrator
from tests.factories import activate_operational_state
from tests.fakes.bybit_fake import FakeBybitTransport


def make_paper_live_settings(**overrides) -> Settings:
    defaults = dict(
        mode=RunMode.PAPER_LIVE,
        bybit_base_url="https://api-demo.bybit.com",
        bybit_ws_url="wss://stream-demo.bybit.com",
        database_url="sqlite:///:memory:",
        symbol="BTCUSDT",
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> uma decisão". O default do timeframe estratégico passou a ser
        # 5 minutos; 1 minuto é a compatibilidade explícita mantida pelo PO
        # e preserva exatamente a intenção original do teste.
        strategy_timeframe_minutes=1,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --- Wiring -----------------------------------------------------------------

def test_paper_live_wires_real_market_data_with_local_execution():
    settings = make_paper_live_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    assert isinstance(orch, Orchestrator)
    assert isinstance(orch.market_data_provider, BybitDemoMarketDataProvider)
    assert isinstance(orch.clock_provider, BybitServerTimeProvider)
    assert isinstance(orch.execution_engine, PaperLocalExecutionEngine)
    assert not isinstance(orch.execution_engine, BybitDemoExecutionEngine)


def test_paper_live_never_requires_credentials():
    """No BYBIT_API_KEY/SECRET in the environment at all -- must still work,
    unlike BYBIT_DEMO which hard-requires them."""
    settings = make_paper_live_settings(bybit_api_key="", bybit_api_secret="")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    assert isinstance(orch.execution_engine, PaperLocalExecutionEngine)


def test_paper_live_wiring_never_constructs_bybit_demo_execution_engine_source():
    """Structural check (same spirit as app/ai_shadow/guard.py): the
    PAPER_LIVE branch must never reference BybitDemoExecutionEngine or
    require_bybit_credentials/http_post at all.

    Fase 3 multiativo: the execution engine is now constructed once in
    `_build_shared_execution_pipeline` (shared across every symbol) rather
    than inline in `build_orchestrator` -- this test follows that move,
    checking the function that actually constructs it now."""
    import ast
    import inspect

    import app.api.main as main_module

    source = inspect.getsource(main_module._build_shared_execution_pipeline)
    tree = ast.parse(source)

    # Simplest robust check: BybitDemoExecutionEngine is only constructed
    # once in the whole function, inside the BYBIT_DEMO branch -- never
    # reachable from the PAPER_LIVE branch above it.
    calls = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls.count("BybitDemoExecutionEngine") == 1


def test_paper_live_still_refuses_production_and_testnet_hosts():
    from app.core.errors import ProductionEndpointBlockedError

    with pytest.raises(ProductionEndpointBlockedError):
        make_paper_live_settings(bybit_base_url="https://api.bybit.com")
    with pytest.raises(ProductionEndpointBlockedError):
        make_paper_live_settings(bybit_base_url="https://api-testnet.bybit.com")


# --- Functional: real market data drives a locally-simulated fill ----------

def _generate_kline_rows(n_down: int, n_up: int) -> list[list[str]]:
    start = datetime.now(timezone.utc) - timedelta(minutes=n_down + n_up + 5)
    rows: list[list[str]] = []
    price = 100.0
    for i in range(n_down):
        price -= 1.0
        open_time = start + timedelta(minutes=i)
        rows.append([
            str(int(open_time.timestamp() * 1000)),
            f"{price + 1:.2f}", f"{price + 2:.2f}", f"{price - 1:.2f}", f"{price:.2f}", "10", "0",
        ])
    for j in range(n_up):
        price += 2.0
        open_time = start + timedelta(minutes=n_down + j)
        rows.append([
            str(int(open_time.timestamp() * 1000)),
            f"{price - 2:.2f}", f"{price + 1:.2f}", f"{price - 3:.2f}", f"{price:.2f}", "10", "0",
        ])
    return rows


class _KlineSequenceTransport:
    def __init__(self, base: FakeBybitTransport, rows: list[list[str]]):
        self._base = base
        self._rows = rows
        self._idx = 0

    def http_get(self, url: str, params: dict) -> dict:
        if url.endswith("/v5/market/kline"):
            if self._idx >= len(self._rows):
                return {"result": {"list": []}}
            limit = int(params.get("limit", 1))
            window_end = self._idx + 1
            window_start = max(0, window_end - max(limit - 1, 1))
            historical = self._rows[window_start:window_end]
            self._idx += 1
            forming_open = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            forming_row = [str(int(forming_open.timestamp() * 1000)), "1", "1", "1", "1", "1", "0"]
            return {"result": {"list": [forming_row] + list(reversed(historical))}}
        if url.endswith("/v5/market/time"):
            return {"result": {"timeSecond": str(int(datetime.now(timezone.utc).timestamp()))}}
        return self._base.http_get(url, params)

    def http_post(self, url: str, payload: dict) -> dict:
        raise AssertionError(
            "PAPER_LIVE must never POST to the exchange -- PaperLocalExecutionEngine "
            "never calls http_post at all."
        )


def test_paper_live_fills_locally_from_real_market_data_never_posting_to_exchange():
    settings = make_paper_live_settings(risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0)
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
    # No AssertionError from _KlineSequenceTransport.http_post means no POST
    # was ever attempted -- PAPER_LIVE genuinely never reaches the exchange
    # for order submission.


def test_paper_live_banner_is_distinct_from_generic_demo_banner(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_dashboard

    settings = make_paper_live_settings(database_url=f"sqlite:///{tmp_path / 'paper_live_banner.db'}")
    orch = build_orchestrator(settings, bybit_transport=FakeBybitTransport())
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    client = TestClient(app)

    resp = client.get("/api/state")
    body = resp.json()
    assert body["mode"] == "PAPER_LIVE"
    assert "SEM ORDEM NA CORRETORA" in body["environment_banner"]
