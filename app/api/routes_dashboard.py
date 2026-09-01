from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app.core.config import RunMode
from app.metrics.engine import ClosedTrade, OrderFillView, compute_cost_metrics, compute_metrics
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import OperationalSession

router = APIRouter()

# Fase 2, item 7.1: PAPER_LIVE gets its own explicit banner -- it uses REAL
# market data, unlike REPLAY/PAPER_LOCAL, so the generic "AMBIENTE DEMO" text
# alone would be misleading about how "live" the data actually is, while
# still needing to make unmistakably clear that no order ever reaches the
# exchange.
_ENVIRONMENT_BANNERS = {
    RunMode.PAPER_LIVE: "PAPER AO VIVO — SIMULAÇÃO, SEM ORDEM NA CORRETORA",
}
_DEFAULT_BANNER = "AMBIENTE DEMO — SEM DINHEIRO REAL"
# Fase 3 multiativo: explicit distinct banner when PAPER_LIVE has more than
# one symbol configured -- never lets the monoativo PAPER_LIVE banner text
# stand in silently for a genuinely multiativo run.
_PAPER_LIVE_MULTIATIVO_BANNER = "PAPER LIVE MULTIATIVO — SIMULAÇÃO LOCAL"


def _environment_banner(orch) -> str:
    if orch.settings.mode == RunMode.PAPER_LIVE and len(orch.settings.symbols) > 1:
        return _PAPER_LIVE_MULTIATIVO_BANNER
    return _ENVIRONMENT_BANNERS.get(orch.settings.mode, _DEFAULT_BANNER)


def _poll_health_dict(request: Request) -> dict:
    poll_health = getattr(request.app.state, "poll_health", None)
    if poll_health is None:
        from app.api.poll_engine import PollHealth

        poll_health = PollHealth()
    return poll_health.as_dict()


def _configured_symbols(orch) -> list[str]:
    """Fase 3 multiativo: works for both a plain `Orchestrator` (monoativo)
    and a `MultiSymbolOrchestrator` -- both expose `.settings.symbols`."""
    return list(orch.settings.symbols)


def _symbols_health_dict(request: Request, orch) -> dict:
    """Additive field for `/state` -- `portfolio_status()` only exists on
    `MultiSymbolOrchestrator`. A monoativo `Orchestrator` has no per-symbol
    health tracker of its own (Fase 3 multiativo introduced that concept
    together with the scheduler); for that single symbol, the already-
    existing `poll_health` (this same process' one worker/executor
    liveness, see `_poll_health_dict`) is the closest honest equivalent --
    reused here rather than fabricating a second, disconnected status."""
    portfolio_status = getattr(orch, "portfolio_status", None)
    if portfolio_status is not None:
        return {"symbols_health": portfolio_status()}
    symbol = orch.settings.symbols[0]
    poll_status = _poll_health_dict(request).get("poll_loop_status", "INICIANDO")
    return {
        "symbols_health": {
            "portfolio": {"status": poll_status, "healthy_count": 1 if poll_status == "SAUDAVEL" else 0, "total": 1},
            "per_symbol": {symbol: {
                "status": poll_status, "consecutive_failures": 0,
                "last_tick_started_at": None, "last_tick_completed_at": None,
                "last_tick_success_at": None, "last_candle_persisted_at": None,
                "last_error": None, "has_gap": False,
            }},
        }
    }


