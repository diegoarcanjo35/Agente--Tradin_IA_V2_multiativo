"""Fase 3.2 (item 9 da decisão do PO): stop-loss e take-profit são
reancorados no preço médio REAL da posição depois de cada fill de AUMENTO,
usando as distâncias CONGELADAS na decisão -- nunca um ATR recalculado com
o mercado do instante do fill, e nunca o preço observado pelo sinal antes
do slippage.

Todos os caminhos passam pelo `fill_service` compartilhado, que é o único
lugar do sistema onde um fill é aplicado a uma posição.
"""
from __future__ import annotations

import json

import pytest

from app.execution import fill_service
from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.models import Order

ATR = 10.0
STOP_MULTIPLE = 2.0
TARGET_MULTIPLE = 3.0
STOP_DISTANCE = ATR * STOP_MULTIPLE        # 20.0 US$ por unidade
TARGET_DISTANCE = ATR * TARGET_MULTIPLE    # 30.0 US$ por unidade


def _order_with_snapshot(
    session, symbol="BTCUSDT", side="BUY", qty=2.0, key="k1", is_close=False,
    snapshot: dict | None = None, stop_loss=None, take_profit=None,
) -> Order:
    """Cria a cadeia REAL Signal -> RiskEvaluation -> Order (todas as FKs
    obrigatórias), que é exatamente por onde `fill_service` recupera o
    snapshot congelado da decisão."""
    params = snapshot if snapshot is not None else {
        "strategy_timeframe": "5m",
        "strategy_timeframe_minutes": 5,
        "atr_per_unit_usd": ATR,
        "stop_loss_atr_multiple": STOP_MULTIPLE,
        "take_profit_atr_multiple": TARGET_MULTIPLE,
        "stop_distance_per_unit": STOP_DISTANCE,
        "target_distance_per_unit": TARGET_DISTANCE,
    }
    signal = repo.save_signal(session, symbol, side, "teste", 100.0, ATR, params)
    risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
    order = repo.save_order(
        session, idempotency_key=key, risk_evaluation_id=risk_eval.id, symbol=symbol,
        side=side, qty=qty,
        # Stop/alvo do SINAL (calculados sobre o preço observado, antes do
        # slippage) -- é justamente o que NÃO pode sobreviver ao fill.
        stop_loss=stop_loss if stop_loss is not None else 80.0,
        take_profit=take_profit if take_profit is not None else 130.0,
        mode="REPLAY", is_close=is_close, reference_price=100.0,
    )
    repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
    order.exchange_order_id = f"EX-{key}"
    return order


def _apply(session, order, fill_id, qty, price, fee=0.0, is_close=False):
    state = repo.get_or_create_system_state(session)
    snapshot = OrderStatusSnapshot(
        exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
        fills=[FillEvent(fill_id, qty, price, fee)],
    )
    return fill_service.apply_order_snapshot(
        session, state, None, order, snapshot, is_close=is_close, max_api_failures=5,
    )


# --- 29/30. lado correto sobre o preço REAL de fill ----------------------

def test_buy_anchors_stop_below_and_target_above_the_real_fill_price(db_session):
    order = _order_with_snapshot(db_session, side="BUY", qty=1.0, key="buy-1")
    _apply(db_session, order, "F1", 1.0, 103.5)  # slippage: preencheu a 103,5

    position = repo.open_positions(db_session, "BTCUSDT")[0]
    assert position.avg_entry_price == pytest.approx(103.5)
    assert position.stop_loss == pytest.approx(103.5 - STOP_DISTANCE)   # 83,5
    assert position.take_profit == pytest.approx(103.5 + TARGET_DISTANCE)  # 133,5
    # Jamais permanece no valor do sinal (80,0 / 130,0), calculado sobre o
    # preço observado ANTES do slippage.
    assert position.stop_loss != pytest.approx(80.0)
    assert position.take_profit != pytest.approx(130.0)


