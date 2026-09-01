"""Control endpoints. Deliberately does NOT include any mode/environment
toggle -- mode is process-level config only (see app/core/config.py).

Correction v1.3 #3 / v1.4 #4 (opções B+C combinadas): a validação de
`Settings.api_host` só é confiável quando o processo foi iniciado pelo
launcher oficial (`app/run.py`) -- um `uvicorn app.api.main:app --host
0.0.0.0` direto contorna completamente esse controle, e a aplicação não tem
como observar ou impedir isso a partir de dentro do processo ASGI. Como
segunda camada, independente de como o servidor foi iniciado:

- ATIVAR o bloqueio de emergência nunca exige autenticação -- é uma ação que
  só aumenta a segurança, então liberá-la de qualquer origem é seguro
  (opção C: "permitir ativar pela interface").
- DESATIVAR o bloqueio de emergência (liberar operações) LOCALMENTE
  (127.0.0.1/::1) sempre funciona, COM ou SEM `CONTROL_API_TOKEN`
  configurado -- correção v1.4 #4: uma versão anterior exigia o token
  mesmo para requisições locais assim que ele era configurado, o que
  quebrava o próprio painel local ao ativar a proteção para acesso remoto.
  O painel nunca precisa (nem deve) conhecer o token.
- DESATIVAR remotamente exige `X-Control-Token` batendo com
  `CONTROL_API_TOKEN`; sem token configurado, acesso remoto é negado por
  padrão.
- A origem é sempre `request.client.host`, o endereço do socket TCP que o
  servidor ASGI realmente aceitou -- nunca um cabeçalho como
  `X-Forwarded-For`/`X-Real-IP`, que qualquer cliente pode forjar. Atrás de
  um proxy reverso, `request.client.host` será o endereço do PRÓPRIO proxy,
  não do usuário final; nesse cenário configure `CONTROL_API_TOKEN` -- não
  há aqui nenhuma lista de proxies confiáveis para repassar cabeçalhos de
  origem.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request

from app.execution import fill_service
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope

router = APIRouter()

LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def require_local_or_authenticated(request: Request) -> None:
    """Dependency guarding any control action that REDUCES safety (i.e.
    releases operations).

    Policy (correction v1.4 #4): a LOCAL client is always allowed, whether
    or not CONTROL_API_TOKEN is configured -- configuring the token
    protects remote access; it must never lock out the local panel. A
    non-local client is denied unless it presents the correct token; with
    no token configured at all, remote access is denied outright (deny by
    default, never silently open).
    """
    client_host = request.client.host if request.client is not None else None
    if client_host in LOCAL_CLIENT_HOSTS:
        return

    settings = request.app.state.settings
    token = getattr(settings, "control_api_token", "") or ""
    if not token:
        raise HTTPException(
            status_code=403,
            detail=(
                "Acesso negado: nenhum CONTROL_API_TOKEN configurado e a requisição não "
                "é local. Desativar o bloqueio de emergência remotamente exige um token "
                "de controle configurado via CONTROL_API_TOKEN."
            ),
        )

    provided = request.headers.get("X-Control-Token", "")
    if hmac.compare_digest(provided, token):
        return
    raise HTTPException(
        status_code=403,
        detail="Token de controle inválido ou ausente. Desativação recusada.",
    )


@router.post("/kill-switch/engage")
def engage_kill_switch(request: Request):
    """Fase 2, item 7.3 / correção v1.1 #1/#4: before considering the
    system stabilized, cancels every non-terminal order this engine can
    actually cancel (has an `exchange_order_id`). CANCEL_PENDING is
    persisted BEFORE the fire-and-forget `request_cancel()` call, then a
    `poll_order()` confirms the real outcome -- if a fill won the race
    instead, it goes through the exact same `fill_service.apply_order_snapshot`
    every other fill path uses, so the position/Execution rows/session
    counters are always updated consistently, never a second, divergent
    code path. A pending order with no exchange_order_id yet (crashed
    before it ever reached the exchange) can't be cancelled remotely --
    reconciliation, run at the end regardless, is what resolves those
    against the exchange's real state."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)
        repo.record_security_event(
            session, "KILL_SWITCH_ENGAGED", "Ativação manual do bloqueio de emergência pelo painel."
        )

        # Fase 3.1.1 (correção final da auditoria do PO, item 2): interface
        # pública comum -- funciona tanto para `Orchestrator` (monoativo)
        # quanto para `MultiSymbolOrchestrator` (multiativo), que nunca
        # teve o antigo `orch._active_session` privado. `active_session_id`
        # é uma única sessão de carteira, global (ver `SystemState`),
        # nunca por símbolo -- nenhuma diferença de comportamento entre os
        # dois tipos de orquestrador aqui.
        op_session = repo.get_active_session(session, state)
        cancelled_order_ids: list[int] = []
        for order in repo.non_terminal_orders(session, mode=orch.settings.mode.value):
            if not order.exchange_order_id:
                continue

            repo.transition_order_status(
                session, order, OrderStatus.CANCEL_PENDING,
                detail="Cancelamento solicitado pelo bloqueio de emergência (kill switch).",
            )
            orch.execution_engine.request_cancel(order.exchange_order_id)
            snapshot = orch.execution_engine.poll_order(order.exchange_order_id)
            result = fill_service.apply_order_snapshot(
                session, state, op_session, order, snapshot,
                is_close=order.is_close, max_api_failures=orch.settings.risk_max_api_failures,
            )
            if result.status == OrderStatus.CANCELLED:
                cancelled_order_ids.append(order.id)

        state.order_state_unknown = repo.has_unknown_orders(session)
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)
        if cancelled_order_ids or repo.non_terminal_orders(session, mode=orch.settings.mode.value):
            orch.reconcile(session, state)

        return {
            "kill_switch_engaged": True,
            "trading_blocked": state.trading_blocked,
            "ordens_canceladas": cancelled_order_ids,
            "mensagem": "Bloqueio de emergência ativado. Nenhuma nova operação será realizada.",
        }


