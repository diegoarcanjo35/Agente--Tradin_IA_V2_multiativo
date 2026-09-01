# Painel Gráfico (Fase 3.1)

Central operacional gráfica multiativo, construída sobre o painel HTML/CSS/
JS puro já existente (sem bundler, sem framework) — um card novo
("Gráfico de Candles") usando **TradingView Lightweight Charts™** 4.1.4,
vendorizado localmente em `frontend/vendor/` (ver
`THIRD_PARTY_NOTICES.md` para origem, versão, licença e SHA-256).

## Arquitetura

```
GET /api/chart-data?symbol=X&limit=N   (app/api/routes_dashboard.py)
        │
        ├─ repo.recent_candles(session, symbol, timeframe, limit)  -- tabela candles
        ├─ orch.visual_price_state[symbol]  -- cache em memória, atualizado a
        │     cada tick() (app/orchestrator.py), NUNCA uma chamada de rede
        │     nova dentro da rota
        ├─ repo.open_positions(session, symbol)  -- posição aberta, se houver
        ├─ repo.recent_signals(session, symbol=symbol)  -- sinais acionáveis
        └─ orch.orchestrators[symbol].strategy_engine.config  -- períodos reais
        │
        ▼
frontend/app.js::refreshChart()  -- entra no mesmo ciclo de refreshAll()
   (setInterval de 2s já existente, sem timer novo), popula o gráfico
   Lightweight Charts (candlestick + volume + SMA rápida/lenta + marcadores
   BUY/SELL + linhas/faixas de posição).
```

Nenhuma chamada de rede acontece dentro da rota HTTP — nem para a Bybit, nem
para qualquer serviço externo. O preço "ao vivo" exibido no gráfico nunca
vem de uma consulta feita pelo navegador; vem de um cache em memória do
processo, atualizado pelo próprio ciclo de polling do orquestrador que já
existia antes desta fase.

## Preço visual vs. candle fechado

- **Candle fechado**: uma linha real da tabela `candles`, produzida pelo
  pipeline normal (`MarketDataProvider` → `Orchestrator.tick()` →
  `repo.save_candle`). É o único dado que alimenta `StrategyEngine`/
  `RiskEngine`/execução.
- **Preço visual** (`visual_price`/`visual_price_at`/`visual_price_source`):
  um valor **puramente observacional**, nunca persistido, nunca lido por
  nenhum código de decisão. Duas origens possíveis:
  - `"forming_candle"`: o preço de fechamento do candle **ainda em
    formação**, capturado em `BybitDemoMarketDataProvider` no exato ponto
    em que a linha "ainda em formação" já é descartada do fluxo normal
    (nunca entra na fila de candles fechados, nunca move o cursor). Só
    existe em `PAPER_LIVE`/`BYBIT_DEMO` — nunca em `REPLAY`/`PAPER_LOCAL`.
  - `"last_closed_candle"`: fallback honesto quando não há preço em
    formação disponível (REPLAY/PAPER_LOCAL, ou símbolo recém-configurado)
    — o fechamento do último candle persistido, rotulado explicitamente
    como tal no painel, nunca fingindo ser um preço em tempo real.

## Contrato da API — `GET /api/chart-data`

| Parâmetro | Obrigatório | Regra |
|---|---|---|
| `symbol` | sim | precisa estar em `Settings.symbols`; caso contrário, `404` com mensagem clara |
| `limit` | não (padrão 500) | **normalizado** (nunca rejeitado) para o intervalo `[50, 2000]` |

Resposta (todos os campos sempre presentes, `null` quando não aplicável):

```json
{
  "symbol": "BTCUSDT", "timeframe": "1m",
  "candles": [{"time": 1735689600, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 10}],
  "visual_price": 65123.4, "visual_price_at": "2026-09-01T12:00:00+00:00",
  "visual_price_source": "forming_candle",
  "strategy_config": {"fast_period": 9, "slow_period": 21, "atr_period": 14, "min_atr_pct_of_price": 0.0005, "max_atr_pct_of_price": 0.05, "stop_loss_atr_multiple": 2.0, "take_profit_atr_multiple": 3.0},
  "position": {"side": "BUY", "qty": 0.01, "avg_entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0, "opened_at": "..."},
  "recent_signals": [{"time": 1735689660, "direction": "BUY", "price": 100.0, "justification": "...", "order_status": "FILLED", "realized_pnl": null}],
  "symbol_health": {"status": "SAUDAVEL", "consecutive_failures": 0, "has_gap": false, "..."},
  "generated_at": "2026-09-01T12:00:05+00:00"
}
```

