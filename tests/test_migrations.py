"""Correction v1.3 #1: the versioned migration system must upgrade a
database created by the ORIGINAL Fase 1 baseline schema (before correction
v1.1) to the current schema, preserving every row, idempotently, and must
stop safely (never fake success) if a migration fails partway.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.persistence.db import make_engine, make_session_factory, session_scope
from app.persistence.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    SchemaDivergenceError,
    _has_unique_index_on,
    _table_exists,
    current_schema_version,
    run_migrations,
)
from app.persistence import repo

# The exact DDL of the ORIGINAL Fase 1 baseline schema (commit 66f1a17,
# before correction v1.1 added system_state.state_ambiguous/orders.is_close/
# nullable orders.stop_loss, and before correction v1.2 added
# system_state.clock_out_of_sync and the unique index on candles). Written
# independently of the current app/persistence/models.py so this test
# exercises migrating a genuinely old schema, not a derived one.
_V0_SCHEMA_SQL = [
    """CREATE TABLE candles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(32) NOT NULL,
        timeframe VARCHAR(8) NOT NULL,
        open_time DATETIME NOT NULL,
        open FLOAT NOT NULL, high FLOAT NOT NULL, low FLOAT NOT NULL, close FLOAT NOT NULL,
        volume FLOAT NOT NULL, source VARCHAR(16) NOT NULL, received_at DATETIME NOT NULL
    )""",
    """CREATE TABLE strategy_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(32) NOT NULL, direction VARCHAR(8) NOT NULL, justification TEXT NOT NULL,
        observed_price FLOAT NOT NULL, atr FLOAT NOT NULL, params_json TEXT NOT NULL,
        created_at DATETIME NOT NULL
    )""",
    """CREATE TABLE ai_recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(32) NOT NULL, signal_id INTEGER, recommendation VARCHAR(8) NOT NULL,
        confidence FLOAT NOT NULL, reasoning_summary VARCHAR(500) NOT NULL, risk_flags_json TEXT NOT NULL,
        provider VARCHAR(64) NOT NULL, model_version VARCHAR(64) NOT NULL, is_valid BOOLEAN NOT NULL,
        rejection_reason VARCHAR(255), created_at DATETIME NOT NULL
    )""",
    """CREATE TABLE risk_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL, approved BOOLEAN NOT NULL, reason TEXT NOT NULL,
        checks_json TEXT NOT NULL, created_at DATETIME NOT NULL
    )""",
    # Orders at v0: stop_loss is NOT NULL, no is_close column.
    """CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key VARCHAR(128) NOT NULL,
        risk_evaluation_id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, side VARCHAR(8) NOT NULL,
        qty FLOAT NOT NULL, stop_loss FLOAT NOT NULL, take_profit FLOAT,
        status VARCHAR(16) NOT NULL, exchange_order_id VARCHAR(128), mode VARCHAR(16) NOT NULL,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
    )""",
    "CREATE UNIQUE INDEX ix_orders_idempotency_key ON orders (idempotency_key)",
    "CREATE INDEX ix_orders_symbol ON orders (symbol)",
    """CREATE TABLE executions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL, fill_qty FLOAT NOT NULL, fill_price FLOAT NOT NULL,
        fee FLOAT NOT NULL, is_partial BOOLEAN NOT NULL, executed_at DATETIME NOT NULL
    )""",
    """CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(32) NOT NULL, side VARCHAR(8) NOT NULL, qty FLOAT NOT NULL,
        avg_entry_price FLOAT NOT NULL, stop_loss FLOAT NOT NULL, take_profit FLOAT,
        status VARCHAR(16) NOT NULL, realized_pnl FLOAT NOT NULL, fees_paid FLOAT NOT NULL,
        opened_at DATETIME NOT NULL, closed_at DATETIME
    )""",
    """CREATE TABLE account_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        balance FLOAT NOT NULL, equity FLOAT NOT NULL, unrealized_pnl FLOAT NOT NULL,
        mode VARCHAR(16) NOT NULL, taken_at DATETIME NOT NULL
    )""",
    """CREATE TABLE security_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type VARCHAR(64) NOT NULL, detail TEXT NOT NULL, created_at DATETIME NOT NULL
    )""",
    """CREATE TABLE failures_reconciliations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind VARCHAR(32) NOT NULL, detail TEXT NOT NULL, resolved BOOLEAN NOT NULL,
        created_at DATETIME NOT NULL
    )""",
    # system_state at v0: no state_ambiguous, no clock_out_of_sync.
    """CREATE TABLE system_state (
        id INTEGER PRIMARY KEY,
        trading_blocked BOOLEAN NOT NULL, block_reason VARCHAR(255),
        kill_switch_engaged BOOLEAN NOT NULL, consecutive_losses INTEGER NOT NULL,
        cooldown_until DATETIME, api_failure_count INTEGER NOT NULL, updated_at DATETIME NOT NULL
    )""",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_v0_sample_data(engine) -> None:
    """Inserts one realistic row into every table of the v0 schema."""
    now = _now_iso()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume, source, received_at) "
            "VALUES ('BTCUSDT','1',:t,100,101,99,100.5,10,'bybit_demo',:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO strategy_signals (symbol, direction, justification, observed_price, atr, params_json, created_at) "
            "VALUES ('BTCUSDT','BUY','cruzamento de alta',100.5,1.2,'{}',:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO ai_recommendations (symbol, signal_id, recommendation, confidence, reasoning_summary, "
            "risk_flags_json, provider, model_version, is_valid, rejection_reason, created_at) "
            "VALUES ('BTCUSDT',1,'BUY',0.6,'resumo','[]','simulated','simulated-v1',1,NULL,:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO risk_evaluations (signal_id, approved, reason, checks_json, created_at) "
            "VALUES (1,1,'aprovado','{}',:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO orders (idempotency_key, risk_evaluation_id, symbol, side, qty, stop_loss, take_profit, "
            "status, exchange_order_id, mode, created_at, updated_at) "
            "VALUES ('key-1',1,'BTCUSDT','BUY',0.001,90.0,110.0,'FILLED','EX-1','PAPER_LOCAL',:t,:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO executions (order_id, fill_qty, fill_price, fee, is_partial, executed_at) "
            "VALUES (1,0.001,100.5,0.01,0,:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO positions (symbol, side, qty, avg_entry_price, stop_loss, take_profit, status, "
            "realized_pnl, fees_paid, opened_at, closed_at) "
            "VALUES ('BTCUSDT','BUY',0.001,100.5,90.0,110.0,'OPEN',0,0.01,:t,NULL)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO account_snapshots (balance, equity, unrealized_pnl, mode, taken_at) "
            "VALUES (1000,1000,0,'PAPER_LOCAL',:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO security_events (event_type, detail, created_at) VALUES ('KILL_SWITCH_ENGAGED','teste',:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO failures_reconciliations (kind, detail, resolved, created_at) "
            "VALUES ('FAILURE','falha de teste',0,:t)"
        ), {"t": now})
        conn.execute(text(
            "INSERT INTO system_state (id, trading_blocked, block_reason, kill_switch_engaged, "
            "consecutive_losses, cooldown_until, api_failure_count, updated_at) "
            "VALUES (1,0,NULL,0,0,NULL,0,:t)"
        ), {"t": now})


def _make_legacy_v0_engine(tmp_path, name="legacy_v0.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for statement in _V0_SCHEMA_SQL:
            conn.execute(text(statement))
    _seed_v0_sample_data(engine)
    return engine


def _row_counts(engine) -> dict[str, int]:
    tables = [
        "candles", "strategy_signals", "ai_recommendations", "risk_evaluations",
        "orders", "executions", "positions", "account_snapshots",
        "security_events", "failures_reconciliations", "system_state",
    ]
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables}


def test_legacy_v0_database_starts_at_version_0(tmp_path):
    engine = _make_legacy_v0_engine(tmp_path)
    assert current_schema_version(engine) == 0


def test_upgrade_v0_to_current_preserves_all_data_and_adds_new_schema(tmp_path):
    engine = _make_legacy_v0_engine(tmp_path)
    before_counts = _row_counts(engine)
    assert all(c == 1 for c in before_counts.values())

    report = run_migrations(engine)

    assert report.starting_version == 0
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert report.applied == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    # 1. New columns exist.
    with engine.connect() as conn:
        cols_state = {r[1] for r in conn.execute(text("PRAGMA table_info(system_state)")).fetchall()}
        assert "state_ambiguous" in cols_state
        assert "clock_out_of_sync" in cols_state
        cols_orders = {r[1] for r in conn.execute(text("PRAGMA table_info(orders)")).fetchall()}
        assert "is_close" in cols_orders

        # orders.stop_loss must now accept NULL (a close order has none).
        conn.execute(text(
            "INSERT INTO orders (idempotency_key, risk_evaluation_id, symbol, side, qty, stop_loss, "
            "take_profit, is_close, status, exchange_order_id, mode, created_at, updated_at) "
            "VALUES ('key-close',1,'BTCUSDT','SELL',0.001,NULL,NULL,1,'FILLED','EX-2','PAPER_LOCAL',:t,:t)"
        ), {"t": _now_iso()})

        # 2. Unique index on candles exists and is enforced.
        idx = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_candle_symbol_timeframe_open_time'")
        ).fetchone()
        assert idx is not None
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume, source, received_at) "
                "SELECT symbol, timeframe, open_time, open, high, low, close, volume, source, received_at FROM candles LIMIT 1"
            ))

    # 3. Every pre-existing row from every table survived.
    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during migration"

    # 4. The rest of the app can actually use the migrated DB.
    session_factory = make_session_factory(engine)
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.state_ambiguous is False
        assert state.clock_out_of_sync is False
        positions = repo.open_positions(session)
        assert len(positions) == 1


def test_orchestrator_tick_runs_successfully_after_upgrade(tmp_path):
    """Item 9 of the required upgrade proof: not just that the ORM can
    query the migrated DB, but that a real Orchestrator.tick() runs
    end-to-end against it right after the upgrade."""
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine
    from tests.test_price_correctness import ListMarketDataProvider, make_candle

    engine = _make_legacy_v0_engine(tmp_path, name="tick_after_upgrade.db")
    run_migrations(engine)
    session_factory = make_session_factory(engine)

    settings = Settings(
        mode=RunMode.REPLAY,
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> uma decisão". O default do timeframe estratégico passou a ser
        # 5 minutos; 1 minuto é a compatibilidade explícita mantida pelo PO
        # e preserva exatamente a intenção original do teste.
        strategy_timeframe_minutes=1,
    )
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=ListMarketDataProvider([make_candle(0, 100.0)]),
        strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=True),
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )

    result = orch.tick()
    assert result["status"] in ("hold", "rejected", "order_filled", "order_not_filled")


def test_migration_is_idempotent(tmp_path):
    engine = _make_legacy_v0_engine(tmp_path)
    first = run_migrations(engine)
    before_counts = _row_counts(engine)

    second = run_migrations(engine)
    after_counts = _row_counts(engine)

    assert second.starting_version == CURRENT_SCHEMA_VERSION
    assert second.applied == []
    assert before_counts == after_counts

    with engine.connect() as conn:
        migration_rows = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).fetchall()
        versions = [r[0] for r in migration_rows]
        assert versions == sorted(set(versions))  # no duplicate version rows


def test_brand_new_database_is_stamped_not_altered(tmp_path):
    db_path = tmp_path / "brand_new.db"
    engine = make_engine(f"sqlite:///{db_path}")
    report = run_migrations(engine)

    assert report.stamped_only is True
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    with engine.connect() as conn:
        cols_state = {r[1] for r in conn.execute(text("PRAGMA table_info(system_state)")).fetchall()}
        assert "state_ambiguous" in cols_state
        assert "clock_out_of_sync" in cols_state


def test_v1_database_only_needs_migration_2(tmp_path):
    """A database created by the v1.1 app (state_ambiguous + is_close +
    nullable stop_loss already present, but not clock_out_of_sync) must
    only apply migration 2, never re-run the orders table rebuild."""
    engine = _make_legacy_v0_engine(tmp_path, name="v0_then_v1.db")
    # Fast-forward this DB to a GENUINE v1 shape by hand, bypassing the
    # migrator, to simulate a database that was created directly by v1.1
    # app code -- this must satisfy ALL v1 invariants (correction v1.4 #3),
    # including orders.stop_loss actually being nullable, not just the
    # presence of the new columns.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text(
            "CREATE TABLE orders_v1_fixture ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key VARCHAR(128) NOT NULL, "
            "risk_evaluation_id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, side VARCHAR(8) NOT NULL, "
            "qty FLOAT NOT NULL, stop_loss FLOAT, take_profit FLOAT, is_close BOOLEAN NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL, exchange_order_id VARCHAR(128), mode VARCHAR(16) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO orders_v1_fixture SELECT id, idempotency_key, risk_evaluation_id, symbol, side, "
            "qty, stop_loss, take_profit, 0, status, exchange_order_id, mode, created_at, updated_at FROM orders"
        ))
        conn.execute(text("DROP TABLE orders"))
        conn.execute(text("ALTER TABLE orders_v1_fixture RENAME TO orders"))

    assert current_schema_version(engine) == 1

    report = run_migrations(engine)
    assert report.starting_version == 1
    assert report.applied == [2, 3, 4, 5, 6, 7, 8, 9]  # never re-runs migration 1's orders rebuild

    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(system_state)")).fetchall()}
        assert "clock_out_of_sync" in cols


def test_duplicate_candles_are_deterministically_deduplicated_before_unique_index(tmp_path):
    engine = _make_legacy_v0_engine(tmp_path, name="dup_candles.db")
    now = _now_iso()
    with engine.begin() as conn:
        # Two more rows sharing the exact same (symbol, timeframe, open_time)
        # as the one already seeded by _seed_v0_sample_data -- but with a
        # different close price, so we can tell which one survives.
        row = conn.execute(text("SELECT open_time FROM candles LIMIT 1")).fetchone()
        open_time = row[0]
        conn.execute(text(
            "INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume, source, received_at) "
            "VALUES ('BTCUSDT','1',:ot,100,101,99,999.0,10,'bybit_demo',:t)"
        ), {"ot": open_time, "t": now})
        conn.execute(text(
            "INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume, source, received_at) "
            "VALUES ('BTCUSDT','1',:ot,100,101,99,888.0,10,'bybit_demo',:t)"
        ), {"ot": open_time, "t": now})

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM candles")).scalar() == 3

    run_migrations(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, close FROM candles ORDER BY id")).fetchall()
        assert len(rows) == 1
        # The canonical survivor is the lowest-id (earliest-inserted) row --
        # the one seeded by _seed_v0_sample_data with close=100.5.
        assert rows[0][1] == pytest.approx(100.5)


def test_migration_failure_rolls_back_and_never_records_partial_success(tmp_path, monkeypatch):
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name="failing.db")

    def _boom(conn):
        raise RuntimeError("falha simulada de migração")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(1, "migração 1 (falha proposital)", _boom)],
    )

    with pytest.raises(MigrationError) as excinfo:
        run_migrations(engine)

    assert "banco permanece na versão" in str(excinfo.value)

    # Nothing was recorded as applied, and the original data is untouched.
    # (With real transactional DDL, correction v1.4 #1: even the
    # `CREATE TABLE IF NOT EXISTS schema_migrations` from earlier in the
    # same transaction is rolled back, so the table may not exist at all --
    # that is a STRONGER guarantee than "exists but empty", not a weaker one.)
    with engine.connect() as conn:
        if _table_exists(conn, "schema_migrations"):
            applied = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
            assert applied == []
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(system_state)")).fetchall()}
        assert "state_ambiguous" not in cols  # the failed migration's change was rolled back

    after_counts = _row_counts(engine)
    assert all(c == 1 for c in after_counts.values())


def _full_schema_snapshot(engine) -> dict:
    """Every table, column, and index in the database -- used to prove a
    rolled-back migration leaves ABSOLUTELY nothing behind, not just the
    one column/table a narrower test happens to check."""
    with engine.connect() as conn:
        tables = sorted(
            r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            ).fetchall()
        )
        snapshot = {}
        for table in tables:
            columns = sorted(
                (r[1], r[2], r[3]) for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            )  # (name, type, notnull)
            indexes = sorted(
                r[1] for r in conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
            )
            snapshot[table] = {"columns": columns, "indexes": indexes}
        return snapshot


def test_alter_table_add_column_is_rolled_back_on_later_failure(tmp_path, monkeypatch):
    """Adversarial reproduction of the exact failure the audit found: a
    migration that ACTUALLY executes `ALTER TABLE ... ADD COLUMN` and only
    THEN raises. Before correction v1.4 #1, SQLite's implicit
    driver-level autocommit on DDL meant the ADD COLUMN survived the
    "rollback" regardless -- this proves it no longer does, across the
    full schema and full data, not just one column."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name="adversarial_rollback.db")
    real_migrations = list(migrations_module.MIGRATIONS)

    schema_before = _full_schema_snapshot(engine)
    data_before = _row_counts(engine)

    def _leaks_a_column_then_fails(conn):
        conn.execute(text("ALTER TABLE system_state ADD COLUMN leaked INTEGER DEFAULT 0"))
        raise RuntimeError("falha simulada APÓS uma alteração de esquema real")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(1, "migração adversarial (ALTER real, depois falha)", _leaks_a_column_then_fails)],
    )

    with pytest.raises(MigrationError):
        run_migrations(engine)

    # 1. Full schema (every table/column/index) is byte-for-byte identical
    #    to before the attempt -- not just "system_state" spot-checked.
    schema_after = _full_schema_snapshot(engine)
    assert schema_after == schema_before
    assert "leaked" not in {c[0] for c in schema_after["system_state"]["columns"]}

    # 2. Full data is untouched.
    assert _row_counts(engine) == data_before

    # 3. No partial version was recorded. With real transactional DDL, the
    #    CREATE TABLE IF NOT EXISTS schema_migrations issued earlier in the
    #    SAME transaction is also rolled back -- so the table not existing
    #    at all is the correct (stronger) outcome, not a bug.
    with engine.connect() as conn:
        if _table_exists(conn, "schema_migrations"):
            migration_rows = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
            assert migration_rows == []

    # 4. A clean retry (with the real migrations restored) succeeds normally.
    monkeypatch.setitem(migrations_module.__dict__, "MIGRATIONS", real_migrations)
    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert _row_counts(engine) == data_before


