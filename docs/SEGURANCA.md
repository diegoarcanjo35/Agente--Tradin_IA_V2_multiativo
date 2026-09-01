# Segurança

## Idioma da experiência do usuário (correção v1.2)

Toda a experiência destinada ao usuário — interface, botões, chips de
estado, justificativas exibidas, mensagens de erro, confirmações — é em
português do Brasil. Termos técnicos consolidados aparecem acompanhados da
tradução, por exemplo `Fator de lucro (Profit Factor)`. Identificadores
internos de código (nomes de classes, eventos como `KILL_SWITCH_ENGAGED`,
endpoints) permanecem em inglês, por não serem lidos diretamente pelo
usuário como frase. Verificado por `tests/test_frontend_i18n.py`.

## Proibição de dinheiro real

- `app/core/config.py` mantém um allowlist explícito de hosts Bybit
  demo/testnet (`ALLOWED_BYBIT_HOSTS`) e uma lista de hosts de produção
  conhecidos (`KNOWN_PRODUCTION_BYBIT_HOSTS`). `assert_demo_host()` é chamado
  na validação do `Settings` (portanto em qualquer modo, mesmo antes de saber
  se `BYBIT_DEMO` será usado) e novamente na construção de
  `BybitDemoMarketDataProvider`/`BybitDemoExecutionEngine`.
- Não existe endpoint HTTP, variável de ambiente em runtime, nem elemento de
  UI para trocar de ambiente. O modo é lido uma única vez na inicialização do
  processo (`get_settings()`, com `lru_cache`).
- Teste: `tests/test_security.py::test_assert_demo_host_rejects_known_production_host`
  e `test_settings_rejects_production_base_url_at_construction`.

## Credenciais

- `Settings` (Pydantic) só lê de variáveis de ambiente / `.env`. Nenhuma
  string de credencial está hard-coded em nenhum arquivo do repositório.
- `.env` está listado em `.gitignore`; `.env.example` traz apenas placeholders
  vazios (verificado por `tests/test_security.py::test_env_example_has_no_real_secret_values`).
- `app/core/logging.py::redact()` remove recursivamente qualquer valor cujo
  nome de campo contenha `key`, `secret`, `token`, `password` ou `signature`
  antes de qualquer linha ser emitida (console ou arquivo).
- O sistema nunca solicita nem usa permissão de saque. Ao gerar uma API key
  Bybit Demo, use **apenas** permissões de leitura + negociação (trade). Se o
  seu plano permitir, restrinja a key por IP (ver `docs/OPERACAO_DEMO.md`).

## IA sem autoridade de execução

- `app/ai_shadow/` não importa `app.execution` nem `pybit`, e não referencia
  nomes de campos de credenciais Bybit — verificado estruturalmente por
  `app/ai_shadow/guard.py` (varredura AST) e exercitado em
  `tests/test_ai_shadow.py::test_ai_module_has_no_execution_import`.
- `AIShadowAgent.observe()` retorna apenas dados (`AIShadowResult`); nunca
  recebe nem devolve uma referência a um `ExecutionEngine` ou `ApprovedOrder`.
- Saída validada contra um schema Pydantic estrito (`AIRecommendationOutput`);
  timeout e tamanho máximo de resposta são configuráveis e aplicados sempre.
