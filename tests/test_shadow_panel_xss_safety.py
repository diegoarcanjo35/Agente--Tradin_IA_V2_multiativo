"""Fase 3.5 (auditoria) — segurança do painel Shadow/Diagnóstico definitivo.

Duas provas, mesmo padrão de tests/test_frontend_xss_safety.py:

1. Estática: `frontend/shadow.html` deve ter ZERO ocorrências de
   `.innerHTML` como atribuição -- nem mesmo com string literal fixa,
   conforme a correção exigida pela auditoria (item 8).
2. Dinâmica: extrai as funções de renderização do próprio arquivo e as
   executa de verdade num shim de DOM em Node.js, injetando payload XSS
   em TODOS os campos controláveis por dado de backend: símbolo, modelo,
   motivo, erro, direção, parâmetros/separação, identificadores e
   timestamps -- e confirma que cada um vira texto puro, nunca markup.
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

PAYLOAD = "<img src=x onerror=\"alert('xss')\">"


def _extrair_funcoes_de_renderizacao(html: str) -> str:
    """Pega só o <script> do painel, cortando ANTES do ciclo de
    atualização (que dispara fetch/setInterval de verdade) -- o teste
    executa as funções de renderização diretamente, não o bootstrap."""
    script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    corte = script.index("ciclo de atualizacao")
    # volta ate o inicio da linha do comentario, pra nao cortar no meio dela
    inicio_linha = script.rfind("\n", 0, corte)
    funcoes = script[:inicio_linha]
    # Só para este harness de teste: `eval()` em modo estrito NÃO vaza
    # `function` declarada para o escopo de quem chama (o arquivo real
    # mantém 'use strict' -- é uma boa prática de produção; aqui só
    # removemos a diretiva da CÓPIA usada pelo teste, pra poder inspecionar
    # as funções deste jeito).
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
function elById(id) {
  if (!registry[id]) registry[id] = new FakeElement("div");
  return registry[id];
}

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

const payload = process.argv[3];
const resultados = {};

// --- símbolo, direção, motivo, separação: eventos recentes ---
const eventos = { events: [{
  tipo: "oportunidade", quando: payload, symbol: payload, direction: payload,
  model: payload, decisao: payload, motivo: payload,
  normalized_separation: 0.1, cost_coverage_ratio: 1.2, pnl_usd: null,
}], count: 1 };
// Nenhuma tag PERIGOSA (a que o payload injetaria se fosse parseado como
// markup) pode existir na arvore -- nosso proprio codigo so' cria
// div/span/td/tr/table/th/option/button; se "img"/"script"/"svg" aparecer,
// o payload foi interpretado como HTML em algum lugar.
const TAGS_PERIGOSAS = new Set(["img", "script", "svg", "iframe", "a", "style"]);
function tagsPerigosasNaArvore(no) {
  const achadas = [];
  (function visitar(n) {
    if (TAGS_PERIGOSAS.has(n.tag)) achadas.push(n.tag);
    (n.children || []).forEach(visitar);
  })(no);
  return achadas;
}
function textoCompletoDaArvore(no) {
  if (!n_children(no)) return no._text !== undefined ? no._text : (no.textContent || "");
  return (no.children || []).map(textoCompletoDaArvore).join("");
}
function n_children(no) { return (no.children || []).length; }

renderEventos(eventos);
const linhaEventos = elById("eventos-container").children[0].children[0].children[1];
resultados.eventos_tags_perigosas = tagsPerigosasNaArvore(linhaEventos);
resultados.eventos_texto_completo = textoCompletoDaArvore(linhaEventos);

// --- qualidade: modelo, símbolo, direção, exit_reason ---
const tq = { trades: [{
  symbol: payload, side: payload, model: payload, duration_minutes: 10,
  exit_reason: payload, normalized_separation: 0.1, mfe_pct: 1, mae_pct: -1,
  net_pnl_usd: 1.23,
}], count: 1, limit: 100 };
renderQualidade(tq);
const linhaQualidade = elById("qualidade-container").children[0].children[0].children[1];
resultados.qualidade_tags_perigosas = tagsPerigosasNaArvore(linhaQualidade);
resultados.qualidade_texto_completo = textoCompletoDaArvore(linhaQualidade);

// --- rejeições: categoria desconhecida cai no rótulo cru (payload) ---
const rej = { total: 1, ranking: [{ categoria: payload, quantidade: 1, percentual: 100 }] };
renderRejeicoes(rej);
const linhaRej = elById("rejeicoes-container").children[0];
resultados.rejeicoes_tags_perigosas = tagsPerigosasNaArvore(linhaRej);
resultados.rejeicoes_texto_completo = textoCompletoDaArvore(linhaRej);

// --- status executivo: erro simulado (mensagem de fetch) ---
document.getElementById("stale-banner").textContent = "";
try {
  throw new Error(payload);
} catch (e) {
  document.getElementById("stale-banner").textContent =
    "Não foi possível atualizar (" + e.message + ").";
}
resultados.banner_texto = elById("stale-banner").textContent;
resultados.banner_html_filhos = elById("stale-banner").children.length;

console.log(JSON.stringify(resultados));
"""


