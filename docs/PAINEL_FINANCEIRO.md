# Painel Financeiro (Fase 3.1.1)

Correção funcional e semântica do painel: antes desta fase, o painel exibia
"Saldo inicial (demo): 1000.00" como uma **string fixa no HTML**, nunca
ligada a nenhuma chamada de API, e o card "Custos e Slippage" apresentava
uma **diferença unitária de preço** (US$ por unidade do ativo) rotulada
como se fosse dinheiro. Esta fase corrigiu as fórmulas primeiro, a
interface depois -- e uma rodada final de auditoria do PO estabeleceu a
semântica contábil definitiva descrita abaixo.

## Decisão definitiva do PO: equity não tem escopo

Patrimônio (`portfolio.equity`) é o estado **atual** da carteira inteira.
Não existe "equity diária" nem "equity da sessão" calculada removendo
seletivamente eventos anteriores -- consultar `GET /api/portfolio-summary`
com `scope=lifetime`, `session` ou `daily` retorna **sempre o mesmo**
bloco `portfolio`. Só o bloco separado `period_performance` varia com o
`scope` pedido -- é um recorte de **desempenho**, nunca de patrimônio.

## Sessão operacional vs. base contábil (último gate contábil)

**"Lifetime" nunca significa "todo o histórico do banco"** -- significa
"desde o início da BASE CONTÁBIL atual". Duas coisas distintas, fáceis de
confundir:

- **Sessão operacional** (`OperationalSession`): nasce a cada mudança de
  fingerprint de configuração -- estratégia, limites de risco, símbolos,
  ... (`app.sessions.start_or_resume_session`). Isso **nunca** reseta
  patrimônio.
- **Base contábil**: a âncora de capital atualmente em vigor. Só nasce
  quando `paper_starting_balance_usd` **especificamente** muda -- a única
  mudança de config tratada como reset de capital (e só permitida sem
  posições abertas, ver `_guard_starting_balance_reset` acima). Uma base
  pode abranger **várias** sessões operacionais consecutivas: trocar a
  estratégia no meio do caminho nunca abre uma base nova.

`app.sessions.resolve_accounting_base(session, active_session)` computa o
início da base **sem nenhuma coluna nova, sem migration**: caminha para
trás pela cadeia de sessões consecutivas (mesmo `mode`+`symbols`,
ordenadas por `started_at`) a partir da sessão ativa, comparando o saldo
inicial CONGELADO (`resolve_starting_balance`) de cada uma -- para no
primeiro valor diferente (a fronteira real do reset); a base começa na
sessão logo após essa fronteira, ou na primeiríssima sessão do portfólio
se nunca houve reset. O campo `started_at` de cada `OperationalSession`
já é suficiente como marco -- comprovado em
`tests/test_accounting_base_reset.py::test_resolve_accounting_base_finds_the_reset_boundary`,
sem exigir nenhuma alteração de schema.

**TODOS** os componentes variáveis de `portfolio` (`realized_price_pnl`,
`fees_paid`, `funding_*`) e o corte de `period_performance` (em qualquer
`scope`, inclusive `lifetime`) são limitados por `base_started_at` --
nunca alcançam dados de uma base anterior a um reset. O boundary é
exposto explicitamente em `portfolio.accounting_base_started_at` para
qualquer auditoria futura.

### Exemplo obrigatório da auditoria do PO

```
Sessão/base A: saldo inicial US$ 1.000, resultado líquido +US$ 100
  -> equity da base A = US$ 1.100

paper_starting_balance_usd muda para US$ 2.000 (sem posição aberta -- reset aceito)
Sessão/base B (nova): saldo inicial US$ 2.000
  -> primeira equity da base B = EXATAMENTE US$ 2.000 (nunca US$ 2.100)

O trade de +US$ 100 da base A CONTINUA no banco (repo.closed_positions()
sem filtro ainda o retorna), mas NUNCA compõe a equity da base B.

Novo trade de +US$ 50 na base B -> equity da base B = US$ 2.050.
```

