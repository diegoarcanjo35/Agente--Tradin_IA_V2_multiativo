# Sessões Operacionais

Fase 2, item 7.7 — `app/sessions.py`, tabela `operational_sessions`. Fase 3
multiativo: a sessão passa a representar **a carteira inteira** (todos os
símbolos configurados), não mais um único símbolo — ver seção dedicada
abaixo.

## Ciclo de vida

`app/api/main.py::build_orchestrator()` chama
`start_or_resume_session(session, settings, strategy_version, risk_limits,
strategy_config)` logo na inicialização, **antes** da reconciliação de
startup (para que essa primeira reconciliação já seja contabilizada nos
contadores da sessão):

- Se já existe uma sessão **não encerrada** (`ended_at IS NULL`) para o
  mesmo `mode` + a mesma **lista ordenada de símbolos** (`symbols`, ver
  abaixo) **e** cujo `config_fingerprint` bate exatamente com o fingerprint
  atual (correção v1.1 #8, ver abaixo), ela é **retomada** — nenhuma linha
  nova é criada. Um reinício de processo com a mesma configuração nunca
  gera uma segunda sessão para o mesmo contexto.
- Se existe uma sessão não encerrada para o mesmo `mode`+`symbols` mas com
  um fingerprint **diferente** (ou sem fingerprint algum — uma linha
  legada, tratada como divergente por padrão, nunca confiada
  implicitamente), essa sessão antiga é encerrada
  (`end_session(..., "Configuração operacional alterada; sessão
  substituída.")`) e uma nova é criada **na mesma transação/sessão
  SQLAlchemy** — nunca em dois commits separados, e nunca deixando a sessão
  anterior ativa para sempre (correção obrigatória do PO, item 4). O
  `flush()` de `end_session` materializa `ended_at` antes do `INSERT`
  seguinte, para que o índice único parcial de sessão ativa por carteira
  (ver abaixo) nunca veja as duas linhas simultaneamente elegíveis.
- Caso contrário (nenhuma sessão anterior), cria uma nova sessão:
  `session_uid` (UUID), `mode`, `symbol` (legado, ver abaixo), `symbols`,
  `timeframe`, `strategy_version`, um snapshot da configuração de risco
  (`risk_config_json`, via `dataclasses.asdict(RiskLimits)`) e um
  **snapshot sanitizado** da configuração geral (`config_snapshot_json`) —
  `_sanitized_config_snapshot()` usa uma **lista de permissão explícita** de
  campos (nunca uma lista de bloqueio), então nenhum campo novo adicionado a
  `Settings` no futuro vaza para o snapshot por acidente;
  `bybit_api_key`/`bybit_api_secret` nunca são incluídos.

## Multiativo (Fase 3): `symbols` vs. `symbol` legado

Migration v7 adiciona `operational_sessions.symbols` (JSON nullable, lista
ordenada e canônica de símbolos, **na ordem de configuração** — não
reordenada alfabeticamente) e relaxa `symbol` de `NOT NULL` para nullable:

- **Sessões novas monoativo** (exatamente 1 símbolo): `symbol` continua
  preenchido com esse único símbolo (100% de compatibilidade de leitura para
  qualquer consumidor que ainda só conhece `symbol`) **e** `symbols` também
  é preenchido (`["BTCUSDT"]`).
- **Sessões novas multiativo** (mais de 1 símbolo): `symbol` fica `NULL` — um
  escalar não pode representar N símbolos sem mentir — e `symbols` carrega a
  lista completa.
- **Linhas pré-migration v7 (legado)**: `symbols` fica `NULL` para sempre —
  nunca reescrito/retroativo. Essas linhas são histórico somente-leitura;
  `start_or_resume_session` nunca as considera candidatas a retomada (o
  critério de busca passou a ser `symbols`, não mais `symbol`).
- **Leitura via API** (`GET /api/session`, correção obrigatória do PO, item
  5): a resposta sempre inclui `symbols_origin` — `"nativo"` quando
  `symbols` está preenchido no banco (qualquer sessão criada a partir da
  v7, mono ou multiativo), `"legado_derivado"` quando a linha é pré-v7
  (`symbols IS NULL` no banco) e a API deriva `symbols: [symbol]` só para
  a resposta, em memória — a linha em si nunca é reescrita.

### Integridade no banco (correção obrigatória do PO, item 3)

`CHECK (symbol IS NOT NULL OR symbols IS NOT NULL)` na tabela
`operational_sessions` (migration v7, `app/persistence/models.py` +
`app/persistence/migrations.py::_migrate_to_v7`) — nenhuma linha pode ter
os dois campos nulos simultaneamente. Sessões legadas (`symbol NOT NULL`,
`symbols NULL`) e sessões novas monoativo ou multiativo (`symbol
NULL`/`symbols NOT NULL`, ou ambos preenchidos no caso monoativo)
satisfazem o CHECK; só a combinação "ambos nulos" é rejeitada.

### Uma única sessão ativa por carteira (correção obrigatória do PO, item 4)

Índice único parcial `uq_operational_session_active_per_portfolio` em
`operational_sessions(mode, symbols) WHERE ended_at IS NULL AND symbols IS
NOT NULL` — no máximo uma sessão ATIVA por `(mode, symbols)`. Linhas legadas
(`symbols IS NULL`) ficam inteiramente fora dessa restrição, então histórico
pré-v7 nunca pode violá-la. Antes de criar esse índice, a migração v7
verifica se já existem sessões ativas duplicadas para a mesma carteira — se
existirem, a migração inteira é recusada (nada é apagado ou corrigido em
silêncio; ver `docs/MIGRACOES.md`).

### Fingerprint de configuração (correção v1.1 #8; Fase 3 multiativo,
incl. rodada de correção obrigatória do PO)