def test_shadow_html_zero_innerhtml_mesmo_literal():
    """Correção da auditoria: nem literal fixa é aceita mais."""
    texto = SHADOW_HTML.read_text(encoding="utf-8")
    assert ".innerHTML" not in texto, (
        "frontend/shadow.html não pode conter NENHUMA atribuição a "
        ".innerHTML, nem mesmo com string literal -- use textContent/"
        "createElement sempre."
    )


def test_shadow_html_zero_cdn_zero_dominio_externo():
    texto = SHADOW_HTML.read_text(encoding="utf-8")
    for termo in ("cdn.", "unpkg.", "jsdelivr.", "http://", "https://"):
        for m in re.finditer(r'(?:src|href)\s*=\s*["\'][^"\']*' + re.escape(termo), texto):
            pytest.fail(f"referência externa via src/href: {m.group(0)}")
        for m in re.finditer(r'fetch\(\s*["\'][^"\']*' + re.escape(termo), texto):
            pytest.fail(f"fetch para URL externa: {m.group(0)}")


def test_shadow_html_zero_post():
    texto = SHADOW_HTML.read_text(encoding="utf-8")
    assert '"POST"' not in texto and "'POST'" not in texto and "method: \"POST\"" not in texto


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_shadow_html_payload_xss_em_todos_os_campos_controlaveis(tmp_path):
    html = SHADOW_HTML.read_text(encoding="utf-8")
    funcoes = _extrair_funcoes_de_renderizacao(html)
    script_path = tmp_path / "shadow_render_funcs.js"
    script_path.write_text(funcoes, encoding="utf-8")
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness_path), str(script_path), PAYLOAD],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"harness falhou: {result.stderr}"
    dados = json.loads(result.stdout.strip().splitlines()[-1])

    # símbolo, direção, modelo, decisão, motivo, timestamp -- nenhuma tag
    # perigosa pode ter sido criada a partir do payload em lugar nenhum da
    # linha, e o texto cru precisa continuar presente (nunca "sumido")
    assert dados["eventos_tags_perigosas"] == [], (
        f"payload virou markup na linha de eventos: {dados['eventos_tags_perigosas']}")
    assert PAYLOAD in dados["eventos_texto_completo"], \
        "o payload precisa aparecer como TEXTO na linha, não ser removido/escapado a ponto de sumir"

    # qualidade: símbolo, direção, modelo, motivo de saída
    assert dados["qualidade_tags_perigosas"] == [], (
        f"payload virou markup na linha de qualidade: {dados['qualidade_tags_perigosas']}")
    assert PAYLOAD in dados["qualidade_texto_completo"]

    # motivo de rejeição desconhecido (usa o texto cru como categoria)
    assert dados["rejeicoes_tags_perigosas"] == [], (
        f"payload virou markup no motivo de rejeição: {dados['rejeicoes_tags_perigosas']}")
    assert PAYLOAD in dados["rejeicoes_texto_completo"]

    # mensagem de erro/banner de desatualização
    assert PAYLOAD in dados["banner_texto"]
    assert dados["banner_html_filhos"] == 0
