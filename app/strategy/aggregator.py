"""Fase 3.2: agregação determinística de candles OPERACIONAIS de 1 minuto
em candles ESTRATÉGICOS de 5 ou 15 minutos (1 é passthrough exato, mantido
como compatibilidade explícita com o comportamento anterior à fase).

Princípios inegociáveis (decisões do PO):

- O candle de 1 minuto continua sendo a fonte primária: nada aqui impede,
  atrasa ou substitui a persistência/exibição dele. Este módulo é
  puramente derivado e não faz I/O nenhum.
- Um bucket fecha IMEDIATAMENTE assim que todos os N slots esperados
  chegam (item 4 da decisão do PO) -- nunca espera o primeiro candle do
  período seguinte, o que criaria um atraso operacional artificial de um
  minuto inteiro.
- Um bucket ao qual falte qualquer slot é finalizado como INCOMPLETO na
  transição para o bucket seguinte, exibido como tal, e NUNCA entregue à
  estratégia. OHLCV ausente jamais é fabricado.
- Nenhum bucket é finalizado duas vezes; um candle atrasado nunca reabre
  um bucket já finalizado; um candle duplicado nunca altera a contagem de
  slots.
- Os buckets são alinhados por UTC (00:00, 00:05, 00:10, ... para 5m).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.timeframe import canonical_timeframe, minutes_to_canonical
from app.market_data.base import CandleTick


@dataclass(frozen=True)
class AggregatedCandle:
    """Um candle estratégico. `complete=True` é a ÚNICA forma que pode
    alimentar o `StrategyEngine`; `partial=True` existe apenas para
    visualização honesta no painel (um bucket em formação, ou finalizado
    com slots faltando), sempre carregando quantos slots eram esperados,
    quantos chegaram e quais faltaram."""

    symbol: str
    timeframe: str  # canônico ("5m"/"15m"/"1m")
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool
    partial: bool
    expected_slots: int
    received_slots: int
    missing_slots: tuple[datetime, ...] = ()
    source: str = "aggregated"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "complete": self.complete,
            "partial": self.partial,
            "expected_slots": self.expected_slots,
            "received_slots": self.received_slots,
            "missing_slots": [t.isoformat() for t in self.missing_slots],
        }


@dataclass
class AggregationStats:
    """Contadores auditáveis, sempre derivados de eventos que realmente
    aconteceram -- nunca reconstruídos por consulta posterior."""

    complete_buckets: int = 0
    incomplete_buckets: int = 0
    duplicate_candles: int = 0
    late_candles_discarded: int = 0

    def to_dict(self) -> dict:
        return {
            "complete_buckets": self.complete_buckets,
            "incomplete_buckets": self.incomplete_buckets,
            "duplicate_candles": self.duplicate_candles,
            "late_candles_discarded": self.late_candles_discarded,
        }


def bucket_start(open_time: datetime, minutes: int) -> datetime:
    """Início do bucket UTC ao qual `open_time` pertence. Alinhado sempre
    ao relógio UTC absoluto (nunca ao primeiro candle recebido), para que
    duas execuções distintas -- e dois símbolos distintos -- produzam
    exatamente as mesmas fronteiras."""
    utc = open_time.astimezone(timezone.utc)
    floored_minute = (utc.minute // minutes) * minutes
    return utc.replace(minute=floored_minute, second=0, microsecond=0)


class CandleAggregator:
    """Agregador de UM símbolo. Cada símbolo tem o seu (isolamento real
    multiativo -- ver `app/api/main.py::build_orchestrator`); nenhum
    estado é compartilhado entre instâncias."""

    def __init__(self, symbol: str, timeframe_minutes: int, operational_minutes: int = 1):
        self.symbol = symbol
        self.timeframe_minutes = timeframe_minutes
        self.operational_minutes = operational_minutes
        self.timeframe = minutes_to_canonical(timeframe_minutes)
        self.expected_slots = timeframe_minutes // operational_minutes
        self.stats = AggregationStats()
        self._bucket_start: datetime | None = None
        self._slots: dict[datetime, CandleTick] = {}
        self._last_finalized_start: datetime | None = None

    # --- consulta ---------------------------------------------------------

    @property
    def has_forming_bucket(self) -> bool:
        return self._bucket_start is not None and bool(self._slots)

    def forming(self) -> AggregatedCandle | None:
        """O bucket em formação, para o painel. SEMPRE `partial=True` e
        `complete=False` -- nunca pode ser confundido com um candle
        fechado, nem usado pela estratégia."""
        if not self.has_forming_bucket:
            return None
        return self._build(self._bucket_start, self._slots, forced_partial=True)

    # --- alimentação ------------------------------------------------------

    def push(self, candle: CandleTick) -> AggregatedCandle | None:
        """Recebe um candle operacional FECHADO e devolve um candle
        estratégico FINALIZADO quando este push o finaliza (completo ou
        incompleto -- o chamador decide o que fazer olhando `.complete`),
        ou `None` quando nada foi finalizado.

        No máximo um bucket é finalizado por push, por construção: um
        candle ou completa o bucket corrente, ou abre um novo (finalizando
        o anterior como incompleto). Com `timeframe_minutes == 1` cada
        candle completa o seu próprio bucket imediatamente -- passthrough
        exato."""
        start = bucket_start(candle.open_time, self.timeframe_minutes)
        # O slot é identificado pelo instante ALINHADO do candle
        # operacional, nunca pelo `open_time` cru: um candle com segundos/
        # microssegundos não nulos pertence ao mesmo slot do minuto
        # correspondente, e usar a chave crua faria o bucket parecer
        # eternamente incompleto (defeito real encontrado ao rodar as
        # suítes de BYBIT_DEMO desta fase).
        slot = bucket_start(candle.open_time, self.operational_minutes)

        # Candle atrasado/fora de ordem cujo bucket já foi finalizado:
        # tratamento EXPLÍCITO -- descartado e contado, nunca reabrindo um
        # bucket fechado nem reordenando silenciosamente a série.
        if self._last_finalized_start is not None and start <= self._last_finalized_start:
            self.stats.late_candles_discarded += 1
            return None

        finalized: AggregatedCandle | None = None

        if self._bucket_start is not None and start != self._bucket_start:
            if start < self._bucket_start:
                # Bucket anterior ao corrente e ainda não finalizado: só
                # acontece com dados genuinamente fora de ordem. Também
                # descartado explicitamente (aceitá-lo mudaria um bucket
                # que a estratégia já pode ter visto pela metade).
                self.stats.late_candles_discarded += 1
                return None
            # Transição de bucket: o anterior fecha AGORA com o que tiver.
            finalized = self._finalize()

        if self._bucket_start is None:
            self._bucket_start = start
            self._slots = {}

        if slot in self._slots:
            # Duplicata: nunca altera a contagem de slots nem o OHLCV.
            self.stats.duplicate_candles += 1
            return finalized

        self._slots[slot] = candle

        # Fechamento imediato (decisão do PO, item 4): assim que os N slots
        # esperados estão presentes o bucket está completo -- não há nada a
        # esperar do minuto seguinte.
        if len(self._slots) >= self.expected_slots:
            immediate = self._finalize()
            # Um push nunca finaliza dois buckets (ver docstring), então
            # `finalized` é necessariamente None aqui.
            return immediate if finalized is None else finalized

        return finalized

    # --- interno ----------------------------------------------------------

    def _finalize(self) -> AggregatedCandle | None:
        if self._bucket_start is None or not self._slots:
            self._bucket_start = None
            self._slots = {}
            return None
        result = self._build(self._bucket_start, self._slots)
        self._last_finalized_start = self._bucket_start
        self._bucket_start = None
        self._slots = {}
        if result.complete:
            self.stats.complete_buckets += 1
        else:
            self.stats.incomplete_buckets += 1
        return result

    def _expected_slot_times(self, start: datetime) -> list[datetime]:
        step = timedelta(minutes=self.operational_minutes)
        return [start + step * i for i in range(self.expected_slots)]

    def _build(
        self, start: datetime, slots: dict[datetime, CandleTick], forced_partial: bool = False,
    ) -> AggregatedCandle:
        ordered = [slots[k] for k in sorted(slots)]
        expected_times = self._expected_slot_times(start)
        missing = tuple(t for t in expected_times if t not in slots)
        complete = not missing and not forced_partial
        return AggregatedCandle(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=start,
            close_time=start + timedelta(minutes=self.timeframe_minutes),
            open=ordered[0].open,
            high=max(c.high for c in ordered),
            low=min(c.low for c in ordered),
            close=ordered[-1].close,
            volume=sum(c.volume for c in ordered),
            complete=complete,
            partial=not complete,
            expected_slots=self.expected_slots,
            received_slots=len(ordered),
            missing_slots=missing,
        )

    # --- hidratação -------------------------------------------------------

    def hydrate(self, candles: list[CandleTick]) -> list[AggregatedCandle]:
        """Reconstrói o estado do agregador a partir de candles de 1
        minuto JÁ persistidos (usado no boot/restart -- ver
        `app/orchestrator.py::hydrate_strategy_state`). Devolve, em ordem
        cronológica, os buckets finalizados durante a reconstrução; o
        bucket parcial que sobrar fica em formação, exatamente como
        estaria numa execução contínua.

        Silenciosa por construção: não persiste nada, não gera sinal, não
        toca em contadores operacionais -- os únicos contadores que mexe
        são os do próprio agregador (`self.stats`), que descrevem a série
        reconstruída e não eventos de negociação. Buracos históricos
        permanecem buracos: um bucket ao qual falte candle é reconstruído
        como incompleto, nunca preenchido."""
        finalized: list[AggregatedCandle] = []
        for candle in candles:
            result = self.push(candle)
            if result is not None:
                finalized.append(result)
        return finalized


def to_candle_tick(aggregated: AggregatedCandle) -> CandleTick:
    """Adapta um candle estratégico completo para o formato que o
    `StrategyEngine` já consome, sem que o engine precise conhecer
    agregação nenhuma (ele nunca dependeu de timeframe -- ver
    `app/strategy/engine.py`)."""
    return CandleTick(
        symbol=aggregated.symbol,
        timeframe=canonical_timeframe(aggregated.timeframe),
        open_time=aggregated.open_time,
        open=aggregated.open,
        high=aggregated.high,
        low=aggregated.low,
        close=aggregated.close,
        volume=aggregated.volume,
        source="aggregated",
        received_at=aggregated.close_time,
    )
