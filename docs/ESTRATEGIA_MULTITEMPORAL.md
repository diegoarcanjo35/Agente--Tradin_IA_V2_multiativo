# Estratégia multitemporal e viabilidade líquida (Fase 3.2)

Este documento descreve a separação definitiva entre **coleta**,
**decisão**, **execução/acompanhamento** e **viabilidade após custos**.

> Nada aqui constitui promessa de rentabilidade. O sistema é um simulador
> de operação em papel; ver `docs/OPERACAO_DEMO.md`.

## 1. As duas cadências

| | Coleta (mercado) | Decisão (estratégia) |
|---|---|---|
| Cadência | 1 minuto, **fixa** nesta fase | 1, 5 (default) ou 15 minutos |
| Configuração | `market_data_timeframe_minutes` (snapshot; não configurável) | `STRATEGY_TIMEFRAME_MINUTES` |
| Persistência | todo candle fechado vai para `candles` | nada é persistido (ver §3) |
| Painel | gráfico de candles de 1 minuto | bloco "Visão da estratégia" |
| Saúde/gap/cursor/backlog | sempre pelo fluxo de 1 minuto | não participa |

O candle de 1 minuto **continua sendo a fonte primária**. Nenhum candle
de 1 minuto deixa de ser persistido ou exibido por causa da agregação.

### Por que 5 minutos como padrão

Duas razões técnicas, não estéticas:

1. **Aquecimento.** A SMA lenta exige 21 candles ESTRATÉGICOS. São 105
   minutos em 5m contra 5h15 em 15m. Depois de qualquer reinício ou
   mudança de configuração, 15m deixa o sistema sem poder decidir por mais
   de cinco horas.
2. **Cobertura de teste em REPLAY.** A fixture histórica tem 180 candles
   de 1 minuto (3h): 36 buckets em 5m (21 de aquecimento + 15 úteis) e
   apenas 12 em 15m -- que não chegam a completar o aquecimento. 15m é
   suportado e testado (cadência de decisão, agregação, guarda de
   mudança), mas a fixture atual, deliberadamente preservada, não gera
   sinal acionável nesse timeframe.

## 2. Timeframe canônico (decisão Q4 do PO)

A representação **canônica**, persistida e exposta na API, é `"1m"`
(`"5m"`, `"15m"`). `"1"` é aceito **apenas como alias legado de leitura**.

- `app/core/timeframe.py` é a única fonte: `canonical_timeframe`,
  `timeframe_aliases`, `minutes_to_canonical`, `bybit_interval`.
- `repo.save_candle` canonicaliza **toda** escrita.
- `repo.recent_candles` / `repo.get_last_candle_open_time` consultam
  **todos os aliases**, então um banco anterior a esta fase continua
  aparecendo no gráfico.
- Se existirem `"1"` e `"1m"` para o mesmo símbolo/`open_time`, a
  deduplicação é **lógica** (na consulta) e prefere o registro canônico.
  O banco legado **nunca** é reescrito e **nenhuma migration** foi criada.
- O formato de intervalo da Bybit (`"1"`) fica confinado à fronteira HTTP
  (`bybit_interval`).

**Defeito corrigido:** antes desta fase, BYBIT_DEMO gravava `"1"` e o
painel consultava `"1m"`; `GET /api/chart-data` devolvia `candles: []`
nesse modo. Invisível em REPLAY, onde as duas pontas coincidiam.

## 3. Agregação (`app/strategy/aggregator.py`)

OHLCV padrão: `open` do primeiro candle, `high` máximo, `low` mínimo,
`close` do último, `volume` somado. `open_time`/`close_time` alinhados ao
relógio **UTC absoluto** (nunca ao primeiro candle recebido).

**Fechamento imediato.** Um bucket 10:00–10:04 fecha ao receber o candle
fechado de **10:04** -- nunca espera o de 10:05, o que criaria um atraso
operacional artificial de um minuto inteiro.

Regras:

- na transição de bucket, o anterior é finalizado como **incompleto** se
  faltarem slots;
