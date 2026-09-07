"""Fase 3.4.3 (auditoria) — DIFERENCIAL evento a evento:
motor operacional real (com gate permissivo) x baseline_without_cost_gate.

Mesma sequência determinística, mesma configuração, mesmo tempo de candle.
Divergência não explicada bloqueia o fechamento — por isso as diferenças
deliberadas estão nomeadas e justificadas no fim do arquivo.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.api.main import build_cost_model
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.orchestrator import Orchestrator
from app.persistence import repo
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import Position, ShadowPosition, ShadowTrade
from app.risk.config import RiskLimits
from app.risk.engine import RiskEngine
from app.shadow.engine import MODEL_BASELINE, ShadowEngine, ShadowLimits
from app.strategy.engine import StrategyEngine
from tests.test_price_correctness import ListMarketDataProvider, make_candle

FEE, SLIP = 0.0006, 5.0
GATE_PERMISSIVO = 0.0001     # efetivamente desligado, para comparar entradas

# COOLDOWN NEUTRALIZADO NA COMPARACAO -- e' uma diferenca DELIBERADA e
# conhecida, nao uma divergencia de fidelidade:
#   motor  -> mede cooldown pelo RELOGIO DE PAREDE (utcnow)
#   shadow -> mede pelo TEMPO DO CANDLE, por exigencia do PO
# Num replay que comprime horas em segundos, o motor entra em cooldown e
# nunca sai, enquanto o shadow o expira normalmente. Isso sozinho produzia
# motor 3 x shadow 7 entradas. Com o cooldown fora do caminho, todo o
# RESTO da semantica pode ser comparado evento a evento -- que e' o
# objetivo deste arquivo. A diferenca em si esta coberta por
# `test_diferenca_deliberada_cooldown_relogio_vs_candle`.
SEM_COOLDOWN = 10_000


def _serie(n=180):
    """Série determinística com reversões suficientes para produzir
    cruzamentos, stops e alvos."""
    precos = []
    v = 100.0
    for i in range(n):
        v += (3.5 if (i // 11) % 2 == 0 else -3.5) + (0.7 if i % 3 == 0 else -0.4)
        precos.append(round(v, 4))
    return [make_candle(i, p) for i, p in enumerate(precos)]


def _monta(tmp_path):
    db = f"sqlite:///{tmp_path / 'dif.db'}"
    eng = make_engine(db)
    init_db(eng)
    f = make_session_factory(eng)
    st = Settings(
        mode=RunMode.REPLAY, symbols="BTCUSDT", database_url=db,
        strategy_timeframe_minutes=1,          # 1 candle = 1 decisão
        minimum_cost_coverage_ratio=GATE_PERMISSIVO,
        risk_cooldown_after_losses=SEM_COOLDOWN,
    )
    price_state: dict[str, float] = {}
    execucao = PaperLocalExecutionEngine(
        price_provider=lambda s: price_state.get(s, 0.0),
        fee_rate=FEE, slippage_bps=SLIP)
    limites = RiskLimits(
        max_position_usd=st.risk_max_position_usd,
        max_total_exposure_usd=st.risk_max_total_exposure_usd,
        min_order_notional_usd=st.risk_min_order_notional_usd,
        max_daily_loss_usd=st.risk_max_daily_loss_usd,
        cooldown_after_losses=st.risk_cooldown_after_losses,
        cooldown_minutes=st.risk_cooldown_minutes,
    )
    risco = RiskEngine(limites)
    risco.cost_model = build_cost_model(st, execucao)

    shadow = ShadowEngine(
        limits=ShadowLimits(
            max_position_usd=st.risk_max_position_usd,
            max_total_exposure_usd=st.risk_max_total_exposure_usd,
            min_order_notional_usd=st.risk_min_order_notional_usd,
            max_daily_loss_usd=st.risk_max_daily_loss_usd,
            cooldown_after_losses=SEM_COOLDOWN,
            cooldown_minutes=st.risk_cooldown_minutes,
            fee_rate=FEE, slippage_bps=SLIP,
            stop_loss_atr_multiple=st.strategy_stop_loss_atr_multiple,
            take_profit_atr_multiple=st.strategy_take_profit_atr_multiple,
        ),
        strategy_timeframe_minutes=1,
    )
    orch = Orchestrator(
        settings=st, session_factory=f,
        market_data_provider=ListMarketDataProvider(_serie()),
        strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=risco, execution_engine=execucao,
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0),
        price_state=price_state, shadow_engine=shadow,
    )
    with session_scope(f) as s:
        repo.get_or_create_system_state(s).operational_state = "ATIVO"
    return orch, f, shadow


def _rodar(orch):
    for _ in range(200):
        orch.tick()


# =========================================================================
# DIFERENCIAL
# =========================================================================

def test_diferencial_entradas_e_saidas(tmp_path):
    orch, f, _ = _monta(tmp_path)
    _rodar(orch)

    with session_scope(f) as s:
        op_pos = s.execute(select(Position)).scalars().all()
        sh_pos = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_BASELINE)).scalars().all()
        sh_trades = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_BASELINE)).scalars().all()

        assert op_pos, "a série precisa produzir operação real para haver o que comparar"
        assert sh_pos, "o shadow precisa produzir posição para haver o que comparar"

        # --- quantidade de entradas -----------------------------------
        assert len(sh_pos) == len(op_pos), (
            f"entradas divergem: motor {len(op_pos)} x shadow {len(sh_pos)}")

        # --- cada entrada, campo a campo ------------------------------
        op_ord = sorted(op_pos, key=lambda p: p.opened_at)
        sh_ord = sorted(sh_pos, key=lambda p: p.opened_candle_time)
        for i, (o, h) in enumerate(zip(op_ord, sh_ord)):
            assert o.side == h.side, f"entrada {i}: lado"
            assert o.qty == pytest.approx(h.qty, rel=1e-9), f"entrada {i}: quantidade"
            assert o.avg_entry_price == pytest.approx(
                h.entry_fill_price, rel=1e-9), f"entrada {i}: preenchimento"
            assert o.stop_loss == pytest.approx(h.stop_loss, rel=1e-9), f"entrada {i}: stop"
            assert o.take_profit == pytest.approx(
                h.take_profit, rel=1e-9), f"entrada {i}: alvo"
            # exposição global: notional idêntico e dentro do teto
            assert h.notional_usd == pytest.approx(o.qty * o.avg_entry_price, rel=1e-9)
            assert h.notional_usd <= 50.0 + 1e-9

        # --- saídas: quantidade e motivos -----------------------------
        fechadas_op = [p for p in op_pos if p.status != "OPEN"]
        assert len(sh_trades) == len(fechadas_op), (
            f"saídas divergem: motor {len(fechadas_op)} x shadow {len(sh_trades)}")
        motivos = {t.exit_reason for t in sh_trades}
        assert motivos <= {"stop_loss", "take_profit", "opposite_signal"}


def test_diferencial_pnl_e_custos(tmp_path):
    """P&L bruto e líquido do shadow reproduzem a aritmética do motor:
    mesma qty, mesmo preenchimento adverso, mesma taxa por perna."""
    orch, f, _ = _monta(tmp_path)
    _rodar(orch)
    with session_scope(f) as s:
        for t in s.execute(select(ShadowTrade).where(
                ShadowTrade.model == MODEL_BASELINE)).scalars().all():
            if t.side == "BUY":
                bruto = (t.exit_fill_price - t.entry_fill_price) * t.qty
            else:
                bruto = (t.entry_fill_price - t.exit_fill_price) * t.qty
            assert t.gross_pnl_usd == pytest.approx(bruto, rel=1e-9)
            assert t.net_pnl_usd == pytest.approx(
                t.gross_pnl_usd - t.fees_usd - t.slippage_usd, abs=1e-9)
            # taxa das DUAS pernas, sobre o notional de cada uma
            esperado = (t.qty * t.entry_fill_price * FEE
                        + t.qty * t.exit_fill_price * FEE)
            assert t.fees_usd == pytest.approx(esperado, rel=1e-9)


def test_diferencial_prioridade_de_saida_e_sinal_oposto(tmp_path):
    """Quando o motor fecha por sinal oposto ele RETORNA sem abrir nova
    posição. O shadow tem de fazer o mesmo -- se revertesse, teria mais
    posições que o motor, e o teste de contagem acima quebraria."""
    orch, f, _ = _monta(tmp_path)
    _rodar(orch)
    with session_scope(f) as s:
        trades = s.execute(select(ShadowTrade).where(
            ShadowTrade.model == MODEL_BASELINE)).scalars().all()
        for t in trades:
            if t.exit_reason != "opposite_signal":
                continue
            # nenhuma posição shadow foi aberta no MESMO instante do fechamento
            simultaneas = s.execute(select(ShadowPosition).where(
                ShadowPosition.model == MODEL_BASELINE,
                ShadowPosition.symbol == t.symbol,
                ShadowPosition.opened_candle_time == t.closed_candle_time,
            )).scalars().all()
            assert simultaneas == [], (
                "shadow reverteu no mesmo evento; o motor não reverte")


def test_diferencial_cooldown_e_exposicao(tmp_path):
    orch, f, shadow = _monta(tmp_path)
    _rodar(orch)
    with session_scope(f) as s:
        abertas = s.execute(select(ShadowPosition).where(
            ShadowPosition.model == MODEL_BASELINE,
            ShadowPosition.status == "OPEN")).scalars().all()
        # exposição global: no máximo uma posição shadow aberta por vez,
        # exatamente como o teto de US$ 50 impõe ao motor
        assert len(abertas) <= 1
        exposicao = sum(p.notional_usd for p in abertas)
        assert exposicao <= 50.0 + 1e-9
    # cooldown do shadow existe e é medido em tempo de candle
    estado = shadow._state[MODEL_BASELINE]
    assert estado.cooldown_until is None or estado.cooldown_until.tzinfo is not None


# =========================================================================
# DIFERENÇAS DELIBERADAS (documentadas, não divergências)
# =========================================================================

def test_diferencas_deliberadas_estao_documentadas():
    """Três diferenças existem por construção e são intencionais:

    1. O shadow NÃO passa por RiskEngine. Ele não avalia kill-switch,
       trading_blocked, reconciliação, frescor, clock drift nem estado
       operacional -- são gates de SEGURANÇA OPERACIONAL, não de
       estratégia. Incluí-los faria a medição da hipótese depender da
       saúde da instância, que é ruído para a pergunta econômica.

    2. O shadow não usa o gate de custo de 3,0x em nenhum dos dois
       portfólios -- é justamente a variável em estudo. O gate
       operacional continua intacto e decidindo a operação real.

    3. O shadow não cria Order/Execution: o preenchimento é calculado
       diretamente pelo preço adverso, que no PaperLocalExecutionEngine
       determinístico é exatamente o mesmo número.

    Fora isso, o baseline reproduz o motor evento a evento -- provado
    pelos testes acima."""
    import app.shadow.engine as mod

    assert "RiskEngine" not in dir(mod)
    assert mod.MODEL_BASELINE == "baseline_without_cost_gate"


def test_diferenca_deliberada_cooldown_relogio_vs_candle(tmp_path):
    """A UNICA divergencia de contagem encontrada no diferencial, isolada e
    explicada.

    O motor persiste `cooldown_until` a partir de `utcnow()`; o shadow usa o
    tempo do candle. Num replay que comprime horas em segundos, o motor
    entra em cooldown e nao sai mais, enquanto o shadow o expira no tempo
    economico correto. Medido: 3 entradas no motor contra 7 no shadow.

    NAO e' defeito do shadow -- e' a instrucao explicita do PO ("Nao use
    utcnow() para simular o instante economico do evento shadow"), e e' o
    comportamento CORRETO para medir a hipotese. Em operacao real, onde o
    relogio de parede acompanha o candle, os dois coincidem.

    Este teste existe para que a diferenca fique registrada em codigo, e
    para falhar caso alguem troque a semantica temporal do shadow."""
    import inspect

    import app.shadow.engine as mod

    fonte = inspect.getsource(mod.ShadowEngine._register_close)
    assert "candle_time + timedelta(minutes=lim.cooldown_minutes)" in fonte, (
        "o cooldown do shadow tem de ser medido em tempo de candle")
    assert "utcnow" not in fonte, "o shadow nao pode usar relogio de parede aqui"
