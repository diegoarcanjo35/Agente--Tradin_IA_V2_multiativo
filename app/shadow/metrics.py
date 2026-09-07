"""Métricas dos portfólios shadow. Somente leitura, nunca misturadas com
patrimônio, ordens ou posições operacionais."""
from __future__ import annotations

from app.persistence.models import (
    ShadowExperiment,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
)
from app.shadow.engine import HYPOTHESIS_VERSION, MODELS

# Gate de promoção — registrado em código para que a régua não dependa de
# memória de ninguém. H2 não vira regra operacional nesta fase.
# Autorizacao de FASE. Enquanto for False, `promotion_eligible` e' False
# independentemente de qualquer metrica -- e' a trava exigida pelo PO.
PROMOTION_ALLOWED_THIS_PHASE = False

MIN_CLOSED_TRADES = 60           # detecta ~US$ 0,10/trade
PREFERRED_CLOSED_TRADES = "200-230"
MAX_SYMBOL_CONCENTRATION = 0.70  # nenhum simbolo pode responder por mais que isto


def promotion_status(m: dict) -> dict:
    """Contrato REAL do gate, com quatro categorias distintas.

    Frase declarada nao e' criterio atendido. Aqui cada item diz se e'
    CALCULAVEL hoje e se esta ATENDIDO. Enquanto intervalo de confianca e
    validacao fora da amostra nao forem calculados de verdade, ficam em
    `pending`, e `promotion_eligible` e' obrigatoriamente False -- nenhuma
    quantidade de trades, sozinha, libera promocao."""
    n = m.get("closed_trades", 0)
    liquido = m.get("net_pnl_usd", 0.0)
    por_sim = m.get("per_symbol", {}) or {}
    total_sim = sum(v.get("closed_trades", 0) for v in por_sim.values())
    concentracao = ((max((v.get("closed_trades", 0) for v in por_sim.values()), default=0)
                     / total_sim) if total_sim else None)

    criterios = {
        "min_closed_trades": {
            "declared": ">= %d trades encerrados" % MIN_CLOSED_TRADES,
            "computable": True, "value": n, "met": n >= MIN_CLOSED_TRADES,
        },
        "positive_net_result": {
            "declared": "resultado liquido positivo",
            "computable": n > 0, "value": liquido, "met": n > 0 and liquido > 0,
        },
        "not_concentrated_in_one_symbol": {
            "declared": "nenhum simbolo acima de %.0f%% dos trades" % (MAX_SYMBOL_CONCENTRATION * 100),
            "computable": total_sim > 0, "value": concentracao,
            "met": concentracao is not None and concentracao <= MAX_SYMBOL_CONCENTRATION,
        },
        "confidence_interval_above_zero": {
            "declared": "IC 95% (bootstrap em blocos) inteiramente acima de zero",
            "computable": False, "value": None, "met": False,
            "why_not_computable": "calculo de intervalo de confianca nao implementado nesta fase",
        },
        "out_of_sample_validation": {
            "declared": "vantagem preservada em corte temporal fora da amostra",
            "computable": False, "value": None, "met": False,
            "why_not_computable": "validacao fora da amostra nao implementada nesta fase",
        },
    }
    pendentes = [k for k, v in criterios.items() if not v["met"]]
    nao_calculaveis = [k for k, v in criterios.items() if not v["computable"]]
    return {
        "criteria": criterios,
        "declared": list(criterios),
        "computable": [k for k, v in criterios.items() if v["computable"]],
        "met": [k for k, v in criterios.items() if v["met"]],
        "pending": pendentes,
        "not_yet_computable": nao_calculaveis,
        "preferred_closed_trades": PREFERRED_CLOSED_TRADES,
        # TRAVA DEFINITIVA. A elegibilidade e' a conjuncao de tres coisas:
        #   1. a fase autorizar promocao (hoje: NAO);
        #   2. todos os criterios serem CALCULAVEIS;
        #   3. todos estarem ATENDIDOS, sem nenhum pendente.
        # Mesmo que um dia (2) e (3) sejam satisfeitos, (1) mantem o
        # resultado False ate que a autorizacao seja explicitamente dada.
        "promotion_eligible": bool(
            PROMOTION_ALLOWED_THIS_PHASE
            and len(nao_calculaveis) == 0
            and len(pendentes) == 0
        ),
        "promotion_allowed_this_phase": PROMOTION_ALLOWED_THIS_PHASE,
    }


# Mantido para leitura humana; o contrato acionavel e' `promotion_status()`.
PROMOTION_GATE = {
    "min_closed_trades_for_review": MIN_CLOSED_TRADES,
    "preferred_closed_trades": PREFERRED_CLOSED_TRADES,
    "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
    "promotion_allowed_this_phase": PROMOTION_ALLOWED_THIS_PHASE,
}


