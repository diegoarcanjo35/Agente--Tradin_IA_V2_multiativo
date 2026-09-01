"""Fase 3 multiativo, item 9.1 do brief: parsing, precedência, duplicatas e
símbolos inválidos de SYMBOLS/SYMBOL -- sem rede, puramente construção de
`Settings`.
"""
from __future__ import annotations

import pytest

from app.core.config import (
    AmbiguousSymbolConfigError,
    MultiSymbolNotSupportedError,
    Settings,
    V1CollisionError,
    get_settings,
)


def test_default_symbol_is_monoativo_btcusdt():
    settings = Settings()
    assert settings.symbols == ["BTCUSDT"]
    assert settings.symbol == "BTCUSDT"


def test_symbol_only_is_monoativo():
    settings = Settings(symbol="ethusdt")
    assert settings.symbols == ["ETHUSDT"]
    assert settings.symbol == "ETHUSDT"


def test_symbols_csv_is_parsed_normalized_and_ordered():
    settings = Settings(symbols="btcusdt, ethusdt , solusdt")
    assert settings.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert settings.symbol == "BTCUSDT"  # legacy field mirrors symbols[0]


def test_symbols_as_list_is_accepted_directly():
    settings = Settings(symbols=["ethusdt", "solusdt"])
    assert settings.symbols == ["ETHUSDT", "SOLUSDT"]


def test_symbols_wins_over_symbol_when_consistent():
    settings = Settings(symbol="BTCUSDT", symbols="BTCUSDT,ETHUSDT")
    assert settings.symbols == ["BTCUSDT", "ETHUSDT"]


def test_symbols_and_symbol_conflicting_is_rejected():
    with pytest.raises(AmbiguousSymbolConfigError):
        Settings(symbol="ETHUSDT", symbols="BTCUSDT,SOLUSDT")


def test_duplicates_in_symbols_are_deduplicated_preserving_first_occurrence_order():
    settings = Settings(symbols="BTCUSDT,ETHUSDT,BTCUSDT,ethusdt")
    assert settings.symbols == ["BTCUSDT", "ETHUSDT"]


def test_reordering_symbols_changes_the_resulting_order():
    forward = Settings(symbols="BTCUSDT,ETHUSDT")
    backward = Settings(symbols="ETHUSDT,BTCUSDT")
    assert forward.symbols == ["BTCUSDT", "ETHUSDT"]
    assert backward.symbols == ["ETHUSDT", "BTCUSDT"]
    assert forward.symbols != backward.symbols


def test_blank_entries_in_symbols_are_dropped():
    settings = Settings(symbols="BTCUSDT,,  ,ETHUSDT")
    assert settings.symbols == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.parametrize("bad_symbol", ["btc", "BTC-USDT", "TOOLONGSYMBOLNAMEOVERLIMIT123", "BTC USDT"])
def test_symbol_failing_format_regex_is_rejected(bad_symbol):
    with pytest.raises(Exception):
        Settings(symbols=f"BTCUSDT,{bad_symbol}")


def test_symbols_resolving_to_empty_list_is_rejected():
    with pytest.raises(Exception):
        Settings(symbols="   ,  ,")


def test_symbols_wrong_type_is_rejected():
    with pytest.raises(Exception):
        Settings(symbols=12345)


def test_bybit_demo_with_single_symbol_is_accepted(monkeypatch):
    monkeypatch.setenv("MODE", "BYBIT_DEMO")
    monkeypatch.setenv("SYMBOLS", "BTCUSDT")
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.symbols == ["BTCUSDT"]
    finally:
        get_settings.cache_clear()


def test_bybit_demo_with_multiple_symbols_is_rejected(monkeypatch):
    monkeypatch.setenv("MODE", "BYBIT_DEMO")
    monkeypatch.setenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    get_settings.cache_clear()
    try:
        with pytest.raises(MultiSymbolNotSupportedError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_multi_symbol_refuses_v1_known_port(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("API_PORT", "8000")
    get_settings.cache_clear()
    try:
        with pytest.raises(V1CollisionError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_multi_symbol_refuses_v1_known_database_filename(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("API_PORT", "8001")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./agente_trader_paper_live.db")
    get_settings.cache_clear()
    try:
        with pytest.raises(V1CollisionError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_mono_symbol_is_never_affected_by_v1_guard(monkeypatch):
    monkeypatch.setenv("SYMBOL", "BTCUSDT")
    monkeypatch.setenv("API_PORT", "8000")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./agente_trader_paper_live.db")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.symbols == ["BTCUSDT"]
    finally:
        get_settings.cache_clear()


def test_multi_symbol_with_v2_defaults_is_accepted(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT")
    monkeypatch.setenv("API_PORT", "8001")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./agente_trader_multiativo_dev.db")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    finally:
        get_settings.cache_clear()
