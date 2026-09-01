"""Wires market data -> strategy -> risk -> execution (one tick), and
market data -> AI shadow agent (parallel, observation-only, never gates
execution). This is the only module allowed to call both the Risk Engine and
an Execution Engine, which keeps the "Risk Engine has sole authority" property
easy to audit: every path that reaches ExecutionEngine.submit() first went
through RiskEngine.evaluate() or RiskEngine.evaluate_close() in this file.

Correction v1.1: closing a position (opposing signal or a stop-loss/
take-profit touch) now goes through RiskEngine.evaluate_close() -- it no
longer fabricates an ApprovedOrder directly. See app/risk/engine.py.

Correção v1.1 (Fase 2, réplica): `submit()` no longer blocks waiting for
confirmation -- see app/execution/base.py::SubmitAck/OrderStatusSnapshot.
The normal path here always calls `poll_order()` once immediately after a
successful submit (so PAPER_LOCAL/PAPER_LIVE and a fast-confirming
BYBIT_DEMO fake still resolve within the same tick, preserving every
existing regression test), and `_poll_open_orders()` re-polls anything
still non-terminal on a configurable periodic cadence -- this is what makes
a process restart with an order in flight, or a real exchange that takes
longer than one tick to fill, actually work instead of hanging or
re-submitting. Every fill, from any of these paths, is applied through the
single `app/execution/fill_service.py::apply_order_snapshot` -- there is no
second place that touches Execution/Position/session counters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from app.ai_shadow.agent import AIShadowAgent
from app.core.clock import RemoteTimeProvider, compute_clock_sync, utcnow
from app.core.config import Settings
from app.core.logging import get_logger, log_event
from app.execution import fill_service
from app.execution.base import ExecutionEngine
from app.execution.funding import FUNDING_WINDOW_SECONDS, BybitFundingProvider, record_new_funding_events
from app.execution.idempotency import make_idempotency_key
from app.execution.order_state import OrderStatus, is_terminal
from app.execution.reconciliation import reconcile_orders, reconcile_positions
from app.market_data.base import CandleFetchStatus, MarketDataProvider
from app.persistence import repo
from app.persistence.models import OperationalSession
from app.risk.engine import RiskContext, RiskEngine
from app.sessions import increment as increment_session_counter
from app.strategy.engine import StrategyEngine

logger = get_logger(__name__)


def _today_start_utc(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        market_data_provider: MarketDataProvider,
        strategy_engine: StrategyEngine,
        risk_engine: RiskEngine,
        execution_engine: ExecutionEngine,
        ai_agent: AIShadowAgent | None,
        clock_provider: RemoteTimeProvider,
        price_state: dict[str, float] | None = None,
        funding_provider: "BybitFundingProvider | None" = None,
        visual_price_state: dict[str, dict] | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.market_data_provider = market_data_provider
        self.strategy_engine = strategy_engine
        self.risk_engine = risk_engine
        self.execution_engine = execution_engine
        self.ai_agent = ai_agent
        self.clock_provider = clock_provider
        # Shared with whatever price_provider fallback the execution engine
        # was built with; the orchestrator is the single writer, updated
        # from the candle that is actually driving each decision.
        self.price_state: dict[str, float] = price_state if price_state is not None else {}
        # Fase 3.1 (painel gráfico): estado PURAMENTE VISUAL, nunca lido por
        # nenhum código de estratégia/risco/execução -- só pela rota HTTP
        # do painel. Um dict {"price": float, "at": datetime, "source":
        # "forming_candle"|"last_closed_candle"} por símbolo, atualizado a
        # cada tick() (mesmo em ticks sem candle novo) quando o provider
        # expõe `get_visual_price()` -- ver app/market_data/bybit_provider.py.
        self.visual_price_state: dict[str, dict] = (
            visual_price_state if visual_price_state is not None else {}
        )
        self._last_open_order_poll_at: datetime | None = None
        # Correção v1.1 #6: only ever set for BYBIT_DEMO (the only mode
        # with private-endpoint credentials) -- None means funding stays
        # UNAVAILABLE rather than a fabricated/simulated value.
        self.funding_provider = funding_provider
        self._last_funding_poll_at: datetime | None = None
        # Correção operacional do poll loop v1.0: set from OUTSIDE (by
        # app/api/poll_engine.py's worker/supervisor) whenever the poll
        # engine itself is DEGRADADO/PARADO or its heartbeat has expired --
        # deliberately process-memory only (resets on restart, exactly like
        # the rest of the poll engine's health state). Read here so
        # RiskEngine.evaluate() can refuse new entries even in the
        # (should-be-impossible-post-fix, but defense-in-depth) case a tick
        # still runs while the engine is otherwise considered unhealthy.
        self.engine_degraded = False

    def tick(self) -> dict:
        from app.persistence.db import session_scope

        with session_scope(self.session_factory) as session:
            state = repo.get_or_create_system_state(session)

            # Fase 2, item 7.4: reconciliation now also runs periodically,
            # not only at startup or right after a failed order -- if it's
            # been longer than `reconciliation_interval_seconds` since the
            # last run, do one now, before anything else this tick. A
            # SystemState with no prior reconciliation at all
            # (last_reconciliation_at is None) is treated as "not yet due"
            # here rather than "infinitely stale" -- app/api/main.py always
            # runs one real reconciliation at startup before the first tick
            # in production; this only matters for tests that build an
            # Orchestrator directly without that startup call.
            # Correção de Datetimes v1.0: state.last_reconciliation_at (e todo
            # timestamp de domínio) já vem UTC-aware da camada ORM -- ver
            # app/persistence/temporal.py::UTCDateTime.
            last_reconciliation_at = state.last_reconciliation_at

            if last_reconciliation_at is not None:
                elapsed = (utcnow() - last_reconciliation_at).total_seconds()
                if elapsed >= self.settings.reconciliation_interval_seconds:
                    self.reconcile(session, state)
                    last_reconciliation_at = state.last_reconciliation_at

            if last_reconciliation_at is not None:
                delay = (utcnow() - last_reconciliation_at).total_seconds()
                state.reconciliation_stale = delay > self.settings.reconciliation_max_delay_seconds
            else:
                state.reconciliation_stale = False
            repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

            # Correção v1.1 #1: real, persistent order-status polling -- an
            # order still non-terminal from a prior tick (or a prior
            # process, before a restart) is re-polled here on a configurable
            # cadence, never left hanging and never re-submitted.
            self._maybe_poll_open_orders(session, state)
            self._maybe_collect_funding(session, state)

            # Correction v1.4 #2: before polling, tell the provider the last
            # candle actually persisted for its symbol/timeframe -- cheap
            # (indexed) and makes a freshly constructed provider (e.g. right
            # after a process restart) resume backlog draining exactly where
            # it left off, never reprocessing or losing track.
            sync_cursor = getattr(self.market_data_provider, "sync_cursor", None)
            if sync_cursor is not None:
                provider_symbol = getattr(self.market_data_provider, "symbol", self.settings.symbol)
                provider_timeframe = getattr(self.market_data_provider, "timeframe", "1")
                persisted_open_time = repo.get_last_candle_open_time(session, provider_symbol, provider_timeframe)
                sync_cursor(persisted_open_time)

            fetch_result = self.market_data_provider.next_candle()

            # Fase 3.1 (painel gráfico): captura o preço visual ANTES de
            # qualquer `return` antecipado abaixo -- o provider já atualizou
            # seu cache interno de "candle em formação" durante a chamada de
            # next_candle() acima (mesmo em ticks sem candle novo/HOLD),
            # então isso precisa rodar incondicionalmente aqui, não só no
            # caminho de sucesso. Duck-typed (mesmo padrão de sync_cursor):
            # REPLAY/PAPER_LOCAL nunca implementam get_visual_price, então
            # nunca têm preço visual -- a rota do painel cai no fallback
            # "último fechamento" (price_state) documentado.
            get_visual_price = getattr(self.market_data_provider, "get_visual_price", None)
            if get_visual_price is not None:
                visual = get_visual_price()
                if visual is not None:
                    visual_price, visual_price_at = visual
                    self.visual_price_state[self.settings.symbol] = {
                        "price": visual_price, "at": visual_price_at, "source": "forming_candle",
                    }

            if fetch_result.status == CandleFetchStatus.REPLAY_FINISHED:
                # The ONLY status allowed to end the orchestrator's loop.
                return {"status": "no_data"}

            if fetch_result.status == CandleFetchStatus.NO_NEW_CANDLE:
                return {"status": "no_new_candle"}

            if fetch_result.status == CandleFetchStatus.RETRYABLE_ERROR:
                state.api_failure_count += 1
                repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)
                detail = fetch_result.detail or "Falha temporária ao consultar dados de mercado."
                repo.record_failure(session, "FAILURE", detail)
                if state.trading_blocked:
                    repo.record_security_event(
                        session, "API_FAILURE_LIMIT_REACHED",
                        f"Bloqueio automático após {state.api_failure_count} falhas consecutivas de API.",
                    )
                return {"status": "retryable_error", "detail": detail}

            if fetch_result.status == CandleFetchStatus.FATAL_ERROR:
                state.api_failure_count += 1
                repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)
                detail = fetch_result.detail or "Falha grave e não recuperável ao consultar dados de mercado."
                repo.record_failure(session, "FAILURE", detail)
                repo.record_security_event(session, "FATAL_MARKET_DATA_ERROR", detail)
                return {"status": "fatal_error", "detail": detail}

            if fetch_result.status == CandleFetchStatus.GAP_DETECTED:
                # Correction v1.4 #2: an unrecoverable hole in the closed-
                # candle sequence is treated as an explicit, safe state --
                # never silently skipped over. Blocks trading like any other
                # data-integrity problem; does NOT end the polling loop
                # (only REPLAY_FINISHED does), so the operator can resolve
                # it and the process keeps observing without a restart.
                state.state_ambiguous = True
                repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)
                detail = fetch_result.detail or "Lacuna detectada na sequência de candles fechados."
                repo.record_failure(session, "FAILURE", detail)
                repo.record_security_event(session, "MARKET_DATA_GAP_DETECTED", detail)
                return {"status": "gap_detected", "detail": detail}

            candle = fetch_result.candle
            saved = repo.save_candle(
                session, candle.symbol, candle.timeframe, candle.open_time,
                candle.open, candle.high, candle.low, candle.close, candle.volume, candle.source,
            )
            if saved is None:
                # Correction v1.2 #2: a concurrent/duplicate write for the
                # same symbol+timeframe+open_time is a no-op, never a crash,
                # and never re-triggers strategy/AI/risk processing.
                return {"status": "duplicate_candle"}

            op_session = self._active_session(session, state)
            increment_session_counter(op_session, "candles_count")

            # A fresh candle was received and persisted: the API is healthy.
            state.api_failure_count = 0
            repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

            self.price_state[candle.symbol] = candle.close

            signal = self.strategy_engine.on_candle(candle)
            signal_row = repo.save_signal(
                session, signal.symbol, signal.direction, signal.justification,
                signal.observed_price, signal.atr, signal.params,
                source_candle_open_time=signal.source_candle_open_time,
            )
            increment_session_counter(op_session, "signals_count")

            self._run_ai_shadow(session, state, op_session, signal, signal_row.id, candle.close)

            data_is_stale = self.market_data_provider.is_stale(
                self.settings.risk_max_data_staleness_seconds
            )

            clock_sync = compute_clock_sync(self.clock_provider, self.settings.risk_max_clock_drift_seconds)
            was_out_of_sync = state.clock_out_of_sync
            state.clock_out_of_sync = not clock_sync.ok
            if not clock_sync.ok and not was_out_of_sync:
                repo.record_security_event(
                    session, "CLOCK_DRIFT_BLOCKED",
                    clock_sync.error or "Relógio local fora de sincronia; motivo desconhecido.",
                )
            repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

            stop_take_result = self._check_stop_take(session, state, candle, data_is_stale, clock_sync)
            if stop_take_result is not None:
                return stop_take_result

            close_result = self._maybe_close_opposing_position(
                session, state, signal, signal_row.id, data_is_stale, clock_sync
            )
            if close_result is not None:
                return close_result

            if signal.direction == "HOLD":
                return {"status": "hold"}

            open_pos = repo.open_positions(session, signal.symbol)
            all_open = repo.open_positions(session)
            open_exposure = sum(p.qty * p.avg_entry_price for p in all_open)
            today_start = _today_start_utc(utcnow())
            daily_loss = sum(
                -p.realized_pnl
                for p in repo.closed_positions(session)
                if p.realized_pnl < 0 and p.closed_at and p.closed_at >= today_start
            )

            context = RiskContext(
                open_positions_count=len(open_pos),
                open_exposure_usd=open_exposure,
                daily_realized_loss_usd=daily_loss,
                consecutive_losses=state.consecutive_losses,
                **self._common_risk_fields(state, data_is_stale, clock_sync),
            )

            risk_result = self.risk_engine.evaluate(signal, signal_row.id, context)
            risk_row = repo.save_risk_evaluation(
                session, signal_row.id, risk_result.approved, risk_result.reason, risk_result.checks
            )
            increment_session_counter(op_session, "approvals_count" if risk_result.approved else "rejections_count")

            if not risk_result.approved or risk_result.approved_order is None:
                return {"status": "rejected", "reason": risk_result.reason}

            return self._submit_and_record(
                session, state, risk_row.id, risk_result.approved_order, candle.open_time, candle.close
            )

    def _active_session(self, session, state) -> OperationalSession | None:
        """Fase 2, item 7.7: the OperationalSession counters are updated
        against, looked up cheaply by primary key. Returns None if no
        session is active yet (e.g. a test-built Orchestrator that never
        went through app.api.main.build_orchestrator) -- counters simply
        aren't incremented in that case (see app.sessions.increment)."""
        if state.active_session_id is None:
            return None
        return session.get(OperationalSession, state.active_session_id)

    def _common_risk_fields(self, state, data_is_stale: bool, clock_sync) -> dict:
        return dict(
            data_is_stale=data_is_stale,
            api_failure_count=state.api_failure_count,
            clock_drift_seconds=clock_sync.drift_seconds,
            kill_switch_engaged=state.kill_switch_engaged,
            trading_blocked=state.trading_blocked,
            state_ambiguous=state.state_ambiguous,
            cooldown_until=state.cooldown_until,
            now=utcnow(),
            reconciliation_stale=state.reconciliation_stale,
            operational_state=state.operational_state,
            engine_degraded=self.engine_degraded,
        )

    def _run_ai_shadow(self, session, state, op_session, signal, signal_id: int, price: float) -> None:
        """Fase 2, item 7.10: `market_context` gains session/position/risk
        context beyond the base strategy params -- strictly ADDITIVE plain
        data (no ORM objects, no execution/credential references), so the
        AI Shadow boundary (app/ai_shadow/guard.py -- no import of
        app.execution/pybit, no credential field names) stays intact."""
        if self.ai_agent is None:
            return

        position = None
        open_pos = repo.open_positions(session, signal.symbol)
        if open_pos:
            p = open_pos[0]
            position = {"side": p.side, "qty": p.qty, "avg_entry_price": p.avg_entry_price}

        market_context = {
            **signal.params,
            "price": price,
            "session": {
                "mode": self.settings.mode.value,
                "symbol": signal.symbol,
                "operational_state": state.operational_state,
                "session_uid": op_session.session_uid if op_session is not None else None,
            },
            "position": position,
            "risk": {
                "trading_blocked": state.trading_blocked,
                "consecutive_losses": state.consecutive_losses,
                "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            },
            "metrics": {
                "open_positions_count": len(open_pos),
                "closed_positions_count": len(repo.closed_positions(session, signal.symbol)),
            },
        }
        result = self.ai_agent.observe(signal.symbol, market_context)
        if result is None:
            return
        if result.is_valid and result.output is not None:
            repo.save_ai_recommendation(
                session, signal.symbol, signal_id, result.output.recommendation,
                result.output.confidence, result.output.reasoning_summary,
                result.output.risk_flags, result.provider_name, result.model_version,
                True, None,
            )
        else:
            repo.save_ai_recommendation(
                session, signal.symbol, signal_id, "HOLD", 0.0,
                "Saída da IA inválida ou indisponível.", [], result.provider_name,
                result.model_version, False, result.rejection_reason,
            )

    def _check_stop_take(self, session, state, candle, data_is_stale: bool, clock_sync) -> dict | None:
        """Evaluates stop-loss/take-profit for the open position (if any) on
        this candle's symbol, using the candle's high/low range. Runs before
        any new signal is considered, so an exit always takes priority over a
        fresh entry within the same tick.
        """
        positions = repo.open_positions(session, candle.symbol)
        if not positions:
            return None
        position = positions[0]

        if position.side == "BUY":
            stop_hit = position.stop_loss is not None and candle.low <= position.stop_loss
            target_hit = position.take_profit is not None and candle.high >= position.take_profit
        else:
            stop_hit = position.stop_loss is not None and candle.high >= position.stop_loss
            target_hit = position.take_profit is not None and candle.low <= position.take_profit

        if not stop_hit and not target_hit:
            return None

        # Conservative assumption (documented in docs/OPERACAO_DEMO.md): with
        # no intrabar sequencing available, if both stop and target were
        # touched within the same candle we assume the WORSE outcome (the
        # stop-loss) happened first.
        if stop_hit:
            trigger_price = position.stop_loss
            trigger_kind = "stop_loss"
            justification = (
                f"Stop-loss atingido: a faixa do candle [{candle.low}, {candle.high}] "
                f"cruzou o stop em {position.stop_loss}."
            )
            if target_hit:
                justification += (
                    " O take-profit também foi tocado no mesmo candle; a regra "
                    "conservadora assume que o stop-loss foi acionado primeiro."
                )
        else:
            trigger_price = position.take_profit
            trigger_kind = "take_profit"
            justification = (
                f"Take-profit atingido: a faixa do candle [{candle.low}, {candle.high}] "
                f"cruzou o alvo em {position.take_profit}."
            )

        close_side = "SELL" if position.side == "BUY" else "BUY"
        signal_row = repo.save_signal(
            session, position.symbol, close_side, justification, candle.close, 0.0,
            {"trigger": trigger_kind, "trigger_price": trigger_price},
            source_candle_open_time=candle.open_time,
        )

        common_fields = self._common_risk_fields(state, data_is_stale, clock_sync)
        bucket = candle.open_time.strftime("%Y%m%dT%H%M") + f":{trigger_kind}"
        return self._close_position_via_risk(
            session, state, position, close_side, position.qty, signal_row.id,
            common_fields, trigger_price, bucket,
        )

    def _maybe_close_opposing_position(
        self, session, state, signal, signal_id: int, data_is_stale: bool, clock_sync
    ) -> dict | None:
        if signal.direction not in ("BUY", "SELL"):
            return None

        positions = repo.open_positions(session, signal.symbol)
        opposing = [p for p in positions if p.side != signal.direction]
        if not opposing:
            return None
        position = opposing[0]

        common_fields = self._common_risk_fields(state, data_is_stale, clock_sync)
        bucket = signal.created_at.strftime("%Y%m%dT%H%M") + ":close"
        return self._close_position_via_risk(
            session, state, position, signal.direction, position.qty, signal_id,
            common_fields, signal.observed_price, bucket,
        )

    def _submit_and_track(self, session, state, order_row, approved, reference_price: float):
        """Correção v1.1 #1: the shared submit -> transition -> (immediate)
        poll -> apply-fill sequence used by both entry and close orders.
        Returns `(ack, snapshot_result)` -- `snapshot_result` is None when
        the exchange never even accepted the order (REJECTED/UNKNOWN)."""
        ack = self.execution_engine.submit(approved, order_row.idempotency_key, reference_price=reference_price)
        if ack.exchange_order_id:
            order_row.exchange_order_id = ack.exchange_order_id
        repo.transition_order_status(session, order_row, ack.status, detail=f"submit(): {ack.status.value}.")

        if ack.status != OrderStatus.SUBMITTED:
            return ack, None

        snapshot = self.execution_engine.poll_order(ack.exchange_order_id)
        result = fill_service.apply_order_snapshot(
            session, state, self._active_session(session, state), order_row, snapshot,
            is_close=order_row.is_close, max_api_failures=self.settings.risk_max_api_failures,
        )
        return ack, result

    def _close_position_via_risk(
        self, session, state, position, close_side: str, qty: float, signal_id: int,
        common_fields: dict, trigger_price: float, idempotency_bucket: str,
    ) -> dict:
        context = RiskContext(
            open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
            consecutive_losses=state.consecutive_losses, **common_fields,
        )
        risk_result = self.risk_engine.evaluate_close(
            signal_id=signal_id, symbol=position.symbol, close_side=close_side, qty=qty,
            position_exists=True, position_qty=position.qty, position_side=position.side,
            context=context,
        )
        risk_row = repo.save_risk_evaluation(
            session, signal_id, risk_result.approved, risk_result.reason, risk_result.checks
        )

        if not risk_result.approved or risk_result.approved_order is None:
            return {"status": "close_rejected", "reason": risk_result.reason}

        approved = risk_result.approved_order
        key = make_idempotency_key(approved, idempotency_bucket)
        existing = repo.find_order_by_idempotency_key(session, key)
        if existing:
            return {"status": "duplicate_suppressed", "order_id": existing.id}

        order_row = repo.save_order(
            session, key, risk_row.id, approved.symbol, approved.side, approved.qty,
            approved.stop_loss, approved.take_profit, mode=self.settings.mode.value, is_close=True,
            reference_price=trigger_price,
        )
        increment_session_counter(self._active_session(session, state), "orders_count")

        ack, result = self._submit_and_track(session, state, order_row, approved, trigger_price)

        if result is None:
            state.api_failure_count += 1
            repo.record_failure(
                session, "FAILURE",
                f"Ordem de fechamento {order_row.id} não foi aceita pela corretora (status={ack.status.value}).",
            )
            increment_session_counter(self._active_session(session, state), "failures_count")
            self.reconcile(session, state)
            return {"status": "close_failed", "order_status": ack.status.value, "order_id": order_row.id}

        if result.new_fill_count == 0:
            if result.status == OrderStatus.UNKNOWN:
                state.api_failure_count += 1
                repo.record_failure(
                    session, "FAILURE",
                    f"Ordem de fechamento {order_row.id} terminou com status={result.status.value}.",
                )
                increment_session_counter(self._active_session(session, state), "failures_count")
                self.reconcile(session, state)
                return {"status": "close_failed", "order_status": result.status.value, "order_id": order_row.id}
            # Accepted, still no fill yet (real exchange not-yet-filled) --
            # the periodic poller (_poll_open_orders) will pick it up.
            return {"status": "close_pending", "order_id": order_row.id, "order_status": result.status.value}

        closed_fully = bool(result.closed_fully)
        if closed_fully:
            if result.realized_pnl_delta_total < 0:
                state.consecutive_losses += 1
            else:
                state.consecutive_losses = 0
            if state.consecutive_losses >= self.settings.risk_cooldown_after_losses:
                state.cooldown_until = utcnow() + timedelta(minutes=self.settings.risk_cooldown_minutes)
                repo.record_security_event(
                    session, "COOLDOWN_ENGAGED",
                    f"{state.consecutive_losses} perdas consecutivas; cooldown até "
                    f"{state.cooldown_until.isoformat()}.",
                )
                log_event(logger, 30, "cooldown_engaged", consecutive_losses=state.consecutive_losses)

        log_event(logger, 20, "position_closed" if closed_fully else "position_reduced",
                  symbol=position.symbol, realized_pnl=result.realized_pnl_delta_total, order_id=order_row.id)
        return {
            "status": "position_closed" if closed_fully else "position_reduced",
            "realized_pnl": result.realized_pnl_delta_total, "order_id": order_row.id,
        }

    def _submit_and_record(self, session, state, risk_evaluation_id: int, approved,
                            open_time, candle_close: float) -> dict:
        key = make_idempotency_key(approved, open_time.strftime("%Y%m%dT%H%M"))
        existing = repo.find_order_by_idempotency_key(session, key)
        if existing:
            return {"status": "duplicate_suppressed", "order_id": existing.id}

        order_row = repo.save_order(
            session, key, risk_evaluation_id, approved.symbol, approved.side, approved.qty,
            approved.stop_loss, approved.take_profit, mode=self.settings.mode.value, is_close=False,
            reference_price=candle_close,
        )
        increment_session_counter(self._active_session(session, state), "orders_count")

        ack, result = self._submit_and_track(session, state, order_row, approved, candle_close)

        if result is None:
            state.api_failure_count += 1
            repo.record_failure(
                session, "FAILURE",
                f"Ordem {order_row.id} não foi aceita pela corretora (status={ack.status.value}).",
            )
            increment_session_counter(self._active_session(session, state), "failures_count")
            self.reconcile(session, state)
            return {"status": "order_not_filled", "order_status": ack.status.value}

        if result.new_fill_count > 0:
            return {"status": "order_filled", "order_id": order_row.id}

        if result.status == OrderStatus.UNKNOWN:
            state.api_failure_count += 1
            repo.record_failure(session, "FAILURE", f"Ordem {order_row.id} terminou com status={result.status.value}.")
            increment_session_counter(self._active_session(session, state), "failures_count")
            self.reconcile(session, state)
            return {"status": "order_not_filled", "order_status": result.status.value}

        # Accepted, still no fill yet -- the periodic poller picks it up.
        return {"status": "order_pending", "order_id": order_row.id, "order_status": result.status.value}

    def _maybe_poll_open_orders(self, session, state) -> None:
        """Correção v1.1 #1: periodic, persistent order-status polling --
        gated by `open_order_poll_interval_seconds` so this doesn't hammer
        the exchange every tick. Every non-terminal order still tracked
        locally is re-polled and its fills (if any) applied through the
        exact same `fill_service.apply_order_snapshot` used by the
        immediate post-submit poll and the kill switch -- one code path,
        never a second one."""
        now = utcnow()
        last = self._last_open_order_poll_at
        if last is not None and (now - last).total_seconds() < self.settings.open_order_poll_interval_seconds:
            return
        self._last_open_order_poll_at = now

        op_session = self._active_session(session, state)
        for order in repo.non_terminal_orders(session, mode=self.settings.mode.value):
            if not order.exchange_order_id:
                continue  # never actually reached the exchange -- nothing to poll yet
            if is_terminal(OrderStatus(order.status)):
                continue
            snapshot = self.execution_engine.poll_order(order.exchange_order_id)
            fill_service.apply_order_snapshot(
                session, state, op_session, order, snapshot,
                is_close=order.is_close, max_api_failures=self.settings.risk_max_api_failures,
            )
            self._maybe_apply_partial_fill_policy(session, state, op_session, order, now)

    def _maybe_collect_funding(self, session, state) -> None:
        """Correção v1.1 #6 / v1.2 #3 / v1.3 #1: periodic, idempotent
        funding collection -- only when `funding_provider` was actually
        wired (BYBIT_DEMO; never PAPER_LIVE, which has no private
        credentials -- see app/api/main.py::build_orchestrator). Gated by
        `funding_poll_interval_seconds`, same periodic-gate pattern as
        `_maybe_poll_open_orders`.

        Correção v1.3 #1: `since` is read from the explicit, persisted
        `FundingCollectionCheckpoint` (`repo.get_funding_checkpoint`) --
        NEVER derived from the MAX `occurred_at` already recorded in
        `funding_events`. That approach was unsafe: a newest-first
        paginated response could persist a recent record from page 1 and
        then fail on an older page 2, and the next cycle's `since` would
        jump past the still-unfetched backlog, making it permanently
        unreachable. The checkpoint only ever advances
        (`repo.advance_funding_checkpoint`) once an ENTIRE window is
        proven complete -- never partially, never based on which records
        happened to be returned or in what order.

        The `[since, now]` gap is walked in fixed-size windows
        (`FUNDING_WINDOW_SECONDS`) -- each window's records are persisted
        as soon as they're gathered (never batched until the end, and
        never discarded even when the window itself turns out
        incomplete), and windows are walked in chronological order,
        stopping at the first incomplete one so a later, more-recent
        window is never collected (nor its checkpoint advanced) while an
        earlier gap is left unfilled. Any incomplete window is logged as a
        structured, unresolved failure -- the period is never presented as
        fully reconciled when it is not."""
        if self.funding_provider is None:
            return
        now = utcnow()
        last = self._last_funding_poll_at
        if last is not None and (now - last).total_seconds() < self.settings.funding_poll_interval_seconds:
            return
        self._last_funding_poll_at = now

        checkpoint = repo.get_funding_checkpoint(session, self.settings.symbol)
        window_start = (
            checkpoint.covered_until if checkpoint is not None
            else (now - timedelta(seconds=FUNDING_WINDOW_SECONDS))
        )

        try:
            while window_start < now:
                window_end = min(window_start + timedelta(seconds=FUNDING_WINDOW_SECONDS), now)
                records, complete = self.funding_provider.list_funding(
                    self.settings.symbol, since=window_start, until=window_end,
                )
                if records:
                    record_new_funding_events(session, records)
                if not complete:
                    detail = (
                        f"Coleta de funding incompleta para {self.settings.symbol} entre "
                        f"{window_start.isoformat()} e {window_end.isoformat()} -- será retomada no "
                        "próximo ciclo pela mesma janela, sem avançar o checkpoint de cobertura."
                    )
                    repo.record_failure(session, "FAILURE", detail)
                    repo.record_security_event(session, "FUNDING_COLLECTION_INCOMPLETE", detail)
                    break
                repo.advance_funding_checkpoint(session, self.settings.symbol, window_end)
                window_start = window_end
        except Exception as exc:  # noqa: BLE001 - a funding-collection failure never blocks trading
            detail = f"Não foi possível coletar funding da corretora: {exc}"
            repo.record_failure(session, "FAILURE", detail)
            repo.record_security_event(session, "FUNDING_COLLECTION_FAILED", detail)
            return

    def _maybe_apply_partial_fill_policy(self, session, state, op_session, order, now) -> None:
        """Correção v1.1 #2/#5: an order stuck PARTIALLY_FILLED longer than
        `partial_fill_timeout_seconds` is handled per `partial_fill_policy`
        -- WAIT (default) never times out; CANCEL_REMAINDER and
        EXPIRE_AND_CANCEL both request cancellation of the unfilled
        remainder, through the exact same CANCEL_PENDING -> request_cancel
        -> poll -> fill_service path used everywhere else (never a second,
        divergent cancellation code path)."""
        if OrderStatus(order.status) != OrderStatus.PARTIALLY_FILLED:
            return
        policy = self.settings.partial_fill_policy
        if policy == "WAIT":
            return
        stalled_for = (now - order.updated_at).total_seconds()
        if stalled_for <= self.settings.partial_fill_timeout_seconds:
            return

        repo.transition_order_status(
            session, order, OrderStatus.CANCEL_PENDING,
            detail=f"Prazo de fill parcial excedido ({policy}, {stalled_for:.0f}s).",
        )
        self.execution_engine.request_cancel(order.exchange_order_id)
        cancel_snapshot = self.execution_engine.poll_order(order.exchange_order_id)
        fill_service.apply_order_snapshot(
            session, state, op_session, order, cancel_snapshot,
            is_close=order.is_close, max_api_failures=self.settings.risk_max_api_failures,
        )

    def reconcile(self, session, state) -> None:
        """Compares locally persisted state against what the execution
        engine reports for the exchange -- both positions (Fase 1) and,
        since correção v1.1 #3, open orders (an order the exchange has that
        isn't tracked locally, or one tracked locally as non-terminal that
        the exchange no longer reports as open). Runs at orchestrator
        construction time (startup / after a restart), whenever an order
        submission ends in an unresolved/error status, and periodically
        (Fase 2, item 7.4 -- see the top of `tick()`).

        Position reconciliation and order reconciliation each run in their
        OWN try/except, each persisting their own structured result --
        correção v1.1 #3 item 7 explicitly requires that a failure in one
        half is never masked by the other half succeeding. The combined
        `reconciliation_diverged`/`state_ambiguous` is only cleared when
        BOTH halves reach the exchange and find no mismatch (a logical
        AND) -- a divergence found by either half, or a failure to even
        reach the exchange for either half, keeps the system blocked.
        `last_reconciliation_at` is always stamped once, whatever the
        outcome, so staleness tracking reflects the most recent attempt,
        not just the most recent success. Both structured results are
        persisted via `repo.record_failure(..., mismatches=...)`
        (correção v1.1 #3) so a result can be inspected programmatically,
        not just read as a paragraph.
        """
        op_session = self._active_session(session, state)
        increment_session_counter(op_session, "reconciliations_count")
        state.last_reconciliation_at = utcnow()

        position_ok = self._reconcile_positions_step(session, state, op_session)
        order_ok = self._reconcile_orders_step(session, state, op_session)

        # Fase 2, item 7.7/7.8: a reconciliation that actually completed at
        # least its position half (reached the exchange and compared,
        # whether or not it found a mismatch) satisfies the "reconciliação
        # inicial concluída" gate.
        if position_ok is not None:
            state.initialization_not_reconciled = False
        state.reconciliation_diverged = not (position_ok and order_ok)
        state.state_ambiguous = state.reconciliation_diverged
        repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

    def _reconcile_positions_step(self, session, state, op_session) -> bool | None:
        """Returns True (clean), False (diverged), or None (couldn't even
        reach the exchange to compare)."""
        local_positions = [
            {"symbol": p.symbol, "side": p.side, "qty": p.qty, "avg_entry_price": p.avg_entry_price}
            for p in repo.open_positions(session)
        ]
        symbols = {p["symbol"] for p in local_positions}
        symbols.add(self.settings.symbol)

        remote_by_symbol: dict[str, dict | None] = {}
        try:
            for symbol in symbols:
                remote_by_symbol[symbol] = self.execution_engine.get_position(symbol)
        except Exception as exc:  # noqa: BLE001 - any failure to verify blocks trading
            detail = f"Não foi possível consultar as posições na corretora para reconciliação: {exc}"
            repo.record_failure(session, "RECONCILIATION", detail, session_id=op_session.id if op_session else None)
            repo.record_security_event(session, "RECONCILIATION_FAILED", detail)
            return None

        report = reconcile_positions(local_positions, remote_by_symbol)
        if report.ok:
            repo.record_failure(
                session, "RECONCILIATION",
                "Reconciliação de posições OK: posições locais e da corretora coincidem.",
                resolved=True, mismatches=report.mismatches,
                session_id=op_session.id if op_session else None,
            )
            return True

        detail = "Divergência de reconciliação de posições: " + "; ".join(report.mismatches)
        repo.record_failure(
            session, "RECONCILIATION", detail, mismatches=report.mismatches,
            session_id=op_session.id if op_session else None,
        )
        repo.record_security_event(session, "RECONCILIATION_MISMATCH", detail)
        return False

    def _reconcile_orders_step(self, session, state, op_session) -> bool | None:
        """Correção v1.1 #3: (a) re-polls every locally non-terminal order
        to recover any fill missed by the periodic poller (through the same
        `fill_service.apply_order_snapshot` used everywhere else -- never a
        second path), then (b) compares the now-current set of locally
        non-terminal orders against `list_open_orders()` to detect an order
        the exchange has that isn't tracked locally at all. Returns True
        (clean), False (diverged), or None (couldn't reach the exchange)."""
        local_orders = repo.non_terminal_orders(session, mode=self.settings.mode.value)
        symbols = {o.symbol for o in local_orders}
        symbols.add(self.settings.symbol)
        try:
            for order in local_orders:
                if not order.exchange_order_id or is_terminal(OrderStatus(order.status)):
                    continue
                snapshot = self.execution_engine.poll_order(order.exchange_order_id)
                fill_service.apply_order_snapshot(
                    session, state, op_session, order, snapshot,
                    is_close=order.is_close, max_api_failures=self.settings.risk_max_api_failures,
                )

            remote_open_orders: list[dict] = []
            for symbol in symbols:
                remote_open_orders.extend(self.execution_engine.list_open_orders(symbol))
        except Exception as exc:  # noqa: BLE001 - any failure to verify blocks trading
            detail = f"Não foi possível consultar as ordens abertas na corretora para reconciliação: {exc}"
            repo.record_failure(session, "RECONCILIATION", detail, session_id=op_session.id if op_session else None)
            repo.record_security_event(session, "RECONCILIATION_FAILED", detail)
            return None

        local_open_orders = [
            {"exchange_order_id": o.exchange_order_id, "side": o.side, "qty": o.qty}
            for o in repo.non_terminal_orders(session, mode=self.settings.mode.value)
            if o.exchange_order_id
        ]
        report = reconcile_orders(local_open_orders, remote_open_orders)
        if report.ok:
            repo.record_failure(
                session, "RECONCILIATION",
                "Reconciliação de ordens OK: ordens locais e da corretora coincidem.",
                resolved=True, mismatches=report.mismatches,
                session_id=op_session.id if op_session else None,
            )
            return True

        detail = "Divergência de reconciliação de ordens: " + "; ".join(report.mismatches)
        repo.record_failure(
            session, "RECONCILIATION", detail, mismatches=report.mismatches,
            session_id=op_session.id if op_session else None,
        )
        repo.record_security_event(session, "RECONCILIATION_MISMATCH", detail)
        return False


# --- Fase 3 multiativo: round-robin scheduler over N per-symbol Orchestrators
#
# Deliberately does NOT change a single line of `Orchestrator` above -- that
# class stays exactly as audited/tested for monoativo. Multiativo is built by
# composing N `Orchestrator` instances (one per symbol, each with its own
# market data provider + StrategyEngine, sharing the same session_factory /
# execution_engine / risk_engine / DB), driven one-at-a-time by this wrapper.
# Reusing the exact same `session_factory` is what makes SystemState (the
# kill-switch/cooldown singleton) and the one portfolio-level
# OperationalSession naturally shared across all symbols, with zero extra
# plumbing -- both are already looked up by primary key / process-wide
# `get_settings()` state, not by `self.settings.symbol`.
#
# `poll_engine.py`'s OWN `PollHealth` (is the single worker thread/executor
# itself alive and cycling on schedule) is UNCHANGED and stays global -- it
# is a process-liveness concept, equally valid for one symbol or many, since
# the same worker services every symbol in round-robin. The per-symbol
# health introduced here (`SymbolHealth`) is a DIFFERENT, market-data/
# trading-domain concept ("is THIS symbol's data healthy right now") that
# `poll_engine.py` has no business tracking.

FAILURE_TICK_STATUSES = frozenset({"retryable_error", "fatal_error", "gap_detected"})

# Fixed "N strikes" threshold before a symbol moves from DEGRADADO to
# PARADO (and starts skipping its round-robin turns) -- deliberately a
# plain constant, not a new Settings field, mirroring the existing
# `risk_cooldown_after_losses` default of 3 elsewhere in this codebase
# rather than growing the configuration surface for this foundation.
SYMBOL_PARADO_THRESHOLD = 3


@dataclass
class SymbolHealth:
    """Per-symbol health -- process-memory only, rebuilt from INICIANDO on
    every boot (Fase 3 multiativo, decisão do PO: sem tabela nova)."""

    status: str = "INICIANDO"  # INICIANDO | SAUDAVEL | DEGRADADO | PARADO | ENCERRANDO
    consecutive_failures: int = 0
    last_tick_started_at: datetime | None = None
    last_tick_completed_at: datetime | None = None
    last_tick_success_at: datetime | None = None
    last_candle_persisted_at: datetime | None = None
    last_error: str | None = None
    eligible_again_at: datetime | None = None  # while PARADO: round-robin turn skipped until this instant
    has_gap: bool = False

    def is_healthy(self) -> bool:
        return self.status == "SAUDAVEL"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "consecutive_failures": self.consecutive_failures,
            "last_tick_started_at": self.last_tick_started_at.isoformat() if self.last_tick_started_at else None,
            "last_tick_completed_at": self.last_tick_completed_at.isoformat() if self.last_tick_completed_at else None,
            "last_tick_success_at": self.last_tick_success_at.isoformat() if self.last_tick_success_at else None,
            "last_candle_persisted_at": (
                self.last_candle_persisted_at.isoformat() if self.last_candle_persisted_at else None
            ),
            "last_error": self.last_error,
            "has_gap": self.has_gap,
        }


