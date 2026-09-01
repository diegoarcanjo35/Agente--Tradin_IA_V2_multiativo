# Arquitetura — Agente de Trading IA (Fase 1 + Fase 2)

## Visão geral

```
MarketDataProvider ──► StrategyEngine ──► RiskEngine ──► ExecutionEngine ──► Persistence
        │                                                                        ▲
        └──────────────────────► AIShadowAgent (observação, sem autoridade) ────┘
```

O `Orchestrator` (`app/orchestrator.py`) é o único módulo que conhece tanto o
`RiskEngine` quanto um `ExecutionEngine`. Isso torna a garantia "o Risk Engine
tem autoridade soberana" auditável por inspeção: basta localizar todos os
pontos que chamam `ExecutionEngine.submit(...)` — há exatamente dois
caminhos de código (abertura via `RiskEngine.evaluate()`, fechamento via
`RiskEngine.evaluate_close()`), e ambos só produzem um `ApprovedOrder`.
`tests/test_risk_engine_boundary.py` prova isso por AST: nenhum arquivo fora
de `app/risk/engine.py` referencia o token privado de aprovação ou constrói
`ApprovedOrder` diretamente.

## Módulos

- **app/core** — configuração (`config.py`, com `RunMode` e allowlist de hosts
  Bybit demo/testnet), relógio UTC (`clock.py`), logging estruturado com
  redação de segredos (`logging.py`), exceções de domínio (`errors.py`).
- **app/persistence** — modelos SQLAlchemy (`models.py`), um repositório
  fino de funções tipadas (`repo.py`) sobre uma `Session`, e um sistema de
  migrações versionadas próprio (`migrations.py`, ver `docs/MIGRACOES.md`)
  que `db.py::init_db()` executa a cada start do processo — nunca um
  `create_all()` cru, que não altera tabelas já existentes.
- **app/run.py** — único ponto de entrada suportado para iniciar o
  servidor (`python -m app.run`); repassa `Settings.api_host`/`api_port`
  validados diretamente a `uvicorn.run()`.
- **app/market_data** — `ReplayMarketDataProvider` (fixture local, zero rede)
  e `BybitDemoMarketDataProvider` (REST polling com backoff exponencial,
  válido apenas contra hosts demo/testnet).
- **app/strategy** — `StrategyEngine`: cruzamento de médias móveis (SMA
  rápida/lenta) com filtro de volatilidade por ATR. Determinístico, sem
  aprendizado de máquina.
- **app/risk** — `RiskEngine`: única fonte de aprovação de ordens, tanto para
  abrir (`evaluate()`) quanto para fechar/reduzir (`evaluate_close()`).
  Produz um `ApprovedOrder`, cuja construção exige um token opaco
  (`_RiskApprovalToken`) que só existe dentro de `app/risk/engine.py`.
- **app/execution** — `PaperLocalExecutionEngine` (simulação local, preenche
  usando o `reference_price` explícito passado pelo chamador — o preço do
  candle que gerou a decisão, ou o preço de gatilho de um stop/take) e
  `BybitDemoExecutionEngine` (Bybit Demo Trading real, via cliente HTTP
  injetado; produção usa `app/execution/bybit_pybit_client.py`, um adaptador
  sobre o cliente oficial `pybit`). Nenhum dos dois aceita nada além de um
  `ApprovedOrder`. Um HTTP 200 na criação da ordem nunca é tratado como
  execução confirmada — o motor sempre consulta o status da ordem antes de
  reportar `FILLED`. `app/execution/order_state.py` (Fase 2) define a
  máquina de estados explícita de ordens e sua tabela de transições
  permitidas — ver `docs/ORDEM_E_FILLS.md`. Ambos os motores implementam
  `cancel()` (Fase 2, item 7.3). `app/execution/reconciliation.py` compara
  posições locais com as reportadas pela exchange; `Orchestrator.reconcile()`
  integra essa checagem à inicialização, a qualquer ordem que termine em
  `UNKNOWN`, e agora também periodicamente (Fase 2, item 7.4).
- **app/ai_shadow** — `AIShadowAgent` + `SimulatedProvider` (determinístico,
  offline, provedor padrão). Produz apenas dados estruturados validados;
  nunca referencia `app.execution` nem credenciais Bybit (ver
  `app/ai_shadow/guard.py` e `tests/test_ai_shadow.py`).
- **app/metrics** — funções puras que recebem posições fechadas e retornam
  métricas; nunca inventam zero quando o dado é insuficiente (retornam a
  string `"indisponível"`).
- **app/sessions** (Fase 2) — cria/retoma `operational_sessions` e
  incrementa seus contadores; ver `docs/SESSOES_OPERACIONAIS.md`.
- **app/api** — FastAPI: rotas de leitura (`routes_dashboard.py`) e de
  controle (`routes_control.py`: kill switch + `operational-state/activate`
  e `/pause` desde a Fase 2 — não há endpoint de troca de modo/ambiente).
