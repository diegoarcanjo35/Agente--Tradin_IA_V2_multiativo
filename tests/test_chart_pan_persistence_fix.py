"""Fase 3.6.1 -- hotfix do pan horizontal do gráfico, comprovado quebrado em
navegador real com a biblioteca Lightweight Charts genuína (não em mocks):
todo o histórico de testes anteriores (incluindo
test_chart_interaction_correction.py) provava apenas que os EVENTOS de
ponteiro chegavam corretamente ao container e que `CHART_STATE.followLive`
mudava de estado -- nunca que a JANELA VISÍVEL do gráfico permanecia onde o
usuário a deixou. Nenhum desses testes jamais simulou:

  (a) o histórico total de candles ultrapassando o `limit` de
      `/api/chart-data` (1500) -- cenário normal depois de a V2 rodar por
      mais de ~25h contínuas -- fazendo a janela devolvida pelo backend
      DESLIZAR (o candle mais antigo cai fora a cada atualização); nem
  (b) o comportamento REAL de `series.setData()`/`shiftVisibleRangeOnNewBar`
      do Lightweight Charts genuíno, que nenhum mock reproduz.

Diagnóstico em navegador real (instância REPLAY descartável com histórico
sintético > 1500 candles, alimentação contínua): confirmado por
instrumentação direta de `candleSeries.setData`/`candleSeries.update` que,
uma vez ultrapassado o limite de 1500, TODO ciclo de refresh chamava
`setData()` (9 vezes em 9s, `update()` zero vezes) -- causa raiz: a
comparação POR POSIÇÃO/ÍNDICE em `applyCandlesToSeries()`, que falha assim
que a janela desliza. Uma segunda causa raiz, também comprovada em
navegador real, foi encontrada depois de eliminar a primeira:
`shiftVisibleRangeOnNewBar` (`true` por padrão no Lightweight Charts)
desloca sozinho o range visível a cada candle novo, mesmo via `update()`
puro.

Rodada 2 (endurecimento, este arquivo já reflete a versão final): "há
sobreposição" sozinho era permissivo demais -- não distinguia um
deslizamento normal de uma DIVERGÊNCIA ESTRUTURAL real (lacuna interna,
candle histórico removido, OHLCV histórico reescrito, timestamp duplicado
ou fora de ordem, candle inserido no meio de uma série já conhecida).
`classifyCandlesUpdate()` agora decide isso com uma função pura,
testável isoladamente (sem qualquer mock do Lightweight Charts); quando
uma divergência real força `setData()`, `resetSeriesPreservingView()`
captura o intervalo de tempo visível ANTES (só quando o usuário está
explorando -- `followLive === false`) e o restaura exatamente depois,
para que mesmo um reset genuinamente necessário nunca mova a posição do
usuário.

Prova funcional (fora deste arquivo, com a biblioteca real, registrada no
relatório da Fase 3.6.1): drag real -> `followLive=false`, range idêntico
byte a byte por 4+ ciclos de poll com histórico crescendo além do limite
de 1500 (0 chamadas a `setData()`), "Voltar ao tempo atual" restabelece o
acompanhamento, liberação fora do canvas nunca deixa `pointerActive`
travado, funciona em desktop e mobile. Esse teste funcional com a
biblioteca genuína não é reproduzível de forma determinística num harness
Node e está registrado como evidência manual no relatório, não como teste
automatizado substituto -- este arquivo cobre os MECANISMOS (a lógica
pura de classificação e a opção de criação do chart) isoladamente.
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


def _app_js_without_bootstrap(tmp_path: Path) -> Path:
    code = APP_JS.read_text(encoding="utf-8")
    trimmed = code[: code.index(_BOOTSTRAP_MARKER)]
    out = tmp_path / "app_no_bootstrap.js"
    out.write_text(trimmed, encoding="utf-8")
    return out


def _run_node(script_path: Path, harness_src: str, tmp_path: Path) -> dict:
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(harness_src, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


_MINIMAL_DOM_PRELUDE = r"""
global.window = global;
function fakeElement() {
  return {
    addEventListener() {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {}, dataset: {}, children: [], appendChild(c) { return c; }, querySelectorAll: () => [],
  };
}
global.document = {
  addEventListener() {}, hidden: false,
  getElementById: () => fakeElement(),
  querySelectorAll: () => [],
  createElement: () => fakeElement(),
};
global.LightweightCharts = undefined;
global.setInterval = () => {};
global.ResizeObserver = class { observe() {} disconnect() {} };
process.on("unhandledRejection", () => {});

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
"""

# `let`/`const` de uma direct eval() NUNCA vazam para fora dessa chamada de
# eval (ao contrário de `function`/`var`, que vazam) -- então qualquer
# harness que precise ler/escrever `CHART_STATE` (declarado com `const` em
# app.js) precisa concatenar seu próprio corpo com `code` e avaliar tudo
# NUMA ÚNICA chamada de eval(), nunca em um eval(code) seguido de
# instruções separadas.
def _eval_with_body(body_js: str) -> str:
    return f"eval(code + {json.dumps(body_js)});"


# ---------------------------------------------------------------------------
# classifyCandlesUpdate(): função pura, um caso por cenário estrutural.
# Roda uma ÚNICA vez num processo Node (fixture de módulo) -- cada caso
# vira um teste pytest independente via parametrize, lendo do resultado
# já computado (sem reabrir o Node por caso).
# ---------------------------------------------------------------------------

_CLASSIFY_HARNESS = _MINIMAL_DOM_PRELUDE + _eval_with_body(r"""
function candle(t, extra) { return Object.assign({ time: t, open: 1, high: 2, low: 0, close: 1 }, extra || {}); }

const out = {};

// 1. Primeira carga: prevMapped vazio -> reset (não é uma "divergência",
// é o caminho de inicialização).
out.first_load = classifyCandlesUpdate([], [candle(1), candle(2), candle(3)]);

// 2. Operação contínua normal, histórico ainda dentro do limite: só um
// candle novo no topo.
out.simple_append = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(1), candle(2), candle(3), candle(4)],
);

