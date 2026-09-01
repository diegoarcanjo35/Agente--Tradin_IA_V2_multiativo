"""Fase 3.2 (item 12 da decisão do PO): contratos ADITIVOS de
`/api/chart-data`, `/api/metrics` e `/api/symbols`, e o painel deixando
explícito o que é MERCADO (1 min) e o que é ESTRATÉGIA (5/15 min).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from tests.factories import activate_operational_state

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
APP_JS = FRONTEND_DIR / "app.js"
INDEX_HTML = FRONTEND_DIR / "index.html"
NODE = shutil.which("node")


def _client(tmp_path, name, minutes=5, symbols="BTCUSDT", ticks=120):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols,
        database_url=f"sqlite:///{tmp_path / name}", strategy_timeframe_minutes=minutes,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    for _ in range(ticks):
        if orch.tick().get("status") == "no_data":
            break
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app), orch


# --- /api/chart-data ------------------------------------------------------

def test_chart_data_keeps_the_one_minute_series_and_adds_the_strategy_block(tmp_path):
    client, _ = _client(tmp_path, "chart_api.db")
    body = client.get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()

    # O gráfico OPERACIONAL continua exatamente onde estava.
    assert body["timeframe"] == "1m"
    assert body["market_data_timeframe"] == "1m"
    assert len(body["candles"]) == 120
    assert {"time", "open", "high", "low", "close", "volume"} <= set(body["candles"][0])

    # E o bloco estratégico é acrescentado ao lado.
    assert body["strategy_timeframe"] == "5m"
    assert body["strategy_timeframe_minutes"] == 5
    assert len(body["strategy_candles"]) == 24  # 120 / 5
    assert all(c["complete"] for c in body["strategy_candles"])
    assert body["last_strategy_candle"]["complete"] is True
    assert body["warmup"]["required"] == 22
    assert body["bucket_integrity"]["complete_buckets"] == 24


def test_chart_data_exposes_authoritative_indicators_from_the_engine(tmp_path):
    client, orch = _client(tmp_path, "chart_ind.db")
    body = client.get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()
    indicators = body["strategy_indicators"]

    assert indicators == orch.strategy_engine.current_indicators()
    assert indicators["fast_period"] == 9 and indicators["slow_period"] == 21
    assert indicators["atr_per_unit_usd"] is not None
    assert indicators["atr_pct_of_price"] is not None


def test_chart_data_marks_a_forming_strategy_candle_as_partial(tmp_path):
    client, _ = _client(tmp_path, "chart_forming.db", ticks=7)
    body = client.get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()

    forming = body["forming_strategy_candle"]
    assert forming is not None
    assert forming["partial"] is True
    assert forming["complete"] is False
    assert forming["received_slots"] == 2
    assert forming["expected_slots"] == 5
    assert len(forming["missing_slots"]) == 3
    # E nunca aparece entre os candles estratégicos FECHADOS.
    assert forming["open_time"] not in [c["open_time"] for c in body["strategy_candles"] if c["complete"]]


def test_chart_data_exposes_the_cost_gate_contract(tmp_path):
    client, _ = _client(tmp_path, "chart_gate.db")
    gate = client.get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()["cost_gate"]

    assert gate["applied"] is True
    assert gate["required_ratio"] == 3.0
    assert gate["expected_move_atr_multiple"] == 1.0
    assert gate["estimate_source"] == "paper_config"
    assert "blocked_entries" in gate and "avg_coverage_ratio_at_entry" in gate


def test_chart_data_reports_incomplete_buckets_without_hiding_them(tmp_path):
    settings = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / 'chart_incomplete.db'}",
        strategy_timeframe_minutes=5,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    for _ in range(3):
        orch.tick()
    orch.market_data_provider._cursor += 2  # buraco real
    for _ in range(6):
        orch.tick()

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    body = TestClient(app).get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()

    assert body["bucket_integrity"]["incomplete_buckets"] == 1
    incomplete = [c for c in body["strategy_candles"] if not c["complete"]]
    assert len(incomplete) == 1
    assert incomplete[0]["partial"] is True
    assert incomplete[0]["received_slots"] < incomplete[0]["expected_slots"]
    assert incomplete[0]["missing_slots"]  # quais faltaram, explicitamente


# --- /api/metrics ---------------------------------------------------------

def test_metrics_add_cost_gate_timeframe_and_incomplete_buckets(tmp_path):
    client, _ = _client(tmp_path, "metrics_api.db", ticks=400)
    body = client.get("/api/metrics").json()

    # Chaves anteriores intactas (compatibilidade).
    for legacy in ("closed_trades_count", "net_profit", "win_rate", "payoff", "profit_factor",
                    "expectancy", "per_symbol"):
        assert legacy in body

    assert body["strategy_timeframe"] == "5m"
    assert body["incomplete_buckets"] == 0
    gate = body["cost_gate"]
    assert gate["evaluated"] >= 1
    assert gate["blocked_entries"] >= 0
    # `None` quando não há cobertura calculável -- nunca zero inventado.
    assert gate["avg_coverage_ratio_at_entry"] is None or isinstance(
        gate["avg_coverage_ratio_at_entry"], float
    )
    per_symbol = body["per_symbol"]["BTCUSDT"]
    assert per_symbol["strategy_timeframe"] == "5m"
    assert "cost_gate" in per_symbol and "bucket_integrity" in per_symbol


def test_metrics_cost_gate_counts_match_the_persisted_rejections(tmp_path):
    from sqlalchemy import select

    from app.persistence.db import session_scope
    from app.persistence.models import RiskEvaluation

    client, orch = _client(tmp_path, "metrics_gate.db", ticks=400)
    gate = client.get("/api/metrics").json()["cost_gate"]

    with session_scope(orch.session_factory) as session:
        rows = session.execute(select(RiskEvaluation.checks_json)).scalars().all()
    blocked = sum(
        1 for raw in rows
        if json.loads(raw).get("cost_gate", {}).get("cost_coverage_ok") is False
    )
    assert gate["blocked_entries"] == blocked


# --- /api/symbols ---------------------------------------------------------

def test_symbols_endpoint_stays_backward_compatible_and_adds_per_symbol(tmp_path):
    client, _ = _client(tmp_path, "symbols_api.db", symbols="BTCUSDT,ETHUSDT", ticks=60)
    body = client.get("/api/symbols").json()

    assert body["symbols"] == ["BTCUSDT", "ETHUSDT"]  # contrato antigo, idêntico
    for symbol in ("BTCUSDT", "ETHUSDT"):
        entry = body["per_symbol"][symbol]
        assert entry["market_data_timeframe"] == "1m"
        assert entry["strategy_timeframe"] == "5m"
        assert entry["warmup"]["required"] == 22
        assert "incomplete_buckets" in entry["bucket_integrity"]


# --- painel ---------------------------------------------------------------

def test_panel_labels_market_and_strategy_separately():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="chart-strategy-timeframe"' in html
    assert 'id="chart-warmup"' in html
    assert 'id="chart-strategy-grid"' in html
    assert "Visão da estratégia" in html
    # O gráfico operacional de 1 minuto NÃO foi retirado.
    assert 'id="chart-container"' in html
    assert 'id="chart-symbol-select"' in html


def test_panel_never_calls_the_simple_moving_average_an_ema():
    import re

    js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    # "EMA" como PALAVRA (a busca ingênua casaria dentro de "DEMANDA",
    # "SISTEMA", etc.) -- as médias deste sistema são simples, nunca
    # exponenciais, e o painel não pode chamá-las de outra coisa.
    token = re.compile(r"EMA")
    assert not token.search(js)
    assert not token.search(html)
    assert "média simples" in js  # rotulado explicitamente como SMA


def test_panel_writes_only_through_textcontent_never_innerhtml():
    import re

    js = APP_JS.read_text(encoding="utf-8")
    # Nenhuma ESCRITA via innerHTML/outerHTML/insertAdjacentHTML. O termo
    # aparece no arquivo apenas dentro de comentários que explicam por que
    # ele nunca é usado -- por isso a checagem é do USO, não da palavra.
    assert not re.search(r"\.(inner|outer)HTML\s*=", js)
    assert "insertAdjacentHTML" not in js


def test_panel_marks_the_forming_strategy_candle_as_partial():
    js = APP_JS.read_text(encoding="utf-8")
    assert "PARCIAL" in js
    assert "stat-card-partial" in js


@pytest.mark.skipif(NODE is None, reason="Node.js não disponível neste ambiente")
def test_describe_timeframe_renders_minutes_in_portuguese():
    harness = r"""
