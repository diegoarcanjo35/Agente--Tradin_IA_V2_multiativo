# Agente de Trading IA — Fase 1 + Fase 2 (Bybit Demo)

Sistema automatizado de trading conectado **exclusivamente** ao ambiente
oficial Bybit Demo Trading. Não opera dinheiro real, não aceita configuração
de produção, e não promete rentabilidade. Ver `docs/SEGURANCA.md` para as
garantias de segurança, `docs/ARQUITETURA.md` para a arquitetura completa e
`docs/FASE_2.md` para o que a Fase 2 (operação contínua e validação
controlada) adiciona sobre a Fase 1.

**AMBIENTE DEMO — SEM DINHEIRO REAL.**

## Instalação

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt      # Linux/Mac
```

Nenhuma credencial é necessária para o modo padrão (`REPLAY`).

## Execução

Use **sempre** o launcher oficial — é o único ponto de entrada que garante
que `API_HOST`/`API_PORT` validados (ver `docs/SEGURANCA.md`) sejam
realmente o host/porta usados pelo servidor:

```bash
.venv/Scripts/python -m app.run
```

Abra `http://127.0.0.1:8000` (ou a porta definida em `API_PORT`). O painel
mostra o estado do sistema, posições, sinais, decisões do Risk Engine,
recomendações da IA em shadow mode, curva de equity e histórico de erros.

Não inicie o servidor com `uvicorn app.api.main:app --host ...` diretamente
— esse caminho contorna a validação de host feita pelo launcher.

Para os demais modos (`PAPER_LOCAL`, `PAPER_LIVE`, `BYBIT_DEMO`), copie
`.env.example` para `.env` e siga `docs/OPERACAO_DEMO.md`.

## Testes

```bash
.venv/Scripts/python -m pytest -v
```

## Modos disponíveis

| Modo | Dados | Execução | Credenciais |
|---|---|---|---|
| `REPLAY` (padrão) | fixture local | simulada | nenhuma |
| `PAPER_LOCAL` | fixture local | simulada (taxa/slippage configuráveis) | nenhuma |
| `PAPER_LIVE` (Fase 2) | Bybit Demo Trading (endpoints públicos) | simulada, nunca reage à corretora | nenhuma |
| `BYBIT_DEMO` | Bybit Demo Trading | Bybit Demo Trading | API key/secret demo, sem saque |

Novas entradas (posições) exigem ativação explícita do operador em
**qualquer** modo (`POST /api/operational-state/activate`) — o processo
sempre inicia em `OBSERVANDO`, nunca `ATIVO`. Ver `docs/FASE_2.md`.

## Multiativo (Fase 3, fundação)

`SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT` (CSV) substitui `SYMBOL` para operar mais
de um símbolo no mesmo processo, com scheduler round-robin sequencial,
saúde por símbolo, e sessão/risco de portfólio. `SYMBOL` sozinho continua
100% funcional para instalações monoativo. `BYBIT_DEMO` autenticado
permanece monoativo nesta fase. Ver `docs/ARQUITETURA.md`, seção
"Multiativo", para o desenho completo — inclusive o isolamento obrigatório
de porta/banco em relação a uma instalação V1 monoativo no mesmo host
(`docs/SEGURANCA.md`, seção "Isolamento V1 vs. V2").

**Limitações desta fundação**: sem dinheiro real em nenhum modo; sem
multiativo autenticado (`BYBIT_DEMO`); sem promessa de rentabilidade;
cooldown/kill-switch permanecem globais ao portfólio, não por símbolo.

## Painel gráfico (Fase 3.1)

O painel web (`http://127.0.0.1:8001`) ganhou um card "Gráfico de Candles"
por símbolo — candles OHLC, volume, SMA rápida/lenta, marcadores de sinal e
linhas/faixas de posição, usando TradingView Lightweight Charts™
vendorizado localmente (sem CDN em runtime — ver `THIRD_PARTY_NOTICES.md`).
Estritamente observacional: nenhum controle de execução manual. Ver
`docs/PAINEL_GRAFICO.md` para arquitetura completa, contrato da API
(`GET /api/chart-data`) e legenda de cores.

## Painel financeiro (Fase 3.1.1)

O topo do painel mostra o **patrimônio atual** (equity), o resultado
líquido realizado, o P&L não realizado das posições abertas e o saldo
inicial configurado (`PAPER_STARTING_BALANCE_USD`, único ponto de
configuração — nunca mais um valor fixo no código). Um card separado
("Impacto dos Custos de Negociação") mostra taxas, slippage (já como
custo financeiro real, nunca a diferença unitária de preço) e funding —
sempre com o aviso de que esses valores já estão refletidos no resultado
líquido, nunca descontados uma segunda vez. Ver `docs/PAINEL_FINANCEIRO.md`
para a fórmula oficial, os escopos disponíveis (`lifetime`/`session`/
`daily`) e o contrato completo da API (`GET /api/portfolio-summary`,
`GET /api/costs`).

## Como gerar credenciais Bybit Demo Trading

Ver `docs/OPERACAO_DEMO.md`.

## Como iniciar sem credenciais

O modo padrão é `REPLAY` — basta rodar o comando de execução acima, sem
`.env`.

## Como ativar e testar o kill switch

Ver seção correspondente em `docs/OPERACAO_DEMO.md`. Resumo: botão "Ativar
bloqueio de emergência" no painel, ou `POST /api/kill-switch/engage`.

## Limitações conhecidas

- Estratégia simples (SMA cross + filtro ATR), sem promessa de
  rentabilidade.
- `BYBIT_DEMO` está implementado e testado com transporte falso, mas requer
  configuração de credenciais reais antes do primeiro uso real (ver
  `docs/OPERACAO_DEMO.md`).
- Funding fee não é coletado nesta fase (métrica reportada como
  "indisponível").
- Sem garantia de rentabilidade, sem promessa de retorno, sem gestão de
  dinheiro de terceiros. Fase 1 é um MVP de observação e validação de
  arquitetura, não um produto de investimento.

## Documentação

- `docs/ARQUITETURA.md`
- `docs/SEGURANCA.md`
- `docs/METRICAS.md`
- `docs/OPERACAO_DEMO.md`
- `docs/MODELO_DE_DADOS.md`
- `docs/MIGRACOES.md`
- `docs/FASE_2.md` — visão geral do que a Fase 2 adiciona
- `docs/ORDEM_E_FILLS.md` — máquina de estados de ordens e cancelamento
- `docs/SESSOES_OPERACIONAIS.md` — sessões e estado operacional
- `docs/ROTEIRO_TESTE_MANUAL_DEMO.md` — roteiro (não executado automaticamente,
  exige autorização separada)

---

**ENTREGA PRONTA PARA AUDITORIA INDEPENDENTE**
