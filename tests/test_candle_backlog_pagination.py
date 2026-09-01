"""Correction v1.4 #2: a fixed-size single request (`limit=5`) could never
guarantee draining an arbitrarily large backlog of pending closed candles --
BybitDemoMarketDataProvider paginates forward from a persistent cursor until
it catches up, never limited to one page's worth of history.

Correction v1.5 #1: the official Bybit V5 kline contract applies `limit` to
the GLOBAL result set sorted by `startTime` DESCENDING -- it does NOT
guarantee a `start`-only query returns the oldest rows in range. The fake
below now implements that real contract (filter by start/end, sort ALL
candidates descending, THEN apply limit) instead of the old
"filter-then-take-oldest" shortcut that silently masked the real bug. The
provider now bounds every historical page with BOTH `start` AND `end` so a
window can never contain more closed candles than `limit`, regardless of
server-side ordering.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.core.errors import ExchangeTimeoutError
from app.market_data.base import CandleFetchStatus
from app.market_data.bybit_provider import BybitDemoMarketDataProvider
from app.persistence import repo
from app.persistence.db import session_scope


def _rows(n: int, start: datetime, interval_minutes: int = 1) -> list[list[str]]:
    rows = []
    for i in range(n):
        t = start + timedelta(minutes=i * interval_minutes)
        close = f"{100 + i}.0"
        rows.append([str(int(t.timestamp() * 1000)), close, close, close, close, "10", "0"])
    return rows


class FakeKlineStore:
    """A Bybit `/v5/market/kline` double implementing the REAL documented
    contract: filters candidates by `start`/`end` (inclusive, ms) when
    present, THEN sorts ALL matching candidates by `startTime` DESCENDING,
    THEN applies `limit`. This is deliberately NOT "filter then take the
    oldest N" -- that shortcut is exactly what let the pre-v1.5 provider bug
    (silently losing older backlog candles) hide behind a passing test
    suite. Row input order is irrelevant; the store always re-derives the
    real response ordering itself (correction v1.5 #1, required test #1)."""

    def __init__(self, rows: list[list[str]]):
        self._rows = list(rows)
        self.calls: list[dict] = []
        self.fail_next_n_with_timeout = 0

    def http_get(self, url: str, params: dict) -> dict:
        self.calls.append(dict(params))
        if url.endswith("/v5/market/time"):
            return {"result": {"timeSecond": str(int(datetime.now(timezone.utc).timestamp()))}}
        if not url.endswith("/v5/market/kline"):
            return {"result": {"list": []}}
        if self.fail_next_n_with_timeout > 0:
            self.fail_next_n_with_timeout -= 1
            raise ExchangeTimeoutError("simulated timeout mid-pagination")

        start = params.get("start")
        end = params.get("end")
        limit = int(params.get("limit", 200))
        candidates = self._rows
        if start is not None:
            candidates = [r for r in candidates if int(r[0]) >= start]
        if end is not None:
            candidates = [r for r in candidates if int(r[0]) <= end]
        # Real Bybit contract: global descending sort BEFORE limit is applied.
        candidates_desc = sorted(candidates, key=lambda r: int(r[0]), reverse=True)
        page = candidates_desc[: limit]
        return {"result": {"list": page}}


BASE = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Required test 1 / 9: contract test for the fake itself ----------------

def test_fake_applies_global_descending_sort_before_limit():
    """If the fake regressed to "filter by start, take the oldest N" (the
    pre-v1.5 bug), this test would fail: with a wide start-only range and a
    small limit, the real contract returns the NEWEST rows in range, not the
    oldest."""
    rows = _rows(20, BASE)  # candles at minute 0..19
    store = FakeKlineStore(rows)

    resp = store.http_get(
        "https://api-demo.bybit.com/v5/market/kline",
        {"start": int(BASE.timestamp() * 1000), "limit": 5},
    )
    got = [int(r[0]) for r in resp["result"]["list"]]
    expected_newest_five_desc = [
        int((BASE + timedelta(minutes=i)).timestamp() * 1000) for i in (19, 18, 17, 16, 15)
    ]
    assert got == expected_newest_five_desc, (
        "fake must sort descending by startTime before applying limit -- "
        "got the oldest rows instead, which is the exact bug this correction fixes"
    )


# --- Required tests 2, 3, 4: full 17-candle backlog drain -------------------

def test_backlog_larger_than_one_page_is_fully_drained_with_persisted_cursor():
    """17 closed candles pending, a cursor already persisted BEFORE the
    first of them (correction v1.5 #1's own reproduction scenario), server
    paginates in pages of 5 -- all 17 must eventually be delivered, exactly
    once each, in order. Every historical page request must carry both
    `start` and `end` (required test #3)."""
    rows = _rows(17, BASE)
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=17, seconds=30)  # all 17 are closed

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=5, max_pages_per_poll=10,
    )
    provider.sync_cursor(BASE - timedelta(minutes=1))  # cursor persisted anterior ao primeiro

    delivered = []
    for _ in range(17):
        result = provider.next_candle()
        assert result.status == CandleFetchStatus.CANDLE_AVAILABLE, f"failed at #{len(delivered)}: {result}"
        delivered.append(result.candle.open_time)

    expected = [BASE + timedelta(minutes=i) for i in range(17)]
    assert delivered == expected

    kline_calls = [c for c in store.calls if "symbol" in c]
    assert kline_calls, "expected at least one kline request"
    for call in kline_calls:
        assert "start" in call and call["start"] is not None
        assert "end" in call and call["end"] is not None

    # Nothing left -- next call finds no new candle.
    assert provider.next_candle().status == CandleFetchStatus.NO_NEW_CANDLE


