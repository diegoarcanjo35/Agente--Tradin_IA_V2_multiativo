"""Fase 3.1.1 (último gate contábil da auditoria do PO): distinção entre
SESSÃO OPERACIONAL (nasce a cada mudança de fingerprint de configuração)
e BASE CONTÁBIL (só nasce quando `paper_starting_balance_usd`
efetivamente muda). `portfolio.equity` é sempre "desde o início da base
contábil ATUAL" -- nunca soma o resultado de uma base anterior a um
reset, mas também nunca a esconde: o histórico permanece no banco,
consultável, apenas não compõe a equity da base atual.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution import fill_service
from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Execution, Order
from app.sessions import resolve_accounting_base
from tests.factories import activate_operational_state


def _make_order(
    session, symbol, side, idempotency_key, is_close=False, qty=1.0, reference_price=None,
) -> Order:
    signal = repo.save_signal(session, symbol, side, "teste", 100.0, 1.0, {})
    risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
    order = repo.save_order(
        session, idempotency_key=idempotency_key, risk_evaluation_id=risk_eval.id,
        symbol=symbol, side=side, qty=qty, stop_loss=90.0, take_profit=1000.0, mode="REPLAY",
        is_close=is_close, reference_price=reference_price,
    )
    repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
    order.exchange_order_id = f"EX-{idempotency_key}"
    return order


def _apply_fill(session, order, exchange_fill_id, qty, price, fee, is_close=False):
    state = repo.get_or_create_system_state(session)
    snapshot = OrderStatusSnapshot(
        exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
        fills=[FillEvent(exchange_fill_id, qty, price, fee)],
    )
    return fill_service.apply_order_snapshot(
        session, state, None, order, snapshot, is_close=is_close, max_api_failures=5,
    )


def _client_for(orch):
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app)


def _build(tmp_path, db_name, starting_balance, symbols="BTCUSDT"):
    """Simula um processo real subindo/reiniciando: `build_orchestrator`
    é o MESMO caminho de produção (app/api/main.py), chama
    `start_or_resume_session` de verdade -- nunca um atalho de teste."""
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols, database_url=f"sqlite:///{tmp_path / db_name}",
        paper_starting_balance_usd=starting_balance,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)
    return orch


def _close_trade_for_profit(orch, symbol, entry_price, exit_price, qty, tag):
    with session_scope(orch.session_factory) as session:
        open_order = _make_order(session, symbol, "BUY", f"{tag}-open", qty=qty)
        _apply_fill(session, open_order, f"EXEC-{tag}-open", qty, entry_price, 0.0, is_close=False)
        close_order = _make_order(session, symbol, "SELL", f"{tag}-close", is_close=True, qty=qty)
        _apply_fill(session, close_order, f"EXEC-{tag}-close", qty, exit_price, 0.0, is_close=True)


# --- Exemplo obrigatório do PO: 1000 -> +100 -> reset 2000 ----------------

def test_full_reset_walkthrough_1000_plus_100_reset_2000(tmp_path):
    """Os itens 1-6 da entrega do PO, num único fluxo cronológico real
    (mesmo db_name -- MESMO arquivo sqlite, simulando reinícios reais do
    mesmo processo com config diferente)."""
    db_name = "reset_walkthrough.db"

    # 1) Sessão/base A: US$ 1.000 + US$ 100 = US$ 1.100.
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    _close_trade_for_profit(orch_a, "BTCUSDT", entry_price=100.0, exit_price=200.0, qty=1.0, tag="base-a")
    client_a = _client_for(orch_a)
    body_a = client_a.get("/api/portfolio-summary").json()["portfolio"]
    assert body_a["equity"] == pytest.approx(1100.0)
    assert body_a["starting_balance"] == pytest.approx(1000.0)

    # Nenhuma posição aberta -- reset permitido.
    with session_scope(orch_a.session_factory) as session:
        assert repo.open_positions(session) == []

    # 2) Reset para US$ 2.000 (processo reiniciado com config nova, MESMO banco).
    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    client_b = _client_for(orch_b)

    # 3) Primeira equity da nova base = exatamente US$ 2.000 (nunca 2.100).
    body_b = client_b.get("/api/portfolio-summary").json()["portfolio"]
    assert body_b["equity"] == pytest.approx(2000.0)
    assert body_b["starting_balance"] == pytest.approx(2000.0)

    # 4) O resultado antigo (US$ 100) continua no banco, mas não entra na nova equity.
    with session_scope(orch_b.session_factory) as session:
        closed = repo.closed_positions(session)  # sem filtro de tempo -- histórico completo
        assert len(closed) == 1
        assert closed[0].realized_pnl == pytest.approx(100.0)  # nunca apagado
    assert body_b["realized_price_pnl"] == pytest.approx(0.0)  # mas não soma na base nova

    # 5) Novo resultado de US$ 50 produz equity de US$ 2.050.
    _close_trade_for_profit(orch_b, "BTCUSDT", entry_price=500.0, exit_price=550.0, qty=1.0, tag="base-b")
    body_b_after = client_b.get("/api/portfolio-summary").json()["portfolio"]
    assert body_b_after["equity"] == pytest.approx(2050.0)


def test_fees_and_funding_before_reset_never_reenter_new_base(tmp_path):
    """Item 6: taxas e funding anteriores ao reset não entram novamente."""
    db_name = "reset_fees_funding.db"

    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    orch_a.funding_provider = object()
    with session_scope(orch_a.session_factory) as session:
        order = _make_order(session, "BTCUSDT", "BUY", "fee-a-open")
        _apply_fill(session, order, "EXEC-fee-a-open", 1.0, 100.0, 5.0, is_close=False)
        close = _make_order(session, "BTCUSDT", "SELL", "fee-a-close", is_close=True)
        _apply_fill(session, close, "EXEC-fee-a-close", 1.0, 110.0, 3.0, is_close=True)

        from app.persistence.models import FundingEvent

        session.add(FundingEvent(funding_id="fund-a", symbol="BTCUSDT", amount=-2.0, occurred_at=datetime.now(timezone.utc)))

    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    client_b = _client_for(orch_b)
    body_b = client_b.get("/api/portfolio-summary").json()["portfolio"]
    assert body_b["equity"] == pytest.approx(2000.0)
    assert body_b["fees_paid"] == pytest.approx(0.0)  # 5.0 + 3.0 da base A nunca entram
    assert body_b["funding_net"] == "indisponível" or body_b["funding_net"] == pytest.approx(0.0)


# --- Sessão operacional comum NUNCA reseta a base -------------------------

def test_strategy_only_session_change_preserves_base_and_equity(tmp_path):
    """Item 7: mudança APENAS de estratégia cria sessão operacional nova,
    mas preserva a mesma base contábil e a equity."""
    from app.risk.config import RiskLimits
    from app.sessions import start_or_resume_session
    from app.strategy.engine import StrategyConfig

    db_name = "strategy_change_same_base.db"
    orch = _build(tmp_path, db_name, starting_balance=1000.0)
    _close_trade_for_profit(orch, "BTCUSDT", entry_price=100.0, exit_price=180.0, qty=1.0, tag="strat-a")
    client = _client_for(orch)
    equity_before = client.get("/api/portfolio-summary").json()["portfolio"]["equity"]
    assert equity_before == pytest.approx(1080.0)

    with session_scope(orch.session_factory) as session:
        old_session_id = repo.get_or_create_system_state(session).active_session_id
        new_session = start_or_resume_session(
            session, orch.settings, "v2-nova-estrategia",
            RiskLimits(
                max_position_usd=999999.0, max_concurrent_positions=5, max_daily_loss_usd=999999.0,
                max_total_exposure_usd=999999.0, cooldown_after_losses=3, cooldown_minutes=30,
                max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
            ),
        )
        state = repo.get_or_create_system_state(session)
        assert new_session.id != old_session_id  # sessão OPERACIONAL nova, confirmado
        state.active_session_id = new_session.id

    equity_after = client.get("/api/portfolio-summary").json()["portfolio"]["equity"]
    assert equity_after == pytest.approx(1080.0)  # a base contábil (e a equity) não mudou


def test_restart_without_balance_change_preserves_base_and_equity(tmp_path):
    """Item 8: reinício sem mudança de saldo preserva a base e a equity."""
    db_name = "restart_same_balance.db"
    orch_1 = _build(tmp_path, db_name, starting_balance=1000.0)
    _close_trade_for_profit(orch_1, "BTCUSDT", entry_price=100.0, exit_price=130.0, qty=1.0, tag="restart-a")
    equity_1 = _client_for(orch_1).get("/api/portfolio-summary").json()["portfolio"]["equity"]

    # "Reinício": novo processo, MESMO banco, MESMA configuração de saldo.
    orch_2 = _build(tmp_path, db_name, starting_balance=1000.0)
    equity_2 = _client_for(orch_2).get("/api/portfolio-summary").json()["portfolio"]["equity"]

    assert equity_1 == equity_2 == pytest.approx(1030.0)


# --- /api/equity-curve nunca finge continuidade através de um reset ------

def test_equity_curve_never_spans_a_reset_boundary(tmp_path):
    """Item 9: a curva não desenha uma continuidade falsa entre bases --
    o primeiro ponto da curva da base nova é sempre o saldo congelado
    dela, nunca um valor que incorpore o resultado da base anterior."""
    db_name = "curve_reset.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    _close_trade_for_profit(orch_a, "BTCUSDT", entry_price=100.0, exit_price=200.0, qty=1.0, tag="curve-a")

    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    points = _client_for(orch_b).get("/api/equity-curve").json()

    assert points[0]["equity"] == pytest.approx(2000.0)  # nunca 2100
    for p in points:
        assert p["equity"] >= 2000.0 - 1e-6  # nunca cai abaixo da nova âncora por causa da base antiga


# --- sessão legada: fallback sem apagar histórico -------------------------

def test_legacy_session_fallback_preserves_history(tmp_path):
    """Item 10: sessão legada mantém fallback de US$ 1.000 sem apagar
    histórico -- confirmado tanto no valor quanto na integridade dos
    dados antigos."""
    import json as json_module

    db_name = "legacy_fallback_history.db"
    orch = _build(tmp_path, db_name, starting_balance=777.0)
    _close_trade_for_profit(orch, "BTCUSDT", entry_price=100.0, exit_price=150.0, qty=1.0, tag="legacy-a")

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active = repo.get_active_session(session, state)
        snapshot = json_module.loads(active.config_snapshot_json)
        del snapshot["paper_starting_balance_usd"]  # simula sessão pré-Fase-3.1.1
        active.config_snapshot_json = json_module.dumps(snapshot)

    body = _client_for(orch).get("/api/portfolio-summary").json()["portfolio"]
    assert body["starting_balance"] == 1000.0  # fallback, nunca 777.0 nem inventado
    assert body["starting_balance_source"] == "legacy_fallback_missing_field"

    with session_scope(orch.session_factory) as session:
        closed = repo.closed_positions(session)
        assert len(closed) == 1
        assert closed[0].realized_pnl == pytest.approx(50.0)  # histórico intacto, nunca apagado


# --- resolve_accounting_base: prova direta da fronteira -------------------

def test_resolve_accounting_base_finds_the_reset_boundary(tmp_path):
    db_name = "resolve_base_boundary.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)

    with session_scope(orch_b.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        active = repo.get_active_session(session, state)
        base_started_at, base_session = resolve_accounting_base(session, active)
        assert base_session.id == active.id  # a base B começa na própria sessão B
        assert base_started_at == active.started_at


# =========================================================================
# /api/costs escopado pela base contábil ativa (gate final do PO)
# =========================================================================
#
# O card de custos NUNCA pode somar taxas/slippage de uma base anterior a
# um reset de `paper_starting_balance_usd` junto com o patrimônio e o P&L
# da base atual -- seria comparar números de universos financeiros
# diferentes na mesma tela. O recorte é feito por `Execution.executed_at`
# (o instante do FILL real), nunca por `Order.created_at`/`updated_at`.


def _round_trip_with_costs(
    orch, symbol, tag, *, entry_ref, entry_fill, exit_ref, exit_fill,
    qty=1.0, entry_fee=0.0, exit_fee=0.0,
):
    """Abre e fecha uma posição deixando ZERO posições abertas ao final
    (pré-condição do reset de saldo), com `reference_price` conhecido em
    ambas as pontas para que o slippage seja verificável à mão."""
    with session_scope(orch.session_factory) as session:
        open_order = _make_order(
            session, symbol, "BUY", f"{tag}-open", qty=qty, reference_price=entry_ref,
        )
        _apply_fill(session, open_order, f"EXEC-{tag}-open", qty, entry_fill, entry_fee, is_close=False)
        close_order = _make_order(
            session, symbol, "SELL", f"{tag}-close", is_close=True, qty=qty, reference_price=exit_ref,
        )
        _apply_fill(session, close_order, f"EXEC-{tag}-close", qty, exit_fill, exit_fee, is_close=True)


def test_costs_before_reset_do_not_appear_in_the_new_base(tmp_path):
    """Custos anteriores ao reset não aparecem no card da base nova."""
    db_name = "costs_before_reset.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    _round_trip_with_costs(
        orch_a, "BTCUSDT", "costs-a",
        entry_ref=100.0, entry_fill=150.0, exit_ref=200.0, exit_fill=180.0,
        qty=2.0, entry_fee=5.0, exit_fee=3.0,
    )
    costs_a = _client_for(orch_a).get("/api/costs").json()
    assert costs_a["fees_total"] == pytest.approx(8.0)  # a base A realmente tinha custos
    assert costs_a["adverse_slippage_cost_usd"] == pytest.approx(140.0)  # (150-100)*2 + (200-180)*2

    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    costs_b = _client_for(orch_b).get("/api/costs").json()

    assert costs_b["fees_total"] == pytest.approx(0.0)  # nunca herda as 8.0 da base A
    assert costs_b["priced_orders_count"] == 0
    assert costs_b["unpriced_orders_count"] == 0
    assert costs_b["adverse_slippage_cost_usd"] == "indisponível"  # honesto, nunca um zero fabricado

    # O histórico da base A continua no banco, apenas fora deste recorte.
    with session_scope(orch_b.session_factory) as session:
        assert len(repo.orders_with_executions_since(session)) == 2


def test_costs_after_reset_do_appear_in_the_new_base(tmp_path):
    """Custos posteriores ao reset aparecem normalmente."""
    db_name = "costs_after_reset.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    _round_trip_with_costs(
        orch_a, "BTCUSDT", "old",
        entry_ref=100.0, entry_fill=150.0, exit_ref=200.0, exit_fill=180.0,
        qty=2.0, entry_fee=5.0, exit_fee=3.0,
    )

    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    _round_trip_with_costs(
        orch_b, "BTCUSDT", "new",
        entry_ref=100.0, entry_fill=101.0, exit_ref=110.0, exit_fill=109.0,
        qty=1.0, entry_fee=1.5, exit_fee=0.5,
    )
    costs_b = _client_for(orch_b).get("/api/costs").json()

    assert costs_b["fees_total"] == pytest.approx(2.0)  # só 1.5 + 0.5 da base nova
    assert costs_b["priced_orders_count"] == 2
    assert costs_b["adverse_slippage_cost_usd"] == pytest.approx(2.0)  # (101-100)*1 + (110-109)*1
    assert costs_b["reference_notional_total_usd"] == pytest.approx(210.0)  # 100*1 + 110*1


def test_costs_symbol_filter_still_works_under_base_scoping(tmp_path):
    """O filtro `?symbol=` continua funcionando após o escopo por base."""
    db_name = "costs_symbol_filter.db"
    orch = _build(tmp_path, db_name, starting_balance=1000.0, symbols="BTCUSDT,ETHUSDT")
    _round_trip_with_costs(
        orch, "BTCUSDT", "btc",
        entry_ref=100.0, entry_fill=102.0, exit_ref=110.0, exit_fill=110.0,
        qty=1.0, entry_fee=3.0, exit_fee=1.0,
    )
    _round_trip_with_costs(
        orch, "ETHUSDT", "eth",
        entry_ref=50.0, entry_fill=51.0, exit_ref=60.0, exit_fill=60.0,
        qty=2.0, entry_fee=4.0, exit_fee=2.0,
    )
    client = _client_for(orch)

    btc = client.get("/api/costs", params={"symbol": "BTCUSDT"}).json()
    eth = client.get("/api/costs", params={"symbol": "ETHUSDT"}).json()

    assert btc["fees_total"] == pytest.approx(4.0)
    assert btc["adverse_slippage_cost_usd"] == pytest.approx(2.0)  # (102-100)*1
    assert eth["fees_total"] == pytest.approx(6.0)
    assert eth["adverse_slippage_cost_usd"] == pytest.approx(2.0)  # (51-50)*2


def test_costs_global_equals_the_monetary_sum_of_each_symbol(tmp_path):
    """Consolidado global == soma MONETÁRIA por símbolo (nunca a soma dos
    percentuais, que não são somáveis)."""
    db_name = "costs_global_sum.db"
    orch = _build(tmp_path, db_name, starting_balance=1000.0, symbols="BTCUSDT,ETHUSDT")
    _round_trip_with_costs(
        orch, "BTCUSDT", "gbtc",
        entry_ref=100.0, entry_fill=102.0, exit_ref=110.0, exit_fill=112.0,
        qty=1.0, entry_fee=3.0, exit_fee=1.0,
    )
    _round_trip_with_costs(
        orch, "ETHUSDT", "geth",
        entry_ref=50.0, entry_fill=51.0, exit_ref=60.0, exit_fill=60.0,
        qty=2.0, entry_fee=4.0, exit_fee=2.0,
    )
    client = _client_for(orch)
    total = client.get("/api/costs").json()
    btc = client.get("/api/costs", params={"symbol": "BTCUSDT"}).json()
    eth = client.get("/api/costs", params={"symbol": "ETHUSDT"}).json()

    for field in (
        "fees_total", "adverse_slippage_cost_usd", "price_improvement_value_usd",
        "net_slippage_impact_usd", "reference_notional_total_usd",
    ):
        assert total[field] == pytest.approx(btc[field] + eth[field]), field
    assert total["priced_orders_count"] == btc["priced_orders_count"] + eth["priced_orders_count"]


def test_slippage_before_reset_never_contaminates_the_new_base(tmp_path):
    """Slippage anterior ao reset não contamina média, total nem
    percentual da base nova."""
    db_name = "costs_slippage_reset.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    _round_trip_with_costs(  # slippage gigante, deliberadamente distorcivo
        orch_a, "BTCUSDT", "huge",
        entry_ref=100.0, entry_fill=150.0, exit_ref=200.0, exit_fill=100.0,
        qty=2.0, entry_fee=0.0, exit_fee=0.0,
    )

    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    with session_scope(orch_b.session_factory) as session:
        order = _make_order(session, "BTCUSDT", "BUY", "small", qty=1.0, reference_price=100.0)
        _apply_fill(session, order, "EXEC-small", 1.0, 101.0, 0.0, is_close=False)
    costs_b = _client_for(orch_b).get("/api/costs").json()

    assert costs_b["adverse_slippage_cost_usd"] == pytest.approx(1.0)  # só (101-100)*1
    assert costs_b["avg_adverse_slippage_per_order_usd"] == pytest.approx(1.0)  # média de 1 ordem
    assert costs_b["reference_notional_total_usd"] == pytest.approx(100.0)
    assert costs_b["adverse_slippage_pct"] == pytest.approx(1.0)  # 1/100 -- nunca inflado pela base A
    assert costs_b["weighted_slippage_pct"] == pytest.approx(1.0)


def test_costs_accounting_base_matches_portfolio_summary(tmp_path):
    """`accounting_base_started_at` de /api/costs coincide exatamente com
    o de /api/portfolio-summary -- as duas telas falam da mesma base."""
    db_name = "costs_base_matches.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    _round_trip_with_costs(
        orch_a, "BTCUSDT", "match-a",
        entry_ref=100.0, entry_fill=101.0, exit_ref=110.0, exit_fill=110.0, qty=1.0,
    )
    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)
    client = _client_for(orch_b)

    costs_base = client.get("/api/costs").json()["accounting_base_started_at"]
    summary_base = client.get("/api/portfolio-summary").json()["portfolio"]["accounting_base_started_at"]

    assert costs_base is not None
    assert costs_base == summary_base


def test_strategy_only_session_change_keeps_the_same_base_costs(tmp_path):
    """Sessão criada APENAS por mudança de estratégia mantém os custos da
    mesma base contábil (a sessão nova não zera o card)."""
    from app.risk.config import RiskLimits
    from app.sessions import start_or_resume_session

    db_name = "costs_strategy_change.db"
    orch = _build(tmp_path, db_name, starting_balance=1000.0)
    _round_trip_with_costs(
        orch, "BTCUSDT", "strat",
        entry_ref=100.0, entry_fill=102.0, exit_ref=110.0, exit_fill=110.0,
        qty=1.0, entry_fee=2.0, exit_fee=1.0,
    )
    client = _client_for(orch)
    before = client.get("/api/costs").json()
    assert before["fees_total"] == pytest.approx(3.0)

    with session_scope(orch.session_factory) as session:
        new_session = start_or_resume_session(
            session, orch.settings, "v2-nova-estrategia",
            RiskLimits(
                max_position_usd=999999.0, max_concurrent_positions=5, max_daily_loss_usd=999999.0,
                max_total_exposure_usd=999999.0, cooldown_after_losses=3, cooldown_minutes=30,
                max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
            ),
        )
        repo.get_or_create_system_state(session).active_session_id = new_session.id

    after = client.get("/api/costs").json()
    assert after["fees_total"] == pytest.approx(3.0)  # nada foi perdido
    assert after["adverse_slippage_cost_usd"] == pytest.approx(before["adverse_slippage_cost_usd"])
    assert after["accounting_base_started_at"] == before["accounting_base_started_at"]


def test_order_without_any_fill_stays_out_of_costs(tmp_path):
    """Ordem sem fill nenhum continua fora do card -- nunca uma entrada
    fantasma com notional/slippage inventados."""
    db_name = "costs_order_without_fill.db"
    orch = _build(tmp_path, db_name, starting_balance=1000.0)
    with session_scope(orch.session_factory) as session:
        filled = _make_order(session, "BTCUSDT", "BUY", "has-fill", qty=1.0, reference_price=100.0)
        _apply_fill(session, filled, "EXEC-has-fill", 1.0, 101.0, 2.0, is_close=False)
        _make_order(session, "BTCUSDT", "BUY", "no-fill", qty=5.0, reference_price=100.0)  # sem fill

    costs = _client_for(orch).get("/api/costs").json()
    assert costs["priced_orders_count"] == 1  # só a ordem que realmente executou
    assert costs["unpriced_orders_count"] == 0
    assert costs["fees_total"] == pytest.approx(2.0)
    assert costs["reference_notional_total_usd"] == pytest.approx(100.0)  # nunca 100*5 da ordem sem fill


def test_order_with_fills_on_both_sides_of_the_boundary_uses_only_the_later_fills(tmp_path):
    """Pedido explícito do PO: uma ordem com fills dos DOIS lados da
    fronteira entra apenas com os fills POSTERIORES -- o `avg_fill_price`
    é recalculado a partir só deles, nunca reaproveitado de
    `Order.avg_fill_price` (que agrega todos os fills da ordem)."""
    db_name = "costs_split_fills.db"
    orch_a = _build(tmp_path, db_name, starting_balance=1000.0)
    orch_b = _build(tmp_path, db_name, starting_balance=2000.0)

    with session_scope(orch_b.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        base_started_at, _base = resolve_accounting_base(session, repo.get_active_session(session, state))

        order = _make_order(session, "BTCUSDT", "BUY", "split", qty=2.0, reference_price=100.0)
        _apply_fill(session, order, "EXEC-split-1", 1.0, 200.0, 7.0, is_close=False)  # "antigo"
        _apply_fill(session, order, "EXEC-split-2", 1.0, 101.0, 1.0, is_close=False)  # atual
        session.flush()

        # Backdata o PRIMEIRO fill para antes da fronteira: a mesma ordem
        # passa a ter execuções dos dois lados do reset.
        first = session.query(Execution).filter_by(exchange_fill_id="EXEC-split-1").one()
        first.executed_at = base_started_at - timedelta(minutes=5)
        session.flush()

        # `Order.avg_fill_price` agrega os DOIS fills -- exatamente o valor
        # que a implementação NÃO pode reaproveitar.
        assert order.avg_fill_price == pytest.approx(150.5)
        assert order.filled_qty == pytest.approx(2.0)

        scoped = repo.orders_with_executions_since(session, since=base_started_at)
        assert len(scoped) == 1
        assert [e.exchange_fill_id for e in scoped[0][1]] == ["EXEC-split-2"]

    costs = _client_for(orch_b).get("/api/costs").json()

    assert costs["fees_total"] == pytest.approx(1.0)  # só a taxa do fill posterior
    assert costs["reference_notional_total_usd"] == pytest.approx(100.0)  # ref 100 * qty 1 (não 2)
    # Se reaproveitasse o avg agregado (150.5), o custo adverso seria
    # (150.5-100)*2 = 101.0. Recalculado só com o fill posterior: (101-100)*1.
    assert costs["adverse_slippage_cost_usd"] == pytest.approx(1.0)
