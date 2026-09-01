"""Fase 3.1 (painel gráfico), itens 18, 19 da matriz de testes: a
biblioteca gráfica é carregada localmente sem dependência de CDN, e a
atribuição/licença estão presentes.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
REPO_ROOT = Path(__file__).resolve().parent.parent

VENDOR_JS = FRONTEND_DIR / "vendor" / "lightweight-charts.standalone.production.js"
VENDOR_LICENSE = FRONTEND_DIR / "vendor" / "LICENSE.lightweight-charts.txt"
INDEX_HTML = FRONTEND_DIR / "index.html"
APP_JS = FRONTEND_DIR / "app.js"
NOTICES = REPO_ROOT / "THIRD_PARTY_NOTICES.md"


def test_vendor_file_exists_locally_and_is_the_expected_official_build():
    assert VENDOR_JS.exists(), "biblioteca gráfica não vendorizada em frontend/vendor/"
    content = VENDOR_JS.read_text(encoding="utf-8")
    assert "TradingView Lightweight Charts" in content
    assert "v4.1.4" in content
    assert "window.LightweightCharts" in content
    # SHA-256 registrado em THIRD_PARTY_NOTICES.md deve bater exatamente
    # com o arquivo real no repositório -- nunca um valor desatualizado.
    sha256 = hashlib.sha256(VENDOR_JS.read_bytes()).hexdigest()
    assert sha256 == "ec4bdfaafb53273e176520caac61ef0f6b69a40b395df7be2445aac33713625d"
    notices_text = NOTICES.read_text(encoding="utf-8")
    assert sha256 in notices_text


def test_vendor_license_file_exists():
    assert VENDOR_LICENSE.exists()
    license_text = VENDOR_LICENSE.read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "2.0" in license_text


def test_index_html_references_only_the_local_vendor_script_never_a_cdn():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "/static/vendor/lightweight-charts.standalone.production.js" in html
    # Nenhuma referência a CDN conhecido em nenhum lugar do HTML.
    forbidden_hosts = ["cdn.", "unpkg.", "jsdelivr.", "cdnjs."]
    for host in forbidden_hosts:
        assert host not in html.lower(), f"referência a CDN encontrada em index.html: {host}"


def test_app_js_never_references_a_cdn_url_for_the_chart_library():
    source = APP_JS.read_text(encoding="utf-8")
    forbidden_hosts = ["cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com"]
    for host in forbidden_hosts:
        assert host not in source


def test_attribution_and_license_link_present_in_footer():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "TradingView Lightweight Charts" in html
    assert "Apache License 2.0" in html
    # Auditoria Fase 3.1, gate 1: o link exigido pelo README oficial do
    # pacote é literal ("a link to https://www.tradingview.com/") -- deve
    # aparecer exatamente, não só a URL do produto (que é mais específica
    # mas não é o link textualmente exigido).
    assert 'href="https://www.tradingview.com/"' in html


def test_third_party_notices_documents_the_attribution_obligation_and_quotes_source_verbatim():
    text = NOTICES.read_text(encoding="utf-8")
    # Citação literal do README oficial do pacote (auditoria gate 1) --
    # nunca parafraseada/inventada.
    assert "You shall add the \"attribution notice\"" in text
    assert "https://www.tradingview.com/" in text
    assert "NOTICE" in text  # documenta a ausência do arquivo NOTICE no pacote npm


def test_third_party_notices_file_contains_all_required_fields():
    assert NOTICES.exists(), "THIRD_PARTY_NOTICES.md ausente na raiz do repositório"
    text = NOTICES.read_text(encoding="utf-8")
    required_substrings = [
        "lightweight-charts", "4.1.4", "registry.npmjs.org",
        "lightweight-charts.standalone.production.js",
        "SHA-256", "Apache", "2026-09-01",
    ]
    for substring in required_substrings:
        assert substring in text, f"campo obrigatório ausente em THIRD_PARTY_NOTICES.md: {substring}"
