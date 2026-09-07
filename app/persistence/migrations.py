"""Correction v1.3 #1: small, dependency-free versioned migration system.

`Base.metadata.create_all()` only creates MISSING tables -- it never alters
an existing one. A database created by an earlier version of this app (Fase
1 baseline, or the v1.1 correction) has a schema that no longer matches
`app/persistence/models.py`, and starting the current app against it fails
hard (e.g. "no such column: system_state.clock_out_of_sync"). This module
fixes that: every schema change since the first public Fase 1 commit is
expressed as a numbered, idempotent, transactional migration, tracked in a
`schema_migrations` table.

Design goals (all required by the correction):
- deterministic: migrations run in a fixed numeric order, never reordered;
- transactional: the whole run happens inside one DB transaction -- any
  failure rolls back everything, never leaving a half-applied schema;
- idempotent: every individual ALTER is guarded by an existence check, so
  re-running an already-applied migration (or one whose target already
  exists in the DB for other reasons) is a safe no-op;
- testable: `run_migrations(engine)` is a plain function tests can call
  against any engine, including one pre-loaded with legacy-schema fixture
  data (see tests/test_migrations.py);
- version-aware: `current_schema_version(engine)` always reflects reality;
- never fakes success: an exception during any migration propagates after
  the transaction is rolled back -- nothing is ever recorded as applied
  unless it actually committed.

Schema history:
  v0 -> v1  (Fase 1 baseline -> correction v1.1): add
            system_state.state_ambiguous, orders.is_close, and relax
            orders.stop_loss from NOT NULL to nullable (close orders carry
            no stop-loss).
  v1 -> v2  (v1.1 -> correction v1.2): add system_state.clock_out_of_sync,
            and a unique index on candles(symbol, timeframe, open_time) --
            historical duplicate candle rows (if any) are deduplicated
            first, keeping the earliest-inserted (lowest id) row as the
            canonical record, deterministically and before the index is
            created.
  v2 -> v3  (Fase 2 v1.0): adds the order status state machine bookkeeping
            (orders.filled_qty/avg_fill_price/fees_total, the order_events
            audit table), operational sessions (operational_sessions
            table), and the new independent SystemState block-cause flags
            (reconciliation_diverged, reconciliation_stale,
            order_state_unknown, initialization_not_reconciled,
            last_reconciliation_at, operational_state, active_session_id),
            plus optional order_id/session_id links on
            failures_reconciliations.
  v3 -> v4  (correção Fase 2 v1.1): adds executions.exchange_fill_id +
            a unique index on (order_id, exchange_fill_id) -- the
            persistent, idempotent fill ledger (app/execution/fill_ledger.py)
            that replaced overwriting cumulative totals with per-fill,
            delta-based application. See docs/ORDEM_E_FILLS.md.
  v4 -> v5  (correção Fase 2 v1.2): adds orders.pending_exchange_status and
            orders.fills_sync_status -- separates "status the exchange
            reported" from "fill history proven complete", so a terminal
            status (Filled/Cancelled) is never persisted while the fill
            sync is still incomplete (the order stays recoverable). See
            docs/ORDEM_E_FILLS.md.
  v5 -> v6  (correção Fase 2 v1.3): adds the funding_collection_checkpoints
            table (one row per symbol, unique index on symbol) -- an
            explicit, persisted proof of funding-collection COVERAGE,
            separate from the funding_events themselves and never derived
            from their MAX(occurred_at) (unsafe under newest-first
            pagination -- see app/execution/funding.py). See docs/METRICAS.md.
  v6 -> v7  (Fase 3 multiativo, fundação): adds
            operational_sessions.symbols (nullable JSON array of the
            canonical, ordered symbol list) -- the new portfolio-level
            session identity. Legacy rows keep `symbols = NULL` and are
            never backfilled/rewritten (read-only history); only new
            sessions populate it (app/sessions.py). Also creates a partial
            UNIQUE index on positions(symbol) WHERE status='OPEN' -- closes
            the one real duplicate-by-(symbol, natural identity) gap found
            auditing the schema for multi-symbol support (nothing
            previously stopped two OPEN rows for the same symbol at the
            database level). See docs/MIGRACOES.md and
            docs/ARQUITETURA.md ("Multiativo").
  v7 -> v8  (Fase 3.1, correção final auditoria PO): adds
            strategy_signals.source_candle_open_time (nullable DATETIME) --
            the deterministic identity of the real candle (candle.open_time)
            that produced a signal, used by the chart panel to place BUY/SELL
            markers. Never derived from price or from created_at. Legacy
            rows keep it NULL forever and are never backfilled/rewritten --
            the API omits the marker for such signals rather than inventing
            an association. See docs/PAINEL_GRAFICO.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.persistence.models import Base

CURRENT_SCHEMA_VERSION = 9


class MigrationError(Exception):
    """Raised when a migration cannot be applied. The triggering exception
    is always chained (`raise ... from exc`) so the root cause is visible;
    the message itself is in Portuguese since this can surface to an
    operator starting the app against an old database."""


class SchemaDivergenceError(MigrationError):
    """Correction v1.4 #3: raised when `schema_migrations` records a
    version as applied, but the real schema does not actually satisfy that
    version's invariants (e.g. someone dropped an index by hand, or an
    older bug recorded success without actually altering everything). The
    system refuses to guess or silently "repair" this -- it stops safely
    and asks for manual intervention, since automatically altering a
    database in an already-unknown state risks making a real corruption
    worse or masking it."""


@dataclass(frozen=True)
class MigrationReport:
    starting_version: int
    ending_version: int
    applied: list[int]
    stamped_only: bool  # True when a brand-new DB was created at head and merely stamped, not ALTERed


def _table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).fetchone()
    return row is not None


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def _column_is_nullable(conn: Connection, table: str, column: str) -> bool:
    """False if the column doesn't exist OR is NOT NULL; True only if it
    exists and genuinely accepts NULL."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    for row in rows:
        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
        if row[1] == column:
            return row[3] == 0
    return False


def _has_unique_index_on(conn: Connection, table: str, columns: set[str]) -> bool:
    """True if SOME unique index exists on `table` covering exactly
    `columns` -- matched by structure (the set of indexed columns), never by
    a hardcoded index name, so a differently-named index providing the same
    guarantee still counts (correction v1.4 #3)."""
    index_list = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
    for index_row in index_list:
        # PRAGMA index_list columns: (seq, name, unique, origin, partial)
        index_name, is_unique = index_row[1], index_row[2]
        if not is_unique:
            continue
        index_info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
        indexed_columns = {r[2] for r in index_info}  # (seqno, cid, name)
        if indexed_columns == columns:
            return True
    return False


