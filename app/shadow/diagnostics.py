"""Fase 3.5 -- diagnóstico agregado do funil operacional e do shadow.

Todo cálculo aqui é SOMENTE LEITURA e SOMENTE AGREGAÇÃO no backend: o
painel nunca baixa linha a linha para recalcular no navegador. Cada
função aceita uma janela temporal explícita (nunca mistura lifetime com
período) e devolve um dicionário serializável.

Correção da auditoria (2ª rodada): `comparison`/`significance`/`progress`
e o ranking de rejeições agora escopam pelo EXPERIMENTO ATIVO de cada
modelo (`ShadowTrade.experiment_id` / `ShadowOpportunity.experiment_id`),
nunca por "todos os trades desse `model`" -- se um dia houver rotação de
configuração (A -> B -> A2, Fase 3.4.3), lifetime-por-model misturaria
configurações diferentes silenciosamente. `funnel`/`cost_gate_distribution`
não têm esse risco: sinais e avaliações de risco reais não pertencem a
nenhum experimento shadow, só à janela de tempo.

Nada aqui altera estratégia, gate, threshold ou qualquer estado -- são
apenas consultas sobre dados já persistidos pelo motor real e pelo
shadow.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.persistence.models import (
    Candle,
    RiskEvaluation,
    ShadowExperiment,
    ShadowOpportunity,
    ShadowPosition,
    ShadowTrade,
    StrategySignal,
)
from app.shadow.engine import MODEL_BASELINE, MODEL_H2, MODELS
from app.shadow.metrics import _agg, active_experiment

# Marcos de amostra do experimento (Fase 3.4.3/3.5) -- puramente
# informativos, nunca liberam promoção sozinhos.
MILESTONES = [
    (15, "observação preliminar"),
    (30, "revisão intermediária"),
    (60, "primeira análise formal"),
    (200, "avaliação de vantagem pequena (mínimo)"),
    (230, "avaliação de vantagem pequena (máximo)"),
]

SCOPES = ("experiment", "24h", "7d", "custom")


def _parse_dt(valor: str) -> datetime:
    dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_since(since: str | None, default_days: int = 7) -> datetime:
    if since:
        return _parse_dt(since)
    return datetime.now(timezone.utc) - timedelta(days=default_days)


def _parse_until(until: str | None) -> datetime:
    if until:
        return _parse_dt(until)
    return datetime.now(timezone.utc)


def resolve_window(session: Session, scope: str, since: str | None = None,
                   until: str | None = None) -> tuple[datetime, datetime, str]:
    """Resolve a janela CANÔNICA de uma requisição.

    'experiment' (padrão): desde o `started_at` do experimento ATIVO mais
      antigo entre baseline e H2, até `until` (ou agora). Esta é a janela
      que o painel principal usa -- nunca mistura com sessões operacionais
      anteriores nem com configurações de experimento já encerradas.
    '24h' / '7d': janelas rolantes fixas, terminando em `until` (ou agora).
    'custom': usa `since`/`until` exatamente como fornecidos.
    """
    if scope not in SCOPES:
        scope = "experiment"
    dt_until = _parse_until(until)
    if scope == "custom":
        dt_since = _parse_since(since, default_days=7)
        rotulo = "personalizado"
    elif scope == "24h":
        dt_since = dt_until - timedelta(hours=24)
        rotulo = "últimas 24 horas"
    elif scope == "7d":
        dt_since = dt_until - timedelta(days=7)
        rotulo = "últimos 7 dias"
    else:
        inicio = session.execute(
            select(func.min(ShadowExperiment.started_at)).where(ShadowExperiment.status == "ATIVO")
        ).scalar_one_or_none()
        dt_since = inicio or (dt_until - timedelta(days=7))
        rotulo = "experimento ativo"
    return dt_since, dt_until, rotulo


def funnel(session: Session, since: str | None = None, until: str | None = None,
          avaliacoes_reais: list | None = None) -> dict:
    """Funil completo, etapa por etapa, dentro de UMA janela explícita.
    Nunca mistura contagem lifetime com contagem do período. Sinais e
    avaliações de risco REAIS não pertencem a nenhum experimento shadow --
    janela de tempo é o único escopo que faz sentido para eles.

    `avaliacoes_reais`: correção de desempenho -- `cost_gate_distribution`
    busca exatamente as MESMAS `RiskEvaluation` (mesmo filtro de sinais
    reais na mesma janela). Quando o chamador (`dashboard`) já carregou
    essa lista, ela é reaproveitada aqui -- nunca reconsultada. Chamada
    isolada (rota individual) continua consultando por conta própria,
    comportamento inalterado."""
    dt_since, dt_until = _parse_since(since), _parse_until(until)

    candles_por_simbolo = dict(session.execute(
        select(Candle.symbol, func.count())
        .where(Candle.open_time >= dt_since, Candle.open_time <= dt_until)
        .group_by(Candle.symbol)
    ).all())

    sinais = session.execute(
        select(StrategySignal.direction, func.count())
        .where(StrategySignal.created_at >= dt_since, StrategySignal.created_at <= dt_until)
        .group_by(StrategySignal.direction)
    ).all()
    por_direcao = dict(sinais)
    total_sinais = sum(por_direcao.values())
    acionaveis = total_sinais - por_direcao.get("HOLD", 0)

    buckets = session.execute(
        select(StrategySignal.symbol, func.count(distinct(StrategySignal.source_candle_open_time)))
        .where(StrategySignal.created_at >= dt_since, StrategySignal.created_at <= dt_until)
        .group_by(StrategySignal.symbol)
    ).all()

    if avaliacoes_reais is not None:
        avaliacoes = avaliacoes_reais
    else:
        sinais_ids = select(StrategySignal.id).where(
            StrategySignal.created_at >= dt_since, StrategySignal.created_at <= dt_until,
            StrategySignal.direction != "HOLD")
        avaliacoes = session.execute(
            select(RiskEvaluation).where(RiskEvaluation.signal_id.in_(sinais_ids))
        ).scalars().all()
    aprovadas_real = sum(1 for a in avaliacoes if a.approved)

    oport = session.execute(
        select(ShadowOpportunity.model, ShadowOpportunity.approved, func.count())
        .where(ShadowOpportunity.source_candle_open_time >= dt_since,
               ShadowOpportunity.source_candle_open_time <= dt_until)
        .group_by(ShadowOpportunity.model, ShadowOpportunity.approved)
    ).all()
    oport_por_modelo: dict[str, dict[str, int]] = {m: {"aprovadas": 0, "rejeitadas": 0} for m in MODELS}
    for modelo, aprovado, n in oport:
        oport_por_modelo.setdefault(modelo, {"aprovadas": 0, "rejeitadas": 0})
        oport_por_modelo[modelo]["aprovadas" if aprovado else "rejeitadas"] += n

    return {
        "janela": {"desde": dt_since.isoformat(), "ate": dt_until.isoformat()},
        "etapas": [
            {"etapa": "candles_1m", "por_simbolo": candles_por_simbolo,
             "total": sum(candles_por_simbolo.values())},
            {"etapa": "buckets_15m_fechados", "por_simbolo": dict(buckets),
             "total": sum(n for _, n in buckets)},
            {"etapa": "sinais_gerados", "por_direcao": por_direcao, "total": total_sinais},
            {"etapa": "sinais_acionaveis", "total": acionaveis,
             "taxa_do_anterior": (acionaveis / total_sinais) if total_sinais else None},
            {"etapa": "avaliados_risk_engine", "total": len(avaliacoes),
             "taxa_do_anterior": (len(avaliacoes) / acionaveis) if acionaveis else None},
            {"etapa": "aprovados_gate_operacional_real", "total": aprovadas_real,
             "taxa_do_anterior": (aprovadas_real / len(avaliacoes)) if avaliacoes else None},
            {"etapa": "ordens_criadas", "total": 0, "taxa_do_anterior": None},
            {"etapa": "fills", "total": 0, "taxa_do_anterior": None},
            {"etapa": "posicoes_operacionais", "total": 0, "taxa_do_anterior": None},
        ],
        "shadow_por_modelo": oport_por_modelo,
        "nota": "sinais_acionaveis/avaliados_risk_engine é sempre 1:1 -- o "
                "RiskEngine avalia exatamente cada sinal BUY/SELL, uma vez. "
                "'282 avaliações' citado em relatórios anteriores é contagem "
                "LIFETIME (desde 01/09, todas as sessões); dentro de QUALQUER "
                "janela o número bate exatamente com os sinais acionáveis "
                "dela, sem duplicidade e sem órfãos.",
    }


def cost_gate_distribution(session: Session, since: str | None = None, until: str | None = None,
                           avaliacoes_reais: list | None = None) -> dict:
    """Distribuição da cobertura de custo observada nas avaliações de risco
    REAIS (não a aproximação do shadow, que é só observacional).

    `avaliacoes_reais`: mesma correção de `funnel` -- MESMA consulta de
    `RiskEvaluation` que `funnel` já faz; reaproveitada quando fornecida
    pelo chamador consolidado, nunca reconsultada duas vezes na mesma
    resposta."""
    dt_since, dt_until = _parse_since(since), _parse_until(until)
    if avaliacoes_reais is not None:
        avaliacoes = avaliacoes_reais
    else:
        sinais_ids = select(StrategySignal.id).where(
            StrategySignal.created_at >= dt_since, StrategySignal.created_at <= dt_until,
            StrategySignal.direction != "HOLD")
        avaliacoes = session.execute(
            select(RiskEvaluation).where(RiskEvaluation.signal_id.in_(sinais_ids))
        ).scalars().all()

    coberturas = []
    for a in avaliacoes:
        cg = (json.loads(a.checks_json) or {}).get("cost_gate") or {}
        cov = cg.get("achieved_coverage_ratio")
        if cov is not None:
            coberturas.append(cov)
    coberturas.sort()
    n = len(coberturas)
    limiar = 3.0
    if n == 0:
        return {"n": 0, "janela": {"desde": dt_since.isoformat(), "ate": dt_until.isoformat()},
               "limiar_operacional": limiar}

    def pct_(p):
        k = (n - 1) * p / 100.0
        f, cidx = math.floor(k), math.ceil(k)
        if f == cidx:
            return coberturas[int(k)]
        return coberturas[f] + (coberturas[cidx] - coberturas[f]) * (k - f)

    faixas = [("abaixo_1_0x", 0, 1.0), ("1_0_a_1_5x", 1.0, 1.5), ("1_5_a_2_0x", 1.5, 2.0),
              ("2_0_a_3_0x", 2.0, 3.0), ("acima_3_0x", 3.0, math.inf)]
    distrib = {nome: sum(1 for x in coberturas if lo <= x < hi) for nome, lo, hi in faixas}

    acima_limiar_sem_ordem = sum(
        1 for a in avaliacoes
        if ((json.loads(a.checks_json) or {}).get("cost_gate") or {}).get("achieved_coverage_ratio", 0) >= limiar
        and not a.approved
    )

    return {
        "janela": {"desde": dt_since.isoformat(), "ate": dt_until.isoformat()},
        "n": n,
        "minimo": coberturas[0], "maximo": coberturas[-1],
        "media": statistics.mean(coberturas), "mediana": statistics.median(coberturas),
        "p10": pct_(10), "p25": pct_(25), "p50": pct_(50),
        "p75": pct_(75), "p90": pct_(90), "p95": pct_(95),
        "limiar_operacional": limiar,
        "distribuicao_por_faixa": distrib,
        "acima_do_limiar_sem_ordem": acima_limiar_sem_ordem,
        # Correção da auditoria: NÃO afirmar que o gate está "bem calibrado"
        # nem que oportunidades recusadas "seriam boas". O único fato
        # comprovado é que nenhuma cobertura observada alcançou o limiar.
        "nota": "Comprovado: nenhuma das oportunidades desta janela alcançou "
                "o limiar de 3,0x (máximo observado fica registrado acima). "
                "NÃO comprovado: que 3,0x esteja bem calibrado, que as "
                "oportunidades recusadas seriam lucrativas, ou que reduzir "
                "o limiar melhoraria o resultado -- nada disso foi testado "
                "nesta fase e nenhuma alteração de gate foi feita.",
    }


def _experiment_ids_for(session: Session, model: str | None) -> list[int]:
    """IDs de experimento ATIVO -- de um modelo específico, ou de todos."""
    stmt = select(ShadowExperiment.id).where(ShadowExperiment.status == "ATIVO")
    if model:
        stmt = stmt.where(ShadowExperiment.model == model)
    return [r[0] for r in session.execute(stmt).all()]


# =========================================================================
# CORREÇÃO DE DESEMPENHO (auditoria -- "reduzir as 35 queries"): as seções
# abaixo -- experiments_identity/comparison/significance/progress/
# trade_quality/rejection_reasons -- respondiam TODAS, de forma
# independente, a uma única pergunta repetida ("qual é o experimento ATIVO
# de cada modelo, agora, e quais trades ele tem nesta janela?"). Cada uma
# fazia sua própria consulta. `dashboard()` agora resolve isso UMA vez e
# passa o resultado adiante -- as funções continuam funcionando sozinhas
# (rotas individuais, testes existentes) exatamente como antes quando
# chamadas sem esses parâmetros; só o caminho consolidado deixa de
# reconsultar o que já tem em mãos.
# =========================================================================
def _active_experiments_map(session: Session) -> dict[str, "ShadowExperiment | None"]:
    """UMA única consulta: todo experimento ATIVO, agrupado por modelo
    (nunca mais que um por modelo -- o banco recusa dois experimentos
    ATIVO do mesmo modelo simultaneamente)."""
    linhas = session.execute(
        select(ShadowExperiment).where(ShadowExperiment.status == "ATIVO")
    ).scalars().all()
    resultado: dict[str, "ShadowExperiment | None"] = {m: None for m in MODELS}
    for exp in linhas:
        resultado[exp.model] = exp
    return resultado


def _ids_from_map(active_map: dict, model: str | None) -> list[int]:
    if model:
        exp = active_map.get(model)
        return [exp.id] if exp else []
    return [exp.id for exp in active_map.values() if exp is not None]


def _trades_by_model(session: Session, active_map: dict, since: str | None,
                     until: str | None) -> dict[str, list]:
    """UMA consulta POR MODELO (2 no total, nunca mais): todos os trades do
    experimento ATIVO daquele modelo, na janela dada, em ordem ascendente
    de id -- exatamente o que `comparison`/`significance`/`progress`/
    `trade_quality` (quando `model=None`) precisam, hoje reconsultado de
    forma independente por cada uma delas."""
    dt_until = _parse_until(until)
    resultado: dict[str, list] = {}
    for modelo in MODELS:
        exp = active_map.get(modelo)
        if exp is None:
            resultado[modelo] = []
            continue
        stmt = select(ShadowTrade).where(ShadowTrade.experiment_id == exp.id)
        if since:
            stmt = stmt.where(ShadowTrade.opened_candle_time >= _parse_dt(since))
        stmt = stmt.where(ShadowTrade.opened_candle_time <= dt_until)
        resultado[modelo] = session.execute(stmt.order_by(ShadowTrade.id)).scalars().all()
    return resultado


def rejection_reasons(session: Session, model: str | None = None, symbol: str | None = None,
                      direction: str | None = None, since: str | None = None,
                      until: str | None = None, active_map: dict | None = None) -> dict:
    """Ranking de motivos de rejeição das oportunidades shadow, com filtros
    opcionais. Escopado ao(s) EXPERIMENTO(S) ATIVO(S) -- nunca mistura com
    uma configuração de experimento já encerrada (rotação A->B->A2).

    `active_map`: reaproveita o mapa {modelo: experimento ATIVO} já
    carregado pelo chamador consolidado, evitando reconsultar."""
    dt_since, dt_until = _parse_since(since, default_days=365), _parse_until(until)
    ids_ativos = _ids_from_map(active_map, model) if active_map is not None else _experiment_ids_for(session, model)
    if not ids_ativos:
        return {"janela": {"desde": dt_since.isoformat(), "ate": dt_until.isoformat()},
               "filtros": {"model": model, "symbol": symbol, "direction": direction},
               "total": 0, "ranking": []}

    stmt = select(ShadowOpportunity).where(
        ShadowOpportunity.experiment_id.in_(ids_ativos),
        ShadowOpportunity.source_candle_open_time >= dt_since,
        ShadowOpportunity.source_candle_open_time <= dt_until,
    )
    if symbol:
        stmt = stmt.where(ShadowOpportunity.symbol == symbol)
    if direction:
        stmt = stmt.where(ShadowOpportunity.direction == direction)
    linhas = session.execute(stmt).scalars().all()

    def categoria(o: ShadowOpportunity) -> str:
        if o.approved:
            return "aprovado"
        r = o.reason or ""
        if "separação normalizada" in r:
            return "separacao_h2_insuficiente"
        if "exposição hipotética" in r:
            return "exposicao_esgotada"
        if "cooldown" in r:
            return "cooldown"
        if "notional disponível" in r:
            return "notional_minimo"
        if "sinal oposto" in r:
            return "sinal_oposto"
        if "mesmo tick" in r:
            return "saida_mesmo_tick"
        if "perda diária" in r:
            return "limite_perda_diaria"
        if "já existe posição" in r:
            return "posicao_ja_aberta"
        return "outro"

    total = len(linhas)
    contagem: Counter[str] = Counter(categoria(o) for o in linhas)
    return {
        "janela": {"desde": dt_since.isoformat(), "ate": dt_until.isoformat()},
        "filtros": {"model": model, "symbol": symbol, "direction": direction},
        "total": total,
        "ranking": [
            {"categoria": cat, "quantidade": n, "percentual": (n / total * 100) if total else 0}
            for cat, n in contagem.most_common()
        ],
    }


# =========================================================================
# MFE/MAE -- EM LOTE, nunca uma consulta por trade
# =========================================================================
def _load_candles_batch(session: Session, ranges: dict[str, tuple[datetime, datetime]]
                        ) -> dict[str, list[tuple[datetime, float, float]]]:
    """UMA consulta por SÍMBOLO (não por trade): busca todos os candles de
    1 min no intervalo [min(opened) .. max(closed)] daquele símbolo entre
    os trades pedidos, e devolve já ordenados por open_time. Quem chama
    filtra em memória o sub-intervalo exato de cada trade -- O(candles do
    símbolo) uma vez, não O(trades × candles)."""
    resultado: dict[str, list[tuple[datetime, float, float]]] = {}
    for symbol, (lo, hi) in ranges.items():
        rows = session.execute(
            select(Candle.open_time, Candle.high, Candle.low).where(
                Candle.symbol == symbol, Candle.timeframe == "1m",
                Candle.open_time > lo, Candle.open_time <= hi,
            ).order_by(Candle.open_time)
        ).all()
        resultado[symbol] = [(r[0], r[1], r[2]) for r in rows]
    return resultado


def _mfe_mae_from_preloaded(candles: list[tuple[datetime, float, float]], side: str,
                            entry_price: float, opened: datetime, closed: datetime
                            ) -> tuple[float | None, float | None]:
    """Calcula MFE/MAE filtrando candles JÁ CARREGADOS em memória (nenhuma
    consulta aqui). Janela: `open_time > opened AND open_time <= closed`
    -- o mesmo critério de `Orchestrator`/`ShadowEngine.on_operational_candle`
    real (o candle que abriu a posição nunca decide o próprio fechamento;
    isso seria look-ahead invertido). Complexidade O(candles no intervalo),
    busca linear pois a lista já vem ordenada e o intervalo é contíguo."""
    janela = [(h, l) for (t, h, l) in candles if opened < t <= closed]
    if not janela:
        return None, None
    if side == "BUY":
        mfe = max((h - entry_price) / entry_price for h, _ in janela)
        mae = min((l - entry_price) / entry_price for _, l in janela)
    else:
        mfe = max((entry_price - l) / entry_price for _, l in janela)
        mae = min((entry_price - h) / entry_price for h, _ in janela)
    return mfe * 100, mae * 100


def trade_quality(session: Session, model: str | None = None, limit: int = 100,
                  since: str | None = None, until: str | None = None,
                  active_map: dict | None = None, trades_by_model: dict | None = None) -> dict:
    """Qualidade das entradas: MFE/MAE, duração, motivo, separação,
    resultado -- por trade shadow fechado do experimento ATIVO.

    MFE/MAE em LOTE: uma consulta de candles por símbolo distinto entre os
    trades retornados (no máximo 3 nesta base), nunca uma por trade --
    corrige o N+1 identificado na auditoria.

    `trades_by_model` (só quando `model` não é dado): reaproveita os
    trades já carregados pelo chamador consolidado para AMBOS os modelos
    -- ordena por id decrescente e corta em `limit` em memória, produzindo
    exatamente o mesmo resultado que a consulta SQL equivalente faria,
    sem reconsultar."""
    limit = max(1, min(limit, 500))
    if trades_by_model is not None and model is None:
        combinados = []
        for modelo in MODELS:
            combinados.extend(trades_by_model.get(modelo, []))
        combinados.sort(key=lambda t: t.id, reverse=True)
        trades = combinados[:limit]
    else:
        ids_ativos = _ids_from_map(active_map, model) if active_map is not None else _experiment_ids_for(session, model)
        if not ids_ativos:
            return {"trades": [], "count": 0, "limit": limit}

        stmt = select(ShadowTrade).where(ShadowTrade.experiment_id.in_(ids_ativos))
        if since:
            stmt = stmt.where(ShadowTrade.opened_candle_time >= _parse_dt(since))
        if until:
            stmt = stmt.where(ShadowTrade.opened_candle_time <= _parse_dt(until))
        trades = session.execute(stmt.order_by(ShadowTrade.id.desc()).limit(limit)).scalars().all()

    ranges: dict[str, tuple[datetime, datetime]] = {}
    for t in trades:
        lo, hi = ranges.get(t.symbol, (t.opened_candle_time, t.closed_candle_time))
        ranges[t.symbol] = (min(lo, t.opened_candle_time), max(hi, t.closed_candle_time))
    candles_por_simbolo = _load_candles_batch(session, ranges)

    linhas = []
    for t in trades:
        mfe, mae = _mfe_mae_from_preloaded(
            candles_por_simbolo.get(t.symbol, []), t.side, t.entry_fill_price,
            t.opened_candle_time, t.closed_candle_time)
        linhas.append({
            "model": t.model, "symbol": t.symbol, "side": t.side,
            "opened_candle_time": t.opened_candle_time.isoformat(),
            "closed_candle_time": t.closed_candle_time.isoformat(),
            "duration_minutes": t.duration_minutes, "exit_reason": t.exit_reason,
            "normalized_separation": t.normalized_separation,
            "net_pnl_usd": t.net_pnl_usd, "gross_pnl_usd": t.gross_pnl_usd,
            "mfe_pct": mfe, "mae_pct": mae,
        })
    return {
        "trades": linhas, "count": len(linhas), "limit": limit,
        "queries_de_candles": len(ranges),
        "nota_mfe_mae": "Candles de 1 min, intervalo (aberta, fechada] -- o "
                        "candle que abriu a posição nunca participa do "
                        "próprio MFE/MAE (sem look-ahead). Posições ainda "
                        "OPEN não aparecem aqui; ver `comparison` para o "
                        "MFE/MAE corrente da posição aberta, calculado até "
                        "o instante da consulta.",
    }


def progress(session: Session, since: str | None = None, until: str | None = None,
            trades_by_model: dict | None = None) -> dict:
    """Trades fechados do EXPERIMENTO ATIVO de cada modelo, na janela dada.

    `trades_by_model`: reaproveita os trades já carregados pelo chamador
    consolidado -- só CONTA (`len`), não reconsulta. Chamada isolada (rota
    individual) continua usando a consulta `COUNT` enxuta, inalterada."""
    resultado = {}
    for modelo in MODELS:
        if trades_by_model is not None:
            n = len(trades_by_model.get(modelo, []))
        else:
            ids_ativos = _experiment_ids_for(session, modelo)
            stmt = select(func.count()).select_from(ShadowTrade).where(
                ShadowTrade.experiment_id.in_(ids_ativos)) if ids_ativos else None
            if since and stmt is not None:
                stmt = stmt.where(ShadowTrade.opened_candle_time >= _parse_dt(since))
            if until and stmt is not None:
                stmt = stmt.where(ShadowTrade.opened_candle_time <= _parse_dt(until))
            n = session.execute(stmt).scalar_one() if stmt is not None else 0
        marcos = [{"alvo": alvo, "descricao": desc, "atingido": n >= alvo,
                   "faltam": max(0, alvo - n)} for alvo, desc in MILESTONES]
        resultado[modelo] = {"trades_fechados": n, "marcos": marcos}
    return resultado


def recent_events(session: Session, limit: int = 30) -> dict:
    """Feed cronológico combinando oportunidades e trades recentes -- uma
    única consulta pequena e ordenada por id decrescente em cada tabela,
    mesclada em memória (ambas já limitadas)."""
    limit = max(1, min(limit, 200))
    oport = session.execute(
        select(ShadowOpportunity).order_by(ShadowOpportunity.id.desc()).limit(limit)
    ).scalars().all()
    trades = session.execute(
        select(ShadowTrade).order_by(ShadowTrade.id.desc()).limit(limit)
    ).scalars().all()

    eventos = []
    for o in oport:
        eventos.append({
            "tipo": "oportunidade", "quando": o.source_candle_open_time.isoformat(),
            "symbol": o.symbol, "direction": o.direction, "model": o.model,
            "decisao": "aprovado" if o.approved else "rejeitado", "motivo": o.reason,
            "normalized_separation": o.normalized_separation,
            "cost_coverage_ratio": o.cost_coverage_ratio, "pnl_usd": None,
        })
    for t in trades:
        eventos.append({
            "tipo": "fechamento", "quando": t.closed_candle_time.isoformat(),
            "symbol": t.symbol, "direction": t.side, "model": t.model,
            "decisao": t.exit_reason, "motivo": None,
            "normalized_separation": t.normalized_separation,
            "cost_coverage_ratio": None, "pnl_usd": t.net_pnl_usd,
        })
    eventos.sort(key=lambda e: e["quando"], reverse=True)
    return {"events": eventos[:limit], "count": min(len(eventos), limit)}


def comparison(session: Session, since: str | None = None, until: str | None = None,
              active_map: dict | None = None, trades_by_model: dict | None = None) -> dict:
    """Baseline vs H2 lado a lado, escopado ao EXPERIMENTO ATIVO de cada
    modelo (nunca lifetime-por-model, que misturaria configurações de
    experimentos já encerrados se algum dia houver rotação A->B->A2).

    `active_map`/`trades_by_model`: reaproveitam o experimento ATIVO e os
    trades já carregados pelo chamador consolidado -- nunca reconsultados
    aqui quando fornecidos. Chamada isolada (rota individual) continua
    consultando por conta própria, comportamento inalterado."""
    resultado = {}
    dt_until = _parse_until(until)
    mapa = active_map if active_map is not None else None
    for modelo in MODELS:
        if trades_by_model is not None:
            trades = trades_by_model.get(modelo, [])
        else:
            ids_ativos = _ids_from_map(mapa, modelo) if mapa is not None else _experiment_ids_for(session, modelo)
            trades = []
            if ids_ativos:
                stmt = select(ShadowTrade).where(ShadowTrade.experiment_id.in_(ids_ativos))
                if since:
                    stmt = stmt.where(ShadowTrade.opened_candle_time >= _parse_dt(since))
                stmt = stmt.where(ShadowTrade.opened_candle_time <= dt_until)
                trades = session.execute(stmt.order_by(ShadowTrade.id)).scalars().all()

        base = _agg(trades)
        # Correção da auditoria: bruto positivo com líquido negativo NUNCA
        # pode virar "verde geral" -- o rótulo é derivado explicitamente
        # aqui, não deixado para o front decidir cor por conta própria.
        if base["closed_trades"] == 0:
            base["classificacao_resultado"] = "sem_dados"
        elif base["net_pnl_usd"] > 0:
            base["classificacao_resultado"] = "liquido_positivo"
        elif base["gross_pnl_usd"] > 0 >= base["net_pnl_usd"]:
            base["classificacao_resultado"] = "bruto_positivo_liquido_negativo"
        else:
            base["classificacao_resultado"] = "liquido_negativo"

        seq = seq_max = 0
        for t in trades:
            if t.net_pnl_usd <= 0:
                seq += 1
                seq_max = max(seq_max, seq)
            else:
                seq = 0
        base["max_losing_streak"] = seq_max
        base["avg_duration_minutes"] = (
            statistics.mean(t.duration_minutes for t in trades) if trades else None
        )

        aberta = session.execute(
            select(ShadowPosition).where(ShadowPosition.model == modelo, ShadowPosition.status == "OPEN")
        ).scalars().first()
        if aberta is None:
            base["current_position"] = None
        else:
            candles = _load_candles_batch(
                session, {aberta.symbol: (aberta.opened_candle_time, dt_until)}
            ).get(aberta.symbol, [])
            mfe, mae = _mfe_mae_from_preloaded(
                candles, aberta.side, aberta.entry_fill_price, aberta.opened_candle_time, dt_until)
            base["current_position"] = {
                "symbol": aberta.symbol, "side": aberta.side,
                "entry_fill_price": aberta.entry_fill_price,
                "stop_loss": aberta.stop_loss, "take_profit": aberta.take_profit,
                "opened_candle_time": aberta.opened_candle_time.isoformat(),
                "mfe_pct_ate_agora": mfe, "mae_pct_ate_agora": mae,
                "nota": "MFE/MAE correntes até o instante da consulta -- a "
                        "posição continua aberta, isto NÃO é o resultado final.",
            }
        exp = mapa.get(modelo) if mapa is not None else active_experiment(session, modelo)
        base["experiment_uid"] = exp.experiment_uid if exp else None
        base["experiment_id"] = exp.id if exp else None
        resultado[modelo] = base
    return resultado


def significance(session: Session, since: str | None = None, until: str | None = None,
                 active_map: dict | None = None, trades_by_model: dict | None = None) -> dict:
    """Amostra, IC e limitações -- nunca chama poucos trades de conclusão.
    Escopado ao experimento ATIVO de cada modelo.

    `active_map`/`trades_by_model`: mesma reutilização de `comparison` --
    quando fornecidos pelo chamador consolidado, os trades já carregados
    são apenas contados/agregados aqui, nunca reconsultados."""
    resultado = {}
    dt_until = _parse_until(until)
    for modelo in MODELS:
        if trades_by_model is not None:
            trades = trades_by_model.get(modelo, [])
        else:
            ids_ativos = _ids_from_map(active_map, modelo) if active_map is not None else _experiment_ids_for(session, modelo)
            trades = []
            if ids_ativos:
                stmt = select(ShadowTrade).where(ShadowTrade.experiment_id.in_(ids_ativos))
                if since:
                    stmt = stmt.where(ShadowTrade.opened_candle_time >= _parse_dt(since))
                stmt = stmt.where(ShadowTrade.opened_candle_time <= dt_until)
                trades = session.execute(stmt).scalars().all()

        n = len(trades)
        wins = sum(1 for t in trades if t.net_pnl_usd > 0)
        por_simbolo = Counter(t.symbol for t in trades)
        concentracao = (max(por_simbolo.values()) / n) if n else None
        if n:
            p = wins / n
            z = 1.96
            denom = 1 + z**2 / n
            centro = p + z**2 / (2 * n)
            margem = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
            ic = [(centro - margem) / denom, (centro + margem) / denom]
        else:
            ic = [None, None]
        resultado[modelo] = {
            "n_trades": n, "win_rate": (wins / n) if n else None,
            "ic95_win_rate": ic, "concentracao_maxima_por_simbolo": concentracao,
            "amostra_suficiente_para_conclusao_formal": n >= 60,
            "conclusao": "INCONCLUSIVA",
        }
    return resultado


def experiments_identity(session: Session, active_map: dict | None = None) -> dict:
    """Identidade dos dois experimentos ATIVOS -- para o topo do painel
    nunca deixar ambíguo QUAL configuração gerou os números exibidos.

    `active_map`: reaproveita o mapa já carregado pelo chamador
    consolidado -- zero consulta adicional quando fornecido."""
    mapa = active_map if active_map is not None else _active_experiments_map(session)
    resultado = {}
    for modelo in MODELS:
        exp = mapa.get(modelo)
        resultado[modelo] = None if exp is None else {
            "experiment_id": exp.id, "experiment_uid": exp.experiment_uid,
            "config_fingerprint": exp.config_fingerprint, "status": exp.status,
            "started_at": exp.started_at.isoformat(), "threshold": exp.threshold,
        }
    return resultado


def economic_verdict(comparison_data: dict, cost_gate_data: dict, significance_data: dict) -> dict:
    """Texto executivo -- gerado no BACKEND, uma vez, para o topo do
    painel nunca divergir do resto. Nunca usa "favorável"/"edge positivo"
    para amostras abaixo do marco formal (60 trades)."""
    h2 = comparison_data.get(MODEL_H2, {})
    baseline = comparison_data.get(MODEL_BASELINE, {})
    h2_sig = significance_data.get(MODEL_H2, {})

    zero_ordens = {
        "comprovado": [
            "o gate de custo de 3,0x é a causa direta de zero aprovações "
            "operacionais na janela mostrada",
            f"cobertura máxima observada: {cost_gate_data.get('maximo', 'N/D')}",
            "nenhuma das oportunidades desta janela alcançou o limiar exigido",
        ],
        "nao_comprovado": [
            "que 3,0x esteja economicamente bem calibrado",
            "que as oportunidades recusadas seriam lucrativas",
            "que reduzir o limiar melhoraria o resultado",
        ],
        "classificacao": "Gate tecnicamente funcional, integralmente "
                         "restritivo no regime observado, com validade "
                         "preditiva/econômica ainda não demonstrada.",
    }

    n_h2 = h2.get("closed_trades", 0)
    h2_texto = (
        f"{n_h2} trades encerrados. Amostra insuficiente. "
        f"Resultado líquido {'positivo' if h2.get('net_pnl_usd', 0) > 0 else 'negativo'}. "
        "Nenhuma vantagem comprovada."
    )
    if h2.get("classificacao_resultado") == "bruto_positivo_liquido_negativo":
        h2_texto = (
            f"O H2 apresentou P&L bruto observado positivo de "
            f"US$ {h2.get('gross_pnl_usd', 0):.4f} em {n_h2} trades, mas P&L "
            f"líquido negativo de US$ {abs(h2.get('net_pnl_usd', 0)):.4f}. A amostra "
            "não demonstra vantagem estatística nem viabilidade econômica."
        )

    return {
        "zero_ordens": zero_ordens,
        "h2_texto_obrigatorio": h2_texto,
        "h2_amostra_suficiente": h2_sig.get("amostra_suficiente_para_conclusao_formal", False),
        "conclusao_geral": "INCONCLUSIVA",
    }


# =========================================================================
# ENDPOINT CONSOLIDADO -- uma única sessão/transação, um único generated_at
# =========================================================================
def dashboard(session: Session, scope: str = "experiment", since: str | None = None,
             until: str | None = None) -> dict:
    """Tudo que o painel principal precisa, numa única chamada e num único
    corte temporal consistente. `since`/`until` (ISO) só têm efeito com
    scope='custom' -- os demais escopos resolvem a janela sozinhos.

    Correção de desempenho (auditoria): `experiments_identity`,
    `comparison`, `significance`, `progress`, `trade_quality` e
    `rejection_reasons` respondiam, cada uma de forma independente, à
    MESMA pergunta ("qual experimento está ATIVO e quais trades ele tem
    nesta janela?") -- e `funnel`/`cost_gate_distribution` buscavam as
    MESMAS avaliações de risco reais. Aqui essas respostas são resolvidas
    UMA vez (`_active_experiments_map`, `_trades_by_model`,
    `avaliacoes_reais`) e passadas adiante -- nenhuma seção reconsulta o
    que outra já carregou nesta mesma resposta.
    """
    generated_at = datetime.now(timezone.utc)
    dt_since, dt_until, rotulo = resolve_window(session, scope, since, until)
    since_iso, until_iso = dt_since.isoformat(), dt_until.isoformat()

    active_map = _active_experiments_map(session)
    trades_map = _trades_by_model(session, active_map, since_iso, until_iso)

    sinais_ids_reais = select(StrategySignal.id).where(
        StrategySignal.created_at >= dt_since, StrategySignal.created_at <= dt_until,
        StrategySignal.direction != "HOLD")
    avaliacoes_reais = session.execute(
        select(RiskEvaluation).where(RiskEvaluation.signal_id.in_(sinais_ids_reais))
    ).scalars().all()

    comp = comparison(session, since=since_iso, until=until_iso,
                      active_map=active_map, trades_by_model=trades_map)
    sig = significance(session, since=since_iso, until=until_iso,
                       active_map=active_map, trades_by_model=trades_map)
    cg = cost_gate_distribution(session, since=since_iso, until=until_iso,
                                avaliacoes_reais=avaliacoes_reais)

    return {
        "generated_at": generated_at.isoformat(),
        "scope": scope,
        "window": {"desde": since_iso, "ate": until_iso, "rotulo": rotulo},
        "experiments": experiments_identity(session, active_map=active_map),
        "funnel": funnel(session, since=since_iso, until=until_iso,
                         avaliacoes_reais=avaliacoes_reais),
        "cost_gate": cg,
        "rejections": rejection_reasons(session, since=since_iso, until=until_iso,
                                        active_map=active_map),
        "comparison": comp,
        "trade_quality": trade_quality(session, limit=100, since=since_iso, until=until_iso,
                                       active_map=active_map, trades_by_model=trades_map),
        "progress": progress(session, since=since_iso, until=until_iso, trades_by_model=trades_map),
        "significance": sig,
        "events": recent_events(session, limit=40),
        "economic_verdict": economic_verdict(comp, cg, sig),
    }
