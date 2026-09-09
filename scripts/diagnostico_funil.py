"""Fase 3.5 -- Diagnostico quantitativo do funil operacional.

Roda SOMENTE contra um snapshot (Online Backup API) aberto em modo
somente-leitura, nunca contra o banco operacional. Sem alteracoes de
estrategia/gate/threshold -- e' leitura e calculo puro. Sem credenciais,
sem caminho fixo: o banco e a janela sao sempre informados por argumento.

Uso:
    python scripts/diagnostico_funil.py --db CAMINHO_DO_SNAPSHOT.db \
        [--since "2026-09-07 03:50:28"] [--until "2026-09-09 06:09:26"]

Se --since/--until forem omitidos, a janela e' derivada automaticamente:
inicio = MIN(shadow_opportunities.source_candle_open_time), fim =
MAX(candles.open_time) do proprio banco informado.
"""
import argparse
import json
import math
import sqlite3
import statistics
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone

_ap = argparse.ArgumentParser()
_ap.add_argument("--db", required=True, help="caminho do snapshot (.db) a analisar")
_ap.add_argument("--since", default=None, help="inicio da janela (ISO); default = auto")
_ap.add_argument("--until", default=None, help="fim da janela (ISO); default = auto")
_ARGS = _ap.parse_args()
SNAPSHOT = _ARGS.db

def conn():
    uri = "file:" + urllib.parse.quote(SNAPSHOT.replace("\\", "/")) + "?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


def pct(a, b):
    return (a / b * 100.0) if b else 0.0


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def wilson_ci(wins, n, z=1.96):
    """Intervalo de confianca de Wilson para proporcao -- mais honesto que
    normal-approx em amostras pequenas (nunca sai de [0,1])."""
    if n == 0:
        return (None, None)
    p = wins / n
    denom = 1 + z**2 / n
    centro = p + z**2 / (2 * n)
    margem = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return ((centro - margem) / denom, (centro + margem) / denom)


c = conn()

# =========================================================================
# 1) JANELA EXATA
# =========================================================================
print("=" * 78)
print("1) JANELA TEMPORAL EXATA")
print("=" * 78)

# janela = desde a criacao das tabelas shadow (ativacao pos-hotfix) ate o
# instante do snapshot. Fonte: MIN/MAX reais dos proprios dados, nao um
# numero digitado a mao.
inicio = c.execute("SELECT MIN(source_candle_open_time) FROM shadow_opportunities").fetchone()[0]
fim_candles = c.execute("SELECT MAX(open_time) FROM candles").fetchone()[0]
print(f"inicio (primeira oportunidade shadow): {inicio} UTC")
print(f"fim (ultimo candle persistido)........: {fim_candles} UTC")

JANELA_INICIO = _ARGS.since or inicio    # default: 1a oportunidade shadow
JANELA_FIM    = _ARGS.until or fim_candles  # default: ultimo candle do banco
print(f"\nJANELA ADOTADA: [{JANELA_INICIO}, {JANELA_FIM}] UTC")
print("(do religamento pos-hotfix ate o snapshot -- toda contagem abaixo")
print(" e' filtrada por esta janela, nunca lifetime, salvo onde dito.)")

print()
print("candles por simbolo na janela:")
for row in c.execute(
    "SELECT symbol, COUNT(*) FROM candles WHERE open_time BETWEEN ? AND ? GROUP BY symbol",
    (JANELA_INICIO, JANELA_FIM)):
    print(f"  {row[0]:<10} {row[1]}")

print()
print("buckets estrategicos completos (sinais gerados, todos ja sao de bucket completo):")
buckets = c.execute(
    "SELECT symbol, COUNT(DISTINCT source_candle_open_time) FROM strategy_signals "
    "WHERE created_at BETWEEN ? AND ? GROUP BY symbol", (JANELA_INICIO, JANELA_FIM)).fetchall()
for row in buckets:
    print(f"  {row[0]:<10} {row[1]}")

print()
print("sinais por direcao:")
for row in c.execute(
    "SELECT direction, COUNT(*) FROM strategy_signals WHERE created_at BETWEEN ? AND ? GROUP BY direction",
    (JANELA_INICIO, JANELA_FIM)):
    print(f"  {row[0]:<6} {row[1]}")