def _has_check_constraint(conn: Connection, table: str, exact_fragment: str) -> bool:
    """SQLite exposes no `PRAGMA` for CHECK constraints -- the only way to
    detect one is to inspect the table's own recorded DDL text in
    `sqlite_master`. Matched by an EXACT substring this module itself
    always writes verbatim (never a loose/normalized comparison), so a
    false positive is not possible; a false negative just means the
    (idempotent) rebuild below runs again, which is always safe."""
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).fetchone()
    return row is not None and row[0] is not None and exact_fragment in row[0]


def _has_unique_partial_index_on(conn: Connection, table: str, columns: set[str]) -> bool:
    """Like `_has_unique_index_on`, but only counts a unique index that is
    ALSO partial (`PRAGMA index_list`'s `partial` flag) -- a plain
    (non-partial) unique index on the same columns would not satisfy this,
    since it would forbid ANY two rows sharing those column values (e.g. two
    historical CLOSED positions for the same symbol), not just two
    simultaneously-OPEN ones."""
    index_list = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
    for index_row in index_list:
        # PRAGMA index_list columns: (seq, name, unique, origin, partial)
        index_name, is_unique, is_partial = index_row[1], index_row[2], index_row[4]
        if not is_unique or not is_partial:
            continue
        index_info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
        indexed_columns = {r[2] for r in index_info}  # (seqno, cid, name)
        if indexed_columns == columns:
            return True
    return False


def _ensure_schema_migrations_table(conn: Connection) -> None:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "description TEXT NOT NULL, "
        "applied_at TEXT NOT NULL"
        ")"
    ))


def _applied_versions(conn: Connection) -> set[int]:
    rows = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def _record_migration(conn: Connection, version: int, description: str) -> None:
    conn.execute(
        text("INSERT INTO schema_migrations (version, description, applied_at) VALUES (:v, :d, :t)"),
        {"v": version, "d": description, "t": datetime.now(timezone.utc).isoformat()},
    )


def _migrate_to_v1(conn: Connection) -> None:
    """Fase 1 baseline -> correction v1.1 schema."""
    if not _column_exists(conn, "system_state", "state_ambiguous"):
        conn.execute(text(
            "ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"
        ))

    # Correction v1.4 #3: a database can have `is_close` already added by
    # some other means (or a prior partial/aborted attempt) while
    # `stop_loss` is STILL NOT NULL -- checking only the column's presence
    # missed that case entirely and left a database silently stuck without
    # a nullable stop_loss forever. Rebuild whenever EITHER invariant of
    # the target shape is not yet met.
    needs_orders_rebuild = (
        not _column_exists(conn, "orders", "is_close")
        or not _column_is_nullable(conn, "orders", "stop_loss")
    )
    if needs_orders_rebuild:
        # SQLite cannot drop a NOT NULL constraint (orders.stop_loss) with a
        # plain ALTER TABLE, and orders.is_close is a new column -- both are
        # handled by rebuilding the table: create the new shape, copy every
        # existing row across (is_close defaults to 0 -- every pre-existing
        # order was an opening order, since close-via-risk-engine did not
        # exist yet), drop the old table, rename the new one into place.
        conn.execute(text(
            "CREATE TABLE orders_v1 ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "idempotency_key VARCHAR(128) NOT NULL, "
            "risk_evaluation_id INTEGER NOT NULL, "
            "symbol VARCHAR(32) NOT NULL, "
            "side VARCHAR(8) NOT NULL, "
            "qty FLOAT NOT NULL, "
            "stop_loss FLOAT, "
            "take_profit FLOAT, "
            "is_close BOOLEAN NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL, "
            "exchange_order_id VARCHAR(128), "
            "mode VARCHAR(16) NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL"
            ")"
        ))
        # Preserve real is_close values if the column already existed on the
        # source table (e.g. a partially-migrated database); default new
        # rows to 0 (opening order) only when the column didn't exist yet.
        is_close_source_expr = "is_close" if _column_exists(conn, "orders", "is_close") else "0"
        conn.execute(text(
            "INSERT INTO orders_v1 (id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, is_close, status, exchange_order_id, mode, created_at, updated_at) "
            f"SELECT id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            f"stop_loss, take_profit, {is_close_source_expr}, status, exchange_order_id, mode, "
            f"created_at, updated_at "
            "FROM orders"
        ))
        conn.execute(text("DROP TABLE orders"))
        conn.execute(text("ALTER TABLE orders_v1 RENAME TO orders"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_idempotency_key ON orders (idempotency_key)"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_symbol ON orders (symbol)"))


def _migrate_to_v2(conn: Connection) -> None:
    """Correction v1.1 -> correction v1.2 schema."""
    if not _column_exists(conn, "system_state", "clock_out_of_sync"):
        conn.execute(text(
            "ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"
        ))

    if not _has_unique_index_on(conn, "candles", {"symbol", "timeframe", "open_time"}):
        # Deterministic, documented dedup strategy (required by the
        # correction): for any (symbol, timeframe, open_time) group with
        # more than one row, keep only the row with the lowest id (the
        # earliest one this app ever inserted) and delete the rest, before
        # the unique index is created.
        conn.execute(text(
            "DELETE FROM candles WHERE id NOT IN ("
            "SELECT MIN(id) FROM candles GROUP BY symbol, timeframe, open_time"
            ")"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_candle_symbol_timeframe_open_time "
            "ON candles (symbol, timeframe, open_time)"
        ))