`candles[].time` é sempre um timestamp UNIX em segundos (exigido pelo
Lightweight Charts). `recent_signals` só inclui sinais acionáveis
(`BUY`/`SELL`, nunca `HOLD`), com `order_status` da ordem resultante quando
rastreável via `RiskEvaluation`→`Order`. `realized_pnl` por sinal individual
é uma **limitação conhecida**: `Position` é um agregado sem referência de
volta ao sinal/ordem que a abriu, então esse campo é sempre `null` nesta
fundação (nunca fabricado).

## Legenda de cores e linhas

| Elemento | Cor/estilo |
|---|---|
| Candle de alta | verde (`#34d399`) |
| Candle de baixa | vermelho (`#f87171`) |
| SMA rápida (período configurado, padrão 9) | âmbar (`#fbbf24`) |
| SMA lenta (período configurado, padrão 21) | azul (`#60a5fa`) |
| Marcador de compra | seta verde para cima, abaixo do candle |
| Marcador de venda | seta vermelha para baixo, acima do candle |
| Linha de entrada | azul sólida |
| Linha de preço atual | branca/amarela tracejada |
| Linha de alvo (take-profit) | verde sólida |
| Linha de stop (stop-loss) | vermelha sólida |
| Faixa entre entrada e alvo | verde translúcida |
| Faixa entre entrada e stop | vermelha translúcida |

As faixas translúcidas são `<div>`s posicionados via
`series.priceToCoordinate()` (o Lightweight Charts não tem um primitivo
nativo de "banda entre duas linhas de preço") — recalculadas a cada
mudança de intervalo visível ou redimensionamento
(`ResizeObserver`/`subscribeVisibleTimeRangeChange`).

## Multiativo e atualização

- O gráfico exibe sempre exatamente um símbolo por vez, escolhido no
  seletor (populado por `GET /api/symbols`).
- `refreshChart()` roda dentro do mesmo `Promise.all` de `refreshAll()` já
  existente (ciclo de 2s) — nenhum timer novo.
- Guarda contra corrida de troca de símbolo: cada chamada de
  `refreshChart()` captura o símbolo selecionado antes do `fetch`; se o
  usuário trocar de símbolo antes da resposta chegar, o resultado é
  descartado silenciosamente (nunca aplica dados do símbolo errado).
- Trocar de símbolo remove todas as linhas/faixas de posição do símbolo
  anterior antes de aplicar o novo estado.
- Falha na rota/gráfico é isolada em `try/catch` dentro de `refreshChart()`
  — nunca derruba `refreshAll()` nem os demais cards.

## Segurança

Estritamente observacional: nenhum botão de compra/venda manual, nenhum
drag para alterar stop/alvo/entrada, nenhuma credencial, nenhum endpoint
privado, nenhum iframe da Bybit, nenhuma chamada de rede em tempo de
execução além das rotas HTTP do próprio backend local. Todo texto vindo do
backend é inserido via `textContent`/criação de elementos DOM — nunca
`innerHTML` (`tests/test_frontend_xss_safety.py` cobre isso para o arquivo
inteiro, incluindo o código do gráfico).

## Limitações conhecidas

- `realized_pnl` por sinal individual sempre `null` (ver acima).
- Itens 14 (marcadores no timestamp exato) e 17 (layout responsivo
  desktop/mobile) da matriz de testes não têm cobertura automatizada de
  ponta a ponta nesta fundação — não há framework de teste visual/E2E na
  base; verificados por inspeção manual e pela lógica pura testada via
  harness Node.js (`tests/test_chart_js_logic.py`).
- Preço visual só existe em `PAPER_LIVE`/`BYBIT_DEMO`; `REPLAY`/
  `PAPER_LOCAL` sempre mostram o fallback "último fechamento".

## Como usar

Com o servidor rodando localmente (`python -m app.run`, porta padrão
`8001` para a V2 multiativo — ver `.env.example`), acesse
`http://127.0.0.1:8001` e use o seletor de símbolo no card "Gráfico de
Candles".
