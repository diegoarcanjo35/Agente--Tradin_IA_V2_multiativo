"""Fase 3.2: gate de VIABILIDADE LÍQUIDA -- o custo estimado de ida e
volta em US$ financeiro comparado ao movimento esperado derivado do ATR do
candle estratégico.

`minimum_cost_coverage_ratio = 3.0` é hipótese operacional inicial e
configurável -- não é parâmetro otimizado nem promessa de rentabilidade.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pytest

from app.core.config import RunMode, Settings
from app.risk.config import RiskLimits
from app.risk.cost_model import (
    SOURCE_BYBIT_DEMO_ESTIMATE,
    SOURCE_PAPER_CONFIG,
    CostModel,
    evaluate_cost_gate,
)
from app.risk.engine import RiskContext, RiskEngine
from app.strategy.schemas import Signal


def _model(**over) -> CostModel:
    base = dict(
        fee_rate=0.0006, slippage_bps=5.0, source=SOURCE_PAPER_CONFIG,
        expected_move_atr_multiple=1.0, minimum_cost_coverage_ratio=3.0,
    )
    base.update(over)
    return CostModel(**base)


def _signal(direction="BUY", price=100.0, atr=5.0) -> Signal:
    now = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    return Signal(
        symbol="BTCUSDT", direction=direction, justification="teste",
        created_at=now, observed_price=price, atr=atr,
        stop_loss=price - 10 if direction == "BUY" else price + 10,
        take_profit=price + 15 if direction == "BUY" else price - 15,
        source_candle_open_time=now, params={},
    )


def _context(**over) -> RiskContext:
    base = dict(
        open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        state_ambiguous=False, cooldown_until=None,
        now=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
        operational_state="ATIVO",
    )
    base.update(over)
    return RiskContext(**base)


# --- 21. unidades corretas (US$ TOTAL, nunca US$ por unidade) ------------

def test_round_trip_cost_is_computed_in_total_usd_with_separate_legs():
    """Entrada e saída têm preços projetados DIFERENTES, logo notionais
    diferentes -- o mesmo notional nunca é reaproveitado nas duas pernas."""
    model = _model(fee_rate=0.001, slippage_bps=10.0)  # 0,1% e 0,10%
    result = evaluate_cost_gate(model, "BUY", qty=2.0, entry_reference_price=100.0, atr=5.0)
    d = result.detail

    # Movimento esperado: ATR (US$/unidade) x múltiplo x quantidade.
    assert d["expected_move_per_unit_usd"] == pytest.approx(5.0)
    assert d["expected_move_usd"] == pytest.approx(10.0)

    # Entrada BUY: paga MAIS (slippage adverso).
    assert d["entry_fill_price_estimated"] == pytest.approx(100.0 * 1.001)
    assert d["entry_notional_usd"] == pytest.approx(100.1 * 2)
    assert d["entry_fee_usd"] == pytest.approx(100.1 * 2 * 0.001)
    assert d["entry_slippage_usd"] == pytest.approx(0.1 * 2)

    # Saída projetada em 105 e do lado oposto (SELL): recebe MENOS.
    assert d["projected_exit_price"] == pytest.approx(105.0)
    assert d["exit_fill_price_estimated"] == pytest.approx(105.0 * 0.999)
    assert d["exit_notional_usd"] == pytest.approx(104.895 * 2)
    # Os dois notionais são realmente diferentes -- prova explícita.
    assert d["entry_notional_usd"] != pytest.approx(d["exit_notional_usd"])

    expected_cost = (
        d["entry_fee_usd"] + d["exit_fee_usd"] + d["entry_slippage_usd"] + d["exit_slippage_usd"]
    )
    assert d["round_trip_cost_usd"] == pytest.approx(expected_cost)


# --- 22/23. aprova e rejeita -------------------------------------------

def test_approves_when_expected_move_covers_the_required_ratio():
    model = _model(fee_rate=0.0001, slippage_bps=1.0)
    result = evaluate_cost_gate(model, "BUY", qty=1.0, entry_reference_price=100.0, atr=10.0)
    assert result.approved is True
    assert result.detail["cost_coverage_ok"] is True
    assert result.detail["achieved_coverage_ratio"] > 3.0


def test_rejects_with_an_explicit_portuguese_reason_when_coverage_is_short():
    model = _model(fee_rate=0.01, slippage_bps=100.0)  # custo alto de propósito
    result = evaluate_cost_gate(model, "BUY", qty=1.0, entry_reference_price=100.0, atr=1.0)
    assert result.approved is False
    assert result.detail["failure"] == "insufficient_cost_coverage"
    assert "não cobre" in result.reason
    assert "custo estimado de ida e volta" in result.reason
    assert f"{SOURCE_PAPER_CONFIG!r}" in result.reason


# --- 24. ATR ausente: rejeita explicitamente, nunca aprova por omissão ---

@pytest.mark.parametrize("atr", [0.0, -1.0, float("nan"), float("inf")])
def test_missing_or_invalid_atr_is_rejected_never_approved_by_omission(atr):
    result = evaluate_cost_gate(_model(), "BUY", qty=1.0, entry_reference_price=100.0, atr=atr)
    assert result.approved is False
    assert result.detail["failure"] in ("atr_unavailable", "non_finite_input")
    assert result.detail["cost_coverage_ok"] is False


def test_non_finite_qty_or_price_is_rejected():
    for qty, price in ((float("nan"), 100.0), (1.0, float("inf"))):
        result = evaluate_cost_gate(_model(), "BUY", qty=qty, entry_reference_price=price, atr=5.0)
        assert result.approved is False
        assert result.detail["failure"] == "non_finite_input"


def test_zero_quantity_is_rejected():
    result = evaluate_cost_gate(_model(), "BUY", qty=0.0, entry_reference_price=100.0, atr=5.0)
    assert result.approved is False
    assert result.detail["failure"] == "qty_not_positive"


# --- custo zero configurado ---------------------------------------------

def test_zero_configured_cost_approves_and_reports_ratio_as_undefined():
    """Um simulador sem custo é configuração legítima. A razão de cobertura
    fica INDEFINIDA (`None`) -- nunca um número inventado (zero ou
    infinito)."""
    model = _model(fee_rate=0.0, slippage_bps=0.0)
    result = evaluate_cost_gate(model, "BUY", qty=1.0, entry_reference_price=100.0, atr=5.0)
    assert result.approved is True
    assert result.detail["round_trip_cost_usd"] == pytest.approx(0.0)
    assert result.detail["achieved_coverage_ratio"] is None
    assert "indefinida" in result.reason


# --- SELL com preço projetado inválido -----------------------------------

def test_sell_with_projected_exit_price_at_or_below_zero_is_rejected():
    """Um SELL cujo movimento esperado é maior que o próprio preço não
    descreve saída realizável -- recusado explicitamente em vez de
    aprovado sobre um preço artificialmente elevado até o piso."""
    result = evaluate_cost_gate(_model(), "SELL", qty=1.0, entry_reference_price=10.0, atr=50.0)
    assert result.approved is False
    assert result.detail["failure"] == "projected_exit_price_invalid"
    assert result.detail["projected_exit_price_clamped"] is True
    assert result.detail["projected_exit_price"] > 0  # nunca negativo/zero exposto


def test_sell_with_a_valid_projection_uses_the_opposite_side_slippage():
    model = _model(fee_rate=0.001, slippage_bps=10.0)
    result = evaluate_cost_gate(model, "SELL", qty=1.0, entry_reference_price=100.0, atr=5.0)
    d = result.detail
    assert d["exit_side"] == "BUY"
    assert d["entry_fill_price_estimated"] == pytest.approx(100.0 * 0.999)  # vende por menos
    assert d["projected_exit_price"] == pytest.approx(95.0)
    assert d["exit_fill_price_estimated"] == pytest.approx(95.0 * 1.001)   # recompra por mais


# --- 25/26. configuração muda a decisão e entra no fingerprint -----------

def test_coverage_ratio_is_configurable_and_changes_the_decision():
    args = dict(side="BUY", qty=1.0, entry_reference_price=100.0, atr=1.0)
    strict = evaluate_cost_gate(_model(minimum_cost_coverage_ratio=50.0), **args)
    lenient = evaluate_cost_gate(_model(minimum_cost_coverage_ratio=0.5), **args)
    assert strict.approved is False
    assert lenient.approved is True


def test_cost_gate_parameters_enter_the_session_fingerprint():
    from app.sessions import _config_fingerprint
    from app.strategy.engine import StrategyConfig

    limits = RiskLimits()
    cfg = StrategyConfig()
    base = Settings(mode=RunMode.REPLAY)
    changed_ratio = Settings(mode=RunMode.REPLAY, minimum_cost_coverage_ratio=4.0)
    changed_move = Settings(mode=RunMode.REPLAY, strategy_expected_move_atr_multiple=2.0)
    changed_bybit = Settings(mode=RunMode.REPLAY, bybit_taker_fee_rate=0.001)

    fp = _config_fingerprint(base, "v1", limits, cfg)
    assert _config_fingerprint(changed_ratio, "v1", limits, cfg) != fp
    assert _config_fingerprint(changed_move, "v1", limits, cfg) != fp
    assert _config_fingerprint(changed_bybit, "v1", limits, cfg) != fp


# --- 27. o que é persistido em checks_json -------------------------------

def test_risk_engine_records_every_gate_number_in_checks():
    engine = RiskEngine(
        limits=RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0),
        cost_model=_model(fee_rate=0.01, slippage_bps=100.0),
    )
    result = engine.evaluate(_signal(atr=0.01), signal_id=1, context=_context())
    assert result.approved is False

    gate = result.checks["cost_gate"]
    for field in (
        "qty", "atr_per_unit_usd", "expected_move_per_unit_usd", "expected_move_usd",
        "entry_reference_price", "projected_exit_price", "entry_fill_price_estimated",
        "exit_fill_price_estimated", "entry_notional_usd", "exit_notional_usd",
        "entry_fee_usd", "exit_fee_usd", "entry_slippage_usd", "exit_slippage_usd",
        "round_trip_cost_usd", "required_coverage_ratio", "achieved_coverage_ratio",
        "estimate_source", "cost_coverage_ok",
    ):
        assert field in gate, field
    assert gate["applied"] is True
    assert gate["cost_coverage_ok"] is False
    assert result.checks["cost_coverage_ok"] is False
    # Serializável: é exatamente assim que vai para RiskEvaluation.checks_json.
    json.dumps(result.checks)


def test_gate_not_wired_is_recorded_as_not_applied_never_as_silent_approval():
    engine = RiskEngine(limits=RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0))
    result = engine.evaluate(_signal(), signal_id=1, context=_context())
    assert result.approved is True
    assert result.checks["cost_gate"] == {"applied": False}
    assert "cost_coverage_ok" not in result.checks


# --- 28. fechamento nunca é bloqueado pelo gate --------------------------

def test_closing_a_position_is_never_blocked_by_the_cost_gate():
    engine = RiskEngine(
        limits=RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0),
        cost_model=_model(fee_rate=0.5, slippage_bps=5000.0),  # custo absurdo
    )
    result = engine.evaluate_close(
        symbol="BTCUSDT", close_side="SELL", qty=1.0, position_exists=True,
        position_qty=1.0, position_side="BUY", signal_id=1, context=_context(),
    )
    assert result.approved is True
    assert "cost_gate" not in result.checks
    assert "cost_coverage_ok" not in result.checks


# --- PAPER e BYBIT_DEMO com configurações distintas ----------------------

def test_paper_and_bybit_demo_use_distinct_configuration_sources():
    from app.api.main import build_cost_model
    from app.execution.paper_local import PaperLocalExecutionEngine

    paper_engine = PaperLocalExecutionEngine(
        price_provider=lambda s: 0.0, fee_rate=0.0007, slippage_bps=7.0,
    )
    paper_settings = Settings(mode=RunMode.PAPER_LIVE, paper_live_fee_rate=0.0007,
                              paper_live_slippage_bps=7.0)
    paper_model = build_cost_model(paper_settings, paper_engine)
    assert paper_model.source == SOURCE_PAPER_CONFIG
    # Exatamente os números que o simulador aplica -- lidos do próprio motor.
    assert paper_model.fee_rate == pytest.approx(0.0007)
    assert paper_model.slippage_bps == pytest.approx(7.0)

    class _BybitEngineWithoutCostAttrs:
        pass

    bybit_settings = Settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s",
        bybit_taker_fee_rate=0.00075, bybit_expected_slippage_bps=9.0,
        paper_live_fee_rate=0.0007, paper_live_slippage_bps=7.0,
    )
    bybit_model = build_cost_model(bybit_settings, _BybitEngineWithoutCostAttrs())
    assert bybit_model.source == SOURCE_BYBIT_DEMO_ESTIMATE
    assert bybit_model.fee_rate == pytest.approx(0.00075)
    assert bybit_model.slippage_bps == pytest.approx(9.0)
    # NUNCA reaproveita silenciosamente a configuração do simulador.
    assert bybit_model.fee_rate != pytest.approx(bybit_settings.paper_live_fee_rate)
    assert bybit_model.slippage_bps != pytest.approx(bybit_settings.paper_live_slippage_bps)


def test_replay_policy_is_the_paper_simulator_numbers_and_is_exact():
    """Política de REPLAY, explícita e documentada: REPLAY usa o mesmo
    `PaperLocalExecutionEngine` do PAPER_LOCAL, então o gate lê os números
    do PRÓPRIO simulador -- a estimativa não é aproximada, é exatamente o
    que será aplicado no fill."""
    from app.api.main import build_cost_model
    from app.execution.paper_local import PaperLocalExecutionEngine

    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0)
    model = build_cost_model(Settings(mode=RunMode.REPLAY), engine)
    assert model.source == SOURCE_PAPER_CONFIG
    assert model.fee_rate == pytest.approx(engine.fee_rate)
    assert model.slippage_bps == pytest.approx(engine.slippage_bps)
