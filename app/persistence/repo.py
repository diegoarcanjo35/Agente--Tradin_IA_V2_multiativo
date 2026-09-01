"""Thin typed repository helpers over a SQLAlchemy Session. Kept intentionally
simple (no repository-pattern abstraction beyond this) since the app is small
enough that a query-builder layer would be premature.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.timeframe import canonical_timeframe, timeframe_aliases
from app.execution.order_state import NON_TERMINAL_STATUSES, OrderStatus, validate_transition
from app.persistence.models import (
    AccountSnapshot,
    AIRecommendation,
    Candle,
    Execution,
    FailureReconciliation,
    FundingCollectionCheckpoint,
    FundingEvent,
    OperationalSession,
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


def get_active_session(session: Session, state: SystemState) -> OperationalSession | None:
    """Fase 3.1.1 (correção final da auditoria do PO, item 2): fonte
    ÚNICA e pública para obter a sessão operacional ativa -- `active_session_id`
    vive em `SystemState`, uma linha ÚNICA e global (nunca por símbolo,
    nunca por instância de orquestrador -- ver `repo.get_or_create_system_state`),
    então esta função nunca precisou de nenhum estado de instância de
    `Orchestrator`/`MultiSymbolOrchestrator` para funcionar. Substitui o
    antigo `Orchestrator._active_session` (método privado, existia só em
    `Orchestrator`, nunca em `MultiSymbolOrchestrator` -- causa exata do
    `AttributeError` em `POST /kill-switch/engage` sob multiativo) como a
    interface pública comum entre rotas HTTP e ambos os tipos de
    orquestrador. Retorna `None` se nenhuma sessão está ativa ainda (ex.:
    um `Orchestrator` de teste construído sem passar por
    `app.api.main.build_orchestrator`)."""
    if state.active_session_id is None:
        return None
    return session.get(OperationalSession, state.active_session_id)


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
    whole session/transaction.

    Fase 3.2 (decisão Q4 do PO): o `timeframe` é SEMPRE canonicalizado
    antes de gravar -- todo candle novo entra como `"1m"`, nunca mais como
    o alias `"1"` que o provider da Bybit usava. O banco legado não é
    reescrito; a convivência é resolvida na LEITURA
    (`recent_candles`/`get_last_candle_open_time`)."""
    timeframe = canonical_timeframe(timeframe)
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
    off.

    Fase 3.2 (Q4): considera TODOS os aliases do timeframe -- um banco que
    já tenha candles gravados como `"1"` (antes da canonicalização)
    continua fornecendo cursor correto, sem reescrita nem migration."""
    row = session.execute(
        select(Candle.open_time)
        .where(Candle.symbol == symbol, Candle.timeframe.in_(timeframe_aliases(timeframe)))
        .order_by(Candle.open_time.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return row


def recent_candles(session: Session, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    """Fase 3.1 (painel gráfico): as últimas `limit` velas FECHADAS, em
    ordem cronológica -- busca as mais recentes primeiro (`ORDER BY
    open_time DESC LIMIT`, o que usa diretamente o índice único composto
    `uq_candle_symbol_timeframe_open_time` de `(symbol, timeframe,
    open_time)`, sem full scan) e inverte em Python para a ordem que o
    gráfico espera.

    Fase 3.2 (decisão Q4 do PO): aceita todos os aliases do timeframe, de
    modo que candles legados gravados como `"1"` continuem aparecendo. Se
    o MESMO `open_time` existir nas duas grafias, a deduplicação é LÓGICA
    e determinística aqui -- prevalece o registro CANÔNICO (`"1m"`), e
    apenas UM candle é emitido para aquele instante. O banco nunca é
    reescrito por causa disso."""
    aliases = timeframe_aliases(timeframe)
    canonical = aliases[0]
    rows = session.execute(
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe.in_(aliases))
        .order_by(Candle.open_time.desc())
        .limit(limit * len(aliases))
    ).scalars().all()

    by_open_time: dict[datetime, Candle] = {}
    for row in rows:
        current = by_open_time.get(row.open_time)
        if current is None or (current.timeframe != canonical and row.timeframe == canonical):
            by_open_time[row.open_time] = row
    deduped = [by_open_time[k] for k in sorted(by_open_time, reverse=True)][:limit]
    return list(reversed(deduped))


def save_signal(session: Session, symbol: str, direction: str, justification: str,
                 observed_price: float, atr: float, params: dict,
                 source_candle_open_time: datetime | None = None) -> StrategySignal:
    s = StrategySignal(
        symbol=symbol, direction=direction, justification=justification,
        observed_price=observed_price, atr=atr, params_json=json.dumps(params),
        source_candle_open_time=source_candle_open_time,
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


def execution_fees(session: Session, symbol: str | None = None, since: datetime | None = None) -> float:
    """Fase 3.1.1 (correção final da auditoria do PO, item 1): fonte
    CANÔNICA de taxas para o cálculo de equity -- soma `Execution.fee`
    diretamente, uma linha por fill REALMENTE ocorrido (deduplicado por
    `UniqueConstraint(order_id, exchange_fill_id)` -- nunca um fill
    duplicado, nunca uma ordem rejeitada/sem fill, que nunca ganha uma
    linha `Execution`).

    Por que não `Position.fees_paid` nem `Order.fees_total`: ambos são
    agregados DERIVADOS. `Position.fees_paid` só é incrementado quando um
    fill é efetivamente APLICADO a uma posição -- um fill bloqueado por
    segurança (`LATE_OPPOSITE_FILL_BLOCKED`, side oposto ao da posição
    aberta) ou um fill de fechamento que chega sem nenhuma posição local
    (`position is None` em `app/execution/fill_service.py`) tem uma taxa
    real (linha `Execution` real) que nunca chega a incrementar nenhum
    `Position.fees_paid` -- a taxa "some" da equity se essa for a única
    fonte somada. `Order.fees_total` é recalculado do zero a cada fill a
    partir do MESMO conjunto de linhas `Execution` (ver
    `app/execution/fill_ledger.py::record_new_fills`) -- somar
    `Order.fees_total` E `Position.fees_paid` juntos duplicaria toda taxa
    normal (a mesma taxa apareceria nas duas fontes).

    `Execution.fee` é a única fonte que nunca duplica (cada linha
    representa exatamente um fill real, uma única vez) e nunca omite
    (inclui até os fills bloqueados/órfãos, cuja taxa foi genuinamente
    incorrida pela execução simulada). `since` filtra por
    `Execution.executed_at` -- o instante real em que o fill (e sua taxa)
    ocorreu, nunca a data de abertura/fechamento da posição que porventura
    o recebeu (ou não)."""
    stmt = select(Execution.fee).join(Order, Execution.order_id == Order.id)
    if symbol is not None:
        stmt = stmt.where(Order.symbol == symbol)
    if since is not None:
        stmt = stmt.where(Execution.executed_at >= since)
    return sum(session.execute(stmt).scalars().all())


def execution_fills_count(session: Session, symbol: str | None = None, since: datetime | None = None) -> int:
    """Fase 3.1.1 (correção final da auditoria do PO, item 2): quantidade
    de fills reais no período -- mesma fonte/filtro de `execution_fees`,
    para o bloco `period_performance` expor "quantidade de fills"
    explicitamente."""
    stmt = select(Execution.id).join(Order, Execution.order_id == Order.id)
    if symbol is not None:
        stmt = stmt.where(Order.symbol == symbol)
    if since is not None:
        stmt = stmt.where(Execution.executed_at >= since)
    return len(session.execute(stmt).scalars().all())


def orders_with_executions_since(
    session: Session, symbol: str | None = None, since: datetime | None = None,
) -> list[tuple[Order, list[Execution]]]:
    """Fase 3.1.1 (último gate contábil da auditoria do PO): fonte para
    `/api/costs`, ESCOPADA pela base contábil ativa -- nunca
    `repo.filled_orders` (que retornava toda ordem FILLED/PARTIALLY_FILLED
    de qualquer época, e usava `Order.avg_fill_price`/`filled_qty`
    agregados por TODOS os fills da ordem, mesmo os de antes de um
    reset).

    Retorna uma lista de `(Order, [Execution, ...])` -- só os `Execution`
    cujo `executed_at >= since` (quando dado), agrupados por ordem. Uma
    ordem com ZERO fills no recorte simplesmente não aparece (nunca uma
    entrada "fantasma"). Uma ordem cujos fills estão PARCIALMENTE do outro
    lado da fronteira (alguns antes, alguns depois de `since`) aparece
    apenas com os fills POSTERIORES -- o chamador deve recalcular
    `avg_fill_price`/`filled_qty` a partir APENAS dessas linhas, nunca
    reaproveitar `Order.avg_fill_price`/`Order.filled_qty` (que agregam
    TODOS os fills da ordem, de qualquer época)."""
    stmt = (
        select(Execution, Order)
        .join(Order, Execution.order_id == Order.id)
        .order_by(Order.id, Execution.id)
    )
    if symbol is not None:
        stmt = stmt.where(Order.symbol == symbol)
    if since is not None:
        stmt = stmt.where(Execution.executed_at >= since)

    by_order: dict[int, tuple[Order, list[Execution]]] = {}
    for execution, order in session.execute(stmt).all():
        if order.id not in by_order:
            by_order[order.id] = (order, [])
        by_order[order.id][1].append(execution)
    return list(by_order.values())


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


def decision_snapshot_for_order(session: Session, order: Order) -> dict | None:
    """Fase 3.2 (decisão Q2 do PO): o snapshot CONGELADO da decisão que
    originou esta ordem, alcançado exclusivamente pela cadeia de chaves
    estrangeiras JÁ existente e obrigatória:

        Order.risk_evaluation_id -> RiskEvaluation.signal_id
                                 -> StrategySignal.params_json

    Ambas as FKs são NOT NULL (ver app/persistence/models.py), então a
    relação é persistida e inequívoca -- nunca uma busca aproximada por
    símbolo, preço ou janela de horário, que o PO proibiu explicitamente.

    Devolve o dicionário de `params_json` do sinal (ou `None` se a ordem
    for legada/sem snapshot -- por exemplo criada antes desta fase). O
    chamador NUNCA deve completar um `None` lendo o `Settings` atual do
    processo: a configuração pode ter mudado desde a decisão, e usar o
    valor de hoje reinterpretaria uma proteção que foi definida ontem."""
    row = session.execute(
        select(StrategySignal.params_json)
        .join(RiskEvaluation, RiskEvaluation.signal_id == StrategySignal.id)
        .where(RiskEvaluation.id == order.risk_evaluation_id)
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        params = json.loads(row)
    except (TypeError, ValueError):
        return None
    return params if isinstance(params, dict) else None


def reanchor_position_protection(
    session: Session, position: Position, stop_distance_per_unit: float,
    target_distance_per_unit: float,
) -> None:
    """Fase 3.2 (item 9 da decisão do PO): reancora stop-loss e take-profit
    no preço médio REAL da posição, usando as distâncias CONGELADAS na
    decisão -- nunca um ATR recalculado com o mercado do momento do fill.

        BUY:  stop = avg_entry_price - stop_distance
              alvo = avg_entry_price + target_distance
        SELL: stop = avg_entry_price + stop_distance
              alvo = avg_entry_price - target_distance

    Chamado após CADA fill de AUMENTO (abertura ou acréscimo). Como
    `add_to_position` já recalculou o preço médio ponderado antes desta
    chamada, o stop e o alvo acompanham o preço médio -- e podem, por
    isso, MOVER-SE em qualquer direção entre fills parciais. Não são
    monotônicos e este módulo não afirma que sejam.

    Fills de REDUÇÃO/fechamento nunca chamam esta função: reduzir não é
    uma nova entrada e não pode reancorar a proteção como se fosse."""
    if position.side == "BUY":
        position.stop_loss = position.avg_entry_price - stop_distance_per_unit
        position.take_profit = position.avg_entry_price + target_distance_per_unit
    else:
        position.stop_loss = position.avg_entry_price + stop_distance_per_unit
        position.take_profit = position.avg_entry_price - target_distance_per_unit
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


def closed_positions(
    session: Session, symbol: str | None = None, since: datetime | None = None,
) -> list[Position]:
    """Fase 3.1.1: `since`, when given, keeps only trades that CLOSED at or
    after that instant -- the scope window (`session`/`daily`) for
    performance metrics and equity. `None` (default) is the unfiltered
    lifetime view, unchanged from before this parameter existed."""
    stmt = select(Position).where(Position.status == "CLOSED").order_by(Position.closed_at)
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    if since is not None:
        stmt = stmt.where(Position.closed_at >= since)
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
    return row


def funding_total(session: Session, symbol: str | None = None, since: datetime | None = None) -> float:
    """Correção v1.1 #6: the real SUM of collected funding -- 0.0 is a
    genuine, correct total when no funding has settled yet (never confused
    with UNAVAILABLE, which app.metrics.engine reports only when there is
    no funding_provider at all to have collected anything with). Fase
    3.1.1: `since`, when given, keeps only events that occurred at or
    after that instant (scope window)."""
    stmt = select(FundingEvent.amount)
    if symbol:
        stmt = stmt.where(FundingEvent.symbol == symbol)
    if since is not None:
        stmt = stmt.where(FundingEvent.occurred_at >= since)
    return sum(session.execute(stmt).scalars().all())


def funding_paid_received(
    session: Session, symbol: str | None = None, since: datetime | None = None,
) -> tuple[float, float]:
    """Fase 3.1.1 (correção final da auditoria do PO, item 7): funding
    pago e recebido SEPARADOS -- nunca apresentar funding recebido como um
    custo negativo sem explicação. `FundingEvent.amount` já é assinado
    (positivo = creditado, negativo = debitado -- ver models.py)."""
    stmt = select(FundingEvent.amount)
    if symbol:
        stmt = stmt.where(FundingEvent.symbol == symbol)
    if since is not None:
        stmt = stmt.where(FundingEvent.occurred_at >= since)
    amounts = session.execute(stmt).scalars().all()
    received = sum(a for a in amounts if a > 0)
    paid = -sum(a for a in amounts if a < 0)
    return paid, received


def get_funding_checkpoint(session: Session, symbol: str) -> FundingCollectionCheckpoint | None:
    """Correção v1.3 #1: the explicit, persisted proof of funding-collection
    coverage for `symbol` -- `None` means nothing has ever been fully
    covered yet (the caller anchors the first window at `now -
    FUNDING_WINDOW_SECONDS` in that case, never at an unbounded past)."""
    row = session.execute(
        select(FundingCollectionCheckpoint).where(FundingCollectionCheckpoint.symbol == symbol)
    ).scalar_one_or_none()
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
        if covered_until > existing.covered_until:
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


def cost_gate_stats(
    session: Session, symbol: str | None = None, since: datetime | None = None,
) -> dict:
    """Fase 3.2 (item 13 da decisão do PO): quantas entradas o gate de
    viabilidade líquida bloqueou e qual a cobertura média de custo
    efetivamente alcançada na avaliação.

    Fonte: o próprio `RiskEvaluation.checks_json` já persistido -- nenhuma
    coluna nova, nenhuma migration, nenhum contador paralelo que pudesse
    divergir do que realmente foi decidido. Só entram avaliações em que o
    gate REALMENTE rodou (`cost_gate.applied is True`); uma avaliação sem
    gate aplicado nunca é contada como "aprovada pelo gate".

    Quando não há nenhuma avaliação com cobertura calculável, a média
    volta como `None` -- nunca um zero inventado."""
    stmt = select(RiskEvaluation.checks_json).join(
        StrategySignal, RiskEvaluation.signal_id == StrategySignal.id
    )
    if symbol is not None:
        stmt = stmt.where(StrategySignal.symbol == symbol)
    if since is not None:
        stmt = stmt.where(RiskEvaluation.created_at >= since)

    evaluated = 0
    blocked = 0
    ratios: list[float] = []
    for raw in session.execute(stmt).scalars().all():
        try:
            checks = json.loads(raw)
        except (TypeError, ValueError):
            continue
        gate = checks.get("cost_gate") if isinstance(checks, dict) else None
        if not isinstance(gate, dict) or gate.get("applied") is not True:
            continue
        evaluated += 1
        if gate.get("cost_coverage_ok") is False:
            blocked += 1
        ratio = gate.get("achieved_coverage_ratio")
        if isinstance(ratio, (int, float)):
            ratios.append(float(ratio))

    return {
        "evaluated": evaluated,
        "blocked": blocked,
        "avg_coverage_ratio": (sum(ratios) / len(ratios)) if ratios else None,
    }


def recent_risk_evaluations(session: Session, limit: int = 50) -> list[RiskEvaluation]:
    stmt = select(RiskEvaluation).order_by(RiskEvaluation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_security_events(session: Session, limit: int = 50) -> list[SecurityEvent]:
    stmt = select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_failures(session: Session, limit: int = 50) -> list[FailureReconciliation]:
    stmt = select(FailureReconciliation).order_by(FailureReconciliation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
