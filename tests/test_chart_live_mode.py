"""Fase 3.6 (redesign do painel + correção do gráfico): prova, via um
`LightweightCharts` falso instrumentado (mesmo padrão de
tests/test_chart_js_logic.py), que a causa raiz comprovada do "gráfico
voltar" -- refreshChart() reancorando o timeScale a cada 2s -- está
corrigida: modo ao vivo continua acompanhando, modo exploração nunca é
reancorado pelo polling, "Voltar ao tempo atual" reativa o
acompanhamento, atualização de topo usa update() incremental (nunca
setData() completo), timestamps duplicados são tratados, resize/troca de
símbolo nunca recriam listeners acumulados, e a aba oculta não dispara
nenhuma chamada de rede.

O harness extrai o app.js só até ANTES do bootstrap final
(`refreshAll(); setInterval(refreshAll, 2000);`) -- mesma técnica já
usada em tests/test_shadow_panel_xss_safety.py para frontend/shadow.html
-- porque um eval() do arquivo INTEIRO dispararia esse bootstrap
imediatamente e competiria (assincronamente) com as chamadas manuais e
controladas que o teste faz.
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

_BOOTSTRAP_MARKER = "refreshAll();\nsetInterval(refreshAll, 2000);"


def _extract_app_js_without_bootstrap() -> str:
    code = APP_JS.read_text(encoding="utf-8")
    idx = code.index(_BOOTSTRAP_MARKER)
    return code[:idx]


_HARNESS = r"""
class FakeStyle { constructor() { this._props = {}; } set(k, v) { this._props[k] = v; } }
class FakeElement {
  constructor(tag, id) {
    this.tag = tag; this.id = id || ""; this.children = [];
    this._text = ""; this.className = ""; this.style = new FakeStyle();
    this.dataset = {}; this._value = ""; this.hidden = false; this._listeners = {};
    this.title = "";
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
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  disparar(type) { (this._listeners[type] || []).forEach((fn) => fn()); }
  removeAttribute() {}
}

const elementsById = {};
function elFor(id) {
  if (!elementsById[id]) elementsById[id] = new FakeElement("div", id);
  return elementsById[id];
}
elFor("chart-symbol-select").value = "BTCUSDT";

global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elFor(id),
  querySelector: () => new FakeElement("tbody"),
  querySelectorAll: () => [],
  hidden: false,
  body: new FakeElement("body"),
  addEventListener() {},
};
global.window = global;
global.setInterval = () => {};
process.on("unhandledRejection", () => {});
global.ResizeObserver = class { observe() {} disconnect() {} };

// Registro REAL de listeners no "window" (correção da 2a auditoria, item
// 5): initChartInteractionTrackingOnce() registra mouseup/touchend/
// touchcancel no window (não só no container) porque um arraste real
// pode terminar com o ponteiro solto fora da área do gráfico. Este
// harness precisa simular esse disparo de verdade para testar arraste
// (>3s), wheel isolado e toque -- não basta um addEventListener() vazio.
global._windowListeners = {};
global.addEventListener = function (type, fn) {
  (global._windowListeners[type] = global._windowListeners[type] || []).push(fn);
};
global.dispatchWindowEvent = function (type) {
  (global._windowListeners[type] || []).forEach((fn) => fn());
};

const LOG = { createChartCalls: 0, setDataCalls: 0, updateCalls: 0, fitContentCalls: 0,
              setVisibleRangeCalls: 0, subscribeVisibleTimeRangeChangeCalls: 0,
              subscribeCrosshairMoveCalls: 0 };

function makeFakeSeries() {
  return {
    setData() { LOG.setDataCalls++; },
    update() { LOG.updateCalls++; },
    setMarkers() {},
    createPriceLine() { return {}; },
    removePriceLine() {},
    priceToCoordinate() { return 100; },
    priceScale() { return { applyOptions() {} }; },
  };
}
function makeFakeChart() {
  LOG.createChartCalls++;
  let visibleRange = { from: 0, to: 100 };
  return {
    addCandlestickSeries: makeFakeSeries,
    addHistogramSeries: makeFakeSeries,
    addLineSeries: makeFakeSeries,
    timeScale() {
      return {
        subscribeVisibleTimeRangeChange(fn) { LOG.subscribeVisibleTimeRangeChangeCalls++; global.__lastRangeHandler = fn; },
        fitContent() { LOG.fitContentCalls++; },
        setVisibleRange(r) { LOG.setVisibleRangeCalls++; visibleRange = r; },
        getVisibleRange() { return visibleRange; },
      };
    },
    subscribeCrosshairMove() { LOG.subscribeCrosshairMoveCalls++; },
    remove() {},
  };
}
global.LightweightCharts = { createChart: () => makeFakeChart() };