# Worst-to-best precedence for the aggregated portfolio status shown on the
# dashboard (plan doc section 4) -- distinct from the boolean activation
# gate below, which requires ALL symbols SAUDAVEL.
_STATUS_PRECEDENCE = ["ENCERRANDO", "PARADO", "DEGRADADO", "INICIANDO", "SAUDAVEL"]


class MultiSymbolOrchestrator:
    """Round-robin scheduler over one `Orchestrator` per configured symbol.
    Exposes the same `.tick()` / `.engine_degraded` surface `poll_engine.py`
    already drives -- no change needed there for the scheduling itself."""

    def __init__(self, orchestrators: dict[str, "Orchestrator"], symbols: list[str], settings=None):
        if not orchestrators or set(orchestrators) != set(symbols):
            raise ValueError("orchestrators deve ter exatamente uma entrada por símbolo em `symbols`.")
        self.orchestrators = orchestrators
        self.symbols = list(symbols)  # canonical configuration order -- the round-robin order
        self._rr_index = 0
        self.health: dict[str, SymbolHealth] = {s: SymbolHealth() for s in self.symbols}
        self._engine_degraded = False
        # The REAL, multi-symbol `Settings` object (not any per-symbol
        # `model_copy`) -- kept so API routes that read `orch.settings.*`
        # (mode, reconciliation intervals, risk limits, ...) work unchanged
        # regardless of whether they hold an `Orchestrator` or a
        # `MultiSymbolOrchestrator`. Falls back to the first underlying
        # orchestrator's settings if not given (e.g. built by hand in a
        # test) -- every field except `.symbol`/`.symbols` is identical
        # across all of them anyway.
        self.settings = settings if settings is not None else next(iter(orchestrators.values())).settings

    @property
    def session_factory(self):
        # Every underlying Orchestrator shares the exact same session_factory
        # (see app/api/main.py::build_orchestrator) -- exposed here purely
        # so callers that don't care whether they hold an `Orchestrator` or a
        # `MultiSymbolOrchestrator` (e.g. app/api/main.py::_graceful_shutdown)
        # can keep using `orch.session_factory` unchanged.
        return next(iter(self.orchestrators.values())).session_factory

    @property
    def funding_provider(self):
        # BYBIT_DEMO (the only mode that ever sets a real funding provider)
        # is guaranteed monoativo (Fase 3 multiativo, item 1) -- a
        # MultiSymbolOrchestrator never genuinely has one, but exposing this
        # (always None here) keeps `orch.funding_provider is not None`
        # checks in API routes working unchanged for both orchestrator types.
        return next(iter(self.orchestrators.values())).funding_provider

    @property
    def price_state(self):
        # The SAME shared dict object is injected into every underlying
        # Orchestrator (see app/api/main.py::build_orchestrator) -- any one
        # of them exposes the whole portfolio's prices.
        return next(iter(self.orchestrators.values())).price_state

    @property
    def visual_price_state(self):
        # Fase 3.1 (painel gráfico): same sharing pattern as price_state
        # above -- one shared dict across every symbol.
        return next(iter(self.orchestrators.values())).visual_price_state

    @property
    def engine_degraded(self) -> bool:
        return self._engine_degraded

    @engine_degraded.setter
    def engine_degraded(self, value: bool) -> None:
        # Set externally by poll_engine.py (process/worker-level liveness) --
        # propagated to every underlying Orchestrator unconditionally, same
        # as before multiativo (a stuck worker blocks new entries on every
        # symbol, not just one).
        self._engine_degraded = value
        for orch in self.orchestrators.values():
            orch.engine_degraded = value

    def _portfolio_healthy(self) -> bool:
        return all(h.is_healthy() and not h.has_gap for h in self.health.values())

    def _sync_engine_degraded_to_orchestrators(self) -> None:
        """The activation gate is portfolio-wide (plan doc section 4): a
        single symbol failing to be SAUDAVEL/synced/contínuo blocks new
        entries on EVERY symbol, but never blocks closing/reducing (each
        underlying Orchestrator already applies that exception in
        RiskEngine.evaluate_close). Re-derived before every tick so it never
        lags behind the latest per-symbol health."""
        degraded = self._engine_degraded or not self._portfolio_healthy()
        for orch in self.orchestrators.values():
            orch.engine_degraded = degraded

    def mark_shutting_down(self) -> None:
        for h in self.health.values():
            h.status = "ENCERRANDO"

    def portfolio_status(self) -> dict:
        statuses = [h.status for h in self.health.values()]
        aggregated = next((s for s in _STATUS_PRECEDENCE if s in statuses), "SAUDAVEL")
        healthy_count = sum(1 for h in self.health.values() if h.is_healthy())
        return {
            "portfolio": {"status": aggregated, "healthy_count": healthy_count, "total": len(self.symbols)},
            "per_symbol": {s: h.to_dict() for s, h in self.health.items()},
        }

    def _update_health(self, symbol: str, now: datetime, result: dict) -> None:
        h = self.health[symbol]
        h.last_tick_completed_at = now
        status = result.get("status", "")

        if status in FAILURE_TICK_STATUSES:
            h.consecutive_failures += 1
            h.last_error = result.get("detail") or status
            h.has_gap = status == "gap_detected"
            if h.consecutive_failures >= SYMBOL_PARADO_THRESHOLD:
                h.status = "PARADO"
                symbol_orch = self.orchestrators[symbol]
                backoff = symbol_orch.settings.poll_backoff_max_seconds
                h.eligible_again_at = now + timedelta(seconds=backoff)
            else:
                h.status = "DEGRADADO"
            return

        # Any non-failure status (hold/no_new_candle/duplicate_candle/
        # order_*/rejected/close_*/position_*) counts as a healthy tick --
        # data is flowing and being processed normally for this symbol.
        h.consecutive_failures = 0
        h.last_error = None
        h.has_gap = False
        h.eligible_again_at = None
        h.last_tick_success_at = now
        if status not in ("no_new_candle", "duplicate_candle", "no_data"):
            h.last_candle_persisted_at = now
        h.status = "SAUDAVEL"

    def tick(self) -> dict:
        now = utcnow()
        n = len(self.symbols)
        for _ in range(n):
            symbol = self.symbols[self._rr_index]
            self._rr_index = (self._rr_index + 1) % n
            h = self.health[symbol]
            if h.status == "PARADO" and h.eligible_again_at is not None and now < h.eligible_again_at:
                continue  # this symbol's turn is skipped -- never blocks the others

            self._sync_engine_degraded_to_orchestrators()
            h.last_tick_started_at = now
            result = self.orchestrators[symbol].tick()
            self._update_health(symbol, utcnow(), result)
            return {**result, "symbol": symbol}

        # Every configured symbol is currently in PARADO backoff -- a
        # legitimate (if unhealthy) outcome, never an exception.
        return {"status": "all_symbols_in_cooldown"}

    def reconcile(self, session, state) -> None:
        """Startup / on-demand full-portfolio reconciliation: runs every
        underlying Orchestrator's own (already audited) `reconcile()` once.
        Each call's own position/order queries are already global (not
        symbol-filtered -- see `_reconcile_positions_step`), so this
        guarantees every configured symbol is touched at least once, not
        just whichever symbol happens to own the next periodic check inside
        `tick()`."""
        for symbol in self.symbols:
            self.orchestrators[symbol].reconcile(session, state)
