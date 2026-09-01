"""Fase 2, item 7.6; contrato definitivo da correção final da auditoria do
PO (Fase 3.1.1): o impacto financeiro do slippage é SEMPRE multiplicado
por `filled_qty` (nunca a diferença unitária de preço sozinha, que não é
dinheiro), adverso e melhoria de preço nunca se cancelam silenciosamente,
e o percentual é ponderado por notional -- nunca a média simples dos
percentuais individuais.
"""
from __future__ import annotations

from app.metrics.engine import UNAVAILABLE, OrderFillView, compute_cost_metrics


def test_empty_order_set_reports_zero_fees_and_unavailable_slippage():
    result = compute_cost_metrics([])
    assert result.fees_total == 0.0  # a genuine, correct total for nothing -- not fabricated
    assert result.adverse_slippage_cost_usd == UNAVAILABLE
    assert result.price_improvement_value_usd == UNAVAILABLE
    assert result.net_slippage_impact_usd == UNAVAILABLE
    assert result.weighted_slippage_pct == UNAVAILABLE
    assert result.adverse_slippage_pct == UNAVAILABLE
    assert result.reference_notional_total_usd == UNAVAILABLE
    assert result.priced_orders_count == 0


def test_orders_without_any_reference_price_report_slippage_unavailable():
    orders = [
        OrderFillView(side="BUY", reference_price=None, avg_fill_price=100.0, filled_qty=1.0, fees_total=0.05),
        OrderFillView(side="SELL", reference_price=None, avg_fill_price=101.0, filled_qty=1.0, fees_total=0.06),
    ]
    result = compute_cost_metrics(orders)
    assert result.fees_total == 0.11
    assert result.adverse_slippage_cost_usd == UNAVAILABLE
    assert result.unpriced_orders_count == 2


def test_buy_adverse_slippage_is_financial_impact_times_filled_qty():
    """PO exemplo: referência US$78.000, fill US$78.039,31, quantidade
    0,001 BTC -> custo financeiro real US$0,03931, NUNCA US$39,31 (a
    diferença unitária de preço não multiplicada por quantidade)."""
    orders = [OrderFillView(
        side="BUY", reference_price=78000.0, avg_fill_price=78039.31,
        filled_qty=0.001, fees_total=0.0,
    )]
    result = compute_cost_metrics(orders)
    assert round(result.adverse_slippage_cost_usd, 5) == 0.03931
    assert result.price_improvement_value_usd == 0.0
    assert round(result.net_slippage_impact_usd, 5) == 0.03931
    assert result.priced_orders_count == 1
    assert result.unpriced_orders_count == 0


def test_sell_adverse_slippage_is_positive_when_receiving_less_than_reference():
    orders = [OrderFillView(
        side="SELL", reference_price=100.0, avg_fill_price=99.5, filled_qty=2.0, fees_total=0.01,
    )]
    result = compute_cost_metrics(orders)
    # unit slippage 0.5 * qty 2.0 = US$ 1.00 de custo adverso real.
    assert result.adverse_slippage_cost_usd == 1.0
    assert result.price_improvement_value_usd == 0.0


def test_favorable_slippage_is_tracked_as_improvement_never_negative_cost():
    """BUY filling BELOW reference, or SELL filling ABOVE reference, is
    favorável -- reportado em `price_improvement_value_usd` (sempre >= 0),
    NUNCA como um valor negativo dentro do custo adverso (que fica 0)."""
    orders = [OrderFillView(
        side="BUY", reference_price=100.0, avg_fill_price=99.0, filled_qty=3.0, fees_total=0.0,
    )]
    result = compute_cost_metrics(orders)
    assert result.adverse_slippage_cost_usd == 0.0
    assert result.price_improvement_value_usd == 3.0  # (100-99) * 3
    assert result.net_slippage_impact_usd == -3.0  # líquido negativo = melhoria líquida


def test_favorable_and_adverse_orders_never_silently_net_each_other_out():
    """Uma ordem adversa de US$10 e uma melhoria de US$10 devem aparecer
    SEPARADAMENTE (US$10 adverso, US$10 de melhoria) -- nunca colapsadas
    num total de US$0 que esconde ambos os eventos."""
    orders = [
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=110.0, filled_qty=1.0, fees_total=0.0),
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=90.0, filled_qty=1.0, fees_total=0.0),
    ]
    result = compute_cost_metrics(orders)
    assert result.adverse_slippage_cost_usd == 10.0
    assert result.price_improvement_value_usd == 10.0
    assert result.net_slippage_impact_usd == 0.0  # líquido correto, mas os brutos continuam visíveis


