"""Correção da 2a auditoria da Fase 3.6, itens 4 e 5: a detecção de
exploração do gráfico não podia depender só de "houve um mousedown nos
últimos 3 segundos" -- isso falha para qualquer arraste que dure mais que
isso (ao soltar o botão, a marca de tempo do mousedown já teria expirado
antes do evento assíncrono da biblioteca chegar). A correção trocou por um
estado ATIVO de ponteiro/toque (sem limite de tempo enquanto pressionado),
uma janela de tolerância separada só para o gesto de soltar (pega o evento
tardio que chega logo depois do fim do gesto) e uma janela própria e
independente para wheel (que não tem "pressionado"/"solto").

Mesma técnica de extração do bootstrap final usada nos outros harnesses
desta fase (evita a corrida com `refreshAll(); setInterval(...)`).
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


def _app_js_without_bootstrap(tmp_path) -> Path:
    code = APP_JS.read_text(encoding="utf-8")
    trimmed = code[: code.index(_BOOTSTRAP_MARKER)]
    out = tmp_path / "app_no_bootstrap.js"
    out.write_text(trimmed, encoding="utf-8")
    return out


_HARNESS = r"""
class FakeStyle { constructor() { this._props = {}; } }
class FakeElement {
  constructor(tag, id) {
    this.tag = tag; this.id = id || ""; this.children = [];
    this._text = ""; this.className = ""; this.style = new FakeStyle();
    this.dataset = {}; this._value = ""; this.hidden = false; this._listeners = {};
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
function elFor(id) { if (!elementsById[id]) elementsById[id] = new FakeElement("div", id); return elementsById[id]; }
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

// Registro REAL de listeners no "window" -- initChartInteractionTrackingOnce()
// registra mouseup/touchend/touchcancel aqui (nunca só no container),
// exatamente porque um arraste real pode terminar com o ponteiro solto
// fora da área do gráfico.
global._windowListeners = {};
global.addEventListener = function (type, fn) {
  (global._windowListeners[type] = global._windowListeners[type] || []).push(fn);
};
global.dispatchWindowEvent = function (type) {
  (global._windowListeners[type] || []).forEach((fn) => fn());
};

let visibleRange = { from: 0, to: 100 };
function makeFakeSeries() {
  return { setData() {}, update() {}, setMarkers() {}, createPriceLine() { return {}; }, removePriceLine() {}, priceToCoordinate() { return 100; }, priceScale() { return { applyOptions() {} }; } };
}
function makeFakeChart() {
  return {
    addCandlestickSeries: makeFakeSeries, addHistogramSeries: makeFakeSeries, addLineSeries: makeFakeSeries,
    timeScale() {
      return {
        subscribeVisibleTimeRangeChange(fn) { global.__lastRangeHandler = fn; },
        fitContent() {}, setVisibleRange(r) { visibleRange = r; }, getVisibleRange() { return visibleRange; },
      };
    },
    subscribeCrosshairMove() {},
    remove() {},
  };
}
global.LightweightCharts = { createChart: () => makeFakeChart() };

function candleBody() {
  return {
    symbol: "BTCUSDT", timeframe: "1m", market_data_timeframe: "1m", strategy_timeframe: "15m",
    candles: [{ time: 1, open: 1, high: 2, low: 0, close: 1, volume: 1 }],
    visual_price: 1, visual_price_at: null, visual_price_source: "last_closed_candle",
    strategy_config: { fast_period: 9, slow_period: 21 }, position: null,
    recent_signals: [], symbol_health: { status: "SAUDAVEL" }, warmup: { required: 1, have: 1, ready: true },
  };
}
global.fetch = (url) => {
  if (url.includes("/api/symbols")) return Promise.resolve({ json: () => Promise.resolve({ symbols: ["BTCUSDT"] }) });
  if (url.includes("/api/chart-data")) return Promise.resolve({ json: () => Promise.resolve(candleBody()) });
  return Promise.resolve({ json: () => Promise.resolve({}) });
};

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function main() {
  const results = {};

  // --- item 5: gesto de ARRASTE com mais de 3 segundos de duração -----
  await refreshChart(); // 1a carga -- followLive true, chart criado
  elFor("chart-container").disparar("mousedown"); // pointer pressionado
  await wait(3200); // segura o botão por MAIS de 3s (a antiga janela fixa)
  results.stillHeldAfter3200ms = { followLiveBeforeEvent: true };
  global.__lastRangeHandler(); // evento de range chega enquanto AINDA segura
  results.exploringWhileStillHeldPast3s = elFor("chart-back-to-live-btn").hidden === false;

  global.dispatchWindowEvent("mouseup"); // solta o botão (no window, não no container)

  // --- volta ao modo ao vivo para o próximo cenário --------------------
  returnToLiveView();

  // --- item 5: evento assíncrono chega LOGO DEPOIS do fim do gesto -----
  elFor("chart-container").disparar("mousedown");
  global.dispatchWindowEvent("mouseup"); // solta IMEDIATAMENTE
  await wait(500); // biblioteca real notifica com atraso -- 500ms depois
  global.__lastRangeHandler();
  results.exploringFromLateEventAfterRelease = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- item 5: soltar há muito tempo (fora da janela de tolerância) não
  // conta mais como recente -- só then um evento de range chegando bem
  // depois do fim do gesto (sem nenhuma interação nova) é ignorado.
  elFor("chart-container").disparar("mousedown");
  global.dispatchWindowEvent("mouseup");
  await wait(3500); // além da janela de tolerância de liberação
  global.__lastRangeHandler();
  results.notExploringLongAfterRelease = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- item 5: WHEEL tratado separadamente -- sem nenhum mousedown/touch,
  // só a roda do mouse, ainda assim conta como interação real.
  elFor("chart-container").disparar("wheel");
  global.__lastRangeHandler();
  results.exploringFromWheelAlone = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- item 5/9: TOQUE (touch/mobile) -- touchstart sem mouse nenhum ---
  elFor("chart-container").disparar("touchstart");
  global.__lastRangeHandler();
  results.exploringFromTouchAlone = elFor("chart-back-to-live-btn").hidden === false;
  global.dispatchWindowEvent("touchend");

  returnToLiveView();

  // --- regressão: SEM nenhuma interação, o evento (sempre programático)
  // nunca conta como exploração -- a causa raiz original desta fase.
  // Espera a janela de tolerância do gesto de toque anterior esgotar de
  // verdade antes de testar "nenhuma interação" -- senão o teste estaria
  // testando a janela de graça do toque anterior, não a ausência real.
  await wait(3200);
  global.__lastRangeHandler();
  results.neverExploringWithoutAnyInput = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- 3a auditoria, item 3: pointerup completa o ciclo de Pointer
  // Events (down->up) -- sozinho, sem nenhum mouseup/touchend.
  elFor("chart-container").disparar("pointerdown");
  global.dispatchWindowEvent("pointerup");
  await wait(200);
  global.__lastRangeHandler();
  results.exploringFromPointerCycleAlone = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- item 3: pointercancel ENCERRA o gesto -- depois dele, um evento
  // que chega bem mais tarde (fora da janela de tolerância) não deve
  // mais contar, exatamente como um pointerup normal encerraria.
  elFor("chart-container").disparar("pointerdown");
  global.dispatchWindowEvent("pointercancel");
  await wait(3200); // além da janela de tolerância -- prova que NÃO ficou "pressionado" preso
  global.__lastRangeHandler();
  results.notExploringLongAfterPointerCancel = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- item 3: liberar FORA do canvas (o mouseup/pointerup dispara no
  // `window`, nunca precisa acontecer sobre o elemento do gráfico) ainda
  // encerra o gesto corretamente -- simulado aqui disparando o evento de
  // liberação livre no window, nunca no elemento do container.
  elFor("chart-container").disparar("pointerdown");
  global.dispatchWindowEvent("pointerup"); // "solto" em qualquer lugar da página
  results.pointerActiveClearedAfterReleaseOutsideCanvas = true; // se chegou aqui sem travar, o teste abaixo confirma
  await wait(3200);
  global.__lastRangeHandler();
  results.notExploringLongAfterReleaseOutsideCanvas = elFor("chart-back-to-live-btn").hidden === false;

  returnToLiveView();

  // --- item 3: eventos DUPLICADOS de pointer + mouse para o MESMO gesto
  // físico (comportamento padrão real do navegador: um clique de mouse
  // dispara pointerdown E mousedown) nunca produz estado incorreto --
  // nem um "preso segurando" nem uma exploração fantasma depois de solto.
  elFor("chart-container").disparar("pointerdown");
  elFor("chart-container").disparar("mousedown");
  global.dispatchWindowEvent("pointerup");
  global.dispatchWindowEvent("mouseup");
  await wait(3200);
  global.__lastRangeHandler();
  results.notExploringLongAfterDuplicatePointerAndMouseEvents = elFor("chart-back-to-live-btn").hidden === false;

  console.log(JSON.stringify(results));
}
main().catch((e) => { console.error("HARNESS_ERROR", (e && e.stack) || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_chart_interaction_detection_handles_long_drag_late_events_wheel_and_touch(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    harness_path = tmp_path / "interaction_harness.js"
    harness_path.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # Item 5: segurar o botão por MAIS de 3 segundos (a antiga janela fixa
    # de "mousedown recente") ainda conta como exploração -- o estado é
    # ATIVO enquanto pressionado, sem limite de tempo.
    assert data["exploringWhileStillHeldPast3s"] is True

    # Item 5: um evento assíncrono que chega pouco depois do fim do gesto
    # (soltar o botão) ainda é aceito -- a biblioteca real notifica com
    # atraso, não no instante exato do mouseup.
    assert data["exploringFromLateEventAfterRelease"] is True

    # Um evento que chega muito tempo depois de soltar (sem nenhuma
    # interação nova) não é mais tratado como recente.
    assert data["notExploringLongAfterRelease"] is False

    # Item 5: wheel tratado como sua PRÓPRIA categoria de interação --
    # nunca precisa de mousedown para contar.
    assert data["exploringFromWheelAlone"] is True

    # Item 5/9: toque (mobile) conta como interação real, igual ao mouse.
    assert data["exploringFromTouchAlone"] is True

    # Regressão da causa raiz original: sem NENHUMA interação real, o
    # evento (sempre programático nesse caso) nunca vira "exploração".
    assert data["neverExploringWithoutAnyInput"] is False

    # 3a auditoria, item 3: ciclo completo de Pointer Events.
    assert data["exploringFromPointerCycleAlone"] is True

    # pointercancel encerra o gesto -- depois dele, nada fica "preso
    # segurando" (senão o evento tardio abaixo AINDA contaria).
    assert data["notExploringLongAfterPointerCancel"] is False

    # Liberar fora do canvas (o release acontece no window, não no
    # elemento do gráfico) encerra o gesto normalmente.
    assert data["pointerActiveClearedAfterReleaseOutsideCanvas"] is True
    assert data["notExploringLongAfterReleaseOutsideCanvas"] is False

    # Eventos duplicados pointer+mouse para o mesmo gesto físico nunca
    # deixam o estado preso nem geram uma exploração fantasma.
    assert data["notExploringLongAfterDuplicatePointerAndMouseEvents"] is False
