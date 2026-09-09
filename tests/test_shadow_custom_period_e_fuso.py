"""Fase 3.5 — correções finais antes da regressão: testes direcionados do
escopo "Período personalizado" (custom) e da rotulagem inequívoca de fuso
horário no painel `frontend/shadow.html`.

Mesmo padrão de `tests/test_shadow_panel_xss_safety.py`: extrai o próprio
`<script>` do arquivo real e executa as funções de verdade num shim de DOM
em Node.js — nunca reimplementa a lógica em Python.

Cobre, ponto a ponto, a exigência da auditoria:
  1. Campos incompletos NUNCA disparam a requisição (bloqueio total).
  2. início >= fim é rejeitado, também sem disparar requisição.
  3. Período válido dispara EXATAMENTE UMA requisição consolidada
     (`/api/diagnostics/dashboard`), com `since`/`until` corretos.
  4. Selecionar "Período personalizado" no <select>, sozinho, não dispara
     nenhuma requisição (os campos começam vazios) -- só o clique em
     "Aplicar" dispara.
  5. Um timestamp UTC conhecido, ao ser exibido, carrega um rótulo de fuso
     explícito e sofre EXATAMENTE UMA conversão (nunca um duplo deslocamento).
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

_FIM_MARCADOR = (
    "atualizarVisibilidadeCampoCustom();\natualizarTudo();\nagendarProximo();"
)


def _extrair_script_ate_antes_do_bootstrap(html: str) -> str:
    """Pega o <script> inteiro (funções + wiring de listeners reais), mas
    corta ANTES das 3 chamadas de bootstrap finais -- elas disparariam um
    fetch/setTimeout de verdade assim que o script fosse avaliado, o que
    quebraria o harness (o teste dispara os ciclos manualmente)."""
    script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    corte = script.index(_FIM_MARCADOR)
    funcoes = script[:corte]
    # Só nesta cópia de teste: eval() direto no topo do módulo NÃO vaza
    # `function` para o escopo de quem chama em modo estrito -- removemos a
    # diretiva só da cópia usada pelo harness (o arquivo real mantém
    # 'use strict').
    return re.sub(r"""['"]use strict['"];""", "", funcoes, count=1)


