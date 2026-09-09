"""SQLAlchemy models. One table per persisted concern (spec section 4.6) so
every decision is traceable end-to-end: candle -> signal -> ai_recommendation
(parallel, non-blocking) -> risk_evaluation -> order -> execution -> position.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.persistence.temporal import UTCDateTime as DateTime  # Correção de Datetimes v1.0:
# mesmo tipo de coluna no SQLite (sem migration), mas garante leitura/gravação
# sempre UTC-aware -- ver app/persistence/temporal.py.


class Base(DeclarativeBase):
    pass


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle_symbol_timeframe_open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))  # replay | bybit_demo
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategySignal(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # BUY | SELL | HOLD
    justification: Mapped[str] = mapped_column(Text)
    observed_price: Mapped[float] = mapped_column(Float)
    atr: Mapped[float] = mapped_column(Float)
    params_json: Mapped[str] = mapped_column(Text)
    # Fase 3.5 (auditoria do painel de diagnóstico): medido com EXPLAIN
    # QUERY PLAN -- toda janela temporal do funil/gate de custos filtra por
    # este campo, e sem índice o SQLite fazia SCAN completo da tabela (full
    # scan CRESCENTE, não um custo fixo). Índice simples, migration v10.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # Fase 3.1 (correção final auditoria PO): identidade determinística do
    # candle que gerou este sinal -- SEMPRE `candle.open_time`, nunca
    # derivado de preço ou de `created_at`. Nullable para compatibilidade
    # com sinais legados (nunca inventar este valor para dados antigos).
    source_candle_open_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_signals.id"), nullable=True)
    recommendation: Mapped[str] = mapped_column(String(8))  # BUY | SELL | HOLD
    confidence: Mapped[float] = mapped_column(Float)
    reasoning_summary: Mapped[str] = mapped_column(String(500))
    risk_flags_json: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64), default="unknown")
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskEvaluation(Base):
    __tablename__ = "risk_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Fase 3.5: indexado -- o painel de diagnóstico busca a avaliação de
    # risco de cada sinal por este campo; sem índice o SQLite varria a
    # tabela inteira (medido). Migration v10.
    signal_id: Mapped[int] = mapped_column(ForeignKey("strategy_signals.id"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    checks_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="risk_evaluation")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    risk_evaluation_id: Mapped[int] = mapped_column(ForeignKey("risk_evaluations.id"))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_close: Mapped[bool] = mapped_column(Boolean, default=False)
    # Free-text column for backward compatibility with the DB, but every
    # writer/reader goes through app.execution.order_state.OrderStatus and
    # app.persistence.repo.transition_order_status() (Fase 2, item 7.2) --
    # never written directly elsewhere.
    status: Mapped[str] = mapped_column(String(16), default="PENDING_SUBMIT")
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))  # PAPER_LOCAL | PAPER_LIVE | BYBIT_DEMO
    # Correção v1.1 #2: cumulative fill bookkeeping, always RE-DERIVED from
    # the full set of persisted `executions` rows for this order (never
    # summed/overwritten ad hoc) -- see app.execution.fill_ledger.
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float] = mapped_column(Float, default=0.0)
    fees_total: Mapped[float] = mapped_column(Float, default=0.0)
    # Fase 2, item 7.6: the price the decision was made against (the
    # candle/trigger price passed as `reference_price` to
    # ExecutionEngine.submit()) -- needed to compute realized slippage
    # (avg_fill_price vs. this) per order. Nullable: a pre-Fase-2 order
    # migrated from an older schema never had this recorded.
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Correção v1.2 #1: the exchange reported a TERMINAL status
    # (Filled/Cancelled) for this order, but the fill-history pagination
    # was not yet proven complete -- `status` itself deliberately stays
    # UNCHANGED (non-terminal) until it is, so the order stays in
    # `repo.non_terminal_orders()`'s recoverable set. Purely an audit/
    # observability trail -- never read as authoritative by anything that
    # gates trading. Cleared back to None once the real transition applies.
    pending_exchange_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # "COMPLETE" (default) | "PENDING" -- mirrors pending_exchange_status
    # (non-None iff "PENDING"), kept as its own column since the audit
    # explicitly names this pattern ("fills_sync_status") as an acceptable
    # design, and it is clearer to read directly than inferring from
    # nullability of the column above.
    fills_sync_status: Mapped[str] = mapped_column(String(16), default="COMPLETE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    risk_evaluation: Mapped["RiskEvaluation"] = relationship(back_populates="orders")
    executions: Mapped[list["Execution"]] = relationship(back_populates="order")


class OrderEvent(Base):
    """Fase 2, item 7.2: audit trail of every order status transition --
    written exclusively by app.persistence.repo.transition_order_status(),
    which validates the transition via app.execution.order_state first."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("order_id", "exchange_fill_id", name="uq_execution_order_exchange_fill_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    # Correção v1.1 #2: the exchange's own fill/execution identifier (Bybit
    # execId; a locally synthesized but stable id for PAPER engines).
    # Nullable only because a pre-Fase-2.1 row migrated from an older
    # schema never had one -- every row written by app.execution.fill_ledger
    # always sets it. The unique constraint above is what makes applying
    # the same fill twice a safe no-op at the database level, not just by
    # convention.
    exchange_fill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fill_qty: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="executions")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        # Migration v7: at most one OPEN position per symbol at the database
        # level -- partial index, so multiple historical CLOSED positions
        # for the same symbol remain unaffected.
        Index(
            "uq_position_open_symbol", "symbol", unique=True,
            sqlite_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    avg_entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN | CLOSED
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(16))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. PRODUCTION_ENDPOINT_BLOCKED, KILL_SWITCH
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FailureReconciliation(Base):
    __tablename__ = "failures_reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))  # FAILURE | RECONCILIATION
    detail: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Fase 2, item 7.4/7.7: optional links so a reconciliation/failure entry
    # can be traced back to the specific order or operational session it
    # happened under -- nullable, since most historical rows (and most
    # reconciliation runs, which are symbol-wide, not order-specific) have
    # no single order to point at.
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("operational_sessions.id"), nullable=True)
    # Correção v1.1 #3: structured mismatch list (JSON array of strings),
    # alongside `detail` (still the human-readable Portuguese summary) --
    # never ONLY free text, so a future audit can programmatically inspect
    # exactly what diverged, not just read a paragraph.
    mismatches_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class FundingEvent(Base):
    """Correção v1.1 #6: one row per funding settlement, deduplicated by
    the exchange's own identifier -- see app.execution.funding."""

    __tablename__ = "funding_events"
    __table_args__ = (
        UniqueConstraint("funding_id", name="uq_funding_event_funding_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funding_id: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    amount: Mapped[float] = mapped_column(Float)  # positive = credited, negative = debited
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FundingCollectionCheckpoint(Base):
    """Correção v1.2/v1.3 #1: explicit, persisted proof of funding-collection
    COVERAGE, separate from the funding events themselves. `covered_until`
    only ever advances when an entire `[since, until]` window was walked to
    completion (every page fetched, every row valid) -- see
    app.execution.funding/app.orchestrator._maybe_collect_funding. Using the
    MAX `occurred_at` already on file (the pre-v1.3 approach) was unsafe: a
    newest-first paginated response could persist a recent record from page
    1 and then fail on an older page 2, and the next cycle's `since` would
    jump past the still-unfetched backlog. One row per symbol -- the unique
    index is what makes `record_new_funding_events`-style "insert or update"
    logic race-safe at the database level, not just by convention."""

    __tablename__ = "funding_collection_checkpoints"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_funding_checkpoint_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    covered_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OperationalSession(Base):
    """Fase 2, item 7.7: one row per execution session -- created or resumed
    at process startup (app.sessions.start_or_resume_session), ended
    explicitly (app.sessions.end_session) on graceful shutdown or a fatal
    condition. Never mutated by anything outside app/sessions.py."""

    __tablename__ = "operational_sessions"
    __table_args__ = (
        # Migration v7 (correção obrigatória do PO): a row can never have
        # BOTH `symbol` and `symbols` NULL -- every row must carry at least
        # one form of its identity, legacy or new.
        CheckConstraint("symbol IS NOT NULL OR symbols IS NOT NULL", name="ck_symbol_or_symbols"),
        # Migration v7 (correção obrigatória do PO): at most one ACTIVE
        # session (ended_at IS NULL) per portfolio (mode + ordered symbol
        # list) -- legacy rows (symbols IS NULL) are excluded from this
        # constraint entirely, so historical data can never violate it.
        Index(
            "uq_operational_session_active_per_portfolio", "mode", "symbols", unique=True,
            sqlite_where=text("ended_at IS NULL AND symbols IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_uid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    # Legacy scalar symbol -- kept ONLY for backward compatibility with rows
    # written before migration v7. New sessions populate it only when
    # monoativo (exactly one symbol); NULL for genuinely multi-symbol
    # sessions (a scalar cannot represent N symbols without lying). Never
    # used for resume matching from v7 onward -- see `symbols` below and
    # app/sessions.py::start_or_resume_session.
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Migration v7: canonical portfolio identity -- JSON array of the
    # configured symbols, in configuration order (order is significant: it
    # drives both the round-robin scheduler and the session fingerprint).
    # NULL only for pre-v7 legacy rows, which are never backfilled and are
    # never resume candidates. Every session created from v7 onward,
    # including monoativo ones, always populates this.
    symbols: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(64))
    risk_config_json: Mapped[str] = mapped_column(Text)
    config_snapshot_json: Mapped[str] = mapped_column(Text)  # sanitized -- never contains secrets
    # Correção v1.1 #8: SHA-256 of the sanitized config snapshot + strategy
    # version + risk config (never a secret, since it's derived from
    # already-sanitized fields). A resumed session must match this exactly
    # -- see app.sessions.start_or_resume_session.
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="INICIALIZANDO")
    candles_count: Mapped[int] = mapped_column(Integer, default=0)
    signals_count: Mapped[int] = mapped_column(Integer, default=0)
    approvals_count: Mapped[int] = mapped_column(Integer, default=0)
    rejections_count: Mapped[int] = mapped_column(Integer, default=0)
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    fills_count: Mapped[int] = mapped_column(Integer, default=0)
    failures_count: Mapped[int] = mapped_column(Integer, default=0)
    reconciliations_count: Mapped[int] = mapped_column(Integer, default=0)


class SystemState(Base):
    """Singleton-ish row (id=1) holding the live TRADING_BLOCKED / kill-switch state."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trading_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kill_switch_engaged: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    api_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    state_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    clock_out_of_sync: Mapped[bool] = mapped_column(Boolean, default=False)
    # Fase 2, item 7.5: independent block-cause flags, each derived and
    # reset by its own code path -- never a shared/generic boolean.
    # recompute_trading_blocked() combines all of these (and the older ones
    # above) without ever letting clearing one silently clear another.
    reconciliation_diverged: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciliation_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    order_state_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    initialization_not_reconciled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Fase 2, item 7.8: process-is-running vs strategy-authorized-to-enter.
    # INICIALIZANDO -> OBSERVANDO -> ATIVO (operator action) -> PAUSADO
    # (operator action) -> BLOQUEADO (mirrors trading_blocked) -> ENCERRANDO.
    operational_state: Mapped[str] = mapped_column(String(16), default="INICIALIZANDO")
    active_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("operational_sessions.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# SHADOW (Fase 3.4.3) — instrumentação contrafactual, ISOLADA da operação.
#
# Estas três tabelas NUNCA se relacionam com orders/executions/positions nem
# com o patrimônio ou a sessão contábil operacional. Não há ForeignKey para
# nenhuma delas, de propósito: um registro shadow não pode, nem por engano de
# join, virar ou influenciar uma ordem real. O shadow observa e mede; quem
# decide operação continua sendo RiskEngine + gate operacional.
#
# Toda coluna temporal guarda o TEMPO DO CANDLE que provocou o evento, nunca
# o relógio de parede -- é o instante econômico do evento, e é o que torna o
# resultado reproduzível num reprocessamento.
# ---------------------------------------------------------------------------


class ShadowOpportunity(Base):
    """Uma decisão hipotética por (modelo, símbolo, bucket estratégico).

    Persistida para TODO sinal acionável, tenha o modelo aprovado ou não --
    é o denominador das métricas. A chave única torna o reprocessamento do
    mesmo candle um no-op no banco."""

    __tablename__ = "shadow_opportunities"
    __table_args__ = (
        # A identidade inclui o EXPERIMENTO: o mesmo simbolo/candle pode
        # aparecer legitimamente em experimentos diferentes.
        UniqueConstraint(
            "experiment_id", "symbol", "source_candle_open_time",
            name="uq_shadow_opportunity_experiment_symbol_candle",
        ),
        Index("ix_shadow_opportunity_model_time", "model", "source_candle_open_time"),
        CheckConstraint("atr > 0", name="ck_shadow_opportunity_atr_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    hypothesis_version: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    # Instante econômico: abertura do bucket estratégico que gerou o sinal.
    source_candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    strategy_timeframe_minutes: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(8))
    reference_price: Mapped[float] = mapped_column(Float)
    fast_sma: Mapped[float] = mapped_column(Float)
    slow_sma: Mapped[float] = mapped_column(Float)
    atr: Mapped[float] = mapped_column(Float)
    # abs(fast - slow) / atr -- a grandeza congelada do H2.
    normalized_separation: Mapped[float] = mapped_column(Float)
    # ATR% / custo de ida e volta -- observada, NUNCA usada como filtro aqui.
    cost_coverage_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    warmup_ready: Mapped[bool] = mapped_column(Boolean, default=True)
    signal_is_fresh: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Só para COMPARAÇÃO: o que o gate operacional de 3,0x teria decidido.
    # Não participa de nenhuma decisão shadow.
    operational_gate_would_approve: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShadowPosition(Base):
    """Posição hipotética aberta. Uma por modelo por vez, no máximo, porque
    cada portfólio respeita o mesmo teto de exposição global da operação."""

    __tablename__ = "shadow_positions"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "symbol", "opened_candle_time",
            name="uq_shadow_position_experiment_symbol_open",
        ),
        Index("ix_shadow_position_model_status", "model", "status"),
        CheckConstraint("qty > 0", name="ck_shadow_position_qty_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    # Preenchimento HIPOTÉTICO (preço adverso estimado). Nunca houve ordem.
    entry_fill_price: Mapped[float] = mapped_column(Float)
    reference_price: Mapped[float] = mapped_column(Float)
    notional_usd: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    entry_fee_usd: Mapped[float] = mapped_column(Float)
    entry_slippage_usd: Mapped[float] = mapped_column(Float)
    atr_at_decision: Mapped[float] = mapped_column(Float)
    normalized_separation: Mapped[float] = mapped_column(Float)
    opened_candle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShadowTrade(Base):
    """Trade hipotético encerrado -- o ledger de resultado do shadow."""

    __tablename__ = "shadow_trades"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "symbol", "opened_candle_time", "closed_candle_time",
            name="uq_shadow_trade_experiment_symbol_window",
        ),
        Index("ix_shadow_trade_model_closed", "model", "closed_candle_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    hypothesis_version: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    entry_fill_price: Mapped[float] = mapped_column(Float)
    exit_fill_price: Mapped[float] = mapped_column(Float)
    notional_usd: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(32))
    opened_candle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_candle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    gross_pnl_usd: Mapped[float] = mapped_column(Float)
    fees_usd: Mapped[float] = mapped_column(Float)
    slippage_usd: Mapped[float] = mapped_column(Float)
    net_pnl_usd: Mapped[float] = mapped_column(Float)
    normalized_separation: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class ShadowExperiment(Base):
    """IDENTIDADE IMUTÁVEL de um experimento shadow.

    Existe para que métricas jamais misturem períodos com timeframes,
    parâmetros de estratégia, custos, limites ou versões de hipótese
    diferentes. Qualquer mudança em parâmetro relevante encerra o
    experimento corrente e abre outro -- o antigo nunca é reescrito, e
    uma posição shadow pertence para sempre ao experimento que a criou.

    `config_fingerprint` é SHA-256 determinístico do snapshot sanitizado:
    mesma configuração, mesmo fingerprint, em qualquer máquina."""

    __tablename__ = "shadow_experiments"
    __table_args__ = (
        # NAO ha unicidade global em (model, config_fingerprint): o
        # fingerprint identifica uma CONFIGURACAO, nao uma execucao. Voltar
        # a uma configuracao antiga precisa criar uma execucao NOVA (A -> B
        # -> A2), com uid e inicio proprios, para que as duas nunca sejam
        # agregadas em silencio.
        Index("ix_shadow_experiment_model_fingerprint", "model", "config_fingerprint"),
        Index("ix_shadow_experiment_model_status", "model", "status"),
        # No maximo UM experimento ATIVO por modelo, garantido pelo BANCO e
        # nao pela disciplina da aplicacao. Indice PARCIAL: varios
        # experimentos ENCERRADOS do mesmo modelo (inclusive com o mesmo
        # fingerprint, como em A -> B -> A2) continuam legitimos. Declarado
        # aqui alem da migration v9 para que um banco criado por
        # `create_all` satisfaca o mesmo invariante que um banco migrado.
        Index(
            "uq_shadow_experiment_um_ativo_por_modelo", "model", unique=True,
            sqlite_where=text("status = 'ATIVO'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_uid: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    hypothesis_version: Mapped[str] = mapped_column(String(32))
    # Limiar CONGELADO desta hipótese. Mudá-lo cria experimento novo.
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy_timeframe_minutes: Mapped[int] = mapped_column(Integer)
    strategy_version: Mapped[str] = mapped_column(String(32))
    fast_period: Mapped[int] = mapped_column(Integer)
    slow_period: Mapped[int] = mapped_column(Integer)
    atr_period: Mapped[int] = mapped_column(Integer)
    fee_rate: Mapped[float] = mapped_column(Float)
    slippage_bps: Mapped[float] = mapped_column(Float)
    stop_loss_atr_multiple: Mapped[float] = mapped_column(Float)
    take_profit_atr_multiple: Mapped[float] = mapped_column(Float)
    max_position_usd: Mapped[float] = mapped_column(Float)
    max_total_exposure_usd: Mapped[float] = mapped_column(Float)
    min_order_notional_usd: Mapped[float] = mapped_column(Float)
    max_daily_loss_usd: Mapped[float] = mapped_column(Float)
    cooldown_after_losses: Mapped[int] = mapped_column(Integer)
    cooldown_minutes: Mapped[int] = mapped_column(Integer)
    config_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    config_snapshot_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ATIVO")
