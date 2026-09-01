"""Fase 3.2: gate de VIABILIDADE LÍQUIDA -- estimativa do custo de ida e
volta de uma entrada, em US$ financeiro, comparada com o movimento
esperado derivado do ATR do candle ESTRATÉGICO.

Regra de unidade (a mesma armadilha que a Fase 3.1.1 corrigiu no
slippage): `atr` é uma diferença de preço POR UNIDADE do ativo. Ele só
pode ser comparado com dinheiro depois de multiplicado por `qty`. Nenhuma
linha deste módulo soma US$/unidade com US$ total.

`minimum_cost_coverage_ratio = 3.0` é uma HIPÓTESE OPERACIONAL INICIAL,
configurável -- a recomendação é exigir margem confortavelmente superior
ao custo estimado. Não é um parâmetro comprovadamente otimizado, não foi
validado contra resultado histórico e não constitui promessa de
rentabilidade alguma.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Origem declarada da estimativa -- sempre exposta em `checks_json` e na
# API, para que ninguém confunda um número simulado com um número medido.
SOURCE_PAPER_CONFIG = "paper_config"
SOURCE_BYBIT_DEMO_ESTIMATE = "bybit_demo_estimate"


@dataclass(frozen=True)
class CostModel:
    """Os parâmetros de custo realmente em vigor para este processo.

    - `fee_rate`: fração (0.0006 = 0,06% do notional), por perna.
    - `slippage_bps`: bps adversos esperados por perna (5.0 = 0,05%).
    - `source`: `paper_config` (o simulador aplica exatamente estes
      números -- a estimativa é EXATA por construção) ou
      `bybit_demo_estimate` (valores configurados pelo operador; a
      corretora real pode cobrar outra coisa -- é estimativa declarada,
      nunca um valor consultado).
    """

    fee_rate: float
    slippage_bps: float
    source: str
    expected_move_atr_multiple: float
    minimum_cost_coverage_ratio: float

    @property
    def slippage_fraction(self) -> float:
        return self.slippage_bps / 10_000.0

    def to_dict(self) -> dict:
        return {
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
            "source": self.source,
            "expected_move_atr_multiple": self.expected_move_atr_multiple,
            "minimum_cost_coverage_ratio": self.minimum_cost_coverage_ratio,
        }


@dataclass(frozen=True)
class CostGateResult:
    approved: bool
    reason: str
    detail: dict


# Piso estritamente positivo para o preço projetado de saída num SELL cujo
# movimento esperado seria maior que o próprio preço. Nunca é usado para
# APROVAR (ver `_reject` do caso `projected_exit_price_invalid`) -- existe
# só para que o número exposto em `checks_json` continue sendo um preço
# válido em vez de zero/negativo.
_MIN_VALID_PRICE_FRACTION = 1e-8


def evaluate_cost_gate(
    model: CostModel, side: str, qty: float, entry_reference_price: float, atr: float,
) -> CostGateResult:
    """Aprova a entrada somente se

        expected_move_usd >= round_trip_cost_usd * minimum_cost_coverage_ratio

    Entrada e saída são estimadas SEPARADAMENTE, cada uma com seu próprio
    preço projetado de fill e seu próprio notional -- nunca reaproveitando
    o notional da entrada para a saída quando os preços projetados diferem
    (foi exatamente essa simplificação que o PO proibiu).
    """
    detail: dict = {
        "side": side,
        "qty": qty,
        "atr_per_unit_usd": atr,
        "entry_reference_price": entry_reference_price,
        "fee_rate": model.fee_rate,
        "slippage_bps": model.slippage_bps,
        "estimate_source": model.source,
        "expected_move_atr_multiple": model.expected_move_atr_multiple,
        "required_coverage_ratio": model.minimum_cost_coverage_ratio,
    }

    def reject(code: str, reason: str) -> CostGateResult:
        detail["failure"] = code
        detail["cost_coverage_ok"] = False
        return CostGateResult(approved=False, reason=reason, detail=detail)

    # --- validação honesta das entradas -----------------------------------
    for name, value in (
        ("qty", qty), ("entry_reference_price", entry_reference_price), ("atr", atr),
    ):
        if value is None or not math.isfinite(value):
            return reject(
                "non_finite_input",
                f"Gate de custo não pôde ser avaliado: {name} não é um número finito "
                f"({value!r}). Entrada recusada -- nunca aprovada por omissão.",
            )
    if qty <= 0:
        return reject(
            "qty_not_positive",
            f"Gate de custo não pôde ser avaliado: quantidade {qty!r} não é positiva.",
        )
    if entry_reference_price <= 0:
        return reject(
            "reference_price_not_positive",
            f"Gate de custo não pôde ser avaliado: preço de referência {entry_reference_price!r} "
            "não é positivo.",
        )
    if atr <= 0:
        return reject(
            "atr_unavailable",
            "Gate de custo não pôde ser avaliado: ATR indisponível ou não positivo -- a "
            "cobertura de custo não pôde ser provada, então a entrada é recusada.",
        )
    if side not in ("BUY", "SELL"):
        return reject(
            "side_invalid",
            f"Gate de custo não pôde ser avaliado: lado {side!r} não é BUY nem SELL.",
        )

    slip = model.slippage_fraction

    # --- movimento esperado (US$ por unidade -> US$ total) ----------------
    expected_move_per_unit = atr * model.expected_move_atr_multiple
    expected_move_usd = expected_move_per_unit * qty
    detail["expected_move_per_unit_usd"] = expected_move_per_unit
    detail["expected_move_usd"] = expected_move_usd

    # --- preço projetado de SAÍDA ----------------------------------------
    if side == "BUY":
        raw_projected_exit = entry_reference_price + expected_move_per_unit
    else:
        raw_projected_exit = entry_reference_price - expected_move_per_unit
    price_floor = entry_reference_price * _MIN_VALID_PRICE_FRACTION
    projected_exit_price = max(raw_projected_exit, price_floor)
    clamped = projected_exit_price != raw_projected_exit
    detail["projected_exit_price"] = projected_exit_price
    detail["projected_exit_price_clamped"] = clamped

    if clamped:
        # Um SELL cujo movimento esperado é maior que o próprio preço não
        # descreve nenhuma saída realizável. Recusar explicitamente é mais
        # honesto do que aprovar em cima de um preço artificialmente
        # elevado até o piso.
        return reject(
            "projected_exit_price_invalid",
            f"Gate de custo não pôde ser avaliado: o preço projetado de saída "
            f"({raw_projected_exit:.8f}) não é um preço válido -- o movimento esperado "
            f"({expected_move_per_unit:.8f} por unidade) é maior ou igual ao próprio preço de "
            f"referência ({entry_reference_price:.8f}). Entrada recusada.",
        )

    # --- perna de ENTRADA -------------------------------------------------
    # Slippage ADVERSO: comprando, paga-se MAIS; vendendo, recebe-se MENOS.
    if side == "BUY":
        entry_fill_price = entry_reference_price * (1.0 + slip)
    else:
        entry_fill_price = entry_reference_price * (1.0 - slip)
    entry_notional_usd = entry_fill_price * qty
    entry_fee_usd = entry_notional_usd * model.fee_rate
    entry_slippage_usd = abs(entry_fill_price - entry_reference_price) * qty

    # --- perna de SAÍDA (lado oposto, notional PRÓPRIO) -------------------
    exit_side = "SELL" if side == "BUY" else "BUY"
    if exit_side == "BUY":
        exit_fill_price = projected_exit_price * (1.0 + slip)
    else:
        exit_fill_price = projected_exit_price * (1.0 - slip)
    exit_notional_usd = exit_fill_price * qty
    exit_fee_usd = exit_notional_usd * model.fee_rate
    exit_slippage_usd = abs(exit_fill_price - projected_exit_price) * qty

    round_trip_cost_usd = entry_fee_usd + exit_fee_usd + entry_slippage_usd + exit_slippage_usd

    detail.update({
        "exit_side": exit_side,
        "entry_fill_price_estimated": entry_fill_price,
        "entry_notional_usd": entry_notional_usd,
        "entry_fee_usd": entry_fee_usd,
        "entry_slippage_usd": entry_slippage_usd,
        "exit_fill_price_estimated": exit_fill_price,
        "exit_notional_usd": exit_notional_usd,
        "exit_fee_usd": exit_fee_usd,
        "exit_slippage_usd": exit_slippage_usd,
        "round_trip_cost_usd": round_trip_cost_usd,
    })

    # Cobertura efetivamente alcançada. Custo zero configurado (um
    # simulador sem custo é configuração legítima) torna a razão
    # indefinida, não infinita nem zero: `None`, nunca um número inventado.
    achieved_ratio = (
        expected_move_usd / round_trip_cost_usd if round_trip_cost_usd > 0 else None
    )
    detail["achieved_coverage_ratio"] = achieved_ratio

    required_usd = round_trip_cost_usd * model.minimum_cost_coverage_ratio
    detail["required_expected_move_usd"] = required_usd

    if expected_move_usd >= required_usd:
        detail["cost_coverage_ok"] = True
        ratio_desc = (
            f"{achieved_ratio:.2f}x" if achieved_ratio is not None else "indefinida (custo zero)"
        )
        return CostGateResult(
            approved=True,
            reason=(
                f"Cobertura de custo aprovada: movimento esperado US$ {expected_move_usd:.4f} "
                f"cobre {ratio_desc} o custo estimado de ida e volta US$ "
                f"{round_trip_cost_usd:.4f} (mínimo exigido: "
                f"{model.minimum_cost_coverage_ratio:.2f}x)."
            ),
            detail=detail,
        )

    ratio_desc = f"{achieved_ratio:.2f}x" if achieved_ratio is not None else "indefinida"
    return reject(
        "insufficient_cost_coverage",
        (
            f"Movimento esperado US$ {expected_move_usd:.4f} não cobre "
            f"{model.minimum_cost_coverage_ratio:.2f}x o custo estimado de ida e volta "
            f"US$ {round_trip_cost_usd:.4f} (exigido US$ {required_usd:.4f}; alcançado "
            f"{ratio_desc}). Estimativa de origem {model.source!r}."
        ),
    )
