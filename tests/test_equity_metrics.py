"""Fase 3.1.1 (correção final da auditoria do PO): funções puras de
patrimônio (equity) e P&L não realizado -- `app/metrics/engine.py`.
Contrato oficial: `equity = starting_balance + realized_price_pnl -
fees_paid + funding_net + unrealized_pnl`.
"""
from __future__ import annotations

import math

from app.metrics.engine import (
    UNAVAILABLE,
    PositionMarkView,
    compute_equity,
    compute_unrealized_pnl,
)


# --- compute_unrealized_pnl --------------------------------------------

def test_no_open_positions_is_genuinely_zero_not_unavailable():
    result = compute_unrealized_pnl([])
    assert result.total == 0.0
    assert result.complete is True
    assert result.per_position == []


def test_long_position_profits_when_mark_above_entry():
    marks = [PositionMarkView(
        symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=40000.0,
        mark_price=41000.0, mark_source="last_closed_candle", mark_at="2026-01-01T00:00:00+00:00",
    )]
    result = compute_unrealized_pnl(marks)
    assert result.total == 10.0  # (41000-40000)*0.01
    assert result.complete is True
    assert result.per_position[0].unrealized_pnl == 10.0


def test_short_position_profits_when_mark_below_entry():
    marks = [PositionMarkView(
        symbol="BTCUSDT", side="SELL", qty=0.01, avg_entry_price=40000.0,
        mark_price=39000.0, mark_source="forming_candle", mark_at="2026-01-01T00:00:00+00:00",
    )]
    result = compute_unrealized_pnl(marks)
    assert result.total == 10.0  # (40000-39000)*0.01


def test_short_position_loses_when_mark_above_entry():
    marks = [PositionMarkView(
        symbol="BTCUSDT", side="SELL", qty=0.01, avg_entry_price=40000.0,
        mark_price=41000.0, mark_source="forming_candle", mark_at="2026-01-01T00:00:00+00:00",
    )]
    result = compute_unrealized_pnl(marks)
    assert result.total == -10.0


def test_position_without_valid_mark_is_excluded_never_zeroed():
    """Uma posição sem preço válido NUNCA vira 0 silenciosamente -- é
    excluída da soma (o total reflete só o que é conhecido) e o resultado
    é sinalizado como incompleto."""
    marks = [
        PositionMarkView(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=40000.0,
            mark_price=41000.0, mark_source="last_closed_candle", mark_at="2026-01-01T00:00:00+00:00",
        ),
        PositionMarkView(
            symbol="ETHUSDT", side="BUY", qty=1.0, avg_entry_price=2000.0,
            mark_price=None, mark_source=None, mark_at=None,
        ),
    ]
    result = compute_unrealized_pnl(marks)
    assert result.total == 10.0  # só a posição marcável contribui
    assert result.complete is False
    eth_entry = next(p for p in result.per_position if p.symbol == "ETHUSDT")
    assert eth_entry.unrealized_pnl == UNAVAILABLE
    assert eth_entry.mark_price == UNAVAILABLE


def test_never_uses_entry_price_as_mark_to_fake_zero_pnl():
    """Regra explícita do PO: nunca usar o preço de entrada para fingir
    P&L zero quando não há mark válido."""
    marks = [PositionMarkView(
        symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=40000.0,
        mark_price=None, mark_source=None, mark_at=None,
    )]
    result = compute_unrealized_pnl(marks)
    assert result.total == 0.0  # soma do CONHECIDO (nada), não uma coincidência de zero P&L
    assert result.complete is False
    assert result.per_position[0].unrealized_pnl == UNAVAILABLE  # nunca 0.0 fabricado


def test_nan_or_infinite_mark_price_is_treated_as_invalid():
    marks = [PositionMarkView(
        symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=40000.0,
        mark_price=math.inf, mark_source="forming_candle", mark_at="2026-01-01T00:00:00+00:00",
    )]
    result = compute_unrealized_pnl(marks)
    assert result.complete is False
    assert result.per_position[0].unrealized_pnl == UNAVAILABLE


