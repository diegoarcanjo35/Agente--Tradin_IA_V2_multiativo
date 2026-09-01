"""Correction v1.2 #4: `RiskEngine.make_test_approved_order()` was removed
from production code -- it could mint a valid `ApprovedOrder` without going
through `evaluate()`/`evaluate_close()`, weakening exactly the boundary the
Risk Engine exists to enforce (any code importing it in-process could call
it). These helpers obtain a real `ApprovedOrder` the only legitimate way:
by actually running `RiskEngine.evaluate()` / `evaluate_close()` against a
valid signal and context.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.persistence import repo
from app.persistence.db import session_scope
from app.risk.config import RiskLimits
from app.risk.engine import ApprovedOrder, RiskContext, RiskEngine
from app.strategy.schemas import Signal


def activate_operational_state(orchestrator) -> None:
    """Fase 2, item 7.8: a freshly built Orchestrator always comes up in
    OBSERVANDO, never ATIVO -- new entries require explicit operator
    activation (POST /operational-state/activate in production). Tests
    that need entries to actually fill call this directly, bypassing the
    HTTP layer, exactly like they bypass it for every other white-box
    orchestrator check in this suite."""
    with session_scope(orchestrator.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.operational_state = "ATIVO"
        if state.active_session_id is not None:
            from app.persistence.models import OperationalSession

            op_session = session.get(OperationalSession, state.active_session_id)
            if op_session is not None:
                op_session.status = "ATIVO"

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def base_risk_context(**overrides) -> RiskContext:
    defaults = dict(
        open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        state_ambiguous=False, cooldown_until=None, now=NOW,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def approved_open_order(
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    qty: float = 0.001,
    price: float = 40000.0,
    stop_loss: float | None = 39000.0,
    take_profit: float | None = 41000.0,
    signal_id: int = 1,
) -> ApprovedOrder:
    """Real approval via RiskEngine.evaluate(), with limits sized so the
    computed position size matches `qty` exactly."""
    position_usd = qty * price
    engine = RiskEngine(RiskLimits(
        max_position_usd=position_usd, max_total_exposure_usd=position_usd,
        require_stop_loss=stop_loss is not None,
    ))
    signal = Signal(
        symbol=symbol, direction=side, justification="fixture de teste",
        created_at=NOW, observed_price=price, atr=100.0,
        source_candle_open_time=NOW,
        stop_loss=stop_loss, take_profit=take_profit, params={},
    )
    result = engine.evaluate(signal, signal_id=signal_id, context=base_risk_context())
    assert result.approved and result.approved_order is not None, result.reason
    return result.approved_order


def approved_close_order(
    symbol: str = "BTCUSDT",
    close_side: str = "SELL",
    qty: float = 0.001,
    position_qty: float = 0.001,
    position_side: str = "BUY",
    signal_id: int = 1,
) -> ApprovedOrder:
    """Real approval via RiskEngine.evaluate_close()."""
    engine = RiskEngine(RiskLimits())
    result = engine.evaluate_close(
        signal_id=signal_id, symbol=symbol, close_side=close_side, qty=qty,
        position_exists=True, position_qty=position_qty, position_side=position_side,
        context=base_risk_context(),
    )
    assert result.approved and result.approved_order is not None, result.reason
    return result.approved_order