`app.sessions._config_fingerprint(settings, strategy_version, risk_limits,
strategy_config)` calcula um SHA-256 sobre
`json.dumps(sort_keys=True, separators=(",", ":"))` de `{mode, symbols,
timeframe, strategy_version, strategy_config: asdict(strategy_config),
risk_config: asdict(risk_limits), config_snapshot:
_sanitized_config_snapshot(settings)}`:

- `strategy_config` é o **objeto `StrategyConfig` realmente entregue** a
  cada `StrategyEngine` (construído uma única vez em
  `build_orchestrator()` e compartilhado por valor entre todos os
  símbolos) — `dataclasses.asdict()` direto sobre ele, nunca uma lista de
  campos duplicada à mão que pudesse divergir do objeto de verdade. Inclui
  `fast_period`, `slow_period`, `atr_period`, `min_atr_pct_of_price`,
  `max_atr_pct_of_price`, `stop_loss_atr_multiple`,
  `take_profit_atr_multiple`.
- `config_snapshot` (`_sanitized_config_snapshot`, allowlist explícita)
  inclui, além dos campos de risco/reconciliação/AI Shadow já existentes:
  `partial_fill_policy`, `partial_fill_timeout_seconds`,
  `paper_live_fee_rate`, `paper_live_slippage_bps` (alteram execução/
  resultado financeiro diretamente), e `market_data_initial_start` **só**
  quando `mode` é `PAPER_LIVE`/`BYBIT_DEMO` (único caso em que esse campo é
  efetivamente lido por um provider — `ReplayMarketDataProvider` o ignora).
  Deliberadamente **fora**: intervalos de polling/heartbeat, porta HTTP,
  caminhos locais, credenciais/segredos — puramente agendamento/
  observabilidade, nunca decisão/execução/resultado financeiro.

`symbols` entra na **ordem de configuração** (não ordenado alfabeticamente
aqui) — trocar a ordem declarada de `SYMBOLS` produz um fingerprint
diferente, a mesma identidade usada pelo round-robin do scheduler
(`docs/ARQUITETURA.md`, seção "Multiativo"). Uma mudança em qualquer um
desses campos (símbolos, ordem dos símbolos, versão de estratégia,
configuração da estratégia, limites de risco, timeframe, ou qualquer campo
do snapshot sanitizado) produz um fingerprint diferente e força uma sessão
nova em vez de uma retomada silenciosa sob configuração desatualizada.
`TIMEFRAME = "1"` permanece uma constante fixa nesta fundação (não há campo
de configuração de timeframe hoje) — o campo continua explícito no
fingerprint para preparar uma evolução futura, mas é estruturalmente
inoperante enquanto só existir um timeframe suportado.

`SystemState.active_session_id` sempre aponta para a sessão em uso; o
estado da sessão (`OperationalSession.status`) é mantido em sincronia com
`SystemState.operational_state` em todo endpoint que muda esse último
(`/kill-switch/engage`, `/operational-state/activate`,
`/operational-state/pause`, e `recompute_trading_blocked` quando entra ou
sai de `BLOQUEADO`).