// 3. O cenário do defeito original: histórico já no limite (ex. 1500),
// candle novo chega e o mais antigo cai fora -- MESMO comprimento, janela
// inteira deslizada +1. Precisa continuar sendo update(), nunca reset.
out.sliding_window_at_cap = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(2), candle(3), candle(4)],
);

// 4. Janela desliza por VÁRIAS posições de uma vez (ex. processo ficou
// alguns ciclos sem poder atualizar) -- ainda update(), nunca reset,
// enquanto a sobreposição continuar coerente.
out.sliding_window_multi_step = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(3), candle(4), candle(5), candle(6)],
);

// 5. Candle mais recente ainda em formação mudando (mesmo time no topo,
// OHLC diferente) -- update(), nunca reset (é o único candle que pode
// legitimamente mudar sem ser tratado como divergência).
out.forming_candle_update = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(1), candle(2), candle(3, { high: 5, close: 4 })],
);

// 6. Resposta vazia nunca apaga um gráfico já populado -- nem reset, nem
// update(), simplesmente não faz nada.
out.empty_response = classifyCandlesUpdate([candle(1), candle(2), candle(3)], []);

// 7. Lacuna GENUÍNA -- zero sobreposição entre o que já estava desenhado
// e o que chegou (ex. processo ficou parado tempo suficiente para o
// candle mais antigo conhecido sair de vez da janela). Reset correto.
out.genuine_gap_no_overlap = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(10), candle(11), candle(12)],
);

// 8. LACUNA NO MEIO de uma janela QUE AINDA TEM sobreposição nas pontas:
// candle(2) desapareceu do meio, mas candle(1) e candle(3) continuam
// presentes -- não é mero deslizamento pela borda, é um buraco interno.
// Reset necessário (nunca deixar o buraco silenciosamente como se fosse
// update()).
out.internal_gap_with_overlap = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3), candle(4)],
  [candle(1), candle(3), candle(4), candle(5)],
);

// 9. Candle histórico REMOVIDO (a série encolheu no meio, sem inserir
// nada no lugar) -- mesmo teste que o 8 sob outro ângulo, cobre
// explicitamente "candle histórico removido" do brief.
out.historical_candle_removed = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(1), candle(3)],
);

// 10. OHLCV HISTÓRICO CORRIGIDO -- candle(2) continua no mesmo lugar,
// mesma posição relativa, mas seu close mudou (não é o último candle,
// então não pode ser "ainda em formação"). Reset necessário -- nunca
// aplicar um update() que deixaria o candle antigo errado na tela.
out.historical_ohlcv_corrected = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3), candle(4)],
  [candle(1, {}), candle(2, { close: 999 }), candle(3), candle(4), candle(5)],
);

// 11. Candle NOVO inserido no MEIO de uma série já conhecida (não no
// topo) -- ex. um candle atrasado que chegou fora de ordem foi
// processado e persistido entre dois já vistos. Reset necessário.
out.candle_inserted_in_middle = classifyCandlesUpdate(
  [candle(1), candle(3), candle(4)],
  [candle(1), candle(2), candle(3), candle(4), candle(5)],
);

// 12. Timestamp DUPLICADO na própria resposta nova -- nunca aplicado via
// update() (poderia corromper a série); força reset explícito.
out.duplicate_timestamp = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(1), candle(2), candle(3), candle(4), candle(4)],
);