def _migrate_to_v3(conn: Connection) -> None:
    """Correction v1.2 -> Fase 2 v1.0 schema."""
    # New tables -- CREATE TABLE IF NOT EXISTS is inherently idempotent, no
    # existence guard needed (unlike ALTER TABLE ADD COLUMN below).
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS operational_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_uid VARCHAR(36) NOT NULL, "
        "mode VARCHAR(16) NOT NULL, "
        "symbol VARCHAR(32) NOT NULL, "
        "timeframe VARCHAR(8) NOT NULL, "
        "started_at DATETIME NOT NULL, "
        "ended_at DATETIME, "
        "end_reason VARCHAR(255), "
        "strategy_version VARCHAR(64) NOT NULL, "
        "risk_config_json TEXT NOT NULL, "
        "config_snapshot_json TEXT NOT NULL, "
        "status VARCHAR(16) NOT NULL DEFAULT 'INICIALIZANDO', "
        "candles_count INTEGER NOT NULL DEFAULT 0, "
        "signals_count INTEGER NOT NULL DEFAULT 0, "
        "approvals_count INTEGER NOT NULL DEFAULT 0, "
        "rejections_count INTEGER NOT NULL DEFAULT 0, "
        "orders_count INTEGER NOT NULL DEFAULT 0, "
        "fills_count INTEGER NOT NULL DEFAULT 0, "
        "failures_count INTEGER NOT NULL DEFAULT 0, "
        "reconciliations_count INTEGER NOT NULL DEFAULT 0"
        ")"
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_operational_sessions_session_uid "
        "ON operational_sessions (session_uid)"
    ))
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS order_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "order_id INTEGER NOT NULL, "
        "from_status VARCHAR(16) NOT NULL, "
        "to_status VARCHAR(16) NOT NULL, "
        "detail TEXT, "
        "created_at DATETIME NOT NULL"
        ")"
    ))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_order_events_order_id ON order_events (order_id)"))

    # orders: cumulative fill bookkeeping + the reference price used for
    # slippage tracking (item 7.6).
    for column, ddl_type in (
        ("filled_qty", "FLOAT NOT NULL DEFAULT 0"),
        ("avg_fill_price", "FLOAT NOT NULL DEFAULT 0"),
        ("fees_total", "FLOAT NOT NULL DEFAULT 0"),
        ("reference_price", "FLOAT"),
    ):
        if not _column_exists(conn, "orders", column):
            conn.execute(text(f"ALTER TABLE orders ADD COLUMN {column} {ddl_type}"))

    # failures_reconciliations: optional links to order/session.
    if not _column_exists(conn, "failures_reconciliations", "order_id"):
        conn.execute(text("ALTER TABLE failures_reconciliations ADD COLUMN order_id INTEGER"))
    if not _column_exists(conn, "failures_reconciliations", "session_id"):
        conn.execute(text("ALTER TABLE failures_reconciliations ADD COLUMN session_id INTEGER"))

    # system_state: new independent block-cause flags + operational state.
    for column, ddl_type in (
        ("reconciliation_diverged", "BOOLEAN NOT NULL DEFAULT 0"),
        ("reconciliation_stale", "BOOLEAN NOT NULL DEFAULT 0"),
        ("order_state_unknown", "BOOLEAN NOT NULL DEFAULT 0"),
        ("initialization_not_reconciled", "BOOLEAN NOT NULL DEFAULT 1"),
        ("last_reconciliation_at", "DATETIME"),
        ("operational_state", "VARCHAR(16) NOT NULL DEFAULT 'INICIALIZANDO'"),
        ("active_session_id", "INTEGER"),
    ):
        if not _column_exists(conn, "system_state", column):
            conn.execute(text(f"ALTER TABLE system_state ADD COLUMN {column} {ddl_type}"))


def _migrate_to_v4(conn: Connection) -> None:
    """Correção Fase 2 v1.1: ledger de fills idempotente + reconciliação
    estruturada + funding + fingerprint de sessão."""
    if not _column_exists(conn, "executions", "exchange_fill_id"):
        conn.execute(text("ALTER TABLE executions ADD COLUMN exchange_fill_id VARCHAR(128)"))
    if not _has_unique_index_on(conn, "executions", {"order_id", "exchange_fill_id"}):
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_execution_order_exchange_fill_id "
            "ON executions (order_id, exchange_fill_id)"
        ))

    if not _column_exists(conn, "failures_reconciliations", "mismatches_json"):
        conn.execute(text("ALTER TABLE failures_reconciliations ADD COLUMN mismatches_json TEXT"))

    if not _column_exists(conn, "operational_sessions", "config_fingerprint"):
        conn.execute(text("ALTER TABLE operational_sessions ADD COLUMN config_fingerprint VARCHAR(64)"))

    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS funding_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "funding_id VARCHAR(128) NOT NULL, "
        "symbol VARCHAR(32) NOT NULL, "
        "amount FLOAT NOT NULL, "
        "occurred_at DATETIME NOT NULL, "
        "created_at DATETIME NOT NULL"
        ")"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_funding_events_symbol ON funding_events (symbol)"
    ))
    if not _has_unique_index_on(conn, "funding_events", {"funding_id"}):
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_funding_event_funding_id ON funding_events (funding_id)"
        ))


def _migrate_to_v5(conn: Connection) -> None:
    """Correção Fase 2 v1.2 #1/#7: separa "status reportado pela corretora"
    de "histórico de fills comprovadamente completo" -- ver
    docs/ORDEM_E_FILLS.md."""
    if not _column_exists(conn, "orders", "pending_exchange_status"):
        conn.execute(text("ALTER TABLE orders ADD COLUMN pending_exchange_status VARCHAR(16)"))
    if not _column_exists(conn, "orders", "fills_sync_status"):
        conn.execute(text(
            "ALTER TABLE orders ADD COLUMN fills_sync_status VARCHAR(16) NOT NULL DEFAULT 'COMPLETE'"
        ))


def _migrate_to_v6(conn: Connection) -> None:
    """Correção Fase 2 v1.3 #1/#3: checkpoint explícito e persistido de
    cobertura de coleta de funding, um por símbolo -- ver
    docs/METRICAS.md."""
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS funding_collection_checkpoints ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "symbol VARCHAR(32) NOT NULL, "
        "covered_until DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL"
        ")"
    ))
    if not _has_unique_index_on(conn, "funding_collection_checkpoints", {"symbol"}):
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_funding_checkpoint_symbol ON funding_collection_checkpoints (symbol)"
        ))


# Written verbatim into the rebuilt table's DDL below AND used as the exact
# detection fragment for `_has_check_constraint` -- keep these in sync.
_SYMBOL_OR_SYMBOLS_CHECK_SQL = "CHECK (symbol IS NOT NULL OR symbols IS NOT NULL)"


