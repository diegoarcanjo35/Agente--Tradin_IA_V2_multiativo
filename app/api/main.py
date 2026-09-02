"""FastAPI application: wires the whole pipeline together and serves the
dashboard + control API. Boots in REPLAY mode by default (no .env required).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.api import routes_control, routes_dashboard
from app.api.poll_engine import PollHealth, supervise_poll_loop, wait_for_in_flight_tick_before_shutdown
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, get_settings
from app.core.logging import get_logger, log_event, setup_logging
from app.execution.paper_local import PaperLocalExecutionEngine
from app.market_data.replay_provider import ReplayMarketDataProvider
from app.orchestrator import MultiSymbolOrchestrator, Orchestrator
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence import repo
from app.persistence.models import OperationalSession
from app.core.errors import ReplayFixtureMissingError
from app.core.freshness import freshness_policy_for_market_data
from app.core.timeframe import CANONICAL_OPERATIONAL_TIMEFRAME, bybit_interval
from app.risk.cost_model import (
    SOURCE_BYBIT_DEMO_ESTIMATE,
    SOURCE_PAPER_CONFIG,
    CostModel,
)
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from app.sessions import end_session, start_or_resume_session
from app.strategy.engine import StrategyConfig, StrategyEngine

STRATEGY_VERSION = "v1"

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = BASE_DIR / "fixtures"
FRONTEND_DIR = BASE_DIR / "frontend"

logger = get_logger(__name__)


def _build_shared_execution_pipeline(settings, price_provider, bybit_transport):
    """Fase 3 multiativo: the execution engine, clock provider and (when
    applicable) funding provider are SHARED across every configured symbol
    -- never one instance per symbol. `PaperLocalExecutionEngine`'s
    position/snapshot bookkeeping is already keyed by symbol internally
    (see app/execution/paper_local.py), so one shared instance is what
    gives a unified, correct cross-symbol view for exposure/reconciliation;
    N separate instances would fragment it. BYBIT_DEMO is guaranteed
    monoativo by Settings/get_settings() (Fase 3 multiativo, item 1), so
    that branch only ever runs for exactly one symbol regardless.

    Returns `(execution_engine, clock_provider, funding_provider, transport)`
    -- `transport` is None for REPLAY/PAPER_LOCAL (no network object at
    all), and is reused by `_build_market_data_provider` below for
    PAPER_LIVE/BYBIT_DEMO so both halves of the pipeline share the exact
    same HTTP transport instance."""
    if settings.mode in (RunMode.REPLAY, RunMode.PAPER_LOCAL):
        execution_engine = PaperLocalExecutionEngine(price_provider=price_provider)
        clock_provider = ReplayClockProvider(drift_seconds=0.0)
        return execution_engine, clock_provider, None, None

    if settings.mode == RunMode.PAPER_LIVE:
        # Fase 2, item 7.1: REAL Bybit Demo market data (public endpoints
        # only), execution stays entirely local/simulated. Deliberately
        # never calls require_bybit_credentials(), never builds an
        # authenticated pybit client or http_post, and never constructs
        # BybitDemoExecutionEngine -- PAPER_LIVE cannot reach the exchange's
        # private order-management endpoints even if this code had a bug,
        # because PaperLocalExecutionEngine (below) simply has no code path
        # that calls http_post at all.
        from app.execution.bybit_pybit_client import PybitTransport, build_public_pybit_client
        from app.market_data.bybit_provider import BybitServerTimeProvider

        if bybit_transport is not None:
            transport = bybit_transport
        else:
            pybit_client = build_public_pybit_client(
                settings.bybit_base_url, settings.bybit_ws_url,
                timeout_seconds=settings.bybit_http_timeout_seconds,
            )
            transport = PybitTransport(pybit_client)

        # Correção v1.1 #5: the configured fee/slippage are genuinely wired
        # here, not left as PaperLocalExecutionEngine's own hardcoded
        # defaults -- every fill this engine produces mathematically
        # reflects settings.paper_live_fee_rate/paper_live_slippage_bps.
        execution_engine = PaperLocalExecutionEngine(
            price_provider=price_provider,
            fee_rate=settings.paper_live_fee_rate,
            slippage_bps=settings.paper_live_slippage_bps,
        )
        clock_provider = BybitServerTimeProvider(settings.bybit_base_url, http_get=transport.http_get)
        return execution_engine, clock_provider, None, transport

    # BYBIT_DEMO
    from app.execution.bybit_demo import BybitDemoExecutionEngine
    from app.execution.bybit_pybit_client import PybitTransport, build_pybit_client
    from app.market_data.bybit_provider import BybitServerTimeProvider

    # require_bybit_credentials() already ran inside get_settings() for
    # BYBIT_DEMO, before this function is ever called -- re-checked here
    # defensively so build_orchestrator() is safe to call directly too.
    # This must happen BEFORE any client/transport is built, so a missing
    # credential fails before a single network call is even possible.
    settings.require_bybit_credentials()

    if bybit_transport is not None:
        transport = bybit_transport
    else:
        pybit_client = build_pybit_client(
            settings.bybit_base_url, settings.bybit_ws_url,
            settings.bybit_api_key, settings.bybit_api_secret,
            timeout_seconds=settings.bybit_http_timeout_seconds,
        )
        transport = PybitTransport(pybit_client)

    execution_engine = BybitDemoExecutionEngine(
        settings.bybit_base_url, http_post=transport.http_post, http_get=transport.http_get,
    )
    clock_provider = BybitServerTimeProvider(settings.bybit_base_url, http_get=transport.http_get)

    # Correção v1.1 #6: funding is only ever collected for BYBIT_DEMO --
    # the sole mode with real private-endpoint credentials. PAPER_LIVE
    # (which reaches the public-transport branch above) and REPLAY/
    # PAPER_LOCAL never get a funding_provider at all, so
    # app.metrics.engine reports funding as UNAVAILABLE for them, rather
    # than a simulated value mixed in with real collected data.
    from app.execution.funding import BybitFundingProvider

    funding_provider = BybitFundingProvider(transport.http_get, settings.bybit_base_url)
    return execution_engine, clock_provider, funding_provider, transport



def replay_fixture_for(symbol: str):
    """Fase 3.2 (correção final da auditoria do PO, item 1): a fixture de
    REPLAY **do símbolo**, obrigatoriamente própria.

    Cada símbolo lê `fixtures/replay_<simbolo>.json` -- dados 100%
    SINTÉTICOS gerados por `fixtures/generate_replay_fixture.py`. NÃO
    existe fallback: um símbolo sem fixture própria levanta
    `ReplayFixtureMissingError` em vez de emprestar a série de outro
    ativo.

    O fallback anterior (qualquer símbolo caía em `replay_btcusdt.json`)
    era inaceitável numa plataforma multiativo: produzia preço falso para
    o símbolo, sinais duplicados de outro ativo, métricas contaminadas e
    uma demonstração enganosa. Documentar que a série era "emprestada"
    não evitava nenhuma dessas consequências.

    A compatibilidade monoativa histórica (`SYMBOL=BTCUSDT`) segue
    naturalmente atendida pela própria fixture do BTC, sem precisar de
    fallback genérico."""
    candidate = FIXTURES_DIR / f"replay_{symbol.lower()}.json"
    if not candidate.exists():
        raise ReplayFixtureMissingError(
            f"Nenhuma fixture de REPLAY para o símbolo {symbol}. Cada símbolo configurado em "
            f"REPLAY/PAPER_LOCAL precisa da SUA própria série -- nunca a de outro ativo. "
            f"Arquivo esperado: {candidate}. Gere-o com fixtures/generate_replay_fixture.py "
            "(dados sintéticos) ou remova o símbolo da configuração. Nenhuma alteração foi feita."
        )
    return candidate


def assert_replay_fixtures_available(settings) -> None:
    """Valida TODOS os símbolos configurados de uma vez, no primeiro
    instante de `build_orchestrator` -- antes de abrir o banco, antes de
    criar/retomar sessão operacional e antes de qualquer candle ser
    persistido. Uma carteira em que UM símbolo não tem fixture falha
    inteira: nunca sobe pela metade, operando alguns símbolos e deixando
    outro com dados de terceiros."""
    if settings.mode not in (RunMode.REPLAY, RunMode.PAPER_LOCAL):
        return
    for symbol in settings.symbols:
        replay_fixture_for(symbol)


def _build_market_data_provider(settings, symbol: str, transport):
    """One instance per symbol (unlike the shared execution pipeline above)
    -- each symbol's backlog/cursor/staleness/gap state must be fully
    independent (Fase 3 multiativo, item 2). `settings` here is a
    PER-SYMBOL copy (`.symbol == symbol`); `transport`, when not None, is
    the SAME shared transport `_build_shared_execution_pipeline` built, so
    every symbol's provider reuses one HTTP client/connection rather than
    opening N."""
    if settings.mode in (RunMode.REPLAY, RunMode.PAPER_LOCAL):
        return ReplayMarketDataProvider(replay_fixture_for(symbol), symbol=symbol)

    from app.market_data.bybit_provider import BybitDemoMarketDataProvider

    # Fase 3.2 (decisão Q4 do PO): o formato de intervalo da Bybit ("1")
    # fica confinado à fronteira HTTP, derivado da representação canônica
    # -- nunca mais um literal solto que divergia do "1m" gravado pelo
    # provider de REPLAY e consultado pelo painel.
    return BybitDemoMarketDataProvider(
        settings.bybit_base_url, symbol,
        bybit_interval(CANONICAL_OPERATIONAL_TIMEFRAME),
        http_get=transport.http_get,
        initial_start=settings.market_data_initial_start,
    )



def build_cost_model(settings, execution_engine) -> CostModel:
    """Fase 3.2 (decisão Q3 do PO): os parâmetros de custo em vigor.

    - REPLAY / PAPER_LOCAL / PAPER_LIVE: lidos do PRÓPRIO motor de execução
      (`fee_rate`/`slippage_bps` de `PaperLocalExecutionEngine`), que é
      quem realmente aplica esses números em cada fill simulado. A
      estimativa é, portanto, EXATA por construção -- e nunca pode divergir
      do simulador, porque é o mesmo objeto. Origem declarada:
      `paper_config`.
    - BYBIT_DEMO: `bybit_taker_fee_rate`/`bybit_expected_slippage_bps`,
      campos próprios e explicitamente declarados como ESTIMATIVA
      OPERACIONAL CONFIGURADA -- nunca consultados da corretora, nunca
      reaproveitando os nomes do simulador PAPER. Origem declarada:
      `bybit_demo_estimate`.
    """
    fee_rate = getattr(execution_engine, "fee_rate", None)
    slippage_bps = getattr(execution_engine, "slippage_bps", None)
    if fee_rate is not None and slippage_bps is not None:
        source = SOURCE_PAPER_CONFIG
    else:
        fee_rate = settings.bybit_taker_fee_rate
        slippage_bps = settings.bybit_expected_slippage_bps
        source = SOURCE_BYBIT_DEMO_ESTIMATE
    return CostModel(
        fee_rate=float(fee_rate),
        slippage_bps=float(slippage_bps),
        source=source,
        expected_move_atr_multiple=settings.strategy_expected_move_atr_multiple,
        minimum_cost_coverage_ratio=settings.minimum_cost_coverage_ratio,
    )


def build_orchestrator(settings, bybit_transport=None) -> Orchestrator | MultiSymbolOrchestrator:
    """`bybit_transport`, when given, replaces the real pybit-backed
    transport used in BYBIT_DEMO/PAPER_LIVE mode with an object exposing the
    same `http_get(url, params)` / `http_post(url, payload)` interface (see
    tests/fakes/bybit_fake.py::FakeBybitTransport). This exists purely so
    tests can exercise this function's REAL wiring logic -- mode branching,
    engine/provider construction, clock provider selection, startup
    reconciliation -- against BYBIT_DEMO without ever importing pybit or
    touching the network. Production code never passes this argument.

    Fase 3 multiativo: returns a plain `Orchestrator` when exactly one
    symbol is configured (byte-for-byte the same object graph as before
    multiativo existed -- full backward compatibility), or a
    `MultiSymbolOrchestrator` (round-robin over one `Orchestrator` per
    symbol) when more than one is configured. Both expose the same
    `.tick()` / `.engine_degraded` / `.reconcile()` surface, so nothing
    downstream (poll_engine.py, the shutdown path) needs to know which one
    it has."""
    # Fase 3.2 (item 1 da correção final): fixtures de REPLAY conferidas
    # ANTES de abrir o banco -- a falha acontece sem criar sessão, sem
    # persistir candle e sem deixar estado parcial nenhum.
    assert_replay_fixtures_available(settings)

    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    risk_limits = RiskLimits(
        max_position_usd=settings.risk_max_position_usd,
        max_concurrent_positions=settings.risk_max_concurrent_positions,
        max_daily_loss_usd=settings.risk_max_daily_loss_usd,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        cooldown_after_losses=settings.risk_cooldown_after_losses,
        cooldown_minutes=settings.risk_cooldown_minutes,
        max_data_staleness_seconds=settings.risk_max_data_staleness_seconds,
        max_api_failures=settings.risk_max_api_failures,
        max_clock_drift_seconds=settings.risk_max_clock_drift_seconds,
    )
    # Shared across every symbol -- portfolio-wide exposure/position limits
    # (Fase 3 multiativo, item 6: RISK_MAX_TOTAL_EXPOSURE_USD/
    # RISK_MAX_CONCURRENT_POSITIONS are already computed across ALL open
    # positions regardless of symbol, unfiltered -- see
    # Orchestrator.tick()). One RiskEngine instance is stateless per call,
    # safe to share.
    # Fase 3.2: o gate de viabilidade líquida usa EXATAMENTE os números
    # que o motor de execução realmente aplica (quando ele os expõe), ou a
    # estimativa declarada de BYBIT_DEMO -- nunca uma terceira cópia que
    # pudesse divergir das duas.
    # Fase 3.3.1: a política de frescor vem da MESMA configuração que
    # dirige o agregador (`strategy_timeframe_minutes`), nunca de uma
    # segunda cópia que pudesse divergir dele.
    # Decidida pela SEMÂNTICA TEMPORAL DA FONTE DE MERCADO, nunca pelo
    # motor de execução: PAPER_LIVE roda PaperLocalExecutionEngine e
    # continua integralmente protegido.
    freshness_policy = freshness_policy_for_market_data(
        settings.mode.value,
        settings.max_signal_delay_after_close_seconds,
        settings.strategy_timeframe_minutes,
    )
    risk_engine = RiskEngine(
        limits=risk_limits, cost_model=None, freshness_policy=freshness_policy,
    )

    # Single source of truth for "the price of the candle currently driving
    # the decision", already keyed by symbol -- the orchestrator writes it
    # every tick; PAPER_LOCAL's price_provider only ever falls back to it if
    # a caller forgets to pass an explicit reference_price (the orchestrator
    # always does).
    price_state: dict[str, float] = {}

    def price_provider(symbol: str) -> float:
        return price_state.get(symbol, 0.0)

    # Fase 3.1 (painel gráfico): estado PURAMENTE VISUAL (preço do candle
    # em formação), keyed by symbol -- nunca lido por nenhum código de
    # estratégia/risco/execução, só pela rota GET /api/chart-data. Mesmo
    # padrão de compartilhamento de `price_state` acima.
    visual_price_state: dict[str, dict] = {}

    execution_engine, clock_provider, funding_provider, transport = _build_shared_execution_pipeline(
        settings, price_provider, bybit_transport
    )
    risk_engine.cost_model = build_cost_model(settings, execution_engine)

    # Correção v1.1 #5: SimulatedProvider stays the default in every case;
    # only a deliberate, fully-configured opt-in (toggle ON AND both the
    # API key and endpoint URL actually set) swaps in the external
    # provider -- a half-configured toggle silently falls back to
    # SimulatedProvider rather than failing or reaching out with an empty
    # key/url.
    ai_provider = SimulatedProvider()
    if settings.ai_shadow_external_provider_enabled and settings.ai_provider_api_key and settings.ai_provider_endpoint_url:
        from app.ai_shadow.http_provider import HttpAIProvider

        ai_provider = HttpAIProvider(
            endpoint_url=settings.ai_provider_endpoint_url, api_key=settings.ai_provider_api_key,
        )

    ai_agent = AIShadowAgent(
        provider=ai_provider,
        timeout_seconds=settings.ai_timeout_seconds,
        max_response_chars=settings.ai_max_response_chars,
        enabled=settings.ai_shadow_enabled_default,
    )

    # Correção obrigatória #1 (Fase 3 multiativo): ONE explicit, shared
    # `StrategyConfig` instance -- constructed once here, handed to every
    # per-symbol `StrategyEngine` AND to `start_or_resume_session` below, so
    # the session fingerprint's `strategy_config` is always
    # `dataclasses.asdict()` of the EXACT object actually driving every
    # engine, never a hand-duplicated list of fields that could drift from
    # what the strategy really uses.
    # Fase 3.2: a configuração de estratégia deixa de ser um
    # `StrategyConfig()` puro (defaults fixados em código) e passa a vir
    # inteiramente do `Settings` -- é ESTE objeto que alimenta cada
    # StrategyEngine E o fingerprint da sessão, então os dois nunca podem
    # divergir.
    strategy_config = StrategyConfig(
        fast_period=settings.strategy_fast_period,
        slow_period=settings.strategy_slow_period,
        atr_period=settings.strategy_atr_period,
        min_atr_pct_of_price=settings.strategy_min_atr_pct,
        max_atr_pct_of_price=settings.strategy_max_atr_pct,
        stop_loss_atr_multiple=settings.strategy_stop_loss_atr_multiple,
        take_profit_atr_multiple=settings.strategy_take_profit_atr_multiple,
        timeframe_minutes=settings.strategy_timeframe_minutes,
    )

    # Fase 3 multiativo: one `Orchestrator` per configured symbol, each
    # with its OWN market data provider + StrategyEngine (never shared --
    # see app/strategy/engine.py's mutable rolling-window state) and its own
    # `.symbol`/`.symbols` Settings view (`model_copy`, no re-validation),
    # but sharing `session_factory` (-> the same SystemState singleton row
    # and the one portfolio-level OperationalSession), `risk_engine`,
    # `execution_engine`, `ai_agent`, `price_state` and the same
    # `strategy_config` (values only -- each engine's rolling-window STATE
    # stays fully independent) with every other symbol.
    orchestrators: dict[str, Orchestrator] = {}
    for symbol in settings.symbols:
        per_symbol_settings = settings.model_copy(update={"symbol": symbol, "symbols": [symbol]})
        market_data_provider = _build_market_data_provider(per_symbol_settings, symbol, transport)
        orchestrators[symbol] = Orchestrator(
            settings=per_symbol_settings,
            session_factory=session_factory,
            market_data_provider=market_data_provider,
            strategy_engine=StrategyEngine(symbol=symbol, config=strategy_config),
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            ai_agent=ai_agent,
            clock_provider=clock_provider,
            price_state=price_state,
            funding_provider=funding_provider,
            visual_price_state=visual_price_state,
        )

    orchestrator: Orchestrator | MultiSymbolOrchestrator
    if len(settings.symbols) == 1:
        orchestrator = orchestrators[settings.symbols[0]]
    else:
        orchestrator = MultiSymbolOrchestrator(orchestrators, settings.symbols, settings=settings)

    # Startup/post-restart reconciliation (correction 8): runs for every
    # mode. For REPLAY/PAPER_LOCAL there are no persisted open positions on a
    # fresh DB, so this is a fast no-op; for BYBIT_DEMO it is the first real
    # network call the process makes, and any mismatch or failure blocks
    # trading immediately rather than trusting stale local state.
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)

        # Fase 2, item 7.7 + Fase 3 multiativo: create or resume the ONE
        # portfolio-level operational session for this exact mode + ordered
        # symbol list BEFORE the startup reconciliation below, so that
        # reconciliation is itself counted in reconciliations_count.
        # Item 7.8: a session/process only ever comes up as OBSERVANDO
        # (monitoring, reconciling, able to close/reduce exposure) -- never
        # ATIVO. Opening new entries always requires an explicit
        # POST /operational-state/activate afterward, regardless of mode or
        # how clean the startup reconciliation was.
        op_session = start_or_resume_session(session, settings, STRATEGY_VERSION, risk_limits, strategy_config)
        state.active_session_id = op_session.id

        # Fase 3.2 (item 5 da decisão do PO): replay SILENCIOSO do estado
        # estratégico a partir dos candles de 1 minuto já persistidos --
        # SMA rápida/lenta, ATR, estado do cruzamento e o bucket parcial em
        # curso. Sem isso, um restart no meio de um bucket (ou depois de 20
        # buckets de aquecimento) recomeçaria do zero e perderia entradas
        # em silêncio. Não persiste sinal, não cria ordem, não incrementa
        # contador operacional e não chama o motor de risco.
        orchestrator.hydrate_strategy_state(session)

        orchestrator.reconcile(session, state)

        state.operational_state = "BLOQUEADO" if state.trading_blocked else "OBSERVANDO"
        op_session.status = state.operational_state

    return orchestrator


async def _graceful_shutdown(app: FastAPI) -> None:
    """Correção v1.1 #7: `end_session()` existed but was dead code -- the
    old shutdown only cancelled the poll task, leaving the operational
    session open forever and the process' final state unrecorded. Now:
    blocks new entries (`operational_state=ENCERRANDO`), stops the loop
    cleanly (awaiting the CancelledError, never leaving it dangling), runs
    one last reconciliation (never lets a failure there hang or crash
    shutdown), and ends the session with `ended_at`/a Portuguese reason --
    all persisted before returning, so a genuine crash (which never
    reaches this function) is the only path that leaves a session
    resumable on the next boot."""
    orch = app.state.orchestrator

    loop_task = app.state.loop_task
    if loop_task is not None:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown must never hang/crash on this
            log_event(logger, 40, "poll_loop_shutdown_error", detail=str(exc))

    # Correção Operacional do Poll Loop v1.1, item 3: cancelling the
    # supervisor/worker Tasks above does NOT kill an in-flight `orch.tick()`
    # running inside its dedicated executor thread -- a Python thread can't
    # be forcibly cancelled. Without this, that tick could still commit a
    # write to the database AFTER the reconciliation/end_session below
    # already ran. This is an absolute wait (never abandons it), so a
    # genuinely stuck tick blocks shutdown rather than risking a late
    # mutation -- see `wait_for_in_flight_tick_before_shutdown`'s
    # docstring for the explicit, testable warning behavior.
    await wait_for_in_flight_tick_before_shutdown(app)

    mark_shutting_down = getattr(orch, "mark_shutting_down", None)
    if mark_shutting_down is not None:
        mark_shutting_down()

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.operational_state = "ENCERRANDO"

        try:
            orch.reconcile(session, state)
        except Exception as exc:  # noqa: BLE001 - shutdown must never hang/crash on this
            log_event(logger, 40, "shutdown_reconciliation_failed", detail=str(exc))

        if state.active_session_id is not None:
            op_session = session.get(OperationalSession, state.active_session_id)
            if op_session is not None and op_session.ended_at is None:
                end_session(session, op_session, "Encerramento gracioso do processo.")

    log_event(logger, 20, "app_shutdown_complete")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Correção operacional do poll loop v1.0: the task started here is now
    # the SUPERVISOR (app/api/poll_engine.py::supervise_poll_loop), not the
    # raw tick loop directly -- it owns creating/restarting/cancelling the
    # actual worker task internally. `app.state.loop_task` keeps its name
    # for the shutdown code below, which only needs "the one task to
    # cancel and await" and doesn't care which coroutine that is.
    app.state.loop_task = asyncio.create_task(supervise_poll_loop(app), name="poll-supervisor")
    try:
        yield
    finally:
        await _graceful_shutdown(app)


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count)
    log_event(logger, 20, "app_starting", mode=settings.mode.value)

    app = FastAPI(title="Agente Trader Demo", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.orchestrator = build_orchestrator(settings)
    app.state.replay_done = False
    app.state.loop_task = None
    # Correção operacional do poll loop v1.0: process-memory health state,
    # shared between the supervisor/worker (which update it) and
    # /api/state + the activation endpoint (which read it). See
    # app/api/poll_engine.py::PollHealth's docstring for why this is never
    # persisted to the database.
    app.state.poll_health = PollHealth()
    app.state.poll_worker_task = None
    app.state.poll_in_flight_future = None

    app.include_router(routes_dashboard.router, prefix="/api")
    app.include_router(routes_control.router, prefix="/api")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    return app


# Correção Operacional do Poll Loop v1.2, Bloqueio 1: NO module-level
# `app = create_app()` -- importing this module (or `create_app`, or any
# route/helper from it) must never itself open a database, run migrations,
# create/resume an operational session, run a reconciliation, spawn the
# poll supervisor task, or touch any external client. Only a DELIBERATE
# call to `create_app()` does any of that. Production starts the server
# via `app/run.py`, which passes uvicorn the factory itself
# (`"app.api.main:create_app"`, `factory=True`) -- uvicorn imports this
# module (a pure import, no side effects) and only THEN calls
# `create_app()` once to build the actual application.