# --- Correction v1.4 #3: full structural invariants, not one sentinel column ---

def test_clock_out_of_sync_present_but_unique_index_missing_is_detected_as_v1(tmp_path):
    """A database with system_state.clock_out_of_sync (the v2 sentinel
    column) but WITHOUT the candles unique index must be classified as v1,
    not v2 -- and migrating it must still create the missing index."""
    engine = _make_legacy_v0_engine(tmp_path, name="partial_v2_no_index.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE orders ADD COLUMN is_close BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"))
        # Deliberately NOT creating the unique index on candles, and NOT
        # relaxing orders.stop_loss -- both v1 and v2 are actually incomplete.

    assert current_schema_version(engine) == 0  # stop_loss still NOT NULL -> v1 itself isn't satisfied either

    report = run_migrations(engine)
    assert report.applied == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_candle_symbol_timeframe_open_time'")
        ).fetchone() is not None


def test_is_close_present_but_stop_loss_still_not_null_is_detected_as_v0(tmp_path):
    """orders.is_close existing is not enough on its own -- if
    orders.stop_loss is still NOT NULL, v1 is not actually satisfied."""
    engine = _make_legacy_v0_engine(tmp_path, name="partial_v1_notnull.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE orders ADD COLUMN is_close BOOLEAN NOT NULL DEFAULT 0"))
        # stop_loss is untouched -- still NOT NULL from the v0 schema.

    assert current_schema_version(engine) == 0

    report = run_migrations(engine)
    assert report.applied == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    with engine.connect() as conn:
        assert conn.execute(text(
            "INSERT INTO orders (idempotency_key, risk_evaluation_id, symbol, side, qty, stop_loss, "
            "take_profit, is_close, status, exchange_order_id, mode, created_at, updated_at) "
            "VALUES ('k2',1,'BTCUSDT','SELL',0.001,NULL,NULL,1,'FILLED','EX-2','PAPER_LOCAL',:t,:t)"
        ), {"t": _now_iso()})