- nenhum bucket é finalizado duas vezes;
- candle atrasado **não reabre** bucket finalizado (descartado e contado);
- candle duplicado **não altera** contagem nem OHLCV;
- candle fora de ordem recebe tratamento explícito (descartado e contado);
- o slot é identificado pelo minuto **alinhado**, não pelo `open_time`
  cru (um candle carimbado 10:00:31 é o slot das 10:00).

Com `STRATEGY_TIMEFRAME_MINUTES=1` a agregação é **passthrough exato** --
a compatibilidade explícita com o comportamento anterior à fase.

### Agregados não são persistidos

São **reconstruídos** dos candles de 1 minuto (decisão do PO). Motivos:
zero migration, uma única fonte de verdade, idempotência (função pura dos
1m já desduplicados) e volume de banco inalterado.

*Limitação assumida:* não há registro do agregado "como foi visto na
hora". Mitigação: o OHLCV do bucket é gravado dentro de
`StrategySignal.params_json` a cada decisão -- auditoria completa, sem
tabela nova.

## 4. Bucket incompleto

Regra definitiva, **sem opção de relaxar**:

- marcado (`complete=false`, `partial=true`) com slots esperados,
  recebidos e **quais faltaram**;
- exposto no painel e na API;
- incrementa `bucket_integrity.incomplete_buckets`;
- **nunca** enviado ao `StrategyEngine`;
- OHLCV ausente **jamais** é fabricado;
- sem backfill tardio nesta fase.

O tick termina com status `incomplete_bucket`. Não é falha de
processamento (o candle chegou e foi persistido, e o status fica fora de
`FAILURE_TICK_STATUSES`), mas **é uma lacuna de mercado** -- e como tal
afeta a saúde:

- o símbolo passa a **DEGRADADO** com `has_gap=true`;
- `_portfolio_healthy()` derruba o gate da carteira, e
  `_sync_engine_degraded_to_orchestrators()` propaga `engine_degraded` a
  **todos** os símbolos: pela política global vigente, nenhuma entrada
  nova é aprovada na carteira inteira enquanto houver degradação;
- em monoativo (onde não existe `SymbolHealth`) o mesmo efeito vem de
  `Orchestrator.strategy_gap_degraded`, que entra no
  **mesmo portão entry-only** já existente (`RiskContext.engine_degraded`);
- o símbolo **nunca** vira PARADO só por bucket incompleto:
  `consecutive_failures` não é incrementado, então ele não perde turnos do
  round-robin (o que atrapalharia a própria recuperação);
- heartbeat e processamento de candles continuam; os demais símbolos
  seguem operando; posições já abertas continuam protegidas por stop/alvo
  avaliados a cada candle de 1 minuto; fechar/reduzir nunca é bloqueado; o
  kill-switch continua disponível;
- **recuperação** somente ao finalizar um bucket estratégico **completo e
  contíguo** (o agregador só finaliza em ordem). Um tick de agregação, uma
  duplicata ou um candle atrasado **nunca** limpam a lacuna.

**Sem contagem dupla.** Um gap operacional (o provider viu o buraco na
sequência de candles fechados) e um bucket estratégico incompleto (o
agregador percebeu a falta dentro da janela) são eventos diferentes e o
mesmo buraco costuma produzir os dois. Por isso `SymbolHealth` mantém
**dois contadores separados**, `operational_gaps` e
`incomplete_strategy_buckets`, nunca somados num único número de "gaps".

## 5. Ordem do tick

1. candle fechado de 1 minuto recebido
2. timeframe canonicalizado
3. candle de 1 minuto **persistido**
4. `price_state` atualizado
5. **stop/take das posições avaliado com high/low de 1 minuto**
6. agregador alimentado
7. sem candle estratégico completo → `aggregating` / `incomplete_bucket`
8. com candle completo → `StrategyEngine.on_candle`
9. sinal persistido com o snapshot do bucket
10. risco + gate de custos
11. execução, se aprovado
12. proteção recalculada a partir do fill real