def _migrate_to_v7(conn: Connection) -> None:
    """Fase 3 multiativo (fundação + rodada de correção obrigatória do PO):

    - `operational_sessions` gains `symbols` (nullable JSON, never
      backfilled for pre-existing rows -- see module docstring) and
      `symbol` is relaxed from NOT NULL to nullable (a genuinely
      multi-symbol session leaves it NULL rather than lying with a single
      value -- see app/sessions.py).
    - `operational_sessions` gains `CHECK (symbol IS NOT NULL OR symbols IS
      NOT NULL)` -- a row can never have BOTH null. Legacy rows already
      satisfy this (`symbol NOT NULL`, `symbols NULL`); new rows satisfy it
      by construction (`symbols` is always populated -- see
      app/sessions.py::start_or_resume_session).
    - `operational_sessions` gains a partial UNIQUE index on
      `(mode, symbols) WHERE ended_at IS NULL AND symbols IS NOT NULL` --
      at most one ACTIVE session per portfolio (mode + ordered symbol
      list); legacy rows (`symbols IS NULL`) are excluded from this
      constraint entirely, so historical data can never violate it.
    - `positions` gains a partial UNIQUE index on `(symbol) WHERE
      status='OPEN'`.

    SQLite cannot drop a NOT NULL constraint or add a CHECK with a plain
    ALTER TABLE, so -- same technique as `_migrate_to_v1`'s `orders`
    rebuild -- the `operational_sessions` table is rebuilt: new shape
    created (with the CHECK baked into the CREATE TABLE, the only place
    SQLite accepts one), every existing row copied across unchanged
    (`symbols` stays NULL for them, exactly as before -- legacy rows are
    NEVER backfilled/rewritten), old table dropped, new one renamed into
    place. Before creating the new-active-session-per-portfolio unique
    index, pre-existing data is checked for violations first -- if found,
    the migration raises and refuses to proceed rather than silently
    deleting or "fixing" real rows (decisão do PO)."""
    needs_sessions_rebuild = (
        not _column_exists(conn, "operational_sessions", "symbols")
        or not _column_is_nullable(conn, "operational_sessions", "symbol")
        or not _has_check_constraint(conn, "operational_sessions", _SYMBOL_OR_SYMBOLS_CHECK_SQL)
    )
    if needs_sessions_rebuild:
        conn.execute(text(
            "CREATE TABLE operational_sessions_v7 ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_uid VARCHAR(36) NOT NULL, "
            "mode VARCHAR(16) NOT NULL, "
            "symbol VARCHAR(32), "
            "symbols TEXT, "
            "timeframe VARCHAR(8) NOT NULL, "
            "started_at DATETIME NOT NULL, "
            "ended_at DATETIME, "
            "end_reason VARCHAR(255), "
            "strategy_version VARCHAR(64) NOT NULL, "
            "risk_config_json TEXT NOT NULL, "
            "config_snapshot_json TEXT NOT NULL, "
            "config_fingerprint VARCHAR(64), "
            "status VARCHAR(16) NOT NULL DEFAULT 'INICIALIZANDO', "
            "candles_count INTEGER NOT NULL DEFAULT 0, "
            "signals_count INTEGER NOT NULL DEFAULT 0, "
            "approvals_count INTEGER NOT NULL DEFAULT 0, "
            "rejections_count INTEGER NOT NULL DEFAULT 0, "
            "orders_count INTEGER NOT NULL DEFAULT 0, "
            "fills_count INTEGER NOT NULL DEFAULT 0, "
            "failures_count INTEGER NOT NULL DEFAULT 0, "
            "reconciliations_count INTEGER NOT NULL DEFAULT 0, "
            f"{_SYMBOL_OR_SYMBOLS_CHECK_SQL}"
            ")"
        ))
        symbols_source_expr = (
            "symbols" if _column_exists(conn, "operational_sessions", "symbols") else "NULL"
        )
        conn.execute(text(
            "INSERT INTO operational_sessions_v7 (id, session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, end_reason, strategy_version, risk_config_json, config_snapshot_json, "
            "config_fingerprint, status, candles_count, signals_count, approvals_count, rejections_count, "
            "orders_count, fills_count, failures_count, reconciliations_count) "
            f"SELECT id, session_uid, mode, symbol, {symbols_source_expr}, timeframe, "
            "started_at, ended_at, end_reason, strategy_version, risk_config_json, config_snapshot_json, "
            "config_fingerprint, status, candles_count, signals_count, approvals_count, rejections_count, "
            "orders_count, fills_count, failures_count, reconciliations_count "
            "FROM operational_sessions"
        ))
        conn.execute(text("DROP TABLE operational_sessions"))
        conn.execute(text("ALTER TABLE operational_sessions_v7 RENAME TO operational_sessions"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_operational_sessions_session_uid "
            "ON operational_sessions (session_uid)"
        ))

    if not _has_unique_partial_index_on(conn, "operational_sessions", {"mode", "symbols"}):
        # Decisão do PO: verificar duplicidades ativas ANTES de criar o
        # índice -- nunca apagar/corrigir silenciosamente dados reais. Uma
        # violação aqui interrompe a migração inteira (a exceção propaga e
        # a transação é revertida por completo, como qualquer outra falha
        # de migração -- ver run_migrations()).
        duplicates = conn.execute(text(
            "SELECT mode, symbols, COUNT(*) AS c FROM operational_sessions "
            "WHERE ended_at IS NULL AND symbols IS NOT NULL "
            "GROUP BY mode, symbols HAVING COUNT(*) > 1"
        )).fetchall()
        if duplicates:
            details = "; ".join(f"mode={d[0]!r} symbols={d[1]!r} ({d[2]} sessões ativas)" for d in duplicates)
            raise MigrationError(
                "Migração v7 recusada: existem múltiplas sessões operacionais ATIVAS "
                f"(ended_at IS NULL) para a mesma carteira (mode + symbols): {details}. "
                "Isso violaria a nova regra de no máximo uma sessão ativa por carteira. "
                "Nenhuma linha foi apagada ou alterada -- encerre manualmente as sessões "
                "duplicadas (ver docs/SESSOES_OPERACIONAIS.md) antes de reiniciar a aplicação."
            )
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_operational_session_active_per_portfolio "
            "ON operational_sessions (mode, symbols) "
            "WHERE ended_at IS NULL AND symbols IS NOT NULL"
        ))

    if not _has_unique_partial_index_on(conn, "positions", {"symbol"}):
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_position_open_symbol ON positions (symbol) WHERE status = 'OPEN'"
        ))


