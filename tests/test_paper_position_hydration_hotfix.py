"""Hotfix: PAPER_LOCAL/PAPER_LIVE (`PaperLocalExecutionEngine`) never
persisted its own simulated position book -- every process restart with a
real OPEN position in the database produced a FALSE reconciliation
divergence ("corretora não reporta nenhuma posição"), blocking trading by
mistake even though nothing was actually wrong. `build_orchestrator()` now
calls `execution_engine.hydrate_positions(...)`, seeding the engine's
in-memory book from `repo.open_positions(session)` BEFORE the startup
`reconcile()` call, so a restart with a genuinely open position no longer
trips a false alarm.

This module tests both layers:
  - `PaperLocalExecutionEngine.hydrate_positions()` in isolation (unit).
  - The full `build_orchestrator()` boot path across a simulated restart
    (integration, same pattern as `test_build_orchestrator_multiativo.py`).
"""
from __future__ import annotations

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.paper_local import PaperLocalExecutionEngine
from app.persistence import repo
from app.persistence.db import session_scope


# ---------------------------------------------------------------------------
# Unit tests: PaperLocalExecutionEngine.hydrate_positions() in isolation
# ---------------------------------------------------------------------------

def test_hydrate_positions_with_empty_list_is_a_no_op():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([])
    assert engine.get_position("BTCUSDT") is None


def test_hydrate_positions_restores_a_single_buy_position():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.05, "avg_entry_price": 61234.5},
    ])
    pos = engine.get_position("BTCUSDT")
    assert pos == {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.05, "avg_entry_price": 61234.5}


def test_hydrate_positions_restores_a_single_sell_position():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "ETHUSDT", "side": "SELL", "qty": 1.5, "avg_entry_price": 3200.0},
    ])
    pos = engine.get_position("ETHUSDT")
    assert pos["side"] == "SELL"
    assert pos["qty"] == 1.5
    assert pos["avg_entry_price"] == 3200.0


def test_hydrate_positions_handles_multiple_symbols_independently():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.01, "avg_entry_price": 60000.0},
        {"symbol": "ETHUSDT", "side": "SELL", "qty": 2.0, "avg_entry_price": 3100.0},
        {"symbol": "SOLUSDT", "side": "BUY", "qty": 10.0, "avg_entry_price": 150.0},
    ])
    assert engine.get_position("BTCUSDT")["side"] == "BUY"
    assert engine.get_position("ETHUSDT")["side"] == "SELL"
    assert engine.get_position("SOLUSDT")["qty"] == 10.0
    # No cross-contamination between symbols.
    assert engine.get_position("BTCUSDT")["qty"] == 0.01


def test_hydrate_positions_skips_zero_or_negative_quantity():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.0, "avg_entry_price": 60000.0},
        {"symbol": "ETHUSDT", "side": "BUY", "qty": -0.5, "avg_entry_price": 3000.0},
        {"symbol": "SOLUSDT", "side": "BUY", "qty": 10.0, "avg_entry_price": 150.0},
    ])
    assert engine.get_position("BTCUSDT") is None
    assert engine.get_position("ETHUSDT") is None
    assert engine.get_position("SOLUSDT") is not None