O passo 5 roda **antes** de qualquer nova decisão estratégica: uma
posição que deveria ter sido encerrada nunca interfere no sinal. O
resultado do stop/take é **guardado** e devolvido só depois do passo 6/8 --
caso contrário um tick que dispara stop abriria um buraco permanente no
bucket e na janela móvel do engine. O que é abandonado nesse tick é a
**entrada nova**, nunca a integridade da série.

Cooldown, posição única e idempotência permanecem exatamente como estavam.

## 6. Hidratação silenciosa no restart

`Orchestrator.hydrate_strategy_state(session)` roda uma vez no boot
(`build_orchestrator`) e reconstrói, dos candles de 1 minuto já
persistidos: SMA rápida, SMA lenta, ATR, `_prev_fast_above_slow` e o
bucket parcial em curso.

Profundidade: `(warmup_required + 1) x slots_por_bucket`, derivada da
configuração -- nunca um número fixo espalhado pelo código.

**Silenciosa por contrato:** não persiste sinal, não cria ordem, não
incrementa contador operacional, não chama o motor de risco. Buracos
históricos permanecem buracos.

## 7. Estratégia: SMA, não EMA

As médias são **simples** (`_sma`). Não existe EMA no repositório e o
painel não pode chamá-las de EMA. O docstring do engine que afirmava
existir um "trend filter" separado foi corrigido: o único gatilho de
direção é o cruzamento, e o único filtro é a faixa de ATR%.

Configuráveis (todos entram no snapshot **e** no fingerprint):
`STRATEGY_FAST_PERIOD`, `STRATEGY_SLOW_PERIOD`, `STRATEGY_ATR_PERIOD`,
`STRATEGY_MIN_ATR_PCT`, `STRATEGY_MAX_ATR_PCT`,
`STRATEGY_STOP_LOSS_ATR_MULTIPLE`, `STRATEGY_TAKE_PROFIT_ATR_MULTIPLE`,
`STRATEGY_EXPECTED_MOVE_ATR_MULTIPLE`, `MINIMUM_COST_COVERAGE_RATIO`,
`STRATEGY_TIMEFRAME_MINUTES`.

Validações: períodos inteiros positivos; `fast < slow`; ATR positivo;
`min_atr_pct < max_atr_pct`; múltiplos positivos; razão de cobertura
positiva; timeframe em `{1,5,15}`.

## 8. Gate de viabilidade líquida

Último check antes da aprovação em `RiskEngine.evaluate` -- é o primeiro
momento em que `qty` existe, e o custo é **financeiro**, não por unidade.
`evaluate_close` **nunca** o consulta: fechar/reduzir jamais é bloqueado.

```
expected_move_per_unit = atr * strategy_expected_move_atr_multiple

BUY : projected_exit_price = entry_reference_price + expected_move_per_unit
SELL: projected_exit_price = max(entry_reference_price - expected_move_per_unit,
                                 valor_mínimo_válido)

# entrada (slippage ADVERSO: compra paga mais, venda recebe menos)
entry_fill_price = ref * (1 + slip)   # BUY
entry_fill_price = ref * (1 - slip)   # SELL
entry_notional_usd = entry_fill_price * qty
entry_fee_usd      = entry_notional_usd * fee_rate
entry_slippage_usd = |entry_fill_price - ref| * qty

# saída (lado OPOSTO, notional PRÓPRIO -- nunca reaproveita o da entrada)
exit_fill_price = projected_exit * (1 - slip)   # saída SELL
exit_fill_price = projected_exit * (1 + slip)   # saída BUY
exit_notional_usd = exit_fill_price * qty
exit_fee_usd      = exit_notional_usd * fee_rate
exit_slippage_usd = |exit_fill_price - projected_exit| * qty

round_trip_cost_usd = entry_fee + exit_fee + entry_slippage + exit_slippage
expected_move_usd   = expected_move_per_unit * qty

APROVA SE: expected_move_usd >= round_trip_cost_usd * minimum_cost_coverage_ratio
```

