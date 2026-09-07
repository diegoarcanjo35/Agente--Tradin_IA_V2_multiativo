"""Fase 3.4.2 — dimensionamento coerente com o preenchimento e piso de
notional.

Cobre o defeito D3 encontrado na auditoria da Fase 3.4.1: a quantidade era
calculada pelo preço do SINAL enquanto a exposição persistida usava o preço
PREENCHIDO. A diferença de `position_usd × slippage` fazia a primeira compra
nascer acima do teto global e transformava a sobra das vendas em posições
degeneradas em cascata (US$ 0,025 -> 0,0000125 -> ...).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.risk.config import RiskLimits
from app.risk.cost_model import CostModel, adverse_fill_price
from app.risk.engine import RiskContext, RiskEngine
from app.strategy.schemas import Signal

FEE, SLIP_BPS = 0.0006, 5.0
SLIP = SLIP_BPS / 10_000.0
PRECO = 100.0


def _cost_model(cobertura=0.0001):
    return CostModel(
        fee_rate=FEE, slippage_bps=SLIP_BPS, source="paper_config",
        expected_move_atr_multiple=1.0, minimum_cost_coverage_ratio=cobertura,
    )


def _engine(**limites):
    base = dict(max_position_usd=50.0, max_total_exposure_usd=50.0,
                min_order_notional_usd=5.0, require_stop_loss=False)
    base.update(limites)
    e = RiskEngine(RiskLimits(**base))
    e.cost_model = _cost_model()
    return e


AGORA = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _signal(direcao="BUY", preco=PRECO, atr=5.0, symbol="BTCUSDT"):
    return Signal(symbol=symbol, direction=direcao, justification="teste",
                  created_at=AGORA, observed_price=preco, atr=atr,
                  stop_loss=preco - atr, take_profit=preco + atr,
                  source_candle_open_time=AGORA, params={})


def _ctx(exposicao=0.0, abertas=0, cooldown_until=None):
    return RiskContext(
        open_positions_count=abertas, open_exposure_usd=exposicao,
        daily_realized_loss_usd=0.0, consecutive_losses=0, data_is_stale=False,
        api_failure_count=0, clock_drift_seconds=0.0, kill_switch_engaged=False,
        trading_blocked=False, state_ambiguous=False, cooldown_until=cooldown_until,
        now=AGORA,
    )


# --- 1 e 13: notional preenchido respeita o teto -------------------------

def test_compra_de_50_nao_ultrapassa_o_teto_no_preenchimento():
    """ANTES: qty = 50/100 = 0,5 e fill = 100,05 -> notional 50,025 (acima
    do teto de 50). AGORA o dimensionamento usa o preço adverso."""
    r = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx())
    assert r.approved, r.reason
    qty = r.approved_order.qty
    fill = adverse_fill_price(PRECO, "BUY", SLIP)
    notional = qty * fill
    assert notional == pytest.approx(50.0, abs=1e-9)
    assert notional <= 50.0 + 1e-9, "entrada nasceu acima do teto global"


def test_venda_de_50_tambem_respeita_o_teto():
    r = _engine().evaluate(_signal("SELL"), signal_id=1, context=_ctx())
    assert r.approved, r.reason
    notional = r.approved_order.qty * adverse_fill_price(PRECO, "SELL", SLIP)
    assert notional == pytest.approx(50.0, abs=1e-9)


def test_dimensionamento_bate_com_o_preenchimento_real_do_paper_local():
    """O motor de execução é quem aplica o slippage de verdade; o notional
    preenchido tem de coincidir com o `position_usd` aprovado."""
    for lado in ("BUY", "SELL"):
        r = _engine().evaluate(_signal(lado), signal_id=1, context=_ctx())
        assert r.approved
        motor = PaperLocalExecutionEngine(price_provider=lambda s: PRECO,
                                          fee_rate=FEE, slippage_bps=SLIP_BPS)
        ack = motor.submit(r.approved_order, f"idem-{lado}", reference_price=PRECO)
        fill = motor.poll_order(ack.exchange_order_id).fills[0]
        assert fill.fill_qty * fill.fill_price == pytest.approx(50.0, abs=1e-9)


# --- 2, 3, 4 e 14: a cascata de resíduos não existe mais -----------------

def test_venda_nao_deixa_residuo_aproveitavel():
    """ANTES: notional 49,975 deixava US$ 0,025 de sobra."""
    r = _engine().evaluate(_signal("SELL"), signal_id=1, context=_ctx())
    notional = r.approved_order.qty * adverse_fill_price(PRECO, "SELL", SLIP)
    sobra = 50.0 - notional
    assert sobra == pytest.approx(0.0, abs=1e-9), f"sobra de US$ {sobra}"


def test_sobra_de_2_centavos_e_recusada():
    """O valor exato observado na auditoria."""
    r = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=49.975))
    assert not r.approved
    assert r.checks["minimum_order_notional_ok"] is False
    assert "mínimo operacional" in r.reason


def test_regressao_da_cascata_encontrada_na_auditoria():
    """Reproduz a sequência exata: 50,025 -> 0,024987 -> 1,2494e-05.
    Cada um desses resíduos tem de ser recusado agora."""
    for exposicao in (49.975, 49.9750125, 49.99998750625):
        sobra = 50.0 - exposicao
        assert 0 < sobra < 5.0, f"cenario invalido: sobra {sobra}"
        r = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=exposicao))
        assert not r.approved, f"sobra de US$ {sobra} virou ordem"
        assert r.checks["minimum_order_notional_ok"] is False


def test_exposicao_consolidada_nunca_excede_o_teto():
    """Duas entradas seguidas, contabilizando o notional preenchido."""
    e = _engine()
    total = 0.0
    for _ in range(5):
        r = e.evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=total))
        if not r.approved:
            break
        total += r.approved_order.qty * adverse_fill_price(PRECO, "BUY", SLIP)
    assert total <= 50.0 + 1e-9, f"exposicao consolidada {total} acima do teto"


# --- 5, 6, 7: redução legítima e fronteira do piso -----------------------

def test_reducao_legitima_de_20_dolares_continua_aprovada():
    r = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=30.0))
    assert r.approved, r.reason
    assert r.approved_order.qty * adverse_fill_price(PRECO, "BUY", SLIP) == pytest.approx(20.0, abs=1e-9)


def test_fronteira_do_piso_499_recusado_500_aceito():
    r499 = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=50.0 - 4.99))
    assert not r499.approved
    assert r499.checks["minimum_order_notional_ok"] is False

    r500 = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=45.0))
    assert r500.approved, r500.reason
    assert r500.checks["minimum_order_notional_ok"] is True


def test_zero_e_negativo_continuam_recusados_por_motivo_proprio():
    for exposicao in (50.0, 60.0):
        r = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=exposicao))
        assert not r.approved
        # falta TOTAL de exposição é motivo distinto de sobra abaixo do piso
        assert r.checks["exposure_room_available"] is False
        assert "minimum_order_notional_ok" not in r.checks


# --- motivos distinguíveis ------------------------------------------------

def test_motivos_de_recusa_sao_distinguiveis():
    sem_exposicao = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=50.0))
    sobra_pequena = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx(exposicao=49.0))
    assert sem_exposicao.checks["exposure_room_available"] is False
    assert sobra_pequena.checks["exposure_room_available"] is True
    assert sobra_pequena.checks["minimum_order_notional_ok"] is False


# --- 8: configuração inválida --------------------------------------------

@pytest.mark.parametrize("valor", [0.0, -1.0, 50.1, float("nan"), float("inf")])
def test_configuracao_invalida_falha_explicitamente(valor):
    with pytest.raises(Exception) as exc:
        Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                 database_url="sqlite:///:memory:", risk_min_order_notional_usd=valor)
    assert "RISK_MIN_ORDER_NOTIONAL_USD" in str(exc.value)


# --- 9: fingerprint / nova sessão ----------------------------------------

def test_mudar_apenas_o_piso_cria_nova_sessao(tmp_path):
    from sqlalchemy import select

    from app.api.main import build_orchestrator
    from app.persistence.db import session_scope
    from app.persistence.models import OperationalSession

    db = f"sqlite:///{tmp_path / 'piso.db'}"
    build_orchestrator(Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                                database_url=db, risk_min_order_notional_usd=5.0))
    o2 = build_orchestrator(Settings(mode=RunMode.REPLAY, symbols="BTCUSDT",
                                     database_url=db, risk_min_order_notional_usd=10.0))
    with session_scope(o2.session_factory) as s:
        sessoes = s.execute(select(OperationalSession).order_by(OperationalSession.id)).scalars().all()
    assert len(sessoes) == 2, "mudar o piso deveria criar sessão nova"
    assert sessoes[0].ended_at is not None
    assert sessoes[1].ended_at is None


def test_mesmo_piso_retoma_a_sessao(tmp_path):
    from sqlalchemy import select

    from app.api.main import build_orchestrator
    from app.persistence.db import session_scope
    from app.persistence.models import OperationalSession

    db = f"sqlite:///{tmp_path / 'mesmo.db'}"
    cfg = dict(mode=RunMode.REPLAY, symbols="BTCUSDT", database_url=db,
               risk_min_order_notional_usd=5.0)
    build_orchestrator(Settings(**cfg))
    o2 = build_orchestrator(Settings(**cfg))
    with session_scope(o2.session_factory) as s:
        assert len(s.execute(select(OperationalSession)).scalars().all()) == 1


# --- 10: monoativo e multiativo ------------------------------------------

def test_limite_vale_igual_em_multiativo():
    """A exposição é global; a sobra abaixo do piso tem de ser recusada
    mesmo quando o símbolo do sinal não é o que consumiu a exposição."""
    e = _engine()
    r = e.evaluate(_signal("BUY", symbol="ETHUSDT"), signal_id=1,
                   context=_ctx(exposicao=49.975))
    assert not r.approved
    assert r.checks["minimum_order_notional_ok"] is False


# --- 11 e 12: sem regressão nos demais gates ------------------------------

def test_cooldown_e_demais_gates_nao_regridem():
    ctx = _ctx(cooldown_until=AGORA + timedelta(minutes=10))
    r = _engine().evaluate(_signal("BUY"), signal_id=1, context=ctx)
    assert not r.approved
    assert r.checks["cooldown_expired"] is False


def test_stop_e_alvo_continuam_do_preenchimento_real():
    """O dimensionamento mudou; a proteção continua reancorada no preço
    REAL do fill pelo fill_service, com as distâncias congeladas."""
    r = _engine().evaluate(_signal("BUY"), signal_id=1, context=_ctx())
    assert r.approved
    motor = PaperLocalExecutionEngine(price_provider=lambda s: PRECO,
                                      fee_rate=FEE, slippage_bps=SLIP_BPS)
    ack = motor.submit(r.approved_order, "idem-prot", reference_price=PRECO)
    fill = motor.poll_order(ack.exchange_order_id).fills[0]
    assert fill.fill_price == pytest.approx(adverse_fill_price(PRECO, "BUY", SLIP))
    assert fill.fee == pytest.approx(fill.fill_qty * fill.fill_price * FEE)
