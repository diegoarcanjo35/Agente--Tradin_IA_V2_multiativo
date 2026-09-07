"""Fase 3.4.3 — endpoints SOMENTE LEITURA da instrumentação shadow.

Nenhuma rota aqui altera estado: não existe POST, não existe caminho para
ativar, desativar, criar ordem ou tocar patrimônio. Os números shadow
NUNCA se misturam com `/api/portfolio-summary`, `/api/positions`,
`/api/orders` ou `/api/metrics` -- são hipóteses, não a carteira.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from app.persistence.db import session_scope
from app.persistence.models import (
    ShadowExperiment,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
)
from app.shadow.engine import MODELS
from app.shadow.metrics import comparative, model_metrics

router = APIRouter()


def _orch(request: Request):
    return request.app.state.orchestrator


def _valida_modelo(model: str | None) -> None:
    if model is not None and model not in MODELS:
        raise HTTPException(status_code=404, detail=f"Modelo shadow desconhecido: {model}")


@router.get("/shadow/health")
def shadow_health(request: Request):
    """Saúde PRÓPRIA da instrumentação -- separada da saúde de mercado.
    Uma falha shadow aparece aqui e em nenhum outro lugar."""
    orch = _orch(request)
    engine = getattr(orch, "shadow_engine", None)
    if engine is None:
        return {"enabled": False, "status": "DESLIGADO",
                "detail": "Instrumentação shadow não está montada nesta instância."}
    return {"enabled": True, **engine.health()}


@router.get("/shadow/opportunities")
def shadow_opportunities(request: Request, model: str | None = None,
                         symbol: str | None = None, limit: int = 100):
    _valida_modelo(model)
    limit = max(1, min(limit, 1000))
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        q = session.query(ShadowOpportunity)
        if model:
            q = q.filter(ShadowOpportunity.model == model)
        if symbol:
            q = q.filter(ShadowOpportunity.symbol == symbol)
        linhas = q.order_by(ShadowOpportunity.id.desc()).limit(limit).all()
        return [{
            "model": o.model, "hypothesis_version": o.hypothesis_version,
            "symbol": o.symbol,
            "source_candle_open_time": o.source_candle_open_time.isoformat(),
            "strategy_timeframe_minutes": o.strategy_timeframe_minutes,
            "direction": o.direction, "reference_price": o.reference_price,
            "fast_sma": o.fast_sma, "slow_sma": o.slow_sma, "atr": o.atr,
            "normalized_separation": o.normalized_separation,
            "cost_coverage_ratio": o.cost_coverage_ratio,
            "approved": o.approved, "reason": o.reason,
            "warmup_ready": o.warmup_ready, "signal_is_fresh": o.signal_is_fresh,
            "operational_gate_would_approve": o.operational_gate_would_approve,
        } for o in linhas]


@router.get("/shadow/positions")
def shadow_positions(request: Request, model: str | None = None):
    """Posições HIPOTÉTICAS abertas. Nunca aparecem em /api/positions."""
    _valida_modelo(model)
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        q = session.query(ShadowPosition).filter(ShadowPosition.status == "OPEN")
        if model:
            q = q.filter(ShadowPosition.model == model)
        return [{
            "model": p.model, "symbol": p.symbol, "side": p.side, "qty": p.qty,
            "entry_fill_price": p.entry_fill_price, "notional_usd": p.notional_usd,
            "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            "entry_fee_usd": p.entry_fee_usd, "entry_slippage_usd": p.entry_slippage_usd,
            "normalized_separation": p.normalized_separation,
            "opened_candle_time": p.opened_candle_time.isoformat(),
            "hypothetical": True,
        } for p in q.order_by(ShadowPosition.id.desc()).all()]


@router.get("/shadow/trades")
def shadow_trades(request: Request, model: str | None = None,
                  symbol: str | None = None, limit: int = 100):
    _valida_modelo(model)
    limit = max(1, min(limit, 1000))
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        q = session.query(ShadowTrade)
        if model:
            q = q.filter(ShadowTrade.model == model)
        if symbol:
            q = q.filter(ShadowTrade.symbol == symbol)
        linhas = q.order_by(ShadowTrade.id.desc()).limit(limit).all()
        return [{
            "model": t.model, "hypothesis_version": t.hypothesis_version,
            "symbol": t.symbol, "side": t.side, "qty": t.qty,
            "entry_fill_price": t.entry_fill_price, "exit_fill_price": t.exit_fill_price,
            "notional_usd": t.notional_usd, "stop_loss": t.stop_loss,
            "take_profit": t.take_profit, "exit_reason": t.exit_reason,
            "opened_candle_time": t.opened_candle_time.isoformat(),
            "closed_candle_time": t.closed_candle_time.isoformat(),
            "duration_minutes": t.duration_minutes,
            "gross_pnl_usd": t.gross_pnl_usd, "fees_usd": t.fees_usd,
            "slippage_usd": t.slippage_usd, "net_pnl_usd": t.net_pnl_usd,
            "normalized_separation": t.normalized_separation,
            "hypothetical": True,
        } for t in linhas]


@router.get("/shadow/metrics")
def shadow_metrics(request: Request, model: str | None = None,
                   experiment_id: int | None = None):
    """Métricas comparativas dos dois portfólios, com o gate de promoção
    explícito. Estes números NÃO são patrimônio operacional."""
    _valida_modelo(model)
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        if model:
            return {"model": model_metrics(session, model, experiment_id),
                    "hypothetical": True}
        return {**comparative(session), "hypothetical": True}


@router.get("/shadow/experiments")
def shadow_experiments(request: Request, model: str | None = None,
                       include_ended: bool = True):
    """Identidade dos experimentos. Permite consultar histórico
    EXPLICITAMENTE -- métricas padrão só olham o experimento ativo, e
    experimentos diferentes nunca são agregados em silêncio."""
    _valida_modelo(model)
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        q = session.query(ShadowExperiment)
        if model:
            q = q.filter(ShadowExperiment.model == model)
        if not include_ended:
            q = q.filter(ShadowExperiment.status == "ATIVO")
        return [{
            "id": e.id, "experiment_uid": e.experiment_uid, "model": e.model,
            "hypothesis_version": e.hypothesis_version, "threshold": e.threshold,
            "strategy_timeframe_minutes": e.strategy_timeframe_minutes,
            "strategy_version": e.strategy_version,
            "fast_period": e.fast_period, "slow_period": e.slow_period,
            "atr_period": e.atr_period, "fee_rate": e.fee_rate,
            "slippage_bps": e.slippage_bps,
            "stop_loss_atr_multiple": e.stop_loss_atr_multiple,
            "take_profit_atr_multiple": e.take_profit_atr_multiple,
            "config_fingerprint": e.config_fingerprint,
            "config_snapshot": json.loads(e.config_snapshot_json),
            "started_at": e.started_at.isoformat(),
            "ended_at": e.ended_at.isoformat() if e.ended_at else None,
            "end_reason": e.end_reason, "status": e.status,
        } for e in q.order_by(ShadowExperiment.id.desc()).all()]