**Unidades.** `atr` é diferença de preço **por unidade**; só vira dinheiro
depois de multiplicado por `qty`. Nenhuma linha soma US$/unidade com US$
total -- a mesma armadilha que a Fase 3.1.1 corrigiu no slippage.

### Exemplo numérico (observado em REPLAY)

| | |
|---|---|
| qty | 0,00123610 BTC |
| ATR (US$/unidade) | 121,2121 |
| movimento esperado | US$ 0,1498 |
| notional de entrada | US$ 49,975 |
| notional de saída | US$ 49,875 (**diferente**) |
| taxa de entrada / saída | US$ 0,029985 / US$ 0,029925 |
| slippage de entrada / saída | US$ 0,025000 / US$ 0,024925 |
| custo de ida e volta | US$ 0,10984 |
| cobertura alcançada | 1,36× |
| cobertura exigida | 3,00× |
| **decisão** | **rejeitada** |

### `minimum_cost_coverage_ratio = 3.0`

**Hipótese operacional inicial, configurável.** Baseada na recomendação
de exigir margem confortavelmente superior ao custo. **Não** é parâmetro
comprovadamente otimizado, não foi validado contra resultado histórico e
**não** é promessa de rentabilidade.

### Origem da estimativa por modo

| Modo | Origem | `estimate_source` |
|---|---|---|
| REPLAY / PAPER_LOCAL | lidos do próprio `PaperLocalExecutionEngine` | `paper_config` |
| PAPER_LIVE | idem (o engine recebe `PAPER_LIVE_FEE_RATE`/`PAPER_LIVE_SLIPPAGE_BPS`) | `paper_config` |
| BYBIT_DEMO | `BYBIT_TAKER_FEE_RATE` / `BYBIT_EXPECTED_SLIPPAGE_BPS` | `bybit_demo_estimate` |

Em PAPER a estimativa é **exata por construção** (é o mesmo objeto que
aplica os números no fill). Em BYBIT_DEMO são **estimativas operacionais
configuradas** -- nunca consultadas da corretora, com nomes próprios, e
**nunca** reaproveitando silenciosamente a configuração do simulador.

### Casos tratados explicitamente

- **Custo zero configurado**: aprova; cobertura alcançada fica `None`
  (indefinida), nunca zero ou infinito inventado.
- **SELL com preço projetado inválido** (movimento maior que o preço):
  **recusado** com motivo explícito; o preço exposto é sempre positivo,
  com `projected_exit_price_clamped=true`.
- **Números não finitos** (NaN/Inf) e **quantidade zero**: recusados.
- **ATR indisponível**: recusado -- nunca aprovado por omissão.
- **Fechamento/redução**: nunca bloqueado.

Tudo é persistido em `RiskEvaluation.checks_json` sob `cost_gate`
(`qty`, ATR, movimentos, preços de referência e fills estimados, os dois
notionais, fees, slippages, custo total, razão exigida, razão alcançada,
origem da estimativa e o booleano final). Gate não ligado é registrado
como `{"applied": false}` -- nunca como aprovação silenciosa.

## 9. Stop e alvo após o fill

Distâncias **congeladas na decisão** e gravadas no snapshot:

```
stop_distance_per_unit   = ATR * stop_loss_atr_multiple
target_distance_per_unit = ATR * take_profit_atr_multiple
```

Reancoragem no preço médio **real** da posição, após cada fill de
**aumento**:

```
BUY : stop = avg_entry_price - stop_distance ; alvo = avg_entry_price + target_distance
SELL: stop = avg_entry_price + stop_distance ; alvo = avg_entry_price - target_distance
```

- O ATR **nunca** é recalculado com o mercado do instante do fill.
- Fill parcial: aplica o fill, lê o `avg_entry_price` atualizado, reancora
  com as mesmas distâncias, e repete a cada novo fill de aumento.
- **Não são monotônicos** -- podem se mover em qualquer direção conforme o
  preço médio.