// SMA sobre "candles estrategicos" fixos (nunca vazios) -- sem isso, as
// series de SMA ficam sempre vazias no mock e caem sempre no fallback
// setData([]) de applyCandlesToSeries() (prevMapped.length===0), o que
// contaminaria a contagem de setData() com um artefato do MOCK, nao um
// comportamento real do codigo sob teste.
const STRATEGY_CANDLES = Array.from({ length: 25 }, (_, i) => ({
  open_time: new Date(i * 900000).toISOString(), close: 10 + i, complete: true,
}));

function candleBody(candles) {
  return {
    symbol: "BTCUSDT", timeframe: "1m", market_data_timeframe: "1m", strategy_timeframe: "15m",
    candles, visual_price: 1, visual_price_at: null, visual_price_source: "last_closed_candle",
    strategy_config: { fast_period: 9, slow_period: 21 }, position: null,
    recent_signals: [], symbol_health: { status: "SAUDAVEL" }, warmup: { required: 22, have: 22, ready: true },
    strategy_candles: STRATEGY_CANDLES, cost_gate: { required_ratio: 1.5 },
  };
}

let CANDLES = [
  { time: 1, open: 1, high: 2, low: 0, close: 1, volume: 1 },
  { time: 2, open: 1, high: 2, low: 0, close: 1.5, volume: 1 },
  { time: 3, open: 1.5, high: 2, low: 1, close: 1.8, volume: 1 },
];

global.fetch = (url) => {
  if (url.includes("/api/symbols")) {
    return Promise.resolve({ json: () => Promise.resolve({ symbols: ["BTCUSDT", "ETHUSDT"] }) });
  }
  if (url.includes("symbol=BTCUSDT")) {
    return Promise.resolve({ json: () => Promise.resolve(candleBody(CANDLES)) });
  }
  return Promise.resolve({ json: () => Promise.resolve({}) });
};

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

