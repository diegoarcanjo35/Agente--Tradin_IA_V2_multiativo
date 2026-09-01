"""Gerador determinístico das fixtures de REPLAY, uma por símbolo.

NÃO faz parte do runtime da aplicação -- rode manualmente se alguma
fixture precisar ser regenerada. Sem `app.core.clock.utcnow()`, sem
aleatoriedade: mesma entrada, mesma saída, byte a byte.

DADOS 100% SINTÉTICOS. Nenhuma cotação real, de nenhuma corretora, é
usada ou reproduzida aqui. As séries existem apenas para exercitar o
pipeline (cruzamento de médias, ATR, agregação, gate de custo) de forma
reproduzível -- nunca para sugerir comportamento real de mercado.

Fase 3.2 (ajuste multiativo): cada símbolo passa a ter a SUA série, em
escala e formato próprios. Antes, a validação multiativo reaproveitava
`replay_btcusdt.json` apenas trocando o rótulo do símbolo -- o que fazia
ETH aparecer cotado perto de US$ 40.000 e produzia sinais idênticos aos
do BTC exatamente nos mesmos horários, sem prova nenhuma de isolamento.

Os parâmetros de BTCUSDT são EXATAMENTE os originais, de modo que
`replay_btcusdt.json` é regenerado idêntico ao arquivo já versionado.
"""
import json
import math
from datetime import datetime, timedelta, timezone

START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
N = 180

# Cada símbolo tem escala de preço, formato de tendência, período e
# amplitude de oscilação próprios -- deliberadamente bem distintos, para
# que SMA, ATR, instantes de cruzamento e sinais NÃO possam coincidir por
# construção.
SYMBOLS = {
    "BTCUSDT": dict(
        filename="replay_btcusdt.json", base_price=40000.0,
        trend_up=15.0, turn=90, trend_down=20.0,
        wave=60.0, period=6.0, phase=0.0,
        volume_base=100.0, volume_step=5.0, wick=10.0, decimals=2,
    ),
    # ETH: escala ~18x menor, movimento ESPELHADO (cai primeiro, sobe
    # depois) e onda mais lenta -- os cruzamentos caem em instantes
    # diferentes dos do BTC.
    "ETHUSDT": dict(
        filename="replay_ethusdt.json", base_price=2200.0,
        trend_up=-1.1, turn=70, trend_down=-1.6,
        wave=9.0, period=9.0, phase=1.7,
        volume_base=820.0, volume_step=37.0, wick=1.2, decimals=2,
    ),
    # SOL: escala ~420x menor que a do BTC, tendência fraca e oscilação
    # rápida e dominante -- perfil de "lateral agitado", um terceiro
    # formato, diferente dos dois anteriores.
    "SOLUSDT": dict(
        filename="replay_solusdt.json", base_price=95.0,
        trend_up=0.06, turn=120, trend_down=0.11,
        wave=1.8, period=4.0, phase=0.6,
        volume_base=15000.0, volume_step=610.0, wick=0.15, decimals=4,
    ),
}


def build(cfg: dict) -> list[dict]:
    """Onda determinística: tendência (subida/queda a partir de `turn`)
    somada a uma senoide, de modo que uma estratégia de cruzamento de
    médias produza tanto COMPRA quanto VENDA."""
    candles = []
    price = cfg["base_price"]
    for i in range(N):
        if i < cfg["turn"]:
            trend = cfg["trend_up"] * i
        else:
            trend = cfg["trend_up"] * cfg["turn"] - cfg["trend_down"] * (i - cfg["turn"])
        wave = cfg["wave"] * math.sin(i / cfg["period"] + cfg["phase"])
        close = cfg["base_price"] + trend + wave
        open_ = price
        high = max(open_, close) + cfg["wick"]
        low = min(open_, close) - cfg["wick"]
        volume = cfg["volume_base"] + cfg["volume_step"] * (i % 10)
        open_time = START + timedelta(minutes=i)
        d = cfg["decimals"]
        candles.append(
            {
                "open_time": open_time.isoformat(),
                "open": round(open_, d),
                "high": round(high, d),
                "low": round(low, d),
                "close": round(close, d),
                "volume": round(volume, 2),
            }
        )
        price = close
    return candles


if __name__ == "__main__":
    for symbol, cfg in SYMBOLS.items():
        rows = build(cfg)
        with open(cfg["filename"], "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        print(f"{symbol}: {len(rows)} candles -> {cfg['filename']}")