def test_mixed_priced_and_unpriced_orders_only_use_the_priced_ones():
    orders = [
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=101.0, filled_qty=1.0, fees_total=0.0),
        OrderFillView(side="BUY", reference_price=None, avg_fill_price=200.0, filled_qty=5.0, fees_total=0.0),
    ]
    result = compute_cost_metrics(orders)
    assert result.adverse_slippage_cost_usd == 1.0
    assert result.priced_orders_count == 1
    assert result.unpriced_orders_count == 1


def test_fees_total_always_sums_every_order_regardless_of_pricing():
    orders = [
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=101.0, filled_qty=1.0, fees_total=0.02),
        OrderFillView(side="SELL", reference_price=None, avg_fill_price=99.0, filled_qty=1.0, fees_total=0.03),
    ]
    result = compute_cost_metrics(orders)
    assert result.fees_total == 0.05


def test_weighted_percentage_gives_more_weight_to_larger_notional_orders():
    """Duas ordens com o MESMO percentual individual de slippage, mas
    tamanhos MUITO diferentes -- o percentual ponderado deve refletir o
    notional, nunca a média simples (que daria peso igual a ambas)."""
    orders = [
        # 1% adverso sobre notional pequeno (100 * 0.01 = 1.0 de notional).
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=101.0, filled_qty=0.01, fees_total=0.0),
        # 1% adverso sobre notional grande (100 * 100 = 10_000 de notional).
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=101.0, filled_qty=100.0, fees_total=0.0),
    ]
    result = compute_cost_metrics(orders)
    # Ambas as ordens têm exatamente 1% de slippage -- ponderado por
    # notional também deve dar ~1%, não distorcido.
    assert round(result.weighted_slippage_pct, 4) == 1.0
    assert round(result.adverse_slippage_pct, 4) == 1.0


def test_weighted_percentage_differs_from_simple_average_when_sizes_differ():
    """Uma ordem GIGANTE com 10% de slippage adverso e uma ordem MINÚSCULA
    com 0% (melhoria total) -- a média simples dos percentuais seria ~5%,
    mas o percentual ponderado por notional deve ficar próximo de 10%
    (dominado pela ordem grande)."""
    orders = [
        # Ordem grande: 10% adverso, notional 100_000.
        OrderFillView(side="BUY", reference_price=1000.0, avg_fill_price=1100.0, filled_qty=100.0, fees_total=0.0),
        # Ordem minúscula: 100% de melhoria, notional 1.0.
        OrderFillView(side="BUY", reference_price=1000.0, avg_fill_price=0.0, filled_qty=0.001, fees_total=0.0),
    ]
    result = compute_cost_metrics(orders)
    simple_average_would_be = (10.0 + (-100.0)) / 2  # -45%, nunca calculado por este código
    assert result.weighted_slippage_pct != simple_average_would_be
    assert result.weighted_slippage_pct > 9.0  # dominado pela ordem grande, próximo de 10%


def test_multiple_fills_on_one_order_already_arrive_pre_aggregated():
    """`avg_fill_price`/`filled_qty` de uma Order já vêm ponderados pelos
    fills individuais (app/execution/fill_ledger.py::record_new_fills) --
    compute_cost_metrics nunca reprocessa fills individuais, apenas usa os
    valores agregados da ordem, exatamente uma vez por ordem (nunca soma
    fills duplicados)."""
    orders = [OrderFillView(
        side="BUY", reference_price=100.0, avg_fill_price=100.5, filled_qty=3.0, fees_total=0.03,
    )]
    result = compute_cost_metrics(orders)
    assert result.adverse_slippage_cost_usd == 1.5  # 0.5 * 3.0, uma única vez
    assert result.priced_orders_count == 1


def test_zero_filled_qty_never_divides_by_zero():
    orders = [OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=105.0, filled_qty=0.0, fees_total=0.0)]
    result = compute_cost_metrics(orders)
    assert result.adverse_slippage_cost_usd == 0.0
    assert result.reference_notional_total_usd == 0.0
    assert result.weighted_slippage_pct == UNAVAILABLE  # notional total 0 -> percentual indefinido, nunca inventado
    assert result.adverse_slippage_pct == UNAVAILABLE
