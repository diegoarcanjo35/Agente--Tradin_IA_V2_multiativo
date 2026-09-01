"""Fase 3 multiativo, itens 9.6/9.8/9.9/9.10/9.11 do brief: gate de
ativação global, exposição/posições globais sob sinais simultâneos,
idempotência entre símbolos, métricas por símbolo somando ao consolidado, e
shutdown sem writes tardios -- sem rede.
"""
from __future__ import annotations

import pytest

from app.execution.idempotency import make_idempotency_key
from app.metrics.engine import ClosedTrade, compute_metrics
from app.orchestrator import MultiSymbolOrchestrator
from app.risk.config import RiskLimits
from app.risk.engine import RiskEngine
from tests.factories import approved_open_order, base_risk_context


# --- item 6: ativação recusada enquanto qualquer símbolo não está saudável --

def test_engine_degraded_blocks_new_entries_but_never_closes():
    """`engine_degraded` is exactly the flag `MultiSymbolOrchestrator`
    pushes into every underlying Orchestrator whenever the portfolio isn't
    fully healthy (see `_sync_engine_degraded_to_orchestrators`) -- this
    confirms the RiskEngine gate it feeds already behaves as required:
    blocks entries, never blocks closes."""
    engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0))
    from app.strategy.schemas import Signal
    from tests.factories import NOW

    signal = Signal(
        symbol="BTCUSDT", direction="BUY", justification="teste", created_at=NOW,
        observed_price=100.0, atr=1.0, stop_loss=95.0, take_profit=110.0, params={},
    )
    ctx = base_risk_context(engine_degraded=True)
    result = engine.evaluate(signal, signal_id=1, context=ctx)
    assert not result.approved

    close_result = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=0.01,
        position_exists=True, position_qty=0.01, position_side="BUY", context=ctx,
    )
    assert close_result.approved


def test_multi_symbol_orchestrator_gate_requires_all_symbols_healthy():
    """One symbol unhealthy propagates `engine_degraded=True` to EVERY
    underlying Orchestrator (portfolio-wide activation gate), not just the
    unhealthy one."""
    orchestrators = {}

    class _Stub:
        def __init__(self):
            self.engine_degraded = False

    for s in ("BTCUSDT", "ETHUSDT"):
        orchestrators[s] = _Stub()
    multi = MultiSymbolOrchestrator.__new__(MultiSymbolOrchestrator)
    multi.orchestrators = orchestrators
    multi.symbols = ["BTCUSDT", "ETHUSDT"]
    multi._engine_degraded = False
    from app.orchestrator import SymbolHealth

    multi.health = {"BTCUSDT": SymbolHealth(status="SAUDAVEL"), "ETHUSDT": SymbolHealth(status="DEGRADADO")}

    multi._sync_engine_degraded_to_orchestrators()
    assert orchestrators["BTCUSDT"].engine_degraded is True
    assert orchestrators["ETHUSDT"].engine_degraded is True

    multi.health["ETHUSDT"] = SymbolHealth(status="SAUDAVEL")
    multi._sync_engine_degraded_to_orchestrators()
    assert orchestrators["BTCUSDT"].engine_degraded is False
    assert orchestrators["ETHUSDT"].engine_degraded is False


# --- item 8: limites de exposição/posições globais sob sinais simultâneos --

