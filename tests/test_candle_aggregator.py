"""Fase 3.2: o agregador determinístico de candles de 1 minuto em candles
estratégicos de 5/15 minutos. Testes de unidade puros -- nenhum banco,
nenhum orquestrador, nenhuma rede.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.base import CandleTick
from app.strategy.aggregator import CandleAggregator, bucket_start, to_candle_tick


def _c(minute_offset: int, o: float, h: float, low: float, c: float, v: float = 1.0,
       base: datetime | None = None, seconds: int = 0) -> CandleTick:
    start = base or datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    return CandleTick(
        symbol="BTCUSDT", timeframe="1m",
        open_time=start + timedelta(minutes=minute_offset, seconds=seconds),
        open=o, high=h, low=low, close=c, volume=v, source="replay",
        received_at=start + timedelta(minutes=minute_offset),
    )


def _agg(minutes: int = 5) -> CandleAggregator:
    return CandleAggregator("BTCUSDT", timeframe_minutes=minutes)


# --- 1/2. OHLCV e fronteiras UTC -----------------------------------------

def test_ohlcv_matches_hand_computed_values():
    agg = _agg(5)
    candles = [
        _c(0, o=100, h=105, low=99, c=101, v=10),
        _c(1, o=101, h=108, low=100, c=104, v=20),
        _c(2, o=104, h=106, low=95, c=97, v=30),
        _c(3, o=97, h=99, low=96, c=98, v=40),
        _c(4, o=98, h=103, low=97, c=102, v=50),
    ]
    result = None
    for candle in candles:
        result = agg.push(candle) or result

    assert result is not None
    assert result.complete is True
    assert result.open == 100      # abertura do PRIMEIRO candle
    assert result.high == 108      # maior máxima
    assert result.low == 95        # menor mínima
    assert result.close == 102     # fechamento do ÚLTIMO
    assert result.volume == 150    # soma
    assert result.received_slots == 5
    assert result.missing_slots == ()


def test_bucket_open_and_close_times_are_exact_utc():
    for minutes in (5, 15):
        agg = _agg(minutes)
        base = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        result = None
        for i in range(minutes):
            result = agg.push(_c(i, 100, 101, 99, 100, base=base)) or result
        assert result.open_time == base
        assert result.close_time == base + timedelta(minutes=minutes)
        assert result.timeframe == f"{minutes}m"


def test_bucket_start_aligns_to_absolute_utc_clock_not_first_candle():
    # Um candle às 10:07 pertence ao bucket 10:05 (5m) e 10:00 (15m) --
    # nunca abre um bucket "10:07".
    t = datetime(2024, 1, 1, 10, 7, 31, tzinfo=timezone.utc)
    assert bucket_start(t, 5) == datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)
    assert bucket_start(t, 15) == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)


# --- 3. fechamento IMEDIATO, sem esperar o período seguinte ---------------

def test_bucket_closes_immediately_on_the_last_expected_slot():
    """Item 4 da decisão do PO: o bucket 10:00-10:04 fecha ao receber o
    candle de 10:04 -- NUNCA espera o candle de 10:05, o que criaria um
    atraso operacional artificial de um minuto inteiro."""
    agg = _agg(5)
    for i in range(4):
        assert agg.push(_c(i, 100, 101, 99, 100)) is None  # ainda em formação

    closed = agg.push(_c(4, 100, 101, 99, 100))
    assert closed is not None
    assert closed.complete is True
    assert closed.open_time == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    # E nada resta em formação: o bucket foi encerrado, não continua aberto.
    assert agg.forming() is None


def test_a_finalized_bucket_is_never_finalized_twice():
    agg = _agg(5)
    for i in range(5):
        agg.push(_c(i, 100, 101, 99, 100))
    assert agg.stats.complete_buckets == 1

    # O candle do período seguinte abre um bucket NOVO; não refecha o antigo.
    assert agg.push(_c(5, 100, 101, 99, 100)) is None
    assert agg.stats.complete_buckets == 1
    assert agg.stats.incomplete_buckets == 0


# --- 4. determinismo ------------------------------------------------------

def test_same_input_produces_identical_aggregates():
    candles = [_c(i, 100 + i, 105 + i, 95 + i, 101 + i, v=i + 1) for i in range(15)]
    first = [b.to_dict() for b in _agg(5).hydrate(candles)]
    second = [b.to_dict() for b in _agg(5).hydrate(candles)]
    assert first == second
    assert len(first) == 3


# --- 5. bucket incompleto: marcado, nunca fabricado ----------------------

def test_missing_candle_produces_an_incomplete_bucket_never_fabricated():
    agg = _agg(5)
    for i in (0, 1, 3, 4):  # falta o minuto 2
        agg.push(_c(i, 100, 101, 99, 100))
    # Transição para o bucket seguinte finaliza o anterior como incompleto.
    closed = agg.push(_c(5, 100, 101, 99, 100))

    assert closed is not None
    assert closed.complete is False
    assert closed.partial is True
    assert closed.expected_slots == 5
    assert closed.received_slots == 4
    assert closed.missing_slots == (datetime(2024, 1, 1, 10, 2, tzinfo=timezone.utc),)
    assert agg.stats.incomplete_buckets == 1
    assert agg.stats.complete_buckets == 0


# --- 6. duplicata ---------------------------------------------------------

def test_duplicate_candle_never_changes_slot_count_or_ohlcv():
    agg = _agg(5)
    agg.push(_c(0, 100, 105, 99, 101, v=10))
    agg.push(_c(0, 999, 999, 999, 999, v=999))  # duplicata do MESMO slot

    forming = agg.forming()
    assert agg.stats.duplicate_candles == 1
    assert forming.received_slots == 1
    assert forming.high == 105  # o valor absurdo da duplicata nunca entrou
    assert forming.volume == 10


def test_candle_with_seconds_belongs_to_the_same_minute_slot():
    """Um candle carimbado 10:00:31 é o slot das 10:00 -- a chave do slot é
    o minuto ALINHADO, nunca o `open_time` cru (defeito real encontrado ao
    rodar as suítes de BYBIT_DEMO nesta fase: com a chave crua o bucket
    parecia eternamente incompleto)."""
    agg = _agg(5)
    agg.push(_c(0, 100, 101, 99, 100, seconds=31))
    for i in range(1, 5):
        agg.push(_c(i, 100, 101, 99, 100, seconds=17))
    assert agg.stats.complete_buckets == 1


# --- 7. fora de ordem -----------------------------------------------------

def test_late_candle_never_reopens_a_finalized_bucket():
    agg = _agg(5)
    for i in range(5):
        agg.push(_c(i, 100, 101, 99, 100))
    assert agg.stats.complete_buckets == 1

    late = agg.push(_c(2, 500, 500, 500, 500))  # pertence ao bucket já fechado
    assert late is None
    assert agg.stats.late_candles_discarded == 1
    assert agg.stats.complete_buckets == 1


def test_out_of_order_candle_before_the_current_bucket_is_discarded_explicitly():
    agg = _agg(5)
    agg.push(_c(5, 100, 101, 99, 100))   # abre o bucket 10:05
    result = agg.push(_c(1, 100, 101, 99, 100))  # pertence ao bucket 10:00
    assert result is None
    assert agg.stats.late_candles_discarded == 1
    assert agg.forming().open_time == datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)


# --- 8. hora, dia e meia-noite UTC ---------------------------------------

@pytest.mark.parametrize(
    "base",
    [
        datetime(2024, 1, 1, 10, 55, tzinfo=timezone.utc),   # atravessa a hora
        datetime(2024, 1, 1, 23, 55, tzinfo=timezone.utc),   # atravessa o dia/meia-noite
        datetime(2024, 12, 31, 23, 45, tzinfo=timezone.utc),  # atravessa o ano
    ],
)
def test_buckets_cross_hour_day_and_midnight_boundaries_correctly(base):
    agg = _agg(5)
    closed = []
    for i in range(10):
        result = agg.push(_c(i, 100, 101, 99, 100, base=base))
        if result:
            closed.append(result)

    assert len(closed) == 2
    assert all(b.complete for b in closed)
    assert closed[0].open_time == base
    assert closed[1].open_time == base + timedelta(minutes=5)
    assert closed[1].close_time == base + timedelta(minutes=10)


# --- 9. fechamentos iguais ------------------------------------------------

def test_identical_closes_do_not_confuse_bucket_identity():
    agg = _agg(5)
    closed = []
    for i in range(10):
        result = agg.push(_c(i, 100, 100, 100, 100))  # todos idênticos
        if result:
            closed.append(result)
    assert len(closed) == 2
    assert closed[0].open_time != closed[1].open_time  # identidade é o tempo, não o preço


# --- 10. passthrough exato com timeframe 1 --------------------------------

def test_one_minute_timeframe_is_an_exact_passthrough():
    agg = _agg(1)
    for i in range(3):
        candle = _c(i, 100 + i, 105 + i, 95 + i, 101 + i, v=i + 1)
        result = agg.push(candle)
        assert result is not None and result.complete is True
        assert (result.open, result.high, result.low, result.close, result.volume) == (
            candle.open, candle.high, candle.low, candle.close, candle.volume
        )
        assert result.open_time == candle.open_time
    assert agg.stats.complete_buckets == 3
    assert agg.stats.incomplete_buckets == 0


def test_to_candle_tick_preserves_the_aggregate_exactly():
    agg = _agg(5)
    closed = None
    for i in range(5):
        closed = agg.push(_c(i, 100, 108, 95, 102, v=10)) or closed
    tick = to_candle_tick(closed)
    assert (tick.open, tick.high, tick.low, tick.close, tick.volume) == (100, 108, 95, 102, 50)
    assert tick.timeframe == "5m"
    assert tick.open_time == closed.open_time
    assert tick.source == "aggregated"


# --- forming(): sempre parcial, nunca confundível com fechado ------------

def test_forming_bucket_is_always_flagged_partial():
    agg = _agg(5)
    agg.push(_c(0, 100, 101, 99, 100))
    agg.push(_c(1, 100, 101, 99, 100))
    forming = agg.forming()
    assert forming.partial is True
    assert forming.complete is False
    assert forming.received_slots == 2
    assert forming.expected_slots == 5
    assert len(forming.missing_slots) == 3
