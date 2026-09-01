"""Domain-specific exceptions. Raising one of these should always be safe-by-default:
callers that catch broadly must still end up blocking trading, never allowing it."""


class TradingSystemError(Exception):
    """Base class for all domain errors."""


class ProductionEndpointBlockedError(TradingSystemError):
    """Raised when configuration or a runtime request points at a non-demo/testnet host."""


class StaleDataError(TradingSystemError):
    """Raised when market data is older than the configured staleness threshold."""


class ClockDriftError(TradingSystemError):
    """Raised when local clock drift versus a trusted reference exceeds the safety threshold."""


class RiskRejectedError(TradingSystemError):
    """Raised (or represented as a rejection record) when the Risk Engine refuses a signal."""


class TradingBlockedError(TradingSystemError):
    """Raised when an action is attempted while the system is in TRADING_BLOCKED state."""


class DuplicateOrderError(TradingSystemError):
    """Raised when an idempotency key collision indicates a duplicate order submission."""


class ExchangeTimeoutError(TradingSystemError):
    """Raised when a call to the exchange (real or fake) exceeds its timeout."""


class RateLimitError(TradingSystemError):
    """Raised when the exchange reports a rate limit violation."""


class ExchangeDataIncompleteError(TradingSystemError):
    """Correção Fase 2 v1.2 #2/#4: raised when a paginated exchange query
    (open orders, execution history, funding transaction log) cannot be
    proven complete -- a malformed page, a repeated `nextPageCursor`, or a
    defensive page-count limit being exceeded. Distinct from
    ExchangeTimeoutError/RateLimitError (pure transport failures) because
    this can happen even when every individual HTTP call "succeeds" but the
    API's own pagination contract was violated or exhausted unsafely.
    Callers must never treat an incomplete result as if it were a genuine
    empty/complete one."""


class ReconciliationMismatchError(TradingSystemError):
    """Raised when local state disagrees with exchange-reported state after restart."""


class InvalidAIOutputError(TradingSystemError):
    """Raised when the AI shadow agent's output fails schema validation."""


class SecretLeakError(TradingSystemError):
    """Raised defensively if code path would emit a secret into logs or persistence."""


class StartingBalanceResetBlockedError(TradingSystemError):
    """Fase 3.1.1 (correção final da auditoria do PO, item 5): raised at
    startup when `Settings.paper_starting_balance_usd` differs from the
    value frozen in the previous operational session's snapshot AND at
    least one position is currently open. There is no capital
    deposit/withdrawal ledger in this system -- changing the starting
    balance is only a safe "reset the simulated wallet" operation when no
    open position exists to be silently re-based onto the new anchor.
    Refusing to start (rather than starting in a financially ambiguous
    state) matches this codebase's existing policy for unsafe startup
    conditions (see ProductionEndpointBlockedError, MigrationError)."""


class StrategyTimeframeChangeBlockedError(TradingSystemError):
    """Fase 3.2 (item 10 da decisão do PO): raised at startup when
    `Settings.strategy_timeframe_minutes` differs from the value frozen in
    the previous operational session's snapshot AND at least one position
    is open anywhere in the portfolio. A position opened under one
    strategic cadence must never be silently taken over by another: its
    stop/target were sized from the ATR of the previous timeframe, and
    `Orchestrator._check_stop_take` would keep managing it under premises
    that no longer hold. Same "refuse to start rather than continue in an
    ambiguous state" policy as StartingBalanceResetBlockedError. Raised
    BEFORE any write, so no session is ended, none is created, and no
    partial state is persisted. Deliberately scoped to the timeframe
    alone -- other strategy changes remain unguarded."""


class ReplayFixtureMissingError(TradingSystemError):
    """Fase 3.2 (correção final da auditoria do PO, item 1): raised at
    startup when a symbol configured for REPLAY/PAPER_LOCAL has no fixture
    of its OWN.

    Until this correction, any symbol without its own file silently fell
    back to `replay_btcusdt.json`. Documenting that the series was
    "borrowed" did not prevent a single one of its consequences: a fake
    price for that symbol, signals duplicated from another asset,
    contaminated metrics, a misleading demonstration, and a symbol being
    operated on data that belongs to a different one. There is no safe
    fallback here -- refusing to start is the only honest option, matching
    this codebase's existing policy for unsafe startup conditions
    (StartingBalanceResetBlockedError, StrategyTimeframeChangeBlockedError,
    MigrationError). Raised BEFORE the database is even opened, so no
    session is created and no candle is ever persisted."""
