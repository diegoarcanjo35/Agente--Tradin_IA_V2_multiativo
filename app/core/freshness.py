"""Fase 3.3.1: FRESCOR do sinal -- a barreira que faltava.

Três conceitos que estavam colapsados num só e agora são separados
explicitamente, porque medem coisas diferentes:

1. `data_reception_recent`  -- o provider entregou ALGUMA linha há pouco
   tempo (`utcnow() - _last_received_at`). Mede saúde da CONEXÃO. Era
   chamado `data_fresh`, um nome que prometia frescor de dado e entregava
   recência de recepção: durante uma drenagem de backlog, candles de
   quatro horas atrás são "recebidos agora" e este check passa.

2. `market_data_temporally_current` -- o último candle FECHADO do símbolo
   tem timestamp próximo do presente. Mede se a série está atualizada.
   É o que o gate de ativação precisa saber.

3. `signal_is_fresh` -- o sinal de ENTRADA ainda está dentro da janela
   permitida depois que o bucket estratégico dele FECHOU. É o que este
   módulo calcula.

Por que medir a partir do FECHAMENTO do bucket, e não da abertura: um
sinal de 5 minutos nasce, por construção, ~5 minutos depois da abertura
do bucket (o bucket 10:00 só fecha às 10:05). Medir desde o `open_time`
contaria a própria duração do candle como atraso e recusaria todo sinal
legítimo.

    bucket_close_time    = source_candle_open_time + duração do timeframe
    signal_delay_seconds = now - bucket_close_time
    fresco  <=>  -tolerância_futuro <= delay <= máximo permitido

O limite é INCLUSIVO: um sinal exatamente no limite é ACEITO.

Esta barreira vale SOMENTE para abertura/aumento de exposição. Fechar,
reduzir, stop-loss, take-profit, liquidação de segurança, reconciliação e
kill-switch NUNCA são bloqueados por frescor -- uma saída de proteção
precisa continuar possível justamente quando o dado está atrasado.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Tolerância para timestamp levemente no futuro: pequenas diferenças de
# relógio entre a corretora e a máquina local são normais e não podem
# recusar um sinal legítimo. Acima disso, o dado é tratado como
# impossível e a entrada é recusada.
FUTURE_TOLERANCE_SECONDS = 2.0


# ---------------------------------------------------------------------
# SEMÂNTICA TEMPORAL DA FONTE DE MERCADO
# ---------------------------------------------------------------------
# A barreira de frescor e o gate de atualidade comparam o timestamp do
# candle com o RELÓGIO DE PAREDE. Isso só faz sentido quando a fonte de
# mercado entrega dado cujo timestamp ACOMPANHA O PRESENTE.
#
# O critério é a semântica temporal da FONTE DE MERCADO (modo/provider) --
# NUNCA o tipo de ExecutionEngine. Usar `PaperLocalExecutionEngine` não
# desativa proteção nenhuma: PAPER_LIVE usa exatamente esse motor e
# consome mercado público ATUAL, então continua integralmente protegido.
# Qualquer modo futuro com execução local e mercado atual entra em LIVE
# por padrão e nasce protegido.
MARKET_DATA_TIME_LIVE = "live"               # timestamps acompanham o presente
MARKET_DATA_TIME_HISTORICAL = "historical"   # série gravada; timestamps do passado

MARKET_DATA_TIME_SEMANTICS: dict[str, str] = {
    # Série gravada em arquivo: a fixture é de 2024-01-01. Comparar com o
    # relógio de parede mediria a idade do ARQUIVO, não risco.
    "REPLAY": MARKET_DATA_TIME_HISTORICAL,
    "PAPER_LOCAL": MARKET_DATA_TIME_HISTORICAL,
    # Mercado público real, timestamps no presente. PAPER_LIVE executa
    # localmente (PaperLocalExecutionEngine) e MESMO ASSIM é protegido --
    # o que decide é a fonte de mercado, não o motor de execução.
    "PAPER_LIVE": MARKET_DATA_TIME_LIVE,
    "BYBIT_DEMO": MARKET_DATA_TIME_LIVE,
}


def market_data_time_semantics(mode_value: str) -> str:
    """Semântica temporal da fonte de mercado do modo.

    Default seguro: um modo DESCONHECIDO é tratado como LIVE, ou seja,
    protegido. Esquecer de mapear um modo novo nunca pode desligar a
    barreira silenciosamente."""
    return MARKET_DATA_TIME_SEMANTICS.get(mode_value, MARKET_DATA_TIME_LIVE)


def market_data_is_historical(mode_value: str) -> bool:
    return market_data_time_semantics(mode_value) == MARKET_DATA_TIME_HISTORICAL


@dataclass(frozen=True)
class FreshnessPolicy:
    """A política em vigor. `strategy_timeframe_minutes` vem da MESMA
    configuração que dirige o agregador, nunca de uma segunda cópia."""

    max_signal_delay_after_close_seconds: float
    strategy_timeframe_minutes: int
    future_tolerance_seconds: float = FUTURE_TOLERANCE_SECONDS

    def to_dict(self) -> dict:
        return {
            "max_signal_delay_after_close_seconds": self.max_signal_delay_after_close_seconds,
            "strategy_timeframe_minutes": self.strategy_timeframe_minutes,
            "future_tolerance_seconds": self.future_tolerance_seconds,
        }


@dataclass(frozen=True)
class FreshnessResult:
    fresh: bool
    reason: str
    detail: dict


def _as_utc(value: datetime) -> datetime:
    """Normaliza para UTC-aware. Um datetime ingênuo é interpretado como
    UTC (é o que a camada ORM já garante -- ver
    app/persistence/temporal.py::UTCDateTime); horário LOCAL nunca é
    usado em lugar nenhum deste cálculo."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def evaluate_signal_freshness(
    policy: FreshnessPolicy,
    source_candle_open_time: datetime | None,
    now: datetime,
) -> FreshnessResult:
    """Decide se um sinal de ENTRADA ainda pode ser executado.

    Recusa explícita (nunca aprovação por omissão) quando:
    - `source_candle_open_time` está ausente -- sinal legado ou de origem
      desconhecida não pode provar seu próprio frescor;
    - o timeframe estratégico é inválido;
    - o timestamp está no futuro além da tolerância;
    - o atraso ultrapassa o máximo configurado.
    """
    detail: dict = {
        "max_signal_delay_after_close_seconds": policy.max_signal_delay_after_close_seconds,
        "strategy_timeframe_minutes": policy.strategy_timeframe_minutes,
        "future_tolerance_seconds": policy.future_tolerance_seconds,
        "evaluated_at": None,
        "source_candle_open_time": None,
        "bucket_close_time": None,
        "signal_delay_seconds": None,
        "passed": False,
    }

    if now is None:
        return FreshnessResult(
            False, "Frescor não pôde ser avaliado: instante de avaliação ausente.", detail
        )
    now_utc = _as_utc(now)
    detail["evaluated_at"] = now_utc.isoformat()

    if source_candle_open_time is None:
        detail["failure"] = "missing_source_candle_open_time"
        return FreshnessResult(
            False,
            "Sinal sem `source_candle_open_time`: a idade do dado que originou a decisão não "
            "pode ser provada, então a abertura de posição é recusada.",
            detail,
        )

    minutes = policy.strategy_timeframe_minutes
    if not isinstance(minutes, int) or minutes <= 0:
        detail["failure"] = "invalid_strategy_timeframe"
        return FreshnessResult(
            False,
            f"Timeframe estratégico inválido ({minutes!r}): o fechamento do bucket não pode ser "
            "calculado, então a abertura de posição é recusada.",
            detail,
        )

    source_utc = _as_utc(source_candle_open_time)
    bucket_close = source_utc + timedelta(minutes=minutes)
    delay = (now_utc - bucket_close).total_seconds()

    detail["source_candle_open_time"] = source_utc.isoformat()
    detail["bucket_close_time"] = bucket_close.isoformat()
    detail["signal_delay_seconds"] = delay

    if delay < -policy.future_tolerance_seconds:
        detail["failure"] = "future_timestamp"
        return FreshnessResult(
            False,
            f"Sinal com fechamento de bucket no FUTURO ({bucket_close.isoformat()}, "
            f"{-delay:.1f}s à frente do instante da avaliação, tolerância "
            f"{policy.future_tolerance_seconds:.1f}s). Abertura de posição recusada.",
            detail,
        )

    if delay > policy.max_signal_delay_after_close_seconds:
        detail["failure"] = "signal_too_old"
        return FreshnessResult(
            False,
            f"Sinal defasado: o bucket estratégico fechou em {bucket_close.isoformat()}, "
            f"há {delay:.1f}s -- acima do máximo de "
            f"{policy.max_signal_delay_after_close_seconds:.0f}s. A decisão foi tomada sobre "
            "preço que já passou; abertura de posição recusada. (Fechar, reduzir, stop-loss e "
            "take-profit NÃO são afetados por esta barreira.)",
            detail,
        )

    detail["passed"] = True
    return FreshnessResult(
        True,
        f"Sinal fresco: bucket fechou há {delay:.1f}s (limite "
        f"{policy.max_signal_delay_after_close_seconds:.0f}s).",
        detail,
    )


