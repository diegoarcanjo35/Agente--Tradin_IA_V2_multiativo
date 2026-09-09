"""Fase 3.5 — endpoints SOMENTE LEITURA do diagnóstico do funil e do
painel Shadow/Diagnóstico. Mesmo contrato de app/api/routes_shadow.py:
nenhuma rota aqui altera estado, não existe POST, nenhum caminho para
tocar estratégia, gate, threshold ou patrimônio.

Correção da auditoria (2ª rodada): `/diagnostics/dashboard` é o endpoint
CONSOLIDADO que o painel usa a cada ciclo -- uma única requisição HTTP,
uma única sessão/transação curta, um único `generated_at` para todas as
seções. Os endpoints individuais abaixo continuam existindo para
auditoria pontual (curl/Postman), mas o frontend não os chama mais em
paralelo.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.routes_dashboard import _new_entries_status, _symbols_health_dict
from app.persistence import repo
from app.persistence.db import session_scope
from app.shadow import diagnostics
from app.shadow.engine import MODELS

router = APIRouter()


def _orch(request: Request):
    return request.app.state.orchestrator


def _valida_modelo(model: str | None) -> None:
    if model is not None and model not in MODELS:
        raise HTTPException(status_code=404, detail=f"Modelo shadow desconhecido: {model}")


def _valida_scope(scope: str) -> None:
    if scope not in diagnostics.SCOPES:
        raise HTTPException(
            status_code=422,
            detail=f"scope inválido: {scope!r} (use um de {diagnostics.SCOPES})")


@router.get("/diagnostics/dashboard")
def dashboard(request: Request, scope: str = "experiment",
             since: str | None = None, until: str | None = None):
    """Endpoint consolidado: tudo que o painel principal precisa, numa
    única sessão/transação curta e um único corte temporal. `scope`:
    'experiment' (padrão, desde o início do experimento ativo mais
    antigo), '24h', '7d' ou 'custom' (usa `since`/`until`)."""
    _valida_scope(scope)
    orch = _orch(request)
    engine = getattr(orch, "shadow_engine", None)
    with session_scope(orch.session_factory) as session:
        corpo = diagnostics.dashboard(session, scope=scope, since=since, until=until)
        state = repo.get_or_create_system_state(session)
        corpo["operational"] = {
            "operational_state": state.operational_state,
            "new_entries_status": _new_entries_status(state),
            "kill_switch_engaged": state.kill_switch_engaged,
            "trading_blocked": state.trading_blocked,
            "block_reason": state.block_reason,
            **_symbols_health_dict(request, orch),
        }
        corpo["shadow_health"] = (
            {"enabled": False, "status": "DESLIGADO",
             "detail": "Instrumentação shadow não está montada nesta instância."}
            if engine is None else {"enabled": True, **engine.health()}
        )
        return corpo


@router.get("/diagnostics/funnel")
def funnel(request: Request, since: str | None = None, until: str | None = None):
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.funnel(session, since, until)


@router.get("/diagnostics/cost-gate")
def cost_gate(request: Request, since: str | None = None, until: str | None = None):
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.cost_gate_distribution(session, since, until)


@router.get("/diagnostics/rejections")
def rejections(request: Request, model: str | None = None, symbol: str | None = None,
               direction: str | None = None, since: str | None = None, until: str | None = None):
    _valida_modelo(model)
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.rejection_reasons(session, model, symbol, direction, since, until)


@router.get("/diagnostics/trade-quality")
def trade_quality(request: Request, model: str | None = None, limit: int = 100,
                  since: str | None = None, until: str | None = None):
    _valida_modelo(model)
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.trade_quality(session, model, limit, since, until)


@router.get("/diagnostics/progress")
def progress(request: Request, since: str | None = None, until: str | None = None):
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.progress(session, since, until)


@router.get("/diagnostics/comparison")
def comparison(request: Request, since: str | None = None, until: str | None = None):
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.comparison(session, since, until)


@router.get("/diagnostics/significance")
def significance(request: Request, since: str | None = None, until: str | None = None):
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.significance(session, since, until)


@router.get("/diagnostics/events")
def events(request: Request, limit: int = 30):
    orch = _orch(request)
    with session_scope(orch.session_factory) as session:
        return diagnostics.recent_events(session, limit)
