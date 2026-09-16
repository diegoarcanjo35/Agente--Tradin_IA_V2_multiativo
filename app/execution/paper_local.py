"""PAPER_LOCAL execution: simulates fills locally with configurable fee and
slippage, never contacts Bybit. Used for local development/testing of the
full pipeline without any exchange dependency.
"""
from __future__ import annotations

import itertools
from typing import Callable

from app.core.logging import get_logger, log_event
from app.execution.base import FillEvent, OrderStatusSnapshot, SubmitAck
from app.execution.order_state import OrderStatus
from app.risk.engine import ApprovedOrder

logger = get_logger(__name__)


class PaperLocalExecutionEngine:
    def __init__(
        self,
        price_provider: Callable[[str], float],
        fee_rate: float = 0.0006,
        slippage_bps: float = 5.0,
        partial_fill_ratio: float | None = None,
    ):
        """price_provider is a fallback used only when the caller does not
        pass an explicit `reference_price` to submit() -- the orchestrator
        always passes the price of the candle that produced the decision
        (or, for a stop-loss/take-profit close, the trigger price), so in
        normal operation price_provider is never actually consulted; it
        exists so ad-hoc/test callers don't have to supply a price for every
        call. partial_fill_ratio, if set (0 < r < 1), simulates a partial
        fill for every order -- used by tests to exercise that path.

        Correção v1.1 #1: `submit()` no longer applies its own idempotency
        guard by trusting a bare in-memory dict as the source of truth --
        that's now the caller's job (the orchestrator never calls submit()
        for an idempotency_key that already has a persisted Order). The
        internal bookkeeping below exists only to let `poll_order()` serve
        whatever `submit()` already computed for a given exchange_order_id
        -- a lookup cache, not a correctness guard."""
        self._price_provider = price_provider
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.partial_fill_ratio = partial_fill_ratio
        self._snapshots: dict[str, OrderStatusSnapshot] = {}
        self._positions: dict[str, dict] = {}
        self._fill_id_seq = itertools.count(1)

    def submit(
        self, order: ApprovedOrder, idempotency_key: str, reference_price: float | None = None
    ) -> SubmitAck:
        exchange_order_id = f"PAPER-{idempotency_key[:16]}"

        price = reference_price if reference_price is not None else self._price_provider(order.symbol)
        slip = price * (self.slippage_bps / 10_000.0)
        fill_price = price + slip if order.side == "BUY" else price - slip

        is_partial = self.partial_fill_ratio is not None
        fill_qty = order.qty * self.partial_fill_ratio if is_partial else order.qty
        fee = fill_qty * fill_price * self.fee_rate

        fill = FillEvent(
            exchange_fill_id=f"PAPER-FILL-{next(self._fill_id_seq)}",
            fill_qty=fill_qty, fill_price=fill_price, fee=fee,
        )
        status = OrderStatus.PARTIALLY_FILLED if is_partial else OrderStatus.FILLED
        self._snapshots[exchange_order_id] = OrderStatusSnapshot(
            exchange_order_id=exchange_order_id, status=status, fills=[fill],
        )
        self._apply_to_position(order, fill_qty, fill_price)
        return SubmitAck(exchange_order_id=exchange_order_id, status=OrderStatus.SUBMITTED)

    def poll_order(self, exchange_order_id: str) -> OrderStatusSnapshot:
        """PAPER fills are computed synchronously inside submit() -- polling
        just serves whatever was already computed, every time (the fill
        ledger, not this engine, is responsible for not double-applying an
        already-recorded `exchange_fill_id`)."""
        snapshot = self._snapshots.get(exchange_order_id)
        if snapshot is None:
            return OrderStatusSnapshot(exchange_order_id=exchange_order_id, status=OrderStatus.UNKNOWN, fills=[])
        return snapshot

    def request_cancel(self, exchange_order_id: str) -> None:
        """Correção v1.1 #1: PAPER_LOCAL/PAPER_LIVE orders fill instantly
        and synchronously inside submit() -- there is never a non-terminal
        window in which a cancel request could arrive. Documented no-op;
        the subsequent poll_order() call will report whatever terminal
        fill state was already computed, never a fabricated CANCELLED."""
        log_event(logger, 20, "paper_cancel_requested_on_already_terminal_order",
                  exchange_order_id=exchange_order_id)

    def list_open_orders(self, symbol: str) -> list[dict]:
        """PAPER orders are never left open -- submit() always resolves
        them (FILLED/PARTIALLY_FILLED) synchronously, so there is never an
        order the 'exchange' (this simulator) considers open."""
        return []

    def _apply_to_position(self, order: ApprovedOrder, fill_qty: float, fill_price: float) -> None:
        """Four cases (Fase 1 correction 9) -- this is the engine's OWN
        simulated exchange-side position book (what get_position() reports
        for reconciliation), independent of the DB-persisted Position table
        the orchestrator/fill_service maintain.
        1. No existing position -> opens a new one.
        2. Existing position, same side -> increases qty, recomputes the
           weighted average entry price.
        3. Existing position, opposite side, fill qty < position qty ->
           reduces the existing position (side/avg price unchanged).
        4. Existing position, opposite side, fill qty >= position qty ->
           closes the existing position; any excess quantity opens a new
           position on the opposite side (flip), never simply summed onto
           the old one.
        """
        pos = self._positions.get(order.symbol)

        if pos is None:
            self._positions[order.symbol] = {
                "symbol": order.symbol, "side": order.side, "qty": fill_qty, "avg_entry_price": fill_price,
            }
            return

        if pos["side"] == order.side:
            total_qty = pos["qty"] + fill_qty
            pos["avg_entry_price"] = (pos["avg_entry_price"] * pos["qty"] + fill_price * fill_qty) / total_qty
            pos["qty"] = total_qty
            return

        if fill_qty < pos["qty"] - 1e-12:
            pos["qty"] -= fill_qty
            return

        if abs(fill_qty - pos["qty"]) <= 1e-12:
            del self._positions[order.symbol]
            return

        excess_qty = fill_qty - pos["qty"]
        self._positions[order.symbol] = {
            "symbol": order.symbol, "side": order.side, "qty": excess_qty, "avg_entry_price": fill_price,
        }

    def get_position(self, symbol: str) -> dict | None:
        return self._positions.get(symbol)

    def hydrate_positions(self, positions: list[dict]) -> None:
        """Restaura o book de posições em memória a partir do banco no
        boot. Sem isso, `self._positions` sempre volta vazio a cada
        reinício do processo, e a reconciliação de startup (que compara
        posição local persistida contra `get_position()`) sempre encontra
        uma divergência falsa ("corretora não reporta nenhuma") sempre que
        houver uma posição real aberta -- a "corretora" aqui é só este
        dicionário em memória, não uma corretora de verdade. Cada dict em
        `positions` precisa ter symbol/side/qty/avg_entry_price. Chamado
        uma única vez, antes da primeira reconciliação; nunca gera fill,
        ordem ou efeito colateral.

        Substitui integralmente `self._positions` (nunca faz merge sobre um
        estado anterior) -- chamadas repetidas são idempotentes mesmo que a
        lista informada mude entre uma chamada e outra, e nenhuma posição
        antiga fica presa caso não reapareça na lista mais recente. Uma
        posição com `qty <= 0` nunca é hidratada: `status == "OPEN"` com
        quantidade zero/negativa é um estado inconsistente que não deveria
        existir no banco (fechamento sempre muda `status` para `CLOSED`),
        e propagá-lo para o book em memória do simulador só criaria uma
        posição fantasma sem contrapartida real."""
        self._positions = {
            pos["symbol"]: {
                "symbol": pos["symbol"], "side": pos["side"],
                "qty": pos["qty"], "avg_entry_price": pos["avg_entry_price"],
            }
            for pos in positions if pos["qty"] > 0
        }