@router.get("/state")
def get_state(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        return {
            "mode": orch.settings.mode.value,
            "trading_blocked": state.trading_blocked,
            "block_reason": state.block_reason,
            "kill_switch_engaged": state.kill_switch_engaged,
            "consecutive_losses": state.consecutive_losses,
            "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            "api_failure_count": state.api_failure_count,
            "replay_done": request.app.state.replay_done,
            "environment_banner": _environment_banner(orch),
            "operational_state": state.operational_state,
            # Fase 2, item 7.5/7.9: every independent block cause, so the
            # painel can show each one separately -- never collapsed into a
            # single opaque boolean beyond `trading_blocked` itself.
            "state_ambiguous": state.state_ambiguous,
            "clock_out_of_sync": state.clock_out_of_sync,
            "reconciliation_diverged": state.reconciliation_diverged,
            "reconciliation_stale": state.reconciliation_stale,
            "order_state_unknown": state.order_state_unknown,
            "initialization_not_reconciled": state.initialization_not_reconciled,
            "last_reconciliation_at": (
                state.last_reconciliation_at.isoformat() if state.last_reconciliation_at else None
            ),
            "reconciliation_interval_seconds": orch.settings.reconciliation_interval_seconds,
            "reconciliation_max_delay_seconds": orch.settings.reconciliation_max_delay_seconds,
            # Correção operacional do poll loop v1.0: heartbeat/saúde do
            # motor de mercado -- nunca aparenta saudável só porque este
            # próprio endpoint HTTP respondeu (esse era exatamente o
            # defeito: servidor web vivo, motor de mercado morto).
            # `getattr` com fallback: só `create_app()` (produção) monta
            # `app.state.poll_health` de verdade; testes que constroem um
            # FastAPI mínimo só com este router continuam funcionando.
            **_poll_health_dict(request),
            "poll_heartbeat_max_age_seconds": orch.settings.poll_heartbeat_max_age_seconds,
            # Fase 3 multiativo: additive -- absent fields above are all
            # unchanged from before, so a monoativo consumer sees byte-for-
            # byte the same response plus this one new key.
            **_symbols_health_dict(request, orch),
        }


@router.get("/symbols")
def get_symbols(request: Request):
    """Fase 3 multiativo: the configured symbols, in round-robin/session-
    identity order -- lets the frontend build per-symbol cards without
    hardcoding anything."""
    orch = request.app.state.orchestrator
    return {"symbols": _configured_symbols(orch)}


@router.get("/session")
def get_active_session(request: Request):
    """Fase 2, item 7.7/7.9: the current operational session -- id, mode,
    symbol(s), start time, and every counter tracked during it.

    Fase 3 multiativo (incl. correção obrigatória do PO, item 5):
    `symbols` (the canonical ordered list) is the native, real identity for
    any session created from migration v7 onward. `symbol` (legacy scalar)
    stays populated only for monoativo sessions -- `null` for a genuinely
    multi-symbol one, never a single value standing in for several.

    For a pre-v7 LEGACY row (`s.symbols` is `NULL` in the database, only
    `s.symbol` was ever recorded), this endpoint derives a compatible view
    (`symbols: [s.symbol]`) so callers that only look at `symbols` still see
    something usable -- but `symbols_origin` makes the provenance explicit
    (`"nativo"` vs. `"legado_derivado"`), and the underlying row is NEVER
    rewritten (the derivation happens only in this read, in memory)."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        if state.active_session_id is None:
            return None
        s = session.get(OperationalSession, state.active_session_id)
        if s is None:
            return None
        if s.symbols:
            symbols, symbols_origin = json.loads(s.symbols), "nativo"
        elif s.symbol:
            symbols, symbols_origin = [s.symbol], "legado_derivado"
        else:
            symbols, symbols_origin = None, "indisponivel"
        return {
            "session_uid": s.session_uid, "mode": s.mode, "symbol": s.symbol,
            "symbols": symbols, "symbols_origin": symbols_origin,
            "timeframe": s.timeframe, "started_at": s.started_at.isoformat(),
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "end_reason": s.end_reason, "strategy_version": s.strategy_version,
            "status": s.status,
            "candles_count": s.candles_count, "signals_count": s.signals_count,
            "approvals_count": s.approvals_count, "rejections_count": s.rejections_count,
            "orders_count": s.orders_count, "fills_count": s.fills_count,
            "failures_count": s.failures_count, "reconciliations_count": s.reconciliations_count,
        }


@router.get("/orders")
def get_orders(request: Request, limit: int = 50, symbol: str | None = None):
    """Fase 2, item 7.2/7.9: recent orders and their state-machine status --
    what the painel shows for "ordens abertas e respectivas máquinas de
    estado" and "fills parciais". Fase 3 multiativo: optional `?symbol=`
    filter -- omitted, behavior is unchanged (every symbol, consolidated)."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        from sqlalchemy import select

        from app.persistence.models import Order

        stmt = select(Order).order_by(Order.created_at.desc()).limit(limit)
        if symbol is not None:
            stmt = stmt.where(Order.symbol == symbol)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty,
                "status": o.status, "is_close": o.is_close, "mode": o.mode,
                "exchange_order_id": o.exchange_order_id,
                "filled_qty": o.filled_qty, "avg_fill_price": o.avg_fill_price,
                "fees_total": o.fees_total, "reference_price": o.reference_price,
                "created_at": o.created_at.isoformat(), "updated_at": o.updated_at.isoformat(),
            }
            for o in rows
        ]


