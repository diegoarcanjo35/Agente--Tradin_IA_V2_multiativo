"""Fase 3.1.1 (correção final da auditoria do PO), item 1: única fonte
configurável do capital inicial PAPER -- `Settings.paper_starting_balance_usd`.
Substitui os dois hardcodes independentes que existiam antes (backend
`1000.0`, frontend `"1000.00"`).
"""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.core.config import RunMode, Settings


def _settings(**overrides) -> Settings:
    defaults = dict(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:")
    defaults.update(overrides)
    return Settings(**defaults)


def test_default_starting_balance_is_1000():
    settings = _settings()
    assert settings.paper_starting_balance_usd == 1000.0


def test_custom_starting_balance_is_accepted():
    settings = _settings(paper_starting_balance_usd=5000.0)
    assert settings.paper_starting_balance_usd == 5000.0


def test_zero_starting_balance_is_rejected():
    with pytest.raises(ValidationError):
        _settings(paper_starting_balance_usd=0.0)


def test_negative_starting_balance_is_rejected():
    with pytest.raises(ValidationError):
        _settings(paper_starting_balance_usd=-100.0)


def test_infinite_starting_balance_is_rejected():
    with pytest.raises(ValidationError):
        _settings(paper_starting_balance_usd=math.inf)


def test_nan_starting_balance_is_rejected():
    with pytest.raises(ValidationError):
        _settings(paper_starting_balance_usd=math.nan)


def test_starting_balance_never_a_secret_field():
    """Confirma que o campo não está na lista de segredos nunca logados --
    apenas um float de configuração comum, sem tratamento especial de
    ocultação (ao contrário de bybit_api_key/bybit_api_secret)."""
    settings = _settings(paper_starting_balance_usd=1234.0)
    assert settings.paper_starting_balance_usd == 1234.0
    # Nenhuma exceção/ofuscação ao acessar diretamente -- é um valor
    # operacional público, não um segredo.