Provado ponta a ponta em
`tests/test_accounting_base_reset.py::test_full_reset_walkthrough_1000_plus_100_reset_2000`
(sessões/bases reais construídas via `app.api.main.build_orchestrator`,
mesmo caminho de produção -- nunca um atalho de teste).

### O histórico de uma base anterior nunca é apagado nem reescrito

`repo.closed_positions()`/`repo.execution_fees()`/etc. sem filtro de
tempo continuam retornando os dados de QUALQUER base anterior --
consultáveis por quem tiver acesso direto ao banco/repo. O que muda é
apenas que **nenhuma resposta de `/api/portfolio-summary` ou
`/api/equity-curve`** soma essas linhas de novo à base atual. Não existe,
nesta fase, um endpoint dedicado para navegar bases anteriores pela API
(ex.: "mostre a equity final da base A") -- isso é uma extensão natural
para uma fase futura, não implementada aqui por não ter sido pedida
explicitamente e para evitar escopo além do necessário.

## Fonte única do saldo inicial (congelado por sessão)

`Settings.paper_starting_balance_usd` (env `PAPER_STARTING_BALANCE_USD`,
default `1000.0`, validado como finito e maior que zero) é o campo de
configuração. Mas o valor **efetivamente usado no cálculo nunca é lido ao
vivo desse campo** -- é lido do `config_snapshot_json` **congelado** na
sessão operacional ativa no momento em que ela foi criada
(`app.sessions.resolve_starting_balance`, chamada por
`GET /api/portfolio-summary` e `GET /api/equity-curve` -- a MESMA
resolução canônica em ambas, nunca duas implementações divergentes):

```python
def resolve_starting_balance(op_session) -> tuple[float, str]:
    # None -> "legacy_fallback_no_session"
    # snapshot sem a chave -> "legacy_fallback_missing_field"
    # snapshot corrompido -> "legacy_fallback_invalid_snapshot"
    # caso normal -> (valor real, "session_snapshot")
```

`starting_balance_source` é exposto na API para que a interface (e
qualquer auditoria) saiba exatamente qual caso se aplica. Sessões legadas
(criadas antes deste campo existir) usam o fallback explícito
`LEGACY_STARTING_BALANCE_FALLBACK_USD = 1000.0` -- nunca reescritas, nunca
reinterpretadas sob o `Settings` atual do processo.

Isso substitui três hardcodes históricos independentes encontrados nesta
fase: o literal `1000.0` do antigo `_metrics_for_trades` (backend), o
literal `"1000.00"` do `frontend/app.js`, e um terceiro em
`GET /api/equity-curve`.

### Mudar `paper_starting_balance_usd` gera uma sessão nova -- mas nunca reinterpreta o passado

O valor entra no `config_snapshot_json`/fingerprint da sessão operacional
(`app.sessions._sanitized_config_snapshot`) -- alterá-lo é uma mudança de
configuração como qualquer outra que afeta o resultado financeiro
simulado, e produz uma sessão nova (`start_or_resume_session`). A sessão
antiga nunca é reescrita; seu `config_snapshot_json` continua provando
qual saldo ela realmente usou.

### Reset seguro: mudar o saldo com posição aberta é recusado

Este sistema **não possui um ledger de depósito/retirada de capital**.
Mudar `paper_starting_balance_usd` só é uma operação segura de "redefinir
a carteira simulada para uma nova âncora" quando **não há nenhuma posição
aberta** que seria reinterpretada silenciosamente sob o novo capital (uma
posição de US$500 aberta sob uma âncora de US$1.000 não deve de repente
"encolher" relativa a uma nova âncora de US$5.000, sem nenhuma explicação
contábil).

`app.sessions._guard_starting_balance_reset` compara o saldo CONGELADO da
sessão anterior (nunca um valor recalculado) com o novo `Settings`; se
diferirem E existir qualquer posição `OPEN`, levanta
`StartingBalanceResetBlockedError` -- interrompe a **inicialização do
processo** (mesma política de `UnsafeBindHostError`/`MigrationError` para
condições de início inseguras: recusar começar, nunca iniciar numa base
financeira ambígua). O operador precisa fechar as posições abertas ou
reverter a configuração antes de reiniciar.

