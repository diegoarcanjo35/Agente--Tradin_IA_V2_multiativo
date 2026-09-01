"""Fase 3.2 (decisão Q4 do PO): representação CANÔNICA única do timeframe.

Antes desta fase a mesma grandeza era escrita no banco com duas strings
diferentes: o provider Bybit recebia `"1"` (o formato de intervalo da
própria corretora, ver `app/api/main.py::_build_market_data_provider`) e
o provider de REPLAY usava `"1m"`, enquanto o painel consultava sempre o
literal `"1m"`. Em BYBIT_DEMO isso fazia `/api/chart-data` devolver uma
lista vazia de candles -- defeito real, encontrado na auditoria da Fase
3.2, invisível em REPLAY justamente porque lá as duas pontas coincidiam.

Decisão do PO, implementada aqui:

- a representação canônica -- persistida e exposta na API -- é `"1m"`;
- `"1"` é aceito APENAS como alias legado de leitura/entrada;
- candles novos são sempre gravados canônicos;
- cursor, backlog, gráfico e qualquer consulta passam por esta função
  única, nunca por um literal solto;
- consultas continuam encontrando candles legados gravados como `"1"`;
- se existirem `"1"` e `"1m"` para o mesmo símbolo/open_time, a
  deduplicação é LÓGICA (na consulta), preferindo o registro canônico --
  o banco legado nunca é reescrito e nenhuma migration é criada só por
  isso.
"""
from __future__ import annotations

# Minutos suportados nesta fase (Q5 do PO): 1 (compatibilidade explícita),
# 5 (default) e 15.
SUPPORTED_TIMEFRAME_MINUTES = (1, 5, 15)

# O timeframe OPERACIONAL de coleta permanece fixo em 1 minuto nesta fase.
# A estratégia pode rodar em 1/5/15 (agregando), mas a fonte primária de
# mercado nunca deixa de ser o candle de 1 minuto.
OPERATIONAL_TIMEFRAME_MINUTES = 1
CANONICAL_OPERATIONAL_TIMEFRAME = "1m"

# Aliases aceitos na LEITURA/ENTRADA, por minuto. O primeiro elemento de
# cada tupla é sempre a forma canônica -- a preferida numa colisão.
_ALIASES_BY_MINUTES: dict[int, tuple[str, ...]] = {
    1: ("1m", "1"),
    5: ("5m", "5"),
    15: ("15m", "15"),
}

_MINUTES_BY_ALIAS: dict[str, int] = {
    alias: minutes
    for minutes, aliases in _ALIASES_BY_MINUTES.items()
    for alias in aliases
}


def canonical_timeframe(raw: str | int | None) -> str:
    """Normaliza qualquer alias aceito para a forma canônica (`"1m"`,
    `"5m"`, `"15m"`). Nunca adivinha: um valor desconhecido levanta
    `ValueError` em vez de ser silenciosamente tratado como 1 minuto (o
    erro que produziria decisões sobre a série errada)."""
    if raw is None:
        raise ValueError("Timeframe não pode ser None.")
    key = str(raw).strip().lower()
    minutes = _MINUTES_BY_ALIAS.get(key)
    if minutes is None:
        raise ValueError(
            f"Timeframe desconhecido: {raw!r}. Suportados: "
            f"{sorted(_MINUTES_BY_ALIAS)}."
        )
    return _ALIASES_BY_MINUTES[minutes][0]


def timeframe_aliases(raw: str | int) -> tuple[str, ...]:
    """Todas as grafias que devem ser aceitas ao CONSULTAR o banco para
    este timeframe -- a canônica primeiro. Usado por
    `repo.recent_candles`/`repo.get_last_candle_open_time` para que
    candles legados gravados como `"1"` continuem sendo encontrados sem
    reescrever o banco."""
    minutes = _MINUTES_BY_ALIAS[canonical_timeframe(raw)]
    return _ALIASES_BY_MINUTES[minutes]


def timeframe_minutes(raw: str | int) -> int:
    """Quantos minutos este timeframe representa."""
    return _MINUTES_BY_ALIAS[canonical_timeframe(raw)]


def minutes_to_canonical(minutes: int) -> str:
    """`5` -> `"5m"`. Levanta `ValueError` para um valor não suportado --
    a validação de `Settings.strategy_timeframe_minutes` usa exatamente
    esta lista, então as duas nunca divergem."""
    if minutes not in _ALIASES_BY_MINUTES:
        raise ValueError(
            f"Timeframe de {minutes} minuto(s) não é suportado. "
            f"Suportados: {list(SUPPORTED_TIMEFRAME_MINUTES)}."
        )
    return _ALIASES_BY_MINUTES[minutes][0]


def bybit_interval(raw: str | int) -> str:
    """A grafia que a API da Bybit espera no parâmetro `interval` (`"1"`,
    não `"1m"`). Existe para que o formato da corretora fique confinado à
    fronteira HTTP -- o que é persistido/exposto continua sempre
    canônico."""
    return str(timeframe_minutes(raw))