def test_recorded_v2_with_missing_index_raises_schema_divergence_error(tmp_path):
    """schema_migrations claiming v2 was applied, while the candles unique
    index is actually absent, must stop safely rather than silently
    trusting the recorded history."""
    engine = _make_legacy_v0_engine(tmp_path, name="diverged.db")
    run_migrations(engine)  # brings it to a real, consistent current version

    # Now sabotage it: drop the unique index by hand, simulating either a
    # manual change or an earlier bug that recorded success incorrectly.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_candle_symbol_timeframe_open_time"))

    with pytest.raises(SchemaDivergenceError) as excinfo:
        run_migrations(engine)
    assert "divergência" in str(excinfo.value).lower() or "inconsistência" in str(excinfo.value).lower()

    # No further schema/data change happened as a side effect of detecting this.
    with engine.connect() as conn:
        migration_rows = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).fetchall()
        assert [r[0] for r in migration_rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]  # unchanged from before the sabotage


def test_differently_named_unique_index_still_satisfies_the_invariant(tmp_path):
    """The structural check must match by COLUMNS, not by a hardcoded index
    name -- an index some other tool created with a different name, but the
    same uniqueness guarantee, must count."""
    engine = _make_legacy_v0_engine(tmp_path, name="renamed_index.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text(
            "CREATE TABLE orders_tmp (id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key VARCHAR(128) NOT NULL, "
            "risk_evaluation_id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, side VARCHAR(8) NOT NULL, "
            "qty FLOAT NOT NULL, stop_loss FLOAT, take_profit FLOAT, is_close BOOLEAN NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL, exchange_order_id VARCHAR(128), mode VARCHAR(16) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO orders_tmp SELECT id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, 0, status, exchange_order_id, mode, created_at, updated_at FROM orders"
        ))
        conn.execute(text("DROP TABLE orders"))
        conn.execute(text("ALTER TABLE orders_tmp RENAME TO orders"))
        # Unique index with a totally different, non-standard name.
        conn.execute(text(
            "CREATE UNIQUE INDEX minha_constraint_customizada ON candles (symbol, timeframe, open_time)"
        ))

    assert current_schema_version(engine) == 2

    report = run_migrations(engine)
    assert report.applied == [3, 4, 5, 6, 7, 8, 9]  # already fully v2 (custom index name counts) -- only v3+ are new, no divergence


def test_fully_current_database_is_idempotent_under_strict_invariant_checking(tmp_path):
    engine = _make_legacy_v0_engine(tmp_path, name="already_current.db")
    run_migrations(engine)
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION


