"""The Risk Engine has sole authority to approve an order -- for both opening
new exposure (`evaluate`) and closing/reducing existing exposure
(`evaluate_close`). It is deterministic code -- no ML, no strategy input
beyond the structured Signal -- and every check plus its outcome is recorded
so a rejection or approval is always explainable.

Structural enforcement of "no decision can bypass the Risk Engine": the
Execution Engine's public API only accepts an `ApprovedOrder`, and
`ApprovedOrder` can only be constructed with a `_RiskApprovalToken`, an opaque
sentinel that is private to this module. `_RiskApprovalToken` must never be
imported or instantiated outside app/risk/engine.py -- see
tests/test_risk_engine_boundary.py, which fails the build if any other file
does so.

Correction v1.2 #4: there is deliberately NO factory in production code that
mints a valid `ApprovedOrder` outside of `evaluate()`/`evaluate_close()` --
an earlier `make_test_approved_order()` helper was removed because, despite
being documented as "test-only", it was still importable and callable by any
code running in the same process, undermining the very guarantee this module
exists to provide. Tests obtain a real `ApprovedOrder` by actually calling
`evaluate()`/`evaluate_close()` with a valid signal/context -- see
tests/factories.py. The only test-support helper that remains here is
`attempt_construct_with_invalid_token_for_testing()`, which can never
succeed in producing a usable order (it always raises `TypeError`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.risk.config import RiskLimits
from app.risk.cost_model import CostModel, evaluate_cost_gate
from app.strategy.schemas import Signal


class _RiskApprovalToken:
    """Opaque sentinel. Private to app/risk/engine.py -- never import this."""


@dataclass(frozen=True)
class ApprovedOrder:
    signal_id: int
    symbol: str
    side: str  # BUY | SELL
    qty: float
    stop_loss: float | None
    take_profit: float | None
    token: _RiskApprovalToken
    is_close: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.token, _RiskApprovalToken):
            raise TypeError(
                "ApprovedOrder can only be constructed via RiskEngine.evaluate()/"
                "evaluate_close() -- no other code path may mint an approval token."
            )


@dataclass(frozen=True)
class RiskContext:
    open_positions_count: int
    open_exposure_usd: float
    daily_realized_loss_usd: float
    consecutive_losses: int
    data_is_stale: bool
    api_failure_count: int
    clock_drift_seconds: float | None
    kill_switch_engaged: bool
    trading_blocked: bool
    state_ambiguous: bool
    cooldown_until: datetime | None
    now: datetime
    # Fase 2, item 7.4: entry-only gate (never applied to evaluate_close --
    # a stale reconciliation must never block reducing/closing exposure).
    # Defaults to False so every pre-existing RiskContext construction
    # (which never mentions this field) is unaffected.
    reconciliation_stale: bool = False
    # Fase 2, item 7.8: entry-only gate -- "process running" vs "strategy
    # authorized to open new entries". Defaults to "ATIVO" so every
    # pre-existing RiskContext construction (predating this field) keeps
    # approving entries exactly as before; only orchestrator-built contexts
    # (which always pass the real SystemState.operational_state) can ever
    # be anything else.
    operational_state: str = "ATIVO"
    # Correção operacional do poll loop v1.0: entry-only gate (never
    # applied to evaluate_close -- a degraded market engine must never
    # block reducing/closing exposure). Defaults to False so every
    # pre-existing RiskContext construction is unaffected.
    engine_degraded: bool = False


@dataclass(frozen=True)
class RiskEvaluationResult:
    approved: bool
    reason: str
    checks: dict = field(default_factory=dict)
    approved_order: ApprovedOrder | None = None


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None, cost_model: CostModel | None = None):
        """Fase 3.2: `cost_model` liga o gate de viabilidade líquida. É
        opcional e default `None` -- sem ele o gate simplesmente não é
        aplicado e isso fica REGISTRADO em `checks["cost_gate"]` como
        `applied: false` (nunca como uma aprovação implícita disfarçada de
        sucesso). Produção sempre passa um: `app/api/main.py::
        build_orchestrator` o constrói a partir dos mesmos números que o
        motor de execução realmente usa."""
        self.limits = limits or RiskLimits()
        self.cost_model = cost_model

    # -- Shared gating checks used by both evaluate() and evaluate_close() ---

    def _check_common_gates(self, checks: dict, context: RiskContext) -> str | None:
        """Returns a (check_name, already recorded) rejection reason string,
        or None if every common gate passes. Mutates `checks` in place."""
        limits = self.limits

        checks["kill_switch_engaged"] = not context.kill_switch_engaged
        if context.kill_switch_engaged:
            return "Bloqueio de emergência ativado; todas as operações estão bloqueadas."

        checks["trading_blocked"] = not context.trading_blocked
        if context.trading_blocked:
            return "Sistema em estado de operações bloqueadas (TRADING_BLOCKED)."

        checks["state_not_ambiguous"] = not context.state_ambiguous
        if context.state_ambiguous:
            return "Estado local ambíguo em relação à corretora; reconciliação necessária."

        checks["data_fresh"] = not context.data_is_stale
        if context.data_is_stale:
            return "Dados de mercado desatualizados; operação recusada com dados obsoletos."

        drift = context.clock_drift_seconds
        checks["clock_synced"] = drift is not None and abs(drift) <= limits.max_clock_drift_seconds
        if not checks["clock_synced"]:
            if drift is None:
                return "Não foi possível determinar o drift do relógio; operação recusada."
            return f"Drift do relógio de {drift:.2f}s excede o limite de {limits.max_clock_drift_seconds}s."

        checks["api_failures_ok"] = context.api_failure_count < limits.max_api_failures
        if not checks["api_failures_ok"]:
            return (
                f"Contagem de falhas de API ({context.api_failure_count}) atingiu o limite "
                f"de {limits.max_api_failures}."
            )

        return None

    def evaluate(self, signal: Signal, signal_id: int, context: RiskContext) -> RiskEvaluationResult:
        checks: dict[str, bool] = {}
        limits = self.limits

        def reject(check_name: str, reason: str) -> RiskEvaluationResult:
            checks[check_name] = False
            return RiskEvaluationResult(approved=False, reason=reason, checks=checks)

        common_rejection = self._check_common_gates(checks, context)
        if common_rejection is not None:
            return RiskEvaluationResult(approved=False, reason=common_rejection, checks=checks)

        # Fase 2, item 7.4: entry-only -- evaluate_close() never checks this,
        # so a stale reconciliation never blocks reducing/closing exposure.
        checks["reconciliation_not_stale"] = not context.reconciliation_stale
        if context.reconciliation_stale:
            return reject(
                "reconciliation_not_stale",
                "Reconciliação periódica está atrasada além do limite configurado; novas "
                "aberturas de posição estão bloqueadas até uma reconciliação recente.",
            )

        # Fase 2, item 7.8: entry-only -- evaluate_close() never checks this,
        # so pausing/not-yet-activating the strategy never blocks
        # reducing/closing exposure (only new entries require explicit
        # operator activation).
        checks["operational_state_active"] = context.operational_state == "ATIVO"
        if not checks["operational_state_active"]:
            return reject(
                "operational_state_active",
                f"Estado operacional atual é {context.operational_state!r}; novas aberturas de "
                f"posição exigem ativação explícita do operador (estado ATIVO).",
            )

        # Correção operacional do poll loop v1.0: entry-only -- a degraded
        # market engine (DEGRADADO/PARADO/heartbeat vencido) never blocks
        # closing/reducing exposure, only new openings.
        checks["engine_not_degraded"] = not context.engine_degraded
        if context.engine_degraded:
            return reject(
                "engine_not_degraded",
                "Motor de mercado degradado ou com heartbeat vencido; novas aberturas de "
                "posição estão bloqueadas até o motor voltar a ficar saudável.",
            )

        checks["cooldown_expired"] = (
            context.cooldown_until is None or context.now >= context.cooldown_until
        )
        if not checks["cooldown_expired"]:
            return reject(
                "cooldown_expired",
                f"Cooldown ativo após {context.consecutive_losses} perdas consecutivas "
                f"até {context.cooldown_until.isoformat()}.",
            )

        checks["actionable_signal"] = signal.direction in ("BUY", "SELL")
        if not checks["actionable_signal"]:
            return reject("actionable_signal", "Direção do sinal é AGUARDAR (HOLD); nada a avaliar.")

        checks["stop_loss_present"] = (not limits.require_stop_loss) or signal.stop_loss is not None
        if not checks["stop_loss_present"]:
            return reject("stop_loss_present", "Ordem rejeitada: sinal sem stop-loss.")

        checks["daily_loss_within_limit"] = context.daily_realized_loss_usd < limits.max_daily_loss_usd
        if not checks["daily_loss_within_limit"]:
            return reject(
                "daily_loss_within_limit",
                f"Perda diária realizada de USD {context.daily_realized_loss_usd:.2f} atingiu "
                f"o limite de USD {limits.max_daily_loss_usd}.",
            )

        checks["concurrent_positions_ok"] = (
            context.open_positions_count < limits.max_concurrent_positions
        )
        if not checks["concurrent_positions_ok"]:
            return reject(
                "concurrent_positions_ok",
                f"Posições abertas ({context.open_positions_count}) atingiram o limite de "
                f"{limits.max_concurrent_positions}.",
            )

        remaining_exposure = limits.max_total_exposure_usd - context.open_exposure_usd
        checks["exposure_room_available"] = remaining_exposure > 0
        if not checks["exposure_room_available"]:
            return reject(
                "exposure_room_available",
                f"Exposição aberta (USD {context.open_exposure_usd:.2f}) já está no limite ou "
                f"acima dele (USD {limits.max_total_exposure_usd}).",
            )

        position_usd = min(limits.max_position_usd, remaining_exposure)
        checks["position_size_positive"] = position_usd > 0
        if not checks["position_size_positive"]:
            return reject("position_size_positive", "Tamanho de posição calculado não é positivo.")

        qty = position_usd / signal.observed_price

        # Fase 3.2: gate de VIABILIDADE LÍQUIDA -- último check antes da
        # aprovação, porque é o primeiro momento em que `qty` existe (o
        # custo é financeiro, não por unidade). Aplica-se EXCLUSIVAMENTE à
        # abertura: `evaluate_close` nunca o consulta, então uma posição
        # aberta jamais fica presa por não "valer a pena" fechar.
        if self.cost_model is not None:
            gate = evaluate_cost_gate(
                self.cost_model, signal.direction, qty, signal.observed_price, signal.atr,
            )
            checks["cost_gate"] = {"applied": True, **gate.detail}
            checks["cost_coverage_ok"] = gate.approved
            if not gate.approved:
                return reject("cost_coverage_ok", gate.reason)
        else:
            checks["cost_gate"] = {"applied": False}

        approved_order = ApprovedOrder(
            signal_id=signal_id,
            symbol=signal.symbol,
            side=signal.direction,
            qty=qty,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            token=_RiskApprovalToken(),
            is_close=False,
        )
        return RiskEvaluationResult(
            approved=True,
            reason=f"Aprovado: valor_posicao_usd={position_usd:.2f}, quantidade={qty:.8f}.",
            checks=checks,
            approved_order=approved_order,
        )

    def evaluate_close(
        self,
        signal_id: int,
        symbol: str,
        close_side: str,
        qty: float,
        position_exists: bool,
        position_qty: float,
        position_side: str,
        context: RiskContext,
    ) -> RiskEvaluationResult:
        """Approve a position-reducing/closing order. Deliberately skips the
        limits that exist only to gate *new* exposure (daily loss cap,
        concurrent-position cap, total-exposure cap, stop-loss requirement):
        closing a position reduces risk, so it is never blocked by those. It
        still enforces every safety gate that applies regardless of
        direction (kill switch, TRADING_BLOCKED, ambiguous state, stale
        data, clock sync, API health) plus close-specific sanity checks.
        """
        checks: dict[str, bool] = {}

        def reject(check_name: str, reason: str) -> RiskEvaluationResult:
            checks[check_name] = False
            return RiskEvaluationResult(approved=False, reason=reason, checks=checks)

        common_rejection = self._check_common_gates(checks, context)
        if common_rejection is not None:
            return RiskEvaluationResult(approved=False, reason=common_rejection, checks=checks)

        checks["position_exists"] = position_exists
        if not position_exists:
            return reject("position_exists", "Não há posição aberta para fechar neste símbolo.")

        checks["close_side_valid"] = close_side in ("BUY", "SELL") and close_side != position_side
        if not checks["close_side_valid"]:
            return reject(
                "close_side_valid",
                f"Lado de fechamento {close_side!r} deve ser COMPRA/VENDA e oposto ao lado da "
                f"posição {position_side!r}.",
            )

        checks["qty_positive"] = qty > 0
        if not checks["qty_positive"]:
            return reject("qty_positive", "Quantidade de fechamento deve ser positiva.")

        checks["qty_within_position"] = qty <= position_qty + 1e-12
        if not checks["qty_within_position"]:
            return reject(
                "qty_within_position",
                f"Quantidade de fechamento {qty} excede a quantidade da posição aberta {position_qty}.",
            )

        approved_order = ApprovedOrder(
            signal_id=signal_id,
            symbol=symbol,
            side=close_side,
            qty=qty,
            stop_loss=None,
            take_profit=None,
            token=_RiskApprovalToken(),
            is_close=True,
        )
        return RiskEvaluationResult(
            approved=True,
            reason=f"Fechamento aprovado: quantidade={qty:.8f} lado={close_side}.",
            checks=checks,
            approved_order=approved_order,
        )

    @staticmethod
    def attempt_construct_with_invalid_token_for_testing(bad_token: object) -> ApprovedOrder:
        """Test-only: proves ApprovedOrder rejects anything but a genuine
        _RiskApprovalToken -- exists so tests can verify the guard without
        importing ApprovedOrder or _RiskApprovalToken themselves. Always
        raises TypeError when `bad_token` isn't a real token."""
        return ApprovedOrder(
            signal_id=1, symbol="TEST", side="BUY", qty=0.001,
            stop_loss=None, take_profit=None, token=bad_token,  # type: ignore[arg-type]
        )