def _migrate_to_v8(conn: Connection) -> None:
    """Fase 3.1 (correção final auditoria PO): adiciona
    strategy_signals.source_candle_open_time -- coluna nullable, simples
    ALTER TABLE ADD COLUMN (sem rebuild, pois não há constraint NOT NULL/
    CHECK envolvida). Nunca preenchida retroativamente para linhas legadas
    -- fica NULL para sempre nesses casos, exatamente como a coluna
    `symbols` de v7."""
    if not _column_exists(conn, "strategy_signals", "source_candle_open_time"):
        conn.execute(text(
            "ALTER TABLE strategy_signals ADD COLUMN source_candle_open_time DATETIME"
        ))


def _migrate_to_v9(conn: Connection) -> None:
    """Fase 3.4.3: cria as tres tabelas do motor SHADOW.

    Sao tabelas NOVAS e isoladas -- nenhuma tabela operacional e' alterada,
    nenhuma FK aponta para orders/executions/positions, e nada e' escrito
    retroativamente. Por isso a migration e' puramente aditiva e nao precisa
    de rebuild.

    Idempotente por construcao: cada CREATE usa IF NOT EXISTS, e as chaves
    unicas garantem que reprocessar o mesmo candle nao duplique registro."""
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS shadow_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_uid VARCHAR(64) NOT NULL UNIQUE,  -- identidade da EXECUCAO
            model VARCHAR(64) NOT NULL,
            hypothesis_version VARCHAR(32) NOT NULL,
            threshold FLOAT,
            strategy_timeframe_minutes INTEGER NOT NULL,
            strategy_version VARCHAR(32) NOT NULL,
            fast_period INTEGER NOT NULL,
            slow_period INTEGER NOT NULL,
            atr_period INTEGER NOT NULL,
            fee_rate FLOAT NOT NULL,
            slippage_bps FLOAT NOT NULL,
            stop_loss_atr_multiple FLOAT NOT NULL,
            take_profit_atr_multiple FLOAT NOT NULL,
            max_position_usd FLOAT NOT NULL,
            max_total_exposure_usd FLOAT NOT NULL,
            min_order_notional_usd FLOAT NOT NULL,
            max_daily_loss_usd FLOAT NOT NULL,
            cooldown_after_losses INTEGER NOT NULL,
            cooldown_minutes INTEGER NOT NULL,
            config_fingerprint VARCHAR(64) NOT NULL,
            config_snapshot_json TEXT NOT NULL,
            started_at DATETIME NOT NULL,
            ended_at DATETIME,
            end_reason TEXT,
            status VARCHAR(16) NOT NULL DEFAULT 'ATIVO'
        )
    """))
    # fingerprint e' ATRIBUTO INDEXADO, nunca exclusivo -- voltar a uma
    # configuracao antiga cria execucao nova (A -> B -> A2).
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_shadow_experiment_model_fingerprint "
        "ON shadow_experiments (model, config_fingerprint)"))
    # No maximo UM experimento ativo por modelo, garantido PELO BANCO --
    # indice unico PARCIAL, nao disciplina da aplicacao.
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_experiment_um_ativo_por_modelo "
        "ON shadow_experiments (model) WHERE status = 'ATIVO'"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_shadow_experiment_model_status "
        "ON shadow_experiments (model, status)"))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS shadow_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL,
            model VARCHAR(64) NOT NULL,
            hypothesis_version VARCHAR(32) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            source_candle_open_time DATETIME NOT NULL,
            strategy_timeframe_minutes INTEGER NOT NULL,
            direction VARCHAR(8) NOT NULL,
            reference_price FLOAT NOT NULL,
            fast_sma FLOAT NOT NULL,
            slow_sma FLOAT NOT NULL,
            atr FLOAT NOT NULL,
            normalized_separation FLOAT NOT NULL,
            cost_coverage_ratio FLOAT,
            approved BOOLEAN NOT NULL,
            reason TEXT NOT NULL,
            warmup_ready BOOLEAN NOT NULL DEFAULT 1,
            signal_is_fresh BOOLEAN,
            operational_gate_would_approve BOOLEAN,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_shadow_opportunity_atr_positive CHECK (atr > 0)
        )
    """))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_opportunity_experiment_symbol_candle "
        "ON shadow_opportunities (experiment_id, symbol, source_candle_open_time)"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_shadow_opportunity_model_time "
        "ON shadow_opportunities (model, source_candle_open_time)"))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS shadow_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL,
            model VARCHAR(64) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            qty FLOAT NOT NULL,
            entry_fill_price FLOAT NOT NULL,
            reference_price FLOAT NOT NULL,
            notional_usd FLOAT NOT NULL,
            stop_loss FLOAT NOT NULL,
            take_profit FLOAT NOT NULL,
            entry_fee_usd FLOAT NOT NULL,
            entry_slippage_usd FLOAT NOT NULL,
            atr_at_decision FLOAT NOT NULL,
            normalized_separation FLOAT NOT NULL,
            opened_candle_time DATETIME NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_shadow_position_qty_positive CHECK (qty > 0)
        )
    """))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_position_experiment_symbol_open "
        "ON shadow_positions (experiment_id, symbol, opened_candle_time)"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_shadow_position_model_status "
        "ON shadow_positions (model, status)"))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL,
            model VARCHAR(64) NOT NULL,
            hypothesis_version VARCHAR(32) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            qty FLOAT NOT NULL,
            entry_fill_price FLOAT NOT NULL,
            exit_fill_price FLOAT NOT NULL,
            notional_usd FLOAT NOT NULL,
            stop_loss FLOAT NOT NULL,
            take_profit FLOAT NOT NULL,
            exit_reason VARCHAR(32) NOT NULL,
            opened_candle_time DATETIME NOT NULL,
            closed_candle_time DATETIME NOT NULL,
            duration_minutes INTEGER NOT NULL,
            gross_pnl_usd FLOAT NOT NULL,
            fees_usd FLOAT NOT NULL,
            slippage_usd FLOAT NOT NULL,
            net_pnl_usd FLOAT NOT NULL,
            normalized_separation FLOAT NOT NULL,
            created_at DATETIME NOT NULL
        )
    """))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_trade_experiment_symbol_window "
        "ON shadow_trades (experiment_id, symbol, opened_candle_time, closed_candle_time)"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_shadow_trade_model_closed "
        "ON shadow_trades (model, closed_candle_time)"))
    for tabela in ("shadow_opportunities", "shadow_positions", "shadow_trades"):
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS ix_{tabela}_experiment "
            f"ON {tabela} (experiment_id)"))


