"""Correção da 2a auditoria da Fase 3.6 (itens 1/2/3) e da 3a auditoria
(itens 1/2 -- título dinâmico do funil e decomposição completa
entrada/encerramento).

3a rodada, item 1: o título fixo "Por que nenhuma operação foi aberta?"
era historicamente FALSO sempre que a sessão realmente teve aberturas
aprovadas (25 aprovações históricas, 13 delas de abertura, todas com
ordem FILLED correspondente). O título agora é dinâmico, calculado a
partir de quantas entradas foram aprovadas NESTA sessão -- e o painel
mostra separadamente sinais recebidos, avaliações de entrada/aprovadas/
recusadas, ordens de abertura executadas, avaliações de encerramento/
aprovadas/recusadas, ordens de encerramento executadas e posições
abertas -- NUNCA misturando a população de entrada com a de encerramento
em uma única contagem ou um único motivo dominante.

3a rodada, item 2: a antiga amostra fixa de 200 registros ("15 de 200")
comparava uma fração não identificada do histórico contra o total de 565
avaliações -- populações incompatíveis. Agora a busca de /api/risk-
evaluations é DIMENSIONADA pelo total canônico (rejection_reasons.
evaluations_total, de /api/metrics): sempre que o histórico cabe dentro
do teto de segurança (WHY_NO_TRADE_EVAL_FETCH_CAP), a "amostra" passa a
ser 100% do histórico. Este arquivo testa os dois regimes: histórico
completo (evaluations_total <= teto) e amostra genuína, claramente
rotulada (evaluations_total > teto).
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


_APP_JS_SOURCE = APP_JS.read_text(encoding="utf-8")


def test_why_no_trades_never_reads_totals_from_the_paginated_risk_evaluations_endpoint():
    """Estático: garante que o total de sinais/avaliações continua vindo
    de /api/metrics (canônico), nunca inventado a partir do tamanho de
    uma lista paginada."""
    assert "getJSON(\"/api/metrics\")" in _APP_JS_SOURCE
    assert "signalCounts.actionable_signals_total" in _APP_JS_SOURCE
    assert "rejectionInfo.evaluations_total" in _APP_JS_SOURCE


def test_why_no_trades_explains_shadow_and_other_types_explicitly():
    assert "o laboratório nunca grava nesta tabela" in _APP_JS_SOURCE
    assert "ai_recommendations" in _APP_JS_SOURCE


def test_why_no_trades_fetch_size_is_dimensioned_by_the_canonical_total():
    """3a rodada, item 2: a busca não usa mais um número fixo (o antigo
    ?limit=200) -- o tamanho vem do total canônico já conhecido."""
    refresh_fn_source = _APP_JS_SOURCE[_APP_JS_SOURCE.index("async function refreshWhyNoTrades"):]
    refresh_fn_source = refresh_fn_source[: refresh_fn_source.index("\nasync function refreshAll")]
    assert "risk-evaluations?limit=200" not in refresh_fn_source
    assert "evalFetchLimit" in _APP_JS_SOURCE
    assert "WHY_NO_TRADE_EVAL_FETCH_CAP" in _APP_JS_SOURCE
    assert "isCompleteHistory" in _APP_JS_SOURCE


def test_why_no_trades_never_mixes_entry_and_close_populations():
    """3a rodada, item 1: cada contagem/motivo dominante é rotulado
    explicitamente como de entrada OU de encerramento -- nunca ambos
    somados numa única linha."""
    assert "Motivo dominante de recusa de entrada" in _APP_JS_SOURCE
    assert "Motivo dominante de recusa de encerramento" in _APP_JS_SOURCE
    assert "Ordens de abertura executadas" in _APP_JS_SOURCE
    assert "Ordens de encerramento executadas" in _APP_JS_SOURCE


# --- harness dinâmico: gera o driver Node a partir de fixtures Python ------

_HARNESS_TEMPLATE = r"""
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
  querySelector() { return new FakeElement("tbody"); }
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
process.on("unhandledRejection", () => {});

