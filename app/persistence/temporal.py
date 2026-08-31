"""Correção de Datetimes v1.0 -- ponto único de normalização de timestamps.

Todo timestamp de domínio é UTC-aware. O driver `sqlite3` usado por este
projeto devolve `DateTime(timezone=True)` como *naive* independentemente do
que foi gravado (comportamento documentado do driver, não deste código) --
sem essa normalização, qualquer comparação/subtração Python entre um valor
recém-lido do banco e um `datetime` aware (ex.: `utcnow()`) levanta
`TypeError: can't compare offset-naive and offset-aware datetimes`. Foi
exatamente essa classe de defeito que causou o incidente operacional
reproduzido em `tests/test_datetime_utc_normalization.py`.

`UTCDateTime` aplica `reattach_utc()` na fronteira real (o próprio
carregamento/gravação da coluna via SQLAlchemy) para toda coluna de
timestamp do schema -- não depende de nenhum call-site lembrar de chamar
nada manualmente.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


def reattach_utc(value: datetime | None) -> datetime | None:
    """Normaliza para um instante UTC-aware. Idempotente.

    - `None` permanece `None`.
    - Um valor *naive* é interpretado como já sendo UTC (este código nunca
      grava outra coisa) e recebe o `tzinfo` de volta sem alterar o
      horário de parede.
    - Um valor *aware* com qualquer offset é convertido ao instante UTC
      correspondente via `astimezone` -- nunca apenas rotulado de novo,
      o que mudaria o significado temporal (item 5 da correção)."""
    if value is None:
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Substituto direto de `DateTime(timezone=True)`: mesmo tipo de coluna
    no SQLite (nenhuma migration necessária), mas garante na leitura e na
    gravação que todo valor Python é UTC-aware."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return reattach_utc(value)

    def process_result_value(self, value, dialect):
        return reattach_utc(value)