// 13. Timestamps FORA DE ORDEM na resposta nova -- mesma proteção.
out.out_of_order_timestamp = classifyCandlesUpdate(
  [candle(1), candle(2), candle(3)],
  [candle(1), candle(2), candle(4), candle(3)],
);

console.log(JSON.stringify(out));
""")


@pytest.fixture(scope="module")
def classify_cases(tmp_path_factory) -> dict:
    if NODE is None:
        pytest.skip("Node.js not available in this environment")
    tmp_path = tmp_path_factory.mktemp("classify_candles_update")
    script_path = _app_js_without_bootstrap(tmp_path)
    return _run_node(script_path, _CLASSIFY_HARNESS, tmp_path)


@pytest.mark.parametrize(
    "case, expect_reset, expect_update_times",
    [
        ("first_load", True, None),
        ("simple_append", False, [3, 4]),
        ("sliding_window_at_cap", False, [3, 4]),
        ("sliding_window_multi_step", False, [3, 4, 5, 6]),
        ("forming_candle_update", False, [3]),
        ("empty_response", False, []),
    ],
)
def test_classify_candles_update_normal_cases(classify_cases, case, expect_reset, expect_update_times):
    result = classify_cases[case]
    assert result["reset"] is expect_reset, f"{case}: {result}"
    if not expect_reset:
        assert [u["time"] for u in result["updates"]] == expect_update_times, f"{case}: {result}"


@pytest.mark.parametrize(
    "case",
    [
        "genuine_gap_no_overlap",
        "internal_gap_with_overlap",
        "historical_candle_removed",
        "historical_ohlcv_corrected",
        "candle_inserted_in_middle",
        "duplicate_timestamp",
        "out_of_order_timestamp",
    ],
)
def test_classify_candles_update_detects_structural_divergence(classify_cases, case):
    result = classify_cases[case]
    assert result["reset"] is True, (
        f"{case}: divergência estrutural real deveria forçar reset (setData()), "
        f"nunca update() silencioso com dado incoerente -- resultado: {result}"
    )
    assert result["reason"], f"{case}: motivo do reset não pode ficar vazio -- {result}"


# ---------------------------------------------------------------------------
# resetSeriesPreservingView(): setData() por divergência real nunca pode
# mover a posição do usuário durante exploração; em modo ao vivo, nada é
# restaurado aqui (o próprio refreshChart() reancora a visão ao vivo).
# ---------------------------------------------------------------------------

_RESET_VIEW_HARNESS = _MINIMAL_DOM_PRELUDE + _eval_with_body(r"""
function candle(t, extra) { return Object.assign({ time: t, open: 1, high: 2, low: 0, close: 1 }, extra || {}); }

function makeFakeChartAndSeries(initialRange) {
  let visibleRange = initialRange;
  const series = {
    setDataCalls: 0, updateCalls: 0, lastSetData: null,
    setData(d) { this.setDataCalls++; this.lastSetData = d; },
    update(d) { this.updateCalls++; },
  };
  const chart = {
    timeScale() {
      return {
        getVisibleRange() { return visibleRange; },
        setVisibleRange(r) { visibleRange = r; },
      };
    },
    _getRange: () => visibleRange,
  };
  return { series, chart };
}

const out = {};

// (a) Divergência estrutural real ENQUANTO o usuário explora o
// histórico: setData() é necessário, mas o range visível precisa voltar
// a ser EXATAMENTE o mesmo depois.
{
  CHART_STATE.followLive = false;
  const exploredRange = { from: 500, to: 800 };
  const { series, chart } = makeFakeChartAndSeries(exploredRange);
  const prev = [candle(1), candle(2), candle(3)];
  const next = [candle(1, {}), candle(2, { close: 999 }), candle(3), candle(4)]; // OHLCV histórico alterado
  applyCandlesToSeries(series, (c) => c, prev, next, chart);
  out.reset_while_exploring = {
    setDataCalls: series.setDataCalls,
    rangeAfter: chart._getRange(),
    rangeUnchanged: JSON.stringify(chart._getRange()) === JSON.stringify(exploredRange),
  };
}

// (b) Mesma divergência, mas em modo AO VIVO (followLive=true): nenhuma
// tentativa de capturar/restaurar range aqui -- quem reancora é o
// refreshChart() logo depois, fora desta função.
{
  CHART_STATE.followLive = true;
  const { series, chart } = makeFakeChartAndSeries({ from: 100, to: 200 });
  const prev = [candle(1), candle(2), candle(3)];
  const next = [candle(1, {}), candle(2, { close: 999 }), candle(3), candle(4)];
  applyCandlesToSeries(series, (c) => c, prev, next, chart);
  out.reset_while_live = {
    setDataCalls: series.setDataCalls,
    // getVisibleRange() nunca foi chamado para capturar -- o range
    // "vazou" para o valor inicial do mock, sem nenhuma tentativa de
    // setVisibleRange() feita por resetSeriesPreservingView() aqui.
    rangeStayedAtMockInitial: JSON.stringify(chart._getRange()) === JSON.stringify({ from: 100, to: 200 }),
  };
}