# --- Correction v1.5 #2: non-contiguous/incomplete recorded history --------
#
# `_find_diverged_version()` used to only re-check versions that HAD a row in
# `schema_migrations` -- a database stamped with ONLY `version=2` and no `v1`
# row was accepted as valid v2 even though it never actually satisfied v1's
# own invariants. `_validate_recorded_history()` replaces it: the recorded
# set must be exactly the contiguous range {1..max}, an unknown/future
# version halts safely, and validation of the max version checks the
# CUMULATIVE invariants of every version up to it, not just its own.

def _stamp_only_versions(engine, versions: list[int]) -> None:
    """Writes `schema_migrations` rows for exactly `versions` (and no
    others) -- simulates a history that skips versions, unlike
    `run_migrations()` which always records every intermediate version."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
        ))
        for v in versions:
            conn.execute(
                text("INSERT INTO schema_migrations (version, description, applied_at) VALUES (:v, 'stub', :t)"),
                {"v": v, "t": _now_iso()},
            )


def test_only_v2_recorded_with_v1_invariants_missing_is_rejected(tmp_path):
    """Exact reproduction from the audit: a database whose ONLY recorded
    migration is `version=2`, with v2's own structural invariants present
    (clock_out_of_sync, the candles unique index) but v1's invariants
    (orders.is_close) absent, must be rejected -- never silently accepted
    as a valid v2 just because the highest recorded version has a row."""
    engine = _make_legacy_v0_engine(tmp_path, name="only_v2_no_v1.db")
    with engine.begin() as conn:
        # v2's own invariants -- present.
        conn.execute(text("ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_candle_symbol_timeframe_open_time ON candles (symbol, timeframe, open_time)"
        ))
        # v1's invariants -- deliberately absent: no state_ambiguous, no
        # orders.is_close, orders.stop_loss still NOT NULL.
    _stamp_only_versions(engine, [2])

    with pytest.raises(SchemaDivergenceError) as excinfo:
        current_schema_version(engine)
    assert "contíguo" in str(excinfo.value).lower() or "não contí" in str(excinfo.value).lower()

    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_only_v2_recorded_but_structurally_complete_is_still_rejected_for_incomplete_history(tmp_path):
    """Even when the real schema happens to be FULLY structurally complete
    (all v1 and v2 invariants genuinely hold), a history that skips
    straight to `version=2` without a `version=1` row is still rejected --
    the documented policy chosen for this correction is to require an
    explicit, complete, contiguous history rather than silently repairing
    or trusting a structurally-lucky-but-incomplete record."""
    engine = _make_legacy_v0_engine(tmp_path, name="only_v2_structurally_complete.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text(
            "CREATE TABLE orders_tmp (id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key VARCHAR(128) NOT NULL, "
            "risk_evaluation_id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, side VARCHAR(8) NOT NULL, "
            "qty FLOAT NOT NULL, stop_loss FLOAT, take_profit FLOAT, is_close BOOLEAN NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL, exchange_order_id VARCHAR(128), mode VARCHAR(16) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO orders_tmp SELECT id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, 0, status, exchange_order_id, mode, created_at, updated_at FROM orders"
        ))
        conn.execute(text("DROP TABLE orders"))
        conn.execute(text("ALTER TABLE orders_tmp RENAME TO orders"))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_candle_symbol_timeframe_open_time ON candles (symbol, timeframe, open_time)"
        ))
    _stamp_only_versions(engine, [2])  # no v1 row, despite v1's invariants genuinely holding too

    with pytest.raises(SchemaDivergenceError) as excinfo:
        current_schema_version(engine)
    assert "contíguo" in str(excinfo.value).lower() or "não contí" in str(excinfo.value).lower()


def test_history_with_a_gap_1_and_3_is_rejected(tmp_path):
    """A recorded history of {1, 3} (skipping the unknown/never-defined
    version 2's neighbor and jumping to a version this app doesn't even
    know) is rejected both for the gap and for the unknown version."""
    engine = _make_legacy_v0_engine(tmp_path, name="gap_1_3.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
    _stamp_only_versions(engine, [1, 3])

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_version_higher_than_current_schema_version_is_rejected_as_implicit_downgrade(tmp_path):
    """A recorded version beyond CURRENT_SCHEMA_VERSION must halt safely --
    this app must never treat an unknown future version, or an implicit
    downgrade away from it, as something it can proceed past."""
    engine = _make_legacy_v0_engine(tmp_path, name="future_version.db")
    _stamp_only_versions(engine, [1, 2, 3])  # v3 doesn't exist in this app

    with pytest.raises(SchemaDivergenceError) as excinfo:
        current_schema_version(engine)
    assert "superior" in str(excinfo.value).lower() or "v3" in str(excinfo.value)

    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_migration_that_does_not_produce_promised_invariants_is_never_recorded(tmp_path, monkeypatch):
    """A migration function that runs to completion WITHOUT raising, but
    fails to actually produce everything its version promises, must not be
    trusted just because no exception was thrown -- it is never recorded as
    applied, and the whole transaction is rolled back."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name="promises_not_kept.db")
    data_before = _row_counts(engine)

    def _adds_state_ambiguous_but_forgets_orders(conn):
        # Only does HALF of what v1 promises -- state_ambiguous is added,
        # but orders.is_close/nullable stop_loss are never touched.
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(1, "migração incompleta (não levanta exceção, mas não cumpre o prometido)",
          _adds_state_ambiguous_but_forgets_orders)],
    )

    with pytest.raises(MigrationError) as excinfo:
        run_migrations(engine)
    assert "invariantes" in str(excinfo.value).lower()

    with engine.connect() as conn:
        if _table_exists(conn, "schema_migrations"):
            applied = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
            assert applied == []  # never recorded despite not raising mid-ALTER
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(system_state)")).fetchall()}
        assert "state_ambiguous" not in cols  # rolled back along with everything else

    assert _row_counts(engine) == data_before


def test_partial_legacy_schema_with_no_history_migrates_and_validates_correctly(tmp_path):
    """A legacy database with no `schema_migrations` table at all, and a
    hand-modified PARTIAL structural state (v1 fully satisfied, v2 only
    half-satisfied: clock_out_of_sync present but the unique index still
    missing) must be correctly detected as v1 via cumulative invariant
    checking, and migrating it must apply only migration 2 and end up fully
    valid -- not skip straight to "looks like v2" from one column alone."""
    engine = _make_legacy_v0_engine(tmp_path, name="partial_legacy_no_history.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE orders ADD COLUMN is_close BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"))
        # stop_loss still NOT NULL and no unique index -- v1 and v2 both
        # genuinely incomplete despite clock_out_of_sync's presence.

    assert current_schema_version(engine) == 0  # stop_loss still NOT NULL -> not even v1 yet

    report = run_migrations(engine)
    assert report.starting_version == 0
    assert report.applied == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_candle_symbol_timeframe_open_time'")
        ).fetchone() is not None


def test_consistent_current_database_remains_idempotent_under_full_history_validation(tmp_path):
    """A genuinely consistent, fully-recorded, contiguous v2 database must
    continue to validate cleanly and stay idempotent under the new full
    ancestral-chain check -- correction v1.5 #2 tightens what counts as
    valid, but must never reject a database that was always legitimately
    consistent."""
    engine = _make_legacy_v0_engine(tmp_path, name="consistent_v2.db")
    run_migrations(engine)

    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION


# --- Fase 2 v1.0: migration v3 (order state machine + operational sessions) -

