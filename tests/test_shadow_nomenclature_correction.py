"""Correção da 2a auditoria da Fase 3.6.

Item 1: `frontend/shadow.html` tinha um rótulo com o texto FIXO "limiar
operacional 3,0×" na marcação estática -- nunca lido de nenhum dado,
nunca atualizado. Isso divergia de qualquer configuração real diferente
do valor original que motivou o texto (a V2 operacional roda com
MINIMUM_COST_COVERAGE_RATIO=1.5) -- não porque o painel calculasse
errado, mas porque esse rótulo específico nunca consultava o valor real
nenhuma vez. A correção faz o badge ler `cg.limiar_operacional` (o mesmo
campo, vindo do mesmo /api/diagnostics/dashboard, já usado na tabela de
distribuição logo abaixo) sempre que a seção é renderizada.

Item 6: nomenclaturas do brief (COMPRA/VENDA, Slippage, Payoff, Profit
Factor, Drawdown, Expectancy, MFE/MAE, Baseline/H2, Risk Engine, Cost
Gate, cooldown) aplicadas também em shadow.html, que tinha ficado de fora
da Fase 3.6 original (só index.html/app.js foram tocados na 1a rodada).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
SHADOW_HTML = FRONTEND_DIR / "shadow.html"
NODE = shutil.which("node")

_SHADOW_SOURCE = SHADOW_HTML.read_text(encoding="utf-8")


def test_cost_coverage_threshold_badge_is_no_longer_a_hardcoded_literal():
    assert "limiar operacional 3,0×" not in _SHADOW_SOURCE, (
        "o badge do limiar operacional não pode mais ser um texto fixo -- "
        "precisa ler cg.limiar_operacional ao vivo."
    )
    assert 'id="badge-limiar-operacional"' in _SHADOW_SOURCE
    assert "badge-limiar-operacional" in _SHADOW_SOURCE.replace(
        'id="badge-limiar-operacional"', "", 1
    ), "o badge precisa ser atualizado via JS (getElementById), não só declarado no HTML"


@pytest.mark.parametrize(
    "must_contain",
    [
        'DIRECTION_LABELS = { BUY: "COMPRA", SELL: "VENDA", HOLD: "AGUARDAR" }',
        "Diferença entre preço esperado e executado (Slippage)",
        "Ganho médio comparado à perda média (Payoff)",
        "Relação entre ganhos e perdas (Profit Factor)",
        "Resultado médio por operação (Expectancy)",
        "Maior queda do patrimônio (Drawdown)",
        "Maior movimento favorável até agora (MFE)",
        "Maior movimento contrário até agora (MAE)",
        "Comparação sem filtro de custos (Baseline)",
        "Modelo experimental: força do cruzamento (H2",
        "Em período de espera após perdas (cooldown)",
        "Capital em posições no limite (Exposição)",
        "Valor da ordem abaixo do mínimo permitido",
        "Laboratório de Estratégias (Shadow Mode)",
    ],
)
def test_shadow_html_nomenclature_matches_brief(must_contain):
    assert must_contain in _SHADOW_SOURCE, (
        f"nomenclatura exigida pelo brief não encontrada em shadow.html: {must_contain!r}"
    )


def _extract_render_functions(html: str) -> str:
    script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    cut = script.index("ciclo de atualizacao")
    line_start = script.rfind("\n", 0, cut)
    functions = script[:line_start]
    return re.sub(r"""['"]use strict['"];""", "", functions, count=1)


_DRIVER = r"""
class FakeElement {
  constructor(tag) {
    this.tag = tag; this.children = []; this._text = ""; this.className = "";
    this.dataset = {}; this.attrs = {};
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join("");
    return this._text;
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
}
const registry = {};
function elById(id) { if (!registry[id]) registry[id] = new FakeElement("div"); return registry[id]; }
global.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (v) => ({ nodeType: 3, textContent: String(v) }),
  getElementById: elById,
  hidden: false,
};
global.window = global;

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

// item 1: o badge lê o MESMO valor ao vivo (nunca um literal fixo) --
// aqui simulado como 1.5x, o valor real configurado na V2 operacional.
renderCobertura({
  n: 4, minimo: 0.8, mediana: 1.1, maximo: 2.95, p90: 2.0, p95: 2.5,
  limiar_operacional: 1.5,
  distribuicao_por_faixa: { abaixo_1_0x: 1, "1_0_a_1_5x": 1, "1_5_a_2_0x": 1, "2_0_a_3_0x": 1, acima_3_0x: 0 },
  nota: "nota de teste",
});
console.log(JSON.stringify({ badgeText: elById("badge-limiar-operacional").textContent }));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_cost_coverage_badge_reflects_live_config_value(tmp_path):
    functions = _extract_render_functions(_SHADOW_SOURCE)
    script_path = tmp_path / "shadow_render_funcs.js"
    script_path.write_text(functions, encoding="utf-8")
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_DRIVER, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(driver_path), str(script_path)], capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # O valor exibido precisa ser o 1.5x passado dinamicamente -- nunca
    # mais o antigo "3,0×" fixo, e nunca inventado.
    assert "1,5×" in data["badgeText"]
    assert "3,0" not in data["badgeText"]