// (c) Deslizamento normal (não é divergência) nunca toca o range, com ou
// sem chart -- update() puro, setVisibleRange() nunca chamado.
{
  CHART_STATE.followLive = false;
  const exploredRange = { from: 500, to: 800 };
  const { series, chart } = makeFakeChartAndSeries(exploredRange);
  const prev = [candle(1), candle(2), candle(3)];
  const next = [candle(2), candle(3), candle(4)]; // deslizou, mesma base
  applyCandlesToSeries(series, (c) => c, prev, next, chart);
  out.normal_slide_never_touches_range = {
    setDataCalls: series.setDataCalls, updateCalls: series.updateCalls,
    rangeUnchanged: JSON.stringify(chart._getRange()) === JSON.stringify(exploredRange),
  };
}

console.log(JSON.stringify(out));
""")


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_setdata_by_structural_divergence_preserves_exploration_range(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    out = _run_node(script_path, _RESET_VIEW_HARNESS, tmp_path)

    assert out["reset_while_exploring"]["setDataCalls"] == 1
    assert out["reset_while_exploring"]["rangeUnchanged"] is True, (
        "setData() por divergência estrutural real nunca pode mover a "
        "posição do usuário durante exploração -- precisa capturar o "
        "range antes e restaurar exatamente o mesmo depois"
    )


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_setdata_by_structural_divergence_in_live_mode_does_not_force_a_range(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    out = _run_node(script_path, _RESET_VIEW_HARNESS, tmp_path)

    assert out["reset_while_live"]["setDataCalls"] == 1
    assert out["reset_while_live"]["rangeStayedAtMockInitial"] is True, (
        "em modo ao vivo, resetSeriesPreservingView() não deve tentar "
        "capturar/restaurar range nenhum -- quem reancora a visão ao "
        "vivo é o refreshChart(), logo depois, normalmente"
    )


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_normal_sliding_window_never_touches_the_visible_range(tmp_path):
    script_path = _app_js_without_bootstrap(tmp_path)
    out = _run_node(script_path, _RESET_VIEW_HARNESS, tmp_path)

    row = out["normal_slide_never_touches_range"]
    # update() é chamado para o candle novo (4) e para o último já
    # conhecido (3, re-aplicado -- necessário para refletir um candle em
    # formação que mudou).
    assert row == {"setDataCalls": 0, "updateCalls": 2, "rangeUnchanged": True}


# ---------------------------------------------------------------------------
# Opção de criação do chart: shiftVisibleRangeOnNewBar precisa estar
# explicitamente desativado (segunda causa raiz, comprovada em navegador
# real).
# ---------------------------------------------------------------------------

def test_chart_created_with_shift_visible_range_on_new_bar_disabled():
    source = APP_JS.read_text(encoding="utf-8")
    idx = source.index("LightweightCharts.createChart(")
    call_block = source[idx: idx + 2000]
    ts_idx = call_block.index("timeScale:")
    ts_block = call_block[ts_idx: ts_idx + 400]
    assert "shiftVisibleRangeOnNewBar: false" in ts_block, (
        "createChart() precisa desativar explicitamente "
        "shiftVisibleRangeOnNewBar -- o padrão da biblioteca (true) desloca "
        "o range visível a cada candle novo, inclusive durante exploração "
        "manual do histórico, mesmo sem nenhuma chamada a setData()"
    )


# ---------------------------------------------------------------------------
# Proteção contra overlay interceptando o canvas (item explícito do
# diagnóstico) -- os únicos elementos posicionados por cima do canvas do
# Lightweight Charts (tooltip customizado, faixas de posição) precisam ser
# estruturalmente incapazes de capturar cliques/arrastes destinados ao
# gráfico.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("selector", [".chart-tooltip", ".chart-price-zone"])
def test_chart_overlays_never_intercept_pointer_events(selector):
    css = STYLES_CSS.read_text(encoding="utf-8")
    idx = css.index(selector + " {")
    block = css[idx: css.index("}", idx)]
    assert "pointer-events: none" in block, (
        f"{selector} sobrepõe o canvas do gráfico e precisa de "
        "pointer-events: none -- caso contrário intercepta o drag/clique "
        "destinado à biblioteca de gráfico por baixo"
    )