const FIXTURE = __FIXTURE_JSON__;

global.fetch = (url) => {
  if (url.includes("/api/metrics")) return Promise.resolve({ json: () => Promise.resolve(FIXTURE.metrics) });
  if (url.includes("/api/session")) return Promise.resolve({ json: () => Promise.resolve(FIXTURE.session) });
  if (url.includes("/api/risk-evaluations")) return Promise.resolve({ json: () => Promise.resolve(FIXTURE.evals) });
  if (url.includes("/api/orders")) return Promise.resolve({ json: () => Promise.resolve(FIXTURE.orders) });
  if (url.includes("/api/positions")) return Promise.resolve({ json: () => Promise.resolve(FIXTURE.positions) });
  return Promise.resolve({ json: () => Promise.resolve({}) });
};

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

async function main() {
  await refreshWhyNoTrades();
  const box = elFor("why-no-trade-box");
  const title = elFor("why-no-trade-title").textContent;
  const rows = {};
  box.children.forEach((child) => {
    if (child.children && child.children.length === 2) {
      rows[child.children[0].textContent] = child.children[1].textContent;
    }
  });
  const paragraphs = box.children.filter((c) => !(c.children && c.children.length === 2)).map((c) => c.textContent);
  console.log(JSON.stringify({ title, rows, paragraphs }));
}
main().catch((e) => { console.error("HARNESS_ERROR", (e && e.stack) || e); process.exit(1); });
"""


def _run_harness(tmp_path, fixture: dict) -> dict:
    script_path = _app_js_without_bootstrap(tmp_path)
    harness = _HARNESS_TEMPLATE.replace("__FIXTURE_JSON__", json.dumps(fixture))
    harness_path = tmp_path / "why_no_trades_harness.js"
    harness_path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _eval(id_, approved, is_entry, when_ms, applied=True):
    checks = {"cost_gate": {"applied": applied}} if is_entry else {"position_exists": True}
    if not approved:
        # Uma avaliação recusada precisa de EXATAMENTE uma chave `false`
        # em `checks`, igual ao motor de risco real grava.
        checks = {"cost_gate": {"applied": applied}, "cost_coverage_ok": False} if is_entry \
            else {"position_exists": False}
    return {
        "id": id_, "approved": approved, "checks": checks,
        "created_at": __import__("datetime").datetime.fromtimestamp(when_ms / 1000, __import__("datetime").timezone.utc).isoformat(),
    }


def _order(id_, is_close, status="FILLED", when_ms=3000):
    created_at = __import__("datetime").datetime.fromtimestamp(
        when_ms / 1000, __import__("datetime").timezone.utc
    ).isoformat()
    return {
        "id": id_, "is_close": is_close, "status": status, "symbol": "BTCUSDT", "side": "BUY",
        "created_at": created_at,
    }


SESSION_STARTED_MS = 2000


def _base_fixture(evals, orders, positions=None, evaluations_total=None, approved_total=None):
    approved = [e for e in evals if e["approved"]]
    entry_approved = [e for e in approved if "cost_gate" in e["checks"]]
    close_approved = [e for e in approved if "cost_gate" not in e["checks"]]
    return {
        "metrics": {
            "signal_counts": {"actionable_signals_total": len(evals)},
            "rejection_reasons": {
                "evaluations_total": evaluations_total if evaluations_total is not None else len(evals),
                "approved_total": approved_total if approved_total is not None else len(approved),
            },
            "cost_gate": {"avg_coverage_ratio_at_entry": 1.23, "coverage_distribution": {"max": 2.95, "required_ratio": 1.5}},
        },
        "session": {
            "session_uid": "abcdef1234567890",
            "started_at": __import__("datetime").datetime.fromtimestamp(SESSION_STARTED_MS / 1000, __import__("datetime").timezone.utc).isoformat(),
            "orders_count": len(orders),
        },
        "evals": evals,
        "orders": orders,
        "positions": positions if positions is not None else [],
    }


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_dynamic_title_shows_zero_operations_phrasing_when_no_open_order_was_executed(tmp_path):
    """Zero ORDENS DE ABERTURA EXECUTADAS -- mesmo com avaliações
    aprovadas na sessão -- ainda mostra o título de "nenhuma operação"."""
    evals = [
        _eval(1, True, is_entry=False, when_ms=3000),  # encerramento aprovado -- nunca conta p/ título
        _eval(2, False, is_entry=True, when_ms=3000),  # entrada recusada
    ]
    data = _run_harness(tmp_path, _base_fixture(evals, orders=[_order(1, is_close=True, when_ms=3000)]))
    assert data["title"] == "Por que nenhuma operação foi aberta nesta sessão?"


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_dynamic_title_stays_zero_operations_when_entry_is_approved_but_no_order_was_executed(tmp_path):
    """Correção pós-auditoria (4a rodada, item 1): uma aprovação de
    entrada, SOZINHA, nunca mais decide o título -- só a ORDEM EXECUTADA
    conta. Aqui a entrada foi aprovada, mas nenhuma ordem de abertura
    aparece na lista (ex.: falhou na submissão à corretora depois de
    aprovada) -- o título tem que continuar dizendo "nenhuma operação",
    preservando a verdade exigida pela auditoria."""
    evals = [_eval(1, True, is_entry=True, when_ms=3000)]  # entrada aprovada
    data = _run_harness(tmp_path, _base_fixture(evals, orders=[]))  # nenhuma ordem executada
    assert data["title"] == "Por que nenhuma operação foi aberta nesta sessão?"
    # "Entradas aprovadas" continua aparecendo separadamente (1), mesmo
    # o título dizendo "nenhuma operação" -- nunca removido.
    assert data["rows"]["Entradas aprovadas"] == "1"
    assert data["rows"]["Ordens de abertura executadas (status EXECUTADA)"] == "0"


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_dynamic_title_shows_few_operations_phrasing_for_one_or_two_executed_open_orders(tmp_path):
    evals = [_eval(1, True, is_entry=True, when_ms=3000), _eval(2, True, is_entry=True, when_ms=3000)]
    orders = [_order(1, is_close=False, when_ms=3000), _order(2, is_close=False, when_ms=3000)]
    data = _run_harness(tmp_path, _base_fixture(evals, orders=orders))
    assert data["title"] == "Por que poucas operações foram abertas nesta sessão?"


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_dynamic_title_shows_generic_funnel_title_when_three_or_more_open_orders_executed(tmp_path):
    evals = [_eval(i, True, is_entry=True, when_ms=3000) for i in range(1, 4)]
    orders = [_order(i, is_close=False, when_ms=3000) for i in range(1, 4)]
    data = _run_harness(tmp_path, _base_fixture(evals, orders=orders))
    assert data["title"] == "Funil de decisões e operações"
    assert "nenhuma operação" not in data["title"].lower()


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_close_orders_never_count_toward_the_open_operations_title(tmp_path):
    """Ordens de ENCERRAMENTO (fechar uma posição) nunca contam como
    operações ABERTAS -- mesmo havendo várias, o título deve continuar
    dizendo "nenhuma operação" se nenhuma ordem de ABERTURA foi
    executada nesta sessão."""
    evals = [_eval(i, True, is_entry=False, when_ms=3000) for i in range(1, 5)]  # 4 encerramentos aprovados
    orders = [_order(i, is_close=True, when_ms=3000) for i in range(1, 5)]  # 4 ordens de encerramento executadas
    data = _run_harness(tmp_path, _base_fixture(evals, orders=orders))
    assert data["title"] == "Por que nenhuma operação foi aberta nesta sessão?"
    assert data["rows"]["Ordens de encerramento executadas (status EXECUTADA)"] == "4"
    assert data["rows"]["Ordens de abertura executadas (status EXECUTADA)"] == "0"


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_full_decomposition_never_mixes_entry_and_close_and_matches_real_25_shape(tmp_path):
    """Reproduz a FORMA real dos dados de produção (13 abertura + 12
    encerramento, todas com ordem FILLED correspondente) e prova que
    cada contagem cai na população certa, sem mistura."""
    evals = []
    orders = []
    eid = 1
    for i in range(13):
        evals.append(_eval(eid, True, is_entry=True, when_ms=3000 + i))
        orders.append(_order(eid, is_close=False))
        eid += 1
    for i in range(12):
        evals.append(_eval(eid, True, is_entry=False, when_ms=3000 + i))
        orders.append(_order(eid, is_close=True))
        eid += 1
    # mais avaliações rejeitadas, de ambos os tipos, para provar que
    # elas não vazam para a contagem de aprovadas nem se misturam entre si.
    evals.append(_eval(eid, False, is_entry=True, when_ms=3000)); eid += 1
    evals.append(_eval(eid, False, is_entry=False, when_ms=3000)); eid += 1

    data = _run_harness(tmp_path, _base_fixture(evals, orders, positions=[{"symbol": "BTCUSDT"}]))
    rows = data["rows"]

    assert rows["Avaliações de entrada"] == "14"  # 13 aprovadas + 1 recusada
    assert rows["Entradas aprovadas"] == "13"
    assert rows["Entradas recusadas"] == "1"
    assert rows["Ordens de abertura executadas (status EXECUTADA)"] == "13"

    assert rows["Avaliações de encerramento"] == "13"  # 12 aprovadas + 1 recusada
    assert rows["Encerramentos aprovados"] == "12"
    assert rows["Encerramentos recusados"] == "1"
    assert rows["Ordens de encerramento executadas (status EXECUTADA)"] == "12"

    scope_text = " ".join(data["paragraphs"])
    assert "1 posição" in scope_text
    # Com 27 avaliações e teto de segurança bem maior, isto é 100% do
    # histórico -- nunca uma amostra não identificada.
    assert "100% do histórico" in scope_text
    assert "nenhuma amostragem" in scope_text


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_genuine_sample_is_labeled_explicitly_when_history_exceeds_the_safety_cap(tmp_path):
    """Quando o histórico realmente excede o teto de segurança, o painel
    precisa avisar isso -- nunca apresentar uma fração do histórico como
    se fosse o todo (o defeito original que motivou toda esta correção)."""
    evals = [_eval(1, True, is_entry=True, when_ms=3000)]
    fixture = _base_fixture(evals, orders=[_order(1, is_close=False)], evaluations_total=999999)
    data = _run_harness(tmp_path, fixture)
    scope_text = " ".join(data["paragraphs"])
    assert "amostra dos" in scope_text
    assert "999999" in scope_text or "999.999" in scope_text
    assert "100% do histórico" not in scope_text


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_dominant_rejection_reason_is_never_shared_between_entry_and_close(tmp_path):
    evals = [
        _eval(1, False, is_entry=True, when_ms=3000),   # entrada recusada -- cost_coverage_ok
        _eval(2, False, is_entry=True, when_ms=3000),   # entrada recusada -- cost_coverage_ok
        _eval(3, False, is_entry=False, when_ms=3000),  # encerramento recusado -- position_exists
    ]
    data = _run_harness(tmp_path, _base_fixture(evals, orders=[]))
    rows = data["rows"]
    assert "cost_coverage_ok" in rows["Motivo dominante de recusa de entrada"] \
        or "Cobertura de custo" in rows["Motivo dominante de recusa de entrada"]
    assert "posição aberta" in rows["Motivo dominante de recusa de encerramento"].lower() \
        or "position_exists" in rows["Motivo dominante de recusa de encerramento"]
    # nunca o motivo de um tipo aparece na linha do outro
    assert rows["Motivo dominante de recusa de entrada"] != rows["Motivo dominante de recusa de encerramento"]


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_shadow_lab_approvals_always_reported_as_zero_by_construction(tmp_path):
    evals = [_eval(1, True, is_entry=True, when_ms=3000)]
    data = _run_harness(tmp_path, _base_fixture(evals, orders=[_order(1, is_close=False)]))
    assert "0" in data["rows"]["Aprovações do Laboratório de Estratégias (Shadow Mode)"]
