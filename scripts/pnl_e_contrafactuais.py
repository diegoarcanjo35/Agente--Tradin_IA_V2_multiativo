"""Fase 3.5 -- decomposicao do P&L, significancia, contrafactuais de
diagnostico (nunca aplicados operacionalmente). Roda contra um snapshot
aberto em modo somente-leitura -- nunca o banco operacional, nunca um
caminho fixo, sem credenciais.

Uso:
    python scripts/pnl_e_contrafactuais.py --db CAMINHO_DO_SNAPSHOT.db
"""
import argparse
import math
import sqlite3
import statistics
import urllib.parse
from collections import Counter

_ap = argparse.ArgumentParser()
_ap.add_argument("--db", required=True, help="caminho do snapshot (.db) a analisar")
_ARGS = _ap.parse_args()

_uri = "file:" + urllib.parse.quote(_ARGS.db.replace("\\", "/")) + "?mode=ro"
c = sqlite3.connect(_uri, uri=True)
c.row_factory = sqlite3.Row


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (None, None)
    p = wins / n
    denom = 1 + z**2 / n
    centro = p + z**2 / (2 * n)
    margem = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return ((centro - margem) / denom, (centro + margem) / denom)


for modelo in ("baseline_without_cost_gate", "h2_ma_separation_015"):
    rows = c.execute("SELECT * FROM shadow_trades WHERE model=? ORDER BY id", (modelo,)).fetchall()
    n = len(rows)
    print("=" * 78)
    print(f"{modelo}  (n={n})")
    print("=" * 78)

    gross = sum(r["gross_pnl_usd"] for r in rows)
    fees = sum(r["fees_usd"] for r in rows)
    slip = sum(r["slippage_usd"] for r in rows)
    net = sum(r["net_pnl_usd"] for r in rows)
    print(f"P&L bruto de preco : US$ {gross:+.4f}")
    print(f"taxas              : US$ {-fees:+.4f}")
    print(f"slippage           : US$ {-slip:+.4f}")
    print(f"P&L liquido        : US$ {net:+.4f}   (confere: {gross-fees-slip:+.4f})")

    vencedores = [r["net_pnl_usd"] for r in rows if r["net_pnl_usd"] > 0]
    perdedores = [r["net_pnl_usd"] for r in rows if r["net_pnl_usd"] <= 0]
    ganho_medio = statistics.mean(vencedores) if vencedores else 0.0
    perda_media = statistics.mean(perdedores) if perdedores else 0.0
    payoff = abs(ganho_medio / perda_media) if perda_media else None
    profit_factor = (sum(vencedores) / abs(sum(perdedores))) if perdedores and sum(perdedores) != 0 else None
    taxa_acerto = len(vencedores) / n if n else 0
    expectativa = net / n if n else 0

    print(f"vencedores={len(vencedores)} perdedores={len(perdedores)} taxa_acerto={taxa_acerto*100:.1f}%")
    print(f"ganho medio=US${ganho_medio:+.4f}  perda media=US${perda_media:+.4f}")
    print(f"payoff (|ganho/perda|)={payoff:.3f}" if payoff else "payoff=n/d")
    print(f"profit_factor={profit_factor:.3f}" if profit_factor else "profit_factor=n/d (sem perdas ou sem ganhos)")
    print(f"expectativa liquida por trade=US${expectativa:+.4f}")

    if payoff:
        # win rate de equilibrio BRUTO (sem custos): 1/(1+payoff_bruto)
        # aqui usamos o payoff LIQUIDO observado como proxy, documentando a limitacao
        be_liquido = 1 / (1 + payoff)
        print(f"taxa de acerto necessaria p/ empate (dado o payoff liquido observado): {be_liquido*100:.1f}%")
    ic = wilson_ci(len(vencedores), n)
    if ic[0] is not None:
        print(f"IC 95% (Wilson) da taxa de acerto: [{ic[0]*100:.1f}%, {ic[1]*100:.1f}%]  <- MUITO largo, amostra pequena")

    # drawdown e sequencia de perdas (equity curve simples, ordem cronologica)
    equity = 0.0
    pico = 0.0
    dd_max = 0.0
    seq = 0
    seq_max = 0
    for r in rows:
        equity += r["net_pnl_usd"]
        pico = max(pico, equity)
        dd_max = max(dd_max, pico - equity)
        if r["net_pnl_usd"] <= 0:
            seq += 1
            seq_max = max(seq_max, seq)
        else:
            seq = 0
    print(f"drawdown maximo (equity curve dos trades): US$ {dd_max:.4f}")
    print(f"sequencia maxima de perdas consecutivas: {seq_max}")
    print(f"tempo medio de posicao: {statistics.mean(r['duration_minutes'] for r in rows):.1f} min")

    # ---------------- CONTRAFACTUAIS DE DIAGNOSTICO ----------------
    print()
    print("CONTRAFACTUAIS (diagnostico apenas -- nao aplicaveis operacionalmente):")
    sem_taxa = sum(r["net_pnl_usd"] + r["fees_usd"] for r in rows)
    sem_slip = sum(r["net_pnl_usd"] + r["slippage_usd"] for r in rows)
    sem_ambos = sum(r["gross_pnl_usd"] for r in rows)
    print(f"  resultado sem taxa..................... US$ {sem_taxa:+.4f}  (delta {sem_taxa-net:+.4f})")
    print(f"  resultado sem slippage.................. US$ {sem_slip:+.4f}  (delta {sem_slip-net:+.4f})")
    print(f"  resultado sem taxa e sem slippage (bruto) US$ {sem_ambos:+.4f}  (delta {sem_ambos-net:+.4f})")
    print(f"  resultado com custos reais (o que ocorreu) US$ {net:+.4f}")

    apenas_stop_alvo = sum(r["net_pnl_usd"] for r in rows if r["exit_reason"] in ("stop_loss", "take_profit"))
    n_stop_alvo = sum(1 for r in rows if r["exit_reason"] in ("stop_loss", "take_profit"))
    print(f"  se so contasse stop/alvo (excluindo sinal oposto): "
          f"US$ {apenas_stop_alvo:+.4f} em {n_stop_alvo} trades (HIPOTETICO -- nao e' o motor real)")
    print(f"  resultado incluindo sinal oposto (o motor ATUAL): US$ {net:+.4f} em {n}")

print()
print("=" * 78)
print("MARCOS DE AMOSTRA (H2)")
print("=" * 78)
n_h2 = c.execute("SELECT COUNT(*) FROM shadow_trades WHERE model='h2_ma_separation_015'").fetchone()[0]
marcos = [(15, "observacao preliminar"), (30, "revisao intermediaria"),
          (60, "primeira analise formal"), (200, "avaliacao de vantagem pequena (min)"),
          (230, "avaliacao de vantagem pequena (max)")]
for alvo, desc in marcos:
    print(f"  {n_h2}/{alvo} trades ({desc}): {'ATINGIDO' if n_h2>=alvo else 'FALTAM '+str(alvo-n_h2)}")
