from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.core.config import RunMode
from app.metrics.engine import (
    UNAVAILABLE,
    ClosedTrade,
    OrderFillView,
    PositionMarkView,
    compute_cost_metrics,
    compute_equity,
    compute_metrics,
    compute_period_performance,
    compute_unrealized_pnl,
)
from app.core.timeframe import CANONICAL_OPERATIONAL_TIMEFRAME, canonical_timeframe
from app.market_data.base import CandleTick
from app.persistence import repo
from app.strategy.aggregator import CandleAggregator
from app.persistence.db import session_scope
from app.persistence.models import OperationalSession
from app.sessions import resolve_accounting_base, resolve_starting_balance

router = APIRouter()

_VALID_SCOPES = ("lifetime", "session", "daily")

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



# Fase 3.2 (ajuste multiativo): o banner do card do gráfico precisa
# refletir o MODO EFETIVO. Antes era um literal fixo no HTML dizendo
# "PAPER LIVE MULTIATIVO" mesmo quando o processo estava em REPLAY --
# misturava dois modos com significados completamente diferentes (um lê
# série gravada/sintética, o outro consome mercado real e simula só a
# execução).
_CHART_BANNERS = {
    RunMode.REPLAY: "REPLAY {escopo}— DADOS HISTÓRICOS/SINTÉTICOS — SEM MERCADO REAL",
    RunMode.PAPER_LOCAL: "PAPER LOCAL {escopo}— DADOS SINTÉTICOS — SEM MERCADO E SEM ORDEM REAL",
    RunMode.PAPER_LIVE: "PAPER LIVE {escopo}— SIMULAÇÃO LOCAL — SEM ORDEM NA CORRETORA",
    RunMode.BYBIT_DEMO: "BYBIT DEMO — MONOATIVO — CONTA DEMO DA CORRETORA, SEM DINHEIRO REAL",
}

# Modos cuja série de candles NÃO é cotação real de mercado.
_SYNTHETIC_DATA_MODES = (RunMode.REPLAY, RunMode.PAPER_LOCAL)
_SYNTHETIC_DATA_DISCLAIMER = "Dados REPLAY sintéticos — sem cotação real"


def _chart_banner(orch) -> str:
    mode = orch.settings.mode
    if mode == RunMode.BYBIT_DEMO:
        # BYBIT_DEMO permanece monoativo nesta fase (Fase 3 multiativo,
        # item 1) -- o banner nunca insinua carteira multiativo.
        return _CHART_BANNERS[mode]
    escopo = "MULTIATIVO " if len(orch.settings.symbols) > 1 else ""
    return _CHART_BANNERS.get(mode, _DEFAULT_BANNER).format(escopo=escopo)


def _data_disclaimer(orch) -> str | None:
    """Aviso explícito de que a série exibida não é cotação real. `None`
    nos modos que consomem mercado de verdade -- nunca um texto vazio que
    o painel pudesse renderizar como se fosse um aviso."""
    if orch.settings.mode in _SYNTHETIC_DATA_MODES:
        return _SYNTHETIC_DATA_DISCLAIMER
    return None


def _poll_health_dict(request: Request) -> dict:
    poll_health = getattr(request.app.state, "poll_health", None)
    if poll_health is None:
        from app.api.poll_engine import PollHealth

        poll_health = PollHealth()
    return poll_health.as_dict()



def _market_processing_status(request, orch) -> str:
    """Fase 3.2 (item 3 da correção final do PO): "o laço de mercado está
    processando candles?" -- um conceito, e SÓ ele. Antes o painel exibia
    "OPERAÇÕES: ATIVAS" para representar ao mesmo tempo processo rodando e
    autorização de entrada, o que aparecia contraditório ao lado de
    "ESTADO OPERACIONAL: OBSERVANDO (novas entradas desativadas)".

    Derivado do estado real: a saúde do motor de mercado (heartbeat do
    poll engine) e, no multiativo, a saúde por símbolo."""
    engine_status = _poll_health_dict(request).get("poll_loop_status")
    if engine_status in ("PARADO", "ENCERRANDO"):
        return engine_status
    symbols_health = _symbols_health_dict(request, orch).get("symbols_health") or {}
    portfolio = (symbols_health.get("portfolio") or {}).get("status")
    if portfolio in ("PARADO", "DEGRADADO", "ENCERRANDO"):
        return portfolio
    if engine_status == "DEGRADADO":
        return "DEGRADADO"
    if engine_status in (None, "INICIANDO"):
        return "INICIANDO"
    return "ATIVO"