Sem posições abertas, a troca é aceita normalmente: a nova sessão usa o
novo saldo como nova âncora; o histórico realizado/taxas/funding
anteriores continua consultável (nunca apagado), mas **nunca somado de
novo** à nova âncora -- `portfolio.equity` sempre usa o `starting_balance`
congelado da sessão **ativa no momento da consulta**, um único valor, uma
única vez.

## Fórmula do patrimônio atual (`portfolio`)

```
current_equity =
    frozen_starting_balance
    + lifetime_realized_price_pnl
    + current_unrealized_price_pnl
    - lifetime_execution_fees
    + lifetime_funding_net
```

Implementada em `app/metrics/engine.py::compute_equity` (função pura,
testada isoladamente em `tests/test_equity_metrics.py`) e servida por
`GET /api/portfolio-summary` (`app/api/routes_dashboard.py::_portfolio_state`).
`frozen_starting_balance` é sempre o saldo congelado da sessão que
estabeleceu a BASE CONTÁBIL atual (não necessariamente a sessão ativa --
ver seção acima); `lifetime_*` significa "desde `base_started_at`", nunca
"desde o início dos tempos" -- os quatro componentes variáveis são
filtrados por `since=base_started_at` (`app.sessions.resolve_accounting_base`),
nunca por `?scope=` (isso é exclusivo de `period_performance`).

### Por que taxas/P&L de posições ABERTAS nunca ficam invisíveis

`Position.realized_pnl` é um campo **cumulativo** por posição, atualizado
em tempo real a cada fechamento parcial (`repo.reduce_position`) e no
fechamento final (`repo.close_position`) -- ver
`app/execution/fill_service.py`. Uma posição ainda **aberta** pode já ter
realizado P&L de fechamentos parciais anteriores, mesmo continuando
aberta com o restante da quantidade -- `_portfolio_state` soma isso
integralmente (nunca filtrado por tempo), ao contrário do antigo
`compute_metrics`/`net_profit`, que só olhava posições **totalmente
fechadas**.

### Fonte canônica de taxas -- `repo.execution_fees`

`Position.fees_paid` só é incrementado quando um fill é efetivamente
**aplicado** a uma posição -- dois casos deixam uma taxa genuinamente
incorrida de fora:

1. **`LATE_OPPOSITE_FILL_BLOCKED`**: um fill de entrada do lado oposto ao
   da posição já aberta é bloqueado por segurança -- a taxa desse fill
   nunca chega a `Position.fees_paid`.
2. **Fill de fechamento sem posição local** (`position is None` em
   `app/execution/fill_service.py`): nada local para reduzir/fechar -- a
   taxa também fica de fora de qualquer `Position`.

Em ambos os casos a taxa é **real** e precisa reduzir a equity. Fonte
definitiva: `repo.execution_fees(session, symbol, since)` -- soma
`Execution.fee` diretamente, uma linha por fill **realmente ocorrido**
(`UniqueConstraint(order_id, exchange_fill_id)` torna duplicação
estruturalmente impossível; uma ordem rejeitada ou nunca preenchida nunca
ganha uma linha `Execution`). `Execution.fee` nunca omite (inclui os dois
casos acima) e nunca duplica (nunca somada junto com `Position.fees_paid`
nem com `Order.fees_total`, que deriva do MESMO conjunto de linhas
`Execution` -- somar as duas duplicaria toda taxa normal, exatamente o
erro que o PO alertou para nunca cometer:
`fees = Position.fees_paid + Order.fees_total`). `Position.fees_paid`
continua existindo para o que sempre foi usado (auditoria por posição
individual), mas não é mais a fonte de `fees_paid` na API.

### Prova de ausência de dupla contagem

