"""Pure functions computing metrics exclusively from persisted rows. Nothing
here talks to the database or the exchange -- it takes plain data in and
returns a MetricsResult, which is what makes it independently auditable and
unit-testable against a hand-computed fixture (see
tests/test_reproducible_fixture.py).

Every metric that cannot be computed from the given data reports the string
sentinel UNAVAILABLE rather than a fabricated 0, per the "não inventar zero"
requirement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Union

UNAVAILABLE = "indisponível"

Metric = Union[float, int, str]


@dataclass(frozen=True)
class ClosedTrade:
    """Minimal view of a closed Position the metrics engine needs."""

    realized_pnl: float  # gross, excluding fees
    fees_paid: float
    opened_at: datetime
    closed_at: datetime


@dataclass(frozen=True)
class MetricsResult:
    period_start: Metric
    period_end: Metric
    closed_trades_count: int
    gross_profit: Metric
    gross_loss: Metric
    net_profit: Metric
    commissions: Metric
    funding: Metric
    win_rate: Metric
    avg_win: Metric
    avg_loss: Metric
    payoff: Metric
    profit_factor: Metric
    expectancy: Metric
    max_win_streak: int
    max_loss_streak: int
    current_drawdown_money: Metric
    current_drawdown_pct: Metric
    max_drawdown_money: Metric
    max_drawdown_pct: Metric
    return_on_capital_pct: Metric
    return_over_drawdown: Metric
    exposure_usd: Metric


@dataclass(frozen=True)
class OrderFillView:
    """Minimal view of a filled Order the cost/slippage metrics need
    (Fase 2, item 7.6; contrato refeito na Fase 3.1.1 -- ver
    `compute_cost_metrics`). `filled_qty` é obrigatório desde a correção
    final da auditoria do PO: o impacto financeiro do slippage NUNCA pode
    ser calculado sem a quantidade real executada."""

    side: str  # BUY | SELL
    reference_price: float | None
    avg_fill_price: float
    filled_qty: float
    fees_total: float


@dataclass(frozen=True)
class CostMetricsResult:
    """Fase 3.1.1 (correção final da auditoria do PO): contrato refeito do
    zero -- os campos antigos `slippage_avg_usd`/`slippage_total_usd` são
    REMOVIDOS (não renomeados com o mesmo nome e semântica diferente,
    exatamente para nunca reintroduzir a confusão entre diferença unitária
    de preço e custo financeiro real). Ver docs/PAINEL_FINANCEIRO.md,
    seção "Slippage".

    - `unit_slippage_*`: diferença de preço por UNIDADE do ativo (US$ por
      unidade) -- nunca multiplicada por quantidade, nunca somável entre
      símbolos diferentes.
    - `adverse_slippage_cost_usd` / `price_improvement_value_usd`: dinheiro
      real (US$), já multiplicado por `filled_qty`, um custo NUNCA
      compensado silenciosamente pelo outro.
    - `net_slippage_impact_usd`: `adverse_slippage_cost_usd -
      price_improvement_value_usd` -- pode ser negativo (melhoria líquida).
    - `weighted_slippage_pct`: impacto financeiro assinado total dividido
      pelo notional de referência total -- NUNCA a média simples dos
      percentuais individuais (daria peso igual a ordens de tamanhos
      muito diferentes).
    - `adverse_slippage_pct`: só o custo adverso bruto sobre o notional de
      referência (sem contar melhorias).
    """

    fees_total: Metric
    adverse_slippage_cost_usd: Metric
    price_improvement_value_usd: Metric
    net_slippage_impact_usd: Metric
    avg_adverse_slippage_per_order_usd: Metric
    weighted_slippage_pct: Metric
    adverse_slippage_pct: Metric
    reference_notional_total_usd: Metric
    priced_orders_count: int
    unpriced_orders_count: int


def compute_cost_metrics(orders: list[OrderFillView]) -> CostMetricsResult:
    """Fase 2, item 7.6; contrato definitivo da correção final da
    auditoria do PO (Fase 3.1.1). Fees accumulated (never fabricated --
    0.0 for an empty set is genuine) plus the REAL financial impact of
    slippage against the reference price each order was decided against,
    always weighted by `filled_qty` -- never a raw sum/average of unitary
    price differences (that mixes units across symbols and ignores size).

    Adverse and favorable executions are tracked SEPARATELY and never
    silently net each other out before being reported -- see
    `adverse_slippage_cost_usd` vs `price_improvement_value_usd`."""
    fees_total = sum(o.fees_total for o in orders)
    priced = [o for o in orders if o.reference_price is not None]
    unpriced_count = len(orders) - len(priced)

    if not priced:
        return CostMetricsResult(
            fees_total=fees_total,
            adverse_slippage_cost_usd=UNAVAILABLE, price_improvement_value_usd=UNAVAILABLE,
            net_slippage_impact_usd=UNAVAILABLE, avg_adverse_slippage_per_order_usd=UNAVAILABLE,
            weighted_slippage_pct=UNAVAILABLE, adverse_slippage_pct=UNAVAILABLE,
            reference_notional_total_usd=UNAVAILABLE,
            priced_orders_count=0, unpriced_orders_count=unpriced_count,
        )

    adverse_cost = 0.0
    improvement_value = 0.0
    reference_notional_total = 0.0
    adverse_orders_count = 0
    for o in priced:
        # BUY: paying MORE than the reference price is adverse. SELL:
        # receiving LESS is adverse. `signed_impact` > 0 = adverso,
        # < 0 = melhoria de preço (contrato definitivo do PO).
        if o.side == "BUY":
            unit_slippage = o.avg_fill_price - o.reference_price
        else:
            unit_slippage = o.reference_price - o.avg_fill_price
        signed_impact = unit_slippage * o.filled_qty
        reference_notional_total += o.reference_price * o.filled_qty
        if signed_impact > 0:
            adverse_cost += signed_impact
            adverse_orders_count += 1
        else:
            improvement_value += -signed_impact

    net_impact = adverse_cost - improvement_value
    avg_adverse = (adverse_cost / adverse_orders_count) if adverse_orders_count else 0.0
    weighted_pct: Metric = (
        (net_impact / reference_notional_total * 100.0) if reference_notional_total > 0 else UNAVAILABLE
    )
    adverse_pct: Metric = (
        (adverse_cost / reference_notional_total * 100.0) if reference_notional_total > 0 else UNAVAILABLE
    )

    return CostMetricsResult(
        fees_total=fees_total,
        adverse_slippage_cost_usd=adverse_cost,
        price_improvement_value_usd=improvement_value,
        net_slippage_impact_usd=net_impact,
        avg_adverse_slippage_per_order_usd=avg_adverse,
        weighted_slippage_pct=weighted_pct,
        adverse_slippage_pct=adverse_pct,
        reference_notional_total_usd=reference_notional_total,
        priced_orders_count=len(priced),
        unpriced_orders_count=unpriced_count,
    )


@dataclass(frozen=True)
class PositionMarkView:
    """Minimal view of an OPEN Position plus the mark price to value it at
    -- `mark_price=None` means no valid live/last-closed price is
    available for this symbol (never substituted by `avg_entry_price`,
    which would fake a zero P&L)."""

    symbol: str
    side: str  # BUY | SELL
    qty: float
    avg_entry_price: float
    mark_price: float | None
    mark_source: str | None  # "forming_candle" | "last_closed_candle" | None
    mark_at: str | None  # ISO-8601, already UTC


@dataclass(frozen=True)
class UnrealizedPositionResult:
    symbol: str
    side: str
    qty: float
    avg_entry_price: float
    unrealized_pnl: Metric
    mark_price: Metric
    mark_source: Metric
    mark_at: Metric


@dataclass(frozen=True)
class UnrealizedPnlResult:
    """`total` is the sum of every position that COULD be marked -- never
    silently includes an unmarkable position as a fabricated 0. `complete`
    is the honest signal: False whenever at least one open position could
    not be marked, so a caller (the equity summary) can flag itself as
    incomplete instead of presenting false precision."""

    total: Metric
    complete: bool
    per_position: list[UnrealizedPositionResult]


def compute_unrealized_pnl(marks: list[PositionMarkView]) -> UnrealizedPnlResult:
    """Fase 3.1.1 (correção final da auditoria do PO), contrato do item 4:
    LONG = `(mark_price - avg_entry_price) * qty`; SHORT =
    `(avg_entry_price - mark_price) * qty`. Nenhuma posição sem preço
    válido contamina o total silenciosamente -- ela é EXCLUÍDA da soma
    (nunca tratada como 0) e `complete` vira False."""
    per_position: list[UnrealizedPositionResult] = []
    total = 0.0
    complete = True
    for m in marks:
        valid_mark = m.mark_price is not None and math.isfinite(m.mark_price)
        pnl: Metric
        if not valid_mark:
            pnl = UNAVAILABLE
            complete = False
        else:
            direction = 1 if m.side == "BUY" else -1
            candidate = direction * (m.mark_price - m.avg_entry_price) * m.qty
            if not math.isfinite(candidate):
                pnl = UNAVAILABLE
                complete = False
            else:
                pnl = candidate
                total += candidate
        per_position.append(UnrealizedPositionResult(
            symbol=m.symbol, side=m.side, qty=m.qty, avg_entry_price=m.avg_entry_price,
            unrealized_pnl=pnl,
            mark_price=m.mark_price if valid_mark else UNAVAILABLE,
            mark_source=m.mark_source if (valid_mark and m.mark_source) else UNAVAILABLE,
            mark_at=m.mark_at if (valid_mark and m.mark_at) else UNAVAILABLE,
        ))
    return UnrealizedPnlResult(total=total, complete=complete, per_position=per_position)


def _split_funding(funding_paid: float | None, funding_received: float | None) -> tuple[float, Metric, Metric, Metric]:
    """Shared helper: returns (component_for_math, net_metric, paid_metric,
    received_metric). `None` inputs (no funding provider at all for this
    run) report UNAVAILABLE and contribute 0.0 to any sum -- never
    confused with "funding exists and settled to exactly zero"."""
    has_funding = funding_paid is not None and funding_received is not None
    component = (funding_received - funding_paid) if has_funding else 0.0
    net: Metric = component if has_funding else UNAVAILABLE
    paid: Metric = funding_paid if has_funding else UNAVAILABLE
    received: Metric = funding_received if has_funding else UNAVAILABLE
    return component, net, paid, received


@dataclass(frozen=True)
class EquitySummary:
    """Fase 3.1.1 (correção final da auditoria do PO -- decisão definitiva
    do PO: "equity não tem escopo"): sempre LIFETIME, nunca parametrizada
    por `scope` -- consultar `/api/portfolio-summary` com
    `scope=lifetime`, `session` ou `daily` deve retornar exatamente o
    mesmo `equity`. Ver `PeriodPerformance` para os recortes por período.

    `equity = starting_balance + realized_price_pnl - fees_paid +
    funding_net + unrealized_pnl`

    `realized_price_pnl` and `fees_paid` are summed across BOTH open and
    closed positions, SEM corte de tempo algum (`Position.realized_pnl`/
    `repo.execution_fees` acumulam desde a abertura -- ver
    app/execution/fill_service.py) -- uma posição aberta antes de
    qualquer sessão/dia continua integralmente representada."""

    starting_balance: Metric
    starting_balance_source: str
    realized_price_pnl: Metric
    unrealized_pnl: Metric
    fees_paid: Metric
    funding_paid: Metric
    funding_received: Metric
    funding_net: Metric
    realized_net_pnl: Metric
    equity: Metric
    equity_complete: bool
    open_positions_count: int
    exposure_usd: Metric


def compute_equity(
    starting_balance: float, starting_balance_source: str, realized_price_pnl: float, fees_paid: float,
    funding_paid: float | None, funding_received: float | None, unrealized: UnrealizedPnlResult,
    open_positions_count: int, exposure_usd: float,
) -> EquitySummary:
    """Pure function -- every input is already aggregated by the caller
    from persisted rows, LIFETIME (never scope-filtered -- see
    app/api/routes_dashboard.py). `starting_balance_source` is one of
    `"session_snapshot"` (real, frozen value) or a `"legacy_fallback_*"`
    variant (see app/sessions.py::resolve_starting_balance) -- exposed so
    the API/UI can be honest about which case applies."""
    funding_component, funding_net_metric, funding_paid_metric, funding_received_metric = _split_funding(
        funding_paid, funding_received,
    )
    realized_net_pnl = realized_price_pnl - fees_paid + funding_component
    equity = starting_balance + realized_net_pnl + unrealized.total
    return EquitySummary(
        starting_balance=starting_balance, starting_balance_source=starting_balance_source,
        realized_price_pnl=realized_price_pnl, unrealized_pnl=unrealized.total,
        fees_paid=fees_paid, funding_paid=funding_paid_metric, funding_received=funding_received_metric,
        funding_net=funding_net_metric, realized_net_pnl=realized_net_pnl,
        equity=equity, equity_complete=unrealized.complete,
        open_positions_count=open_positions_count, exposure_usd=exposure_usd,
    )


@dataclass(frozen=True)
class PeriodPerformance:
    """Fase 3.1.1 (correção final da auditoria do PO): recorte de
    DESEMPENHO por período -- nunca chamado de "equity", nunca inclui
    `starting_balance`. `realized_pnl_attribution` é sempre
    `"position_close"` nesta versão do sistema: não existe um ledger
    persistido de P&L por fill individual (`Execution` grava
    `fill_qty`/`fill_price`/`fee`/`executed_at`, mas NENHUM delta de P&L
    por fill) -- o único dado disponível é `Position.realized_pnl`,
    atribuído inteiro ao instante de fechamento (`closed_at`) da posição.
    Consequência explícita: uma posição fechada PARCIALMENTE numa janela
    anterior e fechada por completo dentro do período atual soma ao
    período o P&L da porção fechada agora (o registro `Position` só
    existe uma vez, com um único `closed_at` -- o fechamento final);
    fechamentos parciais anteriores já persistidos como uma posição ainda
    aberta nunca vazam para um `period_performance` posterior por esse
    caminho, porque só entram na soma quando `Position.status == CLOSED`
    e seu `closed_at` cai dentro do recorte."""

    scope: str
    since: str | None
    realized_price_pnl: Metric
    fees_paid: Metric
    funding_paid: Metric
    funding_received: Metric
    funding_net: Metric
    realized_net_pnl: Metric
    fills_count: int
    closed_trades_count: int
    realized_pnl_attribution: str


def compute_period_performance(
    scope: str, since_iso: str | None, realized_price_pnl: float, fees_paid: float,
    funding_paid: float | None, funding_received: float | None,
    fills_count: int, closed_trades_count: int,
) -> PeriodPerformance:
    """Pure function. `realized_pnl_attribution` is always
    `"position_close"` -- see `PeriodPerformance` docstring for why."""
    funding_component, funding_net_metric, funding_paid_metric, funding_received_metric = _split_funding(
        funding_paid, funding_received,
    )
    realized_net_pnl = realized_price_pnl - fees_paid + funding_component
    return PeriodPerformance(
        scope=scope, since=since_iso, realized_price_pnl=realized_price_pnl, fees_paid=fees_paid,
        funding_paid=funding_paid_metric, funding_received=funding_received_metric,
        funding_net=funding_net_metric, realized_net_pnl=realized_net_pnl,
        fills_count=fills_count, closed_trades_count=closed_trades_count,
        realized_pnl_attribution="position_close",
    )


def _streaks(pnls: list[float]) -> tuple[int, int]:
    max_win = cur_win = 0
    max_loss = cur_loss = 0
    for pnl in pnls:
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
        elif pnl < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = 0
            cur_loss = 0
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)
    return max_win, max_loss


def _max_drawdown(equity_curve: list[float]) -> tuple[float, float, float, float]:
    """Returns (current_drawdown_money, current_drawdown_pct,
    max_drawdown_money, max_drawdown_pct) over a non-empty curve. "Current"
    is the drawdown AT THE LAST point of the curve (peak-so-far minus the
    last value) -- Fase 3.1.1, item "Não usar 'Rebaixamento'... usar
    Drawdown atual; Drawdown máximo". This equity curve is built only from
    REALIZED trades (see `compute_metrics`), never from live unrealized
    P&L -- "atual" here means "no fechamento do último trade", not
    "neste exato instante" (que exigiria o patrimônio ao vivo de
    `/api/portfolio-summary`); documentado em docs/PAINEL_FINANCEIRO.md."""
    peak = equity_curve[0]
    max_dd_money = 0.0
    max_dd_pct = 0.0
    current_dd_money = 0.0
    current_dd_pct = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        dd_money = peak - value
        dd_pct = (dd_money / peak * 100.0) if peak > 0 else 0.0
        max_dd_money = max(max_dd_money, dd_money)
        max_dd_pct = max(max_dd_pct, dd_pct)
        current_dd_money = dd_money
        current_dd_pct = dd_pct
    return current_dd_money, current_dd_pct, max_dd_money, max_dd_pct


def compute_metrics(
    closed_trades: list[ClosedTrade],
    starting_balance: float,
    open_exposure_usd: float | None = None,
    funding_total: float | None = None,
) -> MetricsResult:
    """Correção v1.1 #6: `funding_total`, when given (a real collected SUM
    from app.persistence.repo.funding_total -- only ever available for
    BYBIT_DEMO), is reported as `funding` and contributes to `net_profit`.
    `None` (no funding_provider at all -- REPLAY/PAPER_LOCAL/PAPER_LIVE)
    keeps the exact pre-existing UNAVAILABLE behavior; it is never
    conflated with a genuine 0.0 (no funding settled yet, but a provider
    exists and reached the exchange)."""
    funding: Metric = funding_total if funding_total is not None else UNAVAILABLE
    funding_component = funding_total if funding_total is not None else 0.0

    n = len(closed_trades)
    if n == 0:
        return MetricsResult(
            period_start=UNAVAILABLE, period_end=UNAVAILABLE, closed_trades_count=0,
            gross_profit=UNAVAILABLE, gross_loss=UNAVAILABLE, net_profit=UNAVAILABLE,
            commissions=UNAVAILABLE, funding=funding, win_rate=UNAVAILABLE,
            avg_win=UNAVAILABLE, avg_loss=UNAVAILABLE, payoff=UNAVAILABLE,
            profit_factor=UNAVAILABLE, expectancy=UNAVAILABLE, max_win_streak=0,
            max_loss_streak=0,
            current_drawdown_money=UNAVAILABLE, current_drawdown_pct=UNAVAILABLE,
            max_drawdown_money=UNAVAILABLE, max_drawdown_pct=UNAVAILABLE,
            return_on_capital_pct=UNAVAILABLE, return_over_drawdown=UNAVAILABLE,
            exposure_usd=open_exposure_usd if open_exposure_usd is not None else UNAVAILABLE,
        )

    ordered = sorted(closed_trades, key=lambda t: t.closed_at)
    pnls = [t.realized_pnl for t in ordered]
    fees = [t.fees_paid for t in ordered]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = sum(losses)  # negative or zero
    commissions = sum(fees)
    net_profit = gross_profit + gross_loss - commissions + funding_component

    win_rate = len(wins) / n
    avg_win = (gross_profit / len(wins)) if wins else UNAVAILABLE
    avg_loss = (gross_loss / len(losses)) if losses else UNAVAILABLE

    payoff: Metric
    if isinstance(avg_win, float) and isinstance(avg_loss, float) and avg_loss != 0:
        payoff = abs(avg_win / avg_loss)
    else:
        payoff = UNAVAILABLE

    profit_factor: Metric = (gross_profit / abs(gross_loss)) if gross_loss < 0 else UNAVAILABLE

    expectancy: Metric
    if isinstance(avg_win, float) and isinstance(avg_loss, float):
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    elif wins and not losses:
        expectancy = win_rate * avg_win
    else:
        expectancy = UNAVAILABLE

    max_win_streak, max_loss_streak = _streaks(pnls)

    equity_curve = [starting_balance]
    running = starting_balance
    for pnl, fee in zip(pnls, fees):
        running += pnl - fee
        equity_curve.append(running)

    current_dd_money, current_dd_pct, max_dd_money, max_dd_pct = _max_drawdown(equity_curve)

    return_on_capital_pct: Metric = (
        (net_profit / starting_balance * 100.0) if starting_balance > 0 else UNAVAILABLE
    )
    return_over_drawdown: Metric = (net_profit / max_dd_money) if max_dd_money > 0 else UNAVAILABLE

    return MetricsResult(
        period_start=ordered[0].opened_at.isoformat(),
        period_end=ordered[-1].closed_at.isoformat(),
        closed_trades_count=n,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=net_profit,
        commissions=commissions,
        funding=funding,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=payoff,
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        current_drawdown_money=current_dd_money,
        current_drawdown_pct=current_dd_pct,
        max_drawdown_money=max_dd_money,
        max_drawdown_pct=max_dd_pct,
        return_on_capital_pct=return_on_capital_pct,
        return_over_drawdown=return_over_drawdown,
        exposure_usd=open_exposure_usd if open_exposure_usd is not None else UNAVAILABLE,
    )