- Fills de **redução/fechamento** não reancoram.
- Fill oposto **bloqueado** (nunca aplicado) não altera a proteção.
- Fill de fechamento **sem posição local** não cria proteção.
- Ordem legada, sem as distâncias no snapshot: a proteção continua a do
  próprio `Order`. O `Settings` atual **nunca** é consultado para
  preencher a lacuna.

Caminho do snapshot -- só por chaves estrangeiras persistidas e
obrigatórias, nunca por busca aproximada:

```
Order.risk_evaluation_id -> RiskEvaluation.signal_id -> StrategySignal.params_json
```

Como tudo passa pelo `fill_service` compartilhado, os quatro caminhos
(submit imediato, poller periódico, kill-switch, reconciliação) usam
exatamente a mesma regra.

## 10. Mudança de timeframe com posição aberta

**Bloqueada.** `StrategyTimeframeChangeBlockedError` interrompe a
inicialização quando `STRATEGY_TIMEFRAME_MINUTES` muda e existe qualquer
posição aberta na carteira.

- verificação **global**, todos os símbolos;
- roda **antes** de qualquer escrita: nenhuma sessão anterior é encerrada,
  nenhuma nova é criada, nenhum estado parcial persistido;
- sem posição aberta é permitido: cria sessão operacional nova e
  **preserva a mesma base contábil** (patrimônio, curva e custos seguem
  contínuos -- `resolve_accounting_base` compara apenas
  `paper_starting_balance_usd`);
- reinício sem mudança continua permitido;
- deliberadamente **restrita ao timeframe** -- outras mudanças de
  estratégia seguem sem guarda;
- sessão legada, sem o campo no snapshot, não dispara a guarda (não há
  valor anterior contra o qual comparar honestamente).

## 11. Idempotência

`make_idempotency_key` passa a receber, além do bucket:
`strategy_timeframe`, `strategy_version` e `session_uid` (identidade da
sessão operacional). O `timestamp_bucket` passa a ser o `open_time` do
**bucket estratégico**, o que por si só garante no máximo **uma** ordem de
entrada por bucket.

Compatibilidade: sem os três extras a chave é **byte a byte** a de antes,
então ordens já persistidas continuam sendo encontradas.

## 12. API (tudo aditivo)

`GET /api/chart-data` acrescenta `market_data_timeframe`,
`strategy_timeframe`, `strategy_timeframe_minutes`, `strategy_candles`,
`last_strategy_candle`, `forming_strategy_candle`, `bucket_integrity`,
`warmup`, `strategy_indicators` e `cost_gate`. `candles` (1 minuto)
permanece idêntico.

`GET /api/metrics` acrescenta `cost_gate` (`evaluated`,
`blocked_entries`, `avg_coverage_ratio_at_entry`), `strategy_timeframe` e
`incomplete_buckets`, além dos mesmos campos por símbolo.

`GET /api/symbols` mantém `symbols` idêntico e acrescenta `per_symbol`
com timeframe, aquecimento e integridade.

Razões globais continuam recalculadas a partir dos **totais globais**,
nunca da média simples dos percentuais por símbolo. Denominador
inexistente devolve `null`/N/D, nunca zero inventado. Slippage segue
sendo **atribuição explicativa** e nunca é subtraído de novo do
P&L/equity (ver `docs/PAINEL_FINANCEIRO.md`).

## 12.1 Fixtures de REPLAY por símbolo e banner por modo

**Dados 100% SINTÉTICOS.** Nenhuma cotação real é usada ou reproduzida.
`fixtures/generate_replay_fixture.py` gera, de forma determinística, uma
série por símbolo:

| Símbolo | Arquivo | Faixa de preço | Formato |
|---|---|---|---|
| BTCUSDT | `replay_btcusdt.json` | ~39.510 – 41.389 | sobe e depois cai, onda de período 6 |
| ETHUSDT | `replay_ethusdt.json` | ~2.122 – 2.301 | cai e depois sobe (espelhado), onda de período 9 |
| SOLUSDT | `replay_solusdt.json` | ~94,17 – 103,08 | lateral agitado, onda rápida de período 4 |