| Componente | Fonte persistida | Momento de reconhecimento | Escopo aplicado em `portfolio` | Como evita dupla contagem |
|---|---|---|---|---|
| Realized price P&L | `Position.realized_pnl` (abertas + fechadas) | A cada fill aplicado, `fill_service.py` | `since=base_started_at` (abertas nunca filtradas) | Lido uma vez por posição; nunca rederivado de `Order`/`Execution` |
| Unrealized price P&L | Calculado sob demanda, nunca persistido | No instante da requisição | Nenhum -- sempre "agora" (posições abertas nunca predatam a base -- reset exige zero posições abertas) | Toca só a quantidade AINDA aberta; nunca sobrepõe o realizado |
| Fees | `Execution.fee` (canônico) | No instante do fill (`executed_at`) | `since=base_started_at` | Fonte única; nunca somada com `Position.fees_paid`/`Order.fees_total` |
| Funding | `FundingEvent.amount` | No settlement (`occurred_at`) | `since=base_started_at` | Tabela única, sem agregado derivado paralelo |
| Slippage (atributivo) | `Order.avg_fill_price` vs `reference_price` × `filled_qty` | No fill da ordem | N/A -- nunca entra em `portfolio` | Nunca subtraído da equity -- já embutido em `avg_fill_price` |
| Starting balance | Snapshot congelado da sessão que estabeleceu a base | Uma vez, como âncora | N/A -- idêntico em qualquer escopo, muda SÓ com um reset real | Lido fresco do snapshot a cada requisição, nunca acumulado por sessão nem por base |

O slippage **já está embutido** no `avg_fill_price` usado para calcular
`realized_pnl` -- por isso nunca é subtraído de novo em `compute_equity`.
O card de custos é puramente diagnóstico/atributivo.

## `period_performance`: recorte de desempenho, nunca de patrimônio

`GET /api/portfolio-summary?scope=lifetime|session|daily` -- o bloco
`period_performance` é a ÚNICA parte da resposta que varia com `scope`.
**Nunca inclui `starting_balance` nem `equity`** -- chamar
`starting_balance + componentes_filtrados` de "equity" seria uma equity
fictícia, removendo custos antigos de forma enganosa.

- **`lifetime`**: desde o início da BASE CONTÁBIL atual
  (`base_started_at`) -- NUNCA "todo o histórico do banco" quando já
  existiu um reset. Sem nenhuma sessão ativa ainda, sem corte algum.
- **`session`**: filtrado por `>= sessão_ativa.started_at`; sem sessão
  ativa, cai para o mesmo comportamento de `lifetime`.
- **`daily`**: filtrado por `>= 00:00 UTC do dia corrente` -- UTC real,
  nunca aproximado pelo fuso horário local do processo.

Em TODOS os três casos, o corte final é sempre
`max(corte_calculado, base_started_at)` (`app/api/routes_dashboard.py::_scope_since`)
-- nenhum escopo, nem mesmo um `session`/`daily` recente, pode por engano
alcançar dados de antes de um reset que tenha acontecido no meio da
janela (ex.: reset ocorrido hoje às 14h -- `scope=daily` nunca mostra
dados de antes das 14h, mesmo que "hoje 00h" seja tecnicamente mais
cedo).

Campos: `scope`, `since`, `realized_price_pnl`, `fees_paid`,
`funding_paid`, `funding_received`, `funding_net`, `realized_net_pnl`,
`fills_count`, `closed_trades_count`, `realized_pnl_attribution`.

### Semântica temporal de cada componente do período

- **Fees**: filtradas por `Execution.executed_at >= since` -- o instante
  real do fill.
- **Funding**: filtrado por `FundingEvent.occurred_at >= since` -- o
  instante real do settlement.
- **Realized price P&L**: **auditado explicitamente** -- não existe, em
  lugar nenhum do schema, um ledger de P&L por fill individual
  (`Execution` grava `fill_qty`/`fill_price`/`fee`/`executed_at`, mas
  NENHUM delta de P&L por fill). O único dado disponível é
  `Position.realized_pnl`, atribuído inteiro ao instante de fechamento
  (`Position.closed_at`) da posição. Por isso `period_performance.realized_price_pnl`
  soma **exclusivamente posições FECHADAS cujo `closed_at` cai no
  recorte** -- nunca alega representar um fluxo incremental por fill. A
  flag `realized_pnl_attribution="position_close"` é exposta
  explicitamente na API para que isso nunca seja mal-interpretado.
  Nenhuma migration foi criada para reconstruir retroativamente uma
  granularidade que o histórico atual não prova; uma ledger de P&L por
  fill é uma evolução possível de uma fase futura, não desta.