- **app/orchestrator.py** — conecta tudo por tick.
- **frontend/** — painel single-page estático servido pelo próprio FastAPI.

## Fluxo de uma vela (tick)

1. `MarketDataProvider.next_candle()` retorna a próxima vela (ou `None`).
2. A vela é persistida (`candles`).
3. `StrategyEngine.on_candle()` produz um `Signal` (BUY/SELL/HOLD +
   justificativa + parâmetros usados). Persistido em `strategy_signals`.
4. Em paralelo (mesma tick, não bloqueante), o `AIShadowAgent` observa o
   mesmo contexto de mercado e produz uma recomendação estruturada,
   persistida em `ai_recommendations` — nunca influencia os passos
   seguintes.
5. O relógio é verificado (`compute_clock_sync`); drift desconhecido ou acima
   do limite bloqueia o sistema (`TRADING_BLOCKED`) em vez de assumir zero.
6. Se houver posição aberta no símbolo, o candle é checado contra o
   stop-loss/take-profit da posição (`_check_stop_take`); se tocado, o
   fechamento segue direto para o passo 8 via `RiskEngine.evaluate_close()`.
7. Senão, se houver posição aberta na direção oposta ao sinal da estratégia,
   o Orchestrator tenta fechá-la (reduzir risco é sempre permitido, exceto
   sob kill switch/TRADING_BLOCKED/dados obsoletos/estado ambíguo) via
   `RiskEngine.evaluate_close()` — passo 8, e a tick termina aí.
8. Fechamento: `evaluate_close()` aprova ou rejeita (sempre persistido em
   `risk_evaluations`, ligado ao sinal originador — nunca `signal_id=0`); se
   aprovado, `ExecutionEngine.submit(approved, key, reference_price=...)` é
   chamado com o preço de gatilho correto; ordem, execução, taxas (somadas
   às já acumuladas na posição) e o fechamento/redução da posição são
   persistidos; se a ordem terminar em erro, `Orchestrator.reconcile()` roda
   antes de retornar.
9. Caso contrário (sem posição a fechar), `RiskEngine.evaluate()` roda todos
   os checks de abertura — incluindo, desde a Fase 2, se a reconciliação
   está atrasada (`reconciliation_stale`) e se `operational_state == "ATIVO"`
   (item 7.8: novas entradas exigem ativação explícita do operador) — e
   retorna aprovação ou rejeição, sempre persistido em `risk_evaluations`.
10. Se aprovado, `ExecutionEngine.submit(..., reference_price=candle.close)`
    é chamado com uma chave de idempotência derivada do sinal; o resultado
    (fill total, parcial, `UNKNOWN`) é persistido em `orders`/`executions`
    através da máquina de estados (`repo.transition_order_status`/
    `repo.record_fill`), e uma posição é aberta com a taxa de abertura já
    contabilizada.

Além disso, no início de cada `tick()` (Fase 2, item 7.4): se o intervalo de
reconciliação configurado já passou, `Orchestrator.reconcile()` roda antes
de qualquer outra decisão, e `SystemState.reconciliation_stale` é
recalculado.

## Modos de execução

`REPLAY` (padrão) → `PAPER_LOCAL` → `PAPER_LIVE` (Fase 2: dados reais,
execução simulada) → `BYBIT_DEMO`. Detalhes em `docs/OPERACAO_DEMO.md` e
`docs/FASE_2.md`. Não existe endpoint ou variável de ambiente que alterne
modo em tempo de execução — é configuração de processo, definida uma vez na
inicialização. Separado disso, `operational_state`
(`INICIALIZANDO`/`OBSERVANDO`/`ATIVO`/`PAUSADO`/`BLOQUEADO`/`ENCERRANDO`,
Fase 2, item 7.8) controla se a estratégia pode abrir novas posições —
sempre nasce em `OBSERVANDO`, nunca `ATIVO` automaticamente.

## Multiativo (Fase 3, fundação)

### Configuração e precedência

`SYMBOLS` (CSV, ex. `BTCUSDT,ETHUSDT,SOLUSDT`) substitui `SYMBOL` como fonte
primária de verdade quando definido. A ordem de declaração é preservada (não
reordenada alfabeticamente) — é essa ordem que define tanto o round-robin do
scheduler quanto a identidade da sessão (ver abaixo). `SYMBOL` continua
funcionando sozinho para instalações monoativo (`symbols = [SYMBOL]`, 100%
do comportamento anterior preservado). Se ambos forem definidos, precisam
ser consistentes (`SYMBOL == symbols[0]`) ou a inicialização é recusada como
configuração ambígua — nunca escolhida silenciosamente (ver
`app/core/config.py::Settings._resolve_symbols_precedence`). `BYBIT_DEMO`
autenticado permanece monoativo nesta fase; com mais de um símbolo, o boot
recusa `API_PORT=8000` e qualquer `DATABASE_URL` com o nome de arquivo
conhecido da V1 (`agente_trader_paper_live.db`).

### Scheduler — round-robin sequencial, sem concorrência

`app/orchestrator.py::MultiSymbolOrchestrator` compõe uma instância de
`Orchestrator` **por símbolo** (cada uma com seu próprio `MarketDataProvider`
e `StrategyEngine` — isolamento total de backlog/cursor e de janelas móveis
de indicador entre símbolos), todas compartilhando o mesmo `session_factory`
(→ o mesmo `SystemState` singleton e a mesma `OperationalSession` de
portfólio), `RiskEngine` e `ExecutionEngine`. Cada chamada de `tick()`
continua processando **um símbolo por vez** — nunca múltiplas threads —
preservando o mecanismo (executor único de `poll_engine.py`) que já garante
hoje que duas decisões de risco nunca leem o mesmo saldo desatualizado. O
símbolo processado é escolhido por um índice round-robin determinístico; um
símbolo com falhas consecutivas (`SYMBOL_PARADO_THRESHOLD`, hoje 3) entra em
backoff e tem seu turno pulado **na mesma chamada**, nunca atrasando o ciclo
dos demais.

### Saúde por símbolo (em memória, não persistida)

`MultiSymbolOrchestrator.health: dict[symbol, SymbolHealth]` — estados
`INICIANDO`/`SAUDAVEL`/`DEGRADADO`/`PARADO`/`ENCERRANDO`, reconstruído do
zero (todos `INICIANDO`) a cada boot, exposto via `GET /api/state` tanto
agregado (`portfolio`, regra de precedência do pior estado) quanto detalhado
(`per_symbol`). O gate de ativação (`RiskEngine.evaluate()`) é **global e
conservador** (AND estrito): novas entradas só são autorizadas quando
**todos** os símbolos configurados estão `SAUDAVEL` e sem lacuna de dados —
a falha de um símbolo bloqueia entradas em todos, mas nunca bloqueia
fechamento/redução de posições existentes (mesma exceção já aplicada em
`evaluate_close`).

**`PARADO` nunca é um estado terminal.** Significa "circuito temporariamente
aberto" após `SYMBOL_PARADO_THRESHOLD` (hoje 3) falhas consecutivas — o
símbolo fica inelegível para o round-robin até `eligible_again_at`
(`now + poll_backoff_max_seconds`, fixo, não exponencial). Passado esse
instante, o símbolo volta **automaticamente** ao scheduler no próximo ciclo
(`MultiSymbolOrchestrator.tick()`, `app/orchestrator.py`) — sem intervenção
manual. Um único tick bem-sucedido zera `consecutive_failures` e retorna o
símbolo direto a `SAUDAVEL`. A precedência de agregação
(`ENCERRANDO > PARADO > DEGRADADO > INICIANDO > SAUDAVEL`) e o gate de
ativação como AND estrito permanecem exatamente como descritos acima — esta
correção não os altera, apenas documenta o comportamento de recuperação já
implementado.

### Risco e sessão — decisões explícitas desta fundação

`RISK_MAX_TOTAL_EXPOSURE_USD`/`RISK_MAX_CONCURRENT_POSITIONS` permanecem
globais em `RiskLimits`, computados sobre **todas** as posições abertas
independente de símbolo (já era assim antes da Fase 3). Cooldown/kill-switch
(`SystemState`) também permanecem globais — uma sequência de perdas em um
símbolo pode gerar cooldown que bloqueia entradas em todos. A sessão
operacional (`docs/SESSOES_OPERACIONAIS.md`) passa a ser **uma única sessão
de portfólio**, identificada por `(mode, símbolos ordenados, timeframe,
estratégia, limites de risco)` — nunca uma sessão por símbolo.

## Painel gráfico (Fase 3.1)

Ver `docs/PAINEL_GRAFICO.md` para o desenho completo: rota somente-leitura
`GET /api/chart-data`, preço visual (candle em formação) vs. candle
fechado, TradingView Lightweight Charts vendorizado localmente (ver
`THIRD_PARTY_NOTICES.md`), e isolamento multiativo do gráfico (troca de
símbolo nunca mistura estado).

## Estratégia multitemporal (Fase 3.2)

Ver `docs/ESTRATEGIA_MULTITEMPORAL.md` para o desenho completo: separação
entre a cadência de COLETA (1 minuto, fixa) e a de DECISÃO (1/5/15
minutos, configurável), a canonicalização do timeframe (`"1m"` canônico,
`"1"` apenas como alias legado de leitura), o agregador determinístico com
fechamento imediato e bucket incompleto explícito, a hidratação silenciosa
do estado estratégico no reinício, o gate de viabilidade líquida antes da
abertura e a reancoragem de stop/alvo no preço médio real do fill.

## Painel financeiro (Fase 3.1.1)

Ver `docs/PAINEL_FINANCEIRO.md` para o desenho completo: fórmula oficial
de patrimônio (equity), escopos (`lifetime`/`session`/`daily`), marcação a
mercado de posições abertas, contrato definitivo de slippage (impacto
financeiro real, nunca a diferença unitária de preço sozinha), e prevenção
de dupla contagem entre taxas/slippage/funding/P&amp;L.
