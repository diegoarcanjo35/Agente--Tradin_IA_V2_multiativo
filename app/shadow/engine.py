"""Fase 3.4.3 — MOTOR SHADOW contrafactual.

Mede o que DOIS portfólios hipotéticos teriam feito, sem nenhuma capacidade
de gerar ordem operacional. O gate operacional de 3,0× segue intacto e
continua sendo o único a decidir operação real.

ISOLAMENTO (a propriedade mais importante deste módulo)
Este arquivo não importa `ExecutionEngine`, `RiskEngine`, `Order`,
`Execution`, `Position` nem `SystemState`. Não existe caminho de código
daqui até uma ordem, um fill, o patrimônio ou a sessão contábil. Ele lê
candles e sinais e escreve APENAS nas três tabelas `shadow_*`.

TEMPO
Todo instante econômico vem do CANDLE, nunca de `utcnow()`. Cooldown e
janela de perda diária são medidos em tempo de candle -- é o que torna o
resultado idêntico num reprocessamento e o que faltava ao replay
simplificado da Fase 3.4.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.core.logging import get_logger, log_event
from app.persistence.models import (
    ShadowExperiment,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
)
from app.risk.cost_model import adverse_fill_price

logger = get_logger(__name__)

# --- hipóteses CONGELADAS -------------------------------------------------
# O limiar do H2 está congelado em 0,15 por decisão do PO. Não há, e não
# deve haver, varredura de parâmetros nesta fase: qualquer valor escolhido
# olhando o resultado deixaria de ser hipótese e viraria sobreajuste.
HYPOTHESIS_VERSION = "h2-v1"
MODEL_BASELINE = "baseline_without_cost_gate"
MODEL_H2 = "h2_ma_separation_015"
H2_MIN_SEPARATION = 0.15

MODELS = (MODEL_BASELINE, MODEL_H2)


@dataclass
class ShadowLimits:
    """Espelha os limites operacionais em vigor. Recebidos prontos de quem
    constrói o motor -- o shadow nunca lê configuração por conta própria."""

    max_position_usd: float = 50.0
    max_total_exposure_usd: float = 50.0
    min_order_notional_usd: float = 5.0
    max_daily_loss_usd: float = 25.0
    cooldown_after_losses: int = 3
    cooldown_minutes: int = 30
    fee_rate: float = 0.0006
    slippage_bps: float = 5.0
    stop_loss_atr_multiple: float = 2.0
    take_profit_atr_multiple: float = 3.0
    minimum_cost_coverage_ratio: float = 3.0
    expected_move_atr_multiple: float = 1.0

    @property
    def slippage_fraction(self) -> float:
        return self.slippage_bps / 10_000.0


def _threshold_for(model: str) -> float | None:
    """Limiar CONGELADO do modelo. O baseline não tem limiar."""
    return H2_MIN_SEPARATION if model == MODEL_H2 else None


@dataclass
class _PortfolioState:
    """Estado em memória de UM portfólio. Nunca compartilhado: cada modelo
    tem a sua instância, com posição, cooldown e perda diária próprios."""

    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    daily_loss: dict[str, float] = field(default_factory=dict)


class ShadowEngine:
    """Um motor, dois portfólios independentes.

    Chamado de dois pontos do tick operacional, ambos protegidos por
    try/except no chamador:
      - `on_operational_candle` a cada candle de 1 min (stop/alvo);
      - `on_strategy_signal` a cada bucket estratégico fechado.
    """

    def __init__(self, limits: ShadowLimits | None = None,
                 strategy_timeframe_minutes: int = 15,
                 strategy_version: str = "v1", strategy_config=None):
        self.limits = limits or ShadowLimits()
        self.strategy_timeframe_minutes = strategy_timeframe_minutes
        self.strategy_version = strategy_version
        # Config COMPLETA da estrategia, serializada inteira no fingerprint.
        # Receber o dataclass em vez de campos soltos evita a lista manual
        # que esqueceria um campo novo no futuro.
        if strategy_config is None:
            from app.strategy.engine import StrategyConfig
            strategy_config = StrategyConfig(timeframe_minutes=strategy_timeframe_minutes)
        self.strategy_config = strategy_config
        self.fast_period = strategy_config.fast_period
        self.slow_period = strategy_config.slow_period
        self.atr_period = strategy_config.atr_period
        self._state = {m: _PortfolioState() for m in MODELS}
        self._experiment_id: dict[str, int] = {}
        # Saída no MESMO tick tem prioridade sobre entrada nova, exatamente
        # como no motor (orchestrator.py: `if stop_take_result is not None:
        # ... return`). Guardamos o candle da última saída por (modelo,
        # símbolo) para reproduzir esse bloqueio.
        self._exit_this_tick: dict[tuple[str, str], datetime] = {}
        self.failures = 0
        self._consecutive_by_op: dict[str, int] = {}
        self.last_error: str | None = None
        self.last_failed_operation: str | None = None
        self.last_event_at: datetime | None = None

    # ------------------------------------------------- identidade do experimento
    def _config_snapshot(self, model: str) -> dict:
        """Snapshot SANITIZADO e determinístico da configuração congelada.

        Serializa os dataclasses INTEIROS (`StrategyConfig` e
        `ShadowLimits`, este último contendo limites de risco e modelo de
        custo) em vez de uma lista manual de campos -- uma lista manual
        esqueceria silenciosamente qualquer campo acrescentado no futuro, e
        o experimento continuaria "o mesmo" depois de uma mudança real.

        Fora do snapshot, de propósito: segredo, credencial, caminho de
        banco, e qualquer parâmetro de CADÊNCIA/LOG (intervalo de
        reconciliação, nível de log, intervalo de polling) -- eles não
        alteram o significado econômico da hipótese e não devem encerrar um
        experimento em andamento."""
        return {
            "model": model,
            "hypothesis": {
                "version": HYPOTHESIS_VERSION,
                "threshold": _threshold_for(model),
            },
            "strategy_version": self.strategy_version,
            "strategy_timeframe_minutes": self.strategy_timeframe_minutes,
            "strategy_config": asdict(self.strategy_config),
            "limits_and_costs": asdict(self.limits),
        }

    @staticmethod
    def fingerprint(snapshot: dict) -> str:
        """SHA-256 sobre JSON com chaves ordenadas: mesma configuração produz
        o mesmo fingerprint em qualquer máquina e em qualquer execução."""
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def experiment_id(self, session, model: str, agora: datetime) -> int:
        """Resolve o experimento ATIVO deste modelo, pela EXECUÇÃO e não
        pela configuração.

        Regra temporal (A -> B -> A2):
          - existe ativo com o MESMO fingerprint  -> continua nele;
          - existe ativo com fingerprint DIFERENTE -> encerra e cria novo;
          - não existe ativo                       -> cria novo.

        Voltar a uma configuração antiga NUNCA ressuscita o experimento
        antigo: nasce uma execução nova, com `experiment_uid` e
        `started_at` próprios. É o que impede que duas janelas temporais
        distintas sejam agregadas em silêncio só por terem a mesma
        configuração."""
        if model in self._experiment_id:
            return self._experiment_id[model]
        snap = self._config_snapshot(model)
        fp = self.fingerprint(snap)

        ativo = session.query(ShadowExperiment).filter_by(
            model=model, status="ATIVO").first()
        if ativo is not None:
            if ativo.config_fingerprint == fp:
                self._experiment_id[model] = ativo.id
                return ativo.id
            ativo.status = "ENCERRADO"
            ativo.ended_at = agora
            ativo.end_reason = "Configuração do experimento alterada; substituído."
            session.flush()   # libera o índice único parcial antes do novo

        lim = self.limits
        novo = ShadowExperiment(
            experiment_uid=str(uuid.uuid4()), model=model,
            hypothesis_version=HYPOTHESIS_VERSION, threshold=_threshold_for(model),
            strategy_timeframe_minutes=self.strategy_timeframe_minutes,
            strategy_version=self.strategy_version, fast_period=self.fast_period,
            slow_period=self.slow_period, atr_period=self.atr_period,
            fee_rate=lim.fee_rate, slippage_bps=lim.slippage_bps,
            stop_loss_atr_multiple=lim.stop_loss_atr_multiple,
            take_profit_atr_multiple=lim.take_profit_atr_multiple,
            max_position_usd=lim.max_position_usd,
            max_total_exposure_usd=lim.max_total_exposure_usd,
            min_order_notional_usd=lim.min_order_notional_usd,
            max_daily_loss_usd=lim.max_daily_loss_usd,
            cooldown_after_losses=lim.cooldown_after_losses,
            cooldown_minutes=lim.cooldown_minutes,
            config_fingerprint=fp,
            config_snapshot_json=json.dumps(snap, sort_keys=True),
            started_at=agora, status="ATIVO",
        )
        session.add(novo)
        session.flush()
        self._experiment_id[model] = novo.id
        return novo.id

    # ------------------------------------------------------------------ util
    def _open_position(self, session, model: str, symbol: str | None = None):
        q = session.query(ShadowPosition).filter(
            ShadowPosition.model == model, ShadowPosition.status == "OPEN")
        if symbol is not None:
            q = q.filter(ShadowPosition.symbol == symbol)
        return q.first()

    def _open_exposure(self, session, model: str) -> float:
        return sum(
            p.notional_usd for p in session.query(ShadowPosition).filter(
                ShadowPosition.model == model, ShadowPosition.status == "OPEN").all()
        )

    def _register_close(self, session, model: str, pos: ShadowPosition,
                        exit_reference: float, reason: str, candle_time: datetime) -> None:
        """Fecha a posição hipotética e grava o trade. Idempotente pela
        chave única (experiment_id, symbol, opened_candle_time,
        closed_candle_time) -- a mesma do índice do banco."""
        lim = self.limits
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        exit_fill = adverse_fill_price(exit_reference, close_side, lim.slippage_fraction)
        if pos.side == "BUY":
            gross = (exit_fill - pos.entry_fill_price) * pos.qty
        else:
            gross = (pos.entry_fill_price - exit_fill) * pos.qty
        exit_fee = pos.qty * exit_fill * lim.fee_rate
        exit_slip = abs(exit_fill - exit_reference) * pos.qty
        fees = pos.entry_fee_usd + exit_fee
        slippage = pos.entry_slippage_usd + exit_slip
        net = gross - exit_fee - exit_slip - pos.entry_fee_usd - pos.entry_slippage_usd

        # Idempotente pela chave do BANCO, que inclui o experimento: o
        # trade pertence ao experimento que ABRIU a posição, mesmo que o
        # fechamento aconteça quando outro experimento já está ativo.
        ja = session.query(ShadowTrade).filter_by(
            experiment_id=pos.experiment_id, symbol=pos.symbol,
            opened_candle_time=pos.opened_candle_time,
            closed_candle_time=candle_time).first()
        if ja is None:
            session.add(ShadowTrade(
                experiment_id=pos.experiment_id,
                model=model, hypothesis_version=HYPOTHESIS_VERSION, symbol=pos.symbol,
                side=pos.side, qty=pos.qty, entry_fill_price=pos.entry_fill_price,
                exit_fill_price=exit_fill, notional_usd=pos.notional_usd,
                stop_loss=pos.stop_loss, take_profit=pos.take_profit,
                exit_reason=reason, opened_candle_time=pos.opened_candle_time,
                closed_candle_time=candle_time,
                duration_minutes=max(0, int(
                    (candle_time - pos.opened_candle_time).total_seconds() // 60)),
                gross_pnl_usd=gross, fees_usd=fees, slippage_usd=slippage,
                net_pnl_usd=net, normalized_separation=pos.normalized_separation,
            ))
        pos.status = "CLOSED"
        session.flush()

        st = self._state[model]
        if net < 0:
            dia = candle_time.strftime("%Y-%m-%d")
            st.daily_loss[dia] = st.daily_loss.get(dia, 0.0) + (-net)
            st.consecutive_losses += 1
            if st.consecutive_losses >= lim.cooldown_after_losses:
                # Cooldown em TEMPO DE CANDLE, não de parede.
                st.cooldown_until = candle_time + timedelta(minutes=lim.cooldown_minutes)
                st.consecutive_losses = 0
        else:
            st.consecutive_losses = 0

    # -------------------------------------------------- candle de 1 minuto
    def on_operational_candle(self, session, symbol: str, high: float, low: float,
                              close: float, candle_time: datetime) -> None:
        """Stop e alvo, avaliados a cada candle de 1 min, com a MESMA
        prioridade do motor: a saída vem antes de qualquer entrada nova, e
        quando stop e alvo caem no mesmo candle assume-se o stop."""
        for model in MODELS:
            pos = self._open_position(session, model, symbol)
            if pos is None:
                continue
            # Um candle ANTERIOR a abertura nunca pode fechar a posicao: seria
            # look-ahead invertido, decidindo o passado com informacao futura.
            if candle_time <= pos.opened_candle_time:
                continue
            if pos.side == "BUY":
                bateu_stop, bateu_alvo = low <= pos.stop_loss, high >= pos.take_profit
            else:
                bateu_stop, bateu_alvo = high >= pos.stop_loss, low <= pos.take_profit
            if not (bateu_stop or bateu_alvo):
                continue
            gatilho = pos.stop_loss if bateu_stop else pos.take_profit
            razao = "stop_loss" if bateu_stop else "take_profit"
            self._register_close(session, model, pos, gatilho, razao, candle_time)
            # Marca a saida deste tick: no motor, `if stop_take_result is not
            # None: ... return` impede QUALQUER entrada nova no mesmo tick.
            self._exit_this_tick[(model, symbol)] = candle_time
        self.last_event_at = candle_time

    # ---------------------------------------------- bucket estratégico
    def on_strategy_signal(self, session, symbol: str, direction: str,
                           reference_price: float, atr: float, fast_sma: float,
                           slow_sma: float, source_candle_open_time: datetime,
                           signal_is_fresh: bool | None = None,
                           tick_candle_time: datetime | None = None) -> None:
        """Avalia a oportunidade nos dois portfólios. Só sinais acionáveis
        (BUY/SELL) chegam aqui."""
        if direction not in ("BUY", "SELL") or not atr or atr <= 0 or reference_price <= 0:
            return
        lim = self.limits
        separacao = abs(fast_sma - slow_sma) / atr
        cobertura = self._cost_coverage(reference_price, atr)
        gate_op = cobertura is not None and cobertura >= lim.minimum_cost_coverage_ratio

        for model in MODELS:
            # O EXPERIMENTO é resolvido ANTES da checagem de idempotência: a
            # chave é (experimento, símbolo, candle), nunca (modelo, símbolo,
            # candle). O mesmo candle pode aparecer legitimamente em
            # experimentos diferentes -- em A e depois em A2 -- e tratar isso
            # como repetição apagaria em silêncio a observação da execução
            # nova. A pré-checagem espelha exatamente o índice único do banco.
            exp_id = self.experiment_id(session, model, source_candle_open_time)
            if session.query(ShadowOpportunity).filter_by(
                    experiment_id=exp_id, symbol=symbol,
                    source_candle_open_time=source_candle_open_time).first() is not None:
                continue

            aprovado, razao = self._decide(
                session, model, symbol, direction, separacao, reference_price,
                atr, source_candle_open_time, tick_candle_time)

            session.add(ShadowOpportunity(
                experiment_id=exp_id, model=model, hypothesis_version=HYPOTHESIS_VERSION, symbol=symbol,
                source_candle_open_time=source_candle_open_time,
                strategy_timeframe_minutes=self.strategy_timeframe_minutes,
                direction=direction, reference_price=reference_price,
                fast_sma=fast_sma, slow_sma=slow_sma, atr=atr,
                normalized_separation=separacao, cost_coverage_ratio=cobertura,
                approved=aprovado, reason=razao, warmup_ready=True,
                signal_is_fresh=signal_is_fresh,
                operational_gate_would_approve=gate_op,
            ))
            session.flush()
            if aprovado:
                self._open(session, model, symbol, direction, reference_price,
                           atr, separacao, source_candle_open_time, exp_id)
        self.last_event_at = source_candle_open_time

    # ------------------------------------------------------------ decisão
    def _decide(self, session, model, symbol, direction, separacao,
                reference_price, atr, candle_time,
                tick_candle_time=None) -> tuple[bool, str]:
        lim = self.limits
        st = self._state[model]

        # 1) SAÍDA POR STOP/ALVO NO MESMO TICK bloqueia entrada nova.
        # Fidelidade a orchestrator.py: `if stop_take_result is not None:
        # ... return` -- o sinal é registrado, mas nenhuma entrada é avaliada.
        saida = self._exit_this_tick.get((model, symbol))
        if saida is not None and tick_candle_time is not None and saida == tick_candle_time:
            return False, ("saída por stop/alvo no mesmo tick tem prioridade; "
                           "nenhuma entrada é avaliada.")

        # 2) SINAL OPOSTO fecha a posição e NÃO reverte no mesmo tick.
        # Fidelidade a orchestrator.py::_maybe_close_opposing_position: o
        # motor fecha e RETORNA, deixando a reentrada para uma oportunidade
        # posterior. Reverter aqui inflaria os trades do shadow.
        pos = self._open_position(session, model, symbol)
        if pos is not None and pos.side != direction:
            self._register_close(session, model, pos, reference_price,
                                 "opposite_signal", candle_time)
            return False, ("sinal oposto encerrou a posição; o motor não "
                           "reverte no mesmo evento.")

        if model == MODEL_H2 and separacao < H2_MIN_SEPARATION:
            return False, (f"H2: separação normalizada {separacao:.4f} < "
                           f"{H2_MIN_SEPARATION} — cruzamento marginal.")
        if st.cooldown_until is not None and candle_time < st.cooldown_until:
            return False, f"cooldown ativo até {st.cooldown_until.isoformat()}."
        if pos is not None:
            return False, "já existe posição hipotética aberta neste símbolo."
        if st.daily_loss.get(candle_time.strftime("%Y-%m-%d"), 0.0) >= lim.max_daily_loss_usd:
            return False, "limite de perda diária hipotética atingido."

        restante = lim.max_total_exposure_usd - self._open_exposure(session, model)
        if restante <= 0:
            return False, "exposição hipotética global esgotada."
        position_usd = min(lim.max_position_usd, restante)
        if position_usd < lim.min_order_notional_usd - 1e-4:
            return False, (f"notional disponível US$ {position_usd:.8f} abaixo do "
                           f"mínimo de US$ {lim.min_order_notional_usd:.2f}.")
        return True, "aprovado no shadow."

    def _open(self, session, model, symbol, direction, reference_price, atr,
              separacao, candle_time, experiment_id: int) -> None:
        lim = self.limits
        restante = lim.max_total_exposure_usd - self._open_exposure(session, model)
        position_usd = min(lim.max_position_usd, restante)
        # Mesmo dimensionamento consciente de slippage da correção 3.4.2.
        fill = adverse_fill_price(reference_price, direction, lim.slippage_fraction)
        qty = position_usd / fill
        fee = qty * fill * lim.fee_rate
        slip = abs(fill - reference_price) * qty
        d_stop, d_alvo = atr * lim.stop_loss_atr_multiple, atr * lim.take_profit_atr_multiple
        session.add(ShadowPosition(
            experiment_id=experiment_id,
            model=model, symbol=symbol, side=direction, qty=qty,
            entry_fill_price=fill, reference_price=reference_price,
            notional_usd=qty * fill,
            # Stop e alvo reancorados no preenchimento HIPOTÉTICO.
            stop_loss=fill - d_stop if direction == "BUY" else fill + d_stop,
            take_profit=fill + d_alvo if direction == "BUY" else fill - d_alvo,
            entry_fee_usd=fee, entry_slippage_usd=slip, atr_at_decision=atr,
            normalized_separation=separacao, opened_candle_time=candle_time,
            status="OPEN",
        ))
        # FLUSH obrigatório: sem ele a posição recém-adicionada fica pendente
        # e as consultas seguintes DENTRO da mesma transação não a enxergam --
        # a exposição global deixaria de bloquear e o stop/alvo não acharia a
        # posição para fechar. Em produção cada tick tem sua própria sessão,
        # mas o motor não pode depender disso para estar correto.
        session.flush()

    def _cost_coverage(self, price: float, atr: float) -> float | None:
        """A cobertura ATR/custo, OBSERVADA para comparação. Nenhum portfólio
        shadow a utiliza como filtro -- ela não está validada como preditiva."""
        lim = self.limits
        custo_pct = 2 * (lim.fee_rate + lim.slippage_fraction) * 100
        if custo_pct <= 0:
            return None
        return (atr / price * 100 * lim.expected_move_atr_multiple) / custo_pct

    # ------------------------------------------------------------- saúde
    # ------------------------------------------------------------- saúde
    # SEMÂNTICA DOCUMENTADA
    #   failures ................ contador CUMULATIVO da vida do processo;
    #                             nunca zera, é o histórico de incidentes.
    #   consecutive_failures .... falhas seguidas SEM sucesso no meio; é
    #                             ele, não o cumulativo, que define o
    #                             status.
    #   last_error .............. mensagem da última falha; permanece
    #                             visível após a recuperação, como registro.
    #   status .................. DEGRADADO enquanto houver falha
    #                             consecutiva; volta a SAUDAVEL no primeiro
    #                             ciclo shadow bem-sucedido.
    # Nada disso é persistido: vive em memória, então um reinício começa
    # SAUDAVEL. Uma falha histórica jamais vira bloqueio operacional --
    # aliás, a saúde shadow não bloqueia nada, em momento algum.
    # A contagem consecutiva e' POR OPERACAO (`on_operational_candle`,
    # `on_strategy_signal`). Um contador unico seria enganoso: a checagem de
    # stop/alvo roda em todo candle e quase nunca falha, entao ela zeraria o
    # contador da avaliacao de sinal mesmo que ESTA falhasse em 100% das
    # vezes -- e a saude reportaria SAUDAVEL com um defeito permanente
    # embaixo. Basta uma operacao com falha consecutiva para o status ser
    # DEGRADADO.
    RECOVERY_RULE = ("cada operação shadow volta a SAUDAVEL no primeiro "
                     "ciclo bem-sucedido DELA MESMA; basta uma operação em "
                     "falha para o status geral ser DEGRADADO; `failures` é "
                     "cumulativo e nunca zera")

    def record_failure(self, exc: Exception, operacao: str = "shadow") -> None:
        """Saúde PRÓPRIA da instrumentação. Uma falha aqui nunca toca a
        saúde de mercado nem interrompe o tick operacional."""
        self.failures += 1
        self._consecutive_by_op[operacao] = self._consecutive_by_op.get(operacao, 0) + 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.last_failed_operation = operacao
        log_event(logger, 40, "shadow_instrumentation_failed",
                  operation=operacao, detail=self.last_error)

    def record_success(self, operacao: str = "shadow") -> None:
        """Chamado após um ciclo shadow DESTA operação que completou sem
        exceção. Recupera apenas a própria operação."""
        self._consecutive_by_op[operacao] = 0

    @property
    def consecutive_failures(self) -> int:
        """Pior operação: o status não pode ser melhor que o pior hook."""
        return max(self._consecutive_by_op.values(), default=0)

    def health(self) -> dict:
        return {
            "status": "SAUDAVEL" if self.consecutive_failures == 0 else "DEGRADADO",
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_failures_by_operation": dict(self._consecutive_by_op),
            "last_error": self.last_error,
            "last_failed_operation": self.last_failed_operation,
            "recovery_rule": self.RECOVERY_RULE,
            "blocks_operation": False,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "models": list(MODELS),
            "hypothesis_version": HYPOTHESIS_VERSION,
            "h2_min_separation": H2_MIN_SEPARATION,
        }