`end_session(session, op_session, reason)` marca `ended_at`, `end_reason` e
`status="ENCERRANDO"`. Desde a correção v1.1 #7, é chamado de fato pelo
desligamento gracioso do processo — ver "Desligamento gracioso" abaixo.

## Contadores

Incrementados exatamente no ponto em que cada evento já é sabido ter
acontecido dentro de `Orchestrator.tick()`/`reconcile()` — nunca
recalculados por uma query separada, então não há risco de dupla contagem:

| Contador | Incrementado em |
|---|---|
| `candles_count` | candle novo persistido com sucesso |
| `signals_count` | sinal da estratégia salvo |
| `approvals_count` / `rejections_count` | resultado de `RiskEngine.evaluate()` |
| `orders_count` | `repo.save_order` (abertura ou fechamento) |
| `fills_count` | fill FILLED/PARTIALLY_FILLED registrado |
| `failures_count` | `repo.record_failure(..., "FAILURE", ...)` |
| `reconciliations_count` | toda chamada a `Orchestrator.reconcile()` |

`app.sessions.increment(op_session, field)` é um no-op seguro quando
`op_session is None` (nenhuma sessão ativa ainda) — usado por testes que
constroem um `Orchestrator` diretamente, sem passar por
`build_orchestrator()`.

## Gates de ativação de sessão

Uma sessão só pode chegar a `operational_state="ATIVO"` (via
`POST /api/operational-state/activate`) depois de:

1. ambiente validado (host allowlist — já garantido na construção do
   `Settings`, antes de qualquer wiring);
2. credenciais validadas sem exposição (BYBIT_DEMO: `require_bybit_credentials()`
   já rodou; REPLAY/PAPER_LOCAL/PAPER_LIVE não precisam de credenciais);
3. relógio sincronizado (refletido em `trading_blocked` via
   `clock_out_of_sync`);
4. cursor de mercado definido (implícito: o provider já processou pelo
   menos um candle antes de qualquer sinal existir);
5. reconciliação inicial concluída
   (`SystemState.initialization_not_reconciled == False`, limpo por
   `Orchestrator.reconcile()` na primeira vez que uma reconciliação
   realmente completa — sucesso ou divergência, não apenas uma falha de
   rede que nem chegou a comparar);
6. estado remoto não ambíguo (`not state.trading_blocked` no momento da
   ativação).

O endpoint recusa explicitamente com mensagem em português quando (5) ou
(6) não estão satisfeitos, ou quando o estado operacional atual não é
`OBSERVANDO`/`PAUSADO`.

## Endpoints do painel

`GET /api/session` devolve a sessão ativa (ou `null`) com todos os
contadores — o que alimenta a seção "Sessão Atual" do painel.

## Desligamento gracioso (correção v1.1 #7)

`app/api/main.py::_graceful_shutdown` roda no `finally` do lifespan do
FastAPI (`_lifespan`):

1. marca `SystemState.operational_state = "ENCERRANDO"` (bloqueia novas
   entradas imediatamente);
2. cancela a tarefa de polling (`loop_task.cancel()`) e aguarda o
   `CancelledError`, sem deixá-la solta;
3. roda uma última `Orchestrator.reconcile()` — uma falha aqui nunca trava
   nem derruba o desligamento, apenas é registrada;
4. se há uma sessão ativa não encerrada, chama `end_session(...)` com um
   motivo em português;
5. persiste tudo antes do processo terminar.

Um **crash real** (que nunca alcança `_lifespan`) nunca encerra a sessão —
ela continua com `ended_at IS NULL` e é retomada normalmente no próximo
boot (sujeita ao mesmo gate de fingerprint acima). Só um desligamento que
efetivamente passa por `_lifespan` produz uma sessão encerrada, e só uma
sessão encerrada libera o próximo boot para criar uma sessão nova em vez
de retomar.

## Testes

`tests/test_operational_sessions_and_states.py` (retomada de sessão,
gates de ativação, pausa, causas de bloqueio independentes, contadores
reais via `Orchestrator.tick()`), `tests/test_session_fingerprint.py`
(retomada só com fingerprint idêntico, sessão nova em mudança de
estratégia/risco, nenhum segredo no fingerprint/snapshot),
`tests/test_graceful_shutdown.py` (sessão encerrada no desligamento real via
`TestClient`, ordem pendente nunca finalizada por adivinhação, próximo boot
cria sessão nova, crash continua retomando a sessão não encerrada).
