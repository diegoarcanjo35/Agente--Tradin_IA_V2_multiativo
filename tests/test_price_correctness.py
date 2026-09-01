"""Correction v1.1 #4: the Paper Execution Engine must fill using the price
of the candle that produced the decision, not a hardcoded/stale value. Drives
Orchestrator.tick() over two candles with very different prices and checks
each resulting fill against its own candle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.market_data.base import CandleFetchResult, CandleFetchStatus, CandleTick
from app.orchestrator import Orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Execution
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from app.strategy.engine import StrategyConfig, StrategyEngine
from sqlalchemy import select

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


class ListMarketDataProvider:
    """Minimal in-test double implementing the MarketDataProvider protocol."""

    def __init__(self, candles: list[CandleTick]):
        self._candles = candles
        self._cursor = 0

    def next_candle(self) -> CandleFetchResult:
        if self._cursor >= len(self._candles):
            return CandleFetchResult(status=CandleFetchStatus.REPLAY_FINISHED)
        c = self._candles[self._cursor]
        self._cursor += 1
        return CandleFetchResult(status=CandleFetchStatus.CANDLE_AVAILABLE, candle=c)

    def is_stale(self, max_staleness_seconds: float) -> bool:
        return False


def make_candle(i: int, close: float, symbol="BTCUSDT") -> CandleTick:
    t = T0 + timedelta(minutes=i)
    return CandleTick(
        symbol=symbol, timeframe="1m", open_time=t, open=close, high=close + 1,
        low=close - 1, close=close, volume=10.0, source="test", received_at=t,
    )


def build_test_orchestrator(session_factory, candles):
    # Fase 3.2: pipeline candle-a-candle (ver nota em
    # tests/test_bybit_demo_wiring.py::make_bybit_demo_settings).
    settings = Settings(mode=RunMode.REPLAY, strategy_timeframe_minutes=1)
    market_data_provider = ListMarketDataProvider(candles)
    cfg = StrategyConfig(fast_period=2, slow_period=4, atr_period=2,
                          min_atr_pct_of_price=0.0, max_atr_pct_of_price=1.0)
    strategy_engine = StrategyEngine(symbol=settings.symbol, config=cfg)
    risk_engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0,
                                         require_stop_loss=False))
    price_state: dict[str, float] = {}
    execution_engine = PaperLocalExecutionEngine(
        price_provider=lambda s: price_state.get(s, 0.0), slippage_bps=0.0
    )
    ai_agent = AIShadowAgent(provider=SimulatedProvider(), timeout_seconds=2.0, enabled=False)
    return Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=strategy_engine,
        risk_engine=risk_engine, execution_engine=execution_engine, ai_agent=ai_agent,
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )


def test_fill_price_tracks_the_originating_candle_not_a_fixed_value(session_factory):
    # Downtrend to arm the crossover, then a big jump up to trigger BUY at a
    # LOW price, then a big drop to trigger the opposing SELL/close at a very
    # different (high-to-low) price.
    prices = [100, 99, 98, 97, 96, 500, 5]
    candles = [make_candle(i, p) for i, p in enumerate(prices)]

    orch = build_test_orchestrator(session_factory, candles)
    from tests.factories import activate_operational_state

    activate_operational_state(orch)  # Fase 2, item 7.8: entries require explicit activation

    results = []
    for _ in range(len(candles) + 1):
        r = orch.tick()
        results.append(r)
        if r["status"] == "no_data":
            break

    filled = [r for r in results if r["status"] == "order_filled"]
    closed = [r for r in results if r["status"] in ("position_closed", "position_reduced")]
    assert filled, f"expected at least one fill, got {results}"

    with session_scope(session_factory) as session:
        executions = session.execute(select(Execution)).scalars().all()
        fill_prices = sorted(e.fill_price for e in executions)

    # The opening fill must be near the 500 candle (the BUY trigger price) --
    # never a hardcoded 40000.0-style constant regardless of which candle
    # drove it -- and if a stop-loss/close happened afterwards, its fill
    # price must be meaningfully different from the entry, proving each fill
    # tracks its own triggering price rather than reusing a stale one.
    assert all(p < 40000.0 for p in fill_prices)
    assert any(400.0 <= p <= 600.0 for p in fill_prices)
    if closed:
        assert len(fill_prices) >= 2
        assert max(fill_prices) - min(fill_prices) > 50.0


def test_two_far_apart_candles_produce_two_different_correct_fill_prices(session_factory):
    """Directly submits two opening-style orders back to back (bypassing the
    strategy) at very different reference prices and checks each fill uses
    its own candle's price, not the other one's or a stale default."""
    price_state: dict[str, float] = {}
    engine = PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0), slippage_bps=0.0)

    from app.execution.idempotency import make_idempotency_key
    from tests.factories import approved_open_order

    o1 = approved_open_order(
        symbol="BTCUSDT", side="BUY", qty=0.01, price=100.0,
        stop_loss=None, take_profit=None, signal_id=1,
    )
    ack1 = engine.submit(o1, make_idempotency_key(o1, "b1"), reference_price=100.0)
    fill1 = engine.poll_order(ack1.exchange_order_id).fills[0]

    o2 = approved_open_order(
        symbol="BTCUSDT", side="BUY", qty=0.01, price=50000.0,
        stop_loss=None, take_profit=None, signal_id=2,
    )
    ack2 = engine.submit(o2, make_idempotency_key(o2, "b2"), reference_price=50000.0)
    fill2 = engine.poll_order(ack2.exchange_order_id).fills[0]

    assert fill1.fill_price == pytest.approx(100.0)
    assert fill2.fill_price == pytest.approx(50000.0)
    assert fill1.fill_price != fill2.fill_price
