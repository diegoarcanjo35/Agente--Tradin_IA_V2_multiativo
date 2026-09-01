"""Thin typed repository helpers over a SQLAlchemy Session. Kept intentionally
simple (no repository-pattern abstraction beyond this) since the app is small
enough that a query-builder layer would be premature.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.execution.order_state import NON_TERMINAL_STATUSES, OrderStatus, validate_transition
from app.persistence.models import (
    AccountSnapshot,
    AIRecommendation,
    Candle,
    FailureReconciliation,
    FundingCollectionCheckpoint,
    FundingEvent,
    Order,
    OrderEvent,
    Position,
    RiskEvaluation,
    SecurityEvent,
    StrategySignal,
    SystemState,
)


def get_or_create_system_state(session: Session) -> SystemState:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    return state


def recompute_trading_blocked(state: SystemState, max_api_failures: int) -> None:
    """Correction v1.2 #5: TRADING_BLOCKED is derived from the individual
    block sources (kill switch, ambiguous/divergent reconciliation state,
    clock out of sync, API failure count over the limit), never a single
    flag any one of them can clobber. `block_reason` lists every active
    reason in Portuguese so disengaging the kill switch can never silently
    clear a block caused by something else."""
    reasons: list[str] = []
    if state.kill_switch_engaged:
        reasons.append("bloqueio de emergência ativado manualmente")
    if state.state_ambiguous:
        reasons.append("reconciliação divergente ou estado ambíguo em relação à corretora")
    if state.clock_out_of_sync:
        reasons.append("relógio local fora de sincronia com a referência")
    if state.api_failure_count >= max_api_failures:
        reasons.append(
            f"limite de falhas consecutivas de API atingido ({state.api_failure_count}/{max_api_failures})"
        )
    # Fase 2, item 7.5: each new cause is independent -- clearing one (e.g.
    # a fresh reconciliation succeeding) never clears any of the others.
    # `reconciliation_stale` is deliberately NOT included here: item 7.4
    # requires staleness to block only NEW openings, never closes/reductions
    # -- unlike every other cause above (which correctly blocks both, same
    # as the pre-existing kill_switch/state_ambiguous/clock/api_failure
    # behavior). It is instead checked as an entry-only gate inside
    # RiskEngine.evaluate() via RiskContext.reconciliation_stale.
    if state.reconciliation_diverged:
        reasons.append("reconciliação periódica detectou divergência entre estado local e da corretora")
    if state.order_state_unknown:
        reasons.append("existe ordem em estado UNKNOWN -- não é seguro liberar nova exposição")
    # `initialization_not_reconciled` is deliberately NOT one of the reasons
    # here -- item 7.8 wants "process is running" (trading_blocked, which
    # also affects closes) kept separate from "strategy is authorized to
    # open new entries" (operational_state). Whether initialization has
    # reconciled is instead one of the required gates checked by
    # POST /operational-state/activate (app/api/routes_control.py) before
    # operational_state may ever reach ATIVO, and is cleared automatically
    # by Orchestrator.reconcile() the first time it actually completes.
    state.trading_blocked = bool(reasons)
    state.block_reason = "; ".join(reasons) if reasons else None

    # Fase 2, item 7.8: BLOQUEADO always mirrors trading_blocked. Recovering
    # from a block never auto-restores ATIVO (or even OBSERVANDO) by
    # itself -- it only reverts to OBSERVANDO, so opening new entries again
    # still requires the operator to explicitly re-activate (item 7.8:
    # "ativação de novas entradas exige ação explícita do operador").
    # ENCERRANDO is left untouched -- a graceful shutdown in progress is
    # never overwritten back to BLOQUEADO/OBSERVANDO by this recompute.
    if state.operational_state != "ENCERRANDO":
        if state.trading_blocked:
            state.operational_state = "BLOQUEADO"
        elif state.operational_state == "BLOQUEADO":
            state.operational_state = "OBSERVANDO"


def record_security_event(session: Session, event_type: str, detail: str) -> SecurityEvent:
    ev = SecurityEvent(event_type=event_type, detail=detail)
    session.add(ev)
    session.flush()
    return ev


def record_failure(
    session: Session, kind: str, detail: str, resolved: bool = False,
    mismatches: list[str] | None = None, order_id: int | None = None, session_id: int | None = None,
) -> FailureReconciliation:
    """Correção v1.1 #3: `mismatches`, when given, is persisted as a
    structured JSON array alongside the Portuguese `detail` summary -- so
    a reconciliation result can be inspected programmatically, not just
    read as a paragraph."""
    fr = FailureReconciliation(
        kind=kind, detail=detail, resolved=resolved,
        mismatches_json=json.dumps(mismatches) if mismatches is not None else None,
        order_id=order_id, session_id=session_id,
    )
    session.add(fr)
    session.flush()
    return fr


def save_candle(session: Session, symbol: str, timeframe: str, open_time: datetime,
                 open_: float, high: float, low: float, close: float, volume: float,
                 source: str) -> Candle | None:
    """Returns None (never raises) if this exact symbol+timeframe+open_time
    was already persisted -- the unique constraint on `candles` is the last
    line of defense against duplicate processing (correction v1.2 #2),
    enforced via a SAVEPOINT so a concurrent duplicate never poisons the
    whole session/transaction."""
    c = Candle(
        symbol=symbol, timeframe=timeframe, open_time=open_time,
        open=open_, high=high, low=low, close=close, volume=volume, source=source,
    )
    try:
        with session.begin_nested():
            session.add(c)
            session.flush()
    except IntegrityError:
        return None
    return c


def get_last_candle_open_time(session: Session, symbol: str, timeframe: str) -> datetime | None:
    """Correction v1.4 #2: the persistent cursor for backlog draining --
    the last candle actually committed for this symbol+timeframe. Backed by
    the `candles` table itself (already the source of truth, already
    indexed) rather than a separate cursor table, so a fresh
    BybitDemoMarketDataProvider instance (e.g. after a process restart) can
    call `sync_cursor()` with this value and resume exactly where it left
    off."""
    row = session.execute(
        select(Candle.open_time)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    # SQLite's DATETIME column round-trips as a naive datetime regardless of
    # what was stored -- every timestamp in this app is UTC by convention
    # (app.core.clock.utcnow()), so re-attach that explicitly rather than
    # ever comparing naive vs. aware datetimes.
    return row if row.tzinfo is not None else row.replace(tzinfo=timezone.utc)


def save_signal(session: Session, symbol: str, direction: str, justification: str,
                 observed_price: float, atr: float, params: dict) -> StrategySignal:
    s = StrategySignal(
        symbol=symbol, direction=direction, justification=justification,
        observed_price=observed_price, atr=atr, params_json=json.dumps(params),
    )
    session.add(s)
    session.flush()
    return s


def save_ai_recommendation(session: Session, symbol: str, signal_id: int | None,
                            recommendation: str, confidence: float, reasoning_summary: str,
                            risk_flags: list[str], provider: str, model_version: str,
                            is_valid: bool, rejection_reason: str | None) -> AIRecommendation:
    rec = AIRecommendation(
        symbol=symbol, signal_id=signal_id, recommendation=recommendation,
        confidence=confidence, reasoning_summary=reasoning_summary,
        risk_flags_json=json.dumps(risk_flags), provider=provider,
        model_version=model_version, is_valid=is_valid, rejection_reason=rejection_reason,
    )
    session.add(rec)
    session.flush()
    return rec


def save_risk_evaluation(session: Session, signal_id: int, approved: bool, reason: str,
                          checks: dict) -> RiskEvaluation:
    ev = RiskEvaluation(
        signal_id=signal_id, approved=approved, reason=reason, checks_json=json.dumps(checks),
    )
    session.add(ev)
    session.flush()
    return ev


def find_order_by_idempotency_key(session: Session, key: str) -> Order | None:
    return session.execute(select(Order).where(Order.idempotency_key == key)).scalar_one_or_none()


def non_terminal_orders(session: Session, mode: str | None = None) -> list[Order]:
    """Fase 2, item 7.3: orders still open on the exchange (or in an
    unresolved state) -- what the kill switch must attempt to cancel
    before considering the system stabilized."""
    non_terminal_values = [s.value for s in NON_TERMINAL_STATUSES]
    stmt = select(Order).where(Order.status.in_(non_terminal_values))
    if mode is not None:
        stmt = stmt.where(Order.mode == mode)
    return list(session.execute(stmt).scalars().all())


def filled_orders(session: Session, symbol: str | None = None) -> list[Order]:
    """Fase 2, item 7.6: orders that actually received at least one fill --
    what cost/slippage metrics (app.metrics.engine.compute_cost_metrics)
    are computed over."""
    filled_values = [OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value]
    stmt = select(Order).where(Order.status.in_(filled_values))
    if symbol is not None:
        stmt = stmt.where(Order.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def has_unknown_orders(session: Session) -> bool:
    """Fase 2, item 7.2/7.5: whether any order currently sits in UNKNOWN --
    the SystemState.order_state_unknown block-cause flag is always
    re-derived from this query (never toggled ad hoc), so it self-heals the
    moment every UNKNOWN order is resolved (manually or by reconciliation),
    exactly like `recompute_trading_blocked` re-derives `trading_blocked`
    from its sources instead of trusting a stale boolean."""
    return session.execute(
        select(Order.id).where(Order.status == OrderStatus.UNKNOWN.value).limit(1)
    ).first() is not None


def save_order(session: Session, idempotency_key: str, risk_evaluation_id: int, symbol: str,
               side: str, qty: float, stop_loss: float | None, take_profit: float | None,
               mode: str, is_close: bool = False, reference_price: float | None = None) -> Order:
    o = Order(
        idempotency_key=idempotency_key, risk_evaluation_id=risk_evaluation_id,
        symbol=symbol, side=side, qty=qty, stop_loss=stop_loss, take_profit=take_profit,
        mode=mode, status=OrderStatus.PENDING_SUBMIT.value, is_close=is_close,
        reference_price=reference_price,
    )
    session.add(o)
    session.flush()
    return o


def transition_order_status(
    session: Session, order: Order, new_status: OrderStatus, detail: str | None = None,
) -> None:
    """Fase 2, item 7.2: the ONLY sanctioned way to change `Order.status`.
    Validates the transition against `app.execution.order_state`'s explicit
    table (raises IllegalOrderTransitionError, never silently coerces) and
    writes an `order_events` audit row -- every jump an order ever makes is
    reconstructable after the fact, not just its current status."""
    current = OrderStatus(order.status)
    validate_transition(current, new_status)
    session.add(OrderEvent(
        order_id=order.id, from_status=current.value, to_status=new_status.value, detail=detail,
    ))
    order.status = new_status.value
    session.flush()


def open_position(session: Session, symbol: str, side: str, qty: float,
                   avg_entry_price: float, stop_loss: float | None, take_profit: float | None,
                   opening_fee: float = 0.0) -> Position:
    p = Position(symbol=symbol, side=side, qty=qty, avg_entry_price=avg_entry_price,
                 stop_loss=stop_loss, take_profit=take_profit, status="OPEN",
                 fees_paid=opening_fee)
    session.add(p)
    session.flush()
    return p


def add_to_position(session: Session, position: Position, additional_qty: float,
                     fill_price: float, fee: float) -> None:
    """Same-side fill: increases qty and recomputes the weighted average
    entry price. Fee is accumulated, never overwritten (Fase 1 correction 5:
    commissions must reflect every execution across the position's life)."""
    total_qty = position.qty + additional_qty
    position.avg_entry_price = (
        position.avg_entry_price * position.qty + fill_price * additional_qty
    ) / total_qty
    position.qty = total_qty
    position.fees_paid += fee
    session.flush()


def close_position(session: Session, position: Position, realized_pnl_delta: float,
                    closing_fee: float) -> None:
    """Fully closes the position. `realized_pnl_delta` is the P&L from this
    closing fill only; it is added to any P&L already realized from prior
    partial closes on this position. `closing_fee` is accumulated onto
    fees_paid (which already holds the opening fee and any partial-fill
    fees), never overwritten."""
    position.status = "CLOSED"
    position.realized_pnl += realized_pnl_delta
    position.fees_paid += closing_fee
    position.closed_at = utcnow()
    session.flush()


def reduce_position(session: Session, position: Position, reduce_qty: float,
                     realized_pnl_delta: float, fee: float) -> None:
    """Partial close: reduces qty and accumulates realized P&L/fees without
    closing the position."""
    position.qty -= reduce_qty
    position.realized_pnl += realized_pnl_delta
    position.fees_paid += fee
    session.flush()


def open_positions(session: Session, symbol: str | None = None) -> list[Position]:
    stmt = select(Position).where(Position.status == "OPEN")
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def closed_positions(session: Session, symbol: str | None = None) -> list[Position]:
    stmt = select(Position).where(Position.status == "CLOSED").order_by(Position.closed_at)
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def last_funding_occurred_at(session: Session, symbol: str) -> datetime | None:
    """Informational only (e.g. a "última coleta em X" display) -- NEVER
    used to drive funding-collection retomada since correção v1.3 #1/#3:
    the MAX `occurred_at` of already-persisted events is not proof of
    coverage (a newest-first paginated page can persist a recent record
    while an older page in the SAME window still failed). See
    `get_funding_checkpoint`/`advance_funding_checkpoint` for the real
    coverage mechanism."""
    row = session.execute(
        select(FundingEvent.occurred_at)
        .where(FundingEvent.symbol == symbol)
        .order_by(FundingEvent.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return row if row.tzinfo is not None else row.replace(tzinfo=timezone.utc)


def funding_total(session: Session, symbol: str | None = None) -> float:
    """Correção v1.1 #6: the real SUM of collected funding -- 0.0 is a
    genuine, correct total when no funding has settled yet (never confused
    with UNAVAILABLE, which app.metrics.engine reports only when there is
    no funding_provider at all to have collected anything with)."""
    stmt = select(FundingEvent.amount)
    if symbol:
        stmt = stmt.where(FundingEvent.symbol == symbol)
    return sum(session.execute(stmt).scalars().all())


def get_funding_checkpoint(session: Session, symbol: str) -> FundingCollectionCheckpoint | None:
    """Correção v1.3 #1: the explicit, persisted proof of funding-collection
    coverage for `symbol` -- `None` means nothing has ever been fully
    covered yet (the caller anchors the first window at `now -
    FUNDING_WINDOW_SECONDS` in that case, never at an unbounded past)."""
    row = session.execute(
        select(FundingCollectionCheckpoint).where(FundingCollectionCheckpoint.symbol == symbol)
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.covered_until.tzinfo is None:
        row.covered_until = row.covered_until.replace(tzinfo=timezone.utc)
    return row


def advance_funding_checkpoint(session: Session, symbol: str, covered_until: datetime) -> FundingCollectionCheckpoint:
    """Correção v1.3 #1: only ever called by the caller once an ENTIRE
    `[since, covered_until]` window was walked to completion (every page
    fetched, every row valid) -- never advances on a partial/incomplete
    window, and never moves backwards even if called with an earlier value
    than what is already recorded (defensive -- the caller should never do
    this, but the checkpoint's only job is to be a safe lower bound on what
    is truly covered)."""
    existing = session.execute(
        select(FundingCollectionCheckpoint).where(FundingCollectionCheckpoint.symbol == symbol)
    ).scalar_one_or_none()
    if existing is None:
        existing = FundingCollectionCheckpoint(symbol=symbol, covered_until=covered_until)
        session.add(existing)
    else:
        current = existing.covered_until
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if covered_until > current:
            existing.covered_until = covered_until
    session.flush()
    return existing


def save_account_snapshot(session: Session, balance: float, equity: float,
                           unrealized_pnl: float, mode: str) -> AccountSnapshot:
    snap = AccountSnapshot(balance=balance, equity=equity, unrealized_pnl=unrealized_pnl, mode=mode)
    session.add(snap)
    session.flush()
    return snap


def latest_account_snapshot(session: Session) -> AccountSnapshot | None:
    stmt = select(AccountSnapshot).order_by(AccountSnapshot.taken_at.desc()).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def recent_signals(session: Session, limit: int = 50, symbol: str | None = None) -> list[StrategySignal]:
    stmt = select(StrategySignal).order_by(StrategySignal.created_at.desc()).limit(limit)
    if symbol is not None:
        stmt = stmt.where(StrategySignal.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def recent_ai_recommendations(
    session: Session, limit: int = 50, symbol: str | None = None
) -> list[AIRecommendation]:
    stmt = select(AIRecommendation).order_by(AIRecommendation.created_at.desc()).limit(limit)
    if symbol is not None:
        stmt = stmt.where(AIRecommendation.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def recent_risk_evaluations(session: Session, limit: int = 50) -> list[RiskEvaluation]:
    stmt = select(RiskEvaluation).order_by(RiskEvaluation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_security_events(session: Session, limit: int = 50) -> list[SecurityEvent]:
    stmt = select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_failures(session: Session, limit: int = 50) -> list[FailureReconciliation]:
    stmt = select(FailureReconciliation).order_by(FailureReconciliation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
