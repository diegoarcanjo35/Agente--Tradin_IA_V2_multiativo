# Métricas

Todas as métricas são calculadas por `app/metrics/engine.py::compute_metrics()`,
uma função pura que recebe apenas posições **fechadas** (`ClosedTrade`) e o
saldo inicial. Quando um valor não pode ser calculado (ex.: nenhuma operação
perdedora, para `profit_factor`), o campo retorna a string `"indisponível"` —
nunca `0`.

O teste `tests/test_reproducible_fixture.py` recalcula cada fórmula abaixo à
mão a partir de `fixtures/reproducible_trades.json` e compara com a saída do
motor. Um auditor pode reproduzir os mesmos números manualmente.

## Consolidado vs. por símbolo (Fase 3 multiativo)

`app/metrics/engine.py` não muda nesta fundação — permanece uma função pura
sobre uma lista plana de `ClosedTrade`. `GET /api/metrics`
(`app/api/routes_dashboard.py`) é quem agrupa: chama `compute_metrics()`
**uma vez com todas as posições fechadas** (resultado consolidado, chaves de
topo inalteradas — compatibilidade total com instalações monoativo) e **uma
vez por símbolo configurado**, filtrando as mesmas linhas por `symbol` antes
de cada chamada, expondo o resultado aditivo em `per_symbol: {SYMBOL:
{...}}`. Funding segue a mesma lógica: somado via `repo.funding_total(session,
symbol)` por símbolo configurado, nunca uma soma irrestrita de toda a tabela
`funding_events` (o que incluiria incorretamente símbolos fora da
configuração atual). Ver `tests/test_multiativo_risk_and_metrics.py::test_per_symbol_metrics_sum_to_consolidated`.

## Fórmulas

- **Lucro bruto** = soma dos `realized_pnl` positivos.
- **Prejuízo bruto** = soma dos `realized_pnl` negativos (valor negativo).
- **Comissões** = soma de `fees_paid` de todas as operações fechadas.
- **Lucro líquido** = lucro bruto + prejuízo bruto − comissões + funding
  (0.0 quando funding é `"indisponível"` — nunca deixa o lucro líquido
  indisponível só por causa disso).
- **Funding** (correção v1.1 #6) = soma real de `funding_events` do período
  (`repo.funding_total`), **só quando há um `funding_provider` configurado**
  (exclusivamente `BYBIT_DEMO` — ver abaixo). `0.0` é um total genuíno
  quando o provider existe mas nada foi coletado ainda; `"indisponível"`
  é reservado para quando não há provider algum (`REPLAY`/`PAPER_LOCAL`/
  `PAPER_LIVE`) — os dois nunca são confundidos.
- **Taxa de acerto** = nº de operações vencedoras / nº total de operações
  fechadas.
- **Ganho médio** = lucro bruto / nº de operações vencedoras.
- **Perda média** = prejuízo bruto / nº de operações perdedoras.
- **Payoff** = |ganho médio / perda média|.
- **Profit factor** = lucro bruto / |prejuízo bruto| (indisponível se não
  houver prejuízo).
- **Expectativa matemática** = taxa de acerto × ganho médio + (1 − taxa de
  acerto) × perda média.
- **Maior sequência de ganhos / perdas** = maior run consecutivo de
  `realized_pnl > 0` / `< 0`, na ordem de fechamento.
- **Curva de equity** = saldo inicial + soma cumulativa de
  (`realized_pnl − fees_paid`) por operação fechada, em ordem cronológica.
- **Drawdown máximo (dinheiro)** = maior queda (pico − vale) observada na
  curva de equity.
- **Drawdown máximo (%)** = drawdown máximo em dinheiro / valor do pico
  correspondente × 100.
- **Retorno sobre capital (%)** = lucro líquido / saldo inicial × 100.
- **Retorno/Drawdown** = lucro líquido / drawdown máximo em dinheiro
  (indisponível se não houve drawdown).
- **Exposição** = soma de `qty × avg_entry_price` das posições **abertas**
  no momento da consulta — nunca misturada com o P&L realizado.

## Separação realizado vs. não realizado

`compute_metrics()` só enxerga posições fechadas (`status = CLOSED`); uma
posição aberta não contribui com nenhum valor de P&L realizado, apenas com
`exposure_usd` (opcional, informativo). O painel consulta posições abertas
por um endpoint separado (`/api/positions`), nunca fundido com os números de
`/api/metrics`.

## Custos e slippage (Fase 2, item 7.6)

`app/metrics/engine.py::compute_cost_metrics()` — função pura separada de
`compute_metrics()`, recebe `OrderFillView` (visão mínima de uma ordem
preenchida: `side`, `reference_price`, `avg_fill_price`, `fees_total`),
alimentada por `repo.filled_orders()` via `GET /api/costs`:

- **Taxas acumuladas** = soma de `fees_total` de toda ordem preenchida —
  sempre um valor real (0.0 para um conjunto vazio é um total genuíno, não
  fabricado).
- **Slippage realizado** = diferença entre o preço de execução
  (`avg_fill_price`) e o preço de referência (`reference_price` — o preço
  do candle/gatilho que originou a decisão, gravado em `orders.reference_price`
  no momento do envio). Para `BUY`, pagar MAIS que a referência é
  desfavorável (valor positivo); para `SELL`, receber MENOS é desfavorável.
  Slippage favorável aparece como valor negativo — nunca truncado a zero,
  para não esconder informação real do operador.
- Só é calculado sobre ordens com `reference_price` conhecido
  (`priced_orders_count`); se nenhuma ordem tiver essa informação (ex.:
  ordens migradas de antes da Fase 2), `slippage_avg_usd`/`slippage_total_usd`
  retornam `"indisponível"` — nunca um zero fabricado. `unpriced_orders_count`
  reporta quantas ordens ficaram de fora desse cálculo.

## Funding real (correção v1.1 #6, corrigido em v1.2 #3)

`app/execution/funding.py::BybitFundingProvider.list_funding(symbol, since,
until)` consulta o extrato de transações da Bybit
(`/v5/account/transaction-log`, `type=SETTLEMENT`) através do mesmo
transporte injetável `(url, params) -> dict` usado em todo
`app/execution`/`app/market_data` — testável com `tests/fakes/bybit_fake.py`,
zero rede real. `record_new_funding_events` deduplica por `funding_id`
(índice único, migração v4) — repetir o mesmo lançamento entre polls, ou
após um reinício com uma janela `since` sobreposta, é sempre um no-op
seguro.

**Correção v1.2 #3**: `list_funding` lia `row["change"]` — para uma linha
`SETTLEMENT`, esse é o delta TOTAL da conta (pode incorporar `cashFlow` e
`fee`), não o valor de funding propriamente dito. O campo correto,
`row["funding"]`, é agora o único jamais persistido como valor de funding.
`list_funding` também pagina integralmente (`nextPageCursor`, com guarda
contra cursor repetido, página malformada e limite defensivo de páginas) e
nunca lança exceção nesse processo — retorna `(records, complete)`, e o
chamador (`Orchestrator._maybe_collect_funding`) fatia o intervalo
`[since, now]` em janelas de até `FUNDING_WINDOW_SECONDS` (7 dias),
persistindo cada janela assim que coletada e parando na primeira janela
incompleta (nunca avança além de uma coleta comprovadamente completa). Uma
linha com campo obrigatório ausente/inválido (`id`, `symbol`, `funding`,
`transactionTime`) NUNCA é convertida num zero fabricado — e, desde a
correção v1.3 #2, torna a janela inteira `complete=False` (deixa de ser
apenas descartada silenciosamente com a página ainda contando como
completa), com um diagnóstico estruturado (sem payload/credenciais)
registrado via log.

