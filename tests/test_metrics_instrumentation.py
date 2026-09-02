"""Fase 3.3.1 -- INSTRUMENTAÇÃO (blocos C e D da matriz do PO).

Motivação registrada: a auditoria da Fase 3.3 leu `/api/signals?limit=200`
saturado no limite e reportou 200 sinais quando eram 257; e expôs só a
média de cobertura (0,89×), que não distingue "quase passando" de "muito
longe" -- justamente a distinção necessária para calibrar.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.metrics.coverage import coverage_distribution
from app.persistence import repo
from app.persistence.db import session_scope
from tests.factories import activate_operational_state

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _build(tmp_path, db, symbols=SYMBOLS, minutes=5):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=",".join(symbols),
        database_url=f"sqlite:///{tmp_path / db}", strategy_timeframe_minutes=minutes,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    return orch


def _run(orch, max_ticks=1500):
    for _ in range(max_ticks):
        if orch.tick().get("status") == "no_data":
            break


def _client(orch):
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app)


# --- distribuição: matemática pura --------------------------------------

def _amostras(ratios):
    return [{"applied": True, "achieved_coverage_ratio": r,
             "cost_coverage_ok": (r is not None and r >= 3.0)} for r in ratios]


def test_empty_sample_returns_none_never_zero():
    d = coverage_distribution([], 3.0)
    assert d["evaluated"] == 0
    for campo in ("min", "p50", "mean", "p90", "max",
                  "below_1x", "between_1x_2x", "between_2x_3x", "at_or_above_3x"):
        assert d[campo] is None, campo
    assert d["required_ratio"] == 3.0


def test_null_ratios_are_counted_apart_never_treated_as_zero():
    """Cobertura `None` é legítima quando o custo configurado é zero
    (indefinida, não infinita nem zero)."""
    d = coverage_distribution(_amostras([1.0, None, 2.0, None]), 3.0)
    assert d["evaluated"] == 4
    assert d["samples_with_ratio"] == 2
    assert d["samples_without_ratio"] == 2
    assert d["min"] == 1.0 and d["max"] == 2.0
    assert d["mean"] == pytest.approx(1.5)   # os None NÃO entraram na média


def test_percentiles_are_deterministic_and_are_observed_values():
    ratios = [0.5, 0.9, 1.2, 1.8, 2.4, 2.9, 3.1, 4.0, 5.0, 8.0]
    a = coverage_distribution(_amostras(ratios), 3.0)
    b = coverage_distribution(_amostras(list(reversed(ratios))), 3.0)
    assert a["p50"] == b["p50"] and a["p90"] == b["p90"]     # independe da ordem
    assert a["p50"] in ratios and a["p90"] in ratios         # nearest-rank: valor OBSERVADO
    assert a["min"] == 0.5 and a["max"] == 8.0
    assert a["p50"] == 2.4 and a["p90"] == 5.0


def test_buckets_are_exclusive_and_sum_to_the_sample():
    ratios = [0.3, 0.99, 1.0, 1.5, 1.99, 2.0, 2.5, 2.99, 3.0, 7.0]
    d = coverage_distribution(_amostras(ratios), 3.0)
    assert d["below_1x"] == 2
    assert d["between_1x_2x"] == 3
    assert d["between_2x_3x"] == 3
    assert d["at_or_above_3x"] == 2
    assert (d["below_1x"] + d["between_1x_2x"] + d["between_2x_3x"] + d["at_or_above_3x"]
            == d["samples_with_ratio"])


def test_approved_and_rejected_come_from_the_persisted_flag():
    d = coverage_distribution(_amostras([0.5, 3.5, 4.0]), 3.0)
    assert d["evaluated"] == 3 and d["approved"] == 2 and d["rejected"] == 1


def test_no_rounding_before_the_calculation():
    d = coverage_distribution(_amostras([1.111111111, 2.222222222]), 3.0)
    assert d["mean"] == pytest.approx((1.111111111 + 2.222222222) / 2, abs=1e-12)


# --- contagens canônicas -------------------------------------------------

def test_signal_counts_are_never_truncated_at_200(tmp_path):
    """O defeito de leitura da Fase 3.3: `?limit=200` devolvia 200 de 257."""
    orch = _build(tmp_path, "contagem.db", minutes=1)  # 180x3 = 540 sinais
    _run(orch)
    with session_scope(orch.session_factory) as session:
        counts = repo.signal_counts(session)
        paginado = len(repo.recent_signals(session, limit=200))
    assert counts["signals_total"] > 200
    assert paginado == 200                     # o endpoint satura, como sempre saturou
    assert counts["signals_total"] != paginado


def test_actionable_plus_hold_equals_total(tmp_path):
    orch = _build(tmp_path, "soma.db")
    _run(orch)
    with session_scope(orch.session_factory) as session:
        c = repo.signal_counts(session)
    assert c["actionable_signals_total"] + c["hold_signals_total"] == c["signals_total"]
    assert sum(c["by_direction"].values()) == c["signals_total"]


def test_global_total_equals_the_sum_per_symbol(tmp_path):
    orch = _build(tmp_path, "por_simbolo.db")
    _run(orch)
    with session_scope(orch.session_factory) as session:
        total = repo.signal_counts(session)["signals_total"]
        soma = sum(repo.signal_counts(session, s)["signals_total"] for s in SYMBOLS)
    assert total == soma


def test_empty_database_counts_zero_without_crashing(tmp_path):
    orch = _build(tmp_path, "vazio.db")
    with session_scope(orch.session_factory) as session:
        c = repo.signal_counts(session)
    assert c == {"signals_total": 0, "actionable_signals_total": 0,
                 "hold_signals_total": 0, "by_direction": {}}


# --- motivo dominante de rejeição ---------------------------------------

def test_dominant_rejection_reason_is_reported(tmp_path):
    orch = _build(tmp_path, "motivos.db")
    _run(orch)
    with session_scope(orch.session_factory) as session:
        r = repo.rejection_reasons(session)
    assert r["evaluations_total"] == r["approved_total"] + r["rejected_total"]
    if r["rejected_total"]:
        assert r["dominant_reason"] in r["by_reason"]
        assert sum(r["by_reason"].values()) == r["rejected_total"]


def test_rejection_reasons_read_legacy_checks_json(tmp_path):
    """Compatibilidade histórica: `checks_json` gravado antes desta fase
    usava a chave `data_fresh`. Continua legível."""
    orch = _build(tmp_path, "legado.db")
    with session_scope(orch.session_factory) as session:
        sig = repo.save_signal(session, "BTCUSDT", "BUY", "legado", 100.0, 1.0, {})
        repo.save_risk_evaluation(
            session, sig.id, False, "dado antigo",
            {"kill_switch_engaged": True, "data_fresh": False},   # chave LEGADA
        )
        r = repo.rejection_reasons(session)
    assert r["rejected_total"] == 1
    assert "data_fresh" in r["by_reason"]      # lido, não ignorado


# --- contrato da API -----------------------------------------------------

def test_metrics_expose_distribution_and_canonical_counts(tmp_path):
    orch = _build(tmp_path, "api.db", minutes=1)  # >200 sinais, para provar a contagem
    _run(orch)
    body = _client(orch).get("/api/metrics").json()

    gate = body["cost_gate"]
    dist = gate["coverage_distribution"]
    for campo in ("evaluated", "approved", "rejected", "required_ratio", "samples_with_ratio",
                  "samples_without_ratio", "min", "p50", "mean", "p90", "max",
                  "below_1x", "between_1x_2x", "between_2x_3x", "at_or_above_3x"):
        assert campo in dist, campo
    assert dist["required_ratio"] == 3.0

    counts = body["signal_counts"]
    assert counts["signals_total"] > 200
    assert counts["actionable_signals_total"] + counts["hold_signals_total"] == counts["signals_total"]

    assert "rejection_reasons" in body
    for symbol in SYMBOLS:
        ps = body["per_symbol"][symbol]
        assert "coverage_distribution" in ps["cost_gate"]
        assert "signal_counts" in ps and "rejection_reasons" in ps
        assert "temporal_currency" in ps
    assert (sum(body["per_symbol"][s]["signal_counts"]["signals_total"] for s in SYMBOLS)
            == counts["signals_total"])


# --- fingerprint (bloco D) ----------------------------------------------

def test_changing_only_the_freshness_window_creates_a_new_session(tmp_path):
    from sqlalchemy import select

    from app.persistence.models import OperationalSession

    db = "fingerprint.db"
    s1 = Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                  database_url=f"sqlite:///{tmp_path / db}",
                  max_signal_delay_after_close_seconds=300.0)
    orch1 = build_orchestrator(s1)
    with session_scope(orch1.session_factory) as session:
        antes = session.execute(select(OperationalSession)).scalars().all()
        assert len(antes) == 1
        id_antes = antes[0].id

    s2 = Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                  database_url=f"sqlite:///{tmp_path / db}",
                  max_signal_delay_after_close_seconds=600.0)   # única mudança
    orch2 = build_orchestrator(s2)
    with session_scope(orch2.session_factory) as session:
        todas = session.execute(select(OperationalSession).order_by(OperationalSession.id)).scalars().all()
        assert len(todas) == 2                     # sessão NOVA
        assert todas[0].id == id_antes
        assert todas[0].ended_at is not None       # a anterior foi encerrada
        assert "Configuração operacional alterada" in (todas[0].end_reason or "")
        assert todas[1].ended_at is None


def test_keeping_the_same_window_resumes_the_session(tmp_path):
    from sqlalchemy import select

    from app.persistence.models import OperationalSession

    db = "retomada.db"
    s = Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                 database_url=f"sqlite:///{tmp_path / db}",
                 max_signal_delay_after_close_seconds=300.0)
    build_orchestrator(s)
    orch2 = build_orchestrator(s)
    with session_scope(orch2.session_factory) as session:
        todas = session.execute(select(OperationalSession)).scalars().all()
        assert len(todas) == 1                     # RETOMADA, não recriada
        assert todas[0].ended_at is None


def test_the_window_is_frozen_in_the_session_snapshot(tmp_path):
    s = Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                 database_url=f"sqlite:///{tmp_path / 'snapshot.db'}",
                 max_signal_delay_after_close_seconds=450.0)
    orch = build_orchestrator(s)
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        snap = json.loads(repo.get_active_session(session, state).config_snapshot_json)
    assert snap["max_signal_delay_after_close_seconds"] == 450.0


def test_a_freshness_change_never_creates_a_new_accounting_base(tmp_path):
    from app.sessions import resolve_accounting_base

    db = "base_contabil.db"
    base_kw = dict(mode=RunMode.REPLAY, symbols="BTCUSDT",
                   database_url=f"sqlite:///{tmp_path / db}")
    orch1 = build_orchestrator(Settings(**base_kw, max_signal_delay_after_close_seconds=300.0))
    with session_scope(orch1.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        base_a, _ = resolve_accounting_base(session, repo.get_active_session(session, state))

    orch2 = build_orchestrator(Settings(**base_kw, max_signal_delay_after_close_seconds=900.0))
    with session_scope(orch2.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active = repo.get_active_session(session, state)
        base_b, base_session = resolve_accounting_base(session, active)

    assert base_b == base_a                 # MESMA base contábil
    assert base_session.id != active.id     # embora a sessão seja outra