def _new_entries_status(state) -> str:
    """"Novas entradas estão autorizadas?" -- o outro conceito, separado.
    Derivado exclusivamente do estado real persistido, nunca de um
    literal."""
    if state.kill_switch_engaged:
        return "BLOQUEADAS_EMERGENCIA"
    if state.trading_blocked:
        return "BLOQUEADAS"
    if state.operational_state == "ATIVO":
        return "ATIVADAS"
    return "DESATIVADAS"


def _configured_symbols(orch) -> list[str]:
    """Fase 3 multiativo: works for both a plain `Orchestrator` (monoativo)
    and a `MultiSymbolOrchestrator` -- both expose `.settings.symbols`."""
    return list(orch.settings.symbols)



def _sub_orchestrator(orch, symbol: str):
    """O `Orchestrator` responsável por `symbol` -- o próprio objeto no
    monoativo, ou a entrada correspondente do `MultiSymbolOrchestrator`."""
    return orch.orchestrators[symbol] if hasattr(orch, "orchestrators") else orch


def _strategy_state_for_symbol(orch, symbol: str) -> dict:
    """Fase 3.2: estado estratégico (timeframe, aquecimento, integridade do
    bucket, último candle estratégico fechado e o parcial em formação).
    Estritamente somente-leitura: nada aqui decide nada."""
    return _sub_orchestrator(orch, symbol).strategy_state()


def _resolve_mark_price(orch, session, symbol: str, timeframe: str = "1m", candles=None):
    """Fase 3.1.1: fonte única de marcação a mercado, compartilhada entre
    `/api/chart-data` e `/api/portfolio-summary` -- nunca duas
    implementações divergentes do mesmo conceito. Mesma prioridade
    honesta de sempre: preço visual (candle em formação, quando o
    provider expõe um) -> fechamento do último candle persistido -> nada
    (nunca finge um preço). `candles`, quando já carregado pelo chamador
    (ex.: chart-data já buscou a janela pedida), evita uma segunda
    consulta -- passe `None` para que esta função busque só 1 linha."""
    visual = getattr(orch, "visual_price_state", {}).get(symbol)
    if visual is not None:
        return visual["price"], "forming_candle", visual["at"].isoformat()
    if candles is None:
        candles = repo.recent_candles(session, symbol, timeframe, limit=1)
    if candles:
        last = candles[-1]
        return last.close, "last_closed_candle", last.open_time.isoformat()
    return None, None, None