class FakeElement {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.className = ""; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  get firstChild() { return this.children[0] || null; }
  querySelector() { return new FakeElement("tbody"); }
  querySelectorAll() { return []; }
  getContext() { return { clearRect(){}, fillText(){} }; }
  addEventListener() {}
}
global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: () => new FakeElement("div"),
  querySelector: () => new FakeElement("tbody"),
  querySelectorAll: () => [],
};
global.fetch = () => Promise.resolve({ json: () => Promise.resolve([]) });
global.setInterval = () => {};
global.window = global;
process.on("unhandledRejection", () => {});
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
console.log(JSON.stringify({
  one: describeTimeframe("1m"),
  five: describeTimeframe("5m"),
  fifteen: describeTimeframe("15m"),
  missing: describeTimeframe(null),
  partial: formingLabel({ received_slots: 2, expected_slots: 5 }),
  noPartial: formingLabel(null),
}));
"""
    script = Path(__file__).resolve().parent / "_tmp_timeframe_harness.js"
    script.write_text(harness, encoding="utf-8")
    try:
        out = subprocess.run(
            [NODE, str(script), str(APP_JS)], capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
    finally:
        script.unlink(missing_ok=True)

    assert data["one"] == "1 min"
    assert data["five"] == "5 min"
    assert data["fifteen"] == "15 min"
    assert data["missing"] == "N/D"       # nunca um zero/valor inventado
    assert data["partial"] == "PARCIAL 2/5"
    assert data["noPartial"] == "N/D"


def test_panel_no_longer_hardcodes_a_mode_banner_in_the_html():
    """Fase 3.2 (ajuste multiativo): o banner do card do gráfico vem do
    backend e reflete o MODO EFETIVO. O literal antigo dizia
    "PAPER LIVE MULTIATIVO" mesmo com o processo em REPLAY."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="chart-banner"' in html
    assert 'id="chart-data-disclaimer"' in html
    assert "PAPER LIVE MULTIATIVO" not in html


def test_panel_shows_the_synthetic_data_disclaimer_in_replay(tmp_path):
    client, _ = _client(tmp_path, "banner_replay.db", ticks=10)
    body = client.get("/api/chart-data", params={"symbol": "BTCUSDT"}).json()
    assert body["data_disclaimer"] == "Dados REPLAY sintéticos — sem cotação real"
    assert body["chart_banner"].startswith("REPLAY")
