"""Deterministic, auditable strategy: SIMPLE moving-average (SMA) crossover,
gated by an ATR-based volatility band. No ML, no black box -- every signal
carries the exact numbers that produced it.

Correção da Fase 3.2 (documentação vs. código): este docstring afirmava
existir também um "trend filter" separado. Não existe e nunca existiu -- o
único gatilho de direção é o próprio cruzamento das duas SMAs (ver
`on_candle`), e o único filtro é a faixa de ATR%. As médias são SIMPLES
(`_sma`), nunca exponenciais: nada aqui calcula EMA, e nada no painel pode
chamá-las de EMA.

Fase 3.2: o engine continua sem NENHUMA dependência de timeframe -- ele
apenas consome `CandleTick`. Quem decide se o candle recebido é de 1, 5 ou
15 minutos é `app/strategy/aggregator.py`, fora daqui. `StrategyConfig.
timeframe_minutes` existe só para que o valor entre no fingerprint da
sessão junto com o resto da configuração de estratégia realmente entregue
aos engines.

This strategy makes no promise of profitability; see docs/OPERACAO_DEMO.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.clock import utcnow
from app.market_data.base import CandleTick
from app.strategy.schemas import Signal


@dataclass
class StrategyConfig:
    fast_period: int = 9
    slow_period: int = 21
    atr_period: int = 14
    min_atr_pct_of_price: float = 0.0005  # below this, market judged too quiet to trade
    max_atr_pct_of_price: float = 0.05  # above this, market judged too volatile to trade
    stop_loss_atr_multiple: float = 2.0
    take_profit_atr_multiple: float = 3.0
    # Fase 3.2: o timeframe ESTRATÉGICO em minutos (1/5/15). Não é usado em
    # nenhum cálculo deste módulo -- vive aqui para viajar junto com o
    # resto da configuração real da estratégia para dentro do fingerprint
    # da sessão (`app/sessions.py::_config_fingerprint` usa
    # `dataclasses.asdict(strategy_config)`).
    timeframe_minutes: int = 5


class StrategyEngine:
    def __init__(self, symbol: str, config: StrategyConfig | None = None):
        self.symbol = symbol
        self.config = config or StrategyConfig()
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._prev_fast_above_slow: bool | None = None

    # --- aquecimento e hidratação (Fase 3.2) ------------------------------

    def warmup_required(self) -> int:
        """Quantos candles ESTRATÉGICOS completos são necessários para que
        o engine possa emitir um sinal não-HOLD. Derivado exclusivamente da
        configuração -- nunca um número fixo espalhado pelo código.

        `slow_period` closes para a SMA lenta; `atr_period + 1` closes para
        o ATR (`_atr` olha `self._closes[i-1]`); e mais UM candle para que
        `_prev_fast_above_slow` já esteja definido -- sem ele o primeiro
        candle após o aquecimento nunca poderia detectar um cruzamento
        (`on_candle` exige `_prev_fast_above_slow is not None`)."""
        cfg = self.config
        return max(cfg.slow_period, cfg.fast_period, cfg.atr_period + 1) + 1

    def warmup_state(self) -> dict:
        """Estado de aquecimento para painel/API -- sempre com o que é
        exigido e o que já se tem, nunca só um booleano opaco."""
        required = self.warmup_required()
        have = len(self._closes)
        return {"required": required, "have": have, "ready": have >= required}

    def current_indicators(self) -> dict:
        """Os indicadores no estado ATUAL do engine -- exatamente os
        números que a próxima decisão usaria. Existe para que o painel
        exiba o valor AUTORITATIVO (o do engine) em vez de recalcular a
        matemática numa segunda implementação que pudesse divergir.

        `None` em qualquer campo significa "ainda não calculável" (histórico
        insuficiente) -- nunca um zero fabricado. `atr_pct_of_price` é o
        ATR dividido pelo último fechamento; `atr_per_unit_usd` é a
        diferença de preço POR UNIDADE, a mesma unidade usada pelo gate de
        custo antes de multiplicar por `qty`."""
        cfg = self.config
        fast = self._sma(self._closes, cfg.fast_period)
        slow = self._sma(self._closes, cfg.slow_period)
        atr = self._atr()
        last_close = self._closes[-1] if self._closes else None
        atr_pct = (
            atr / last_close if atr is not None and last_close else None
        )
        return {
            "fast_period": cfg.fast_period,
            "slow_period": cfg.slow_period,
            "atr_period": cfg.atr_period,
            "fast_sma": fast,
            "slow_sma": slow,
            "atr_per_unit_usd": atr,
            "atr_pct_of_price": atr_pct,
            "last_close": last_close,
            "prev_fast_above_slow": self._prev_fast_above_slow,
        }

    def hydrate(self, candles: list[CandleTick]) -> None:
        """Fase 3.2 (item 5 da decisão do PO): replay SILENCIOSO do estado
        estratégico a partir de candles já persistidos, usado no
        boot/restart.

        Restaura integralmente o que uma execução contínua teria: as séries
        de closes/highs/lows (base da SMA rápida, da SMA lenta e do ATR) e
        o `_prev_fast_above_slow` -- sem o qual o primeiro candle após um
        restart nunca detectaria um cruzamento, e o sistema silenciosamente
        perderia entradas.

        Silenciosa por construção: NÃO retorna sinal, não persiste nada,
        não cria ordem, não incrementa contador operacional, não chama
        risco. É o mesmo cálculo de estado que `on_candle` faz, sem
        nenhum dos efeitos.

        Idempotente em relação ao histórico: chamar com o mesmo conjunto de
        candles a partir de um engine novo produz exatamente o mesmo
        estado. Buracos históricos permanecem buracos -- este método
        consome apenas o que lhe for entregue e nunca fabrica um candle
        ausente."""
        cfg = self.config
        for candle in candles:
            self._closes.append(candle.close)
            self._highs.append(candle.high)
            self._lows.append(candle.low)
            fast = self._sma(self._closes, cfg.fast_period)
            slow = self._sma(self._closes, cfg.slow_period)
            # Exatamente a mesma regra aplicada em TODOS os caminhos de
            # `on_candle` (aquecimento, filtros de ATR e caminho normal):
            # indefinido enquanto qualquer uma das médias não existir.
            self._prev_fast_above_slow = (
                None if fast is None or slow is None else fast > slow
            )

    def _sma(self, values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    def _atr(self) -> float | None:
        period = self.config.atr_period
        if len(self._closes) < period + 1:
            return None
        true_ranges = []
        for i in range(-period, 0):
            high = self._highs[i]
            low = self._lows[i]
            prev_close = self._closes[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        return sum(true_ranges) / period

    def on_candle(self, candle: CandleTick) -> Signal:
        self._closes.append(candle.close)
        self._highs.append(candle.high)
        self._lows.append(candle.low)

        cfg = self.config
        fast = self._sma(self._closes, cfg.fast_period)
        slow = self._sma(self._closes, cfg.slow_period)
        atr = self._atr()

        params = {
            "fast_period": cfg.fast_period,
            "slow_period": cfg.slow_period,
            "atr_period": cfg.atr_period,
            "fast_sma": fast,
            "slow_sma": slow,
            "atr": atr,
        }

        if fast is None or slow is None or atr is None:
            self._prev_fast_above_slow = None if fast is None or slow is None else fast > slow
            return Signal(
                symbol=self.symbol, direction="HOLD",
                justification="Histórico insuficiente para calcular os indicadores ainda.",
                created_at=utcnow(), observed_price=candle.close,
                source_candle_open_time=candle.open_time, atr=atr or 0.0,
                stop_loss=None, take_profit=None, params=params,
            )

        atr_pct = atr / candle.close if candle.close else 0.0
        if atr_pct < cfg.min_atr_pct_of_price:
            self._prev_fast_above_slow = fast > slow
            return Signal(
                symbol=self.symbol, direction="HOLD",
                justification=(
                    f"ATR% {atr_pct:.5f} abaixo do filtro mínimo de volatilidade "
                    f"({cfg.min_atr_pct_of_price}); mercado considerado parado demais."
                ),
                created_at=utcnow(), observed_price=candle.close,
                source_candle_open_time=candle.open_time, atr=atr,
                stop_loss=None, take_profit=None, params=params,
            )
        if atr_pct > cfg.max_atr_pct_of_price:
            self._prev_fast_above_slow = fast > slow
            return Signal(
                symbol=self.symbol, direction="HOLD",
                justification=(
                    f"ATR% {atr_pct:.5f} acima do filtro máximo de volatilidade "
                    f"({cfg.max_atr_pct_of_price}); mercado considerado volátil demais."
                ),
                created_at=utcnow(), observed_price=candle.close,
                source_candle_open_time=candle.open_time, atr=atr,
                stop_loss=None, take_profit=None, params=params,
            )

        fast_above_slow = fast > slow
        direction = "HOLD"
        justification = f"Sem cruzamento: média rápida={fast:.2f} média lenta={slow:.2f}."
        stop_loss = None
        take_profit = None

        if self._prev_fast_above_slow is not None and fast_above_slow != self._prev_fast_above_slow:
            if fast_above_slow:
                direction = "BUY"
                justification = (
                    f"Cruzamento de alta: média rápida({cfg.fast_period})={fast:.2f} cruzou "
                    f"acima da média lenta({cfg.slow_period})={slow:.2f}; filtro de tendência e "
                    f"filtro de volatilidade ATR (ATR%={atr_pct:.5f}) aprovados."
                )
                stop_loss = candle.close - cfg.stop_loss_atr_multiple * atr
                take_profit = candle.close + cfg.take_profit_atr_multiple * atr
            else:
                direction = "SELL"
                justification = (
                    f"Cruzamento de baixa: média rápida({cfg.fast_period})={fast:.2f} cruzou "
                    f"abaixo da média lenta({cfg.slow_period})={slow:.2f}; filtro de tendência e "
                    f"filtro de volatilidade ATR (ATR%={atr_pct:.5f}) aprovados."
                )
                stop_loss = candle.close + cfg.stop_loss_atr_multiple * atr
                take_profit = candle.close - cfg.take_profit_atr_multiple * atr

        self._prev_fast_above_slow = fast_above_slow

        return Signal(
            symbol=self.symbol, direction=direction, justification=justification,
            created_at=utcnow(), observed_price=candle.close,
                source_candle_open_time=candle.open_time, atr=atr,
            stop_loss=stop_loss, take_profit=take_profit, params=params,
        )