def test_multiple_symbols_never_mix_marks():
    """Cada posição usa APENAS seu próprio mark -- nunca o preço de outro
    símbolo contamina o cálculo."""
    marks = [
        PositionMarkView(
            symbol="BTCUSDT", side="BUY", qty=1.0, avg_entry_price=40000.0,
            mark_price=40100.0, mark_source="last_closed_candle", mark_at="t",
        ),
        PositionMarkView(
            symbol="SOLUSDT", side="BUY", qty=1.0, avg_entry_price=150.0,
            mark_price=140.0, mark_source="last_closed_candle", mark_at="t",
        ),
    ]
    result = compute_unrealized_pnl(marks)
    assert result.total == 100.0 + (-10.0)  # nunca BTC usando o preço de SOL ou vice-versa
    btc = next(p for p in result.per_position if p.symbol == "BTCUSDT")
    sol = next(p for p in result.per_position if p.symbol == "SOLUSDT")
    assert btc.unrealized_pnl == 100.0
    assert sol.unrealized_pnl == -10.0


# --- compute_equity ------------------------------------------------------

def _unrealized(total=0.0, complete=True):
    from app.metrics.engine import UnrealizedPnlResult
    return UnrealizedPnlResult(total=total, complete=complete, per_position=[])


def test_equity_with_no_positions_equals_starting_balance():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.0,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.equity == 1000.0
    assert summary.equity_complete is True


def test_equity_reflects_open_position_unrealized_pnl():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.0,
        funding_paid=None, funding_received=None, unrealized=_unrealized(25.0),
        open_positions_count=1, exposure_usd=400.0,
    )
    assert summary.equity == 1025.0


def test_equity_after_profitable_closed_trade():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=50.0, fees_paid=0.0,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.equity == 1050.0


def test_equity_after_losing_closed_trade():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=-30.0, fees_paid=0.0,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.equity == 970.0


def test_entry_fee_of_a_still_open_position_reduces_equity():
    """Regra central do item 3 da decisão do PO: uma posição aberta com
    taxa de entrada já paga (mas nenhum trade fechado ainda) já reduz o
    patrimônio -- nunca invisível até o fechamento."""
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.5,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0),
        open_positions_count=1, exposure_usd=400.0,
    )
    assert summary.equity == 999.5


def test_exit_fee_is_not_double_counted():
    """Uma posição fechada com realized_price_pnl=10 e fees_paid=0.2 (soma
    de entrada+saída) -- a taxa aparece UMA vez no equity, nunca duas."""
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=10.0, fees_paid=0.2,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.equity == 1009.8  # 1000 + 10 - 0.2, nunca 1000 + 10 - 0.2 - 0.2


def test_funding_received_increases_equity():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.0,
        funding_paid=0.0, funding_received=5.0, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.equity == 1005.0
    assert summary.funding_net == 5.0


def test_funding_paid_reduces_equity():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.0,
        funding_paid=3.0, funding_received=0.0, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.equity == 997.0
    assert summary.funding_net == -3.0


def test_funding_unavailable_when_no_provider_never_confused_with_zero():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.0,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0),
        open_positions_count=0, exposure_usd=0.0,
    )
    assert summary.funding_net == UNAVAILABLE
    assert summary.funding_paid == UNAVAILABLE
    assert summary.funding_received == UNAVAILABLE
    assert summary.equity == 1000.0  # funding ausente contribui 0.0 ao cálculo, mas é reportado como indisponível


def test_equity_incomplete_when_a_position_cannot_be_marked():
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=0.0, fees_paid=0.0,
        funding_paid=None, funding_received=None, unrealized=_unrealized(0.0, complete=False),
        open_positions_count=1, exposure_usd=400.0,
    )
    assert summary.equity_complete is False


def test_no_double_counting_across_realized_fees_and_unrealized():
    """Prova matemática: cada componente aparece exatamente uma vez na
    fórmula. starting=1000, realized_price_pnl=20 (de um trade fechado),
    fees=1.0 (0.5 de uma posição fechada + 0.5 de uma ainda aberta),
    funding líquido=+2.0, não realizado=+15 (posição aberta atual)."""
    summary = compute_equity(
        starting_balance_source="session_snapshot", starting_balance=1000.0, realized_price_pnl=20.0, fees_paid=1.0,
        funding_paid=0.0, funding_received=2.0, unrealized=_unrealized(15.0),
        open_positions_count=1, exposure_usd=500.0,
    )
    # 1000 + 20 - 1.0 + 2.0 + 15 = 1036.0 -- cada termo somado uma única vez.
    assert summary.equity == 1036.0
    assert summary.realized_net_pnl == 21.0  # 20 - 1.0 + 2.0 (nunca inclui o não realizado)
