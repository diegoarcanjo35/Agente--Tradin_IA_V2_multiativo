"""Fase 3.3.1: distribuição da COBERTURA DE CUSTOS.

A Fase 3.3 expôs apenas a média (0,89×) e, com isso, calibrar o
`minimum_cost_coverage_ratio` continuava sendo adivinhação: uma média não
diz se as tentativas estão todas empilhadas logo abaixo do limiar (o
limiar está quase certo) ou espalhadas muito abaixo dele (o horizonte é
que está errado). Este módulo transforma as avaliações já persistidas em
`RiskEvaluation.checks_json` numa distribuição auditável.

Regras de honestidade, iguais às do resto do sistema:

- amostra vazia devolve `None` em todas as estatísticas, nunca zero;
- valor nulo/não numérico é descartado da amostra e CONTADO à parte,
  nunca tratado como zero;
- percentil determinístico pelo método do menor índice (nearest-rank),
  sem interpolação -- duas execuções sobre os mesmos dados devolvem
  exatamente o mesmo número;
- nenhum arredondamento antes do cálculo; arredondar é responsabilidade
  de quem apresenta.
"""
from __future__ import annotations


def _nearest_rank(ordenada: list[float], p: float) -> float:
    """Percentil determinístico por nearest-rank: o menor valor da
    amostra ordenada cuja posição cobre pelo menos `p` da amostra. Sem
    interpolação -- o resultado é sempre um valor OBSERVADO, nunca um
    número que nunca aconteceu."""
    n = len(ordenada)
    idx = max(1, -(-int(p * n * 100) // 100)) if n else 0  # ceil(p*n), mínimo 1
    idx = min(idx, n)
    return ordenada[idx - 1]


def coverage_distribution(samples: list[dict], required_ratio: float | None) -> dict:
    """`samples` são os dicts `cost_gate` de `repo.cost_gate_samples`."""
    avaliadas = len(samples)
    aprovadas = sum(1 for g in samples if g.get("cost_coverage_ok") is True)
    rejeitadas = sum(1 for g in samples if g.get("cost_coverage_ok") is False)

    ratios: list[float] = []
    sem_cobertura = 0
    for g in samples:
        r = g.get("achieved_coverage_ratio")
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            ratios.append(float(r))
        else:
            # `None` acontece legitimamente quando o custo configurado é
            # zero (cobertura indefinida, não infinita). Contado à parte.
            sem_cobertura += 1

    base = {
        "evaluated": avaliadas,
        "approved": aprovadas,
        "rejected": rejeitadas,
        "required_ratio": required_ratio,
        "samples_with_ratio": len(ratios),
        "samples_without_ratio": sem_cobertura,
    }

    if not ratios:
        base.update({
            "min": None, "p50": None, "mean": None, "p90": None, "max": None,
            "below_1x": None, "between_1x_2x": None, "between_2x_3x": None, "at_or_above_3x": None,
        })
        return base

    ordenada = sorted(ratios)
    base.update({
        "min": ordenada[0],
        "p50": _nearest_rank(ordenada, 0.50),
        "mean": sum(ordenada) / len(ordenada),
        "p90": _nearest_rank(ordenada, 0.90),
        "max": ordenada[-1],
        # Faixas fixas e mutuamente exclusivas -- somam sempre
        # `samples_with_ratio`. Deliberadamente ancoradas em 1x/2x/3x e
        # não no ratio configurado: 1x é a fronteira econômica real
        # (abaixo dela o movimento esperado não paga o custo), e mantê-las
        # fixas permite comparar distribuições entre configurações
        # diferentes.
        "below_1x": sum(1 for r in ordenada if r < 1.0),
        "between_1x_2x": sum(1 for r in ordenada if 1.0 <= r < 2.0),
        "between_2x_3x": sum(1 for r in ordenada if 2.0 <= r < 3.0),
        "at_or_above_3x": sum(1 for r in ordenada if r >= 3.0),
    })
    return base