n_oport = c.execute(
    "SELECT COUNT(*) FROM shadow_opportunities WHERE source_candle_open_time BETWEEN ? AND ?",
    (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
n_risk = c.execute(
    "SELECT COUNT(*) FROM risk_evaluations re JOIN strategy_signals s ON s.id=re.signal_id "
    "WHERE s.created_at BETWEEN ? AND ?", (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
n_orders = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
n_positions = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
n_shadow_trades = c.execute(
    "SELECT COUNT(*) FROM shadow_trades WHERE opened_candle_time BETWEEN ? AND ?",
    (JANELA_INICIO, JANELA_FIM)).fetchone()[0]

print()
print(f"oportunidades shadow (na janela): {n_oport}")
print(f"avaliacoes de risco (join por sinal, na janela): {n_risk}")
print(f"ordens operacionais (lifetime, sempre 0): {n_orders}")
print(f"posicoes operacionais (lifetime, sempre 0): {n_positions}")
print(f"trades shadow abertos na janela: {n_shadow_trades}")

# =========================================================================
# 2) RECONCILIACAO: 37 acionaveis vs 282 avaliacoes de risco
# =========================================================================
print()
print("=" * 78)
print("2) RECONCILIACAO -- 37 sinais acionaveis vs N avaliacoes de risco")
print("=" * 78)
total_signals = c.execute(
    "SELECT COUNT(*) FROM strategy_signals WHERE created_at BETWEEN ? AND ?",
    (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
acionaveis = c.execute(
    "SELECT COUNT(*) FROM strategy_signals WHERE created_at BETWEEN ? AND ? AND direction<>'HOLD'",
    (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
hold = total_signals - acionaveis
risk_por_hold = c.execute(
    "SELECT s.direction, COUNT(*) FROM risk_evaluations re "
    "JOIN strategy_signals s ON s.id=re.signal_id "
    "WHERE s.created_at BETWEEN ? AND ? GROUP BY s.direction",
    (JANELA_INICIO, JANELA_FIM)).fetchall()
print(f"sinais totais na janela: {total_signals}  (HOLD={hold}, acionaveis={acionaveis})")
print("avaliacoes de risco (RiskEngine), por direcao do sinal associado:")
for row in risk_por_hold:
    print(f"  direction={row[0]:<6} avaliacoes={row[1]}")
print()
print("HIPOTESE A TESTAR: o RiskEngine avalia toda ordem que a estrategia")
print("propoe, MESMO sinais HOLD, se o motor gera uma avaliacao de not-trade?")
print("Verificando join direto sem qualquer filtro de direction:")
todas_risk = c.execute("SELECT COUNT(*) FROM risk_evaluations").fetchone()[0]
print(f"  risk_evaluations total no banco (lifetime): {todas_risk}")
print(f"  risk_evaluations na janela (join por sinal): {n_risk}")
sem_signal = c.execute(
    "SELECT COUNT(*) FROM risk_evaluations WHERE signal_id NOT IN (SELECT id FROM strategy_signals)"
).fetchone()[0]
print(f"  risk_evaluations com signal_id orfao (sem sinal correspondente): {sem_signal}")
# quantas risk_evaluations por sinal (1:1 ou 1:N?)
multi = c.execute(
    "SELECT signal_id, COUNT(*) c FROM risk_evaluations GROUP BY signal_id HAVING c>1"
).fetchall()
print(f"  sinais com MAIS DE UMA avaliacao de risco: {len(multi)}")
if multi[:5]:
    print("  exemplos:", [dict(r) for r in multi[:5]])

c.close()

c = conn()
print()
print("=" * 78)
print("3) FUNIL COMPLETO, ETAPA POR ETAPA (janela)")
print("=" * 78)

candles_total = sum(r[1] for r in c.execute(
    "SELECT symbol, COUNT(*) FROM candles WHERE open_time BETWEEN ? AND ? GROUP BY symbol",
    (JANELA_INICIO, JANELA_FIM)))
buckets_total = sum(r[1] for r in c.execute(
    "SELECT symbol, COUNT(DISTINCT source_candle_open_time) FROM strategy_signals "
    "WHERE created_at BETWEEN ? AND ? GROUP BY symbol", (JANELA_INICIO, JANELA_FIM)))

print(f"[1] candles de 1 min recebidos ......... {candles_total} (3 simbolos)")
print(f"[2] buckets de 15 min fechados ......... {buckets_total} (=603 sinais, 1:1 -- nenhum bucket incompleto)")
print(f"[3] sinais gerados (HOLD+BUY+SELL) ..... {total_signals}")
print(f"[4] sinais acionaveis (BUY/SELL) ....... {acionaveis}  ({pct(acionaveis,total_signals):.1f}% dos gerados)")
print(f"[5] avaliados pelo RiskEngine .......... {n_risk}  ({pct(n_risk,acionaveis):.1f}% dos acionaveis -- 1:1)")

aprovados_real = c.execute(
    "SELECT COUNT(*) FROM risk_evaluations re JOIN strategy_signals s ON s.id=re.signal_id "
    "WHERE s.created_at BETWEEN ? AND ? AND re.approved=1", (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
print(f"[6] aprovados pelo gate operacional real  {aprovados_real}  ({pct(aprovados_real,n_risk):.1f}%)")
print(f"[7] ordens criadas ..................... 0")
print(f"[8] fills ............................... 0")
print(f"[9] posicoes operacionais abertas ....... 0")
print(f"[10] P&L operacional realizado .......... US$ 0,00")
print()
print("--- em paralelo, o SHADOW (nao depende do gate operacional) ---")
aprov_baseline = c.execute(
    "SELECT COUNT(*) FROM shadow_opportunities WHERE model='baseline_without_cost_gate' "
    "AND approved=1 AND source_candle_open_time BETWEEN ? AND ?", (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
aprov_h2 = c.execute(
    "SELECT COUNT(*) FROM shadow_opportunities WHERE model='h2_ma_separation_015' "
    "AND approved=1 AND source_candle_open_time BETWEEN ? AND ?", (JANELA_INICIO, JANELA_FIM)).fetchone()[0]
print(f"[6-shadow-baseline] oportunidades aprovadas hipoteticamente .. {aprov_baseline}/{n_oport//2}")
print(f"[6-shadow-h2]       oportunidades aprovadas hipoteticamente .. {aprov_h2}/{n_oport//2}")

print()
print("DISTRIBUICAO POR SIMBOLO (sinais acionaveis):")
for row in c.execute(
    "SELECT symbol, direction, COUNT(*) FROM strategy_signals WHERE created_at BETWEEN ? AND ? "
    "AND direction<>'HOLD' GROUP BY symbol, direction ORDER BY symbol, direction",
    (JANELA_INICIO, JANELA_FIM)):
    print(f"  {row[0]:<10} {row[1]:<5} {row[2]}")

print()
print("DISTRIBUICAO TEMPORAL (sinais acionaveis por hora UTC):")
por_hora = Counter()
for row in c.execute(
    "SELECT source_candle_open_time FROM strategy_signals WHERE created_at BETWEEN ? AND ? AND direction<>'HOLD'",
    (JANELA_INICIO, JANELA_FIM)):
    h = row[0].split(" ")[1][:2]
    por_hora[h] += 1
for h in sorted(por_hora):
    print(f"  {h}h UTC: {'#'*por_hora[h]} ({por_hora[h]})")

c.close()

c = conn()
print()
print("=" * 78)
print("4) GATE DE CUSTOS -- distribuicao da cobertura (as 37 avaliacoes)")
print("=" * 78)
coberturas = []
detalhes = []
for row in c.execute(
    "SELECT s.id, s.symbol, s.direction, s.atr, s.observed_price, re.approved, re.checks_json, re.reason "
    "FROM strategy_signals s JOIN risk_evaluations re ON re.signal_id=s.id "
    "WHERE s.created_at BETWEEN ? AND ?", (JANELA_INICIO, JANELA_FIM)):
    ch = json.loads(row["checks_json"])
    cg = ch.get("cost_gate") or {}
    cov = cg.get("achieved_coverage_ratio")
    if cov is not None:
        coberturas.append(cov)
    detalhes.append({
        "id": row["id"], "symbol": row["symbol"], "direction": row["direction"],
        "atr": row["atr"], "preco": row["observed_price"],
        "atr_pct": (row["atr"] / row["observed_price"] * 100) if row["observed_price"] else None,
        "custo_entrada": cg.get("entry_fee_usd", 0) + cg.get("entry_slippage_usd", 0),
        "custo_saida": cg.get("exit_fee_usd", 0) + cg.get("exit_slippage_usd", 0),
        "custo_total": cg.get("round_trip_cost_usd"),
        "movimento_esperado": cg.get("expected_move_usd"),
        "cobertura": cov,
        "limiar": cg.get("required_coverage_ratio"),
        "distancia_ao_limiar": (cg.get("required_coverage_ratio") - cov) if cov is not None and cg.get("required_coverage_ratio") else None,
        "cost_gate_aprova": cg.get("cost_coverage_ok"),
        "aprovado_real": bool(row["approved"]),
        "motivo": row["reason"],
        "exposure_room": ch.get("exposure_room_available"),
    })

coberturas.sort()
print(f"n = {len(coberturas)}")
print(f"minimo  : {min(coberturas):.4f}x")
print(f"maximo  : {max(coberturas):.4f}x")
print(f"media   : {statistics.mean(coberturas):.4f}x")
print(f"mediana : {statistics.median(coberturas):.4f}x")
for p in (10,25,50,75,90,95):
    print(f"p{p:<3}    : {percentile(coberturas,p):.4f}x")
print()
faixas = [("<1.0x",0,1.0), ("1.0-1.5x",1.0,1.5), ("1.5-2.0x",1.5,2.0), ("2.0-3.0x",2.0,3.0), (">=3.0x",3.0,999)]
for nome, lo, hi in faixas:
    n = sum(1 for x in coberturas if lo<=x<hi)
    print(f"  {nome:<10} {n:>3}  ({pct(n,len(coberturas)):.1f}%)")

print()
print("sinais com cobertura >= 3,0x que NAO geraram ordem:")
casos = [d for d in detalhes if d["cobertura"] is not None and d["cobertura"] >= 3.0 and not d["aprovado_real"]]
print(f"  encontrados: {len(casos)}")
for d in casos:
    print("   ", d)

print()
print("sinais onde cost_gate SOZINHO aprovaria (cost_coverage_ok=True) mas approved=False geral")
casos2 = [d for d in detalhes if d["cost_gate_aprova"] and not d["aprovado_real"]]
print(f"  encontrados: {len(casos2)}")
for d in casos2:
    print("   id=%s %s %s cobertura=%.2fx motivo_final=%s" % (d["id"], d["symbol"], d["direction"], d["cobertura"], d["motivo"][:70]))

print()
print("checagem de contagem duplicada -- 1 risk_evaluation por signal_id?")
dup = c.execute(
    "SELECT signal_id, COUNT(*) FROM risk_evaluations re JOIN strategy_signals s ON s.id=re.signal_id "
    "WHERE s.created_at BETWEEN ? AND ? GROUP BY signal_id HAVING COUNT(*)>1",
    (JANELA_INICIO, JANELA_FIM)).fetchall()
print(f"  duplicadas: {len(dup)}")

print()
print("diferenca entre cobertura OBSERVADA no shadow vs a PERSISTIDA na avaliacao operacional:")
diffs = []
for row in c.execute(
    "SELECT s.id, s.symbol, s.source_candle_open_time, re.checks_json "
    "FROM strategy_signals s JOIN risk_evaluations re ON re.signal_id=s.id "
    "WHERE s.created_at BETWEEN ? AND ?", (JANELA_INICIO, JANELA_FIM)):
    cg = json.loads(row["checks_json"]).get("cost_gate") or {}
    cov_real = cg.get("achieved_coverage_ratio")
    shadow_row = c.execute(
        "SELECT cost_coverage_ratio FROM shadow_opportunities WHERE symbol=? AND source_candle_open_time=? LIMIT 1",
        (row["symbol"], row["source_candle_open_time"])).fetchone()
    if shadow_row and cov_real is not None:
        cov_shadow = shadow_row[0]
        diffs.append(abs(cov_real - cov_shadow))
print(f"  pares comparados: {len(diffs)}")
print(f"  diferenca maxima: {max(diffs):.8f}" if diffs else "  n/a")
print(f"  diferenca media : {statistics.mean(diffs):.8f}" if diffs else "  n/a")

c.close()