def test_global_exposure_limit_rejects_second_symbol_once_first_consumed_the_whole_cap():
    """RISK_MAX_TOTAL_EXPOSURE_USD is a single global RiskLimits field --
    already computed by the caller (Orchestrator.tick()) across ALL open
    positions regardless of symbol (repo.open_positions(session), no
    symbol filter). This confirms the RiskEngine side of that contract:
    once the FIRST symbol's fill has consumed the entire global exposure
    budget, a second signal for a DIFFERENT symbol is rejected outright
    (never silently sized down to a residual sliver from a different
    market) -- `remaining_exposure = max_total_exposure_usd -
    open_exposure_usd` must be strictly positive to approve at all."""
    engine = RiskEngine(RiskLimits(max_position_usd=1000.0, max_total_exposure_usd=150.0))
    from app.strategy.schemas import Signal
    from tests.factories import NOW

    btc_signal = Signal(
        symbol="BTCUSDT", direction="BUY", justification="t", created_at=NOW,
        observed_price=100.0, atr=1.0, stop_loss=95.0, take_profit=110.0, params={},
    )
    eth_signal = Signal(
        symbol="ETHUSDT", direction="BUY", justification="t", created_at=NOW,
        observed_price=100.0, atr=1.0, stop_loss=95.0, take_profit=110.0, params={},
    )

    # First signal: 0 exposure so far -> approved (sized to the cap, since
    # max_position_usd=1000 > the 150 remaining).
    ctx1 = base_risk_context(open_exposure_usd=0.0)
    result1 = engine.evaluate(btc_signal, signal_id=1, context=ctx1)
    assert result1.approved
    assert result1.approved_order.qty * 100.0 == pytest.approx(150.0)

    # Second signal (different symbol): exposure is already AT the global
    # cap (from the first fill) -- rejected outright, not sized down.
    ctx2 = base_risk_context(open_exposure_usd=150.0)
    result2 = engine.evaluate(eth_signal, signal_id=2, context=ctx2)
    assert not result2.approved
    assert result2.approved_order is None
    assert "exposi" in result2.reason.lower() or "exposure" in result2.reason.lower()


# --- item 9: idempotência de ordens/fills entre símbolos ---------------------

def test_idempotency_key_differs_across_symbols_for_otherwise_identical_orders():
    btc = approved_open_order(symbol="BTCUSDT", side="BUY", qty=0.001, price=100.0, signal_id=1)
    eth = approved_open_order(symbol="ETHUSDT", side="BUY", qty=0.001, price=100.0, signal_id=1)
    bucket = "20240101T1200"
    assert make_idempotency_key(btc, bucket) != make_idempotency_key(eth, bucket)


# --- item 10: métricas por símbolo somam exatamente ao consolidado ----------

def test_per_symbol_metrics_sum_to_consolidated():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    btc_trades = [
        ClosedTrade(realized_pnl=10.0, fees_paid=0.1, opened_at=t0, closed_at=t0 + timedelta(minutes=1)),
        ClosedTrade(realized_pnl=-4.0, fees_paid=0.1, opened_at=t0, closed_at=t0 + timedelta(minutes=2)),
    ]
    eth_trades = [
        ClosedTrade(realized_pnl=6.0, fees_paid=0.05, opened_at=t0, closed_at=t0 + timedelta(minutes=3)),
    ]
    consolidated = compute_metrics(btc_trades + eth_trades, starting_balance=1000.0)
    btc_result = compute_metrics(btc_trades, starting_balance=1000.0)
    eth_result = compute_metrics(eth_trades, starting_balance=1000.0)

    assert consolidated.gross_profit == btc_result.gross_profit + eth_result.gross_profit
    assert consolidated.gross_loss == btc_result.gross_loss + eth_result.gross_loss
    assert consolidated.closed_trades_count == btc_result.closed_trades_count + eth_result.closed_trades_count
    assert abs(consolidated.net_profit - (btc_result.net_profit + eth_result.net_profit)) < 1e-9


# --- item 11: shutdown sem writes tardios em nenhum símbolo -----------------

def test_mark_shutting_down_sets_encerrando_for_every_symbol():
    orchestrators = {}

    class _Stub:
        engine_degraded = False

    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        orchestrators[s] = _Stub()
    multi = MultiSymbolOrchestrator.__new__(MultiSymbolOrchestrator)
    multi.orchestrators = orchestrators
    multi.symbols = list(orchestrators)
    from app.orchestrator import SymbolHealth

    multi.health = {s: SymbolHealth(status="SAUDAVEL") for s in orchestrators}

    multi.mark_shutting_down()
    assert all(h.status == "ENCERRANDO" for h in multi.health.values())