@router.get("/costs")
def get_costs(request: Request):
    """Fase 2, item 7.6/7.9: fees accumulated and realized slippage vs. the
    reference price -- never a fabricated zero when unknown."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        orders = repo.filled_orders(session)
        views = [
            OrderFillView(
                side=o.side, reference_price=o.reference_price,
                avg_fill_price=o.avg_fill_price, fees_total=o.fees_total,
            )
            for o in orders
        ]
        result = compute_cost_metrics(views)
        return result.__dict__


def _metrics_for_trades(trades: list[ClosedTrade], open_exposure_usd: float, funding_total: float | None) -> dict:
    result = compute_metrics(
        trades, starting_balance=1000.0, open_exposure_usd=open_exposure_usd, funding_total=funding_total,
    )
    return result.__dict__


@router.get("/metrics")
def get_metrics(request: Request):
    """Fase 3 multiativo: consolidated metrics (unchanged shape/keys --
    backward compatible) PLUS a new additive `per_symbol` breakdown, one
    `compute_metrics` result per configured symbol -- `app/metrics/engine.py`
    itself is untouched, this just buckets the same rows by `symbol` before
    calling it, once per bucket plus once for everything."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        closed = repo.closed_positions(session)
        trades = [
            ClosedTrade(
                realized_pnl=p.realized_pnl, fees_paid=p.fees_paid,
                opened_at=p.opened_at, closed_at=p.closed_at,
            )
            for p in closed
            if p.closed_at is not None
        ]
        open_pos = repo.open_positions(session)
        open_exposure = sum(p.qty * p.avg_entry_price for p in open_pos)
        # Correção v1.1 #6: a real collected SUM only when a funding
        # provider actually exists (BYBIT_DEMO) -- None otherwise, so
        # UNAVAILABLE is never confused with "collected and it's 0.0".
        # Fase 3 multiativo: summed across every CONFIGURED symbol
        # explicitly (never an unfiltered `funding_total(session)`), so a
        # monoativo install's result is unchanged and a multiativo one
        # never silently includes an unconfigured symbol's historical data.
        funding = (
            sum(repo.funding_total(session, s) for s in _configured_symbols(orch))
            if orch.funding_provider is not None else None
        )
        result = _metrics_for_trades(trades, open_exposure, funding)

        per_symbol = {}
        for symbol in _configured_symbols(orch):
            symbol_closed = repo.closed_positions(session, symbol)
            symbol_trades = [
                ClosedTrade(
                    realized_pnl=p.realized_pnl, fees_paid=p.fees_paid,
                    opened_at=p.opened_at, closed_at=p.closed_at,
                )
                for p in symbol_closed
                if p.closed_at is not None
            ]
            symbol_open_pos = repo.open_positions(session, symbol)
            symbol_exposure = sum(p.qty * p.avg_entry_price for p in symbol_open_pos)
            symbol_funding = (
                repo.funding_total(session, symbol) if orch.funding_provider is not None else None
            )
            per_symbol[symbol] = _metrics_for_trades(symbol_trades, symbol_exposure, symbol_funding)

        result["per_symbol"] = per_symbol
        return result


@router.get("/positions")
def get_positions(request: Request, symbol: str | None = None):
    """Fase 3 multiativo: optional `?symbol=` filter -- omitted, behavior is
    unchanged (every symbol, consolidated)."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        open_pos = repo.open_positions(session, symbol)
        return [
            {
                "id": p.id, "symbol": p.symbol, "side": p.side, "qty": p.qty,
                "avg_entry_price": p.avg_entry_price, "stop_loss": p.stop_loss,
                "take_profit": p.take_profit, "opened_at": p.opened_at.isoformat(),
            }
            for p in open_pos
        ]


@router.get("/equity-curve")
def get_equity_curve(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        closed = sorted(
            [p for p in repo.closed_positions(session) if p.closed_at],
            key=lambda p: p.closed_at,
        )
        running = 1000.0
        points = [{"t": None, "equity": running}]
        for p in closed:
            running += p.realized_pnl - p.fees_paid
            points.append({"t": p.closed_at.isoformat(), "equity": running})
        return points


@router.get("/signals")
def get_signals(request: Request, limit: int = 50, symbol: str | None = None):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_signals(session, limit=limit, symbol=symbol)
        return [
            {
                "id": r.id, "symbol": r.symbol, "direction": r.direction,
                "justification": r.justification, "observed_price": r.observed_price,
                "atr": r.atr, "params": json.loads(r.params_json),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/ai-recommendations")
def get_ai_recommendations(request: Request, limit: int = 50, symbol: str | None = None):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_ai_recommendations(session, limit=limit, symbol=symbol)
        return [
            {
                "id": r.id, "symbol": r.symbol, "signal_id": r.signal_id,
                "recommendation": r.recommendation, "confidence": r.confidence,
                "reasoning_summary": r.reasoning_summary,
                "risk_flags": json.loads(r.risk_flags_json), "provider": r.provider,
                "model_version": r.model_version, "is_valid": r.is_valid,
                "rejection_reason": r.rejection_reason, "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/risk-evaluations")
def get_risk_evaluations(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_risk_evaluations(session, limit=limit)
        return [
            {
                "id": r.id, "signal_id": r.signal_id, "approved": r.approved,
                "reason": r.reason, "checks": json.loads(r.checks_json),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/security-events")
def get_security_events(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_security_events(session, limit=limit)
        return [
            {"id": r.id, "event_type": r.event_type, "detail": r.detail,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ]


@router.get("/failures")
def get_failures(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_failures(session, limit=limit)
        return [
            {"id": r.id, "kind": r.kind, "detail": r.detail, "resolved": r.resolved,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ]