def market_data_temporally_current(
    last_candle_open_time: datetime | None,
    market_data_timeframe_minutes: int,
    now: datetime,
    max_delay_seconds: float,
) -> dict:
    """Conceito (2): o último candle FECHADO está próximo do presente?

    Diferente de `data_reception_recent`, que só prova que o provider
    entregou alguma coisa há pouco. Usado pelo gate de ativação, onde a
    pergunta certa é "a série está no presente", não "a conexão está
    viva".

    O atraso é medido a partir do FECHAMENTO do candle operacional, pela
    mesma razão do frescor do sinal: um candle de 1 minuto só existe
    depois que o minuto termina."""
    result = {
        "last_candle_open_time": None,
        "last_candle_close_time": None,
        "evaluated_at": None,
        "delay_seconds": None,
        "max_delay_seconds": max_delay_seconds,
        "current": False,
    }
    if now is None:
        return result
    now_utc = _as_utc(now)
    result["evaluated_at"] = now_utc.isoformat()

    if last_candle_open_time is None:
        result["failure"] = "no_candle"
        return result

    open_utc = _as_utc(last_candle_open_time)
    close = open_utc + timedelta(minutes=market_data_timeframe_minutes)
    delay = (now_utc - close).total_seconds()
    result["last_candle_open_time"] = open_utc.isoformat()
    result["last_candle_close_time"] = close.isoformat()
    result["delay_seconds"] = delay

    if delay < -FUTURE_TOLERANCE_SECONDS:
        result["failure"] = "future_candle"
        return result
    if delay > max_delay_seconds:
        result["failure"] = "market_data_stale"
        return result

    result["current"] = True
    return result


