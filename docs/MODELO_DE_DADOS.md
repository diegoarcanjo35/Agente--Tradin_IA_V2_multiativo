# Modelo de Dados

Definido em `app/persistence/models.py` (SQLAlchemy). Toda tabela usa
timestamps em UTC (`app/core/clock.py::utcnow()`). Cadeia de rastreabilidade:
`candles → strategy_signals → (ai_recommendations | risk_evaluations) → orders → executions → positions`.

| Tabela | Propósito | Campos-chave |
|---|---|---|
| `candles` | Velas recebidas (replay ou Bybit) | `symbol`, `open_time`, OHLCV, `source` |
| `strategy_signals` | Sinal produzido pela estratégia determinística | `direction`, `justification`, `observed_price`, `atr`, `params_json` |
| `ai_recommendations` | Saída do AI Shadow Agent (validada ou rejeitada) | `signal_id` (FK), `recommendation`, `confidence`, `is_valid`, `rejection_reason` |
| `risk_evaluations` | Decisão do Risk Engine para um sinal | `signal_id` (FK), `approved`, `reason`, `checks_json` (cada check individual) |
| `orders` | Ordem submetida (PAPER_LOCAL, PAPER_LIVE ou BYBIT_DEMO) | `idempotency_key` (único), `risk_evaluation_id` (FK), `status` (máquina de estados — ver `docs/ORDEM_E_FILLS.md`), `exchange_order_id`, `is_close`, `filled_qty`, `avg_fill_price`, `fees_total`, `reference_price` |
| `order_events` (Fase 2) | Auditoria de toda transição de status de ordem | `order_id` (FK), `from_status`, `to_status`, `detail` |
| `executions` | Preenchimentos (fills), incluindo parciais | `order_id` (FK), `fill_qty`, `fill_price`, `fee`, `is_partial` |
| `positions` | Posições abertas/fechadas | `status`, `realized_pnl`, `fees_paid` (acumula taxa de abertura + parciais + fechamento — nunca só a última), `opened_at`, `closed_at`. Fase 3 multiativo: índice único **parcial** `uq_position_open_symbol` em `(symbol) WHERE status='OPEN'` — no máximo uma posição `OPEN` por símbolo no nível do banco; não afeta múltiplas posições `CLOSED` históricas do mesmo símbolo. |
| `account_snapshots` | Fotos periódicas de saldo/equity | `balance`, `equity`, `unrealized_pnl`, `mode` |
| `security_events` | Eventos de segurança (kill switch, bloqueio de endpoint de produção, drift de relógio, divergência de reconciliação, ativação/pausa operacional) | `event_type`, `detail` |
| `failures_reconciliations` | Falhas de execução e resultados de reconciliação (integrada ao Orchestrator, não só função isolada) | `kind` (FAILURE\|RECONCILIATION), `detail`, `resolved`, `order_id`/`session_id` (FK opcionais, Fase 2) |
| `operational_sessions` (Fase 2; Fase 3 multiativo) | Uma linha por sessão de execução — ver `docs/SESSOES_OPERACIONAIS.md` | `session_uid`, `mode`, `symbol` (legado, nullable desde a v7 — preenchido só quando monoativo), `symbols` (novo na v7, JSON nullable — lista ordenada e canônica, identidade real da sessão a partir da v7; `NULL` só em linhas legadas pré-migration, nunca retroativo), `status`, `started_at`/`ended_at`, contadores (`candles_count` etc.) |
| `system_state` | Estado vivo (linha única, id=1) | `trading_blocked`, `kill_switch_engaged`, `consecutive_losses`, `cooldown_until`, `api_failure_count`, `state_ambiguous`, `clock_out_of_sync`, `reconciliation_diverged`, `reconciliation_stale`, `order_state_unknown`, `initialization_not_reconciled`, `last_reconciliation_at`, `operational_state`, `active_session_id` (FK) |

## Rastreabilidade ponta a ponta

Toda ordem tem um caminho auditável: `orders.risk_evaluation_id` →
`risk_evaluations.signal_id` → `strategy_signals.id`. Uma recomendação de IA
para o mesmo instante é linkada por `ai_recommendations.signal_id`, mas nunca
aparece na cadeia de aprovação — ela é sempre um ramo paralelo, nunca
consumida pelo Risk Engine ou pelo Execution Engine.

Isso vale igualmente para fechamentos (`orders.is_close = true`): um
fechamento por sinal oposto ou por stop-loss/take-profit sempre gera sua
própria linha em `strategy_signals` (direção = lado de fechamento,
justificativa explica o gatilho) antes de `risk_evaluations` ser criada —
nunca existe uma ordem de fechamento com `signal_id` sintético/zero.