def _v9_invariants_satisfied(conn: Connection) -> bool:
    """As tres tabelas shadow existem E carregam suas chaves unicas -- sem
    elas o reprocessamento duplicaria registro, que e' exatamente o que a
    idempotencia do shadow promete."""
    if not all(_table_exists(conn, t) for t in
               ("shadow_experiments", "shadow_opportunities",
                "shadow_positions", "shadow_trades")):
        return False
    # A identidade do experimento precisa existir em toda linha shadow --
    # sem ela, metricas de configuracoes diferentes poderiam se misturar.
    if not all(_column_exists(conn, t, "experiment_id") for t in
               ("shadow_opportunities", "shadow_positions", "shadow_trades")):
        return False
    # Exatamente UM experimento ativo por modelo, garantido pelo banco.
    if not _has_unique_partial_index_on(conn, "shadow_experiments", {"model"}):
        return False
    if not _has_unique_index_on(conn, "shadow_experiments", {"experiment_uid"}):
        return False
    # Idempotencia por EXPERIMENTO, nao por modelo.
    if not _has_unique_index_on(conn, "shadow_opportunities",
                                {"experiment_id", "symbol", "source_candle_open_time"}):
        return False
    if not _has_unique_index_on(conn, "shadow_positions",
                                {"experiment_id", "symbol", "opened_candle_time"}):
        return False
    if not _has_unique_index_on(conn, "shadow_trades",
                                {"experiment_id", "symbol", "opened_candle_time",
                                 "closed_candle_time"}):
        return False
    # Verifica por COLUNAS, nunca por nome do indice. Um banco criado pela
    # migration tem indices nomeados (CREATE UNIQUE INDEX); um criado por
    # `Base.metadata.create_all` declara a mesma unicidade como CONSTRAINT
    # DE TABELA, que o SQLite materializa num `sqlite_autoindex_*`. Os dois
    # esquemas sao equivalentes e ambos precisam satisfazer o invariante --
    # mesmo criterio que `_v2_invariants_satisfied` ja adota.
    return True


# Order matters: applied strictly in ascending version order.
MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "Adiciona system_state.state_ambiguous, orders.is_close; relaxa orders.stop_loss para opcional.", _migrate_to_v1),
    (2, "Adiciona system_state.clock_out_of_sync; cria índice único em candles (dedup determinístico antes).", _migrate_to_v2),
    (3, "Adiciona máquina de estados de ordens (filled_qty/avg_fill_price/fees_total, order_events), "
        "sessões operacionais (operational_sessions) e novas causas independentes de bloqueio em system_state.",
     _migrate_to_v3),
    (4, "Adiciona executions.exchange_fill_id e índice único (order_id, exchange_fill_id) -- ledger de fills "
        "idempotente; failures_reconciliations.mismatches_json -- resultado estruturado de reconciliação; "
        "operational_sessions.config_fingerprint -- retomada de sessão sensível a mudança de configuração; "
        "tabela funding_events -- coleta idempotente de funding.",
     _migrate_to_v4),
    (5, "Adiciona orders.pending_exchange_status e orders.fills_sync_status -- separa status reportado "
        "pela corretora de sincronização comprovada do histórico de fills, para nunca terminalizar uma "
        "ordem antes de todos os fills serem aplicados.",
     _migrate_to_v5),
    (6, "Cria a tabela funding_collection_checkpoints (uma linha por símbolo, índice único em symbol) -- "
        "checkpoint explícito de cobertura de coleta de funding, nunca derivado do maior occurred_at já "
        "persistido em funding_events.",
     _migrate_to_v6),
    (7, "Adiciona operational_sessions.symbols (JSON nullable, lista ordenada e canônica -- nunca "
        "retroativo em linhas legadas) e índice único parcial em positions(symbol) WHERE status='OPEN' -- "
        "fundação da Fase 3 multiativo.",
     _migrate_to_v7),
    (8, "Adiciona strategy_signals.source_candle_open_time (DATETIME nullable) -- identidade determinística "
        "do candle.open_time que gerou o sinal, nunca derivada de preço/created_at, nunca retroativa em "
        "linhas legadas -- painel gráfico Fase 3.1.",
     _migrate_to_v8),
    (9, "Fase 3.4.3: cria shadow_experiments, shadow_opportunities, shadow_positions e "
        "shadow_trades -- "
        "instrumentacao contrafactual isolada, puramente aditiva, sem FK para tabelas "
        "operacionais e sem qualquer escrita retroativa.",
     _migrate_to_v9),
]