# Motivo registrado em `checks["signal_freshness"]` quando a barreira não
# se aplica -- explícito, nunca uma aprovação silenciosa.
HISTORICAL_DATA_REASON = "market_data_is_historical_in_this_mode"


def freshness_policy_for_market_data(
    mode_value: str, max_signal_delay_after_close_seconds: float, strategy_timeframe_minutes: int,
) -> FreshnessPolicy | None:
    """A política de frescor, decidida pela SEMÂNTICA TEMPORAL DA FONTE DE
    MERCADO -- nunca pelo tipo de ExecutionEngine.

    - fonte LIVE (mercado atual): barreira OBRIGATÓRIA. Vale para
      PAPER_LIVE mesmo executando localmente com
      `PaperLocalExecutionEngine`, para BYBIT_DEMO, e para qualquer modo
      futuro não mapeado (default seguro).
    - fonte HISTÓRICA (série gravada): a política não se aplica, e a
      ausência fica registrada com motivo explícito. Medir uma fixture de
      2024 contra o relógio de parede recusaria 100% dos sinais para
      sempre, sem medir risco algum.

    Alternativa considerada e descartada: usar o tempo do próprio candle
    como "agora" na fonte histórica. Ficaria elegante, mas o mesmo
    mecanismo aplicado a uma fonte LIVE tornaria o atraso SEMPRE zero
    durante uma drenagem de backlog -- justamente o cenário que esta
    barreira existe para impedir."""
    if market_data_is_historical(mode_value):
        return None
    return FreshnessPolicy(
        max_signal_delay_after_close_seconds=max_signal_delay_after_close_seconds,
        strategy_timeframe_minutes=strategy_timeframe_minutes,
    )