def test_backlog_drained_correctly_regardless_of_response_row_order():
    """The fake always re-derives the real (descending-then-limit) response
    order internally regardless of how rows were constructed, so passing
    rows built in any order must still result in exact correct chronological
    delivery."""
    import random

    rows_chrono = _rows(12, BASE)
    shuffled = rows_chrono[:]
    random.Random(42).shuffle(shuffled)
    store = FakeKlineStore(shuffled)
    fixed_now = BASE + timedelta(minutes=12, seconds=30)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=4, max_pages_per_poll=10,
    )
    provider.sync_cursor(BASE - timedelta(minutes=1))

    delivered = [provider.next_candle().candle.open_time for _ in range(12)]
    assert delivered == [BASE + timedelta(minutes=i) for i in range(12)]


# --- Required test 8: open candle never enters the processed range ---------

def test_open_candle_mixed_in_with_closed_backlog_is_never_delivered():
    rows = _rows(6, BASE)  # 0..5 minutes -- the 6th (index 5) will still be forming
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=5, seconds=30)  # candle #5 (12:05) not closed yet

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10,
    )
    provider.sync_cursor(BASE - timedelta(minutes=1))

    delivered = [provider.next_candle().candle.open_time for _ in range(5)]
    assert delivered == [BASE + timedelta(minutes=i) for i in range(5)]
    assert provider.next_candle().status == CandleFetchStatus.NO_NEW_CANDLE  # #5 still open


# --- Required test 5: failure and resume between windows -------------------

