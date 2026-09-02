"""Correction v1.1 #1: BYBIT_DEMO must no longer end in NotImplementedError.
This exercises app.api.main.build_orchestrator()'s REAL wiring logic for
MODE=BYBIT_DEMO -- mode branching, provider/engine construction, clock
provider selection, and startup reconciliation -- with only the outermost
HTTP transport replaced by the no-network fake (tests/fakes/bybit_fake.py).
No mock replaces build_orchestrator() itself or any internal wiring code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.market_data.bybit_provider import BybitDemoMarketDataProvider, BybitServerTimeProvider
from app.orchestrator import Orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport


def make_bybit_demo_settings(**overrides) -> Settings:
    defaults = dict(
        mode=RunMode.BYBIT_DEMO,
        bybit_api_key="test-key",
        bybit_api_secret="test-secret",
        bybit_base_url="https://api-demo.bybit.com",
        bybit_ws_url="wss://stream-demo.bybit.com",
        database_url="sqlite:///:memory:",
        symbol="BTCUSDT",
        # Fase 3.2: estes testes exercitam o pipeline OPERACIONAL "um
        # candle -> uma decisão". O default passou a ser 5 minutos, então
        # o timeframe estratégico de 1 minuto (compatibilidade explícita
        # mantida pelo PO) preserva exatamente a intenção original.
        strategy_timeframe_minutes=1,
        # Fase 3.2: estas suítes verificam o CICLO DE VIDA da ordem
        # (submit/poll/fill/reconciliação), não a viabilidade de custo.
        # Com estimativa de custo zero o gate aprova qualquer movimento
        # esperado positivo -- é o cenário "custo zero configurado" que o
        # próprio PO listou como caso obrigatório, e mantém a intenção
        # original destes testes intacta. O gate em si é coberto, com
        # números reais, em tests/test_cost_gate.py.
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_bybit_demo_no_longer_raises_not_implemented():
    settings = make_bybit_demo_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    assert isinstance(orch, Orchestrator)
    assert isinstance(orch.execution_engine, BybitDemoExecutionEngine)
    assert isinstance(orch.market_data_provider, BybitDemoMarketDataProvider)
    assert isinstance(orch.clock_provider, BybitServerTimeProvider)


def test_bybit_demo_startup_reconciliation_runs_with_no_network():
    """build_orchestrator() calls orchestrator.reconcile() once at
    construction; with the fake transport reporting no remote positions and
    no local ones, it must complete cleanly (no exception, no network)."""
    settings = make_bybit_demo_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    assert len(transport.get_calls) >= 1  # reconciliation queried positions


def test_bybit_demo_requires_credentials_before_any_transport_is_built():
    settings = make_bybit_demo_settings(bybit_api_key="", bybit_api_secret="")
    from app.core.errors import ProductionEndpointBlockedError

    with pytest.raises(ProductionEndpointBlockedError):
        build_orchestrator(settings, bybit_transport=FakeBybitTransport())


def _generate_kline_rows(n_down: int, n_up: int) -> list[list[str]]:
    """Deterministic downtrend-then-rally sequence, long enough for the
    DEFAULT StrategyEngine config (fast=9/slow=21/atr=14) to actually
    produce a bullish crossover -- real past timestamps (minutes before
    "now") so BybitDemoMarketDataProvider's closed-candle check passes with
    its default now_fn=utcnow, no clock injection needed.

    Fase 3.3.1 -- ANCORAGEM NO PRESENTE: a série termina no último minuto
    JÁ FECHADO (o minuto corrente é servido à parte pelo
    `_KlineSequenceTransport` como candle em formação). Antes ela terminava
    cinco minutos atrás e começava ~50 min atrás, o que fazia dela uma
    série genuinamente velha -- e a barreira de frescor recusava, com
    razão, o sinal que estes testes precisam aprovar.

    Consequência que dita o tamanho da rampa: o cruzamento acontece no
    índice 32 da sequência (fast=9/slow=21 sobre 25 candles de queda),
    então CADA candle de rampa depois dele empurra o cruzamento um minuto
    mais para o passado. Com `n_up=10` o sinal nasce entre 2 e 3 minutos
    atrás -- dentro da janela real de 300 s, sem afrouxar nada. Uma rampa
    de 20 candles colocaria o cruzamento a ~12 min do presente, e nenhuma
    ancoragem consertaria isso: numa série de 1 minuto, ter N candles
    depois do cruzamento significa, literalmente, N minutos de idade."""
    start = (
        datetime.now(timezone.utc).replace(second=0, microsecond=0)
        - timedelta(minutes=n_down + n_up)
    )
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
        # A gentle rally slope keeps ATR% (true range relative to price)
        # under the strategy's 5% volatility ceiling all the way through
        # the crossover -- a steep rally previously pushed ATR% just over
        # that ceiling right at the crossover candle, silently forcing HOLD.
        rows.append([
            str(int(open_time.timestamp() * 1000)),
            f"{price - 2:.2f}", f"{price + 1:.2f}", f"{price - 3:.2f}", f"{price:.2f}", "10", "0",
        ])
    return rows


class _KlineSequenceTransport:
    """Correction v1.3 #2: serves REAL Bybit response shape/ordering -- each
    call returns several rows, newest-first, with the newest row being a
    still-forming "current" candle (rejected by the provider's closed-check)
    followed by a window of already-closed historical candles in descending
    order. This is what forced the fix: with limit=1 the still-forming row
    alone used to be the only thing ever returned. Delegates order create/
    status/position calls to the real fake -- so order submission is
    genuinely exercised, not mocked."""

    def __init__(self, base: FakeBybitTransport, rows: list[list[str]]):
        self._base = base
        self._rows = rows
        self._idx = 0
        self.get_calls = base.get_calls
        self.post_calls = base.post_calls

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
            newest_first = [forming_row] + list(reversed(historical))
            return {"result": {"list": newest_first}}
        if url.endswith("/v5/market/time"):
            # Keeps BybitServerTimeProvider in sync so the clock-drift gate
            # doesn't itself block the order this test is trying to reach.
            return {"result": {"timeSecond": str(int(datetime.now(timezone.utc).timestamp()))}}
        return self._base.http_get(url, params)

    def http_post(self, url: str, payload: dict) -> dict:
        resp = self._base.http_post(url, payload)
        # Auto-confirm any newly created order as Filled, at the price of
        # the most recently served candle, so the wiring test doesn't need
        # to predict order IDs to pre-queue a status.
        order_id = resp.get("result", {}).get("orderId")
        if url.endswith("/v5/order/create") and order_id:
            last_close = self._rows[self._idx - 1][4] if self._idx > 0 else "100"
            qty = payload.get("qty", "0")
            self._base.queue_status(order_id, [
                {"orderStatus": "Filled", "cumExecQty": qty, "avgPrice": last_close, "cumExecFee": "0.01"},
            ])
            self._base.queue_executions(order_id, [
                {"execId": f"{order_id}-EXEC-1", "execQty": qty, "execPrice": last_close, "execFee": "0.01"},
            ])
        return resp

    def queue_status(self, order_id: str, rows: list[dict]) -> None:
        self._base.queue_status(order_id, rows)


def test_bybit_demo_pipeline_reaches_execution_engine_with_zero_network():
    """Drives the pipeline through Orchestrator.tick() end to end (market
    data -> strategy -> risk -> BybitDemoExecutionEngine.submit()) using only
    the fake transport, with a candle sequence that deterministically
    produces a BUY signal. Proves the full BYBIT_DEMO wiring actually
    reaches execution -- not just that it's reachable in principle."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0,
    )
    base_transport = FakeBybitTransport()
    rows = _generate_kline_rows(n_down=25, n_up=10)
    transport = _KlineSequenceTransport(base_transport, rows)

    orch = build_orchestrator(settings, bybit_transport=transport)
    from tests.factories import activate_operational_state

    activate_operational_state(orch)  # Fase 2, item 7.8: entries require explicit activation

    results = []
    for _ in range(len(rows) + 2):
        result = orch.tick()
        results.append(result)
        if result["status"] == "order_filled":
            break

    assert results[-1]["status"] == "order_filled", f"never filled an order: {results}"
    assert any(call[0].endswith("/v5/order/create") for call in base_transport.post_calls), (
        "BybitDemoExecutionEngine.submit() never actually called order create"
    )

    order_id = results[-1]["order_id"]
    with orch.session_factory() as session:
        from app.persistence.models import Execution, Order, RiskEvaluation, StrategySignal

        order = session.get(Order, order_id)
        assert order is not None
        assert order.status in ("FILLED", "PARTIALLY_FILLED")
        assert order.exchange_order_id

        risk_eval = session.get(RiskEvaluation, order.risk_evaluation_id)
        assert risk_eval is not None
        assert risk_eval.approved is True

        signal = session.get(StrategySignal, risk_eval.signal_id)
        assert signal is not None
        assert signal.direction in ("BUY", "SELL")

        executions = session.query(Execution).filter_by(order_id=order.id).all()
        assert len(executions) >= 1
        assert executions[0].fill_qty > 0

    with session_scope(orch.session_factory) as session:
        open_positions = repo.open_positions(session)
        assert len(open_positions) == 1
