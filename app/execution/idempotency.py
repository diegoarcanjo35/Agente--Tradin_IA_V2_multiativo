from __future__ import annotations

import hashlib

from app.risk.engine import ApprovedOrder


def make_idempotency_key(
    order: ApprovedOrder,
    timestamp_bucket: str,
    *,
    strategy_timeframe: str | None = None,
    strategy_version: str | None = None,
    session_uid: str | None = None,
) -> str:
    """Deterministic key from signal id + symbol + side + qty + a coarse time
    bucket (minute-resolution string) so retries of the *same* decision
    collapse to one order, while a genuinely new signal gets a new key.

    Fase 3.2 (item 11 da decisão do PO): a identidade passa a incluir também
    o TIMEFRAME ESTRATÉGICO, a versão da estratégia e a identidade da sessão
    operacional. Sem isso, duas configurações diferentes decidindo no mesmo
    minuto de relógio poderiam produzir a mesma chave -- e, com o
    `timestamp_bucket` passando a ser o `open_time` do BUCKET estratégico
    (que se repete por 5 ou 15 minutos), a colisão deixaria de ser teórica.

    Compatibilidade preservada: quando nenhum dos três extras é informado, a
    chave produzida é BYTE A BYTE a mesma de antes desta fase -- ordens já
    persistidas continuam sendo encontradas por
    `repo.find_order_by_idempotency_key`, e nenhuma delas é reprocessada ou
    duplicada por causa da mudança."""
    raw = f"{order.signal_id}:{order.symbol}:{order.side}:{order.qty:.8f}:{timestamp_bucket}"
    if strategy_timeframe is not None or strategy_version is not None or session_uid is not None:
        raw += f":{strategy_timeframe or ''}:{strategy_version or ''}:{session_uid or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
