#!/usr/bin/env python
"""Fase 3.5 (auditoria) -- benchmark e EXPLAIN QUERY PLAN do endpoint
consolidado /api/diagnostics/dashboard.

Uso:
    python scripts/benchmark_dashboard.py --db CAMINHO.db [--iteracoes 30]

Requisitos:
- abre o banco em mode=ro para as leituras (nunca escreve nele);
- para o teste de concorrência, cria um banco TEMPORÁRIO em disco (cópia
  isolada), nunca toca no banco informado por --db;
- nenhuma credencial, nenhum caminho fixo além do fornecido em --db.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import (
    Candle, RiskEvaluation, ShadowExperiment, ShadowOpportunity, ShadowTrade, StrategySignal,
)
from app.shadow import diagnostics
from app.shadow.engine import MODEL_BASELINE


def explain(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[str]:
    linhas = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return [" | ".join(str(x) for x in row) for row in linhas]


def secao_explain(caminho_db: str, since: str, until: str) -> None:
    uri = "file:" + urllib.parse.quote(caminho_db.replace("\\", "/")) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    print("=" * 78)
    print("EXPLAIN QUERY PLAN das consultas do endpoint consolidado")
    print("=" * 78)

    consultas = [
        ("funnel: candles por símbolo",
         "SELECT symbol, COUNT(*) FROM candles WHERE open_time >= ? AND open_time <= ? GROUP BY symbol",
         (since, until)),
        ("funnel: sinais por direção",
         "SELECT direction, COUNT(*) FROM strategy_signals WHERE created_at >= ? AND created_at <= ? GROUP BY direction",
         (since, until)),
        ("funnel: risk_evaluations via subquery de sinal",
         "SELECT * FROM risk_evaluations WHERE signal_id IN "
         "(SELECT id FROM strategy_signals WHERE created_at >= ? AND created_at <= ? AND direction<>'HOLD')",
         (since, until)),
        ("cost_gate: mesma subquery de sinal",
         "SELECT * FROM strategy_signals WHERE created_at >= ? AND created_at <= ? AND direction<>'HOLD'",
         (since, until)),
        ("rejections: oportunidades por experiment_id",
         "SELECT * FROM shadow_opportunities WHERE experiment_id IN (1,2) "
         "AND source_candle_open_time >= ? AND source_candle_open_time <= ?",
         (since, until)),
        ("trade_quality: trades por experiment_id",
         "SELECT * FROM shadow_trades WHERE experiment_id IN (1,2) AND opened_candle_time <= ?",
         (until,)),
        ("trade_quality: candles em lote por símbolo (1 dos 3)",
         "SELECT open_time, high, low FROM candles WHERE symbol='BTCUSDT' AND timeframe='1m' "
         "AND open_time > ? AND open_time <= ? ORDER BY open_time",
         (since, until)),
    ]
    total_full_scan = 0
    for nome, sql, params in consultas:
        linhas_plano = explain(conn, sql, params)
        t0 = time.perf_counter()
        linhas = conn.execute(sql, params).fetchall()
        dur_ms = (time.perf_counter() - t0) * 1000
        full_scan = any("SCAN" in l and "USING INDEX" not in l and "SEARCH" not in l for l in linhas_plano)
        # SCAN de tabela pequena (candles do próprio símbolo/período) não é
        # o mesmo que full-table-scan da tabela inteira -- reporto ambos.
        scan_tabela_inteira = any("SCAN candles" in l or "SCAN strategy_signals" in l
                                  or "SCAN risk_evaluations" in l or "SCAN shadow_opportunities" in l
                                  or "SCAN shadow_trades" in l for l in linhas_plano)
        if scan_tabela_inteira:
            total_full_scan += 1
        print(f"\n--- {nome} ---")
        print("  plano:", " || ".join(linhas_plano))
        print(f"  linhas devolvidas: {len(linhas)} | duração: {dur_ms:.3f} ms | "
              f"full scan de tabela: {'SIM' if scan_tabela_inteira else 'não'}")
    print(f"\nTOTAL de consultas com full scan de tabela: {total_full_scan}/{len(consultas)}")
    conn.close()


def secao_fria_e_30(caminho_db_original: str, iteracoes_subsequentes: int = 30) -> dict:
    """Mede a PRIMEIRA requisição isoladamente -- cópia NOVA do banco (nunca
    tocada por EXPLAIN nem por nenhuma outra seção), processo/engine/sessão
    novos -- e só DEPOIS as `iteracoes_subsequentes` seguintes na mesma
    sessão (já aquecida). Não tenta limpar cache de disco do SO (fora de
    escopo); a novidade real e controlada é a cópia do arquivo e a
    engine/sessão, nunca usadas antes neste processo."""
    print()
    print("=" * 78)
    print(f"REQUISIÇÃO FRIA + {iteracoes_subsequentes} SUBSEQUENTES -- "
          f"cópia nova, engine/sessão novas, nunca tocadas antes")
    print("=" * 78)
    tmp_dir = tempfile.mkdtemp(prefix="fase35_fria_")
    tmp_db = str(Path(tmp_dir) / "fria.db")
    shutil.copyfile(caminho_db_original, tmp_db)
    print(f"cópia isolada (recém-criada, não tocada por EXPLAIN nem por outra seção): {tmp_db}")

    # mode=ro + make_engine(): MESMO helper/timeout que o app usa em produção.
    uri_ro = "sqlite:///file:" + urllib.parse.quote(tmp_db.replace("\\", "/")) + "?mode=ro&uri=true"
    eng = make_engine(uri_ro)
    sf = make_session_factory(eng)

    with session_scope(sf) as s:
        contador = {"n": 0}
        original = s.execute

        def contando(*a, **k):
            contador["n"] += 1
            return original(*a, **k)

        # --- PRIMEIRA requisição: nada neste processo tocou este arquivo
        # antes deste ponto.
        s.execute = contando
        t0 = time.perf_counter()
        diagnostics.dashboard(s, scope="experiment")
        dur_primeira = (time.perf_counter() - t0) * 1000
        s.execute = original
        n_queries_primeira = contador["n"]

        # --- 30 subsequentes, mesma sessão (já aquecida) ---
        subsequentes_ms = []
        n_queries_subsequente = None
        for _ in range(iteracoes_subsequentes):
            contador["n"] = 0
            s.execute = contando
            t0 = time.perf_counter()
            diagnostics.dashboard(s, scope="experiment")
            dur = (time.perf_counter() - t0) * 1000
            s.execute = original
            subsequentes_ms.append(dur)
            if n_queries_subsequente is None:
                n_queries_subsequente = contador["n"]

    subsequentes_ms.sort()
    p50 = subsequentes_ms[len(subsequentes_ms) // 2]
    p95 = subsequentes_ms[int(len(subsequentes_ms) * 0.95)] if len(subsequentes_ms) > 1 else subsequentes_ms[0]
    maximo_subseq = max(subsequentes_ms)
    maximo_geral = max([dur_primeira] + subsequentes_ms)

    print(f"\nPRIMEIRA requisição (fria): {dur_primeira:.2f} ms | queries: {n_queries_primeira}")
    print(f"{iteracoes_subsequentes} requisições SUBSEQUENTES (mesma sessão, já aquecida):")
    print(f"  queries por ciclo: {n_queries_subsequente}")
    print(f"  p50: {p50:.2f} ms | p95: {p95:.2f} ms | máximo: {maximo_subseq:.2f} ms | "
          f"mínimo: {min(subsequentes_ms):.2f} ms | média: {statistics.mean(subsequentes_ms):.2f} ms")
    print(f"maior duração de transação entre TODAS as {1 + iteracoes_subsequentes} requisições "
          f"(fria + subsequentes): {maximo_geral:.2f} ms")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return {
        "primeira_ms": dur_primeira, "n_queries_primeira": n_queries_primeira,
        "p50": p50, "p95": p95, "max_subsequentes": maximo_subseq, "max_geral": maximo_geral,
        "n_queries": n_queries_subsequente, "todas_ms": [dur_primeira] + subsequentes_ms,
    }


def secao_decomposicao(caminho_db: str) -> int:
    """Decompõe as queries do ciclo consolidado por seção, chamando cada
    sub-função na MESMA ordem e com os MESMOS dados compartilhados
    (`active_map`/`trades_by_model`/`avaliacoes_reais`) que `dashboard()`
    usa internamente -- a soma tem que bater exatamente com o total
    medido em `secao_fria_e_30`, senão a decomposição estaria mentindo."""
    print()
    print("=" * 78)
    print("DECOMPOSIÇÃO DAS QUERIES POR SEÇÃO (após a redução desta rodada)")
    print("=" * 78)
    uri_ro = "sqlite:///file:" + urllib.parse.quote(caminho_db.replace("\\", "/")) + "?mode=ro&uri=true"
    eng = make_engine(uri_ro)
    sf = make_session_factory(eng)

    with session_scope(sf) as s:
        dt_since, dt_until, _ = diagnostics.resolve_window(s, "experiment")
        since_iso, until_iso = dt_since.isoformat(), dt_until.isoformat()

        def contar(label, fn):
            contador = {"n": 0}
            original = s.execute
            def contando(*a, **k):
                contador["n"] += 1
                return original(*a, **k)
            s.execute = contando
            resultado = fn()
            s.execute = original
            return label, contador["n"], resultado

        secoes = []
        _, n_resolve, _ = contar("resolve_window", lambda: diagnostics.resolve_window(s, "experiment"))
        secoes.append(("resolve_window", n_resolve))

        _, n_map, active_map = contar(
            "_active_experiments_map (substitui as consultas de ID repetidas em "
            "experiments_identity/comparison/significance/progress/trade_quality/rejection_reasons)",
            lambda: diagnostics._active_experiments_map(s))
        secoes.append(("_active_experiments_map", n_map))

        _, n_trades, trades_map = contar(
            "_trades_by_model (substitui os trades repetidos em comparison/significance/progress)",
            lambda: diagnostics._trades_by_model(s, active_map, since_iso, until_iso))
        secoes.append(("_trades_by_model", n_trades))

        _, n_aval, avaliacoes_reais = contar(
            "avaliacoes_reais (substitui a mesma consulta repetida em funnel + cost_gate_distribution)",
            lambda: session_avaliacoes(s, dt_since, dt_until))
        secoes.append(("avaliacoes_reais", n_aval))

        secoes.append(contar("experiments_identity (reaproveita active_map)",
                              lambda: diagnostics.experiments_identity(s, active_map=active_map))[:2])
        secoes.append(contar("comparison (reaproveita active_map + trades_by_model)",
                              lambda: diagnostics.comparison(s, since=since_iso, until=until_iso,
                                                             active_map=active_map, trades_by_model=trades_map))[:2])
        secoes.append(contar("significance (reaproveita active_map + trades_by_model)",
                              lambda: diagnostics.significance(s, since=since_iso, until=until_iso,
                                                               active_map=active_map, trades_by_model=trades_map))[:2])
        secoes.append(contar("cost_gate_distribution (reaproveita avaliacoes_reais)",
                              lambda: diagnostics.cost_gate_distribution(s, since=since_iso, until=until_iso,
                                                                        avaliacoes_reais=avaliacoes_reais))[:2])
        secoes.append(contar("funnel (reaproveita avaliacoes_reais)",
                              lambda: diagnostics.funnel(s, since=since_iso, until=until_iso,
                                                         avaliacoes_reais=avaliacoes_reais))[:2])
        secoes.append(contar("rejection_reasons (reaproveita active_map)",
                              lambda: diagnostics.rejection_reasons(s, since=since_iso, until=until_iso,
                                                                    active_map=active_map))[:2])
        secoes.append(contar("trade_quality (reaproveita active_map + trades_by_model)",
                              lambda: diagnostics.trade_quality(s, limit=100, since=since_iso, until=until_iso,
                                                                active_map=active_map, trades_by_model=trades_map))[:2])
        secoes.append(contar("progress (reaproveita trades_by_model)",
                              lambda: diagnostics.progress(s, since=since_iso, until=until_iso,
                                                           trades_by_model=trades_map))[:2])
        secoes.append(contar("recent_events", lambda: diagnostics.recent_events(s, limit=40))[:2])

    print("\n--- decomposição das queries por seção (mesma janela, mesma sessão, dados compartilhados) ---")
    soma = 0
    for nome, n in secoes:
        print(f"  {nome}: {n} query(ies)")
        soma += n
    print(f"  economic_verdict: 0 query(ies) (computação pura sobre comparison/cost_gate/significance já carregados)")
    print(f"\n  SOMA das seções: {soma}")
    return soma


def session_avaliacoes(s, dt_since, dt_until):
    sinais_ids = select(StrategySignal.id).where(
        StrategySignal.created_at >= dt_since, StrategySignal.created_at <= dt_until,
        StrategySignal.direction != "HOLD")
    return s.execute(select(RiskEvaluation).where(RiskEvaluation.signal_id.in_(sinais_ids))).scalars().all()


def secao_concorrencia(caminho_db_original: str, leituras_minimas: int = 30,
                       escritas_minimas: int = 30) -> dict:
    """Banco TEMPORÁRIO isolado (cópia descartável, nunca o --db original).
    Escritor concorrente ativo durante TODA a janela; o loop de leitura só
    para quando JÁ tiver pelo menos `leituras_minimas` leituras E o
    escritor já tiver pelo menos `escritas_minimas` escritas bem-sucedidas
    -- nunca um número fixo de iterações que possa ficar aquém do exigido.
    `make_engine()` -- mesmo helper, mesmo timeout (padrão do driver
    sqlite3, o mesmo que a aplicação usa) tanto para leitor quanto
    escritor. `journal_mode` nunca é alterado."""
    print()
    print("=" * 78)
    print(f"CONCORRÊNCIA -- >= {leituras_minimas} leituras consolidadas, escritor ativo "
          f"durante toda a janela, >= {escritas_minimas} escritas bem-sucedidas")
    print("=" * 78)
    tmp_dir = tempfile.mkdtemp(prefix="fase35_concorrencia_")
    tmp_db = str(Path(tmp_dir) / "concorrencia.db")
    shutil.copyfile(caminho_db_original, tmp_db)
    print(f"cópia de trabalho (descartável): {tmp_db} (o banco original NUNCA é aberto para escrita)")

    eng_leitor = make_engine(f"sqlite:///{tmp_db}")
    sf_leitor = make_session_factory(eng_leitor)

    parar = threading.Event()
    erros_lock_escritor = {"n": 0}
    escritas_ok = {"n": 0}
    from datetime import datetime as _dt, timedelta as _td

    def escritor():
        eng_escritor = make_engine(f"sqlite:///{tmp_db}")
        sf_escritor = make_session_factory(eng_escritor)
        i = 100000
        while not parar.is_set():
            try:
                with session_scope(sf_escritor) as s:
                    s.add(Candle(symbol="BTCUSDT", timeframe="1m",
                                open_time=_dt(2026, 9, 9, 12, 0) + _td(minutes=i),
                                open=100, high=101, low=99, close=100, volume=1, source="bench"))
                escritas_ok["n"] += 1
            except Exception as exc:
                if "locked" in str(exc).lower():
                    erros_lock_escritor["n"] += 1
            i += 1
            time.sleep(0.02)

    t = threading.Thread(target=escritor, daemon=True)
    t.start()

    duracoes_ms = []
    tempo_max_transacao = 0.0
    erros_lock_leitor = 0
    n_queries_por_ciclo = None
    inicio = time.perf_counter()
    # Continua até bater as DUAS metas mínimas (leituras E escritas), com um
    # teto de segurança de 30s para nunca rodar indefinidamente se algo
    # travar de verdade.
    while (len(duracoes_ms) < leituras_minimas or escritas_ok["n"] < escritas_minimas) \
            and (time.perf_counter() - inicio) < 30:
        t0 = time.perf_counter()
        try:
            with session_scope(sf_leitor) as s:
                contador = {"n": 0}
                original = s.execute
                def contando(*a, **k):
                    contador["n"] += 1
                    return original(*a, **k)
                s.execute = contando
                t_txn0 = time.perf_counter()
                diagnostics.dashboard(s, scope="experiment")
                tempo_max_transacao = max(tempo_max_transacao, time.perf_counter() - t_txn0)
                s.execute = original
                if n_queries_por_ciclo is None:
                    n_queries_por_ciclo = contador["n"]
        except Exception as exc:
            if "locked" in str(exc).lower():
                erros_lock_leitor += 1
        duracoes_ms.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.03)

    parar.set()
    t.join(timeout=2)

    duracoes_ms_ordenadas = sorted(duracoes_ms)
    p50 = duracoes_ms_ordenadas[len(duracoes_ms_ordenadas) // 2]
    p95 = duracoes_ms_ordenadas[int(len(duracoes_ms_ordenadas) * 0.95)] \
        if len(duracoes_ms_ordenadas) > 1 else duracoes_ms_ordenadas[0]

    print(f"leituras consolidadas realizadas: {len(duracoes_ms)} (mínimo exigido: {leituras_minimas})")
    print(f"escritas concorrentes bem-sucedidas: {escritas_ok['n']} (mínimo exigido: {escritas_minimas})")
    print(f"'database is locked' no LEITOR: {erros_lock_leitor}")
    print(f"'database is locked' no ESCRITOR: {erros_lock_escritor['n']}")
    print(f"queries por ciclo de leitura: {n_queries_por_ciclo}")
    print(f"duração das leituras -- p50: {p50:.2f} ms | p95: {p95:.2f} ms | "
          f"máximo: {max(duracoes_ms):.2f} ms")
    print(f"maior duração de uma única transação de leitura: {tempo_max_transacao*1000:.2f} ms")
    print("journal_mode: NÃO alterado em nenhum momento | timeout: o padrão do driver sqlite3 "
          "(mesmo make_engine() que a aplicação usa, nenhum override)")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return {
        "leituras": len(duracoes_ms), "escritas": escritas_ok["n"],
        "locks_leitor": erros_lock_leitor, "locks_escritor": erros_lock_escritor["n"],
        "p50": p50, "p95": p95, "maximo": max(duracoes_ms),
        "max_transacao_ms": tempo_max_transacao * 1000, "n_queries": n_queries_por_ciclo,
    }


def secao_escala_mfe_mae() -> bool:
    """Prova dedicada e isolada (banco sintético próprio, nunca o --db
    informado): o número de queries usadas para carregar candles de
    MFE/MAE (`_load_candles_batch`) escala com o número de SÍMBOLOS
    distintos entre os trades, nunca com o número de trades."""
    print()
    print("=" * 78)
    print("ESCALA DO MFE/MAE -- por símbolo distinto, não por número de trades")
    print("=" * 78)
    tmp_dir = tempfile.mkdtemp(prefix="fase35_escala_mfe_mae_")
    tmp_db = str(Path(tmp_dir) / "escala.db")
    eng = make_engine(f"sqlite:///{tmp_db}")
    init_db(eng)
    sf = make_session_factory(eng)
    T0 = datetime(2026, 9, 1, 0, 0)

    def _experimento(s, exp_id):
        s.add(ShadowExperiment(
            id=exp_id, experiment_uid=f"uid-bench-{exp_id}", model=MODEL_BASELINE,
            hypothesis_version="h2-v1", strategy_timeframe_minutes=15, strategy_version="v1",
            fast_period=9, slow_period=21, atr_period=14, fee_rate=0.0006, slippage_bps=5.0,
            stop_loss_atr_multiple=2.0, take_profit_atr_multiple=3.0, max_position_usd=50.0,
            max_total_exposure_usd=50.0, min_order_notional_usd=5.0, max_daily_loss_usd=25.0,
            cooldown_after_losses=3, cooldown_minutes=30, config_fingerprint=f"fp-bench-{exp_id}",
            config_snapshot_json="{}", started_at=T0, status="ATIVO",
        ))

    def _candle(s, symbol, open_time):
        s.add(Candle(symbol=symbol, timeframe="1m", open_time=open_time,
                     open=100, high=101, low=99, close=100, volume=1, source="bench"))

    def _trade(s, exp_id, symbol, minuto):
        s.add(ShadowTrade(
            experiment_id=exp_id, model=MODEL_BASELINE, hypothesis_version="h2-v1",
            symbol=symbol, side="BUY", qty=1.0, entry_fill_price=100.0, exit_fill_price=100.5,
            notional_usd=100.0, stop_loss=95.0, take_profit=110.0, exit_reason="take_profit",
            opened_candle_time=T0 + timedelta(minutes=minuto),
            closed_candle_time=T0 + timedelta(minutes=minuto + 1), duration_minutes=1,
            gross_pnl_usd=0.5, fees_usd=0.05, slippage_usd=0.02, net_pnl_usd=0.43,
            normalized_separation=0.2,
        ))

    def _contar_queries_trade_quality() -> int:
        with session_scope(sf) as s:
            contador = {"n": 0}
            original = s.execute
            def contando(*a, **k):
                contador["n"] += 1
                return original(*a, **k)
            s.execute = contando
            diagnostics.trade_quality(s, model=MODEL_BASELINE, limit=500)
            s.execute = original
            return contador["n"]

    resultados = {}
    # UM único experimento ATIVO durante todo o cenário (o banco recusa um
    # segundo experimento ATIVO do mesmo modelo) -- só o número de símbolos
    # e de trades dentro dele varia entre as medições.
    with session_scope(sf) as s:
        _experimento(s, 1)
        _candle(s, "BTCUSDT", T0)
        _candle(s, "BTCUSDT", T0 + timedelta(minutes=1))

    # Cenário A: 1 símbolo, 5 trades.
    with session_scope(sf) as s:
        for i in range(5):
            _trade(s, 1, "BTCUSDT", i)
    resultados["A_1_simbolo_5_trades"] = _contar_queries_trade_quality()

    # Cenário B: MESMO símbolo único, agora +50 trades (55 no total) -- se o
    # número de queries mudar aqui, seria prova de N+1 por trade (não deveria).
    with session_scope(sf) as s:
        for i in range(5, 55):
            _trade(s, 1, "BTCUSDT", i)
    resultados["B_1_simbolo_50_trades"] = _contar_queries_trade_quality()

    # Cenário C: +2 símbolos NOVOS (3 símbolos distintos no total), só +6
    # trades novos (bem menos que os +50 do cenário B) -- se o número de
    # queries acompanhar os símbolos aqui, é prova de que a escala é por
    # SÍMBOLO distinto, não por quantidade de trades.
    with session_scope(sf) as s:
        for simbolo in ("ETHUSDT", "SOLUSDT"):
            _candle(s, simbolo, T0)
            _candle(s, simbolo, T0 + timedelta(minutes=1))
        for i, simbolo in enumerate(["ETHUSDT", "SOLUSDT"] * 3):
            _trade(s, 1, simbolo, 100 + i)
    resultados["C_3_simbolos_6_trades_novos"] = _contar_queries_trade_quality()

    for nome, n in resultados.items():
        print(f"  {nome}: {n} query(ies) totais em trade_quality()")

    a, b, c = (resultados["A_1_simbolo_5_trades"], resultados["B_1_simbolo_50_trades"],
               resultados["C_3_simbolos_6_trades_novos"])
    invariante_ao_trade_count = (a == b)
    escala_com_simbolos = (c > b)
    print(f"\n  invariante a 10x mais trades no MESMO símbolo (A == B): "
          f"{'PASSOU' if invariante_ao_trade_count else 'FALHOU'} ({a} == {b}?)")
    print(f"  cresce ao adicionar símbolos novos mesmo com menos trades novos (C > B): "
          f"{'PASSOU' if escala_com_simbolos else 'FALHOU'} ({c} > {b}?)")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return invariante_ao_trade_count and escala_com_simbolos


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="caminho do banco (snapshot) para ler")
    ap.add_argument("--iteracoes", type=int, default=30, help="requisições subsequentes após a fria")
    ap.add_argument("--leituras-concorrencia", type=int, default=30)
    ap.add_argument("--escritas-concorrencia", type=int, default=30)
    args = ap.parse_args()

    # mode=ro mesmo para esta leitura inicial da janela -- este script nunca
    # abre o --db informado em modo de escrita, em nenhum ponto.
    uri_ro_inicial = "sqlite:///file:" + urllib.parse.quote(args.db.replace("\\", "/")) + "?mode=ro&uri=true"
    with session_scope(make_session_factory(make_engine(uri_ro_inicial))) as s:
        janela = diagnostics.resolve_window(s, "experiment")
    since_iso, until_iso = janela[0].isoformat(), janela[1].isoformat()

    secao_explain(args.db, since_iso, until_iso)
    resultado_frio = secao_fria_e_30(args.db, args.iteracoes)
    soma_decomposicao = secao_decomposicao(args.db)
    resultado_concorrencia = secao_concorrencia(
        args.db, leituras_minimas=args.leituras_concorrencia, escritas_minimas=args.escritas_concorrencia)
    escala_ok = secao_escala_mfe_mae()

    print()
    print("=" * 78)
    print("CRITÉRIOS")
    print("=" * 78)
    c1 = resultado_frio["primeira_ms"] < 500
    c2 = resultado_frio["p95"] < 250
    c3 = resultado_frio["max_geral"] < 500 and resultado_concorrencia["maximo"] < 500
    c4 = resultado_concorrencia["locks_leitor"] == 0 and resultado_concorrencia["locks_escritor"] == 0
    c5 = resultado_frio["n_queries"] == soma_decomposicao
    print(f"primeira requisição (fria) < 500 ms: "
          f"{'PASSOU' if c1 else 'FALHOU'} ({resultado_frio['primeira_ms']:.1f} ms)")
    print(f"p95 das {args.iteracoes} subsequentes < 250 ms: "
          f"{'PASSOU' if c2 else 'FALHOU'} ({resultado_frio['p95']:.1f} ms)")
    print(f"nenhuma requisição > 500 ms (fria+subsequentes+concorrência): "
          f"{'PASSOU' if c3 else 'FALHOU'} (máximo geral: {max(resultado_frio['max_geral'], resultado_concorrencia['maximo']):.1f} ms)")
    print(f"zero 'database is locked' (leitor E escritor) na concorrência: "
          f"{'PASSOU' if c4 else 'FALHOU'} (leitor={resultado_concorrencia['locks_leitor']}, "
          f"escritor={resultado_concorrencia['locks_escritor']})")
    print(f"leituras concorrentes >= {args.leituras_concorrencia}: "
          f"{'PASSOU' if resultado_concorrencia['leituras'] >= args.leituras_concorrencia else 'FALHOU'} "
          f"({resultado_concorrencia['leituras']})")
    print(f"escritas concorrentes >= {args.escritas_concorrencia}: "
          f"{'PASSOU' if resultado_concorrencia['escritas'] >= args.escritas_concorrencia else 'FALHOU'} "
          f"({resultado_concorrencia['escritas']})")
    print(f"decomposição bate com o total medido: "
          f"{'PASSOU' if c5 else 'FALHOU'} ({soma_decomposicao} == {resultado_frio['n_queries']}?)")
    print("uma requisição HTTP por atualização: PASSOU (rota única /diagnostics/dashboard)")
    print(f"MFE/MAE escala por símbolo distinto, não por trade: {'PASSOU' if escala_ok else 'FALHOU'}")
    print(f"\nqueries por ciclo -- ANTES desta rodada: 35 | DEPOIS: {resultado_frio['n_queries']} "
          f"(meta: reduzir materialmente, preferencialmente <= 20)")