def _v1_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v1 -- not just one sentinel column
    (correction v1.4 #3). A database only counts as "at least v1" if every
    one of these holds."""
    return (
        _column_exists(conn, "system_state", "state_ambiguous")
        and _column_exists(conn, "orders", "is_close")
        and _column_is_nullable(conn, "orders", "stop_loss")
    )


def _v2_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v2. The unique index check is
    structural (by columns), so an index with a different name providing
    the same guarantee still satisfies it."""
    return (
        _column_exists(conn, "system_state", "clock_out_of_sync")
        and _has_unique_index_on(conn, "candles", {"symbol", "timeframe", "open_time"})
    )


def _v3_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v3 (Fase 2 v1.0)."""
    return (
        _table_exists(conn, "operational_sessions")
        and _table_exists(conn, "order_events")
        and _column_exists(conn, "orders", "filled_qty")
        and _column_exists(conn, "orders", "avg_fill_price")
        and _column_exists(conn, "orders", "fees_total")
        and _column_exists(conn, "orders", "reference_price")
        and _column_exists(conn, "failures_reconciliations", "order_id")
        and _column_exists(conn, "failures_reconciliations", "session_id")
        and _column_exists(conn, "system_state", "reconciliation_diverged")
        and _column_exists(conn, "system_state", "reconciliation_stale")
        and _column_exists(conn, "system_state", "order_state_unknown")
        and _column_exists(conn, "system_state", "initialization_not_reconciled")
        and _column_exists(conn, "system_state", "last_reconciliation_at")
        and _column_exists(conn, "system_state", "operational_state")
        and _column_exists(conn, "system_state", "active_session_id")
    )


def _v4_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v4 -- not just column presence: the
    UNIQUE indexes (not just the columns) are what actually make duplicate
    fill/funding application impossible at the database level."""
    return (
        _column_exists(conn, "executions", "exchange_fill_id")
        and _has_unique_index_on(conn, "executions", {"order_id", "exchange_fill_id"})
        and _column_exists(conn, "failures_reconciliations", "mismatches_json")
        and _column_exists(conn, "operational_sessions", "config_fingerprint")
        and _table_exists(conn, "funding_events")
        and _has_unique_index_on(conn, "funding_events", {"funding_id"})
    )


def _v5_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v5 -- both columns exist, and
    `fills_sync_status` genuinely rejects NULL (its NOT NULL DEFAULT is
    what guarantees every pre-existing row got a safe 'COMPLETE' value
    rather than silently ending up NULL)."""
    return (
        _column_exists(conn, "orders", "pending_exchange_status")
        and _column_is_nullable(conn, "orders", "pending_exchange_status")
        and _column_exists(conn, "orders", "fills_sync_status")
        and not _column_is_nullable(conn, "orders", "fills_sync_status")
    )


def _v6_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v6 -- the table AND the unique index on
    `symbol` (not just the column), since it's the index that actually
    guarantees at most one checkpoint row per symbol at the database
    level."""
    return (
        _table_exists(conn, "funding_collection_checkpoints")
        and _has_unique_index_on(conn, "funding_collection_checkpoints", {"symbol"})
    )


def _v7_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v7 -- `symbols` must exist and remain
    nullable (legacy rows are never backfilled), the CHECK constraint
    against both `symbol`/`symbols` being NULL simultaneously must exist,
    the partial unique index enforcing at most one ACTIVE session per
    portfolio must exist, and the partial unique index on positions must
    exist (not just any unique index on `symbol`, which would wrongly
    forbid multiple historical CLOSED positions for the same symbol)."""
    return (
        _column_exists(conn, "operational_sessions", "symbols")
        and _column_is_nullable(conn, "operational_sessions", "symbols")
        and _column_is_nullable(conn, "operational_sessions", "symbol")
        and _has_check_constraint(conn, "operational_sessions", _SYMBOL_OR_SYMBOLS_CHECK_SQL)
        and _has_unique_partial_index_on(conn, "operational_sessions", {"mode", "symbols"})
        and _has_unique_partial_index_on(conn, "positions", {"symbol"})
    )


def _v8_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v8 -- the column must exist AND remain
    nullable (legacy signals are never backfilled)."""
    return (
        _column_exists(conn, "strategy_signals", "source_candle_open_time")
        and _column_is_nullable(conn, "strategy_signals", "source_candle_open_time")
    )


_VERSION_INVARIANTS: dict[int, Callable[[Connection], bool]] = {
    1: _v1_invariants_satisfied,
    2: _v2_invariants_satisfied,
    3: _v3_invariants_satisfied,
    4: _v4_invariants_satisfied,
    5: _v5_invariants_satisfied,
    6: _v6_invariants_satisfied,
    7: _v7_invariants_satisfied,
    8: _v8_invariants_satisfied,
    9: _v9_invariants_satisfied,
}


def _invariants_satisfied_for_version(conn: Connection, version: int) -> bool:
    check = _VERSION_INVARIANTS.get(version)
    return check(conn) if check is not None else True


def _detect_legacy_version(conn: Connection) -> int:
    """For a database with tables but no schema_migrations history yet,
    validates ALL structural invariants of each version (never a single
    sentinel column), version by version in ascending order, to determine
    which baseline it already matches. Stops at the first version whose
    invariants don't hold -- since each version's own check already implies
    every earlier one held (this loop only ever advances after the
    previous version's check passed), the result is inherently the highest
    version whose CUMULATIVE invariants (1..that version) are satisfied."""
    version = 0
    for candidate in range(1, CURRENT_SCHEMA_VERSION + 1):
        if not _invariants_satisfied_for_version(conn, candidate):
            break
        version = candidate
    return version


def _cumulative_invariants_satisfied(conn: Connection, version: int) -> bool:
    """Correction v1.5 #2: a database only genuinely satisfies version N if
    EVERY invariant from v1 through vN holds -- not merely the invariants
    introduced at N itself. A database stamped only `version=2` with v2's
    own columns/index present but v1's `orders.is_close` missing must NOT
    be treated as valid v2."""
    return all(_invariants_satisfied_for_version(conn, v) for v in range(1, version + 1))


def _validate_recorded_history(conn: Connection, recorded_versions: set[int]) -> None:
    """Correction v1.5 #2: schema_migrations recording versions as applied
    is not, by itself, trusted. Unlike the earlier per-version check this
    replaces, this validates the FULL ancestral chain, not just whichever
    versions happen to have a row:

    - a recorded version newer than CURRENT_SCHEMA_VERSION halts safely
      (never treat an unknown/future version, or an implicit downgrade
      away from it, as valid);
    - the recorded set must be EXACTLY the contiguous range {1..N} for the
      max recorded version N -- a gap (e.g. only v2, or {1, 3}) is rejected
      as an incomplete history, never silently accepted just because the
      highest version has a row;
    - the CUMULATIVE structural invariants of 1..N must hold against the
      real schema, not just N's own.

    Never repairs anything automatically -- same policy as before
    (correction v1.4 #3): refuse and require manual intervention, since
    auto-"fixing" an already-divergent database risks masking real
    corruption."""
    if not recorded_versions:
        return
    max_version = max(recorded_versions)

    if max_version > CURRENT_SCHEMA_VERSION:
        raise SchemaDivergenceError(
            f"schema_migrations registra a versão v{max_version}, superior à versão máxima "
            f"conhecida por esta aplicação (v{CURRENT_SCHEMA_VERSION}). Isso indica que o banco foi "
            f"criado ou migrado por uma versão mais nova do sistema, ou por engano. Por segurança, "
            f"nenhuma alteração automática (incluindo qualquer downgrade implícito) foi feita -- "
            f"intervenção manual é necessária antes de reiniciar a aplicação. Ver docs/MIGRACOES.md, "
            f"seção 'Divergência de esquema'."
        )

    expected = set(range(1, max_version + 1))
    if recorded_versions != expected:
        missing = sorted(expected - recorded_versions)
        raise SchemaDivergenceError(
            f"Histórico de migrações não contíguo: schema_migrations registra as versões "
            f"{sorted(recorded_versions)}, mas a versão máxima registrada (v{max_version}) exigiria "
            f"exatamente {sorted(expected)} (faltando: {missing}). Um histórico incompleto nunca é "
            f"aceito só porque a versão mais alta tem uma linha registrada. Por segurança, nenhuma "
            f"alteração automática foi feita -- intervenção manual é necessária antes de reiniciar a "
            f"aplicação. Ver docs/MIGRACOES.md, seção 'Divergência de esquema'."
        )

    if not _cumulative_invariants_satisfied(conn, max_version):
        raise SchemaDivergenceError(
            f"Inconsistência de esquema detectada: schema_migrations registra até a versão "
            f"v{max_version} como aplicada, mas o esquema real do banco não satisfaz todos os "
            f"invariantes estruturais cumulativos de v1 até v{max_version} (colunas, nulabilidade ou "
            f"índices únicos ausentes/divergentes em alguma versão da cadeia -- não apenas na mais "
            f"recente). Isso pode indicar uma migração anterior malsucedida, uma alteração manual do "
            f"banco, ou corrupção. Por segurança, nenhuma alteração automática foi feita -- "
            f"intervenção manual é necessária antes de reiniciar a aplicação. Ver docs/MIGRACOES.md, "
            f"seção 'Divergência de esquema'."
        )


def current_schema_version(engine: Engine) -> int:
    """Correction v1.5 #2: never reports a recorded version as valid when
    the chain or the schema is actually divergent -- validates the full
    recorded history the same way run_migrations() does, rather than
    trusting `max(applied)` at face value."""
    with engine.connect() as conn:
        if not _table_exists(conn, "schema_migrations"):
            if not _table_exists(conn, "system_state"):
                return 0
            return _detect_legacy_version(conn)
        applied = _applied_versions(conn)
        if not applied:
            return _detect_legacy_version(conn)
        _validate_recorded_history(conn, applied)
        return max(applied)


def run_migrations(engine: Engine) -> MigrationReport:
    """Brings the database at `engine` up to CURRENT_SCHEMA_VERSION. Safe to
    call on every app startup, on any of: a brand-new empty database, a
    Fase 1 baseline database, a v1.1 database, or an already-fully-migrated
    database -- idempotent in every case.
    """
    try:
        with engine.begin() as conn:
            _ensure_schema_migrations_table(conn)
            already_applied = _applied_versions(conn)
            starting_version = max(already_applied) if already_applied else None

            if already_applied:
                _validate_recorded_history(conn, already_applied)

            brand_new = not _table_exists(conn, "system_state")
            if brand_new:
                # Nothing to migrate FROM -- create every table at the
                # current model shape directly, then stamp every migration
                # version as satisfied (their target shape already exists;
                # running the ALTER statements would be redundant, and for
                # the orders-table-rebuild step, actively wrong to repeat).
                Base.metadata.create_all(conn)
                for version, description, _upgrade in MIGRATIONS:
                    if version not in already_applied:
                        _record_migration(conn, version, description)
                return MigrationReport(
                    starting_version=0, ending_version=CURRENT_SCHEMA_VERSION,
                    applied=[], stamped_only=True,
                )

            if starting_version is None:
                starting_version = _detect_legacy_version(conn)
                # Stamp any version the legacy schema already satisfies, so
                # we never try to re-run (e.g.) the orders table rebuild
                # against a database that already has the target shape for
                # an unrelated reason.
                for version, description, _upgrade in MIGRATIONS:
                    if version <= starting_version:
                        _record_migration(conn, version, description)

            applied_now: list[int] = []
            for version, description, upgrade in MIGRATIONS:
                if version <= starting_version or version in _applied_versions(conn):
                    continue
                try:
                    upgrade(conn)
                except Exception as exc:  # noqa: BLE001 - always wrapped and re-raised
                    raise MigrationError(
                        f"Falha ao aplicar a migração v{version} ({description}). "
                        f"Nenhuma alteração foi confirmada; o banco permanece na versão "
                        f"{starting_version}. Causa original: {exc}"
                    ) from exc

                # Correction v1.5 #2: validate this version's CUMULATIVE
                # invariants (1..version) against the real schema BEFORE
                # recording it as applied -- a migration that runs without
                # raising but doesn't actually produce everything it
                # promises must never be stamped as successful.
                if not _cumulative_invariants_satisfied(conn, version):
                    raise MigrationError(
                        f"A migração v{version} ({description}) foi executada sem levantar "
                        f"exceção, mas o esquema resultante não satisfaz todos os invariantes "
                        f"estruturais cumulativos esperados até essa versão. Por segurança, a "
                        f"versão NÃO foi registrada como aplicada e toda a transação será "
                        f"revertida; o banco permanece na versão {starting_version}."
                    )
                _record_migration(conn, version, description)
                applied_now.append(version)

            ending_version = max(starting_version, max(applied_now, default=starting_version))

            # Final full re-validation before declaring success: the
            # schema actually reachable at `ending_version` must satisfy
            # every cumulative invariant from v1 through it.
            if not _cumulative_invariants_satisfied(conn, ending_version):
                raise MigrationError(
                    f"Validação final falhou: o esquema não satisfaz os invariantes estruturais "
                    f"cumulativos esperados até a versão v{ending_version} depois da execução das "
                    f"migrações. Nenhuma alteração foi confirmada."
                )

            return MigrationReport(
                starting_version=starting_version, ending_version=ending_version,
                applied=applied_now, stamped_only=False,
            )
    except MigrationError:
        # engine.begin()'s context manager already rolled back the
        # transaction on the exception above -- re-raise as-is so the
        # caller (app startup) stops safely instead of proceeding against a
        # half-migrated (or entirely unmigrated) schema.
        raise


if __name__ == "__main__":
    # Verification command (documented in docs/MIGRACOES.md):
    #   python -m app.persistence.migrations sqlite:///./agente_trader.db
    import sys

    from app.persistence.db import make_engine

    if len(sys.argv) != 2:
        print("uso: python -m app.persistence.migrations <DATABASE_URL>")
        raise SystemExit(2)

    _engine = make_engine(sys.argv[1])
    print(f"Versão atual do esquema: {current_schema_version(_engine)}")
    _report = run_migrations(_engine)
    print(
        f"Migração concluída: v{_report.starting_version} -> v{_report.ending_version} "
        f"(aplicadas: {_report.applied}, apenas registrada={_report.stamped_only})"
    )
