"""Correction v1.2 #8: the entire user-facing experience must be in
Portuguese (Brazil). Scans frontend/index.html and frontend/app.js for the
specific English interface terms the correction calls out by name.
Consolidated technical terms are allowed ONLY when paired with a Portuguese
translation (e.g. "Fator de lucro (Profit Factor)") -- this test checks for
the literal STANDALONE English phrasing that would appear directly in the
UI, not for the technical term's mere presence inside a parenthetical.
"""
from __future__ import annotations

from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Each entry: a literal string that must NEVER appear verbatim in the
# frontend source -- these are the exact English UI strings named in the
# correction as forbidden (button labels, chip prefixes, raw booleans).
FORBIDDEN_LITERALS = [
    "Engage Kill Switch",
    "Disengage</button",
    ">Disengage<",
    "TRADING: ",
    "KILL SWITCH: ",
    ">true<",
    ">false<",
]


def _frontend_sources() -> dict[str, str]:
    return {
        "index.html": (FRONTEND_DIR / "index.html").read_text(encoding="utf-8"),
        "app.js": (FRONTEND_DIR / "app.js").read_text(encoding="utf-8"),
    }


def test_no_forbidden_english_ui_literals():
    sources = _frontend_sources()
    violations = []
    for filename, content in sources.items():
        for literal in FORBIDDEN_LITERALS:
            if literal in content:
                violations.append(f"{filename}: {literal!r}")
    assert violations == [], f"Forbidden English UI terms found: {violations}"


def test_kill_switch_buttons_are_in_portuguese():
    html = _frontend_sources()["index.html"]
    assert "Ativar bloqueio de emergência" in html
    assert "Desativar bloqueio de emergência" in html


def test_status_chip_labels_are_in_portuguese():
    js = _frontend_sources()["app.js"]
    # Fase 3.2: o chip único "OPERAÇÕES:" foi separado em DOIS conceitos --
    # o laço processando candles e a autorização de entradas -- porque um
    # rótulo só para os dois aparecia contraditório ao lado do estado
    # OBSERVANDO.
    assert "PROCESSAMENTO DE MERCADO:" in js
    assert "NOVAS ENTRADAS:" in js
    assert "BLOQUEIO DE EMERGÊNCIA:" in js


def test_direction_labels_are_translated_for_display():
    js = _frontend_sources()["app.js"]
    assert '"BUY": "COMPRA"' in js.replace("  ", " ").replace("\n", " ") or "COMPRA" in js
    assert "VENDA" in js
    assert "AGUARDAR" in js


def test_boolean_risk_decisions_are_displayed_as_approved_rejected_not_raw_booleans():
    js = _frontend_sources()["app.js"]
    assert "APROVADO" in js
    assert "REJEITADO" in js


def test_html_declares_portuguese_language():
    html = _frontend_sources()["index.html"]
    assert 'lang="pt-BR"' in html


def test_disclaimer_and_banner_are_in_portuguese():
    html = _frontend_sources()["index.html"]
    assert "AMBIENTE DEMO" in html
    assert "SEM DINHEIRO REAL" in html
    assert "garantia de rentabilidade" in html
