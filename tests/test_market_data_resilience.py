"""Correction v1.2 #1 and #2: a transitory failure (timeout, rate limit, an
empty response, or a still-forming candle) must never look like "REPLAY_FINISHED"
to the orchestrator -- only an actually exhausted REPLAY fixture may end the
polling loop. Also covers persistent deduplication by symbol+timeframe+
open_time and rejection of a still-open candle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ExchangeTimeoutError
from app.market_data.base import CandleFetchStatus
from app.market_data.bybit_provider import BybitDemoMarketDataProvider
from app.persistence import repo
from app.persistence.db import session_scope


def _kline_row(open_time: datetime, close: float = 100.0) -> list:
    return [str(int(open_time.timestamp() * 1000)), "100", "101", "99", str(close), "10", "0"]


def test_timeout_then_empty_then_valid_candle_keeps_provider_alive():
    """The exact sequence required by correction v1.2 #1: timeout ->
    empty response -> valid candle. The provider must report
    RETRYABLE_ERROR, then NO_NEW_CANDLE, then CANDLE_AVAILABLE -- never
    anything that looks like the feed being "finished"."""
    calls = {"n": 0}
    fixed_now = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    candle_open_time = fixed_now - timedelta(minutes=5)

    def fake_get(url, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExchangeTimeoutError("simulated timeout")
        if calls["n"] == 2:
            return {"result": {"list": []}}
        return {"result": {"list": [_kline_row(candle_open_time)]}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
    )

    r1 = provider.next_candle()
    assert r1.status == CandleFetchStatus.RETRYABLE_ERROR

    r2 = provider.next_candle()
    assert r2.status == CandleFetchStatus.NO_NEW_CANDLE

    r3 = provider.next_candle()
    assert r3.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert r3.candle is not None
    assert r3.candle.close == pytest.approx(100.0)
    assert calls["n"] == 3


def test_orchestrator_loop_never_reports_no_data_for_transitory_failures(session_factory):
    """Drives Orchestrator.tick() through the same timeout -> empty -> valid
    sequence and checks that only a real REPLAY_FINISHED (never produced
    here) would report status "no_data" -- the status the polling loop in
    app/api/main.py treats as the ONLY reason to stop."""
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine

    calls = {"n": 0}
    fixed_now = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    candle_open_time = fixed_now - timedelta(minutes=5)

    def fake_get(url, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExchangeTimeoutError("simulated timeout")
        if calls["n"] == 2:
            return {"result": {"list": []}}
        return {"result": {"list": [_kline_row(candle_open_time)]}}

    market_data_provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
    )

    settings = Settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s",
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> um sinal". Com o novo default (5 min) um único candle apenas
        # agrega; o timeframe estratégico de 1 minuto é a compatibilidade
        # explícita mantida pelo PO e preserva exatamente a intenção
        # original do teste.
        strategy_timeframe_minutes=1,
    )
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )

    r1 = orch.tick()
    assert r1["status"] == "retryable_error"

    r2 = orch.tick()
    assert r2["status"] == "no_new_candle"

    r3 = orch.tick()
    assert r3["status"] not in ("no_data",)

    with session_scope(session_factory) as session:
        candles = repo.recent_signals(session, limit=10)
        assert len(candles) >= 1  # the valid candle was actually processed


def test_provider_deduplicates_repeated_candle_before_reaching_orchestrator():
    """Correction v1.2 #2: the SAME forming/duplicate candle returned over
    and over by a limit=1 kline poll must never be handed to the caller
    twice as CANDLE_AVAILABLE."""
    fixed_now = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    candle_open_time = fixed_now - timedelta(minutes=5)
    row = _kline_row(candle_open_time)

    def fake_get(url, params):
        return {"result": {"list": [row]}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
    )

    first = provider.next_candle()
    assert first.status == CandleFetchStatus.CANDLE_AVAILABLE

    for _ in range(10):
        repeat = provider.next_candle()
        assert repeat.status == CandleFetchStatus.NO_NEW_CANDLE


def test_still_forming_candle_is_never_processed_before_it_closes():
    """Correction v1.2 #2: a candle whose period has not fully elapsed yet
    relative to "now" must be rejected as NO_NEW_CANDLE, never handed to the
    caller as a decision-worthy closed candle."""
    fixed_now = datetime(2024, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
    still_open_candle_time = fixed_now - timedelta(seconds=10)  # started 10s ago, 1m interval

    def fake_get(url, params):
        return {"result": {"list": [_kline_row(still_open_candle_time)]}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
    )
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.NO_NEW_CANDLE
    assert result.candle is None


def test_candle_becomes_available_once_its_period_has_elapsed():
    fixed_now = datetime(2024, 6, 1, 12, 1, 5, tzinfo=timezone.utc)
    candle_open_time = fixed_now - timedelta(minutes=1, seconds=5)  # closed 5s ago

    def fake_get(url, params):
        return {"result": {"list": [_kline_row(candle_open_time)]}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
    )
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.CANDLE_AVAILABLE


def test_db_unique_constraint_prevents_duplicate_candle_rows(db_session):
    """Defense in depth beyond provider-level dedup: even if the same
    symbol+timeframe+open_time is submitted twice, save_candle() must not
    raise and must not create a second row.

    Fase 3.2 (decisão Q4 do PO): a gravação passa a ser CANONICALIZADA --
    o alias legado "1" (o formato de intervalo da Bybit) continua aceito
    na entrada, mas a linha persistida é sempre "1m". A deduplicação vale
    entre as duas grafias, porque ambas convergem para a mesma linha."""
    open_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    first = repo.save_candle(db_session, "BTCUSDT", "1", open_time, 100, 101, 99, 100.5, 10, "bybit_demo")
    assert first is not None
    assert first.timeframe == "1m"  # canonicalizado na escrita, nunca "1"

    second = repo.save_candle(db_session, "BTCUSDT", "1", open_time, 100, 101, 99, 100.5, 10, "bybit_demo")
    assert second is None  # duplicata, não exceção, não segunda linha

    # A mesma vela enviada já na grafia canônica também é duplicata.
    third = repo.save_candle(db_session, "BTCUSDT", "1m", open_time, 100, 101, 99, 100.5, 10, "bybit_demo")
    assert third is None

    from sqlalchemy import select
    from app.persistence.models import Candle

    rows = db_session.execute(
        select(Candle).where(Candle.symbol == "BTCUSDT", Candle.open_time == open_time)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].timeframe == "1m"


def test_orchestrator_skips_signal_ai_and_risk_for_a_duplicate_candle(session_factory):
    """No duplicate signal/AI recommendation/risk evaluation is ever created
    for the same candle -- the orchestrator returns "duplicate_candle" and
    stops before any of that processing."""
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine

    fixed_now = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    candle_open_time = fixed_now - timedelta(minutes=5)
    row = _kline_row(candle_open_time)

    def fake_get(url, params):
        return {"result": {"list": [row]}}

    market_data_provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
    )
    # Force the provider to hand back the SAME candle twice, bypassing its
    # own dedup, to prove the DB-level guard in the orchestrator also holds.
    market_data_provider._last_processed_open_time = None

    settings = Settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s",
        # Fase 3.2: este teste exercita o pipeline OPERACIONAL "um candle
        # -> um sinal". Com o novo default (5 min) um único candle apenas
        # agrega; o timeframe estratégico de 1 minuto é a compatibilidade
        # explícita mantida pelo PO e preserva exatamente a intenção
        # original do teste.
        strategy_timeframe_minutes=1,
    )
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=True),
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )

    first = orch.tick()
    assert first["status"] not in ("duplicate_candle", "no_data", "retryable_error", "no_new_candle")

    market_data_provider._last_processed_open_time = None  # simulate the provider forgetting
    second = orch.tick()
    # Correction v1.4 #2: Orchestrator.tick() now re-syncs the provider's
    # cursor from the persisted candle at the START of every tick, so the
    # provider itself already refuses to re-deliver a candle that was
    # actually saved -- "no_new_candle" is the (stronger) outcome now,
    # instead of ever reaching the DB-level "duplicate_candle" guard. Either
    # way, no duplicate signal/AI/risk row may be created -- checked below.
    assert second["status"] in ("duplicate_candle", "no_new_candle")

    with session_scope(session_factory) as session:
        signals = repo.recent_signals(session, limit=100)
        ai_recs = repo.recent_ai_recommendations(session, limit=100)
        risk_evals = repo.recent_risk_evaluations(session, limit=100)
        assert len(signals) == 1
        assert len(ai_recs) == 1
        assert len(risk_evals) <= 1
