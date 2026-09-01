from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: str  # BUY | SELL | HOLD
    justification: str
    created_at: datetime
    observed_price: float
    atr: float
    stop_loss: float | None
    take_profit: float | None
    # Fase 3.1 (auditoria, gate bloqueante): identidade determinística do
    # candle que realmente gerou este sinal -- SEMPRE `candle.open_time`,
    # nunca derivado de preço (dois candles podem fechar no mesmo valor) nem
    # de `created_at` (o instante de gravação/persistência, que pode
    # divergir muito do candle em REPLAY/backlog). Ver
    # app/strategy/engine.py::StrategyEngine.on_candle.
    source_candle_open_time: datetime
    params: dict = field(default_factory=dict)