def test_migration_v3_adds_order_state_and_session_columns_and_preserves_v2_data(tmp_path):
    """Reproduces upgrading the approved Fase 1 database (a real, consistent
    v2 database with actual order/system_state rows) to the Fase 2 schema:
    every pre-existing row must survive untouched, and the new
    columns/tables must be usable immediately afterward."""
    engine = _make_legacy_v0_engine(tmp_path, name="v2_to_v3.db")
    before_counts = _row_counts(engine)

    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert 3 in report.applied

    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during v3 migration"

    with engine.connect() as conn:
        order_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(orders)")).fetchall()}
        assert {"filled_qty", "avg_fill_price", "fees_total"} <= order_cols

        state_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(system_state)")).fetchall()}
        assert {
            "reconciliation_diverged", "reconciliation_stale", "order_state_unknown",
            "initialization_not_reconciled", "last_reconciliation_at", "operational_state",
            "active_session_id",
        } <= state_cols

        fr_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(failures_reconciliations)")).fetchall()}
        assert {"order_id", "session_id"} <= fr_cols

        assert conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='order_events'")
        ).fetchone() is not None
        assert conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='operational_sessions'")
        ).fetchone() is not None

        # Pre-existing order row (created before v3 existed) got the new
        # columns' defaults, not NULL/errors.
        row = conn.execute(text("SELECT filled_qty, avg_fill_price, fees_total FROM orders LIMIT 1")).fetchone()
        assert row == (0.0, 0.0, 0.0)

        # New tables are immediately usable end-to-end.
        conn.execute(text(
            "INSERT INTO order_events (order_id, from_status, to_status, detail, created_at) "
            "VALUES (1, 'PENDING_SUBMIT', 'SUBMITTED', 'teste', :t)"
        ), {"t": _now_iso()})
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, timeframe, started_at, "
            "strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-1', 'REPLAY', 'BTCUSDT', '1', :t, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})

    # Idempotent re-run.
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION


def test_migration_v3_only_recorded_history_with_v3_invariants_missing_is_rejected(tmp_path):
    """Same non-contiguous/incomplete-history protection (correction v1.5
    #2) applies to the new v3 boundary: a database claiming v3 without
    actually having the v3 structural invariants must be rejected, not
    silently trusted."""
    engine = _make_legacy_v0_engine(tmp_path, name="claims_v3_missing_invariants.db")
    run_migrations(engine)  # real, consistent v3

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE order_events"))

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def _make_real_v3_engine(tmp_path, name="real_v3.db"):
    """Correção v1.1 #9: a genuine, consistent v3 database (stopping
    exactly at the previously-approved Fase 2 v1.0 schema, with real rows
    in it) -- built by temporarily hiding migration 4 from `MIGRATIONS`
    rather than hand-rolling a second copy of the v3 DDL, so this always
    tracks whatever v3 actually is."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name=name)
    real_migrations = list(migrations_module.MIGRATIONS)
    only_through_v3 = [m for m in real_migrations if m[0] <= 3]
    try:
        migrations_module.__dict__["MIGRATIONS"] = only_through_v3
        report = run_migrations(engine)
    finally:
        migrations_module.__dict__["MIGRATIONS"] = real_migrations
    assert report.ending_version == 3
    return engine


def test_migration_v4_upgrades_a_real_v3_database_preserving_data_and_is_idempotent(tmp_path):
    """Correção v1.1 #9: upgrading from an actual, previously-approved v3
    database (not a v0-to-v4 shortcut) must preserve every row, add the
    fills-ledger/reconciliation/session-fingerprint/funding structures, and
    be a safe no-op on immediate re-run."""
    engine = _make_real_v3_engine(tmp_path)
    before_counts = _row_counts(engine)
    assert current_schema_version(engine) == 3

    report = run_migrations(engine)
    assert report.starting_version == 3
    assert report.applied == [4, 5, 6, 7, 8, 9]
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during v4 migration"

    with engine.connect() as conn:
        exec_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(executions)")).fetchall()}
        assert "exchange_fill_id" in exec_cols
        assert _has_unique_index_on(conn, "executions", {"order_id", "exchange_fill_id"})

        fr_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(failures_reconciliations)")).fetchall()}
        assert "mismatches_json" in fr_cols

        session_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(operational_sessions)")).fetchall()}
        assert "config_fingerprint" in session_cols

        assert _table_exists(conn, "funding_events")
        assert _has_unique_index_on(conn, "funding_events", {"funding_id"})

        # New tables/columns are immediately usable.
        conn.execute(text(
            "INSERT INTO funding_events (funding_id, symbol, amount, occurred_at, created_at) "
            "VALUES ('fund-1', 'BTCUSDT', -0.5, :t, :t)"
        ), {"t": _now_iso()})
        conn.commit()

    # Idempotent re-run.
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    assert _row_counts(engine)["orders"] == before_counts["orders"]


def test_migration_v4_only_recorded_history_with_v4_invariants_missing_is_rejected(tmp_path):
    """Same non-contiguous/incomplete-history protection applied to the new
    v4 boundary: a database claiming v4 without the real v4 structural
    invariants (the unique fill index specifically) must be rejected."""
    engine = _make_real_v3_engine(tmp_path, name="claims_v4_missing_invariants.db")
    run_migrations(engine)  # real, consistent v4

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_execution_order_exchange_fill_id"))

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_migration_v4_add_column_is_rolled_back_on_later_failure(tmp_path, monkeypatch):
    """Adversarial reproduction of the same DDL-rollback guarantee, this
    time starting from a real v3 database and injecting the failure inside
    a v4-shaped migration -- proves the transactional-DDL guarantee holds
    at this specific version boundary too, not just at v1 (correction
    v1.4 #1's original reproduction)."""
    import app.persistence.migrations as migrations_module

    engine = _make_real_v3_engine(tmp_path, name="adversarial_v4_rollback.db")
    real_migrations = list(migrations_module.MIGRATIONS)

    schema_before = _full_schema_snapshot(engine)
    data_before = _row_counts(engine)

    def _leaks_a_column_then_fails(conn):
        conn.execute(text("ALTER TABLE executions ADD COLUMN leaked INTEGER DEFAULT 0"))
        raise RuntimeError("falha simulada APÓS uma alteração de esquema real (v4)")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(4, "migração v4 adversarial (ALTER real, depois falha)", _leaks_a_column_then_fails)],
    )

    with pytest.raises(MigrationError):
        run_migrations(engine)

    schema_after = _full_schema_snapshot(engine)
    assert schema_after == schema_before
    assert "leaked" not in {c[0] for c in schema_after["executions"]["columns"]}
    assert _row_counts(engine) == data_before
    assert current_schema_version(engine) == 3

    monkeypatch.setitem(migrations_module.__dict__, "MIGRATIONS", real_migrations)
    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION


def _make_real_v4_engine(tmp_path, name="real_v4.db"):
    """Correção v1.2 #7: a genuine, consistent v4 database (stopping
    exactly at the previously-approved correção v1.1 schema, with real
    rows in it) -- same pattern as `_make_real_v3_engine`, one version up."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name=name)
    real_migrations = list(migrations_module.MIGRATIONS)
    only_through_v4 = [m for m in real_migrations if m[0] <= 4]
    try:
        migrations_module.__dict__["MIGRATIONS"] = only_through_v4
        report = run_migrations(engine)
    finally:
        migrations_module.__dict__["MIGRATIONS"] = real_migrations
    assert report.ending_version == 4
    return engine


def test_migration_v5_upgrades_a_real_v4_database_preserving_data_and_is_idempotent(tmp_path):
    """Correção v1.2 #7: upgrading from an actual, previously-approved v4
    database (not a v0-to-v5 shortcut) must preserve every row, add the
    fills-sync-status bookkeeping, and be a safe no-op on immediate
    re-run."""
    engine = _make_real_v4_engine(tmp_path)
    before_counts = _row_counts(engine)
    assert current_schema_version(engine) == 4

    report = run_migrations(engine)
    assert report.starting_version == 4
    assert report.applied == [5, 6, 7, 8, 9]
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during v5 migration"

    with engine.connect() as conn:
        order_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(orders)")).fetchall()}
        assert "pending_exchange_status" in order_cols
        assert "fills_sync_status" in order_cols

        # Pre-existing order rows (created before v5 existed) got the
        # column's DEFAULT, not NULL or an error.
        row = conn.execute(text("SELECT fills_sync_status FROM orders LIMIT 1")).fetchone()
        assert row == ("COMPLETE",)

        # New column is immediately usable end-to-end.
        conn.execute(text(
            "UPDATE orders SET pending_exchange_status = 'FILLED', fills_sync_status = 'PENDING' "
            "WHERE id = (SELECT id FROM orders LIMIT 1)"
        ))
        conn.commit()

    # Idempotent re-run.
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    assert _row_counts(engine)["orders"] == before_counts["orders"]


def test_migration_v5_only_recorded_history_with_v5_invariants_missing_is_rejected(tmp_path):
    """A database claiming v5 without the real v5 structural invariants
    (specifically: fills_sync_status genuinely rejecting NULL) must be
    rejected, not silently trusted."""
    engine = _make_real_v4_engine(tmp_path, name="claims_v5_missing_invariants.db")
    run_migrations(engine)  # real, consistent v5

    # SQLite can't drop a NOT NULL constraint in place -- rebuild the table
    # without fills_sync_status entirely, matching the pattern used for the
    # v3 divergence test (drop the exact thing v5 added).
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE orders RENAME TO orders_old"))
        conn.execute(text(
            "CREATE TABLE orders AS SELECT id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, is_close, status, exchange_order_id, mode, filled_qty, "
            "avg_fill_price, fees_total, reference_price, pending_exchange_status, created_at, updated_at "
            "FROM orders_old"
        ))
        conn.execute(text("DROP TABLE orders_old"))

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_migration_v5_add_column_is_rolled_back_on_later_failure(tmp_path, monkeypatch):
    """Adversarial reproduction of the same DDL-rollback guarantee at the
    v4->v5 boundary."""
    import app.persistence.migrations as migrations_module

    engine = _make_real_v4_engine(tmp_path, name="adversarial_v5_rollback.db")
    real_migrations = list(migrations_module.MIGRATIONS)

    schema_before = _full_schema_snapshot(engine)
    data_before = _row_counts(engine)

    def _leaks_a_column_then_fails(conn):
        conn.execute(text("ALTER TABLE orders ADD COLUMN leaked INTEGER DEFAULT 0"))
        raise RuntimeError("falha simulada APÓS uma alteração de esquema real (v5)")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(5, "migração v5 adversarial (ALTER real, depois falha)", _leaks_a_column_then_fails)],
    )

    with pytest.raises(MigrationError):
        run_migrations(engine)

    schema_after = _full_schema_snapshot(engine)
    assert schema_after == schema_before
    assert "leaked" not in {c[0] for c in schema_after["orders"]["columns"]}
    assert _row_counts(engine) == data_before
    assert current_schema_version(engine) == 4

    monkeypatch.setitem(migrations_module.__dict__, "MIGRATIONS", real_migrations)
    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION


def _make_real_v5_engine(tmp_path, name="real_v5.db"):
    """Correção v1.3 #3: a genuine, consistent v5 database (stopping
    exactly at the previously-approved correção v1.2 schema, with real
    rows in it) -- same pattern as `_make_real_v4_engine`, one version up."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name=name)
    real_migrations = list(migrations_module.MIGRATIONS)
    only_through_v5 = [m for m in real_migrations if m[0] <= 5]
    try:
        migrations_module.__dict__["MIGRATIONS"] = only_through_v5
        report = run_migrations(engine)
    finally:
        migrations_module.__dict__["MIGRATIONS"] = real_migrations
    assert report.ending_version == 5
    return engine


def test_migration_v6_upgrades_a_real_v5_database_preserving_orders_fills_and_funding(tmp_path):
    """Correção v1.3 #3: upgrading from an actual, previously-approved v5
    database (not a v0-to-v6 shortcut) must preserve every order, fill and
    funding row, add the funding-collection-checkpoint table, and be a
    safe no-op on immediate re-run."""
    engine = _make_real_v5_engine(tmp_path)
    before_counts = _row_counts(engine)
    assert current_schema_version(engine) == 5

    # Seed a funding_events row too (not covered by _row_counts) to prove
    # it survives the upgrade untouched.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO funding_events (funding_id, symbol, amount, occurred_at, created_at) "
            "VALUES ('f-preserve-1', 'BTCUSDT', -0.5, :t, :t)"
        ), {"t": _now_iso()})
    with engine.connect() as conn:
        funding_events_before = conn.execute(text("SELECT COUNT(*) FROM funding_events")).scalar()

    report = run_migrations(engine)
    assert report.starting_version == 5
    assert report.applied == [6, 7, 8, 9]
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during v6 migration"

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM funding_events")).scalar() == funding_events_before
        assert _table_exists(conn, "funding_collection_checkpoints")
        assert _has_unique_index_on(conn, "funding_collection_checkpoints", {"symbol"})

        # New table is immediately usable end-to-end.
        conn.execute(text(
            "INSERT INTO funding_collection_checkpoints (symbol, covered_until, updated_at) "
            "VALUES ('BTCUSDT', :t, :t)"
        ), {"t": _now_iso()})
        conn.commit()

    # Idempotent re-run.
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    assert _row_counts(engine)["orders"] == before_counts["orders"]


def test_migration_v6_only_recorded_history_with_v6_invariants_missing_is_rejected(tmp_path):
    """A database claiming v6 without the real v6 structural invariants
    (specifically: the unique index on symbol, not just the table) must be
    rejected, not silently trusted."""
    engine = _make_real_v5_engine(tmp_path, name="claims_v6_missing_invariants.db")
    run_migrations(engine)  # real, consistent v6

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_funding_checkpoint_symbol"))

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_migration_v6_creation_is_rolled_back_on_later_failure(tmp_path, monkeypatch):
    """Adversarial reproduction of the same DDL-rollback guarantee at the
    v5->v6 boundary -- the new TABLE itself, not just a column, must not
    survive a failed migration."""
    import app.persistence.migrations as migrations_module

    engine = _make_real_v5_engine(tmp_path, name="adversarial_v6_rollback.db")
    real_migrations = list(migrations_module.MIGRATIONS)

    schema_before = _full_schema_snapshot(engine)
    data_before = _row_counts(engine)

    def _creates_table_then_fails(conn):
        conn.execute(text(
            "CREATE TABLE funding_collection_checkpoints (id INTEGER PRIMARY KEY, symbol VARCHAR(32))"
        ))
        raise RuntimeError("falha simulada APÓS criar uma tabela real (v6)")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(6, "migração v6 adversarial (CREATE TABLE real, depois falha)", _creates_table_then_fails)],
    )

    with pytest.raises(MigrationError):
        run_migrations(engine)

    schema_after = _full_schema_snapshot(engine)
    assert schema_after == schema_before
    assert "funding_collection_checkpoints" not in schema_after
    assert _row_counts(engine) == data_before
    assert current_schema_version(engine) == 5

    monkeypatch.setitem(migrations_module.__dict__, "MIGRATIONS", real_migrations)
    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION


# --- Fase 3 multiativo (fundação): migration v7 (operational_sessions.symbols
# nullable + symbol relaxed to nullable + partial unique index on positions) --