As faixas são **disjuntas** e os formatos normalizados são **diferentes**
-- nenhuma série é outra apenas reescalada ou relabelada.

**Fixture própria é obrigatória.** Um símbolo configurado em
REPLAY/PAPER_LOCAL sem `fixtures/replay_<simbolo>.json` **interrompe a
inicialização** com `ReplayFixtureMissingError`, informando o símbolo e o
caminho local esperado. Não existe fallback: emprestar a série de outro
ativo produziria preço falso, sinais duplicados, métricas contaminadas e
demonstração enganosa -- documentar o empréstimo não evitava nenhuma
dessas consequências. A verificação roda para TODOS os símbolos no
primeiro instante de `build_orchestrator`, **antes de o banco ser
aberto**: nenhuma sessão é criada, nenhum candle é persistido, nenhum
estado parcial fica para trás, e uma carteira com um símbolo sem fixture
falha inteira em vez de subir pela metade. A compatibilidade monoativa
histórica (`SYMBOL=BTCUSDT`) segue atendida pela própria fixture do BTC.

Antes deste ajuste todo símbolo em REPLAY lia `replay_btcusdt.json`
apenas trocando o rótulo: ETH aparecia cotado perto de US$ 40.000 e
produzia exatamente os mesmos sinais do BTC nos mesmos horários.

**Banner por modo.** O card do gráfico tinha um literal fixo dizendo
"PAPER LIVE MULTIATIVO" mesmo com o processo em REPLAY -- dois modos com
significados completamente diferentes. Agora o texto vem do backend
(`_chart_banner`) e reflete o modo efetivo:

| Modo | Banner |
|---|---|
| REPLAY | `REPLAY [MULTIATIVO ]— DADOS HISTÓRICOS/SINTÉTICOS — SEM MERCADO REAL` |
| PAPER_LOCAL | `PAPER LOCAL [MULTIATIVO ]— DADOS SINTÉTICOS — SEM MERCADO E SEM ORDEM REAL` |
| PAPER_LIVE | `PAPER LIVE [MULTIATIVO ]— SIMULAÇÃO LOCAL — SEM ORDEM NA CORRETORA` |
| BYBIT_DEMO | `BYBIT DEMO — MONOATIVO — CONTA DEMO DA CORRETORA, SEM DINHEIRO REAL` |

Em REPLAY e PAPER_LOCAL o painel exibe ainda a linha explícita
**"Dados REPLAY sintéticos — sem cotação real"**. BYBIT_DEMO permanece
**monoativo** nesta fase e seu banner nunca insinua carteira multiativo.

A tabela "Resumo por Símbolo" passou a exibir o preço de marcação **de
cada símbolo** (mesma `_resolve_mark_price` da equity e do gráfico, fonte
única) mesmo sem posição aberta -- antes mostrava N/D em todos, o que
escondia justamente a prova de que cada série tem preço próprio. `None`
continua sendo N/D quando genuinamente não há preço.

## 12.2 Frescor, atualidade e recepção (Fase 3.3.1)

Três conceitos que estavam colapsados num só. Confundi-los foi o que
permitiu, em 01/09, autorizar a carteira com 185 minutos de defasagem
enquanto tudo reportava saúde plena.

| Conceito | O que mede | Onde atua |
|---|---|---|
| `data_reception_recent` | `utcnow() - provider._last_received_at` — saúde da **conexão** | check no `RiskEngine` (era `data_fresh`) |
| `market_data_temporally_current` | idade do último **candle fechado** de 1 min | gate de ativação e `/api/state` |
| `signal_is_fresh` | idade do **sinal**, a partir do fechamento do bucket estratégico | check no `RiskEngine`, só ABERTURA |

**Por que o nome antigo enganava.** `data_fresh` prometia frescor de dado
e entregava recência de recepção: durante a drenagem de um backlog,
candles de quatro horas atrás são "recebidos agora" e o check passa —
corretamente, porque a conexão está viva. O nome é que estava errado.
Registros históricos com a chave `data_fresh` continuam legíveis
(`repo.rejection_reasons` reconhece as duas); nada foi reescrito.

