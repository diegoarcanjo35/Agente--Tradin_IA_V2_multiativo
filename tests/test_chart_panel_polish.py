"""Fase 3.6 -- itens restantes da matriz de 20 testes direcionados que ainda
não tinham cobertura própria: nomenclaturas exatas do brief (item de
tradução), tooltip do gráfico em português com dados reais e nunca como
markup, volume numa escala separada dos candles, e os novos estados visuais
(saudável/degradado/indisponível, sem posição/posição aberta, dados
desatualizados/erro) nunca usando verde para algo que não é lucro/sucesso
comprovado.

Mesma técnica de extração do bootstrap final (`refreshAll();
setInterval(refreshAll, 2000);`) usada em tests/test_chart_js_logic.py e
tests/test_chart_live_mode.py -- um eval() do arquivo inteiro dispararia
esse bootstrap de forma assíncrona e concorreria com as chamadas manuais
que estes harnesses fazem.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
APP_JS = FRONTEND_DIR / "app.js"
STYLES_CSS = FRONTEND_DIR / "styles.css"
NODE = shutil.which("node")

_BOOTSTRAP_MARKER = "refreshAll();\nsetInterval(refreshAll, 2000);"


def _app_js_without_bootstrap(tmp_path) -> Path:
    code = APP_JS.read_text(encoding="utf-8")
    trimmed = code[: code.index(_BOOTSTRAP_MARKER)]
    out = tmp_path / "app_no_bootstrap.js"
    out.write_text(trimmed, encoding="utf-8")
    return out


# --- item 5 (nomenclaturas): mapeamento EXATO exigido pelo brief ------------

_APP_JS_SOURCE = APP_JS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "must_contain",
    [
        '"Dados reais, operações simuladas (PAPER_LIVE)"',
        "FILLED: \"EXECUTADA\"",
        "REJECTED: \"RECUSADA\"",
        "OPEN: \"ABERTA\"",
        "CLOSED: \"ENCERRADA\"",
        'BUY: "COMPRA"',
        'SELL: "VENDA"',
        'HOLD: "AGUARDAR"',
        "Cobertura mínima dos custos (Minimum Cost Coverage Ratio)",
        "Cobertura estimada dos custos",
    ],
)
def test_exact_nomenclature_mapping_matches_brief(must_contain):
    assert must_contain in _APP_JS_SOURCE, (
        f"nomenclatura exigida pelo brief não encontrada em app.js: {must_contain!r}"
    )


def test_symbol_display_names_use_full_name_then_technical_code():
    assert 'BTCUSDT: "Bitcoin — BTC/USDT"' in _APP_JS_SOURCE
    assert 'ETHUSDT: "Ethereum — ETH/USDT"' in _APP_JS_SOURCE
    assert 'SOLUSDT: "Solana — SOL/USDT"' in _APP_JS_SOURCE


def test_why_no_trade_box_never_claims_profitability_without_evidence():
    assert "não afirma que a estratégia é lucrativa" in _APP_JS_SOURCE


# --- item 3 (leitura do gráfico): volume em faixa própria, escala separada -

def test_volume_series_uses_a_separate_price_scale_from_candles():
    assert 'priceScaleId: "volume"' in _APP_JS_SOURCE, (
        "a série de volume precisa da sua própria priceScaleId -- nunca "
        "pode compartilhar a escala dos candles (item 3 do brief: "
        "'volume não pode esconder preço')."
    )
    assert "scaleMargins" in _APP_JS_SOURCE


# --- itens 3/8 (tooltip): OHLCV + médias + sinal em português, sempre texto -

_TOOLTIP_HARNESS = r"""
class FakeStyle { constructor() { this._props = {}; } }
class FakeElement {
  constructor(tag, id) {
    this.tag = tag; this.id = id || ""; this.children = [];
    this._text = ""; this.className = ""; this.style = new FakeStyle();
    this.dataset = {}; this._value = ""; this.hidden = false;
    this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  remove() {}
  get firstChild() { return this.children[0] || null; }
  querySelector(sel) {
    if (sel === ".label") return new FakeElement("span");
    return new FakeElement("tbody");
  }
  querySelectorAll() { return []; }
  getContext() { return { clearRect(){}, fillText(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){} }; }
  addEventListener() {}
}
const elementsById = {};
function elFor(id) { if (!elementsById[id]) elementsById[id] = new FakeElement("div", id); return elementsById[id]; }
global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elFor(id),
  querySelector: () => new FakeElement("tbody"),
  querySelectorAll: () => [],
};
global.window = global;
global.addEventListener = () => {};
global.setInterval = () => {};
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
process.on("unhandledRejection", () => {});

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");