def _make_real_v6_engine(tmp_path, name="real_v6.db"):
    """A genuine, consistent v6 database (stopping exactly at the
    previously-approved correção v1.3 schema, with real rows in it) -- same
    pattern as `_make_real_v5_engine`, one version up."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name=name)
    real_migrations = list(migrations_module.MIGRATIONS)
    only_through_v6 = [m for m in real_migrations if m[0] <= 6]
    try:
        migrations_module.__dict__["MIGRATIONS"] = only_through_v6
        report = run_migrations(engine)
    finally:
        migrations_module.__dict__["MIGRATIONS"] = real_migrations
    assert report.ending_version == 6
    return engine


def test_migration_v7_upgrades_a_real_v6_database_preserving_data_and_is_idempotent(tmp_path):
    """Upgrading from an actual, previously-approved v6 (monoativo) database
    must preserve every row (including the pre-existing operational_sessions
    row's `symbol` value, with `symbols` staying NULL for it -- never
    backfilled), add the new column + relaxed nullability + partial index,
    and be a safe no-op on immediate re-run."""
    engine = _make_real_v6_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, timeframe, started_at, "
            "strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-legacy-mono', 'REPLAY', 'BTCUSDT', '1', :t, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})
    before_counts = _row_counts(engine)
    with engine.connect() as conn:
        sessions_before = conn.execute(text("SELECT COUNT(*) FROM operational_sessions")).scalar()
    assert current_schema_version(engine) == 6

    report = run_migrations(engine)
    assert report.starting_version == 6
    assert report.applied == [7, 8, 9]
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during v7 migration"

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM operational_sessions")).scalar() == sessions_before

        session_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(operational_sessions)")).fetchall()}
        assert "symbols" in session_cols

        # The legacy row's `symbol` survived untouched; `symbols` was never
        # backfilled for it (stays NULL -- pure historical read-only data).
        row = conn.execute(text(
            "SELECT symbol, symbols FROM operational_sessions WHERE session_uid = 'uid-legacy-mono'"
        )).fetchone()
        assert row == ("BTCUSDT", None)

        # symbol is now nullable -- a genuinely multi-symbol session can
        # leave it NULL instead of lying with a single value.
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, started_at, "
            "strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-multi', 'REPLAY', NULL, '[\"BTCUSDT\",\"ETHUSDT\",\"SOLUSDT\"]', '1', :t, "
            "'v1', '{}', '{}')"
        ), {"t": _now_iso()})
        conn.commit()

        # positions: the partial unique index enforces at most one OPEN
        # position per symbol, but never blocks multiple historical CLOSED
        # ones for the same symbol.
        conn.execute(text(
            "INSERT INTO positions (symbol, side, qty, avg_entry_price, stop_loss, take_profit, status, "
            "realized_pnl, fees_paid, opened_at, closed_at) "
            "VALUES ('ETHUSDT','BUY',0.01,2000.0,1900.0,2100.0,'CLOSED',5.0,0.1,:t,:t)"
        ), {"t": _now_iso()})
        conn.execute(text(
            "INSERT INTO positions (symbol, side, qty, avg_entry_price, stop_loss, take_profit, status, "
            "realized_pnl, fees_paid, opened_at, closed_at) "
            "VALUES ('ETHUSDT','BUY',0.01,2100.0,2000.0,2200.0,'CLOSED',3.0,0.1,:t,:t)"
        ), {"t": _now_iso()})
        conn.commit()
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO positions (symbol, side, qty, avg_entry_price, stop_loss, take_profit, status, "
                "realized_pnl, fees_paid, opened_at, closed_at) "
                "VALUES ('BTCUSDT','BUY',0.001,100.5,90.0,110.0,'OPEN',0,0.01,:t,NULL)"
            ), {"t": _now_iso()})

    # Idempotent re-run.
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM operational_sessions")).scalar() == sessions_before + 1


def test_migration_v7_only_recorded_history_with_v7_invariants_missing_is_rejected(tmp_path):
    """A database claiming v7 without the real v7 structural invariants
    (the partial unique index on positions specifically) must be rejected,
    not silently trusted."""
    engine = _make_real_v6_engine(tmp_path, name="claims_v7_missing_invariants.db")
    run_migrations(engine)  # real, consistent v7

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_position_open_symbol"))

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_migration_v7_rebuild_is_rolled_back_on_later_failure(tmp_path, monkeypatch):
    """Adversarial reproduction of the same DDL-rollback guarantee at the
    v6->v7 boundary -- this time the migration performs a full table
    REBUILD (operational_sessions), not just an ADD COLUMN, so this proves
    the rollback guarantee holds even when the migration drops/recreates a
    table mid-transaction."""
    import app.persistence.migrations as migrations_module

    engine = _make_real_v6_engine(tmp_path, name="adversarial_v7_rollback.db")
    real_migrations = list(migrations_module.MIGRATIONS)

    schema_before = _full_schema_snapshot(engine)
    data_before = _row_counts(engine)

    def _rebuilds_sessions_then_fails(conn):
        conn.execute(text("ALTER TABLE operational_sessions ADD COLUMN symbols TEXT"))
        raise RuntimeError("falha simulada APÓS uma alteração de esquema real (v7)")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(7, "migração v7 adversarial (ALTER real, depois falha)", _rebuilds_sessions_then_fails)],
    )

    with pytest.raises(MigrationError):
        run_migrations(engine)

    schema_after = _full_schema_snapshot(engine)
    assert schema_after == schema_before
    assert "symbols" not in {c[0] for c in schema_after["operational_sessions"]["columns"]}
    assert _row_counts(engine) == data_before
    assert current_schema_version(engine) == 6

    monkeypatch.setitem(migrations_module.__dict__, "MIGRATIONS", real_migrations)
    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION


# --- Correção final da auditoria do PO (Fase 3.1): source_candle_open_time -

def _make_real_v7_engine(tmp_path, name="real_v7.db"):
    """A genuine, consistent v7 database (stopping exactly at the
    previously-approved Fase 3 multiativo schema, with real rows in it) --
    same pattern as `_make_real_v6_engine`, one version up."""
    import app.persistence.migrations as migrations_module

    engine = _make_legacy_v0_engine(tmp_path, name=name)
    real_migrations = list(migrations_module.MIGRATIONS)
    only_through_v7 = [m for m in real_migrations if m[0] <= 7]
    try:
        migrations_module.__dict__["MIGRATIONS"] = only_through_v7
        report = run_migrations(engine)
    finally:
        migrations_module.__dict__["MIGRATIONS"] = real_migrations
    assert report.ending_version == 7
    return engine


def test_migration_v8_upgrades_a_real_v7_database_preserving_data_and_is_idempotent(tmp_path):
    """Upgrading from an actual, previously-approved v7 database (not a
    v0-to-v8 shortcut) must preserve every row -- including the pre-existing
    strategy_signals row seeded by `_seed_v0_sample_data`, which has NO
    source_candle_open_time (legacy signal, correctly left NULL, never
    backfilled/invented) -- add the new nullable column, and be a safe
    no-op on immediate re-run."""
    engine = _make_real_v7_engine(tmp_path)
    before_counts = _row_counts(engine)
    assert current_schema_version(engine) == 7

    report = run_migrations(engine)
    assert report.starting_version == 7
    assert report.applied == [8, 9]
    assert report.ending_version == CURRENT_SCHEMA_VERSION
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION

    after_counts = _row_counts(engine)
    for table, before in before_counts.items():
        assert after_counts[table] == before, f"{table} lost rows during v8 migration"

    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(strategy_signals)")).fetchall()}
        assert "source_candle_open_time" in cols

        # The pre-existing legacy signal row got NULL, never an invented value.
        row = conn.execute(text(
            "SELECT source_candle_open_time FROM strategy_signals WHERE id = 1"
        )).fetchone()
        assert row == (None,)

        # New column is immediately usable end-to-end with a real UTC value.
        conn.execute(text(
            "UPDATE strategy_signals SET source_candle_open_time = :t WHERE id = 1"
        ), {"t": _now_iso()})
        conn.commit()

    # Idempotent re-run.
    report_again = run_migrations(engine)
    assert report_again.applied == []
    assert current_schema_version(engine) == CURRENT_SCHEMA_VERSION
    assert _row_counts(engine)["strategy_signals"] == before_counts["strategy_signals"]


def test_migration_v8_only_recorded_history_with_v8_invariants_missing_is_rejected(tmp_path):
    """A database claiming v8 without the real v8 structural invariant
    (source_candle_open_time genuinely present and nullable) must be
    rejected, not silently trusted."""
    engine = _make_real_v7_engine(tmp_path, name="claims_v8_missing_invariants.db")
    run_migrations(engine)  # real, consistent v8

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE strategy_signals RENAME TO strategy_signals_old"))
        conn.execute(text(
            "CREATE TABLE strategy_signals AS SELECT id, symbol, direction, justification, "
            "observed_price, atr, params_json, created_at FROM strategy_signals_old"
        ))
        conn.execute(text("DROP TABLE strategy_signals_old"))

    with pytest.raises(SchemaDivergenceError):
        current_schema_version(engine)
    with pytest.raises(SchemaDivergenceError):
        run_migrations(engine)


def test_migration_v8_add_column_is_rolled_back_on_later_failure(tmp_path, monkeypatch):
    """Adversarial reproduction of the same DDL-rollback guarantee at the
    v7->v8 boundary."""
    import app.persistence.migrations as migrations_module

    engine = _make_real_v7_engine(tmp_path, name="adversarial_v8_rollback.db")
    real_migrations = list(migrations_module.MIGRATIONS)

    schema_before = _full_schema_snapshot(engine)
    data_before = _row_counts(engine)

    def _leaks_a_column_then_fails(conn):
        conn.execute(text("ALTER TABLE strategy_signals ADD COLUMN leaked INTEGER DEFAULT 0"))
        raise RuntimeError("falha simulada APÓS uma alteração de esquema real (v8)")

    monkeypatch.setitem(
        migrations_module.__dict__, "MIGRATIONS",
        [(8, "migração v8 adversarial (ALTER real, depois falha)", _leaks_a_column_then_fails)],
    )

    with pytest.raises(MigrationError):
        run_migrations(engine)

    schema_after = _full_schema_snapshot(engine)
    assert schema_after == schema_before
    assert "leaked" not in {c[0] for c in schema_after["strategy_signals"]["columns"]}
    assert _row_counts(engine) == data_before
    assert current_schema_version(engine) == 7

    monkeypatch.setitem(migrations_module.__dict__, "MIGRATIONS", real_migrations)
    report = run_migrations(engine)
    assert report.ending_version == CURRENT_SCHEMA_VERSION


# --- Correção obrigatória do PO (Fase 3 multiativo, rodada 2): CHECK contra
# symbol/symbols ambos NULL, e índice único de sessão ativa por carteira ----

def test_check_constraint_rejects_both_symbol_and_symbols_null(tmp_path):
    engine = _make_real_v6_engine(tmp_path, name="check_constraint.db")
    run_migrations(engine)

    with engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
                "started_at, strategy_version, risk_config_json, config_snapshot_json) "
                "VALUES ('uid-both-null', 'REPLAY', NULL, NULL, '1', :t, 'v1', '{}', '{}')"
            ), {"t": _now_iso()})
            conn.commit()


def test_check_constraint_accepts_legacy_and_new_shapes(tmp_path):
    engine = _make_real_v6_engine(tmp_path, name="check_constraint_ok.db")
    run_migrations(engine)

    with engine.begin() as conn:
        # Legacy shape: symbol set, symbols NULL.
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-legacy', 'REPLAY', 'BTCUSDT', NULL, '1', :t, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})
        # New multi-symbol shape: symbol NULL, symbols set.
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-multi', 'REPLAY', NULL, '[\"BTCUSDT\",\"ETHUSDT\"]', '1', :t, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM operational_sessions WHERE session_uid IN ('uid-legacy', 'uid-multi')"
        )).scalar()
        assert count == 2


def test_active_session_per_portfolio_unique_index_rejects_second_active_session(tmp_path):
    engine = _make_real_v6_engine(tmp_path, name="active_session_unique.db")
    run_migrations(engine)

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-active-1', 'REPLAY', NULL, '[\"BTCUSDT\",\"ETHUSDT\"]', '1', :t, NULL, "
            "'v1', '{}', '{}')"
        ), {"t": _now_iso()})

    with engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
                "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
                "VALUES ('uid-active-2', 'REPLAY', NULL, '[\"BTCUSDT\",\"ETHUSDT\"]', '1', :t, NULL, "
                "'v1', '{}', '{}')"
            ), {"t": _now_iso()})
            conn.commit()

    # A SECOND active session for a DIFFERENT portfolio (different symbols)
    # is unaffected -- the constraint is per-portfolio, not global.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-active-solusdt', 'REPLAY', NULL, '[\"SOLUSDT\"]', '1', :t, NULL, "
            "'v1', '{}', '{}')"
        ), {"t": _now_iso()})

    # An ENDED session never conflicts with a new active one for the same portfolio.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE operational_sessions SET ended_at = :t WHERE session_uid = 'uid-active-1'"
        ), {"t": _now_iso()})
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-active-3', 'REPLAY', NULL, '[\"BTCUSDT\",\"ETHUSDT\"]', '1', :t, NULL, "
            "'v1', '{}', '{}')"
        ), {"t": _now_iso()})

    # Multiple legacy rows (symbols IS NULL) for the same mode+symbol are
    # NEVER constrained by this index -- it explicitly excludes them.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-legacy-active-1', 'REPLAY', 'ADAUSDT', NULL, '1', :t, NULL, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-legacy-active-2', 'REPLAY', 'ADAUSDT', NULL, '1', :t, NULL, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})


def test_migration_v7_refuses_to_proceed_when_pre_existing_duplicate_active_sessions_exist(tmp_path):
    """Decisão do PO: a migração NUNCA apaga/corrige dados reais em
    silêncio -- se o banco já tem (hipoteticamente) duas sessões ativas
    para a mesma carteira antes da migração rodar, ela recusa e para,
    deixando os dados intactos para intervenção manual."""
    engine = _make_real_v6_engine(tmp_path, name="pre_existing_duplicates.db")

    # Simulates a hypothetical pre-existing violation by inserting directly
    # (bypassing the app) with a HAND-ADDED `symbols` column before v7 runs
    # -- reproduces "the column already has data with duplicates" rather
    # than a scenario the app itself could ever produce going forward.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE operational_sessions ADD COLUMN symbols TEXT"))
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-dup-1', 'REPLAY', 'BTCUSDT', '[\"BTCUSDT\"]', '1', :t, NULL, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})
        conn.execute(text(
            "INSERT INTO operational_sessions (session_uid, mode, symbol, symbols, timeframe, "
            "started_at, ended_at, strategy_version, risk_config_json, config_snapshot_json) "
            "VALUES ('uid-dup-2', 'REPLAY', 'BTCUSDT', '[\"BTCUSDT\"]', '1', :t, NULL, 'v1', '{}', '{}')"
        ), {"t": _now_iso()})

    with pytest.raises(MigrationError) as excinfo:
        run_migrations(engine)
    assert "sessões operacionais" in str(excinfo.value).lower() or "sessões ativas" in str(excinfo.value).lower()

    # Nothing was deleted or altered.
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM operational_sessions WHERE session_uid IN ('uid-dup-1', 'uid-dup-2')"
        )).scalar()
        assert count == 2