@router.post("/kill-switch/disengage")
def disengage_kill_switch(request: Request):
    """Correction v1.2 #5: desengatar o kill switch remove SOMENTE o
    bloqueio manual. Se houver qualquer outro motivo de bloqueio ativo
    (reconciliação divergente/estado ambíguo, relógio fora de sincronia,
    limite de falhas de API), o sistema permanece bloqueado e o motivo é
    devolvido em português -- nunca liberado por engano.

    Correction v1.3 #3: exige origem local ou token de controle válido
    (ver require_local_or_authenticated) antes de liberar qualquer coisa."""
    require_local_or_authenticated(request)

    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = False
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)

        if state.trading_blocked:
            repo.record_security_event(
                session, "KILL_SWITCH_DISENGAGED_OUTROS_BLOQUEIOS_ATIVOS",
                f"Bloqueio de emergência desativado, mas as operações continuam bloqueadas: {state.block_reason}",
            )
            return {
                "kill_switch_engaged": False,
                "trading_blocked": True,
                "mensagem": (
                    "Bloqueio de emergência desativado, mas as operações continuam "
                    f"bloqueadas por outro motivo: {state.block_reason}."
                ),
            }

        repo.record_security_event(
            session, "KILL_SWITCH_DISENGAGED", "Desativação manual do bloqueio de emergência pelo painel."
        )
        return {
            "kill_switch_engaged": False,
            "trading_blocked": False,
            "mensagem": "Bloqueio de emergência desativado. Operações liberadas.",
        }


_ACTIVATABLE_FROM = frozenset({"OBSERVANDO", "PAUSADO"})


@router.post("/operational-state/activate")
def activate_operational_state(request: Request):
    """Fase 2, item 7.8: the ONLY way new entries ever become authorized --
    a process/session always comes up in OBSERVANDO (see
    app/api/main.py::build_orchestrator), never ATIVO. Reducing safety, so
    it requires local origin or a valid control token (item 7.3/7.8: 'nenhum
    endpoint permite mudar REPLAY/PAPER_LIVE/BYBIT_DEMO' -- this never
    touches `mode`, only `operational_state`, which is a different axis)."""
    require_local_or_authenticated(request)

    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)

        # Correção operacional do poll loop v1.0: recusa ativar novas
        # entradas se o motor de mercado estiver DEGRADADO/PARADO ou com
        # heartbeat vencido -- nunca confia só no fato de este próprio
        # endpoint HTTP ter respondido (era exatamente esse o defeito
        # original: servidor web saudável não prova motor de mercado vivo).
        from app.api.poll_engine import engine_unhealthy

        poll_health = getattr(request.app.state, "poll_health", None)
        if poll_health is not None and engine_unhealthy(poll_health, orch.settings.poll_heartbeat_max_age_seconds):
            return {
                "operational_state": state.operational_state,
                "mensagem": (
                    f"Não é possível ativar novas entradas: o motor de mercado está "
                    f"'{poll_health.status.value}' ou com heartbeat vencido. Consulte "
                    "/api/state para detalhes antes de tentar novamente."
                ),
            }

        if state.trading_blocked:
            return {
                "operational_state": state.operational_state,
                "mensagem": (
                    f"Não é possível ativar novas entradas: operações estão bloqueadas "
                    f"({state.block_reason})."
                ),
            }
        if state.initialization_not_reconciled:
            return {
                "operational_state": state.operational_state,
                "mensagem": (
                    "Não é possível ativar novas entradas: a reconciliação inicial ainda não "
                    "foi concluída."
                ),
            }
        if state.operational_state not in _ACTIVATABLE_FROM:
            return {
                "operational_state": state.operational_state,
                "mensagem": (
                    f"Não é possível ativar a partir do estado operacional atual "
                    f"({state.operational_state!r}); só é permitido a partir de OBSERVANDO ou PAUSADO."
                ),
            }

        state.operational_state = "ATIVO"
        if state.active_session_id is not None:
            from app.persistence.models import OperationalSession

            op_session = session.get(OperationalSession, state.active_session_id)
            if op_session is not None:
                op_session.status = "ATIVO"
        repo.record_security_event(
            session, "OPERATIONAL_STATE_ACTIVATED",
            "Novas entradas ativadas manualmente pelo operador.",
        )
        return {
            "operational_state": "ATIVO",
            "mensagem": "Novas entradas ativadas. A estratégia pode abrir novas posições.",
        }


@router.post("/operational-state/pause")
def pause_operational_state(request: Request):
    """Fase 2, item 7.8: pausing, like engaging the kill switch, only ever
    INCREASES safety -- it requires no authentication, same as
    /kill-switch/engage. Never touches `mode`. Monitoring, reconciliation,
    and reducing/closing exposure all continue while paused."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.operational_state = "PAUSADO"
        if state.active_session_id is not None:
            from app.persistence.models import OperationalSession

            op_session = session.get(OperationalSession, state.active_session_id)
            if op_session is not None:
                op_session.status = "PAUSADO"
        repo.record_security_event(
            session, "OPERATIONAL_STATE_PAUSED",
            "Novas entradas pausadas manualmente pelo operador.",
        )
        return {
            "operational_state": "PAUSADO",
            "mensagem": "Novas entradas pausadas. Monitoramento, reconciliação e saídas continuam ativos.",
        }