def _scope_since(scope: str, session_started_at: datetime | None, base_started_at: datetime | None) -> datetime | None:
    """Fase 3.1.1 (último gate contábil da auditoria do PO): traduz o
    `scope` pedido num corte de tempo para filtrar POSIÇÕES FECHADAS e
    FUNDING -- nunca para posições abertas (seu estado atual sempre
    participa integralmente; ver docs/PAINEL_FINANCEIRO.md, seção
    "Escopos"). SEMPRE limitado (nunca antes) pelo início da BASE
    CONTÁBIL atual (`app.sessions.resolve_accounting_base`) -- nenhum
    escopo, nem mesmo `lifetime`, pode alcançar dados de uma base contábil
    anterior a um reset de `paper_starting_balance_usd`.

    - `lifetime`: "desde o início da base contábil ATUAL" -- NUNCA "todo o
      histórico do banco" quando já existiu um reset (ver
      `resolve_accounting_base`). Sem nenhuma base resolvida (nenhuma
      sessão ativa ainda), continua sem corte algum.
    - `session`: desde o início da sessão operacional ativa (`started_at`)
      -- já é, por construção, `>= base_started_at` (a base nunca começa
      DEPOIS da sessão que a define).
    - `daily`: desde 00:00 UTC do dia corrente -- filtro real de UTC.
    """
    if scope == "session":
        candidate = session_started_at
    elif scope == "daily":
        candidate = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # "lifetime"
        candidate = None

    if base_started_at is None:
        return candidate
    if candidate is None:
        return base_started_at
    return max(candidate, base_started_at)


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
            # Fase 3.2 (item 3): DOIS conceitos separados, cada um com o
            # seu campo -- nunca mais um "OPERAÇÕES: ATIVAS" ambíguo
            # representando processo rodando E autorização de entrada.
            "market_processing_status": _market_processing_status(request, orch),
            "new_entries_status": _new_entries_status(state),
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
    symbols = _configured_symbols(orch)
    # Fase 3.2 (item 12 da decisão do PO): acréscimo ADITIVO -- a chave
    # "symbols" continua idêntica (lista de strings), então todo consumidor
    # anterior segue funcionando byte a byte.
    per_symbol = {}
    for symbol in symbols:
        state = _strategy_state_for_symbol(orch, symbol)
        per_symbol[symbol] = {
            "market_data_timeframe": state["market_data_timeframe"],
            "strategy_timeframe": state["strategy_timeframe"],
            "strategy_timeframe_minutes": state["strategy_timeframe_minutes"],
            "warmup": state["warmup"],
            "bucket_integrity": state["bucket_integrity"],
        }
    return {"symbols": symbols, "per_symbol": per_symbol}


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
def get_costs(request: Request, symbol: str | None = None):
    """Fase 2, item 7.6/7.9; contrato definitivo do último gate contábil
    da auditoria do PO (Fase 3.1.1): fees acumuladas e o impacto
    financeiro REAL do slippage (já multiplicado por `filled_qty`, nunca a
    diferença unitária de preço sozinha) -- nunca um zero fabricado quando
    desconhecido. `?symbol=` opcional filtra para um único símbolo, nunca
    misturando notionais de ativos diferentes na mesma soma quando usado.

    ESCOPADO PELA BASE CONTÁBIL ATIVA por padrão -- mesma resolução de
    `/api/portfolio-summary` (`app.sessions.resolve_accounting_base`).
    Nunca soma custos de uma base anterior a um reset de
    `paper_starting_balance_usd` junto com o patrimônio/P&L da base
    atual (misturaria universos financeiros diferentes, exatamente o
    problema que motivou este gate). Fonte: `repo.orders_with_executions_since`
    -- para cada ordem, `avg_fill_price`/`filled_qty` são RECALCULADOS a
    partir apenas dos fills (`Execution` rows) cujo `executed_at` cai na
    base ativa -- NUNCA reaproveita `Order.avg_fill_price`/`filled_qty`
    (que agregam TODOS os fills da ordem, de qualquer época -- misturaria
    fills de antes e depois de um reset numa ordem que porventura tenha
    ambos)."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active_session = repo.get_active_session(session, state)
        base_started_at, _base_session = resolve_accounting_base(session, active_session)

        views = []
        for order, executions in repo.orders_with_executions_since(session, symbol, since=base_started_at):
            total_qty = sum(e.fill_qty for e in executions)
            if total_qty <= 0:
                continue
            avg_price = sum(e.fill_qty * e.fill_price for e in executions) / total_qty
            fees_total = sum(e.fee for e in executions)
            views.append(OrderFillView(
                side=order.side, reference_price=order.reference_price,
                avg_fill_price=avg_price, filled_qty=total_qty, fees_total=fees_total,
            ))

        result = compute_cost_metrics(views)
        payload = result.__dict__.copy()
        payload["accounting_base_started_at"] = base_started_at.isoformat() if base_started_at is not None else None
        return payload


def _metrics_for_trades(
    orch, trades: list[ClosedTrade], open_exposure_usd: float, funding_total: float | None,
) -> dict:
    # Fase 3.1.1: única fonte do saldo inicial -- Settings.paper_starting_balance_usd
    # (nunca mais um literal solto aqui; ver docs/PAINEL_FINANCEIRO.md).
    result = compute_metrics(
        trades, starting_balance=orch.settings.paper_starting_balance_usd,
        open_exposure_usd=open_exposure_usd, funding_total=funding_total,
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
        result = _metrics_for_trades(orch, trades, open_exposure, funding)

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
            per_symbol[symbol] = _metrics_for_trades(orch, symbol_trades, symbol_exposure, symbol_funding)

        # Fase 3.2 (item 13): bloqueios pelo gate de custos, cobertura
        # média na entrada, timeframe estratégico e buckets incompletos --
        # tudo ADITIVO (nenhuma chave anterior mudou de nome ou de
        # significado). As razões globais continuam recalculadas a partir
        # dos totais globais em `_metrics_for_trades`, nunca da média
        # simples dos percentuais por símbolo.
        global_gate = repo.cost_gate_stats(session)
        result["cost_gate"] = {
            "evaluated": global_gate["evaluated"],
            "blocked_entries": global_gate["blocked"],
            # `None` (nunca 0.0 inventado) quando não houve nenhuma
            # avaliação com cobertura calculável.
            "avg_coverage_ratio_at_entry": global_gate["avg_coverage_ratio"],
        }
        incomplete_total = 0
        for symbol in _configured_symbols(orch):
            state = _strategy_state_for_symbol(orch, symbol)
            symbol_gate = repo.cost_gate_stats(session, symbol)
            incomplete_total += state["bucket_integrity"]["incomplete_buckets"]
            per_symbol[symbol]["cost_gate"] = {
                "evaluated": symbol_gate["evaluated"],
                "blocked_entries": symbol_gate["blocked"],
                "avg_coverage_ratio_at_entry": symbol_gate["avg_coverage_ratio"],
            }
            per_symbol[symbol]["strategy_timeframe"] = state["strategy_timeframe"]
            per_symbol[symbol]["strategy_timeframe_minutes"] = state["strategy_timeframe_minutes"]
            per_symbol[symbol]["bucket_integrity"] = state["bucket_integrity"]

        first = _configured_symbols(orch)[0] if _configured_symbols(orch) else None
        result["strategy_timeframe"] = (
            _strategy_state_for_symbol(orch, first)["strategy_timeframe"] if first else None
        )
        result["incomplete_buckets"] = incomplete_total
        result["per_symbol"] = per_symbol
        return result


def _funding_for_symbols(orch, session, symbols, since):
    if orch.funding_provider is None:
        return None, None
    paid, received = 0.0, 0.0
    for s in symbols:
        p, r = repo.funding_paid_received(session, s, since=since)
        paid += p
        received += r
    return paid, received


def _portfolio_state(orch, session, symbol: str | None, base_started_at):
    """Fase 3.1.1 (último gate contábil da auditoria do PO): componentes
    do bloco `portfolio` -- "equity não tem escopo" continua valendo (o
    `?scope=` da requisição NUNCA afeta este bloco), mas "sem escopo"
    NUNCA significou "todo o histórico do banco": significa "desde o
    início da BASE CONTÁBIL atual" (`base_started_at`, de
    `app.sessions.resolve_accounting_base`) -- um reset de
    `paper_starting_balance_usd` nunca soma o resultado de uma base
    anterior à nova âncora. `realized_price_pnl`/`fees_paid` somam
    posições ABERTAS (sempre, sem corte -- por construção nunca existem
    posições abertas atravessando um reset, já que
    `_guard_starting_balance_reset` só permite resetar sem posições
    abertas) e FECHADAS (cortadas por `closed_at >= base_started_at`).
    Taxas vêm da fonte canônica `repo.execution_fees`, cortadas por
    `Execution.executed_at >= base_started_at`."""
    symbols = [symbol] if symbol else _configured_symbols(orch)

    closed_realized = 0.0
    for s in symbols:
        for p in repo.closed_positions(session, s, since=base_started_at):
            closed_realized += p.realized_pnl

    fees_paid = sum(repo.execution_fees(session, s, since=base_started_at) for s in symbols)

    open_positions = repo.open_positions(session, symbol)
    open_realized = sum(p.realized_pnl for p in open_positions)
    exposure_usd = sum(p.qty * p.avg_entry_price for p in open_positions)

    marks = []
    for p in open_positions:
        price, source, at = _resolve_mark_price(orch, session, p.symbol)
        marks.append(PositionMarkView(
            symbol=p.symbol, side=p.side, qty=p.qty, avg_entry_price=p.avg_entry_price,
            mark_price=price, mark_source=source, mark_at=at,
        ))
    unrealized = compute_unrealized_pnl(marks)

    funding_paid, funding_received = _funding_for_symbols(orch, session, symbols, since=base_started_at)

    return {
        "realized_price_pnl": closed_realized + open_realized,
        "fees_paid": fees_paid,
        "funding_paid": funding_paid,
        "funding_received": funding_received,
        "unrealized": unrealized,
        "exposure_usd": exposure_usd,
        "open_positions_count": len(open_positions),
    }


def _period_state(orch, session, since, symbol: str | None):
    """Fase 3.1.1: componentes do bloco `period_performance` -- um
    recorte de DESEMPENHO, nunca de patrimônio. `realized_price_pnl`
    soma EXCLUSIVAMENTE posições FECHADAS cujo `closed_at` cai no
    recorte (`realized_pnl_attribution="position_close"` -- não existe
    ledger de P&L por fill individual, ver `compute_period_performance`)
    -- NUNCA inclui o P&L parcial já realizado de uma posição AINDA
    aberta (decisão explícita do PO: isso vazaria histórico de uma
    posição aberta antes do recorte para dentro de todo `daily`
    subsequente). Taxas/funding filtrados pelo instante do próprio
    evento (`Execution.executed_at`/`FundingEvent.occurred_at`)."""
    symbols = [symbol] if symbol else _configured_symbols(orch)

    closed_realized = 0.0
    closed_trades_count = 0
    for s in symbols:
        for p in repo.closed_positions(session, s, since=since):
            closed_realized += p.realized_pnl
            closed_trades_count += 1

    fees_paid = sum(repo.execution_fees(session, s, since=since) for s in symbols)
    fills_count = sum(repo.execution_fills_count(session, s, since=since) for s in symbols)
    funding_paid, funding_received = _funding_for_symbols(orch, session, symbols, since=since)

    return {
        "realized_price_pnl": closed_realized,
        "fees_paid": fees_paid,
        "funding_paid": funding_paid,
        "funding_received": funding_received,
        "fills_count": fills_count,
        "closed_trades_count": closed_trades_count,
    }


@router.get("/portfolio-summary")
def get_portfolio_summary(request: Request, scope: str = "lifetime"):
    """Fase 3.1.1 (último gate contábil da auditoria do PO): patrimônio
    (equity) calculado SOB DEMANDA -- nunca persistido nesta fase
    (account_snapshots permanece reservada para uma fase futura de série
    histórica, decisão do PO). Contrato completo em docs/PAINEL_FINANCEIRO.md.

    Dois blocos DELIBERADAMENTE separados, nunca confundidos:
    - `portfolio`: `current_equity = frozen_starting_balance +
      base_realized_price_pnl + current_unrealized_price_pnl -
      base_execution_fees + base_funding_net` -- SEMPRE o mesmo valor,
      IDÊNTICO independente de `?scope=`. "base" aqui é a BASE CONTÁBIL
      atual (`app.sessions.resolve_accounting_base`) -- desde o último
      reset de `paper_starting_balance_usd`, NUNCA "todo o histórico do
      banco" quando já existiu um reset. `starting_balance` vem CONGELADO
      no snapshot da sessão que estabeleceu a base
      (`app.sessions.resolve_starting_balance`), nunca lido de `Settings`
      ao vivo.
    - `period_performance`: recorte de DESEMPENHO pelo `scope` pedido --
      nunca chamado de equity, nunca soma `starting_balance`; também
      nunca alcança dados de antes do início da base contábil atual."""
    if scope not in _VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope inválido: {scope!r}. Valores aceitos: {list(_VALID_SCOPES)}.",
        )
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active_session = repo.get_active_session(session, state)
        session_started_at = active_session.started_at if active_session is not None else None
        base_started_at, base_session = resolve_accounting_base(session, active_session)
        since = _scope_since(scope, session_started_at, base_started_at)
        since_iso = since.isoformat() if since is not None else None

        starting_balance, starting_balance_source = resolve_starting_balance(active_session)

        # --- portfolio (equity): SEMPRE lifetime-DA-BASE, nunca afetado
        # pelo ?scope= da requisição. ------------------------------------
        consolidated = _portfolio_state(orch, session, symbol=None, base_started_at=base_started_at)
        equity = compute_equity(
            starting_balance=starting_balance, starting_balance_source=starting_balance_source,
            realized_price_pnl=consolidated["realized_price_pnl"],
            fees_paid=consolidated["fees_paid"],
            funding_paid=consolidated["funding_paid"], funding_received=consolidated["funding_received"],
            unrealized=consolidated["unrealized"],
            open_positions_count=consolidated["open_positions_count"],
            exposure_usd=consolidated["exposure_usd"],
        )

        # --- period_performance: recorte pelo scope pedido, sempre
        # limitado pela base contábil atual (_scope_since já garante). ---
        period_consolidated = _period_state(orch, session, since, symbol=None)
        period = compute_period_performance(
            scope=scope, since_iso=since_iso,
            realized_price_pnl=period_consolidated["realized_price_pnl"],
            fees_paid=period_consolidated["fees_paid"],
            funding_paid=period_consolidated["funding_paid"], funding_received=period_consolidated["funding_received"],
            fills_count=period_consolidated["fills_count"],
            closed_trades_count=period_consolidated["closed_trades_count"],
        )

        per_symbol = {}
        for symbol in _configured_symbols(orch):
            comp = _portfolio_state(orch, session, symbol=symbol, base_started_at=base_started_at)
            has_funding = comp["funding_paid"] is not None
            per_symbol[symbol] = {
                "realized_price_pnl": comp["realized_price_pnl"],
                "unrealized_pnl": comp["unrealized"].total,
                "unrealized_complete": comp["unrealized"].complete,
                "fees_paid": comp["fees_paid"],
                "funding_paid": comp["funding_paid"] if has_funding else UNAVAILABLE,
                "funding_received": comp["funding_received"] if has_funding else UNAVAILABLE,
                "funding_net": (
                    (comp["funding_received"] - comp["funding_paid"]) if has_funding else UNAVAILABLE
                ),
                "exposure_usd": comp["exposure_usd"],
                "open_positions_count": comp["open_positions_count"],
                "positions": [p.__dict__ for p in comp["unrealized"].per_position],
            }
            # Fase 3.2 (ajuste multiativo): o preço de marcação DO SÍMBOLO,
            # pela MESMA `_resolve_mark_price` já usada por /api/chart-data
            # e pela equity -- nunca uma segunda implementação. Antes o
            # painel só conseguia mostrar preço quando havia posição
            # aberta, e a tabela "Resumo por Símbolo" exibia N/D para
            # todos os ativos, escondendo justamente a prova de que cada
            # série tem preço próprio. `None` continua sendo `None` quando
            # realmente não há preço -- nunca um valor inventado nem o
            # preço de outro símbolo.
            mark_price, mark_source, mark_at = _resolve_mark_price(orch, session, symbol)
            per_symbol[symbol]["mark_price"] = mark_price
            per_symbol[symbol]["mark_price_source"] = mark_source
            per_symbol[symbol]["mark_price_at"] = mark_at

        return {
            "portfolio": {
                "accounting_base_started_at": base_started_at.isoformat() if base_started_at is not None else None,
                "starting_balance": equity.starting_balance,
                "starting_balance_source": equity.starting_balance_source,
                "realized_price_pnl": equity.realized_price_pnl,
                "unrealized_pnl": equity.unrealized_pnl,
                "fees_paid": equity.fees_paid,
                "funding_paid": equity.funding_paid,
                "funding_received": equity.funding_received,
                "funding_net": equity.funding_net,
                "realized_net_pnl": equity.realized_net_pnl,
                "equity": equity.equity,
                "equity_complete": equity.equity_complete,
                "open_positions_count": equity.open_positions_count,
                "exposure_usd": equity.exposure_usd,
            },
            "period_performance": {
                "scope": period.scope,
                "since": period.since,
                "realized_price_pnl": period.realized_price_pnl,
                "fees_paid": period.fees_paid,
                "funding_paid": period.funding_paid,
                "funding_received": period.funding_received,
                "funding_net": period.funding_net,
                "realized_net_pnl": period.realized_net_pnl,
                "fills_count": period.fills_count,
                "closed_trades_count": period.closed_trades_count,
                "realized_pnl_attribution": period.realized_pnl_attribution,
            },
            "per_symbol": per_symbol,
        }


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




def _cost_gate_summary(orch, session, symbol: str) -> dict:
    """Fase 3.2: o contrato do gate de viabilidade líquida em vigor mais o
    que ele já bloqueou para ESTE símbolo. `required_ratio`/`estimate_source`
    vêm do `CostModel` realmente ligado ao motor de risco -- nunca de uma
    cópia da configuração que pudesse divergir dele."""
    # O `RiskEngine` é COMPARTILHADO por todos os símbolos (uma única
    # instância em `build_orchestrator`), mas só o `Orchestrator` por
    # símbolo o expõe como atributo -- por isso a leitura passa por
    # `_sub_orchestrator`, nunca por `orch.risk_engine` direto (que
    # levantaria AttributeError num MultiSymbolOrchestrator).
    model = getattr(_sub_orchestrator(orch, symbol).risk_engine, "cost_model", None)
    stats = repo.cost_gate_stats(session, symbol)
    return {
        "applied": model is not None,
        "required_ratio": model.minimum_cost_coverage_ratio if model else None,
        "expected_move_atr_multiple": model.expected_move_atr_multiple if model else None,
        "estimate_source": model.source if model else None,
        "fee_rate": model.fee_rate if model else None,
        "slippage_bps": model.slippage_bps if model else None,
        "evaluated": stats["evaluated"],
        "blocked_entries": stats["blocked"],
        "avg_coverage_ratio_at_entry": stats["avg_coverage_ratio"],
    }


def _strategy_candles(orch, symbol: str, candles) -> list[dict]:
    """Reconstrói, apenas para EXIBIÇÃO, a série de candles estratégicos a
    partir dos candles de 1 minuto já carregados para o gráfico -- mesma
    agregação determinística usada na decisão
    (`app/strategy/aggregator.py`), num agregador NOVO e descartável, para
    nunca tocar no estado do agregador que está de fato operando.

    Buckets incompletos entram na lista marcados (`complete=false`,
    `partial=true`, com slots esperados/recebidos/ausentes) -- nunca
    escondidos, nunca preenchidos."""
    sub = _sub_orchestrator(orch, symbol)
    replay = CandleAggregator(
        symbol=symbol,
        timeframe_minutes=sub.aggregator.timeframe_minutes,
        operational_minutes=sub.aggregator.operational_minutes,
    )
    ticks = [
        CandleTick(
            symbol=c.symbol, timeframe=canonical_timeframe(c.timeframe), open_time=c.open_time,
            open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
            source=c.source, received_at=c.received_at,
        )
        for c in candles
    ]
    return [b.to_dict() for b in replay.hydrate(ticks)]


def _strategy_config_for_symbol(orch, symbol: str) -> dict:
    """Fase 3.1 (painel gráfico): a configuração REAL entregue à
    StrategyEngine do símbolo -- funciona tanto para `Orchestrator`
    (monoativo) quanto para `MultiSymbolOrchestrator` (cada símbolo tem sua
    própria instância, todas compartilhando o mesmo `StrategyConfig` por
    valor -- ver app/api/main.py::build_orchestrator)."""
    sub_orch = orch.orchestrators[symbol] if hasattr(orch, "orchestrators") else orch
    cfg = sub_orch.strategy_engine.config
    return {
        "fast_period": cfg.fast_period, "slow_period": cfg.slow_period, "atr_period": cfg.atr_period,
        "min_atr_pct_of_price": cfg.min_atr_pct_of_price, "max_atr_pct_of_price": cfg.max_atr_pct_of_price,
        "stop_loss_atr_multiple": cfg.stop_loss_atr_multiple, "take_profit_atr_multiple": cfg.take_profit_atr_multiple,
    }


@router.get("/chart-data")
def get_chart_data(request: Request, symbol: str, limit: int = 500):
    """Fase 3.1 (painel gráfico): rota somente-leitura, estritamente
    observacional -- nenhuma chamada de rede aqui (candles vêm da tabela
    `candles`; preço visual vem de um cache em memória já atualizado pelo
    orquestrador a cada tick, nunca por uma consulta nova à corretora).

    `limit` é sempre NORMALIZADO (nunca rejeitado) para o intervalo
    `[50, 2000]` -- contrato documentado em docs/PAINEL_GRAFICO.md."""
    orch = request.app.state.orchestrator
    if symbol not in _configured_symbols(orch):
        raise HTTPException(status_code=404, detail=f"Símbolo não configurado: {symbol}")
    limit = max(50, min(limit, 2000))
    # Fase 3.2 (decisão Q4 do PO): a representação canônica única, nunca um
    # literal solto. `repo.recent_candles` aceita os aliases legados ("1")
    # e deduplica logicamente preferindo o registro canônico, então um
    # banco BYBIT_DEMO anterior a esta fase continua aparecendo no gráfico
    # -- o defeito que deixava `candles: []` nesse modo.
    timeframe = CANONICAL_OPERATIONAL_TIMEFRAME

    with session_scope(orch.session_factory) as session:
        from sqlalchemy import select

        from app.persistence.models import Order, RiskEvaluation, StrategySignal

        candles = repo.recent_candles(session, symbol, timeframe, limit=limit)

        # Preço visual: candle em formação (quando o provider expõe um,
        # nunca REPLAY/PAPER_LOCAL) -- fallback honesto para o fechamento
        # do último candle persistido, nunca fingindo tempo real. Mesma
        # função usada por /api/portfolio-summary (_resolve_mark_price) --
        # uma única fonte de marcação a mercado.
        visual_price, visual_price_source, visual_price_at = _resolve_mark_price(
            orch, session, symbol, timeframe, candles=candles,
        )

        open_pos = repo.open_positions(session, symbol)
        position = None
        if open_pos:
            p = open_pos[0]
            position = {
                "side": p.side, "qty": p.qty, "avg_entry_price": p.avg_entry_price,
                "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                "opened_at": p.opened_at.isoformat(),
            }

        # Correção final da auditoria (Fase 3.1): o marcador usa
        # EXCLUSIVAMENTE `signal.source_candle_open_time` -- a identidade
        # determinística do candle.open_time gravada no momento da criação
        # do sinal (app/strategy/engine.py::StrategyEngine.on_candle e
        # app/orchestrator.py, nunca inferida por preço ou por
        # created_at). Um sinal legado sem esse valor (coluna nullable,
        # nunca retroativamente preenchida -- ver migração v8) é OMITIDO
        # dos marcadores do gráfico; o sinal em si permanece visível em
        # /api/signals normalmente, apenas sem posição no gráfico.
        signal_rows = repo.recent_signals(session, limit=20, symbol=symbol)
        recent_signals = []
        for s in signal_rows:
            if s.direction not in ("BUY", "SELL"):
                continue
            if s.source_candle_open_time is None:
                continue
            candle_time = int(s.source_candle_open_time.timestamp())
            order_status = None
            risk_eval = session.execute(
                select(RiskEvaluation).where(RiskEvaluation.signal_id == s.id)
            ).scalars().first()
            if risk_eval is not None:
                order = session.execute(
                    select(Order).where(Order.risk_evaluation_id == risk_eval.id)
                ).scalars().first()
                if order is not None:
                    order_status = order.status
            recent_signals.append({
                "time": candle_time, "direction": s.direction,
                "price": s.observed_price, "justification": s.justification,
                "order_status": order_status,
                # Fase 3.1: `realized_pnl` por sinal individual não é
                # rastreável de forma confiável nesta fundação -- Position
                # é um agregado sem FK de volta ao sinal/ordem que a abriu.
                # Nunca fabricado; sempre `None` (o frontend exibe
                # "indisponível").
                "realized_pnl": None,
            })

        symbols_health = _symbols_health_dict(request, orch)["symbols_health"]
        symbol_health = symbols_health["per_symbol"].get(symbol)

        from datetime import datetime, timezone

        strategy_state = _strategy_state_for_symbol(orch, symbol)

        return {
            "symbol": symbol, "timeframe": timeframe,
            # Fase 3.2: banner do MODO EFETIVO e aviso explícito quando a
            # série não é cotação real -- o painel nunca mais decide isso
            # sozinho a partir de um literal fixo no HTML.
            "chart_banner": _chart_banner(orch),
            "data_disclaimer": _data_disclaimer(orch),
            # Fase 3.2: o gráfico OPERACIONAL de 1 minuto permanece
            # exatamente onde estava, com o mesmo nome de campo -- a
            # agregação é sempre acrescentada ao lado, nunca no lugar.
            "market_data_timeframe": strategy_state["market_data_timeframe"],
            "strategy_timeframe": strategy_state["strategy_timeframe"],
            "strategy_timeframe_minutes": strategy_state["strategy_timeframe_minutes"],
            "strategy_candles": _strategy_candles(orch, symbol, candles),
            "last_strategy_candle": strategy_state["last_strategy_candle"],
            "forming_strategy_candle": strategy_state["forming_strategy_candle"],
            "bucket_integrity": strategy_state["bucket_integrity"],
            "warmup": strategy_state["warmup"],
            # Indicadores AUTORITATIVOS (lidos do engine que decide), nunca
            # recalculados no navegador.
            "strategy_indicators": strategy_state["indicators"],
            "cost_gate": _cost_gate_summary(orch, session, symbol),
            "candles": [
                {
                    "time": int(c.open_time.timestamp()), "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close, "volume": c.volume,
                }
                for c in candles
            ],
            "visual_price": visual_price, "visual_price_at": visual_price_at,
            "visual_price_source": visual_price_source,
            "strategy_config": _strategy_config_for_symbol(orch, symbol),
            "position": position,
            "recent_signals": recent_signals,
            "symbol_health": symbol_health,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/equity-curve")
def get_equity_curve(request: Request):
    """Fase 3.1.1 (último gate contábil da auditoria do PO): mesma
    resolução CANÔNICA do saldo inicial E da base contábil usadas por
    `GET /api/portfolio-summary` -- `app.sessions.resolve_starting_balance`/
    `resolve_accounting_base`, lidas do snapshot CONGELADO da sessão que
    estabeleceu a base ativa, nunca de `Settings.paper_starting_balance_usd`
    ao vivo. A curva NUNCA desenha uma continuidade falsa através de um
    reset de saldo: `repo.closed_positions(since=base_started_at)` já
    exclui estruturalmente qualquer trade fechado ANTES do início da base
    atual -- o primeiro ponto da curva é sempre o saldo congelado da base,
    nunca um valor que incorpore resultado de uma base anterior. Terceiro
    hardcode independente de `1000.0` encontrado e corrigido nesta
    correção (os outros dois eram o antigo `_metrics_for_trades` e o
    literal `"1000.00"` do frontend). Curva REALIZADA apenas (nunca
    inclui P&L não realizado de posições abertas) -- mesma limitação
    documentada de `current_drawdown_money`/`max_drawdown_money`. Cada
    degrau usa `Position.fees_paid` (não a fonte canônica `Execution.fee`)
    -- uma taxa órfã não move esta curva; é uma visualização aproximada,
    não o patrimônio oficial (esse é sempre `GET /api/portfolio-summary`)."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active_session = repo.get_active_session(session, state)
        starting_balance, _source = resolve_starting_balance(active_session)
        base_started_at, _base_session = resolve_accounting_base(session, active_session)

        closed = sorted(
            [p for p in repo.closed_positions(session, since=base_started_at) if p.closed_at],
            key=lambda p: p.closed_at,
        )
        running = starting_balance
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