- O provedor externo opcional (correção v1.1 #5, `app/ai_shadow/http_provider.py::HttpAIProvider`)
  vive dentro do mesmo pacote `app/ai_shadow/` e está sujeito à mesma
  varredura estrutural do `guard.py` — não pode importar `app.execution`/
  `pybit` nem referenciar credenciais Bybit, exatamente como
  `SimulatedProvider`. Desligado por padrão
  (`AI_SHADOW_EXTERNAL_PROVIDER_ENABLED=false`); só é usado quando
  explicitamente ligado **e** com `AI_PROVIDER_API_KEY`/
  `AI_PROVIDER_ENDPOINT_URL` preenchidos — uma configuração incompleta
  nunca tenta alcançar a rede, apenas mantém `SimulatedProvider`.

## Autoridade soberana do Risk Engine

- `app/risk/engine.py::ApprovedOrder` só pode ser construído com um
  `_RiskApprovalToken`, classe privada do módulo, **nunca** importada ou
  instanciada fora de `app/risk/engine.py`. Qualquer tentativa de construir um
  `ApprovedOrder` com um token inválido levanta `TypeError` — ver
  `tests/test_risk_engine.py::test_execution_requires_risk_approval`.
- Isso vale tanto para abrir quanto para fechar/reduzir posição:
  `RiskEngine.evaluate()` aprova aberturas, `RiskEngine.evaluate_close()`
  aprova fechamentos. Nenhum outro módulo (incluindo `app/orchestrator.py`)
  fabrica um `ApprovedOrder` diretamente — ambos os fluxos passam pelo Risk
  Engine. Um teste estrutural (AST) falha o build se qualquer arquivo fora de
  `app/risk/engine.py` referenciar `_RiskApprovalToken` ou chamar
  `ApprovedOrder(...)`: `tests/test_risk_engine_boundary.py`. Testes usam as
  factories públicas e controladas `RiskEngine.make_test_approved_order()` e
  `RiskEngine.attempt_construct_with_invalid_token_for_testing()`.
- `evaluate_close()` dispensa apenas os limites que existem para conter
  *nova* exposição (perda diária, posições concorrentes, exposição total,
  stop-loss obrigatório) — fechar uma posição reduz risco, então nunca é
  bloqueado por eles. Continua exigindo: kill switch livre, não
  `TRADING_BLOCKED`, estado não ambíguo (ver reconciliação abaixo), dados
  frescos, relógio sincronizado, saúde da API, posição existente, lado
  correto (oposto ao da posição) e quantidade positiva não superior à
  posição aberta.
- `ExecutionEngine.submit()` (Protocol em `app/execution/base.py`) só aceita
  esse tipo. Não há sobrecarga, atalho ou segundo construtor.

## Segredos em logs

- `app/core/logging.py` usa um `JsonFormatter` que passa todo campo extra por
  `redact()` antes de serializar. Testado em
  `tests/test_security.py::test_redact_hides_secret_like_fields`.

## Idempotência e reconciliação

- `app/execution/idempotency.py::make_idempotency_key()` gera uma chave
  determinística por sinal; motores de execução deduplicam por essa chave
  (`tests/test_execution.py::*duplicate*`).
- `app/execution/reconciliation.py::reconcile_positions()` (função pura)
  compara posições locais com posições reportadas pela exchange; qualquer
  divergência é reportada, nunca corrigida silenciosamente.
- `Orchestrator.reconcile()` integra essa função pura ao sistema em execução:
  roda uma vez na inicialização de `build_orchestrator()` (cobre "primeira
  inicialização" e "depois de reinício", em qualquer modo), sempre que uma
  ordem termina em status não confirmado (`UNKNOWN` — timeout na criação,
  falha ao confirmar status via polling), e agora também **periodicamente**
  (Fase 2, item 7.4): a cada `tick()`, se já se passou
  `RECONCILIATION_INTERVAL_SECONDS` desde a última reconciliação, uma nova
  roda automaticamente, antes de qualquer outra decisão do tick. Se a
  exchange não puder sequer ser consultada, ou se houver qualquer
  divergência, o sistema entra em `state_ambiguous=True` +
  `reconciliation_diverged=True` (Fase 2: causa nomeada especificamente,
  em paralelo ao `state_ambiguous` já existente) + `TRADING_BLOCKED=True`,
  registrado em `security_events` e `failures_reconciliations`. O Risk
  Engine (tanto `evaluate()` quanto `evaluate_close()`) recusa qualquer
  decisão enquanto `state_ambiguous` estiver ativo — só uma reconciliação
  bem-sucedida subsequente libera automaticamente um bloqueio causado por
  divergência (um bloqueio por kill switch nunca é limpo automaticamente).
  Se a reconciliação ficar mais atrasada que `RECONCILIATION_MAX_DELAY_SECONDS`,
  `SystemState.reconciliation_stale=True` bloqueia **apenas** novas
  aberturas (`RiskEngine.evaluate()`) — `evaluate_close()` nunca verifica
  essa causa, então fechar/reduzir exposição continua sempre permitido
  mesmo com reconciliação atrasada. Ver
  `tests/test_reconciliation_integration.py` e
  `tests/test_reconciliation_periodic_and_order_lifecycle.py`.

## Estado operacional e ativação de novas entradas (Fase 2, item 7.8)

- `POST /api/operational-state/activate` — move `OBSERVANDO`/`PAUSADO` para
  `ATIVO`, autorizando a estratégia a abrir novas posições. Segue a MESMA
  política de autenticação de `disengage_kill_switch`
  (`require_local_or_authenticated`): origem local sempre permitida, remota
  exige `CONTROL_API_TOKEN`. Recusa explicitamente (com mensagem em
  português) se `trading_blocked`, se a reconciliação inicial ainda não
  concluiu (`initialization_not_reconciled`), ou se o estado atual não é
  `OBSERVANDO`/`PAUSADO`.
- `POST /api/operational-state/pause` — sempre permitido, sem autenticação
  (só aumenta segurança, mesma política de `engage_kill_switch`). Nunca
  bloqueia fechamentos/reduções de posição.
- Nenhum dos dois endpoints altera `mode` — a restrição de Fase 1 ("nenhum
  endpoint troca REPLAY/PAPER_LOCAL/PAPER_LIVE/BYBIT_DEMO em tempo de
  execução") permanece intacta; `operational_state` é um eixo
  completamente separado. Ver `docs/FASE_2.md`.

## Relógio (drift)

- `app/core/clock.py::compute_clock_sync()` nunca assume drift=0: se a fonte
  de tempo de referência (`RemoteTimeProvider`) não puder ser consultada,
  retorna `ok=False` com `drift_seconds=None`, e o Risk Engine trata drift
  desconhecido como reprovação. Em `REPLAY`/`PAPER_LOCAL` a referência é
  `ReplayClockProvider`, determinística e injetável (usada nos testes para
  simular sincronizado, fora do limite, e indisponível). Em `BYBIT_DEMO`, a
  referência é `BybitServerTimeProvider`, que consulta `/v5/market/time` real
  da Bybit. Ver `tests/test_clock_sync.py`.

## Bind real do servidor e proteção da API de controle (correção v1.3 #3)

Auditoria anterior apontou que validar `Settings.api_host` não bastava:
nada impedia iniciar o processo com `uvicorn app.api.main:app --host
0.0.0.0` diretamente, que ignora por completo a validação interna — a
decisão do host de bind acontece na camada do `uvicorn`/CLI, fora do
alcance do código Python da aplicação. Duas camadas independentes resolvem
isso:

1. **Launcher oficial** (`app/run.py`, `python -m app.run`): único ponto de
   entrada suportado. Sempre lê `host`/`port` de `Settings` (já validados
   por `Settings.assert_safe_bind_host()`) e os repassa a
   `uvicorn.run(...)` programaticamente — nunca há um argumento de CLI
   solto que possa divergir do valor validado. Testado alcançando o
   ponto real de chamada a `uvicorn.run()`:
   `tests/test_bind_protection.py::test_launcher_passes_validated_settings_host_and_port_to_uvicorn`.
   Documentação e README instruem a nunca iniciar por outro caminho.
2. **Autenticação da API de controle**, independente de como o servidor foi
   iniciado. Política final (correção v1.4 #4, depois de uma versão
   anterior exigir token mesmo localmente e travar o próprio painel):
   - **Ativar** o bloqueio de emergência nunca exige autenticação — só
     aumenta a segurança.
   - **Desativar** LOCALMENTE (`request.client.host` em `127.0.0.1`/`::1`)
     sempre funciona, **com ou sem** `CONTROL_API_TOKEN` configurado. O
     painel local nunca envia nem precisa conhecer o token
     (`tests/test_bind_protection.py::test_frontend_never_sends_or_embeds_the_control_token`).
   - **Desativar remotamente** exige o cabeçalho `X-Control-Token` batendo
     com `CONTROL_API_TOKEN` (comparação com `hmac.compare_digest`). Sem
     token configurado, origem não local é negada por padrão.
   - A origem confiável é **sempre** `request.client.host` — o endereço do
     socket TCP que o servidor ASGI realmente aceitou. Cabeçalhos como
     `X-Forwarded-For`/`X-Real-IP`/`X-Forwarded-Host` **nunca** são
     consultados para essa decisão, então não podem ser forjados por um
     cliente remoto para se passar por local
     (`tests/test_bind_protection.py::test_spoofed_forwarded_headers_alone_never_grant_access`).
     Atrás de um proxy reverso, `request.client.host` será o endereço do
     próprio proxy — configure `CONTROL_API_TOKEN` nesse cenário; esta fase
     não implementa uma lista de proxies confiáveis para repassar cabeçalho
     de origem.
   - `request.client` ausente é tratado como não confiável (negado), nunca
     como bypass implícito.
   Ver `app/api/routes_control.py::require_local_or_authenticated` e
   `tests/test_bind_protection.py`.

Uma implementação completa de autenticação para todos os endpoints (não só
o kill switch) fica para uma fase futura.

## Proteção contra injeção de HTML no painel (correção v1.2 #7)

- `frontend/app.js` nunca usa `innerHTML` para inserir dado vindo do
  backend (justificativa da estratégia, motivo do Risk Engine, resumo da
  IA, detalhe de erro). Toda inserção de texto passa por `textContent` ou
  criação segura de elementos DOM (`buildRow`/`kvRow`), então uma string
  contendo marcação (ex.: `<img src=x onerror="...">`) é sempre exibida
  como texto literal, nunca interpretada como HTML executável.
- Prova estática: `tests/test_frontend_xss_safety.py::test_app_js_never_uses_innerhtml`.
- Prova dinâmica: o mesmo arquivo de teste executa as funções reais de
  construção de linha (`buildRow`/`kvRow`) de `app.js` num shim mínimo de
  DOM via Node.js, injeta um payload clássico de XSS e comprova que ele
  chega como `textContent` puro, sem gerar nenhum nó filho.

## Isolamento V1 vs. V2 (Fase 3 multiativo)

A V2 multiativo nunca deve colidir com uma instalação V1 (monoativo)
rodando no mesmo host:

- **Porta**: quando mais de um símbolo está configurado (`len(SYMBOLS) > 1`),
  `Settings.assert_no_v1_collision()` recusa `API_PORT=8000` (a porta
  conhecida da V1) na inicialização — falha cedo, antes de qualquer bind.
  `.env.example` da V2 já usa `API_PORT=8001` por padrão.
- **Banco de dados**: o mesmo guard recusa `DATABASE_URL` cujo nome de
  arquivo bata com o nome conhecido do banco da V1
  (`agente_trader_paper_live.db`). `.env.example` da V2 usa
  `agente_trader_multiativo_dev.db`.
- **Instalações monoativo (1 símbolo) nunca são afetadas** por este guard —
  retrocompatibilidade total com quem já roda a V2 com um único símbolo.
- **Processo**: a V2 nunca importa, referencia ou toca no diretório, no
  processo ou nas variáveis de ambiente da V1 — são bases de código e
  processos totalmente independentes; o único ponto de contato é
  intencional e histórico (o histórico Git da V1 foi usado para fundar o
  repositório da V2 — ver o commit-base `fd22a7b8...` na branch
  `fase-3-multiativo`), nunca em tempo de execução.
- Testado em `tests/test_config_symbols.py`
  (`test_multi_symbol_refuses_v1_known_port`,
  `test_multi_symbol_refuses_v1_known_database_filename`,
  `test_mono_symbol_is_never_affected_by_v1_guard`).