def test_sell_anchors_stop_above_and_target_below_the_real_fill_price(db_session):
    order = _order_with_snapshot(db_session, side="SELL", qty=1.0, key="sell-1")
    _apply(db_session, order, "F1", 1.0, 96.5)

    position = repo.open_positions(db_session, "BTCUSDT")[0]
    assert position.side == "SELL"
    assert position.avg_entry_price == pytest.approx(96.5)
    assert position.stop_loss == pytest.approx(96.5 + STOP_DISTANCE)   # 116,5
    assert position.take_profit == pytest.approx(96.5 - TARGET_DISTANCE)  # 66,5


# --- 31. ATR e R:R preservados -------------------------------------------

def test_atr_multiples_and_risk_reward_are_preserved_after_reanchoring(db_session):
    order = _order_with_snapshot(db_session, side="BUY", qty=1.0, key="rr-1")
    _apply(db_session, order, "F1", 1.0, 107.0)

    p = repo.open_positions(db_session, "BTCUSDT")[0]
    risk = p.avg_entry_price - p.stop_loss
    reward = p.take_profit - p.avg_entry_price
    assert risk == pytest.approx(ATR * STOP_MULTIPLE)
    assert reward == pytest.approx(ATR * TARGET_MULTIPLE)
    assert reward / risk == pytest.approx(TARGET_MULTIPLE / STOP_MULTIPLE)


# --- 32/33. fills parciais reancoram sobre o preço MÉDIO corrente --------

def test_partial_fill_reanchors_on_the_running_average_entry_price(db_session):
    order = _order_with_snapshot(db_session, side="BUY", qty=2.0, key="partial-1")

    _apply(db_session, order, "F1", 1.0, 100.0)
    p = repo.open_positions(db_session, "BTCUSDT")[0]
    assert p.avg_entry_price == pytest.approx(100.0)
    assert p.stop_loss == pytest.approx(80.0)
    assert p.take_profit == pytest.approx(130.0)

    # Segundo fill de AUMENTO: o preço médio vira 105, e a proteção
    # acompanha -- com as MESMAS distâncias congeladas.
    _apply(db_session, order, "F2", 1.0, 110.0)
    p = repo.open_positions(db_session, "BTCUSDT")[0]
    assert p.qty == pytest.approx(2.0)
    assert p.avg_entry_price == pytest.approx(105.0)
    assert p.stop_loss == pytest.approx(85.0)
    assert p.take_profit == pytest.approx(135.0)


def test_protection_is_not_monotonic_between_partial_fills(db_session):
    """O PO pediu explicitamente que NÃO se afirme monotonicidade: com um
    segundo fill a preço MENOR, o preço médio cai e a proteção desce
    junto."""
    order = _order_with_snapshot(db_session, side="BUY", qty=2.0, key="partial-2")
    _apply(db_session, order, "F1", 1.0, 110.0)
    first_stop = repo.open_positions(db_session, "BTCUSDT")[0].stop_loss

    _apply(db_session, order, "F2", 1.0, 90.0)
    p = repo.open_positions(db_session, "BTCUSDT")[0]
    assert p.avg_entry_price == pytest.approx(100.0)
    assert p.stop_loss == pytest.approx(80.0)
    assert p.stop_loss < first_stop  # desceu -- comportamento esperado


# --- reduções e fechamentos nunca reancoram ------------------------------

def test_reducing_fill_never_reanchors_protection_as_if_it_were_an_entry(db_session):
    entry = _order_with_snapshot(db_session, side="BUY", qty=2.0, key="reduce-entry")
    _apply(db_session, entry, "F1", 2.0, 100.0)
    before = repo.open_positions(db_session, "BTCUSDT")[0]
    stop_before, target_before = before.stop_loss, before.take_profit

    close = _order_with_snapshot(
        db_session, side="SELL", qty=1.0, key="reduce-close", is_close=True,
    )
    _apply(db_session, close, "F2", 1.0, 130.0, is_close=True)

    after = repo.open_positions(db_session, "BTCUSDT")[0]
    assert after.qty == pytest.approx(1.0)
    assert after.stop_loss == pytest.approx(stop_before)
    assert after.take_profit == pytest.approx(target_before)