def _agg(trades: list[ShadowTrade], capital: float = 1000.0) -> dict:
    n = len(trades)
    if n == 0:
        return {"closed_trades": 0, "win_rate": None, "payoff": None,
                "profit_factor": None, "expectancy_usd": None,
                "gross_pnl_usd": 0.0, "fees_usd": 0.0, "net_pnl_usd": 0.0,
                "drawdown_usd": 0.0, "exit_reasons": {}}
    liq = [t.net_pnl_usd for t in trades]
    ganhos = [x for x in liq if x > 0]
    perdas = [x for x in liq if x <= 0]
    eq = pico = capital
    dd = 0.0
    for x in liq:
        eq += x
        pico = max(pico, eq)
        dd = max(dd, pico - eq)
    razoes: dict[str, int] = {}
    for t in trades:
        razoes[t.exit_reason] = razoes.get(t.exit_reason, 0) + 1
    return {
        "closed_trades": n,
        "wins": len(ganhos),
        "win_rate": len(ganhos) / n,
        "payoff": ((sum(ganhos) / len(ganhos)) / abs(sum(perdas) / len(perdas)))
                  if ganhos and perdas and sum(perdas) != 0 else None,
        "profit_factor": (sum(ganhos) / abs(sum(perdas)))
                         if perdas and sum(perdas) != 0 else None,
        "expectancy_usd": sum(liq) / n,
        "gross_pnl_usd": sum(t.gross_pnl_usd for t in trades),
        "fees_usd": sum(t.fees_usd for t in trades),
        "slippage_usd": sum(t.slippage_usd for t in trades),
        "net_pnl_usd": sum(liq),
        "drawdown_usd": dd,
        "exit_reasons": razoes,
    }


def active_experiment(session, model: str):
    return session.query(ShadowExperiment).filter_by(model=model, status="ATIVO").first()


def model_metrics(session, model: str, experiment_id: int | None = None) -> dict:
    """Por padrao mede APENAS o experimento ativo. Experimentos diferentes
    nunca sao agregados em silencio -- misturar timeframes, custos ou
    limiares diferentes produziria um numero sem significado."""
    if experiment_id is None:
        exp = active_experiment(session, model)
        experiment_id = exp.id if exp else None
    else:
        exp = session.query(ShadowExperiment).filter_by(id=experiment_id).first()

    if experiment_id is None:
        ops, trades, abertas = [], [], []
    else:
        ops = session.query(ShadowOpportunity).filter_by(
            model=model, experiment_id=experiment_id).all()
        trades = session.query(ShadowTrade).filter_by(
            model=model, experiment_id=experiment_id).all()
        abertas = session.query(ShadowPosition).filter_by(
            model=model, experiment_id=experiment_id, status="OPEN").all()
    tempos = [t.closed_candle_time for t in trades]
    por_simbolo = {}
    for sym in {o.symbol for o in ops}:
        por_simbolo[sym] = {
            "opportunities": sum(1 for o in ops if o.symbol == sym),
            "approved": sum(1 for o in ops if o.symbol == sym and o.approved),
            "rejected": sum(1 for o in ops if o.symbol == sym and not o.approved),
            **_agg([t for t in trades if t.symbol == sym]),
        }
    base = {
        "model": model,
        "hypothesis_version": HYPOTHESIS_VERSION,
        "experiment_id": experiment_id,
        "experiment_uid": exp.experiment_uid if exp else None,
        "experiment_fingerprint": exp.config_fingerprint if exp else None,
        "experiment_status": exp.status if exp else None,
        "experiment_started_at": exp.started_at.isoformat() if exp else None,
        "threshold": exp.threshold if exp else None,
        "opportunities": len(ops),
        "approved": sum(1 for o in ops if o.approved),
        "rejected": sum(1 for o in ops if not o.approved),
        "open_positions": len(abertas),
        "observed_from": min(tempos).isoformat() if tempos else None,
        "observed_to": max(tempos).isoformat() if tempos else None,
        **_agg(trades),
        "per_symbol": por_simbolo,
    }
    base["promotion"] = promotion_status(base)
    return base


def comparative(session) -> dict:
    return {
        "models": {m: model_metrics(session, m) for m in MODELS},
        "promotion_gate": PROMOTION_GATE,
        "note": ("Numeros HIPOTETICOS do experimento ATIVO de cada modelo. "
                 "Nunca agregam experimentos diferentes e nunca se misturam "
                 "com patrimonio operacional."),
    }
