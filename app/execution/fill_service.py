"""Correção v1.1 #1/#2/#4: the SINGLE place any fill is ever applied to an
order and a position. Used by every caller that can learn about a fill --
the immediate post-submit poll, the periodic open-order poller, a
kill-switch cancellation race, and reconciliation -- so there is exactly
one code path, never a second one that applies things differently (the
audited gap in v1.0: the kill switch called `repo.record_fill` directly and
never touched `Execution`/`Position`/session counters at all).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.execution import fill_ledger
from app.execution.base import OrderStatusSnapshot
from app.execution.order_state import IllegalOrderTransitionError, OrderStatus, is_terminal
from app.persistence import repo
from app.persistence.models import Order, OperationalSession, SystemState
from app.sessions import increment as increment_session_counter


@dataclass(frozen=True)
class FillApplicationResult:
    status: OrderStatus
    new_fill_count: int
    realized_pnl_delta_total: float = 0.0
    closed_fully: bool | None = None  # None when is_close=False or no fill applied yet



def _frozen_protection_distances(session, order: Order) -> tuple[float, float] | None:
    """As distâncias de stop e alvo POR UNIDADE congeladas no snapshot da
    decisão (`StrategySignal.params_json`, gravado por
    `app/orchestrator.py` no momento em que o sinal nasceu).

    Devolve `None` -- e a proteção fica exatamente como o `Order` a
    trouxe, o comportamento anterior a esta fase -- quando a ordem é
    legada (criada antes do snapshot existir) ou quando o snapshot não
    tem as duas distâncias. Nunca completa a lacuna lendo a configuração
    atual do processo: o `Settings` de hoje pode não ser o que definiu
    aquela proteção."""
    snapshot = repo.decision_snapshot_for_order(session, order)
    if not snapshot:
        return None
    stop = snapshot.get("stop_distance_per_unit")
    target = snapshot.get("target_distance_per_unit")
    if not isinstance(stop, (int, float)) or not isinstance(target, (int, float)):
        return None
    if not (stop > 0 and target > 0):
        return None
    return float(stop), float(target)


def _reanchor(session, position, protection: tuple[float, float] | None) -> None:
    """Reancora a proteção no preço médio real -- só para fills de
    AUMENTO. Um fill oposto BLOQUEADO (nunca aplicado à posição) e um fill
    de fechamento sem posição local jamais chegam aqui, então nunca criam
    nem alteram proteção."""
    if protection is None or position is None:
        return
    stop_distance, target_distance = protection
    repo.reanchor_position_protection(session, position, stop_distance, target_distance)


def apply_order_snapshot(
    session, state: SystemState, op_session: OperationalSession | None, order: Order,
    snapshot: OrderStatusSnapshot, is_close: bool, max_api_failures: int,
) -> FillApplicationResult:
    """Reconciles `order`'s persisted status/fills with `snapshot` (from
    `ExecutionEngine.poll_order()`):
    1. Correção v1.2 #1: a TERMINAL `snapshot.status` (FILLED/CANCELLED/
       REJECTED) is only ever persisted once `snapshot.fills_complete` is
       True -- the audited defect was exactly this: `poll_order()` could
       report `status=FILLED` with `fills=[]` after an execution-list
       timeout, `apply_order_snapshot` would still persist FILLED, and
       `repo.non_terminal_orders()` would then drop the order from the
       recoverable set forever, permanently losing its fills/position/fees.
       When `fills_complete=False`, `order.status` is deliberately left
       UNCHANGED (so it stays non-terminal and therefore stays selected by
       `repo.non_terminal_orders()` for the next poll) -- only
       `order.pending_exchange_status`/`order.fills_sync_status` record
       what the exchange reported, as an audit trail, never as the
       authoritative status. The eventual real transition (once a later
       poll proves `fills_complete=True`) applies the terminal status AND
       every gathered fill together, in this same function call/transaction
       -- there is no window where one is persisted without the other.
    2. Records any NEW fills via the idempotent ledger
       (`fill_ledger.record_new_fills` -- already-seen `exchange_fill_id`s
       are silently skipped) REGARDLESS of `fills_complete` -- fills
       already validated before an interruption are never held back or
       discarded, only the terminal status transition is deferred. Applies
       each new fill's DELTA to the position: opens/adds for an entry
       order, reduces/closes (with realized PnL) for a close order.
       Correção v1.2 #5: an entry fill (`is_close=False`) is NEVER summed
       onto a position on the OPPOSITE side -- a late/opposite fill is
       blocked (never fabricated into the wrong position) and flagged as a
       security event + ambiguous state, requiring reconciliation.
    3. Refreshes `SystemState.order_state_unknown` and recomputes
       `trading_blocked` accordingly.
    """
    current = OrderStatus(order.status)
    if is_terminal(snapshot.status) and not snapshot.fills_complete:
        # Correção v1.2 #1: never terminalize before the fill history is
        # proven complete -- record the exchange's claim for audit/
        # observability, but leave `order.status` exactly where it is.
        if snapshot.exchange_order_id and not order.exchange_order_id:
            order.exchange_order_id = snapshot.exchange_order_id
        order.pending_exchange_status = snapshot.status.value
        order.fills_sync_status = "PENDING"
    elif snapshot.status != current:
        if snapshot.exchange_order_id and not order.exchange_order_id:
            order.exchange_order_id = snapshot.exchange_order_id
        try:
            repo.transition_order_status(
                session, order, snapshot.status,
                detail=f"poll_order() reportou {snapshot.status.value}.",
            )
            if is_terminal(snapshot.status):
                order.fills_sync_status = "COMPLETE"
                order.pending_exchange_status = None
        except IllegalOrderTransitionError:
            # A snapshot arriving out of order (e.g. a stale poll racing a
            # newer one) is reported as UNKNOWN rather than silently
            # ignored or crashing the tick/poller loop.
            if not is_terminal(OrderStatus(order.status)):
                repo.transition_order_status(
                    session, order, OrderStatus.UNKNOWN,
                    detail="Transição de status inesperada durante poll_order().",
                )

    new_rows = fill_ledger.record_new_fills(session, order, snapshot.fills)
    realized_pnl_delta_total = 0.0
    closed_fully: bool | None = None

    if new_rows:
        increment_session_counter(op_session, "fills_count", by=len(new_rows))
        existing = repo.open_positions(session, order.symbol)
        position = existing[0] if existing else None
        # Fase 3.2 (item 9 da decisão do PO): distâncias de proteção
        # CONGELADAS na decisão, lidas UMA vez pela cadeia de FKs
        # Order -> RiskEvaluation -> StrategySignal. Nunca do `Settings`
        # atual do processo (que pode ter mudado desde a decisão), nunca
        # de um ATR recalculado agora.
        protection = _frozen_protection_distances(session, order)

        for row in new_rows:
            if not is_close:
                if position is None:
                    position = repo.open_position(
                        session, order.symbol, order.side, row.fill_qty, row.fill_price,
                        order.stop_loss, order.take_profit, opening_fee=row.fee,
                    )
                    _reanchor(session, position, protection)
                elif position.side != order.side:
                    # Correção v1.2 #5: a late/opposite fill -- e.g. the
                    # position already flipped/closed by the time this fill
                    # arrived. Never fabricate a state by summing onto the
                    # wrong side; block and require reconciliation instead.
                    state.state_ambiguous = True
                    detail = (
                        f"Fill de entrada ({order.side}) da ordem {order.id} recebido, mas a posição "
                        f"aberta em {order.symbol} está do lado {position.side} -- fill NÃO aplicado "
                        "à posição (bloqueio de segurança); requer reconciliação manual."
                    )
                    repo.record_security_event(session, "LATE_OPPOSITE_FILL_BLOCKED", detail)
                else:
                    repo.add_to_position(session, position, row.fill_qty, row.fill_price, row.fee)
                    # Fill de AUMENTO: `add_to_position` acabou de
                    # recalcular o preço médio ponderado, então a proteção
                    # é reancorada sobre ele. Repetido a cada novo fill de
                    # aumento -- stop/alvo acompanham o preço médio e
                    # podem se mover em qualquer direção entre fills
                    # parciais (não são monotônicos).
                    _reanchor(session, position, protection)
            else:
                if position is None:
                    # Nothing local to reduce/close -- reconciliation is what
                    # will flag this divergence; fill_service never fabricates
                    # a position to apply a close fill against.
                    continue
                direction = 1 if position.side == "BUY" else -1
                delta = direction * (row.fill_price - position.avg_entry_price) * row.fill_qty
                realized_pnl_delta_total += delta
                fully = row.fill_qty >= position.qty - 1e-9
                if fully:
                    repo.close_position(session, position, delta, row.fee)
                    closed_fully = True
                    position = None
                else:
                    repo.reduce_position(session, position, row.fill_qty, delta, row.fee)
                    closed_fully = False

    state.order_state_unknown = repo.has_unknown_orders(session)
    repo.recompute_trading_blocked(state, max_api_failures)

    return FillApplicationResult(
        status=OrderStatus(order.status), new_fill_count=len(new_rows),
        realized_pnl_delta_total=realized_pnl_delta_total, closed_fully=closed_fully,
    )