_HARNESS = r"""
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this._text = "";
    this.className = "";
    this.dataset = {};
    this.attrs = {};
    this.value = "";
    this.hidden = false;
    this._listeners = {};
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
  addEventListener(tipo, fn) {
    (this._listeners[tipo] = this._listeners[tipo] || []).push(fn);
  }
  disparar(tipo) { (this._listeners[tipo] || []).forEach(fn => fn()); }
}

const registry = {};
function elById(id) {
  if (!registry[id]) registry[id] = new FakeElement("div");
  return registry[id];
}

global.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (v) => ({ nodeType: 3, textContent: String(v) }),
  getElementById: elById,
  addEventListener: () => {},
  hidden: false,
};
global.window = global;

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

// --- a partir daqui, getJSON e as funcoes de renderizacao sao substituidas
// por dublês -- o teste isola exclusivamente a logica de escopo custom/fuso,
// nunca a forma dos dados do dashboard. `getJSON`/`renderX` foram
// declaradas como `function` no escopo do modulo pelo eval() direto acima
// (mesma tecnica ja usada em test_shadow_panel_xss_safety.py); reatribuir
// aqui, no MESMO escopo, sobrescreve a referencia que atualizarTudo() usa.
const chamadasGetJSON = [];
getJSON = async function (rota, params) {
  chamadasGetJSON.push({ rota, params });
  return { funnel: {}, comparison: {}, cost_gate: {}, rejections: {}, trade_quality: {},
           progress: {}, events: {}, economic_verdict: {} };
};
renderStatusExecutivo = () => {};
renderFunil = () => {};
renderComparacao = () => {};
renderCobertura = () => {};
popularFiltros = () => {};
renderRejeicoes = () => {};
renderQualidade = () => {};
renderProgresso = () => {};
renderEventos = () => {};

const resultados = {};

async function cenario(nome, fn) {
  resultados[nome] = await fn();
}

(async () => {
  const scopeSelect = elById("scope-select");
  const since = elById("custom-since");
  const until = elById("custom-until");
  const err = elById("custom-period-error");

  // --- 1. campos vazios: bloqueia totalmente, nunca chama getJSON ---
  scopeSelect.value = "custom";
  since.value = "";
  until.value = "";
  await cenario("campos_vazios", async () => {
    await atualizarTudo();
    return {
      chamadas_getjson: chamadasGetJSON.length,
      erro_visivel: err.hidden === false,
      erro_texto: err.textContent,
    };
  });

  // --- 2. inicio >= fim: tambem bloqueia, nunca chama getJSON ---
  since.value = "2026-09-07T10:00";
  until.value = "2026-09-07T09:00";
  await cenario("inicio_depois_do_fim", async () => {
    await atualizarTudo();
    return {
      chamadas_getjson: chamadasGetJSON.length,
      erro_visivel: err.hidden === false,
      erro_texto: err.textContent,
    };
  });

  // --- 3. periodo valido: dispara EXATAMENTE UMA requisicao consolidada ---
  since.value = "2026-09-07T09:00";
  until.value = "2026-09-07T10:00";
  await cenario("periodo_valido", async () => {
    const antes = chamadasGetJSON.length;
    await atualizarTudo();
    const chamada = chamadasGetJSON[chamadasGetJSON.length - 1];
    return {
      chamadas_novas: chamadasGetJSON.length - antes,
      rota: chamada.rota,
      params: chamada.params,
      erro_oculto: err.hidden === true,
      since_esperado: new Date(since.value).toISOString(),
      until_esperado: new Date(until.value).toISOString(),
    };
  });

  // --- 4. selecionar "custom" sozinho (campos vazios de novo) nao dispara nada ---
  since.value = "";
  until.value = "";
  scopeSelect.value = "experiment";
  await cenario("selecionar_custom_nao_dispara_sozinho", async () => {
    const antes = chamadasGetJSON.length;
    scopeSelect.value = "custom";
    scopeSelect.disparar("change");   // simula o operador escolhendo a opcao
    return { chamadas_novas: chamadasGetJSON.length - antes };
  });

  // --- 4b. clicar "Aplicar" com periodo valido dispara EXATAMENTE UMA ---
  since.value = "2026-09-08T09:00";
  until.value = "2026-09-08T10:00";
  await cenario("clicar_aplicar_dispara_uma_unica", async () => {
    const antes = chamadasGetJSON.length;
    elById("custom-apply-btn").disparar("click");
    // o handler e' async; aguarda o microtask/fetch simulado terminar
    await new Promise(r => setTimeout(r, 0));
    return { chamadas_novas: chamadasGetJSON.length - antes };
  });

  // --- visibilidade dos campos conforme o escopo ---
  scopeSelect.value = "custom";
  atualizarVisibilidadeCampoCustom();
  resultados.visibilidade_custom = {
    campos_ocultos: elById("custom-period-fields").hidden,
    rotulo_fuso: elById("fuso-label-custom").textContent,
  };
  scopeSelect.value = "experiment";
  atualizarVisibilidadeCampoCustom();
  resultados.visibilidade_experiment = {
    campos_ocultos: elById("custom-period-fields").hidden,
    rotulo_fuso: elById("fuso-label-custom").textContent,
  };

  // --- fuso horario: timestamp UTC conhecido, uma unica conversao ---
  // `FUSO_LABEL` e' `const` -- eval() direto NAO vaza let/const para o
  // escopo de quem chama (só `function`/`var` vazam), então recalculamos
  // com a MESMA função pura `rotuloFuso()` (já vazada) em vez de referenciar
  // a constante diretamente; é o valor idêntico que `fmtData` já usa
  // internamente, fechado sobre o `FUSO_LABEL` do próprio escopo avaliado.
  const isoConhecido = "2026-09-07T06:45:00+00:00";
  const fusoLabel = rotuloFuso();
  resultados.fuso_label = fusoLabel;
  resultados.fmt_data = fmtData(isoConhecido);
  resultados.conversao_unica_esperada =
    new Date(isoConhecido).toLocaleString("pt-BR", { hour12: false }) + " (" + fusoLabel + ")";

  console.log(JSON.stringify(resultados));
})();
"""


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_periodo_custom_bloqueia_disparo_e_fuso_e_inequivoco(tmp_path):
    html = SHADOW_HTML.read_text(encoding="utf-8")
    funcoes = _extrair_script_ate_antes_do_bootstrap(html)
    script_path = tmp_path / "shadow_custom_funcs.js"
    script_path.write_text(funcoes, encoding="utf-8")
    harness_path = tmp_path / "harness_custom.js"
    harness_path.write_text(_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness_path), str(script_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    dados = json.loads(result.stdout.strip().splitlines()[-1])

    # 1. campos vazios -- bloqueio total, zero requisicao
    cv = dados["campos_vazios"]
    assert cv["chamadas_getjson"] == 0, "campos vazios nao podem disparar a requisicao"
    assert cv["erro_visivel"] is True
    assert cv["erro_texto"] == "Preencha início e fim antes de aplicar."

    # 2. inicio >= fim -- tambem bloqueia, zero requisicao nova
    idf = dados["inicio_depois_do_fim"]
    assert idf["chamadas_getjson"] == 0, "inicio >= fim nao pode disparar a requisicao"
    assert idf["erro_texto"] == "O início precisa ser anterior ao fim."

    # 3. periodo valido -- exatamente UMA requisicao consolidada, params corretos
    pv = dados["periodo_valido"]
    assert pv["chamadas_novas"] == 1, "periodo valido deve disparar exatamente UMA requisicao"
    assert pv["rota"] == "/api/diagnostics/dashboard"
    assert pv["params"]["scope"] == "custom"
    assert pv["params"]["since"] == pv["since_esperado"]
    assert pv["params"]["until"] == pv["until_esperado"]
    assert pv["erro_oculto"] is True

    # 4. selecionar "custom" sozinho (campos vazios) nao dispara nada
    assert dados["selecionar_custom_nao_dispara_sozinho"]["chamadas_novas"] == 0

    # 4b. clicar "Aplicar" com periodo valido preenchido dispara EXATAMENTE UMA
    assert dados["clicar_aplicar_dispara_uma_unica"]["chamadas_novas"] == 1

    # visibilidade dos campos conforme o escopo selecionado
    assert dados["visibilidade_custom"]["campos_ocultos"] is False
    assert "horário local" in dados["visibilidade_custom"]["rotulo_fuso"]
    assert dados["visibilidade_experiment"]["campos_ocultos"] is True
    assert dados["visibilidade_experiment"]["rotulo_fuso"] == ""

    # 5. fuso horario inequivoco: rotulo explicito presente, UMA unica conversao
    assert re.fullmatch(r"UTC[+-]\d{2}", dados["fuso_label"]), \
        f"rotulo de fuso mal formado: {dados['fuso_label']}"
    assert dados["fmt_data"].endswith("(" + dados["fuso_label"] + ")"), \
        "todo timestamp exibido precisa carregar o rotulo de fuso explicito"
    assert dados["fmt_data"] == dados["conversao_unica_esperada"], (
        "a conversao exibida precisa ser EXATAMENTE UMA aplicacao do deslocamento "
        "local -- qualquer diferenca aqui indicaria um deslocamento duplicado"
    )