def test_hydrate_positions_fully_replaces_state_not_merges():
    """A second call with a shrunk/changed list must not leave any stale
    entry from the previous call behind -- required for the operation to
    stay idempotent/correct even if it were ever invoked more than once
    (production only calls it once, at boot, on a freshly built engine)."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.01, "avg_entry_price": 60000.0},
        {"symbol": "ETHUSDT", "side": "BUY", "qty": 1.0, "avg_entry_price": 3000.0},
    ])
    engine.hydrate_positions([
        {"symbol": "ETHUSDT", "side": "SELL", "qty": 2.0, "avg_entry_price": 3100.0},
    ])
    assert engine.get_position("BTCUSDT") is None  # no longer in the new list
    assert engine.get_position("ETHUSDT") == {
        "symbol": "ETHUSDT", "side": "SELL", "qty": 2.0, "avg_entry_price": 3100.0,
    }


def test_hydrate_positions_repeated_call_with_same_data_is_idempotent():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    payload = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.01, "avg_entry_price": 60000.0}]
    engine.hydrate_positions(payload)
    first = dict(engine.get_position("BTCUSDT"))
    engine.hydrate_positions(payload)
    second = engine.get_position("BTCUSDT")
    assert first == second


def test_hydrate_positions_preserves_exact_quantity_and_price_precision():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.123456789, "avg_entry_price": 61234.987654321},
    ])
    pos = engine.get_position("BTCUSDT")
    assert pos["qty"] == 0.123456789
    assert pos["avg_entry_price"] == 61234.987654321


def test_hydrate_positions_never_creates_order_fill_or_execution_side_effect():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 100.0)
    engine.hydrate_positions([
        {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.01, "avg_entry_price": 60000.0},
    ])
    assert engine._snapshots == {}
    assert next(engine._fill_id_seq) == 1  # sequence untouched, still starts at 1


def test_bybit_demo_execution_engine_has_no_hydrate_positions_attribute():
    """Confirms the `getattr(execution_engine, "hydrate_positions", None)`
    guard in `build_orchestrator()` is a genuine no-op for BYBIT_DEMO --
    that engine talks to the real demo exchange via `get_position()` and
    never needs (or has) an in-memory book to seed."""
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", lambda *a, **k: {}, lambda *a, **k: {}, sleep=lambda s: None,
    )
    assert getattr(engine, "hydrate_positions", None) is None


# ---------------------------------------------------------------------------
# Integration tests: full build_orchestrator() boot path across a
# simulated restart (same DB file reopened, new process-equivalent engine).
# ---------------------------------------------------------------------------

def _boot(tmp_path, db_name="restart.db", symbols="BTCUSDT"):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols,
        database_url=f"sqlite:///{tmp_path / db_name}",
        strategy_timeframe_minutes=1,
    )
    return build_orchestrator(settings)


def test_boot_with_no_persisted_position_hydrates_nothing_and_does_not_block(tmp_path):
    orch = _boot(tmp_path)
    assert orch.execution_engine.get_position("BTCUSDT") is None
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is False
        assert state.state_ambiguous is False


def test_restart_with_open_buy_position_no_longer_produces_false_positive_block(tmp_path):
    # First boot: fresh process, no position yet.
    orch1 = _boot(tmp_path)
    with session_scope(orch1.session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 61000.0, 59000.0, 63000.0)

    # Second boot: simulates a process restart -- brand-new orchestrator and
    # brand-new (empty) PaperLocalExecutionEngine, same underlying database.
    orch2 = _boot(tmp_path)

    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.state_ambiguous is False, (
            "restart with a genuinely open position must not be treated as a divergence"
        )
        assert state.trading_blocked is False
        assert state.reconciliation_diverged is False

    hydrated = orch2.execution_engine.get_position("BTCUSDT")
    assert hydrated is not None
    assert hydrated["side"] == "BUY"
    assert hydrated["qty"] == 0.01
    assert hydrated["avg_entry_price"] == 61000.0


def test_restart_with_open_sell_position_no_longer_produces_false_positive_block(tmp_path):
    orch1 = _boot(tmp_path)
    with session_scope(orch1.session_factory) as session:
        repo.open_position(session, "BTCUSDT", "SELL", 0.02, 61000.0, 63000.0, 59000.0)

    orch2 = _boot(tmp_path)
    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is False
        assert state.state_ambiguous is False

    hydrated = orch2.execution_engine.get_position("BTCUSDT")
    assert hydrated["side"] == "SELL"
    assert hydrated["qty"] == 0.02


def test_restart_with_multiple_open_positions_across_symbols(tmp_path):
    symbols = "BTCUSDT,ETHUSDT,SOLUSDT"
    orch1 = _boot(tmp_path, db_name="multi.db", symbols=symbols)
    with session_scope(orch1.session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 61000.0, 59000.0, 63000.0)
        repo.open_position(session, "ETHUSDT", "SELL", 1.5, 3100.0, 3300.0, 2900.0)
        # SOLUSDT intentionally left without a position.

    orch2 = _boot(tmp_path, db_name="multi.db", symbols=symbols)
    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is False
        assert state.state_ambiguous is False

    engine = orch2.orchestrators["BTCUSDT"].execution_engine
    assert engine is orch2.orchestrators["ETHUSDT"].execution_engine  # shared instance
    assert engine.get_position("BTCUSDT")["side"] == "BUY"
    assert engine.get_position("ETHUSDT")["side"] == "SELL"
    assert engine.get_position("SOLUSDT") is None


def test_closed_position_is_never_hydrated(tmp_path):
    orch1 = _boot(tmp_path)
    with session_scope(orch1.session_factory) as session:
        pos = repo.open_position(session, "BTCUSDT", "BUY", 0.01, 61000.0, 59000.0, 63000.0)
        repo.close_position(session, pos, realized_pnl_delta=25.0, closing_fee=0.5)

    orch2 = _boot(tmp_path)
    assert orch2.execution_engine.get_position("BTCUSDT") is None
    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        # No open position anywhere -> nothing to hydrate -> clean reconciliation.
        assert state.trading_blocked is False


def test_hydration_runs_before_reconciliation(tmp_path, monkeypatch):
    """Direct proof of ordering, not just of the outcome: patches both
    `hydrate_positions` and `reconcile` on the constructed engine/orchestrator
    to record call order."""
    calls: list[str] = []

    orch1 = _boot(tmp_path)
    with session_scope(orch1.session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 61000.0, 59000.0, 63000.0)

    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / 'restart.db'}",
        strategy_timeframe_minutes=1,
    )

    original_hydrate = PaperLocalExecutionEngine.hydrate_positions
    original_reconcile_orch = None

    def traced_hydrate(self, positions):
        calls.append("hydrate_positions")
        return original_hydrate(self, positions)

    monkeypatch.setattr(PaperLocalExecutionEngine, "hydrate_positions", traced_hydrate)

    from app.orchestrator import Orchestrator

    original_reconcile_orch = Orchestrator.reconcile

    def traced_reconcile(self, session, state):
        calls.append("reconcile")
        return original_reconcile_orch(self, session, state)

    monkeypatch.setattr(Orchestrator, "reconcile", traced_reconcile)

    build_orchestrator(settings)

    assert "hydrate_positions" in calls
    assert "reconcile" in calls
    assert calls.index("hydrate_positions") < calls.index("reconcile")


def test_genuine_divergence_after_boot_is_still_detected(tmp_path):
    """Hydration only fixes the boot-time false positive -- it must not
    disable reconciliation's ability to catch a REAL divergence that
    develops afterward (engine and DB genuinely disagreeing)."""
    orch1 = _boot(tmp_path)
    with session_scope(orch1.session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 61000.0, 59000.0, 63000.0)

    orch2 = _boot(tmp_path)
    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is False  # sanity: hydration worked

    # Simulate a genuine drift developing after boot: the "exchange" (this
    # simulator) now reports a materially different quantity than the DB.
    orch2.execution_engine._positions["BTCUSDT"]["qty"] = 999.0

    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch2.reconcile(session, state)

    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.state_ambiguous is True
        assert state.trading_blocked is True