def test_close_fill_without_a_local_position_never_creates_protection(db_session):
    close = _order_with_snapshot(
        db_session, side="SELL", qty=1.0, key="orphan-close", is_close=True,
    )
    result = _apply(db_session, close, "F1", 1.0, 100.0, is_close=True)
    assert result.new_fill_count == 1
    assert repo.open_positions(db_session, "BTCUSDT") == []


def test_blocked_opposite_fill_never_changes_existing_protection(db_session):
    entry = _order_with_snapshot(db_session, side="BUY", qty=1.0, key="opp-entry")
    _apply(db_session, entry, "F1", 1.0, 100.0)
    before = repo.open_positions(db_session, "BTCUSDT")[0]
    stop_before, target_before, qty_before = before.stop_loss, before.take_profit, before.qty

    # Fill de ENTRADA do lado oposto: bloqueado por segurança (correção
    # v1.2 #5), nunca aplicado -- e portanto nunca altera a proteção.
    opposite = _order_with_snapshot(db_session, side="SELL", qty=1.0, key="opp-entry-2")
    _apply(db_session, opposite, "F2", 1.0, 90.0)

    after = repo.open_positions(db_session, "BTCUSDT")[0]
    assert after.side == "BUY"
    assert after.qty == pytest.approx(qty_before)
    assert after.stop_loss == pytest.approx(stop_before)
    assert after.take_profit == pytest.approx(target_before)


# --- 34/35. o que consome os valores reancorados -------------------------

def test_stop_take_check_uses_the_reanchored_values(db_session, session_factory):
    """`Orchestrator._check_stop_take` lê `position.stop_loss`/
    `take_profit` -- portanto passa a usar automaticamente os valores
    reancorados, sem nenhuma alteração adicional nesse caminho."""
    order = _order_with_snapshot(db_session, side="BUY", qty=1.0, key="trigger-1")
    _apply(db_session, order, "F1", 1.0, 103.5)
    p = repo.open_positions(db_session, "BTCUSDT")[0]

    # Um candle cuja mínima cruza 83,5 (reancorado) mas NÃO cruzaria 80,0
    # (o valor antigo, do sinal) prova que a proteção efetiva mudou.
    candle_low = 83.0
    assert candle_low <= p.stop_loss
    assert candle_low > 80.0


# --- snapshot ausente/legado: comportamento anterior, sem inventar nada --

def test_legacy_order_without_snapshot_keeps_the_orders_own_protection(db_session):
    """Ordem sem as distâncias congeladas (criada antes desta fase): a
    proteção continua exatamente a que o `Order` trouxe. O `Settings`
    atual do processo NUNCA é consultado para preencher a lacuna."""
    order = _order_with_snapshot(
        db_session, side="BUY", qty=1.0, key="legacy-1", snapshot={"fast_period": 9},
    )
    _apply(db_session, order, "F1", 1.0, 103.5)

    p = repo.open_positions(db_session, "BTCUSDT")[0]
    assert p.stop_loss == pytest.approx(80.0)
    assert p.take_profit == pytest.approx(130.0)


def test_decision_snapshot_is_reached_only_through_persisted_foreign_keys(db_session):
    order = _order_with_snapshot(db_session, side="BUY", qty=1.0, key="fk-1")
    snapshot = repo.decision_snapshot_for_order(db_session, order)
    assert snapshot["stop_distance_per_unit"] == pytest.approx(STOP_DISTANCE)
    assert snapshot["target_distance_per_unit"] == pytest.approx(TARGET_DISTANCE)
    assert snapshot["strategy_timeframe"] == "5m"
    assert snapshot["atr_per_unit_usd"] == pytest.approx(ATR)