// CHART_STATE é `const` dentro de app.js -- um eval() direto do CÓDIGO
// completo faz `const`/`let` ficarem visíveis só dentro do próprio eval()
// (nunca vazam pro escopo externo, diferente de `function`/`var`). Por
// isso o código que PRECISA acessar CHART_STATE diretamente entra no
// MESMO eval(), concatenado depois do app.js.
const driver = `
// Sinal cujo direction bruto NAO esta no dicionario de traducao --
// translateDirection() cai no fallback (retorna o valor bruto) -- prova
// que mesmo esse caminho nunca vira HTML, só texto (item 15 da matriz).
const payload = '<img src=x onerror="alert(1)">';
CHART_STATE.lastMarkers = [{ time: 1000, rawDirection: payload }];

const container = new FakeElement("div");
const candleSeriesToken = {};
const volumeSeriesToken = {};
const fastToken = {};
const slowToken = {};
CHART_STATE.candleSeries = candleSeriesToken;
CHART_STATE.volumeSeries = volumeSeriesToken;
CHART_STATE.smaFastSeries = fastToken;
CHART_STATE.smaSlowSeries = slowToken;

const seriesData = new Map();
seriesData.set(candleSeriesToken, { open: 100.5, high: 101.25, low: 99.75, close: 100.9 });
seriesData.set(volumeSeriesToken, { value: 12.345 });
seriesData.set(fastToken, { value: 100.1 });
// smaSlowSeries deliberately has NO data at this point -- must render
// "Não disponível", never invent a value (item 3 do brief).

const param = { time: 1000, point: { x: 50, y: 60 }, seriesData };
renderChartTooltip(param, container);

const tooltip = CHART_STATE.tooltipEl;
const rows = tooltip.children.map((row) => ({
  label: row.children[0].textContent,
  value: row.children[1].textContent,
  valueChildCount: row.children[1].children.length,
}));

console.log(JSON.stringify({ hidden: tooltip.hidden, rows }));
`;
eval(code + "\n" + driver);
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_tooltip_shows_ohlcv_and_smas_in_portuguese_never_inventing_missing_data(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    harness_path = tmp_path / "tooltip_harness.js"
    harness_path.write_text(_TOOLTIP_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    assert data["hidden"] is False
    rows = {row["label"]: row for row in data["rows"]}

    assert "Data e hora" in rows
    assert "(UTC" in rows["Data e hora"]["value"]  # fuso explícito (item 3 do brief)
    assert "100" in rows["Abertura"]["value"]
    assert "101" in rows["Máxima"]["value"]
    assert rows["Volume"]["value"] != "Não disponível"
    assert rows["Média rápida"]["value"] != "Não disponível"
    # SMA lenta sem dado no mock -- nunca inventar, mostrar honestamente.
    assert rows["Média lenta"]["value"] == "Não disponível"

    # item 15 da matriz: payload malicioso no campo Sinal permanece TEXTO
    # puro -- nunca é interpretado como HTML (zero filhos no span de valor).
    payload = "<img src=x onerror=\"alert(1)\">"
    assert rows["Sinal"]["value"] == payload
    assert rows["Sinal"]["valueChildCount"] == 0


# --- item 7 (estados visuais): saúde/posição/erro nunca em verde indevido --

_VISUAL_STATE_HARNESS = r"""
class FakeClassList {
  constructor() { this._set = new Set(); }
  add(...cls) { cls.forEach((c) => this._set.add(c)); }
  remove(...cls) { cls.forEach((c) => this._set.delete(c)); }
  toggle(c, force) { if (force) this._set.add(c); else this._set.delete(c); }
  contains(c) { return this._set.has(c); }
  get list() { return Array.from(this._set); }
}
class FakeStyle { constructor() { this._props = {}; } }
class FakeElement {
  constructor(tag, id) {
    this.tag = tag; this.id = id || ""; this.children = [];
    this._text = ""; this.className = ""; this.style = new FakeStyle();
    this.dataset = {}; this._value = ""; this.hidden = false;
    this.classList = new FakeClassList();
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
function elFor(id) { if (!elementsById[id]) elementsById[id] = new FakeElement("div", id); return elementsById[id]; }
elFor("chart-symbol-select").value = "BTCUSDT";

global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elFor(id),
  querySelector: () => new FakeElement("tbody"),
  querySelectorAll: () => [],
  hidden: false,
  addEventListener() {},
};
global.window = global;
global.addEventListener = () => {};
global.setInterval = () => {};
process.on("unhandledRejection", () => {});
global.ResizeObserver = class { observe() {} disconnect() {} };

function makeFakeSeries() {
  return {
    setData() {}, update() {}, setMarkers() {},
    createPriceLine() { return {}; }, removePriceLine() {},
    priceToCoordinate() { return 100; },
    priceScale() { return { applyOptions() {} }; },
  };
}
function makeFakeChart() {
  let visibleRange = null;
  return {
    addCandlestickSeries: makeFakeSeries, addHistogramSeries: makeFakeSeries, addLineSeries: makeFakeSeries,
    timeScale() {
      return {
        subscribeVisibleTimeRangeChange() {}, fitContent() {}, setVisibleRange(r) { visibleRange = r; },
        getVisibleRange() { return visibleRange; },
      };
    },
    subscribeCrosshairMove() {},
    remove() {},
  };
}
global.LightweightCharts = { createChart: () => makeFakeChart() };

let RESPONSE = null;
global.fetch = (url) => {
  if (url.includes("/api/symbols")) {
    return Promise.resolve({ json: () => Promise.resolve({ symbols: ["BTCUSDT"] }) });
  }
  if (url.includes("/api/chart-data")) {
    return Promise.resolve({ json: () => Promise.resolve(RESPONSE) });
  }
  return Promise.resolve({ json: () => Promise.resolve({}) });
};

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

function bodyFor(status, warmupReady, position) {
  return {
    symbol: "BTCUSDT", timeframe: "1m", market_data_timeframe: "1m", strategy_timeframe: "15m",
    candles: [{ time: 1, open: 1, high: 2, low: 0, close: 1, volume: 1 }],
    visual_price: 1, visual_price_at: null, visual_price_source: "last_closed_candle",
    strategy_config: { fast_period: 9, slow_period: 21 },
    position, recent_signals: [],
    symbol_health: { status },
    warmup: { required: 22, have: warmupReady ? 22 : 5, ready: warmupReady },
  };
}

async function main() {
  const out = {};

  RESPONSE = bodyFor("SAUDAVEL", true, null);
  await refreshChart();
  out.healthySymbol = elFor("chart-status").classList.list;
  out.noPosition = elFor("chart-position-state").classList.list;
  out.warmupReady = elFor("chart-warmup").classList.list;

  RESPONSE = bodyFor("DEGRADADO", false, { side: "BUY", qty: 1, avg_entry_price: 1, stop_loss: null, take_profit: null, opened_at: new Date().toISOString() });
  await refreshChart();
  out.degradedSymbol = elFor("chart-status").classList.list;
  out.openPosition = elFor("chart-position-state").classList.list;
  out.warmupNotReady = elFor("chart-warmup").classList.list;

  RESPONSE = bodyFor("PARADO", true, null);
  await refreshChart();
  out.stoppedSymbol = elFor("chart-status").classList.list;

  console.log(JSON.stringify(out));
}
main().catch((e) => { console.error("HARNESS_ERROR", (e && e.stack) || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_symbol_health_and_position_states_are_visually_distinct_never_green_without_evidence(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    harness_path = tmp_path / "visual_state_harness.js"
    harness_path.write_text(_VISUAL_STATE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # Ativo saudável -> estado positivo explícito.
    assert "state-ok" in data["healthySymbol"]
    assert "state-bad" not in data["healthySymbol"]

    # Ativo degradado -> jamais no mesmo estado visual do saudável.
    assert "state-warn" in data["degradedSymbol"]
    assert "state-ok" not in data["degradedSymbol"]

    # Ativo parado (indisponível) -> estado negativo, nunca positivo.
    assert "state-bad" in data["stoppedSymbol"]
    assert "state-ok" not in data["stoppedSymbol"]

    # Sem posição -> neutro (nunca verde, nunca vermelho -- não é um
    # resultado, é a ausência de uma posição).
    assert "state-neutral" in data["noPosition"]
    assert "state-ok" not in data["noPosition"]

    # Posição aberta -> nunca verde (ter uma posição não é lucro).
    assert "state-ok" not in data["openPosition"]

    # Aquecendo -> estado de atenção; pronto -> estado positivo.
    assert "state-warn" in data["warmupNotReady"]
    assert "state-ok" in data["warmupReady"]


def test_visual_state_css_classes_exist_and_are_used_by_app_js():
    css = STYLES_CSS.read_text(encoding="utf-8")
    for cls in (".state-pill", ".state-ok", ".state-warn", ".state-bad", ".state-neutral", ".error-state", ".stale-state"):
        assert cls in css, f"classe de estado visual ausente em styles.css: {cls}"
    for cls in ("state-pill", "state-ok", "state-warn", "state-bad", "state-neutral", "error-state", "stale-state"):
        assert cls in _APP_JS_SOURCE, (
            f"classe de estado visual '{cls}' existe em styles.css mas nunca é aplicada em app.js "
            "(CSS morto -- item 7 do brief exige estados realmente distintos, não só definidos)."
        )
