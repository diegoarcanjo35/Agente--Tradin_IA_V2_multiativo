"""Covers spec section 7 items 1-6 and 23: P&L, fees, profit factor,
drawdown, win rate, return/drawdown, and realized-vs-unrealized separation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.metrics.engine import UNAVAILABLE, ClosedTrade, compute_metrics

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def trade(pnl: float, fee: float, day_offset: int) -> ClosedTrade:
    opened = T0 + timedelta(days=day_offset)
    closed = opened + timedelta(hours=1)
    return ClosedTrade(realized_pnl=pnl, fees_paid=fee, opened_at=opened, closed_at=closed)


def test_no_trades_reports_unavailable_not_zero():
    result = compute_metrics([], starting_balance=1000.0)
    assert result.closed_trades_count == 0
    assert result.net_profit == UNAVAILABLE
    assert result.profit_factor == UNAVAILABLE
    assert result.win_rate == UNAVAILABLE


def test_pnl_and_fees_computation():
    trades = [trade(100.0, 1.0, 0), trade(-40.0, 1.0, 1)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.gross_profit == pytest.approx(100.0)
    assert result.gross_loss == pytest.approx(-40.0)
    assert result.commissions == pytest.approx(2.0)
    # net = gross_profit + gross_loss - commissions
    assert result.net_profit == pytest.approx(100.0 - 40.0 - 2.0)


def test_profit_factor():
    trades = [trade(100.0, 0, 0), trade(-50.0, 0, 1)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.profit_factor == pytest.approx(2.0)


def test_profit_factor_unavailable_when_no_losses():
    trades = [trade(100.0, 0, 0), trade(50.0, 0, 1)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.profit_factor == UNAVAILABLE


def test_win_rate():
    trades = [trade(10, 0, 0), trade(-5, 0, 1), trade(10, 0, 2), trade(-5, 0, 3)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.win_rate == pytest.approx(0.5)


def test_max_drawdown_money_and_pct():
    # equity: 1000 -> 1100 (win 100) -> 1020 (loss 80) -> 1120 (win 100)
    trades = [trade(100, 0, 0), trade(-80, 0, 1), trade(100, 0, 2)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.max_drawdown_money == pytest.approx(80.0)
    assert result.max_drawdown_pct == pytest.approx(80.0 / 1100.0 * 100.0)


def test_current_drawdown_reflects_the_last_equity_point_not_the_peak():
    """Fase 3.1.1: 'drawdown atual' é a distância do pico até o ÚLTIMO
    ponto da curva (não o máximo histórico já visto). Curva: 1000 -> 1100
    (pico) -> 1020 (drawdown de 80 aqui) -> 1120 (novo pico, drawdown
    atual volta a 0)."""
    trades = [trade(100, 0, 0), trade(-80, 0, 1)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.current_drawdown_money == pytest.approx(80.0)
    assert result.max_drawdown_money == pytest.approx(80.0)

    trades_recovered = [trade(100, 0, 0), trade(-80, 0, 1), trade(100, 0, 2)]
    result_recovered = compute_metrics(trades_recovered, starting_balance=1000.0)
    assert result_recovered.current_drawdown_money == pytest.approx(0.0)  # 1120 é um novo pico
    assert result_recovered.max_drawdown_money == pytest.approx(80.0)  # o máximo histórico persiste


def test_return_over_drawdown():
    trades = [trade(100, 0, 0), trade(-80, 0, 1), trade(100, 0, 2)]
    result = compute_metrics(trades, starting_balance=1000.0)
    expected_net = 100 - 80 + 100
    assert result.return_over_drawdown == pytest.approx(expected_net / 80.0)


def test_realized_vs_unrealized_pnl_are_separate_fields():
    """compute_metrics only ever sees CLOSED trades -- an open position's
    unrealized P&L must never leak into realized figures. The dashboard
    exposes open positions (with their own avg_entry_price, no realized_pnl)
    via a separate endpoint, keeping the two concepts structurally apart."""
    closed = [trade(50.0, 1.0, 0)]
    result = compute_metrics(closed, starting_balance=1000.0, open_exposure_usd=25.0)
    assert result.net_profit == pytest.approx(49.0)
    assert result.exposure_usd == pytest.approx(25.0)
    # Nothing about an open position's unrealized P&L is representable here.
    assert not hasattr(result, "unrealized_pnl")


def test_streaks():
    trades = [trade(10, 0, 0), trade(10, 0, 1), trade(-5, 0, 2), trade(-5, 0, 3), trade(-5, 0, 4)]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.max_win_streak == 2
    assert result.max_loss_streak == 3
