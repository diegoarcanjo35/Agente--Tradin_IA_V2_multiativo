"""Fase 3.1 (painel gráfico), itens 11, 12, 13 da matriz de testes:
SMA9/SMA21 batem matematicamente com fixture conhecida, troca rápida de
símbolo com resposta atrasada nunca contamina o símbolo errado, e trocar
para um símbolo sem posição remove overlays anteriores -- tudo via um
harness Node.js mínimo (mesmo padrão de tests/test_frontend_xss_safety.py),
com um `LightweightCharts` falso desta vez, para exercitar de fato o
código do gráfico.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
APP_JS = FRONTEND_DIR / "app.js"

NODE = shutil.which("node")

# Fase 3.6: app.js termina com um bootstrap incondicional
# (`refreshAll(); setInterval(refreshAll, 2000);`) que dispara IMEDIATAMENTE
# ao avaliar o arquivo inteiro -- concorrendo, de forma assíncrona e não
# determinística, com as chamadas manuais e controladas que estes harnesses
# fazem (mais evidente desde que refreshWhyNoTrades() -- que estes mocks
# antigos não simulam -- entrou no Promise.all de refreshAll()). Extrai só
# até ANTES dessa linha, mesma técnica já usada para frontend/shadow.html.
_BOOTSTRAP_MARKER = "refreshAll();\nsetInterval(refreshAll, 2000);"


def _app_js_without_bootstrap(tmp_path) -> Path:
    code = APP_JS.read_text(encoding="utf-8")
    trimmed = code[: code.index(_BOOTSTRAP_MARKER)]
    out = tmp_path / "app_no_bootstrap.js"
    out.write_text(trimmed, encoding="utf-8")
    return out

# --- item 13: computeSMA pure-function fixture -------------------------------

_SMA_HARNESS = r"""
class FakeElement {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.className = ""; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  get firstChild() { return this.children[0] || null; }
  querySelector() { return new FakeElement("tbody"); }
  querySelectorAll() { return []; }
  getContext() { return { clearRect(){}, fillText(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){} }; }
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
global.addEventListener = () => {};
process.on("unhandledRejection", () => {});

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

// Known fixture: closes 1..10 (open_time as fake unix seconds 0..9).
const candles = [1,2,3,4,5,6,7,8,9,10].map((c, i) => ({ time: i, close: c }));
const sma3 = computeSMA(candles, 3);
const sma5 = computeSMA(candles, 5);
console.log(JSON.stringify({ sma3, sma5 }));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_compute_sma_matches_hand_calculated_fixture(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    harness_path = tmp_path / "sma_harness.js"
    harness_path.write_text(_SMA_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # Hand-calculated: closes 1..10, period 3 -> first value at index 2:
    # mean(1,2,3)=2, mean(2,3,4)=3, ..., mean(8,9,10)=9.
    expected_sma3 = [2, 3, 4, 5, 6, 7, 8, 9]
    assert [p["value"] for p in data["sma3"]] == expected_sma3
    assert [p["time"] for p in data["sma3"]] == [2, 3, 4, 5, 6, 7, 8, 9]

    # period 5: mean(1..5)=3, mean(2..6)=4, mean(3..7)=5, mean(4..8)=6,
    # mean(5..9)=7, mean(6..10)=8.
    expected_sma5 = [3, 4, 5, 6, 7, 8]
    assert [p["value"] for p in data["sma5"]] == expected_sma5


# --- items 11/12: race guard + overlay cleanup on symbol switch ------------

_CHART_HARNESS = r"""
class FakeStyle { constructor() { this._props = {}; } }
class FakeElement {
  constructor(tag, id) {
    this.tag = tag; this.id = id || ""; this.children = [];
    this._text = ""; this.className = ""; this.style = new FakeStyle();
    this.dataset = {}; this._value = "";
    this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  remove() {}
  get firstChild() { return this.children[0] || null; }
  querySelector() { return new FakeElement("tbody"); }
  querySelectorAll() { return []; }
  getContext() { return { clearRect(){}, fillText(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){} }; }
  addEventListener() {}
  removeAttribute() {}
}

const elementsById = {};
function elFor(id) {
  if (!elementsById[id]) elementsById[id] = new FakeElement("div", id);
  return elementsById[id];
}
// The symbol <select> starts pointed at BTCUSDT.
elFor("chart-symbol-select").value = "BTCUSDT";

global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elFor(id),
  querySelector: () => new FakeElement("tbody"),
  querySelectorAll: () => [],
};
global.window = global;
global.addEventListener = () => {};
global.setInterval = () => {};
process.on("unhandledRejection", () => {});
global.ResizeObserver = class { observe() {} };

// Fake LightweightCharts: enough surface for ensureChart()/refreshChart().
function makeFakeSeries() {
  return {
    setData() {}, setMarkers() {},
    createPriceLine() { return {}; },
    removePriceLine() {},
    priceToCoordinate() { return 100; },
    priceScale() { return { applyOptions() {} }; },
  };
}
function makeFakeChart() {
  return {
    addCandlestickSeries: makeFakeSeries,
    addHistogramSeries: makeFakeSeries,
    addLineSeries: makeFakeSeries,
    timeScale() { return { subscribeVisibleTimeRangeChange() {}, fitContent() {}, setVisibleRange() {}, getVisibleRange() { return null; } }; },
    subscribeCrosshairMove() {},
    remove() {},
  };
}
global.LightweightCharts = { createChart: () => makeFakeChart() };

// Two responses: BTCUSDT resolves SLOWLY (after ETHUSDT), ETHUSDT resolves fast.
let btcResolve;
const btcPromise = new Promise((resolve) => { btcResolve = resolve; });
global.fetch = (url) => {
  if (url.includes("/api/symbols")) {
    return Promise.resolve({ json: () => Promise.resolve({ symbols: ["BTCUSDT", "ETHUSDT"] }) });
  }
  if (url.includes("symbol=BTCUSDT")) {
    return btcPromise.then(() => ({ json: () => Promise.resolve({
      symbol: "BTCUSDT", timeframe: "1m", candles: [{time:1,open:1,high:2,low:0,close:1,volume:1}],
      visual_price: 1, visual_price_at: null, visual_price_source: "last_closed_candle",
      strategy_config: { fast_period: 9, slow_period: 21 }, position: { side: "BUY", qty: 1, avg_entry_price: 1, stop_loss: null, take_profit: null, opened_at: new Date().toISOString() },
      recent_signals: [], symbol_health: { status: "SAUDAVEL" },
    }) }));
  }
  if (url.includes("symbol=ETHUSDT")) {
    return Promise.resolve({ json: () => Promise.resolve({
      symbol: "ETHUSDT", timeframe: "1m", candles: [{time:1,open:1,high:2,low:0,close:1,volume:1}],
      visual_price: 2, visual_price_at: null, visual_price_source: "last_closed_candle",
      strategy_config: { fast_period: 9, slow_period: 21 }, position: null,
      recent_signals: [], symbol_health: { status: "SAUDAVEL" },
    }) });
  }
  return Promise.resolve({ json: () => Promise.resolve({}) });
};

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

async function main() {
  // Kick off a refresh for BTCUSDT (its fetch will hang on btcPromise).
  const p1 = refreshChart();
  // Immediately "switch" to ETHUSDT before BTC's response arrives.
  elFor("chart-symbol-select").value = "ETHUSDT";
  const p2 = refreshChart();
  await p2; // ETH resolves fast
  const symbolAfterEth = elFor("chart-status")._text;

  // Now let BTC's delayed response finally arrive -- it must be discarded
  // (the select value is no longer "BTCUSDT").
  btcResolve();
  await p1;
  const positionStateAfterStaleArrival = elFor("chart-position-state")._text;

  console.log(JSON.stringify({
    symbolAfterEth,
    positionStateAfterStaleArrival, // must reflect ETH (no position), never BTC's stale BUY
  }));
}
main();
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_fast_symbol_switch_discards_stale_response_and_clears_overlay(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    harness_path = tmp_path / "chart_harness.js"
    harness_path.write_text(_CHART_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # Item 12: ETHUSDT has no position -- overlay/state must show "SEM
    # POSIÇÃO", never leaking BTCUSDT's stale BUY overlay/state even after
    # BTC's delayed response arrives afterward.
    assert data["positionStateAfterStaleArrival"] == "SEM POSIÇÃO"