### A fórmula do frescor

```
bucket_close_time    = source_candle_open_time + duração do timeframe estratégico
signal_delay_seconds = now - bucket_close_time

fresco  <=>  -tolerância_futuro <= signal_delay_seconds <= MAX_SIGNAL_DELAY_AFTER_CLOSE_SECONDS
```

Medido a partir do **fechamento**, nunca da abertura: um sinal de 5
minutos nasce, por construção, ~5 minutos depois da abertura do bucket.
Medir desde o `open_time` contaria a duração do próprio candle como
atraso e recusaria todo sinal legítimo.

- **`MAX_SIGNAL_DELAY_AFTER_CLOSE_SECONDS` = 300** (padrão). Unidade:
  segundos, contados **após o fechamento** do bucket.
- O limite é **INCLUSIVO**: exatamente no limite é ACEITO.
- Tolerância de 2 s para timestamp levemente no futuro (diferença de
  relógio entre corretora e máquina local é normal). Além disso, recusa.
- `source_candle_open_time` ausente → recusa. Timeframe inválido →
  recusa. Nunca aprovação por omissão.
- Tudo UTC-aware; horário local não é usado em ponto algum do cálculo.

### Entradas bloqueadas, saídas sempre permitidas

A barreira vale **somente** para abertura/aumento de exposição. Nunca
bloqueiam por frescor: fechamento, redução, stop-loss, take-profit,
liquidação de segurança, reconciliação e kill-switch. `evaluate_close`
sequer consulta a política — uma saída de proteção precisa continuar
possível justamente quando o dado está atrasado.

Ordem no `RiskEngine`: `actionable_signal` → **`signal_is_fresh`** →
dimensionamento → `cost_gate`. Um sinal defasado não chega a ser medido
pelo gate de custos.

### O que decide se a barreira se aplica: a fonte de mercado

O critério é a **semântica temporal da fonte de mercado** do modo —
**nunca** o tipo de `ExecutionEngine`:

| Semântica | Significado | Barreira |
|---|---|---|
| `live` | os timestamps do candle acompanham o presente | **aplica** |
| `historical` | série gravada; os timestamps são do passado | não se aplica |

`PAPER_LIVE` executa localmente, com `PaperLocalExecutionEngine`, e ainda
assim consome **mercado público atual**: sua fonte é `live` e ele fica
**integralmente protegido**. Usar um motor de execução local não desativa
proteção nenhuma. Um modo **não mapeado** cai em `live` por padrão — o
default é seguro, e esquecer de mapear um modo novo nunca desliga a
barreira em silêncio.

Fonte `historical` (a fixture de REPLAY é de 2024-01-01): compará-la com o
relógio de parede mediria a idade do **arquivo**, não risco, e recusaria
100% dos sinais para sempre. Aí a política **não se aplica**, e a ausência
fica registrada explicitamente em `checks["signal_freshness"] =
{"applied": false, "reason": "market_data_is_historical_in_this_mode"}` —
nunca como aprovação silenciosa. Ver
`app/core/freshness.py::freshness_policy_for_market_data`.

*Alternativa considerada e descartada:* usar o tempo do próprio candle
como "agora" na fonte histórica. Ficaria elegante, mas o mesmo mecanismo
aplicado a uma fonte `live` tornaria o atraso SEMPRE zero durante uma
drenagem de backlog — exatamente o cenário que a barreira existe para
impedir.

### Gate temporal de ativação

`POST /api/operational-state/activate` passou a exigir, **por símbolo**:
saúde SAUDÁVEL, `has_gap=false`, zero falhas consecutivas, sem erro
impeditivo, aquecimento concluído e série no presente. É **atômico**: um
único símbolo bloqueador impede a carteira inteira de ficar ATIVA. A
resposta nomeia o símbolo, o atraso medido, o limite e o último candle.