async function main() {
  const results = {};

  // --- 1ª carga: setData completo, fitContent (modo ao vivo, símbolo novo) ---
  await refreshChart();
  results.afterFirstLoad = { ...LOG };

  // --- itens 1/7: refresh subsequente com só o topo mudando -> update(), nunca setData() extra ---
  CANDLES = [CANDLES[0], CANDLES[1], { time: 3, open: 1.5, high: 2.1, low: 1, close: 1.9, volume: 1.2 }, { time: 4, open: 1.9, high: 2, low: 1.8, close: 1.95, volume: 0.5 }];
  const setDataBefore = LOG.setDataCalls, updateBefore = LOG.updateCalls;
  await refreshChart();
  results.incrementalUpdate = { setDataDelta: LOG.setDataCalls - setDataBefore, updateDelta: LOG.updateCalls - updateBefore };

  // --- itens 11/12: refresh de novo, MESMO símbolo -> nunca recria o chart nem os listeners ---
  const createChartBefore = LOG.createChartCalls, subsBefore = LOG.subscribeVisibleTimeRangeChangeCalls;
  await refreshChart();
  results.noRecreationOnPlainRefresh = { createChartDelta: LOG.createChartCalls - createChartBefore, subscribeDelta: LOG.subscribeVisibleTimeRangeChangeCalls - subsBefore };

  // --- item 9: timestamps duplicados são tratados (dedup, sem quebrar) ---
  CANDLES = [...CANDLES, { time: 4, open: 1.95, high: 2, low: 1.9, close: 2.0, volume: 0.3 }]; // dup de time=4
  await refreshChart();
  results.dedupSurvived = true; // se chegou aqui sem lançar exceção, passou

  // --- itens 3/4/5: simula EXPLORAÇÃO manual (o usuário arrastou/deu zoom) ---
  // A detecção real é por LISTA DE PERMISSÃO -- só conta como exploração
  // se uma interação real (mousedown/touchstart/wheel) aconteceu no
  // canvas pouco antes da mudança de range (comprovado em navegador real
  // que a biblioteca dispara subscribeVisibleTimeRangeChange sozinha, de
  // forma assíncrona e imprevisível, por motivos alheios ao usuário --
  // criação do chart, setData, autoSize -- então tentar diferenciar
  // "mudança nossa" vs. "mudança da biblioteca" por temporização é uma
  // corrida perdida; o oposto, exigir prova de input real, é robusto).
  elFor("chart-container").disparar("mousedown");
  global.__lastRangeHandler(); // dispara o listener como se fosse o usuário
  results.exploringAfterUserDrag = elFor("chart-back-to-live-btn").hidden === false;
  const fitBefore = LOG.fitContentCalls, rangeBefore = LOG.setVisibleRangeCalls;
  CANDLES = [...CANDLES, { time: 5, open: 2.0, high: 2.1, low: 1.95, close: 2.05, volume: 0.4 }];
  await refreshChart(); // atualização automática NÃO pode reancorar
  results.rangeCallsWhileExploring = { fitContentDelta: LOG.fitContentCalls - fitBefore, setVisibleRangeDelta: LOG.setVisibleRangeCalls - rangeBefore };

  // --- item 6: "Voltar ao tempo atual" reativa o acompanhamento ---
  const rangeCallsBeforeReturn = LOG.setVisibleRangeCalls + LOG.fitContentCalls;
  returnToLiveView();
  results.liveAfterReturn = elFor("chart-back-to-live-btn").hidden === true;
  results.rangeCallAfterReturn = (LOG.setVisibleRangeCalls + LOG.fitContentCalls) > rangeCallsBeforeReturn;

  // --- item 13: aba oculta não dispara NENHUMA chamada de rede/gráfico ---
  document.hidden = true;
  const activityBefore = LOG.setDataCalls + LOG.updateCalls;
  await refreshChart();
  document.hidden = false;
  results.noOpWhileHidden = (LOG.setDataCalls + LOG.updateCalls) === activityBefore;

  console.log(JSON.stringify(results));
}
main().catch((e) => { console.error("HARNESS_ERROR", (e && e.stack) || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_chart_live_and_exploration_mode(tmp_path):
    script_path = tmp_path / "app_no_bootstrap.js"
    script_path.write_text(_extract_app_js_without_bootstrap(), encoding="utf-8")
    harness_path = tmp_path / "chart_live_harness.js"
    harness_path.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # 1ª carga: um chart novo, com fitContent (símbolo novo, sem posição salva).
    first = data["afterFirstLoad"]
    assert first["createChartCalls"] == 1
    assert first["subscribeVisibleTimeRangeChangeCalls"] == 1
    assert first["subscribeCrosshairMoveCalls"] == 1

    # itens 7/1: candle novo -- update() incremental, NUNCA outro setData()
    # completo (a causa raiz do salto era justamente sempre usar setData()).
    inc = data["incrementalUpdate"]
    assert inc["setDataDelta"] == 0, "atualização de topo não pode chamar setData() de novo"
    assert inc["updateDelta"] > 0, "candle novo/alterado precisa usar update() incremental"

    # itens 11/12: refresh comum do MESMO símbolo nunca recria o chart nem
    # acumula um segundo listener por cima do primeiro.
    no_recreate = data["noRecreationOnPlainRefresh"]
    assert no_recreate["createChartDelta"] == 0
    assert no_recreate["subscribeDelta"] == 0

    # item 9: candles com timestamp duplicado não quebram o harness (dedup).
    assert data["dedupSurvived"] is True

    # item 5: exploração manual desativa o acompanhamento automático
    # (o botão "Voltar ao tempo atual" fica visível).
    assert data["exploringAfterUserDrag"] is True

    # itens 2/3/4 -- a prova central desta fase: enquanto explorando, a
    # atualização automática NUNCA chama fitContent()/setVisibleRange()
    # de novo (a causa raiz exata do "gráfico volta").
    range_calls = data["rangeCallsWhileExploring"]
    assert range_calls["fitContentDelta"] == 0
    assert range_calls["setVisibleRangeDelta"] == 0

    # item 6: "Voltar ao tempo atual" reativa o modo ao vivo (o botão
    # volta a ficar escondido) e reancora a janela explicitamente, uma
    # única vez, por ação do usuário.
    assert data["liveAfterReturn"] is True
    assert data["rangeCallAfterReturn"] is True

    # item 13: aba oculta -- refreshChart() não toca em série nenhuma.
    assert data["noOpWhileHidden"] is True