### Checkpoint de cobertura (correção v1.3 #1)

O `since` de cada ciclo de coleta é lido de `FundingCollectionCheckpoint`
(`repo.get_funding_checkpoint`/`advance_funding_checkpoint`, migração v6) —
**nunca** do maior `occurred_at` já persistido em `funding_events`
(abordagem anterior, insegura): numa resposta paginada do mais novo para o
mais antigo, a página 1 podia persistir um registro recente e a página 2
(mais antiga) falhar; o próximo ciclo, usando o maior `occurred_at`
disponível, pularia o backlog ainda não coletado dessa mesma janela,
tornando-o irrecuperável. O checkpoint só avança
(`advance_funding_checkpoint`) quando uma janela inteira é comprovadamente
completa (paginação inteira percorrida, nenhuma linha inválida) — nunca
parcialmente, nunca com base em quais registros vieram ou em que ordem.

Coleta periódica via `Orchestrator._maybe_collect_funding`, gated por
`FUNDING_POLL_INTERVAL_SECONDS`, **só quando `mode=BYBIT_DEMO`**
(`app/api/main.py::build_orchestrator` só constrói um `BybitFundingProvider`
nesse branch — o único com credenciais privadas). `PAPER_LIVE` reaproveita o
mesmo transporte público de mercado, mas nunca ganha um `funding_provider`:
funding ali permanece `"indisponível"` com razão explícita ("modo
simulado, sem lançamentos reais de funding a coletar") — nunca simulado e
misturado com dado real coletado.

## AI Shadow: concordância e contrafactual (Fase 2, item 7.10)

`app/metrics/ai_shadow_metrics.py::compute_ai_shadow_metrics()` — pura,
compara cada recomendação da IA com a decisão real e autoritativa da
estratégia (nunca o contrário: a IA nunca influencia a decisão real).
`hypothetical_hit_rate` e `counterfactual_pnl` são **explicitamente
retrospectivos** (só sobre operações que a estratégia já fechou de verdade
e onde a IA concordou com a direção real) e devem ser sempre rotulados como
**SIMULAÇÃO** em qualquer lugar que apareçam — nunca apresentados como um
histórico real de desempenho da IA, já que ela nunca teve autoridade de
execução.