O limite de atualidade é **exatamente**
`MAX_SIGNAL_DELAY_AFTER_CLOSE_SECONDS` (300 s com os padrões), decisão do
PO. A duração do bucket estratégico **não** é somada de novo: a idade já é
contada a partir do **fechamento** do candle, então somá-la contaria duas
vezes o mesmo intervalo.

Coerência exigida: se um sinal não pode virar entrada acima dessa janela,
o endpoint de ativação não pode declarar pronta uma carteira que já a
ultrapassou. Ativação e barreira de entrada falam a mesma língua — 300 s
dos dois lados. A margem sobre o backlog real da Fase 3.3 (185 min)
continua sendo de 37×.

A instância **nunca** restaura ATIVO automaticamente após boot, queda ou
reinício: `app/api/main.py` força OBSERVANDO em todo boot, e isso
continua valendo.

## 12.3 Instrumentação da cobertura e contagens (Fase 3.3.1)

`GET /api/metrics` passou a expor, global e por símbolo:

- `cost_gate.coverage_distribution`: avaliadas, aprovadas, rejeitadas,
  `min`, `p50`, `mean`, `p90`, `max`, e as faixas `below_1x`,
  `between_1x_2x`, `between_2x_3x`, `at_or_above_3x`;
- `signal_counts`: `signals_total`, `actionable_signals_total`,
  `hold_signals_total`, `by_direction`;
- `rejection_reasons`: contagem por check que falhou e o motivo dominante;
- `temporal_currency` por símbolo.

Regras: amostra vazia devolve `None` em todas as estatísticas, nunca
zero; cobertura nula (custo configurado igual a zero) é contada à parte
em `samples_without_ratio`, nunca tratada como zero; percentil
determinístico por *nearest-rank*, sem interpolação — o valor devolvido é
sempre um valor **observado**; nenhum arredondamento antes do cálculo.

As contagens vêm de consulta canônica ao banco (`repo.signal_counts`),
**nunca** de `/api/signals?limit=N` — foi assim que a auditoria da Fase
3.3 reportou 200 sinais quando eram 257.

## 12.4 O que NÃO mudou nesta fase

Registrado para não haver dúvida de que nenhum controle foi afrouxado
para produzir operação:

- timeframe estratégico continua **5 minutos** — e continua em avaliação;
- `minimum_cost_coverage_ratio` continua **3,0×**, não foi afrouxado;
- `strategy_expected_move_atr_multiple` continua **1× ATR**;
- taxas e slippage inalterados; **5 bps continuam sendo estimativa
  configurada, nunca medida** (não houve execução para medir);
- `risk_max_concurrent_positions` continua **1** — decisão conservadora
  inicial, herdada da Fase 1, ainda pendente de decisão do PO;
- estratégia continua SMA 9/21, stop 2× ATR, alvo 3× ATR.

## 13. Limitações conhecidas

- Menos decisões: em 5m a estratégia avalia 1/5 das vezes. É o objetivo,
  mas muda o perfil da operação.
- Aquecimento longo após qualquer mudança de configuração (105 min em 5m;
  5h15 em 15m).
- O agregado reconstruído pode divergir do "visto na hora" se houver
  backfill posterior (mitigado pelo OHLCV gravado em `params_json`).
- O slippage do gate é **estimado por configuração**, não medido. Um gate
  calibrado pelo slippage realizado histórico é evolução futura.
- Não há backfill tardio de bucket incompleto nesta fase.
- A fixture histórica de REPLAY (180 candles) não completa o aquecimento
  em 15m -- 15m é testado pela cadência, não por sinal acionável.
- `repo.cost_gate_stats` percorre e desserializa `checks_json` em Python
  (sem índice/coluna dedicada). Adequado à escala deste sistema; um
  contador materializado seria evolução futura, não desta fase.
- `_check_stop_take` mantém a premissa conservadora de que, se stop e
  alvo forem tocados no mesmo candle, o stop veio primeiro -- agora
  avaliada em 1 minuto, o que é mais preciso do que seria em 5.
