"""Fase 3.3.1 -- BARREIRA DE FRESCOR (bloco A da matriz do PO).

A barreira existe porque, até a Fase 3.3, nada no caminho de decisão
comparava `source_candle_open_time` com o relógio: durante a drenagem de
um backlog, um cruzamento de duas horas atrás chegava ao motor de risco
com todos os checks verdes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.freshness import (
    FreshnessPolicy,
    evaluate_signal_freshness,
    freshness_policy_for_market_data,
    market_data_is_historical,
    market_data_temporally_current,
)
from app.risk.config import RiskLimits
from app.risk.engine import RiskContext, RiskEngine
from app.strategy.schemas import Signal

TF = 5
MAX = 300.0
POLICY = FreshnessPolicy(max_signal_delay_after_close_seconds=MAX, strategy_timeframe_minutes=TF)
BUCKET = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
FECHA = BUCKET + timedelta(minutes=TF)          # 10:05 -- o bucket fecha aqui


def _signal(direction="BUY", src=BUCKET, price=100.0, atr=5.0) -> Signal:
    return Signal(
        symbol="BTCUSDT", direction=direction, justification="teste",
        created_at=FECHA, observed_price=price, atr=atr,
        stop_loss=price - 10 if direction == "BUY" else price + 10,
        take_profit=price + 15 if direction == "BUY" else price - 15,
        source_candle_open_time=src, params={},
    )


def _context(now, **over) -> RiskContext:
    base = dict(
        open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        state_ambiguous=False, cooldown_until=None, now=now, operational_state="ATIVO",
    )
    base.update(over)
    return RiskContext(**base)


def _engine(policy=POLICY) -> RiskEngine:
    return RiskEngine(
        limits=RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0),
        cost_model=None, freshness_policy=policy,
    )


# --- a fórmula em si -----------------------------------------------------

def test_delay_is_measured_from_the_bucket_close_never_from_the_open():
    """Um sinal de 5 min nasce ~5 min depois da ABERTURA do bucket. Medir
    desde o open_time contaria a própria duração do candle como atraso e
    recusaria todo sinal legítimo."""
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA)
    assert r.fresh is True
    assert r.detail["signal_delay_seconds"] == pytest.approx(0.0)
    assert r.detail["bucket_close_time"] == FECHA.isoformat()
    assert r.detail["source_candle_open_time"] == BUCKET.isoformat()
    # Se medisse desde a abertura, o atraso seria 300s -- exatamente o
    # limite -- e qualquer processamento normal já estouraria.
    assert (FECHA - BUCKET).total_seconds() == 300.0


def test_a_signal_evaluated_right_after_the_close_is_fresh():
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA + timedelta(seconds=12))
    assert r.fresh is True
    assert r.detail["signal_delay_seconds"] == pytest.approx(12.0)
    assert r.detail["passed"] is True


def test_exactly_at_the_limit_is_ACCEPTED():
    """Contrato documentado: o limite é INCLUSIVO."""
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA + timedelta(seconds=MAX))
    assert r.fresh is True
    assert r.detail["signal_delay_seconds"] == pytest.approx(MAX)


def test_one_second_over_the_limit_is_REJECTED():
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA + timedelta(seconds=MAX + 1))
    assert r.fresh is False
    assert r.detail["failure"] == "signal_too_old"
    assert "defasado" in r.reason


def test_a_backlog_signal_two_hours_late_is_rejected():
    """O cenário real da auditoria da Fase 3.3: sinais chegaram a ter 125
    minutos de idade durante a drenagem."""
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA + timedelta(hours=2))
    assert r.fresh is False
    assert r.detail["failure"] == "signal_too_old"
    assert r.detail["signal_delay_seconds"] == pytest.approx(7200.0)


def test_future_timestamp_beyond_tolerance_is_rejected():
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA - timedelta(seconds=30))
    assert r.fresh is False
    assert r.detail["failure"] == "future_timestamp"


def test_small_clock_jitter_into_the_future_is_tolerated():
    """Diferença de relógio entre corretora e máquina local é normal e não
    pode recusar sinal legítimo."""
    r = evaluate_signal_freshness(POLICY, BUCKET, FECHA - timedelta(seconds=1))
    assert r.fresh is True


def test_missing_source_candle_open_time_is_rejected():
    r = evaluate_signal_freshness(POLICY, None, FECHA)
    assert r.fresh is False
    assert r.detail["failure"] == "missing_source_candle_open_time"


@pytest.mark.parametrize("tf", [0, -5, None, "5m", 2.5])
def test_invalid_strategy_timeframe_is_rejected(tf):
    bad = FreshnessPolicy(max_signal_delay_after_close_seconds=MAX, strategy_timeframe_minutes=tf)
    r = evaluate_signal_freshness(bad, BUCKET, FECHA)
    assert r.fresh is False
    assert r.detail["failure"] == "invalid_strategy_timeframe"


def test_naive_datetimes_are_treated_as_utc_never_local():
    """Nenhum uso de horário local em lugar nenhum do cálculo."""
    naive_src = BUCKET.replace(tzinfo=None)
    naive_now = (FECHA + timedelta(seconds=10)).replace(tzinfo=None)
    r = evaluate_signal_freshness(POLICY, naive_src, naive_now)
    assert r.fresh is True
    assert r.detail["signal_delay_seconds"] == pytest.approx(10.0)
    assert r.detail["source_candle_open_time"].endswith("+00:00")
    assert r.detail["evaluated_at"].endswith("+00:00")


def test_a_signal_from_another_timezone_yields_the_same_delay():
    from datetime import timezone as tz

    src_sp = BUCKET.astimezone(tz(timedelta(hours=-3)))
    r = evaluate_signal_freshness(POLICY, src_sp, FECHA + timedelta(seconds=10))
    assert r.fresh is True
    assert r.detail["signal_delay_seconds"] == pytest.approx(10.0)


# --- integração com o RiskEngine ----------------------------------------

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_entry_is_blocked_for_both_sides_when_stale(direction):
    engine = _engine()
    r = engine.evaluate(
        _signal(direction, price=100.0),
        signal_id=1, context=_context(FECHA + timedelta(hours=2)),
    )
    assert r.approved is False
    assert r.checks["signal_is_fresh"] is False
    assert r.checks["signal_freshness"]["applied"] is True
    assert r.checks["signal_freshness"]["failure"] == "signal_too_old"


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_fresh_entry_passes_the_barrier_for_both_sides(direction):
    engine = _engine()
    r = engine.evaluate(
        _signal(direction, price=100.0),
        signal_id=1, context=_context(FECHA + timedelta(seconds=20)),
    )
    assert r.checks["signal_is_fresh"] is True
    assert r.approved is True  # sem cost_model, nada mais barra


def test_checks_json_records_every_required_field():
    engine = _engine()
    r = engine.evaluate(_signal(), signal_id=1, context=_context(FECHA + timedelta(seconds=42)))
    f = r.checks["signal_freshness"]
    for campo in ("source_candle_open_time", "strategy_timeframe_minutes", "bucket_close_time",
                  "evaluated_at", "signal_delay_seconds",
                  "max_signal_delay_after_close_seconds", "passed"):
        assert campo in f, campo
    import json
    json.dumps(r.checks)  # serializável: é assim que vai para checks_json


def test_the_barrier_runs_before_the_cost_gate():
    """Um sinal defasado não merece nem ser medido pelo gate de custos."""
    from app.risk.cost_model import SOURCE_PAPER_CONFIG, CostModel

    engine = RiskEngine(
        limits=RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0),
        cost_model=CostModel(fee_rate=0.0006, slippage_bps=5.0, source=SOURCE_PAPER_CONFIG,
                             expected_move_atr_multiple=1.0, minimum_cost_coverage_ratio=3.0),
        freshness_policy=POLICY,
    )
    r = engine.evaluate(_signal(), signal_id=1, context=_context(FECHA + timedelta(hours=2)))
    assert r.approved is False
    assert r.checks["signal_is_fresh"] is False
    assert "cost_gate" not in r.checks  # nem chegou lá


def test_without_a_policy_the_barrier_is_recorded_as_not_applied():
    engine = _engine(policy=None)
    r = engine.evaluate(_signal(), signal_id=1, context=_context(FECHA + timedelta(hours=99)))
    assert r.checks["signal_freshness"]["applied"] is False
    assert "reason" in r.checks["signal_freshness"]
    assert "signal_is_fresh" not in r.checks  # nunca uma aprovação silenciosa


# --- saídas NUNCA são bloqueadas ----------------------------------------

def test_closing_is_never_blocked_by_freshness():
    engine = _engine()
    r = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=1.0,
        position_exists=True, position_qty=1.0, position_side="BUY",
        context=_context(FECHA + timedelta(days=30)),   # dado absurdamente velho
    )
    assert r.approved is True
    assert "signal_is_fresh" not in r.checks
    assert "signal_freshness" not in r.checks


def test_reducing_is_never_blocked_by_freshness():
    engine = _engine()
    r = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=0.5,
        position_exists=True, position_qty=1.0, position_side="BUY",
        context=_context(FECHA + timedelta(hours=8)),
    )
    assert r.approved is True


def test_stop_and_take_paths_use_evaluate_close_and_are_immune(tmp_path):
    """Prova estrutural: `_check_stop_take` chama `_close_position_via_risk`,
    que usa `evaluate_close` -- caminho que jamais consulta frescor."""
    import inspect

    from app.orchestrator import Orchestrator

    fonte = inspect.getsource(Orchestrator._check_stop_take)
    assert "_close_position_via_risk" in fonte
    fechamento = inspect.getsource(Orchestrator._close_position_via_risk)
    assert "evaluate_close" in fechamento
    assert "freshness" not in fechamento


# --- cenário adversarial exigido pelo PO --------------------------------

def test_adversarial_active_process_receives_two_hour_old_crossover(tmp_path):
    """Processo ATIVO recebe cruzamento de duas horas atrás: nenhuma ordem
    é criada. É o cenário que motivou a fase inteira."""
    from app.risk.cost_model import SOURCE_PAPER_CONFIG, CostModel

    engine = RiskEngine(
        limits=RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0),
        cost_model=CostModel(fee_rate=0.0, slippage_bps=0.0, source=SOURCE_PAPER_CONFIG,
                             expected_move_atr_multiple=1.0, minimum_cost_coverage_ratio=3.0),
        freshness_policy=POLICY,
    )
    # Custo ZERO: sem a barreira, o gate de custos aprovaria com folga.
    antigo = _signal("BUY", src=FECHA - timedelta(minutes=TF) - timedelta(hours=2))
    r = engine.evaluate(antigo, signal_id=1, context=_context(FECHA, operational_state="ATIVO"))
    assert r.approved is False
    assert r.approved_order is None          # nenhuma ordem pode ser cunhada
    assert r.checks["signal_is_fresh"] is False
    assert r.checks["operational_state_active"] is True   # estava ATIVO mesmo


def test_restart_scenario_old_signal_never_becomes_an_order():
    """Retomada após queda: o backlog traz sinal antigo, o sistema está
    ATIVO e nada vira ordem."""
    engine = _engine()
    for atraso_min in (10, 60, 125, 240):
        r = engine.evaluate(
            _signal(), signal_id=1,
            context=_context(FECHA + timedelta(minutes=atraso_min)),
        )
        assert r.approved is False, f"atraso de {atraso_min} min foi aprovado"
        assert r.approved_order is None


# --- semântica temporal da FONTE DE MERCADO ------------------------------
# O que decide se a barreira se aplica é a fonte de mercado do modo, nunca
# o motor de execução. Os testes abaixo fixam esse critério.

def test_the_barrier_applies_when_the_market_data_source_is_live():
    assert market_data_is_historical("PAPER_LIVE") is False
    assert market_data_is_historical("BYBIT_DEMO") is False
    assert freshness_policy_for_market_data("PAPER_LIVE", MAX, TF) is not None
    assert freshness_policy_for_market_data("BYBIT_DEMO", MAX, TF) is not None


def test_the_barrier_does_not_apply_when_the_market_data_source_is_historical():
    """Série gravada em arquivo (fixture de 2024-01-01): compará-la com o
    relógio de parede mediria a idade do ARQUIVO, não risco."""
    assert market_data_is_historical("REPLAY") is True
    assert market_data_is_historical("PAPER_LOCAL") is True
    assert freshness_policy_for_market_data("REPLAY", MAX, TF) is None
    assert freshness_policy_for_market_data("PAPER_LOCAL", MAX, TF) is None


def test_an_unmapped_mode_defaults_to_protected():
    """Default seguro: esquecer de mapear um modo novo NUNCA pode desligar
    a barreira silenciosamente."""
    assert market_data_is_historical("MODO_QUE_AINDA_NAO_EXISTE") is False
    assert freshness_policy_for_market_data("MODO_QUE_AINDA_NAO_EXISTE", MAX, TF) is not None


def test_paper_live_really_runs_the_local_execution_engine(tmp_path):
    """Fixa o fato que torna o teste seguinte necessário: PAPER_LIVE usa
    `PaperLocalExecutionEngine`. Se o critério da barreira fosse o motor de
    execução, este modo perderia a proteção justamente operando com
    mercado real."""
    from app.api.main import build_orchestrator
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from tests.fakes.bybit_fake import FakeBybitTransport

    orch = build_orchestrator(
        Settings(mode=RunMode.PAPER_LIVE, symbols="BTCUSDT",
                 database_url=f"sqlite:///{tmp_path / 'motor.db'}",
                 strategy_timeframe_minutes=TF),
        bybit_transport=FakeBybitTransport(),
    )
    assert isinstance(orch.execution_engine, PaperLocalExecutionEngine)
    # ... e mesmo assim a barreira está montada para este modo.
    assert freshness_policy_for_market_data(RunMode.PAPER_LIVE.value, MAX, TF) is not None


def test_local_execution_engine_does_not_disable_the_barrier():
    """PAPER_LIVE roda `PaperLocalExecutionEngine` -- execução local, sem
    corretora -- e MESMO ASSIM consome mercado público ATUAL. Prova ponta a
    ponta: a política obtida pelo caminho REAL do modo, com um sinal
    atrasado, recusa por `signal_is_fresh`."""
    politica = freshness_policy_for_market_data("PAPER_LIVE", MAX, TF)
    assert politica is not None, "PAPER_LIVE não pode ficar sem barreira"

    engine = RiskEngine(limits=RiskLimits(), cost_model=None, freshness_policy=politica)

    # Sinal cujo bucket fechou 40 minutos atrás -- backlog sendo drenado.
    r = engine.evaluate(
        _signal(), signal_id=1, context=_context(FECHA + timedelta(minutes=40)),
    )
    assert r.approved is False
    assert r.approved_order is None
    assert r.checks["signal_is_fresh"] is False
    assert r.checks["signal_freshness"]["applied"] is True
    assert r.checks["signal_freshness"]["failure"] == "signal_too_old"
    assert "defasado" in r.reason


# --- conceito (2): atualidade do dado de mercado ------------------------

def test_market_data_temporally_current_measures_the_candle_not_the_reception():
    agora = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)
    atual = market_data_temporally_current(
        last_candle_open_time=agora - timedelta(minutes=2),
        market_data_timeframe_minutes=1, now=agora, max_delay_seconds=300.0,
    )
    assert atual["current"] is True
    assert atual["delay_seconds"] == pytest.approx(60.0)

    velho = market_data_temporally_current(
        last_candle_open_time=agora - timedelta(hours=3),
        market_data_timeframe_minutes=1, now=agora, max_delay_seconds=300.0,
    )
    assert velho["current"] is False
    assert velho["failure"] == "market_data_stale"

    sem = market_data_temporally_current(None, 1, agora, 300.0)
    assert sem["current"] is False and sem["failure"] == "no_candle"