### Por que uma posição aberta nunca "vaza" P&L histórico para um recorte novo

Uma posição com um fechamento **parcial** anterior, mas ainda `OPEN`,
nunca aparece em `repo.closed_positions()` -- então seu `realized_pnl`
acumulado (do fechamento parcial) nunca é somado a nenhum
`period_performance`, em nenhum escopo, enquanto ela permanecer aberta.
Esse P&L só entra em `period_performance` no dia/sessão em que a posição
for **finalmente fechada** (quando `Position.status` vira `CLOSED` e
`closed_at` é gravado) -- e nesse momento entra inteiro, pela mesma regra
de atribuição por fechamento. Enquanto isso, `portfolio.realized_price_pnl`
(lifetime, sempre) já reflete esse P&L parcial desde o instante em que
ele foi realizado, porque posições abertas nunca são filtradas por tempo
ali.

### Cenário com ambiguidade documentada (decisão pendente do PO)

Uma posição cuja taxa de ENTRADA ocorreu antes do recorte mas cujo
fechamento (e taxa de SAÍDA) caiu dentro dele: `period_performance`
mostra o `realized_price_pnl` **completo** do trade (atribuído ao
fechamento), mas só a taxa de **saída** em `fees_paid` (cortada por
`executed_at`) -- o resultado do período pode parecer melhor do que o
trade realmente foi. Isso é diferente de `portfolio.equity`, que nunca
tem esse problema (soma tudo, sempre). Duas alternativas propostas para
uma correção futura, sem resolver silenciosamente agora: (a) atribuir
taxas por TRADE (ambas as pernas seguem `closed_at`, como o P&L); ou (b)
rotular visualmente que os custos do período podem estar parcialmente
fora da janela. Aguardando decisão do PO.

## Marcação a mercado (mark-to-market) de posições abertas

```
LONG:  unrealized_pnl = (mark_price - avg_entry_price) * qty
SHORT: unrealized_pnl = (avg_entry_price - mark_price) * qty
```

Implementada em `compute_unrealized_pnl` (`app/metrics/engine.py`). Fonte
do `mark_price`, com a mesma prioridade honesta usada pelo painel gráfico
(`_resolve_mark_price`, compartilhada entre `/api/chart-data` e
`/api/portfolio-summary` -- uma única implementação, nunca duas
divergentes):

1. `visual_price_state[symbol]` -- candle em formação (`forming_candle`),
   quando o provider expõe um (nunca REPLAY/PAPER_LOCAL);
2. fechamento do último candle persistido (`last_closed_candle`);
3. nenhum preço disponível → a posição é **excluída** da soma total (nunca
   tratada como P&L zero, nunca usa `avg_entry_price` como se fosse o
   preço atual) e `equity_complete` vira `false` -- a interface mostra um
   aviso explícito de patrimônio incompleto em vez de uma precisão falsa.

Cada posição usa exclusivamente seu **próprio** símbolo/mark -- nunca o
preço de outro ativo contamina o cálculo.

## Slippage -- contrato definitivo

Contrato antigo (**removido**, nunca reaproveitado com o mesmo nome):
`slippage_avg_usd`/`slippage_total_usd` eram a diferença **unitária** de
preço (US$ por unidade do ativo), somada/mediada diretamente -- nunca
multiplicada pela quantidade executada, e por isso não representava
dinheiro real nem era comparável entre símbolos de escalas diferentes.

Contrato novo (`app/metrics/engine.py::compute_cost_metrics`):

