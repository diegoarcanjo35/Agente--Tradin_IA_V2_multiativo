#!/usr/bin/env python
"""Fase 3.5 (auditoria) -- simulação contrafactual dos 5 sinais do H2 mais
próximos do limiar 0,15 (informativa apenas -- NUNCA usada para alterar o
threshold).

Para cada sinal, reconstrói o estado REAL do H2 naquele instante
(exposição aberta e cooldown, a partir do histórico verdadeiro de
`shadow_positions`/`shadow_trades`) e, se nenhum outro gate estrutural já
bloquearia, abre a posição hipotética com a MESMA matemática de
`ShadowEngine._open()`/`_register_close()` (adverse_fill_price, stop/alvo
por múltiplo de ATR, taxas e slippage reais) e avança pelos candles REAIS
subsequentes até stop, alvo ou sinal oposto -- exatamente a prioridade do
motor real.

Uso:
    python scripts/simulacao_quase_limiares_h2.py --db CAMINHO.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.risk.cost_model import adverse_fill_price

ALVOS = [0.1498, 0.1491, 0.1446, 0.1444, 0.1397]


def conectar_ro(caminho: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(caminho.replace("\\", "/")) + "?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


def exposicao_h2_em(c, quando: str) -> float:
    """Soma o notional das posições H2 que estavam OPEN naquele instante --
    abertas antes/em `quando` e (ainda abertas, ou fechadas depois)."""
    linhas = c.execute(
        "SELECT p.notional_usd, p.opened_candle_time, t.closed_candle_time "
        "FROM shadow_positions p LEFT JOIN shadow_trades t "
        "ON t.experiment_id=p.experiment_id AND t.symbol=p.symbol "
        "AND t.opened_candle_time=p.opened_candle_time "
        "WHERE p.model='h2_ma_separation_015'"
    ).fetchall()
    total = 0.0
    for r in linhas:
        aberta_em = r["opened_candle_time"]
        fechada_em = r["closed_candle_time"]
        if aberta_em <= quando and (fechada_em is None or fechada_em > quando):
            total += r["notional_usd"]
    return total


def cooldown_ativo_em(c, quando: str, apos_perdas: int, minutos: int) -> tuple[bool, str | None]:
    """Reconstrói o cooldown real: percorre os trades H2 fechados ANTES de
    `quando`, em ordem cronológica, contando perdas consecutivas -- mesma
    lógica de `ShadowEngine._register_close`."""
    trades = c.execute(
        "SELECT closed_candle_time, net_pnl_usd FROM shadow_trades "
        "WHERE model='h2_ma_separation_015' AND closed_candle_time < ? "
        "ORDER BY closed_candle_time", (quando,)
    ).fetchall()
    consecutivas = 0
    cooldown_ate = None
    for t in trades:
        if t["net_pnl_usd"] < 0:
            consecutivas += 1
            if consecutivas >= apos_perdas:
                from datetime import datetime, timedelta
                fechado = datetime.fromisoformat(t["closed_candle_time"])
                cooldown_ate = (fechado + timedelta(minutes=minutos)).isoformat(sep=" ")
                consecutivas = 0
        else:
            consecutivas = 0
    ativo = cooldown_ate is not None and cooldown_ate > quando
    return ativo, cooldown_ate


def simular_uma(c, cfg: dict, oport: sqlite3.Row) -> dict:
    symbol = oport["symbol"]
    direction = oport["direction"]
    preco = oport["reference_price"]
    atr = oport["atr"]
    quando = oport["source_candle_open_time"]

    # 1) gates estruturais, no estado REAL do H2 naquele instante
    exposicao_atual = exposicao_h2_em(c, quando)
    restante = cfg["max_total_exposure_usd"] - exposicao_atual
    cooldown_ativo, cooldown_ate = cooldown_ativo_em(
        c, quando, cfg["cooldown_after_losses"], cfg["cooldown_minutes"])

    if cooldown_ativo:
        return {"symbol": symbol, "direction": direction, "quando": quando,
               "resultado": "recusado_por_outro_gate", "motivo": f"cooldown ativo até {cooldown_ate}"}
    if restante <= 0:
        return {"symbol": symbol, "direction": direction, "quando": quando,
               "resultado": "recusado_por_outro_gate",
               "motivo": f"exposição esgotada (aberto US$ {exposicao_atual:.2f} de US$ {cfg['max_total_exposure_usd']:.2f})"}
    position_usd = min(cfg["max_position_usd"], restante)
    if position_usd < cfg["min_order_notional_usd"] - 1e-4:
        return {"symbol": symbol, "direction": direction, "quando": quando,
               "resultado": "recusado_por_outro_gate",
               "motivo": f"notional disponível US$ {position_usd:.2f} abaixo do mínimo"}

    # 2) abertura -- mesma matemática de ShadowEngine._open()
    slippage_fraction = cfg["slippage_bps"] / 10_000.0
    fill = adverse_fill_price(preco, direction, slippage_fraction)
    qty = position_usd / fill
    fee_entrada = qty * fill * cfg["fee_rate"]
    slip_entrada = abs(fill - preco) * qty
    d_stop = atr * cfg["stop_loss_atr_multiple"]
    d_alvo = atr * cfg["take_profit_atr_multiple"]
    if direction == "BUY":
        stop, alvo = fill - d_stop, fill + d_alvo
    else:
        stop, alvo = fill + d_stop, fill - d_alvo

    # 3) avanca pelos candles REAIS de 1 min subsequentes, MESMA prioridade
    #    do motor: stop/alvo por candle (empate = stop), e sinal oposto do
    #    H2 real (mesmo símbolo) fecha sem reverter no mesmo tick.
    candles = c.execute(
        "SELECT open_time, high, low FROM candles WHERE symbol=? AND timeframe='1m' "
        "AND open_time > ? ORDER BY open_time", (symbol, quando)
    ).fetchall()
    sinais_opostos = c.execute(
        "SELECT source_candle_open_time, direction FROM shadow_opportunities "
        "WHERE model='h2_ma_separation_015' AND symbol=? AND source_candle_open_time > ? "
        "ORDER BY source_candle_open_time", (symbol, quando)
    ).fetchall()
    oposto_em = next((s["source_candle_open_time"] for s in sinais_opostos
                      if s["direction"] != direction), None)

    for cand in candles:
        if direction == "BUY":
            bateu_stop, bateu_alvo = cand["low"] <= stop, cand["high"] >= alvo
        else:
            bateu_stop, bateu_alvo = cand["high"] >= stop, cand["low"] <= alvo
        if bateu_stop or bateu_alvo:
            gatilho = stop if bateu_stop else alvo
            razao = "stop_loss" if bateu_stop else "take_profit"
            return _fechar(cfg, symbol, direction, quando, cand["open_time"], fill, qty,
                          fee_entrada, slip_entrada, gatilho, razao, slippage_fraction)
        if oposto_em is not None and cand["open_time"] >= oposto_em:
            # o sinal oposto fecha ao preço de referência daquele candle
            preco_saida = next((s2["source_candle_open_time"] for s2 in sinais_opostos
                               if s2["source_candle_open_time"] == oposto_em), None)
            ref = c.execute(
                "SELECT reference_price FROM shadow_opportunities WHERE model='h2_ma_separation_015' "
                "AND symbol=? AND source_candle_open_time=?", (symbol, oposto_em)).fetchone()
            return _fechar(cfg, symbol, direction, quando, oposto_em, fill, qty,
                          fee_entrada, slip_entrada, ref["reference_price"], "opposite_signal",
                          slippage_fraction)

    return {"symbol": symbol, "direction": direction, "quando": quando,
           "resultado": "ainda_aberta_no_fim_dos_dados_disponiveis"}


def _fechar(cfg, symbol, direction, aberta_em, fechada_em, entry_fill, qty,
           fee_entrada, slip_entrada, preco_saida_ref, razao, slippage_fraction) -> dict:
    lado_saida = "SELL" if direction == "BUY" else "BUY"
    exit_fill = adverse_fill_price(preco_saida_ref, lado_saida, slippage_fraction)
    if direction == "BUY":
        gross = (exit_fill - entry_fill) * qty
    else:
        gross = (entry_fill - exit_fill) * qty
    fee_saida = qty * exit_fill * cfg["fee_rate"]
    slip_saida = abs(exit_fill - preco_saida_ref) * qty
    net = gross - fee_entrada - slip_entrada - fee_saida - slip_saida
    return {
        "symbol": symbol, "direction": direction, "aberta_em": aberta_em, "fechada_em": fechada_em,
        "resultado": "ganharia" if net > 0 else "perderia",
        "exit_reason": razao, "net_pnl_usd": round(net, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    c = conectar_ro(args.db)

    cfg = dict(c.execute(
        "SELECT fee_rate, slippage_bps, stop_loss_atr_multiple, take_profit_atr_multiple, "
        "max_position_usd, max_total_exposure_usd, min_order_notional_usd, "
        "cooldown_after_losses, cooldown_minutes FROM shadow_experiments "
        "WHERE model='h2_ma_separation_015' AND status='ATIVO'").fetchone())

    print("Configuração usada na simulação (do próprio experimento H2 ativo):")
    print(" ", cfg)
    print()
    print("AVISO: puramente informativo. Threshold do H2 (0,15) NÃO foi alterado.")
    print()

    resultados = []
    for alvo in ALVOS:
        r = c.execute(
            "SELECT * FROM shadow_opportunities WHERE model='h2_ma_separation_015' "
            "AND ROUND(normalized_separation,4)=? AND approved=0", (alvo,)).fetchone()
        if r is None:
            print(f"separação {alvo}: NÃO ENCONTRADA")
            continue
        res = simular_uma(c, cfg, r)
        res["separacao"] = alvo
        resultados.append(res)
        print(f"separação {alvo} | {r['symbol']} {r['direction']} em {r['source_candle_open_time']}:")
        for k, v in res.items():
            if k not in ("separacao",):
                print(f"    {k}: {v}")
        print()

    ganhariam = sum(1 for r in resultados if r["resultado"] == "ganharia")
    perderiam = sum(1 for r in resultados if r["resultado"] == "perderia")
    recusados = sum(1 for r in resultados if r["resultado"] == "recusado_por_outro_gate")
    print("=" * 60)
    print(f"RESUMO: ganhariam={ganhariam} perderiam={perderiam} "
          f"recusados_por_outro_gate={recusados} de {len(resultados)}")
    print("Amostra de 5 -- não usado para alterar o threshold nem para "
          "qualquer conclusão de vantagem estatística.")


if __name__ == "__main__":
    main()