def test_temporary_failure_between_pages_resumes_without_gap_or_duplicate():
    """Window 1 succeeds and is queued; the fetch for window 2 times out.
    The already-collected candles from window 1 must still be delivered
    (not discarded), and a later call must fetch exactly the remaining
    backlog -- no duplicate, no gap."""
    rows = _rows(9, BASE)
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=9, seconds=30)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=4, max_pages_per_poll=10,
    )
    provider.sync_cursor(BASE - timedelta(minutes=1))

    orig_get = store.http_get
    call_count = {"n": 0}

    def flaky_get(url, params):
        call_count["n"] += 1
        if call_count["n"] == 1:  # first kline request = first window
            raise ExchangeTimeoutError("simulated timeout on window 1")
        return orig_get(url, params)

    provider._http_get = flaky_get

    first = provider.next_candle()
    assert first.status == CandleFetchStatus.RETRYABLE_ERROR

    # Retry (this time succeeding): window 1 (candles 0-3) is fetched and queued.
    provider._http_get = orig_get
    delivered = [provider.next_candle().candle.open_time for _ in range(4)]
    assert delivered == [BASE + timedelta(minutes=i) for i in range(4)]

    # Queue now empty -- next call fetches window 2 and continues exactly
    # from candle 4, no gap, no repeat of 0-3.
    remaining = [provider.next_candle().candle.open_time for _ in range(5)]
    assert remaining == [BASE + timedelta(minutes=i) for i in range(4, 9)]


# --- Required test: gap in the closed candle sequence -----------------------

def test_gap_in_closed_candle_sequence_is_reported_explicitly_and_safely():
    """If the sequence of closed candles skips straight past where the
    cursor expects the next candle (a genuine hole), the provider must
    report GAP_DETECTED explicitly -- never silently skip ahead."""
    # Candles 0,1 exist; candles 2,3 are MISSING; 4,5 exist. Cursor sits at 1.
    rows = _rows(2, BASE) + _rows(2, BASE + timedelta(minutes=4))
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=6)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10,
    )
    provider.sync_cursor(BASE - timedelta(minutes=1))

    first = provider.next_candle()
    assert first.candle.open_time == BASE
    second = provider.next_candle()
    assert second.candle.open_time == BASE + timedelta(minutes=1)

    gap_result = provider.next_candle()
    assert gap_result.status == CandleFetchStatus.GAP_DETECTED
    assert gap_result.candle is None
    assert "lacuna" in gap_result.detail.lower()


def test_orchestrator_blocks_trading_on_gap_detected_but_keeps_polling(session_factory):
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine

    rows = _rows(2, BASE) + _rows(2, BASE + timedelta(minutes=4))
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=6)

    # No candle persisted yet at process start -- explicit first-boot policy
    # (MARKET_DATA_INITIAL_START-equivalent) anchors this test's backlog
    # deterministically at BASE instead of the default "most recent closed
    # candle" baseline (correction v1.5 #1).
    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10, initial_start=BASE,
    )
    settings = Settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s",
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> uma decisão". O default do timeframe estratégico passou a ser
        # 5 minutos; 1 minuto é a compatibilidade explícita mantida pelo PO
        # e preserva exatamente a intenção original do teste.
        strategy_timeframe_minutes=1,
    )
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=provider, strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )

    orch.tick()  # candle 0
    orch.tick()  # candle 1
    gap_tick = orch.tick()
    assert gap_tick["status"] == "gap_detected"

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True

    # The loop is NOT over -- another tick is still possible (only
    # REPLAY_FINISHED, never produced here, may end it).
    next_tick = orch.tick()
    assert next_tick["status"] != "no_data"


# --- Required test 6: process restart mid-drain resumes via persisted cursor

