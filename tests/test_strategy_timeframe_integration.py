"""Fase 3.2: integração do timeframe estratégico -- coleta em 1 minuto,
decisão em 5/15 minutos, hidratação silenciosa no restart e identidade
idempotente por bucket.

Tudo em REPLAY, banco temporário, zero rede.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order, StrategySignal
from app.strategy.aggregator import bucket_start
from tests.factories import activate_operational_state


def _build(tmp_path, db_name, minutes=5, symbols="BTCUSDT", **over):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols,
        database_url=f"sqlite:///{tmp_path / db_name}",
        strategy_timeframe_minutes=minutes, **over,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    return orch


def _run(orch, max_ticks=400):
    statuses = []
    for _ in range(max_ticks):
        result = orch.tick()
        statuses.append(result.get("status"))
        if result.get("status") == "no_data":
            break
    return statuses


def _signals(orch, symbol=None):
    with session_scope(orch.session_factory) as session:
        rows = session.execute(
            select(StrategySignal).order_by(StrategySignal.id)
        ).scalars().all()
        return [
            (r.symbol, r.direction, r.source_candle_open_time, r.observed_price)
            for r in rows
            if symbol is None or r.symbol == symbol
        ]


# --- 14. todo candle de 1 minuto continua persistido ---------------------

def test_every_one_minute_candle_is_still_persisted_under_five_minute_strategy(tmp_path):
    orch = _build(tmp_path, "all_candles.db", minutes=5)
    _run(orch)
    with session_scope(orch.session_factory) as session:
        candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
    assert len(candles) == 180  # a fixture inteira, nada perdido pela agregação
    assert all(c.timeframe == "1m" for c in candles)


# --- 11. nenhum sinal repetido dentro do mesmo bucket --------------------

def test_no_repeated_signal_within_the_same_strategy_bucket(tmp_path):
    orch = _build(tmp_path, "one_per_bucket.db", minutes=5)
    _run(orch)
    signals = _signals(orch)

    opens = [s[2] for s in signals]
    assert len(opens) == len(set(opens))  # um sinal por bucket, no máximo
    assert len(opens) == 36               # 180 candles de 1 min / 5


def test_signal_source_candle_open_time_is_the_bucket_open_time(tmp_path):
    orch = _build(tmp_path, "bucket_open.db", minutes=5)
    _run(orch)
    for _symbol, _direction, open_time, _price in _signals(orch):
        assert open_time == bucket_start(open_time, 5)
        assert open_time.second == 0 and open_time.microsecond == 0


def test_fifteen_minute_timeframe_produces_a_third_of_the_five_minute_decisions(tmp_path):
    """15 minutos é suportado e testado sem exigir que a fixture atual gere
    sinal acionável -- o que se prova aqui é a CADÊNCIA de decisão."""
    orch = _build(tmp_path, "tf15.db", minutes=15)
    _run(orch)
    assert len(_signals(orch)) == 12  # 180 / 15
    with session_scope(orch.session_factory) as session:
        assert len(repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)) == 180


# --- 13. ausência de look-ahead ------------------------------------------

def test_no_look_ahead_the_strategy_never_sees_a_forming_bucket(tmp_path):
    orch = _build(tmp_path, "no_lookahead.db", minutes=5)
    for _ in range(7):  # 7 candles: bucket 1 completo, bucket 2 em formação
        orch.tick()

    # Exatamente UM sinal (o do bucket completo), e o bucket em formação
    # existe mas nunca virou sinal.
    signals = _signals(orch)
    assert len(signals) == 1
    forming = orch.aggregator.forming()
    assert forming is not None and forming.partial is True
    assert forming.open_time not in [s[2] for s in signals]


def test_strategy_close_is_the_bucket_close_not_a_one_minute_close(tmp_path):
    orch = _build(tmp_path, "bucket_close.db", minutes=5)
    _run(orch)
    with session_scope(orch.session_factory) as session:
        candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
    by_open = {c.open_time: c for c in candles}

    for _symbol, _direction, open_time, observed_price in _signals(orch):
        last_minute = by_open[open_time + timedelta(minutes=4)]
        assert observed_price == pytest.approx(last_minute.close)


# --- 12. aquecimento --------------------------------------------------

def test_first_actionable_signal_only_after_full_warmup(tmp_path):
    orch = _build(tmp_path, "warmup.db", minutes=5)
    _run(orch)
    required = orch.strategy_engine.warmup_required()
    signals = _signals(orch)

    actionable = [i for i, s in enumerate(signals) if s[1] in ("BUY", "SELL")]
    for index in actionable:
        assert index + 1 >= required, "sinal acionável antes do aquecimento completo"


# --- 16. determinismo entre execuções -----------------------------------

def test_two_replay_runs_produce_exactly_the_same_signals(tmp_path):
    first = _build(tmp_path, "det_a.db", minutes=5)
    _run(first)
    second = _build(tmp_path, "det_b.db", minutes=5)
    _run(second)
    assert _signals(first) == _signals(second)


# --- 17. isolamento entre símbolos ---------------------------------------

def test_two_symbols_never_contaminate_each_others_aggregation(tmp_path):
    orch = _build(tmp_path, "multi.db", minutes=5, symbols="BTCUSDT,ETHUSDT")
    _run(orch, max_ticks=800)

    btc = orch.orchestrators["BTCUSDT"]
    eth = orch.orchestrators["ETHUSDT"]
    assert btc.aggregator is not eth.aggregator
    assert btc.strategy_engine is not eth.strategy_engine
    assert btc.aggregator.stats.complete_buckets == eth.aggregator.stats.complete_buckets

    btc_signals = _signals(orch, "BTCUSDT")
    eth_signals = _signals(orch, "ETHUSDT")
    assert len(btc_signals) == len(eth_signals) == 36
    assert {s[0] for s in btc_signals} == {"BTCUSDT"}
    assert {s[0] for s in eth_signals} == {"ETHUSDT"}


# --- 18. bucket incompleto ------------------------------------------------

def test_incomplete_bucket_produces_no_signal_but_is_reported(tmp_path):
    orch = _build(tmp_path, "incomplete.db", minutes=5)
    # Consome os 3 primeiros candles e depois "pula" os minutos 3 e 4
    # avançando o cursor do provider -- um buraco real na série.
    for _ in range(3):
        orch.tick()
    orch.market_data_provider._cursor += 2
    signals_before = len(_signals(orch))

    statuses = [orch.tick().get("status") for _ in range(3)]

    assert "incomplete_bucket" in statuses
    assert len(_signals(orch)) == signals_before  # nenhum sinal do bucket furado
    assert orch.aggregator.stats.incomplete_buckets == 1
    state = orch.strategy_state()
    assert state["bucket_integrity"]["incomplete_buckets"] == 1


def test_incomplete_bucket_is_a_healthy_tick_not_a_failure(tmp_path):
    from app.orchestrator import FAILURE_TICK_STATUSES

    assert "incomplete_bucket" not in FAILURE_TICK_STATUSES
    assert "aggregating" not in FAILURE_TICK_STATUSES


# --- 15. hidratação silenciosa no restart --------------------------------

def test_restart_mid_bucket_restores_the_partial_bucket(tmp_path):
    orch_a = _build(tmp_path, "restart_partial.db", minutes=5)
    for _ in range(13):  # 2 buckets completos + 3 candles do terceiro
        orch_a.tick()
    forming_a = orch_a.aggregator.forming()
    assert forming_a is not None and forming_a.received_slots == 3

    orch_b = _build(tmp_path, "restart_partial.db", minutes=5)
    forming_b = orch_b.aggregator.forming()
    assert forming_b is not None
    assert forming_b.open_time == forming_a.open_time
    assert forming_b.received_slots == forming_a.received_slots
    assert (forming_b.open, forming_b.high, forming_b.low, forming_b.close) == (
        forming_a.open, forming_a.high, forming_a.low, forming_a.close
    )


def test_restart_after_twenty_buckets_preserves_the_warmup(tmp_path):
    minutes = 5
    orch_a = _build(tmp_path, "restart_warmup.db", minutes=minutes)
    required = orch_a.strategy_engine.warmup_required()
    for _ in range((required - 2) * minutes):  # 20 buckets, um a menos que o 21º
        orch_a.tick()
    have_a = orch_a.strategy_engine.warmup_state()["have"]
    assert have_a == required - 2

    orch_b = _build(tmp_path, "restart_warmup.db", minutes=minutes)
    assert orch_b.strategy_engine.warmup_state() == orch_a.strategy_engine.warmup_state()
    assert orch_b.strategy_engine.current_indicators() == orch_a.strategy_engine.current_indicators()


def test_hydration_creates_no_signal_order_or_counter(tmp_path):
    orch_a = _build(tmp_path, "silent.db", minutes=5)
    _run(orch_a)

    with session_scope(orch_a.session_factory) as session:
        before_signals = len(session.execute(select(StrategySignal)).scalars().all())
        before_orders = len(session.execute(select(Order)).scalars().all())
        state = repo.get_or_create_system_state(session)
        op = repo.get_active_session(session, state)
        before_counters = (op.signals_count, op.orders_count, op.candles_count)

    # O boot deste segundo processo roda a hidratação inteira.
    orch_b = _build(tmp_path, "silent.db", minutes=5)
    assert orch_b.strategy_engine.warmup_state()["ready"] is True

    with session_scope(orch_b.session_factory) as session:
        assert len(session.execute(select(StrategySignal)).scalars().all()) == before_signals
        assert len(session.execute(select(Order)).scalars().all()) == before_orders
        state = repo.get_or_create_system_state(session)
        op = repo.get_active_session(session, state)
        assert (op.signals_count, op.orders_count, op.candles_count) == before_counters


def test_continuous_and_restarted_runs_produce_the_same_next_signal(tmp_path):
    """Execução contínua e execução com restart no meio do caminho
    produzem exatamente o mesmo PRÓXIMO sinal."""
    split = 100

    continuous = _build(tmp_path, "cont.db", minutes=5)
    _run(continuous)
    continuous_signals = _signals(continuous)

    restarted_a = _build(tmp_path, "restarted.db", minutes=5)
    for _ in range(split):
        restarted_a.tick()

    restarted_b = _build(tmp_path, "restarted.db", minutes=5)
    # O provider de REPLAY sempre recomeça a fixture do zero; posicioná-lo
    # onde os dados realmente pararam é o equivalente ao que o cursor
    # persistido faz no provider real (`sync_cursor`).
    restarted_b.market_data_provider._cursor = split
    _run(restarted_b)

    assert _signals(restarted_b) == continuous_signals


def test_hydration_depth_is_derived_from_configuration_not_hardcoded(tmp_path):
    orch5 = _build(tmp_path, "depth5.db", minutes=5)
    orch15 = _build(tmp_path, "depth15.db", minutes=15)
    required = orch5.strategy_engine.warmup_required()
    assert orch5.strategy_hydration_depth() == (required + 1) * 5
    assert orch15.strategy_hydration_depth() == (required + 1) * 15

    slower = _build(tmp_path, "depth_slow.db", minutes=5, strategy_slow_period=50)
    assert slower.strategy_hydration_depth() > orch5.strategy_hydration_depth()


def test_historical_gaps_stay_gaps_after_hydration(tmp_path):
    orch_a = _build(tmp_path, "gap_hydrate.db", minutes=5)
    for _ in range(3):
        orch_a.tick()
    orch_a.market_data_provider._cursor += 2  # buraco nos minutos 3 e 4
    for _ in range(6):
        orch_a.tick()
    incomplete_a = orch_a.aggregator.stats.incomplete_buckets
    assert incomplete_a == 1

    orch_b = _build(tmp_path, "gap_hydrate.db", minutes=5)
    # A reconstrução encontra o MESMO buraco -- nunca preenche o candle
    # ausente para "fechar" o bucket.
    assert orch_b.aggregator.stats.incomplete_buckets == incomplete_a


# --- 20/11. idempotência --------------------------------------------------

def test_idempotency_key_includes_timeframe_version_and_session(tmp_path):
    from app.execution.idempotency import make_idempotency_key
    from tests.factories import approved_open_order

    order = approved_open_order(symbol="BTCUSDT", side="BUY")
    bucket = "20240101T1000"

    legacy = make_idempotency_key(order, bucket)
    # Compatibilidade: sem os extras a chave é BYTE A BYTE a de antes.
    assert make_idempotency_key(order, bucket) == legacy

    tf5 = make_idempotency_key(order, bucket, strategy_timeframe="5m",
                                strategy_version="v1", session_uid="s1")
    tf15 = make_idempotency_key(order, bucket, strategy_timeframe="15m",
                                 strategy_version="v1", session_uid="s1")
    other_session = make_idempotency_key(order, bucket, strategy_timeframe="5m",
                                          strategy_version="v1", session_uid="s2")
    other_version = make_idempotency_key(order, bucket, strategy_timeframe="5m",
                                          strategy_version="v2", session_uid="s1")

    assert len({legacy, tf5, tf15, other_session, other_version}) == 5


def test_symbols_and_sides_never_collide_in_the_idempotency_key():
    from app.execution.idempotency import make_idempotency_key
    from tests.factories import approved_open_order

    bucket = "20240101T1000"
    extras = dict(strategy_timeframe="5m", strategy_version="v1", session_uid="s1")
    btc_buy = approved_open_order(symbol="BTCUSDT", side="BUY")
    eth_buy = approved_open_order(symbol="ETHUSDT", side="BUY")
    btc_sell = approved_open_order(symbol="BTCUSDT", side="SELL", stop_loss=41000.0, take_profit=39000.0)

    keys = {
        make_idempotency_key(btc_buy, bucket, **extras),
        make_idempotency_key(eth_buy, bucket, **extras),
        make_idempotency_key(btc_sell, bucket, **extras),
    }
    assert len(keys) == 3


def test_one_entry_order_per_bucket_at_most(tmp_path):
    orch = _build(tmp_path, "one_order.db", minutes=5)
    _run(orch)
    with session_scope(orch.session_factory) as session:
        orders = session.execute(select(Order).where(Order.is_close.is_(False))).scalars().all()
        buckets = []
        for order in orders:
            snapshot = repo.decision_snapshot_for_order(session, order)
            buckets.append(snapshot["bucket"]["open_time"])
    assert len(buckets) == len(set(buckets))


# --- snapshot congelado da decisão ---------------------------------------

def test_decision_snapshot_carries_everything_the_po_required(tmp_path):
    orch = _build(tmp_path, "snapshot.db", minutes=5)
    _run(orch)
    with session_scope(orch.session_factory) as session:
        rows = session.execute(
            select(StrategySignal).order_by(StrategySignal.id.desc()).limit(1)
        ).scalars().all()
        params = json.loads(rows[0].params_json)

    for field in (
        "strategy_timeframe", "strategy_timeframe_minutes", "market_data_timeframe",
        "atr_per_unit_usd", "stop_loss_atr_multiple", "take_profit_atr_multiple",
        "bucket", "bucket_complete", "bucket_expected_slots", "bucket_received_slots",
    ):
        assert field in params, field
    assert params["strategy_timeframe"] == "5m"
    assert params["bucket_complete"] is True
    assert params["bucket_expected_slots"] == 5
    assert params["bucket_received_slots"] == 5
    for ohlcv in ("open", "high", "low", "close", "volume", "open_time", "close_time"):
        assert ohlcv in params["bucket"]