```
diferença unitária (BUY):  fill_price - reference_price
diferença unitária (SELL): reference_price - fill_price
impacto financeiro assinado = diferença_unitária * filled_qty

adverse_slippage_cost_usd   = soma dos impactos > 0
price_improvement_value_usd = soma de abs(impactos < 0)
net_slippage_impact_usd     = adverse_slippage_cost_usd - price_improvement_value_usd

reference_notional_total_usd = soma(reference_price * filled_qty)
weighted_slippage_pct = net_slippage_impact_usd / reference_notional_total_usd * 100
adverse_slippage_pct  = adverse_slippage_cost_usd / reference_notional_total_usd * 100
```

Adverso e melhoria nunca se cancelam silenciosamente; o percentual é
ponderado pelo notional de referência, nunca a média simples dos
percentuais individuais. `GET /api/costs?symbol=X` filtra por símbolo --
o consolidado nunca mistura notional de ativos diferentes.

### Slippage já está no P&L -- nunca descontado duas vezes

O `avg_fill_price` (que já incorpora o slippage simulado) é o preço
realmente usado para calcular `Position.realized_pnl`. O card "Impacto
dos Custos de Negociação" é **diagnóstico de atribuição**, nunca uma
segunda dedução -- aviso fixo na tela: *"Taxas e slippage já estão
refletidos no resultado líquido e não são descontados novamente."*
Funding recebido é um **crédito**, nunca apresentado como custo negativo.

### `/api/costs` pertence à base contábil ativa

Os custos exibidos são **sempre e apenas os da base contábil ativa**,
resolvida exatamente como em `/api/portfolio-summary`
(`repo.get_active_session` -> `app.sessions.resolve_accounting_base`).
Nunca somam taxas ou slippage de uma base anterior a um reset de
`paper_starting_balance_usd` junto com o patrimônio e o P&L da base
atual -- seriam números de universos financeiros diferentes na mesma
tela, comparados como se fossem do mesmo. A resposta expõe
`accounting_base_started_at` para que isso seja auditável na própria
carga, e `?symbol=` continua filtrando dentro da base.

**Marco temporal usado**: `Execution.executed_at` -- o instante do FILL
real, nunca `Order.created_at`/`updated_at`. Uma ordem criada antes do
reset mas executada depois entra; uma ordem criada antes e executada
antes fica fora; uma ordem sem nenhum fill fica fora dos dois lados
(nunca uma entrada fantasma com notional inventado).

**Ordem com fills dos dois lados da fronteira**: entra apenas com os
fills POSTERIORES. A fonte é `repo.orders_with_executions_since`, que
devolve `(Order, [Execution, ...])` já recortado, e o endpoint
**recalcula** `avg_fill_price`/`filled_qty`/`fees_total` a partir só
dessas linhas:

```
filled_qty     = soma(e.fill_qty                 para e nos fills da base)
avg_fill_price = soma(e.fill_qty * e.fill_price) / filled_qty
fees_total     = soma(e.fee)
```

`Order.avg_fill_price`/`Order.filled_qty` (agregados por TODOS os fills
da ordem, de qualquer época) **nunca** são reaproveitados aqui --
exatamente para que um preço médio contaminado por fills de uma base
anterior não se disfarce de custo da base atual. Provado em
`tests/test_accounting_base_reset.py::test_order_with_fills_on_both_sides_of_the_boundary_uses_only_the_later_fills`.

## Drawdown atual vs. máximo