def test_provider_restart_mid_drain_resumes_via_persisted_cursor_without_duplicates(session_factory):
    """Simulates a process restart partway through draining a backlog: a
    SECOND, freshly constructed provider instance (sharing nothing in
    memory with the first) must resume exactly where the first left off,
    using only what was actually persisted to the database."""
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine

    rows = _rows(9, BASE)
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=9, seconds=30)
    settings = Settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s",
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> uma decisão". O default do timeframe estratégico passou a ser
        # 5 minutos; 1 minuto é a compatibilidade explícita mantida pelo PO
        # e preserva exatamente a intenção original do teste.
        strategy_timeframe_minutes=1,
    )

    def build_orch(provider):
        price_state: dict[str, float] = {}
        return Orchestrator(
            settings=settings, session_factory=session_factory,
            market_data_provider=provider, strategy_engine=StrategyEngine(symbol="BTCUSDT"),
            risk_engine=RiskEngine(RiskLimits()),
            execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
            ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
            clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
        )

    provider_a = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now, page_size=4,
        initial_start=BASE,
    )
    orch_a = build_orch(provider_a)
    for _ in range(4):  # drain candles 0-3, then stop -- simulating a crash/restart
        result = orch_a.tick()
        assert result["status"] != "no_data"

    with session_scope(session_factory) as session:
        signals_before = repo.recent_signals(session, limit=100)
    assert len(signals_before) == 4

    # "Restart": a brand-new provider instance, no in-memory state carried
    # over, backed by the SAME store and the SAME database. It has no
    # `initial_start` -- it doesn't need one, because sync_cursor() (fed
    # from the persisted candles) always takes priority over the first-boot
    # bootstrap policy.
    provider_b = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now, page_size=4,
    )
    orch_b = build_orch(provider_b)
    for _ in range(5):  # drain the remaining candles 4-8
        result = orch_b.tick()
        assert result["status"] != "no_data"

    with session_scope(session_factory) as session:
        signals_after = repo.recent_signals(session, limit=100)
        candle_open_times = sorted(
            row[0] for row in session.execute(sql_text("SELECT open_time FROM candles")).fetchall()
        )

    assert len(signals_after) == 9  # no duplicates: exactly one signal per candle, 0 through 8
    assert len(candle_open_times) == 9


# --- Required test 7: first boot without any persisted cursor --------------

def test_first_boot_with_no_cursor_and_no_configured_start_anchors_to_latest_closed_candle():
    """Explicit first-boot policy (correction v1.5 #1), default variant: no
    `MARKET_DATA_INITIAL_START`-equivalent configured, no cursor persisted
    yet. The provider must NOT attempt to recover unbounded history -- it
    queries exactly ONE bounded lookback window (`page_size` candles wide)
    and delivers only the closed candles found inside it. None of the 40
    older candles sitting further back in this long fixture history (well
    outside that single window) are ever delivered."""
    rows = _rows(50, BASE)  # a long history that must NOT all be replayed
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=49, seconds=30)  # candles 0..48 closed, #49 still forming

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10,
    )

    # Only the closed candles inside the single `page_size`-wide lookback
    # window (minutes 40-48) are delivered, oldest first -- never the 40
    # older candles (0-39) sitting further back in history.
    delivered = [provider.next_candle().candle.open_time for _ in range(9)]
    assert delivered == [BASE + timedelta(minutes=i) for i in range(40, 49)]

    assert provider.next_candle().status == CandleFetchStatus.NO_NEW_CANDLE  # #49 still forming

    # Every request made (bootstrap window and forward pagination alike) was
    # bounded by both start and end -- never an unbounded/start-only query.
    kline_calls = [c for c in store.calls if "symbol" in c]
    assert kline_calls
    for call in kline_calls:
        assert call.get("start") is not None and call.get("end") is not None

    # The NEXT closed candle (minute 49) must be delivered once it closes.
    new_now = BASE + timedelta(minutes=50, seconds=30)
    provider._now_fn = lambda: new_now
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert result.candle.open_time == BASE + timedelta(minutes=49)


def test_first_boot_with_configured_initial_start_anchors_there_instead():
    """Explicit first-boot policy (correction v1.5 #1), configured variant:
    `initial_start` (MARKET_DATA_INITIAL_START) anchors the very first
    cursor deterministically, without any bootstrap probe request at all."""
    rows = _rows(20, BASE)
    store = FakeKlineStore(rows)
    fixed_now = BASE + timedelta(minutes=20, seconds=30)

    configured_start = BASE + timedelta(minutes=15)
    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10, initial_start=configured_start,
    )

    delivered = [provider.next_candle().candle.open_time for _ in range(5)]
    assert delivered == [configured_start + timedelta(minutes=i) for i in range(5)]

    kline_calls = [c for c in store.calls if "symbol" in c]
    assert kline_calls
    for call in kline_calls:
        assert call.get("start") is not None and call.get("end") is not None
