"""Fase 3.5 -- decomposicao de trades shadow: MFE/MAE, motivo, duracao,
comparacao baseline vs H2, contrafactuais. Roda contra um snapshot aberto
em modo somente-leitura -- nunca o banco operacional, nunca um caminho
fixo, sem credenciais.

Uso:
    python scripts/decomposicao_trades.py --db CAMINHO_DO_SNAPSHOT.db
"""
import argparse
import json
import sqlite3
import statistics
import urllib.parse
from collections import Counter, defaultdict

_ap = argparse.ArgumentParser()
_ap.add_argument("--db", required=True, help="caminho do snapshot (.db) a analisar")
_ARGS = _ap.parse_args()

_uri = "file:" + urllib.parse.quote(_ARGS.db.replace("\\", "/")) + "?mode=ro"
c = sqlite3.connect(_uri, uri=True)
c.row_factory = sqlite3.Row


def mfe_mae(symbol, side, entry_price, opened, closed):
    """Maxima excursao favoravel/adversa em % do preco de entrada, varrendo
    os candles de 1 min entre abertura e fechamento (exclusive/inclusive
    conforme o motor real usa em on_operational_candle)."""
    rows = c.execute(
        "SELECT high, low FROM candles WHERE symbol=? AND open_time > ? AND open_time <= ? "
        "ORDER BY open_time", (symbol, opened, closed)).fetchall()
    if not rows:
        return None, None
    if side == "BUY":
        mfe = max((r["high"] - entry_price) / entry_price for r in rows)
        mae = min((r["low"] - entry_price) / entry_price for r in rows)
    else:
        mfe = max((entry_price - r["low"]) / entry_price for r in rows)
        mae = min((entry_price - r["high"]) / entry_price for r in rows)
    return mfe * 100, mae * 100


print("=" * 78)
print("5/6) DECOMPOSICAO DE TRADES -- baseline vs H2")
print("=" * 78)

trades = {}
for modelo in ("baseline_without_cost_gate", "h2_ma_separation_015"):
    rows = c.execute(
        "SELECT * FROM shadow_trades WHERE model=? ORDER BY id", (modelo,)).fetchall()
    lista = []
    for r in rows:
        mfe, mae = mfe_mae(r["symbol"], r["side"], r["entry_fill_price"],
                            r["opened_candle_time"], r["closed_candle_time"])
        lista.append({
            "symbol": r["symbol"], "side": r["side"],
            "opened": r["opened_candle_time"], "closed": r["closed_candle_time"],
            "duration_min": r["duration_minutes"], "exit_reason": r["exit_reason"],
            "gross": r["gross_pnl_usd"], "fees": r["fees_usd"], "slippage": r["slippage_usd"],
            "net": r["net_pnl_usd"], "sep": r["normalized_separation"],
            "mfe_pct": mfe, "mae_pct": mae,
        })
    trades[modelo] = lista

for modelo, lista in trades.items():
    print(f"\n--- {modelo} ({len(lista)} trades) ---")
    for t in lista:
        print(f"  {t['symbol']:<8} {t['side']:<4} dur={t['duration_min']:>4}min "
              f"saida={t['exit_reason']:<16} net=US${t['net']:+.4f} "
              f"sep={t['sep']:.4f} MFE={t['mfe_pct']:+.3f}% MAE={t['mae_pct']:+.3f}%")

print()
print("=" * 78)
print("PERGUNTAS OBJETIVAS")
print("=" * 78)

for modelo, lista in trades.items():
    print(f"\n--- {modelo} ---")
    stops = [t for t in lista if t["exit_reason"] == "stop_loss"]
    alvos = [t for t in lista if t["exit_reason"] == "take_profit"]
    opostos = [t for t in lista if t["exit_reason"] == "opposite_signal"]
    print(f"  stops: {len(stops)} | alvos: {len(alvos)} | sinal oposto: {len(opostos)}")

    # Os stops acontecem imediatamente ou apos movimento favoravel?
    if stops:
        mfe_antes_stop = [t["mfe_pct"] for t in stops if t["mfe_pct"] is not None]
        imediatos = sum(1 for t in stops if t["mfe_pct"] is not None and t["mfe_pct"] < 0.05)
        print(f"  stops com MFE < 0,05% antes de bater (praticamente imediato): {imediatos}/{len(stops)}")
        print(f"  MFE medio dos stops: {statistics.mean(mfe_antes_stop):.3f}% "
              f"(ou seja, em media o trade andou tanto a favor antes de reverter)")

    # os trades chegam perto do alvo?
    if lista:
        # distancia percorrida (MFE) vs a distancia do alvo (3x ATR aprox, mas
        # usamos take_profit relative a entrada correspondente na posicao)
        mfes = [t["mfe_pct"] for t in lista if t["mfe_pct"] is not None]
        print(f"  MFE medio geral: {statistics.mean(mfes):.3f}% | mediana: {statistics.median(mfes):.3f}%")
        maes = [t["mae_pct"] for t in lista if t["mae_pct"] is not None]
        print(f"  MAE medio geral: {statistics.mean(maes):.3f}% | mediana: {statistics.median(maes):.3f}%")

    # duracao media
    durs = [t["duration_min"] for t in lista]
    print(f"  duracao media: {statistics.mean(durs):.1f} min | mediana: {statistics.median(durs):.1f} min")

    # concentracao por simbolo
    por_simbolo = Counter(t["symbol"] for t in lista)
    pnl_por_simbolo = defaultdict(float)
    for t in lista:
        pnl_por_simbolo[t["symbol"]] += t["net"]
    print(f"  trades por simbolo: {dict(por_simbolo)}")
    print(f"  pnl liquido por simbolo: {dict(pnl_por_simbolo)}")

    # concentracao por direcao
    por_lado = Counter(t["side"] for t in lista)
    pnl_por_lado = defaultdict(float)
    for t in lista:
        pnl_por_lado[t["side"]] += t["net"]
    print(f"  trades por lado: {dict(por_lado)}")
    print(f"  pnl liquido por lado: {dict(pnl_por_lado)}")

    # vencedores -- caracteristica comum?
    vencedores = [t for t in lista if t["net"] > 0]
    perdedores = [t for t in lista if t["net"] <= 0]
    if vencedores:
        print(f"  vencedores ({len(vencedores)}): sep_media={statistics.mean(t['sep'] for t in vencedores):.4f} "
              f"dur_media={statistics.mean(t['duration_min'] for t in vencedores):.1f}min "
              f"motivos={Counter(t['exit_reason'] for t in vencedores)}")
    if perdedores:
        print(f"  perdedores ({len(perdedores)}): sep_media={statistics.mean(t['sep'] for t in perdedores):.4f} "
              f"dur_media={statistics.mean(t['duration_min'] for t in perdedores):.1f}min "
              f"motivos={Counter(t['exit_reason'] for t in perdedores)}")