`current_drawdown_money`/`current_drawdown_pct` é a distância do
**último ponto** da curva de equity REALIZADA até o pico anterior --
`max_drawdown_money`/`max_drawdown_pct` é o pior já visto na curva
inteira. Ambos vêm de `app/metrics/engine.py::compute_metrics` (curva
apenas REALIZADA -- nunca incorpora P&L não realizado ao vivo; "atual"
aqui significa "no fechamento do último trade", não "neste exato
instante", que é sempre `portfolio.equity`).

## `account_snapshots` -- permanece reservada

A tabela `account_snapshots` já existia desde a fundação do schema, mas
nunca foi preenchida por nenhum código. Decisão desta fase: **continua
não sendo usada**. O patrimônio é calculado sob demanda a cada requisição,
sem persistir nenhuma linha nova -- nenhuma política de retenção, nenhum
backfill, nenhuma migration. Reservada para uma fase futura de série
histórica real de patrimônio.

## Contrato da API -- `GET /api/portfolio-summary`

```json
{
  "portfolio": {
    "accounting_base_started_at": "2026-03-01T00:00:00+00:00",
    "starting_balance": 1000.0,
    "starting_balance_source": "session_snapshot",
    "realized_price_pnl": 10.0,
    "unrealized_pnl": 5.0,
    "fees_paid": 0.5,
    "funding_paid": "indisponível",
    "funding_received": "indisponível",
    "funding_net": "indisponível",
    "realized_net_pnl": 9.5,
    "equity": 1014.5,
    "equity_complete": true,
    "open_positions_count": 1,
    "exposure_usd": 400.0
  },
  "period_performance": {
    "scope": "daily",
    "since": "2026-03-15T00:00:00+00:00",
    "realized_price_pnl": 0.0,
    "fees_paid": 0.0,
    "funding_paid": "indisponível",
    "funding_received": "indisponível",
    "funding_net": "indisponível",
    "realized_net_pnl": 0.0,
    "fills_count": 0,
    "closed_trades_count": 0,
    "realized_pnl_attribution": "position_close"
  },
  "per_symbol": {
    "BTCUSDT": {
      "realized_price_pnl": 10.0, "unrealized_pnl": 5.0, "unrealized_complete": true,
      "fees_paid": 0.5, "funding_paid": "indisponível", "funding_received": "indisponível",
      "funding_net": "indisponível", "exposure_usd": 400.0, "open_positions_count": 1,
      "positions": ["..."]
    }
  }
}
```

`per_symbol` espelha `portfolio` (sempre lifetime, mesmos componentes) --
**nunca** inclui `starting_balance`/`equity` (capital não é fatiado por
símbolo). `GET /api/costs?symbol=X` (opcional): mesmo contrato de
`compute_cost_metrics`, servido separadamente, mais o campo
`accounting_base_started_at` -- sempre idêntico ao de `portfolio`, porque
as duas telas falam obrigatoriamente da MESMA base contábil.

## Limitações conhecidas

- `daily`/`session` (em `period_performance`) não reconstroem um
  "patrimônio no início da janela" real -- não existe conceito de
  patrimônio por período nesta fase (decisão do PO: equity não tem
  escopo).
- **Assimetria pendente de decisão do PO**: ver "Cenário com ambiguidade
  documentada" acima -- taxa de entrada anterior ao recorte não aparece em
  `period_performance`, mesmo com o P&L completo do trade presente.
- `current_drawdown`/`max_drawdown`/`return_over_drawdown`
  (`GET /api/metrics`) permanecem baseados apenas na curva REALIZADA --
  nunca incorporam P&L não realizado ao vivo.
- Cada degrau de `GET /api/equity-curve` usa `Position.fees_paid` (não a
  fonte canônica `Execution.fee`) -- uma taxa órfã não move essa curva; é
  uma visualização aproximada, não o patrimônio oficial (esse é sempre
  `GET /api/portfolio-summary`).
- Não existe UI de seletor de escopo para `period_performance` nesta
  fase -- a tela principal usa `scope=lifetime` (equivalente a "desde o
  início da base contábil atual"), com `portfolio` sempre em destaque;
  um seletor dedicado para "Desempenho do período" é uma extensão de
  interface natural para uma fase futura.
- Não existe endpoint dedicado para consultar bases contábeis anteriores
  (ex.: "qual foi a equity final da base A antes do reset?") -- os dados
  permanecem no banco, consultáveis por quem tiver acesso direto ao
  repo, mas não há uma rota HTTP para isso nesta fase. Extensão natural
  para uma fase futura, não implementada por não ter sido pedida
  explicitamente.
- Não existe `?scope=all` em `/api/costs` para auditar custos de bases
  anteriores numa única chamada -- os dados permanecem no banco e
  `repo.orders_with_executions_since(session)` (sem `since=`) devolve o
  histórico completo, mas não há rota HTTP para isso nesta fase (não foi
  pedido; ver "`/api/costs` pertence à base contábil ativa" acima).
