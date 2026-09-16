// Correction v1.2 #7: NEVER use innerHTML with data that came from the
// backend (justificativas, motivos, resumos de IA, mensagens de erro) --
// all of it is inserted via textContent / DOM element creation only, so an
// externally-supplied string (e.g. from a future real AI provider) can
// never be interpreted as markup or an executable event handler.
const $ = (id) => document.getElementById(id);

const DIRECTION_LABELS = { BUY: "COMPRA", SELL: "VENDA", HOLD: "AGUARDAR" };
// Fase 3.6, item 5: nome compreensível primeiro, termo técnico entre
// parênteses depois -- nunca o único texto apresentado.
const MODE_LABELS = {
  REPLAY: "Dados históricos, sem operação real (REPLAY)",
  PAPER_LOCAL: "Simulação local (PAPER_LOCAL)",
  PAPER_LIVE: "Dados reais, operações simuladas (PAPER_LIVE)",
  BYBIT_DEMO: "Bybit Demo Trading (BYBIT_DEMO)",
};
const POSITION_STATUS_LABELS = { OPEN: "ABERTA", CLOSED: "ENCERRADA" };
const OPERATIONAL_STATE_LABELS = {
  INICIALIZANDO: "INICIALIZANDO",
  OBSERVANDO: "OBSERVANDO — novas entradas desativadas",
  ATIVO: "ATIVO — novas entradas autorizadas",
  PAUSADO: "PAUSADO — novas entradas desativadas",
  BLOQUEADO: "BLOQUEADO",
  ENCERRANDO: "ENCERRANDO",
};
// Fase 3.2: "o laço está processando candles?" -- um conceito só.
const MARKET_PROCESSING_LABELS = {
  INICIANDO: "INICIANDO",
  ATIVO: "ATIVO",
  DEGRADADO: "DEGRADADO",
  PARADO: "PARADO",
  ENCERRANDO: "ENCERRANDO",
};
// Fase 3.2: "entradas novas estão autorizadas?" -- o outro conceito.
const NEW_ENTRIES_LABELS = {
  ATIVADAS: "ATIVADAS",
  DESATIVADAS: "DESATIVADAS",
  BLOQUEADAS: "BLOQUEADAS",
  BLOQUEADAS_EMERGENCIA: "BLOQUEADAS (bloqueio de emergência)",
};
const POLL_STATUS_LABELS = {
  INICIANDO: "INICIANDO",
  SAUDAVEL: "SAUDÁVEL",
  DEGRADADO: "DEGRADADO (falha recente, tentando recuperar)",
  PARADO: "PARADO (heartbeat vencido ou tarefa morta)",
  ENCERRANDO: "ENCERRANDO",
};
const ORDER_STATUS_LABELS = {
  PENDING_SUBMIT: "AGUARDANDO ENVIO", SUBMITTED: "ENVIADA", PARTIALLY_FILLED: "PARCIALMENTE EXECUTADA",
  FILLED: "EXECUTADA", CANCEL_PENDING: "CANCELAMENTO PENDENTE", CANCELLED: "CANCELADA",
  REJECTED: "RECUSADA", UNKNOWN: "DESCONHECIDA",
};
const SCOPE_LABELS = {
  lifetime: "histórico completo", session: "sessão atual", daily: "hoje (UTC)",
};

function translateDirection(direction) {
  return DIRECTION_LABELS[direction] || direction;
}

function fmtNumber(v, digits = 2) {
  if (v === "indisponível" || v === null || v === undefined) return "indisponível";
  if (typeof v === "number") return v.toFixed(digits);
  return String(v);
}

function isUnavailable(v) {
  return v === "indisponível" || v === null || v === undefined;
}

function pnlClass(v) {
  if (typeof v !== "number") return "";
  return v > 0 ? "positive" : v < 0 ? "negative" : "";
}

// Fase 3.1.1 (correção final da auditoria do PO): toda métrica financeira
// exibida no painel passa por um destes três formatadores -- nenhum número
// cru sem unidade. `N/D` (nunca `null`/`NaN`/`Infinity`/um zero inventado)
// para qualquer valor não numérico ou não finito, incluindo o sentinela
// `"indisponível"` que a API já usa.
function fmtCurrency(v, signed = false) {
  if (isUnavailable(v) || typeof v !== "number" || !Number.isFinite(v)) return "N/D";
  const abs = Math.abs(v);
  const formatted = abs.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let prefix = "";
  if (v > 0) prefix = signed ? "+" : "";
  else if (v < 0) prefix = "–"; // travessão curto, nunca hífen ASCII
  return `${prefix}US$ ${formatted}`;
}

// `v` já deve estar na escala 0-100 (nunca a fração 0-1 crua) -- ver os
// pontos de chamada (ex.: win_rate * 100).
function fmtPercent(v, digits = 1, signed = false) {
  if (isUnavailable(v) || typeof v !== "number" || !Number.isFinite(v)) return "N/D";
  const formatted = Math.abs(v).toLocaleString("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  let prefix = "";
  if (v > 0) prefix = signed ? "+" : "";
  else if (v < 0) prefix = "–";
  return `${prefix}${formatted}%`;
}

function fmtRatio(v, digits = 2) {
  if (isUnavailable(v) || typeof v !== "number" || !Number.isFinite(v)) return "N/D";
  return `${v.toLocaleString("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits })}×`;
}

function fmtInt(v) {
  if (isUnavailable(v) || typeof v !== "number" || !Number.isFinite(v)) return "N/D";
  return String(Math.round(v));
}

// Cria um cartão de estatística (rótulo + valor) dentro de `container`,
// sempre via textContent/createElement -- nunca innerHTML. `opts.title`
// vira o tooltip nativo do navegador E um ícone de ajuda visível (item 5
// do brief: "cada métrica menos óbvia deve ter um ícone de ajuda com
// explicação curta em português simples") -- nunca só um title invisível.
function statCard(container, label, valueText, opts = {}) {
  const card = document.createElement("div");
  card.className = "stat-card" + (opts.cardClass ? ` ${opts.cardClass}` : "");

  const labelEl = document.createElement("span");
  labelEl.className = "stat-label";
  labelEl.textContent = label;
  if (opts.title) {
    const help = document.createElement("span");
    help.className = "help-icon";
    help.textContent = "?";
    help.title = opts.title;
    labelEl.appendChild(help);
  }

  const valueEl = document.createElement("span");
  valueEl.className = "stat-value" + (opts.valueClass ? ` ${opts.valueClass}` : "");
  valueEl.textContent = valueText;

  card.appendChild(labelEl);
  card.appendChild(valueEl);
  container.appendChild(card);
  return card;
}

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  return res.json();
}

function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Builds one <div class="kv"><span>label</span><span class="v ...">value</span></div>
// entirely via textContent -- no markup ever passes through as HTML.
function kvRow(container, label, value, extraClass) {
  const row = document.createElement("div");
  row.className = "kv";

  const labelSpan = document.createElement("span");
  labelSpan.textContent = label;

  const valueSpan = document.createElement("span");
  valueSpan.className = "v" + (extraClass ? ` ${extraClass}` : "") + (isUnavailable(value) ? " unavailable" : "");
  valueSpan.textContent = value;

  row.appendChild(labelSpan);
  row.appendChild(valueSpan);
  container.appendChild(row);
}

// Builds a <tr> from an array of cell descriptors: string | {text, className}.
function buildRow(cells) {
  const tr = document.createElement("tr");
  cells.forEach((cell) => {
    const td = document.createElement("td");
    if (cell && typeof cell === "object") {
      td.textContent = cell.text;
      if (cell.className) td.className = cell.className;
    } else {
      td.textContent = cell;
    }
    tr.appendChild(td);
  });
  return tr;
}

function setRows(tbody, rowsData) {
  clearChildren(tbody);
  rowsData.forEach((cells) => tbody.appendChild(buildRow(cells)));
}

async function refreshState() {
  const s = await getJSON("/api/state");
  $("chip-mode").textContent = `MODO: ${MODE_LABELS[s.mode] || s.mode}`;
  $("chip-conn").textContent = `CONEXÃO: ${s.mode === "REPLAY" ? "offline (replay)" : "ativa"}`;
  // Fase 3.2: os dois chips vêm de campos DERIVADOS DO ESTADO REAL no
  // backend (`market_processing_status`/`new_entries_status`) -- nunca de
  // um literal, e nunca de um mesmo booleano servindo aos dois conceitos.
  $("chip-processing").textContent =
    `PROCESSAMENTO DE MERCADO: ${MARKET_PROCESSING_LABELS[s.market_processing_status] || s.market_processing_status || "indisponível"}`;
  $("chip-entries").textContent =
    `NOVAS ENTRADAS: ${NEW_ENTRIES_LABELS[s.new_entries_status] || s.new_entries_status || "indisponível"}`;
  $("chip-kill").textContent = `BLOQUEIO DE EMERGÊNCIA: ${s.kill_switch_engaged ? "ATIVADO" : "desativado"}`;
  $("chip-op-state").textContent = `ESTADO OPERACIONAL: ${OPERATIONAL_STATE_LABELS[s.operational_state] || s.operational_state}`;
  $("env-banner").textContent = s.environment_banner;
  $("last-updated").textContent = new Date().toLocaleString("pt-BR");

  // Item A da nova hierarquia: o motivo principal de bloqueio precisa
  // aparecer junto do banner de estado, nunca só dentro do diagnóstico
  // técnico recolhido.
  const reasonLine = $("block-reason-line");
  if (s.trading_blocked && s.block_reason) {
    reasonLine.textContent = `Motivo do bloqueio: ${s.block_reason}`;
    reasonLine.hidden = false;
  } else {
    reasonLine.hidden = true;
    reasonLine.textContent = "";
  }

  // Causas de bloqueio independentes -- nunca colapsadas num único booleano
  // (item 7.5/7.9), e estados críticos nunca dependem só de cor: cada linha
  // também tem o texto SIM/NÃO em português, não apenas uma classe CSS.
  const box = $("block-causes-box");
  clearChildren(box);
  kvRow(box, "Bloqueio de emergência manual", s.kill_switch_engaged ? "SIM" : "não", s.kill_switch_engaged ? "negative" : "");
  kvRow(box, "Estado ambíguo / lacuna de mercado", s.state_ambiguous ? "SIM" : "não", s.state_ambiguous ? "negative" : "");
  kvRow(box, "Relógio fora de sincronia", s.clock_out_of_sync ? "SIM" : "não", s.clock_out_of_sync ? "negative" : "");
  kvRow(box, "Reconciliação divergente", s.reconciliation_diverged ? "SIM" : "não", s.reconciliation_diverged ? "negative" : "");
  kvRow(box, "Reconciliação atrasada (só bloqueia novas entradas)", s.reconciliation_stale ? "SIM" : "não", s.reconciliation_stale ? "negative" : "");
  kvRow(box, "Ordem em estado desconhecido", s.order_state_unknown ? "SIM" : "não", s.order_state_unknown ? "negative" : "");
  kvRow(box, "Falhas de API", s.api_failure_count);
  kvRow(box, "Última reconciliação", s.last_reconciliation_at ? new Date(s.last_reconciliation_at).toLocaleString("pt-BR") : "indisponível");
  kvRow(box, "Intervalo de reconciliação (s)", s.reconciliation_interval_seconds);

  // Correção operacional do poll loop v1.0: o servidor HTTP respondendo
  // nunca prova que o motor de mercado está vivo -- por isso este bloco
  // tem sua própria seção, nunca escondida atrás do resto do painel.
  const pollBox = $("poll-health-box");
  clearChildren(pollBox);
  const pollStatusLabel = POLL_STATUS_LABELS[s.poll_loop_status] || s.poll_loop_status;
  const pollUnhealthy = s.poll_loop_status === "DEGRADADO" || s.poll_loop_status === "PARADO";
  kvRow(pollBox, "Status do motor", pollStatusLabel, pollUnhealthy ? "negative" : "");
  kvRow(pollBox, "Último ciclo iniciado", s.poll_last_started_at ? new Date(s.poll_last_started_at).toLocaleString("pt-BR") : "ainda não iniciou");
  kvRow(pollBox, "Último ciclo concluído", s.poll_last_completed_at ? new Date(s.poll_last_completed_at).toLocaleString("pt-BR") : "ainda não concluiu");
  kvRow(pollBox, "Último sucesso", s.poll_last_success_at ? new Date(s.poll_last_success_at).toLocaleString("pt-BR") : "nenhum ainda");
  kvRow(pollBox, "Falhas consecutivas", s.poll_consecutive_failures, s.poll_consecutive_failures > 0 ? "negative" : "");
  kvRow(pollBox, "Último erro", s.poll_last_error || "nenhum");
  kvRow(pollBox, "Reinícios automáticos da tarefa", s.poll_restart_count);
  kvRow(pollBox, "Limite de heartbeat (s)", s.poll_heartbeat_max_age_seconds);
}

async function refreshSession() {
  const s = await getJSON("/api/session");
  const box = $("session-box");
  clearChildren(box);
  if (!s) {
    kvRow(box, "Sessão", "nenhuma sessão ativa ainda");
    return;
  }
  kvRow(box, "Sessão", s.session_uid.slice(0, 8));
  kvRow(box, "Status da sessão", OPERATIONAL_STATE_LABELS[s.status] || s.status);
  kvRow(box, "Iniciada em", new Date(s.started_at).toLocaleString("pt-BR"));
  kvRow(box, "Candles processados", s.candles_count);
  kvRow(box, "Sinais gerados", s.signals_count);
  kvRow(box, "Aprovações / Rejeições", `${s.approvals_count} / ${s.rejections_count}`);
  kvRow(box, "Ordens / Fills", `${s.orders_count} / ${s.fills_count}`);
  kvRow(box, "Falhas / Reconciliações", `${s.failures_count} / ${s.reconciliations_count}`);
}

async function refreshOrders() {
  const rows = await getJSON("/api/orders?limit=20");
  setRows(
    document.querySelector("#orders-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      r.symbol,
      translateDirection(r.side),
      { text: ORDER_STATUS_LABELS[r.status] || r.status, className: r.status === "FILLED" ? "positive" : (r.status === "REJECTED" || r.status === "UNKNOWN") ? "negative" : "" },
      r.filled_qty.toFixed(6),
      r.avg_fill_price ? r.avg_fill_price.toFixed(2) : "-",
    ])
  );
}

// Fase 3.1.1 (correção final da auditoria do PO), seção F: "Impacto dos
// Custos de Negociação". Nunca soma o slippage de novo ao patrimônio/
// resultado líquido -- ele já está embutido no preço executado (aviso
// fixo no HTML, ver index.html). Campos antigos `slippage_avg_usd`/
// `slippage_total_usd` (diferença unitária de preço, nunca dinheiro) não
// existem mais na API -- ver app/metrics/engine.py.
// `N/D` nunca herda cor de positivo/negativo/custo -- só um valor
// numérico conhecido justifica a classe visual.
function classIfKnown(v, cls) {
  return isUnavailable(v) || typeof v !== "number" || !Number.isFinite(v) ? "" : cls;
}

async function refreshCosts() {
  const [c, summary] = await Promise.all([
    getJSON("/api/costs"), getJSON("/api/portfolio-summary"),
  ]);
  const portfolio = summary.portfolio;
  const box = $("costs-box");
  clearChildren(box);

  const hasFunding = !isUnavailable(portfolio.funding_paid) && !isUnavailable(portfolio.funding_received);
  const adverseGross = (typeof c.adverse_slippage_cost_usd === "number" ? c.adverse_slippage_cost_usd : 0)
    + c.fees_total + (hasFunding ? portfolio.funding_paid : 0);
  const credits = (typeof c.price_improvement_value_usd === "number" ? c.price_improvement_value_usd : 0)
    + (hasFunding ? portfolio.funding_received : 0);

  statCard(box, "Taxas pagas", fmtCurrency(c.fees_total), {
    valueClass: classIfKnown(c.fees_total, "cost"), title: "Soma de todas as taxas de execução pagas (entrada e saída).",
  });
  statCard(box, "Slippage adverso", fmtCurrency(c.adverse_slippage_cost_usd), {
    valueClass: classIfKnown(c.adverse_slippage_cost_usd, "cost"),
    title: "Custo financeiro real (diferença de preço × quantidade executada) das execuções piores que a referência.",
  });
  statCard(box, "Melhoria de preço", fmtCurrency(c.price_improvement_value_usd), {
    valueClass: classIfKnown(c.price_improvement_value_usd, "positive"),
    title: "Valor financeiro ganho em execuções melhores que a referência -- nunca cancela o slippage adverso silenciosamente.",
  });
  statCard(box, "Impacto líquido de execução", fmtCurrency(c.net_slippage_impact_usd, true), {
    valueClass: pnlClass(typeof c.net_slippage_impact_usd === "number" ? -c.net_slippage_impact_usd : 0),
    title: "Slippage adverso menos melhoria de preço -- diagnóstico de atribuição, já refletido no resultado líquido.",
  });
  statCard(box, "Funding pago", fmtCurrency(portfolio.funding_paid), { valueClass: classIfKnown(portfolio.funding_paid, "cost") });
  statCard(box, "Funding recebido", fmtCurrency(portfolio.funding_received), { valueClass: classIfKnown(portfolio.funding_received, "positive") });
  statCard(box, "Funding líquido", fmtCurrency(portfolio.funding_net, true), {
    valueClass: pnlClass(typeof portfolio.funding_net === "number" ? portfolio.funding_net : 0),
  });
  statCard(box, "Slippage % ponderado", fmtPercent(c.weighted_slippage_pct, 3, true), {
    title: "Impacto financeiro líquido do slippage dividido pelo notional de referência -- ponderado por tamanho, nunca a média simples dos percentuais.",
  });
  statCard(box, "Ordens analisadas", `${fmtInt(c.priced_orders_count)} (${fmtInt(c.unpriced_orders_count)} sem referência)`);
  statCard(box, "Impacto adverso bruto", fmtCurrency(adverseGross), {
    valueClass: classIfKnown(adverseGross, "cost"), title: "Taxas + slippage adverso + funding pago -- diagnóstico, nunca uma segunda dedução do patrimônio.",
  });
  statCard(box, "Benefícios / créditos", fmtCurrency(credits), { valueClass: "positive" });

  const bySymbolTbody = document.querySelector("#costs-by-symbol-table tbody");
  const symbols = Object.keys(summary.per_symbol || {});
  const rows = await Promise.all(symbols.map(async (symbol) => {
    const sc = await getJSON(`/api/costs?symbol=${encodeURIComponent(symbol)}`);
    return [
      symbol, `${fmtInt(sc.priced_orders_count)} (${fmtInt(sc.unpriced_orders_count)} s/ ref.)`,
      fmtCurrency(sc.fees_total), fmtCurrency(sc.adverse_slippage_cost_usd),
      fmtCurrency(sc.price_improvement_value_usd),
    ];
  }));
  setRows(bySymbolTbody, rows);
}

// Fase 3.1.1, seção E: "Desempenho" -- taxa de acerto/payoff/profit
// factor/drawdown, sempre com unidade explícita.
async function refreshMetrics() {
  const m = await getJSON("/api/metrics");
  const grid = $("performance-grid");
  clearChildren(grid);

  statCard(grid, "Operações encerradas", fmtInt(m.closed_trades_count));
  statCard(grid, "Taxa de acerto", fmtPercent(typeof m.win_rate === "number" ? m.win_rate * 100 : m.win_rate));
  statCard(grid, "Profit Factor", fmtRatio(m.profit_factor), {
    title: "Lucro bruto dividido pelo prejuízo bruto absoluto -- acima de 1× é lucrativo no período.",
  });
  statCard(grid, "Payoff", fmtRatio(m.payoff), {
    title: "Ganho médio por operação vencedora dividido pela perda média por operação perdedora.",
  });
  statCard(grid, "Expectância por operação", fmtCurrency(m.expectancy, true), {
    valueClass: pnlClass(typeof m.expectancy === "number" ? m.expectancy : 0),
    title: "Resultado médio esperado por operação, combinando taxa de acerto e tamanho médio de ganhos/perdas.",
  });
  statCard(grid, "Drawdown atual", fmtCurrency(m.current_drawdown_money), {
    valueClass: (typeof m.current_drawdown_money === "number" && m.current_drawdown_money > 0) ? "negative" : "",
    title: "Distância do último ponto da curva realizada até o pico anterior -- 0 quando no próprio pico.",
  });
  statCard(grid, "Drawdown máximo", fmtCurrency(m.max_drawdown_money), {
    valueClass: (typeof m.max_drawdown_money === "number" && m.max_drawdown_money > 0) ? "negative" : "",
  });
  statCard(grid, "Drawdown máximo (%)", fmtPercent(m.max_drawdown_pct));
  statCard(grid, "Retorno / Drawdown", fmtRatio(m.return_over_drawdown));
}

// Fase 3.1.1, seção B: "Resumo financeiro principal" -- patrimônio
// calculado SOB DEMANDA a cada atualização (nunca persistido nesta fase,
// ver app/api/routes_dashboard.py::get_portfolio_summary). Decisão
// definitiva do PO: equity NÃO TEM escopo -- `portfolio` é sempre
// lifetime, idêntico não importa o que `period_performance` mostre; o
// badge exibe "histórico completo" porque é isso que `portfolio` sempre
// representa aqui, nunca um seletor que trocaria o patrimônio exibido.
async function refreshPortfolioSummary() {
  const body = await getJSON("/api/portfolio-summary?scope=lifetime");
  const p = body.portfolio;

  $("equity-scope-badge").textContent = `escopo: ${SCOPE_LABELS.lifetime}`;
  $("equity-incomplete-notice").hidden = p.equity_complete !== false;

  const grid = $("hero-grid");
  clearChildren(grid);

  const equityDelta = (typeof p.equity === "number" && typeof p.starting_balance === "number")
    ? p.equity - p.starting_balance : null;
  statCard(grid, "Patrimônio atual", fmtCurrency(p.equity), {
    cardClass: "stat-card-hero",
    valueClass: "stat-value-hero " + pnlClass(equityDelta || 0),
    title: "Patrimônio = saldo inicial + P&L realizado - taxas + funding líquido + P&L não realizado.",
  });
  statCard(grid, "Resultado líquido realizado", fmtCurrency(p.realized_net_pnl, true), {
    valueClass: pnlClass(typeof p.realized_net_pnl === "number" ? p.realized_net_pnl : 0),
    title: "P&L de preço já fechado, descontadas as taxas pagas até agora e somado o funding líquido.",
  });
  statCard(grid, "P&L não realizado", fmtCurrency(p.unrealized_pnl, true), {
    valueClass: pnlClass(typeof p.unrealized_pnl === "number" ? p.unrealized_pnl : 0),
    title: "Valor a mercado das posições abertas agora -- preço visual quando disponível, senão o último fechamento.",
  });
  statCard(grid, "Saldo inicial", fmtCurrency(p.starting_balance), {
    title: "Capital inicial configurado para esta carteira PAPER -- nunca um depósito repetido por sessão.",
  });
  statCard(grid, "Posições abertas", fmtInt(p.open_positions_count));
  statCard(grid, "Exposição total", fmtCurrency(p.exposure_usd), {
    title: "Soma do valor nocional (quantidade × preço de entrada) das posições abertas -- não é lucro nem prejuízo.",
  });
}

async function refreshPositionsTable() {
  const positions = await getJSON("/api/positions");
  setRows(
    document.querySelector("#positions-table tbody"),
    positions.map((p) => [
      p.symbol,
      translateDirection(p.side),
      p.qty.toFixed(6),
      p.avg_entry_price.toFixed(2),
      p.stop_loss != null ? p.stop_loss.toFixed(2) : "-",
      p.take_profit != null ? p.take_profit.toFixed(2) : "-",
    ])
  );
}

async function refreshSignals() {
  const rows = await getJSON("/api/signals?limit=20");
  setRows(
    document.querySelector("#signals-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      r.symbol,
      translateDirection(r.direction),
      r.observed_price.toFixed(2),
      r.justification,
    ])
  );
}

// Fase 3.1.1, seção D: tabela dinâmica (nunca fixa em BTC/ETH/SOL) gerada
// a partir de `/api/symbols` -- uma linha por símbolo REALMENTE
// configurado. Cada linha usa exclusivamente os componentes monetários
// daquele símbolo (nunca soma/mistura preço unitário ou percentuais de
// outro ativo -- item 8 da decisão do PO).
// Fase 3.6, item 4: nome completo primeiro, código técnico como
// complemento -- "Bitcoin — BTC/USDT", nunca só "BTCUSDT". Símbolo sem
// entrada aqui (par futuro ainda não mapeado) cai no próprio código cru,
// nunca um nome inventado.
const SYMBOL_DISPLAY_NAMES = {
  BTCUSDT: "Bitcoin — BTC/USDT",
  ETHUSDT: "Ethereum — ETH/USDT",
  SOLUSDT: "Solana — SOL/USDT",
};
function symbolDisplayName(symbol) {
  return SYMBOL_DISPLAY_NAMES[symbol] || symbol;
}

const SYMBOL_HEALTH_LABELS = {
  INICIANDO: "INICIANDO", SAUDAVEL: "SAUDÁVEL",
  DEGRADADO: "DEGRADADO", PARADO: "PARADO", ENCERRANDO: "ENCERRANDO",
};

async function refreshSymbolsSummary() {
  const [symbolsResp, state, positions, summary] = await Promise.all([
    getJSON("/api/symbols"), getJSON("/api/state"), getJSON("/api/positions"),
    getJSON("/api/portfolio-summary?scope=lifetime"),
  ]);
  const symbols = symbolsResp.symbols || [];
  const health = (state.symbols_health && state.symbols_health.per_symbol) || {};
  const perSymbolPortfolio = summary.per_symbol || {};
  const positionsBySymbol = {};
  positions.forEach((p) => { positionsBySymbol[p.symbol] = p; });

  const lastSignals = await Promise.all(symbols.map(async (symbol) => {
    const rows = await getJSON(`/api/signals?limit=1&symbol=${encodeURIComponent(symbol)}`);
    return rows[0] || null;
  }));

  const tbody = document.querySelector("#symbols-summary-table tbody");
  const rows = symbols.map((symbol, i) => {
    const h = health[symbol] || {};
    const healthLabel = SYMBOL_HEALTH_LABELS[h.status] || h.status || "indisponível";
    const healthy = h.status === "SAUDAVEL";

    const p = positionsBySymbol[symbol];
    const comp = perSymbolPortfolio[symbol] || {};
    const openPosition = (comp.positions || [])[0];
    // Fase 3.2: preço de marcação DO SÍMBOLO, vindo da fonte única do
    // backend (`_resolve_mark_price`) -- disponível mesmo sem posição
    // aberta. Continua "N/D" quando genuinamente não há preço; nunca o
    // preço de outro símbolo, nunca inventado.
    const markPrice = typeof comp.mark_price === "number"
      ? comp.mark_price
      : (openPosition && typeof openPosition.mark_price === "number" ? openPosition.mark_price : null);
    const price = markPrice === null ? "N/D" : fmtCurrency(markPrice);
    const positionText = p
      ? `${translateDirection(p.side)} ${p.qty.toFixed(6)} @ ${p.avg_entry_price.toFixed(2)}`
      : "sem posição";

    const lastSignal = lastSignals[i];
    const lastSignalText = lastSignal ? translateDirection(lastSignal.direction) : "N/D";

    return [
      symbolDisplayName(symbol),
      { text: healthLabel, className: healthy ? "" : "negative" },
      price,
      positionText,
      fmtCurrency(comp.exposure_usd),
      { text: fmtCurrency(comp.realized_price_pnl, true), className: pnlClass(typeof comp.realized_price_pnl === "number" ? comp.realized_price_pnl : 0) },
      { text: fmtCurrency(comp.unrealized_pnl, true), className: pnlClass(typeof comp.unrealized_pnl === "number" ? comp.unrealized_pnl : 0) },
      lastSignalText,
    ];
  });
  setRows(tbody, rows);
}

// Fase 3.1 (painel gráfico): TradingView Lightweight Charts, vendorizado
// localmente (frontend/vendor/, ver THIRD_PARTY_NOTICES.md). Toda a lógica
// abaixo é guardada por `typeof LightweightCharts !== "undefined"` -- a
// biblioteca é carregada via <script> separado antes deste arquivo; sem
// ela (ex.: o harness Node de tests/test_frontend_xss_safety.py, que faz
// eval() deste arquivo inteiro sem a lib), nada de gráfico é executado,
// nunca lança exceção.
const CHART_UP_COLOR = "#34d399";
const CHART_DOWN_COLOR = "#f87171";
// Fase 3.6: causa raiz comprovada do "gráfico voltar" -- refreshChart()
// era chamado a cada 2s (setInterval(refreshAll, 2000)) e SEMPRE
// reancorava a janela visível no candle mais recente no final (fitContent
// ou setVisibleRange), sem nenhuma noção de "o usuário está navegando".
// `followLive` (true = modo ao vivo, acompanha o candle mais recente;
// false = modo exploração, nunca reancorado pelo polling) e
// `wasRecentChartUserInput()` (só uma interação real do usuário no canvas
// -- pointer/touch ativo ou recém-solto, ou wheel recente -- nunca uma
// recalculagem interna da biblioteca -- pode marcar exploração) resolvem
// isso sem tocar em nenhum dado real.
const CHART_STATE = {
  chart: null, candleSeries: null, volumeSeries: null,
  smaFastSeries: null, smaSlowSeries: null, symbol: null,
  priceLines: [], zoneEls: [], lastCandles: [], activeWindow: "all",
  followLive: true,
  pointerActive: false, lastPointerReleaseAt: 0, lastWheelAt: 0,
  viewStateBySymbol: new Map(), tooltipEl: null, resizeObserver: null,
  visibleRangeHandler: null, crosshairHandler: null,
};

// Correção comprovada em navegador real (nunca reproduzida pelos mocks do
// Node, que nunca simulam a biblioteca reagindo por conta própria): a
// Lightweight Charts real dispara subscribeVisibleTimeRangeChange de
// forma ASSÍNCRONA e por conta PRÓPRIA em vários momentos que não são o
// usuário explorando -- criar um chart novo, popular a primeira leva de
// dados (setData) e o próprio autoSize/ResizeObserver interno já disparam
// isso sozinhos, em instantes imprevisíveis. Tentar listar e cobrir cada
// gatilho programático com uma flag/temporizador é uma corrida perdida.
//
// Por isso a detecção é uma LISTA DE PERMISSÃO: só conta como "o usuário
// está explorando" se houver prova de uma interação real em andamento OU
// recém-terminada no próprio canvas do gráfico. Correção da 2ª auditoria
// (item 5): a versão anterior dependia só de "mousedown nos últimos 3s",
// o que falha para um arraste (drag) que dure mais que isso -- ao soltar
// o botão depois de 3s+ segurando, a marca de tempo do mousedown já teria
// expirado antes do evento assíncrono de range chegar. Agora o estado é
// ATIVO enquanto o ponteiro/toque estiver pressionado (sem limite de
// tempo -- um arraste pode durar o quanto for), e o wheel (que não tem um
// "pressionado"/"solto" natural) tem sua PRÓPRIA janela de tolerância,
// separada e mais curta.
function isChartPointerHeld() {
  return CHART_STATE.pointerActive;
}
function wasChartPointerJustReleased() {
  return Date.now() - CHART_STATE.lastPointerReleaseAt < CHART_POINTER_RELEASE_GRACE_MS;
}
function wasRecentChartWheel() {
  return Date.now() - CHART_STATE.lastWheelAt < CHART_WHEEL_WINDOW_MS;
}
// Comprovado em navegador real: o evento assíncrono da biblioteca pode
// chegar bem mais de meio segundo depois do gesto que o causou (zoom por
// roda do mouse envolve uma transição animada até assentar). Janelas
// curtas demais perderiam exploração genuína -- exatamente o defeito que
// esta fase existe para corrigir.
const CHART_POINTER_RELEASE_GRACE_MS = 3000;
const CHART_WHEEL_WINDOW_MS = 3000;

function wasRecentChartUserInput() {
  return isChartPointerHeld() || wasChartPointerJustReleased() || wasRecentChartWheel();
}

function markChartPointerDown() {
  CHART_STATE.pointerActive = true;
}
// No `window`, não só no container: um arraste real frequentemente
// termina com o ponteiro solto FORA da área do gráfico (o usuário
// arrasta rápido e o mouseup dispara em cima de outro elemento da
// página) -- um listener só no container perderia esse término.
function markChartPointerUp() {
  if (!CHART_STATE.pointerActive) return;
  CHART_STATE.pointerActive = false;
  CHART_STATE.lastPointerReleaseAt = Date.now();
}
function markChartWheel() {
  CHART_STATE.lastWheelAt = Date.now();
}

let chartInteractionTrackingInitialized = false;
// Registrado UMA VEZ (guardado, como initChartControlsOnce) -- o
// container do gráfico é o mesmo <div> por toda a vida da página (só seus
// filhos são recriados a cada troca de símbolo/ensureChart()); registrar
// isso dentro de ensureChart() acumularia um listener duplicado por troca
// de símbolo (item 12 da matriz de testes: "listeners não se acumulam").
//
// Correção pós-auditoria (3ª rodada, item 3): o ciclo de Pointer Events
// precisa estar COMPLETO -- pointerdown inicia o gesto, pointerup e
// pointercancel o encerram (pointercancel é o que a auditoria apontou
// como faltante: dispara quando o navegador CANCELA o gesto no meio,
// ex.: o sistema interpreta um toque como rolagem de página em vez de
// arraste). mousedown/touchstart/mouseup/touchend/touchcancel continuam
// registrados (compatibilidade com mouse/touch mesmo sem Pointer Events)
// -- markChartPointerDown() e markChartPointerUp() são IDEMPOTENTES (a
// segunda chamada para o mesmo gesto físico, seja qual for o tipo de
// evento que a disparou, não muda nada: markChartPointerDown sempre seta
// true, e o `if (!pointerActive) return` de markChartPointerUp faz a
// segunda notificação de liberação ser um no-op seguro) -- disparar
// pointerdown E mousedown para o mesmo clique (comportamento padrão do
// navegador) nunca produz estado incorreto.
//
// blur/visibilitychange são uma rede de segurança ADICIONAL (nunca a
// via principal) para a garantia "pointerActive nunca fica preso em
// true": se a janela perder o foco ou a aba ficar oculta enquanto um
// gesto está em andamento (ex.: alt-tab no meio do arraste, sem nenhum
// mouseup/pointerup/touchend chegando a disparar), o gesto é encerrado
// mesmo assim.
function initChartInteractionTrackingOnce(container) {
  if (chartInteractionTrackingInitialized) return;
  chartInteractionTrackingInitialized = true;
  container.addEventListener("pointerdown", markChartPointerDown);
  container.addEventListener("mousedown", markChartPointerDown);
  container.addEventListener("touchstart", markChartPointerDown, { passive: true });
  container.addEventListener("wheel", markChartWheel, { passive: true });
  window.addEventListener("pointerup", markChartPointerUp);
  window.addEventListener("pointercancel", markChartPointerUp);
  window.addEventListener("mouseup", markChartPointerUp);
  window.addEventListener("touchend", markChartPointerUp, { passive: true });
  window.addEventListener("touchcancel", markChartPointerUp, { passive: true });
  window.addEventListener("blur", markChartPointerUp);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) markChartPointerUp();
  });
}

function setChartLiveIndicator(exploring) {
  const el = $("chart-live-indicator");
  if (!el) return;
  el.classList.toggle("exploring", exploring);
  const label = el.querySelector(".label");
  if (label) label.textContent = exploring ? "Explorando histórico" : "Acompanhando mercado";
  const backBtn = $("chart-back-to-live-btn");
  if (backBtn) backBtn.hidden = !exploring;
}

// Pure function, deliberately mirroring app/strategy/engine.py::StrategyEngine._sma
// (simple mean of the last `period` closes) -- kept standalone so it can be
// tested directly against a known fixture (item 13 da matriz de testes),
// independent of whether LightweightCharts is loaded.
function computeSMA(candles, period) {
  const out = [];
  for (let i = 0; i < candles.length; i++) {
    if (i + 1 < period) continue;
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += candles[j].close;
    out.push({ time: candles[i].time, value: sum / period });
  }
  return out;
}

// Fase 3.2: "1m" -> "1 min", "5m" -> "5 min". Nunca abrevia a ponto de
// deixar ambíguo qual das duas cadências está sendo mostrada.
function describeTimeframe(tf) {
  if (!tf) return "N/D";
  const minutes = parseInt(String(tf).replace("m", ""), 10);
  if (!Number.isFinite(minutes)) return String(tf);
  return `${minutes} min`;
}

// Fase 3.2: painel da visão estratégica. Todo texto via textContent
// (statCard já garante isso) -- nenhum innerHTML em lugar nenhum.
function renderStrategyPanel(body) {
  const grid = $("chart-strategy-grid");
  if (!grid) return;
  clearChildren(grid);

  const ind = body.strategy_indicators || {};
  const last = body.last_strategy_candle;
  const forming = body.forming_strategy_candle;
  const integrity = body.bucket_integrity || {};

  statCard(grid, `SMA rápida (${fmtInt(ind.fast_period)}) — média simples`, fmtRatioOrND(ind.fast_sma), {
    title: "Média móvel SIMPLES (SMA) do fechamento dos candles estratégicos. Não é EMA.",
  });
  statCard(grid, `SMA lenta (${fmtInt(ind.slow_period)}) — média simples`, fmtRatioOrND(ind.slow_sma), {
    title: "Média móvel SIMPLES (SMA) do fechamento dos candles estratégicos. Não é EMA.",
  });
  statCard(grid, "ATR (US$ por unidade)", fmtCurrency(ind.atr_per_unit_usd), {
    title: "Amplitude média real, em dólares POR UNIDADE do ativo -- só vira dinheiro depois de multiplicada pela quantidade.",
  });
  statCard(grid, "ATR (% do preço)", ind.atr_pct_of_price != null ? fmtPercent(ind.atr_pct_of_price * 100) : "N/D", {
    title: "O mesmo ATR expresso como fração do último fechamento -- é este valor que os filtros de volatilidade comparam.",
  });
  statCard(grid, "Último candle estratégico fechado", last ? new Date(last.open_time).toLocaleString("pt-BR") : "N/D", {
    title: "Abertura (UTC convertida) do último bucket COMPLETO que alimentou a estratégia.",
  });
  statCard(grid, "Candle estratégico em formação", formingLabel(forming), {
    cardClass: forming ? "stat-card-partial" : "",
    title: "Bucket ainda incompleto. Exibido apenas para acompanhamento -- nunca usado para decidir.",
  });
  statCard(grid, "Buckets incompletos", fmtInt(integrity.incomplete_buckets), {
    valueClass: integrity.incomplete_buckets ? "cost" : "",
    title: "Buckets estratégicos finalizados com candles de 1 minuto faltando. Não geram sinal e nunca têm OHLCV fabricado.",
  });
  statCard(grid, "Cobertura mínima dos custos (Minimum Cost Coverage Ratio)",
    body.cost_gate ? fmtRatio(body.cost_gate.required_ratio) : "N/D", {
    title: "Quantas vezes o movimento esperado precisa cobrir o custo estimado de ida e volta para uma entrada ser aprovada -- o Filtro de viabilidade da operação (Cost Gate). Valor lido AO VIVO da configuração deste processo em execução agora -- nunca um valor fixo ou de outra instância/ambiente.",
  });
  if (body.cost_gate && typeof body.cost_gate.avg_coverage_ratio_at_entry === "number") {
    statCard(grid, "Cobertura estimada dos custos (Cost Coverage)",
      fmtRatio(body.cost_gate.avg_coverage_ratio_at_entry), {
      title: "Média da cobertura de custo observada nos sinais avaliados deste símbolo -- quanto o movimento esperado cobriu o custo estimado, na média.",
    });
  }

  const note = $("chart-strategy-note");
  if (note) {
    note.textContent = forming
      ? `Bucket em formação: ${fmtInt(forming.received_slots)} de ${fmtInt(forming.expected_slots)} candles de 1 minuto recebidos — PARCIAL, ainda não fechado e não usado pela estratégia.`
      : "Nenhum bucket estratégico em formação no momento.";
  }
}

function formingLabel(forming) {
  if (!forming) return "N/D";
  return `PARCIAL ${fmtInt(forming.received_slots)}/${fmtInt(forming.expected_slots)}`;
}

function fmtRatioOrND(v) {
  return typeof v === "number" && Number.isFinite(v) ? fmtCurrency(v) : "N/D";
}

function chartWindowSeconds(windowKey) {
  const HOUR = 3600;
  return { "1h": HOUR, "4h": 4 * HOUR, "12h": 12 * HOUR, "24h": 24 * HOUR }[windowKey] || null;
}

function clearPositionOverlay() {
  CHART_STATE.priceLines.forEach((line) => {
    if (CHART_STATE.candleSeries) CHART_STATE.candleSeries.removePriceLine(line);
  });
  CHART_STATE.priceLines = [];
  CHART_STATE.zoneEls.forEach((el) => el.remove());
  CHART_STATE.zoneEls = [];
}

function repositionPriceZones() {
  const container = $("chart-container");
  if (!container || !CHART_STATE.candleSeries) return;
  CHART_STATE.zoneEls.forEach((el) => {
    const topPrice = Number(el.dataset.topPrice);
    const bottomPrice = Number(el.dataset.bottomPrice);
    const yTop = CHART_STATE.candleSeries.priceToCoordinate(topPrice);
    const yBottom = CHART_STATE.candleSeries.priceToCoordinate(bottomPrice);
    if (yTop == null || yBottom == null) { el.style.display = "none"; return; }
    el.style.display = "block";
    el.style.top = `${Math.min(yTop, yBottom)}px`;
    el.style.height = `${Math.max(2, Math.abs(yBottom - yTop))}px`;
  });
}

function addPriceZone(container, topPrice, bottomPrice, className) {
  const el = document.createElement("div");
  el.className = `chart-price-zone ${className}`;
  el.dataset.topPrice = String(topPrice);
  el.dataset.bottomPrice = String(bottomPrice);
  container.appendChild(el);
  CHART_STATE.zoneEls.push(el);
}

function applyPositionOverlay(position, visualPrice) {
  clearPositionOverlay();
  const container = $("chart-container");
  const stateChip = $("chart-position-state");
  const legend = $("chart-position-legend");
  clearChildren(legend);

  if (!position) {
    stateChip.textContent = "SEM POSIÇÃO";
    stateChip.classList.remove("state-pill", "state-ok", "state-warn", "state-bad");
    stateChip.classList.add("state-pill", "state-neutral");
    return;
  }
  stateChip.textContent = position.side === "BUY" ? "COMPRADO" : "VENDIDO";
  // Fase 3.6, item 7: posição aberta nunca é sinalizada em verde aqui --
  // ter uma posição aberta não é "bom" nem "ruim" por si só (o resultado
  // dela é mostrado separadamente em P&L não realizado); âmbar sinaliza
  // apenas "requer atenção", nunca lucro.
  stateChip.classList.remove("state-pill", "state-neutral", "state-ok", "state-bad");
  stateChip.classList.add("state-pill", "state-warn");

  const entry = position.avg_entry_price;
  const tp = position.take_profit;
  const sl = position.stop_loss;

  CHART_STATE.priceLines.push(CHART_STATE.candleSeries.createPriceLine({
    price: entry, color: "#60a5fa", lineWidth: 2, lineStyle: 0, title: "Entrada",
  }));
  if (typeof visualPrice === "number") {
    CHART_STATE.priceLines.push(CHART_STATE.candleSeries.createPriceLine({
      price: visualPrice, color: "#e6e9f0", lineWidth: 1, lineStyle: 2, title: "Atual",
    }));
  }
  if (tp != null) {
    CHART_STATE.priceLines.push(CHART_STATE.candleSeries.createPriceLine({
      price: tp, color: CHART_UP_COLOR, lineWidth: 2, lineStyle: 0, title: "Alvo (TP)",
    }));
    addPriceZone(container, entry, tp, "profit");
  }
  if (sl != null) {
    CHART_STATE.priceLines.push(CHART_STATE.candleSeries.createPriceLine({
      price: sl, color: CHART_DOWN_COLOR, lineWidth: 2, lineStyle: 0, title: "Stop (SL)",
    }));
    addPriceZone(container, entry, sl, "risk");
  }
  repositionPriceZones();

  kvRow(legend, "Quantidade", position.qty.toFixed(6));
  kvRow(legend, "Exposição (USD)", (position.qty * entry).toFixed(2));
  if (tp != null) kvRow(legend, "Distância até o alvo", Math.abs(tp - (visualPrice != null ? visualPrice : entry)).toFixed(2));
  if (sl != null) kvRow(legend, "Distância até o stop", Math.abs((visualPrice != null ? visualPrice : entry) - sl).toFixed(2));
  const openedAt = new Date(position.opened_at);
  const durationMin = Math.max(0, Math.round((Date.now() - openedAt.getTime()) / 60000));
  kvRow(legend, "Duração", `${durationMin} min`);
}

// Fase 3.6: guarda o range visível ATUAL do símbolo que está saindo de
// cena -- para, se o usuário voltar a ele durante a mesma sessão da
// página, a posição ser restaurada em vez de sempre reabrir do zero.
function saveCurrentSymbolViewState() {
  if (!CHART_STATE.chart || !CHART_STATE.symbol) return;
  // Defensivo: preservar a posição ao trocar de símbolo é uma
  // OTIMIZAÇÃO de conveniência (item "ao voltar para um símbolo já
  // visitado, restaurar a última posição") -- uma falha aqui nunca pode
  // interromper o restante de refreshChart()/ensureChart().
  try {
    const range = CHART_STATE.chart.timeScale().getVisibleRange();
    if (range) CHART_STATE.viewStateBySymbol.set(CHART_STATE.symbol, range);
  } catch (err) {
    // sem posição salva para este símbolo -- comportamento padrão
    // (abrir nos candles mais recentes) continua se aplicando.
  }
}

function ensureChart(symbol) {
  if (CHART_STATE.chart && CHART_STATE.symbol === symbol) return false;
  saveCurrentSymbolViewState();
  if (CHART_STATE.chart) {
    CHART_STATE.chart.remove();
    CHART_STATE.chart = null;
  }
  const container = $("chart-container");
  clearChildren(container);
  CHART_STATE.priceLines = [];
  CHART_STATE.zoneEls = [];
  CHART_STATE.lastCandles = [];
  CHART_STATE.followLive = true;
  CHART_STATE.pointerActive = false;
  CHART_STATE.lastPointerReleaseAt = 0;
  CHART_STATE.lastWheelAt = 0;
  setChartLiveIndicator(false);

  // Só uma interação real do usuário no canvas do gráfico pode marcar
  // "exploração" (ver wasRecentChartUserInput acima) -- um chart
  // recém-criado nunca teve nenhuma interação ainda, então nenhuma
  // recalculagem de range interna da biblioteca durante a configuração
  // inicial (setData, autoSize) pode ser malinterpretada como o usuário
  // explorando. Os listeners em si são globais e registrados uma única
  // vez (initChartInteractionTrackingOnce) -- nunca por chamada de
  // ensureChart(), que roda de novo a cada troca de símbolo.
  initChartInteractionTrackingOnce(container);

  const chart = LightweightCharts.createChart(container, {
    layout: { background: { color: "#0c1120" }, textColor: "#e6e9f0" },
    grid: { vertLines: { color: "#1c2333" }, horzLines: { color: "#1c2333" } },
    rightPriceScale: { borderColor: "#26314a" },
    // Correção Fase 3.6.1 (segunda causa raiz): shiftVisibleRangeOnNewBar
    // é `true` por padrão no Lightweight Charts -- a biblioteca desloca
    // sozinha a janela visível para frente sempre que um candle novo
    // chega (mesmo via update(), nunca setData()), sempre que o range
    // exibido ainda alcança onde estava o último candle no momento do
    // pan. Isso "vazava" o modo exploração de volta para perto do tempo
    // real a cada novo candle, mesmo já eliminado o setData() em loop
    // (comprovado em navegador real: range mudava a cada poll mesmo com
    // followLive=false e zero chamadas a setData()). O reancoramento ao
    // vivo já é feito explicitamente pelo próprio app.js
    // (applyChartWindow() quando followLive===true, em refreshChart()) --
    // a biblioteca nunca precisa fazer isso por conta própria.
    timeScale: {
      borderColor: "#26314a", timeVisible: true, secondsVisible: false,
      shiftVisibleRangeOnNewBar: false,
    },
    autoSize: true,
  });
  const candleSeries = chart.addCandlestickSeries({
    upColor: CHART_UP_COLOR, downColor: CHART_DOWN_COLOR,
    borderVisible: false, wickUpColor: CHART_UP_COLOR, wickDownColor: CHART_DOWN_COLOR,
  });
  const volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" }, priceScaleId: "volume",
  });
  // Fase 3.6, item 3: volume numa faixa PRÓPRIA, inferior, escala
  // separada -- nunca ocupa/encobre a região principal dos candles.
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  const smaFastSeries = chart.addLineSeries({ color: "#fbbf24", lineWidth: 1, title: "SMA rápida" });
  const smaSlowSeries = chart.addLineSeries({ color: "#60a5fa", lineWidth: 1, title: "SMA lenta" });

  // Fase 3.6: detecta EXPLORAÇÃO MANUAL -- só quando o range mudou logo
  // depois de uma interação real do usuário no canvas (drag/zoom/scroll),
  // nunca por uma recalculagem interna da biblioteca (ver
  // wasRecentChartUserInput acima).
  const visibleRangeHandler = () => {
    repositionPriceZones();
    if (!wasRecentChartUserInput()) return;
    if (!CHART_STATE.followLive) return;
    CHART_STATE.followLive = false;
    setChartLiveIndicator(true);
  };
  chart.timeScale().subscribeVisibleTimeRangeChange(visibleRangeHandler);
  CHART_STATE.visibleRangeHandler = visibleRangeHandler;

  const crosshairHandler = (param) => renderChartTooltip(param, container);
  chart.subscribeCrosshairMove(crosshairHandler);
  CHART_STATE.crosshairHandler = crosshairHandler;

  // autoSize:true já redimensiona a lib sozinha; o ResizeObserver aqui
  // só reposiciona as faixas de entrada/stop/alvo (que são <div>s nossos,
  // não geridos pela lib) -- nunca recria o chart.
  if (typeof ResizeObserver !== "undefined") {
    if (CHART_STATE.resizeObserver) CHART_STATE.resizeObserver.disconnect();
    CHART_STATE.resizeObserver = new ResizeObserver(repositionPriceZones);
    CHART_STATE.resizeObserver.observe(container);
  }

  CHART_STATE.chart = chart;
  CHART_STATE.candleSeries = candleSeries;
  CHART_STATE.volumeSeries = volumeSeries;
  CHART_STATE.smaFastSeries = smaFastSeries;
  CHART_STATE.smaSlowSeries = smaSlowSeries;
  CHART_STATE.symbol = symbol;
  return true;
}

// `fitContent()`/`setVisibleRange()` só devem rodar: (1) na primeira
// carga válida de um símbolo, ou (2) mediante ação explícita do usuário
// (trocar o intervalo, clicar "Voltar ao tempo atual"/"Redefinir zoom").
// NUNCA a cada atualização automática enquanto o usuário está explorando.
function applyChartWindow(windowKey) {
  CHART_STATE.activeWindow = windowKey;
  document.querySelectorAll(".chart-window-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.window === windowKey);
  });
  if (!CHART_STATE.chart || !CHART_STATE.lastCandles.length) return;
  if (windowKey === "all") {
    CHART_STATE.chart.timeScale().fitContent();
  } else {
    const seconds = chartWindowSeconds(windowKey);
    const lastTime = CHART_STATE.lastCandles[CHART_STATE.lastCandles.length - 1].time;
    CHART_STATE.chart.timeScale().setVisibleRange({ from: lastTime - seconds, to: lastTime + 60 });
  }
}

function returnToLiveView() {
  CHART_STATE.followLive = true;
  setChartLiveIndicator(false);
  applyChartWindow(CHART_STATE.activeWindow);
}

function resetChartZoom() {
  if (!CHART_STATE.chart) return;
  CHART_STATE.chart.timeScale().fitContent();
}

function toggleChartExpanded() {
  const card = $("chart-card");
  if (!card) return;
  const expanding = !card.classList.contains("chart-expanded");
  card.classList.toggle("chart-expanded", expanding);
  document.body.classList.toggle("chart-expanded-active", expanding);
  const btn = $("chart-expand-btn");
  if (btn) btn.textContent = expanding ? "Sair da tela cheia" : "Expandir gráfico";
}

let chartControlsInitialized = false;

function initChartControlsOnce() {
  if (chartControlsInitialized) return;
  chartControlsInitialized = true;
  document.querySelectorAll(".chart-window-btn").forEach((btn) => {
    // Trocar o intervalo é uma ação EXPLÍCITA do usuário -- volta ao modo
    // ao vivo (item 2 do brief: "Ao clicar em Voltar ao tempo atual, o
    // gráfico deve... reativar o acompanhamento automático"; o mesmo vale
    // para escolher um novo intervalo, que é igualmente uma decisão
    // explícita de "para onde olhar agora").
    btn.addEventListener("click", () => {
      CHART_STATE.followLive = true;
      setChartLiveIndicator(false);
      applyChartWindow(btn.dataset.window);
    });
  });
  const backBtn = $("chart-back-to-live-btn");
  if (backBtn) backBtn.addEventListener("click", returnToLiveView);
  const resetBtn = $("chart-reset-zoom-btn");
  if (resetBtn) resetBtn.addEventListener("click", resetChartZoom);
  const expandBtn = $("chart-expand-btn");
  if (expandBtn) expandBtn.addEventListener("click", toggleChartExpanded);
  const select = $("chart-symbol-select");
  if (select) {
    // Reação IMEDIATA à troca de símbolo -- antes dependia inteiramente
    // do próximo tick do polling de 2s.
    select.addEventListener("change", () => refreshChart());
  }
}

// Fase 3.6, item 3: tooltip/crosshair com data e hora (fuso explícito),
// OHLCV, médias e direção do sinal quando existir. Nunca inventa dado
// ausente -- campo indisponível aparece como "Não disponível". Todo texto
// via textContent, nunca innerHTML (item 8 do brief).
const CHART_TZ_LABEL = "UTC" + (-new Date().getTimezoneOffset() >= 0 ? "+" : "-")
  + String(Math.floor(Math.abs(-new Date().getTimezoneOffset()) / 60)).padStart(2, "0");

function ensureTooltipEl(container) {
  if (CHART_STATE.tooltipEl) return CHART_STATE.tooltipEl;
  const el = document.createElement("div");
  el.className = "chart-tooltip";
  el.hidden = true;
  container.appendChild(el);
  CHART_STATE.tooltipEl = el;
  return el;
}

function ttRow(container, label, value) {
  const row = document.createElement("div");
  row.className = "tt-row";
  const l = document.createElement("span");
  l.className = "tt-label";
  l.textContent = label;
  const v = document.createElement("span");
  v.className = "tt-value";
  v.textContent = value;
  row.appendChild(l);
  row.appendChild(v);
  container.appendChild(row);
}

function findMarkerForTime(time) {
  return (CHART_STATE.lastMarkers || []).find((m) => m.time === time) || null;
}

function renderChartTooltip(param, container) {
  const tooltip = ensureTooltipEl(container);
  if (!param || !param.time || !param.point || !CHART_STATE.candleSeries) {
    tooltip.hidden = true;
    return;
  }
  const candleData = param.seriesData && param.seriesData.get(CHART_STATE.candleSeries);
  if (!candleData) {
    tooltip.hidden = true;
    return;
  }
  clearChildren(tooltip);
  const dt = new Date(param.time * 1000);
  ttRow(tooltip, "Data e hora", `${dt.toLocaleString("pt-BR")} (${CHART_TZ_LABEL})`);
  ttRow(tooltip, "Abertura", fmtCurrency(candleData.open));
  ttRow(tooltip, "Máxima", fmtCurrency(candleData.high));
  ttRow(tooltip, "Mínima", fmtCurrency(candleData.low));
  ttRow(tooltip, "Fechamento", fmtCurrency(candleData.close));
  const volData = CHART_STATE.volumeSeries && param.seriesData.get(CHART_STATE.volumeSeries);
  ttRow(tooltip, "Volume", volData ? fmtNumber(volData.value, 3) : "Não disponível");
  const fastData = CHART_STATE.smaFastSeries && param.seriesData.get(CHART_STATE.smaFastSeries);
  ttRow(tooltip, "Média rápida", fastData ? fmtCurrency(fastData.value) : "Não disponível");
  const slowData = CHART_STATE.smaSlowSeries && param.seriesData.get(CHART_STATE.smaSlowSeries);
  ttRow(tooltip, "Média lenta", slowData ? fmtCurrency(slowData.value) : "Não disponível");
  const marker = findMarkerForTime(param.time);
  ttRow(tooltip, "Sinal", marker ? marker.rawDirection : "Não disponível");

  tooltip.hidden = false;
  const containerWidth = container.clientWidth;
  const tooltipWidth = 200;
  let left = param.point.x + 16;
  if (left + tooltipWidth > containerWidth) left = param.point.x - tooltipWidth - 16;
  tooltip.style.left = `${Math.max(4, left)}px`;
  tooltip.style.top = `${Math.max(4, param.point.y - 10)}px`;
}

// Ordena ascendente e remove timestamps duplicados (mantém o último valor
// recebido para cada timestamp) -- defensivo mesmo que o backend já
// entregue ordenado, nunca confia cegamente numa premissa não garantida
// pelo contrato.
function sanitizeCandles(rawCandles) {
  const byTime = new Map();
  rawCandles.forEach((c) => byTime.set(c.time, c));
  return Array.from(byTime.values()).sort((a, b) => a.time - b.time);
}

// Compara dois pontos de dados IGNORANDO `time` -- funciona tanto para
// candles (open/high/low/close) quanto para volume ({value, color}) e
// médias ({value}), os três formatos que passam por applyCandlesToSeries.
function sameDataPoint(a, b) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  keys.delete("time");
  for (const k of keys) {
    if (a[k] !== b[k]) return false;
  }
  return true;
}

// Função PURA (nunca toca o Lightweight Charts) que decide como aplicar
// `nextMapped` sobre uma série que já tem `prevMapped` desenhado.
// Devolve `{ reset: false, updates: [...] }` (update() incremental,
// nunca move o viewport) ou `{ reset: true, reason: "..." }` (setData()
// necessário).
//
// Correção Fase 3.6.1, rodada 1 (causa raiz do pan não persistir): a
// rota /api/chart-data devolve uma JANELA DESLIZANTE limitada a `limit`
// candles. Assim que o histórico total ultrapassa esse limite -- o caso
// normal depois de a V2 rodar por mais de ~25h contínuas -- cada
// atualização derruba o candle mais antigo da janela. Comparar por
// POSIÇÃO/ÍNDICE (versão anterior) falhava em todo ciclo assim que isso
// acontecia, forçando setData() continuamente (comprovado em navegador
// real: 9 chamadas a setData() em 9s, 0 a update()) -- e setData() no
// Lightweight Charts real preserva a posição LÓGICA a partir da borda
// direita, não o intervalo de tempo visível, arrastando a janela visível
// para frente mesmo com o usuário explorando o histórico.
//
// Correção Fase 3.6.1, rodada 2 (endurecimento): "há alguma sobreposição"
// sozinho é permissivo demais -- não distinguia um deslizamento normal
// de uma DIVERGÊNCIA ESTRUTURAL real (lacuna no meio, candle histórico
// removido, OHLCV histórico reescrito, timestamp duplicado ou fora de
// ordem). Um deslizamento normal preserva, para cada candle de
// `prevMapped` que ainda cabe na janela nova, o mesmo tempo E o mesmo
// OHLCV (exceto o último, que pode estar em formação) -- qualquer
// violação disso é tratada como divergência estrutural (setData(),
// nunca update() com dado incoerente na tela).
function classifyCandlesUpdate(prevMapped, nextMapped) {
  if (prevMapped.length === 0) return { reset: true, reason: "primeira carga" };
  if (nextMapped.length === 0) return { reset: false, updates: [] };

  for (let i = 1; i < nextMapped.length; i++) {
    if (nextMapped[i].time === nextMapped[i - 1].time) {
      return { reset: true, reason: `timestamp duplicado na resposta (time=${nextMapped[i].time})` };
    }
    if (nextMapped[i].time < nextMapped[i - 1].time) {
      return { reset: true, reason: `timestamps fora de ordem na resposta (time=${nextMapped[i].time})` };
    }
  }

  const prevMaxTime = prevMapped[prevMapped.length - 1].time;
  const nextMinTime = nextMapped[0].time;
  const nextMaxTime = nextMapped[nextMapped.length - 1].time;
  if (nextMinTime > prevMaxTime) {
    return { reset: true, reason: "lacuna sem nenhuma sobreposição com o que já estava desenhado" };
  }

  // Todo candle de `prevMapped` que ainda deveria caber na janela nova
  // (não "caiu" pela frente por deslizamento normal) precisa continuar
  // presente, com o mesmo OHLCV -- exceto o próprio último candle
  // conhecido, que pode estar em formação.
  const nextByTime = new Map(nextMapped.map((c) => [c.time, c]));
  const stillExpected = prevMapped.filter((p) => p.time >= nextMinTime && p.time <= nextMaxTime);
  for (const p of stillExpected) {
    const n = nextByTime.get(p.time);
    if (!n) return { reset: true, reason: `candle histórico removido da janela (time=${p.time})` };
    if (p.time !== prevMaxTime && !sameDataPoint(p, n)) {
      return { reset: true, reason: `OHLCV histórico alterado (time=${p.time})` };
    }
  }

  // Todo candle de `nextMapped` dentro do intervalo que `prevMapped` já
  // cobria precisa já existir em `prevMapped` -- senão é um candle
  // inserido no meio de uma série já conhecida (lacuna estrutural
  // interna), não um deslizamento pela borda.
  const prevByTime = new Map(prevMapped.map((c) => [c.time, c]));
  const prevMinTime = prevMapped[0].time;
  for (const n of nextMapped) {
    if (n.time < prevMinTime || n.time > prevMaxTime) continue;
    if (!prevByTime.has(n.time)) {
      return { reset: true, reason: `candle novo inserido no meio da série já conhecida (time=${n.time})` };
    }
  }

  return { reset: false, updates: nextMapped.filter((n) => n.time >= prevMaxTime) };
}

// Aplica o plano de `classifyCandlesUpdate()` -- só esta função toca o
// Lightweight Charts. Quando um reset é necessário e o usuário está
// explorando o histórico (`followLive === false`), o intervalo de tempo
// visível é capturado ANTES do setData() e restaurado exatamente depois
// -- setData() nunca pode ser a causa de perder a posição do pan, mesmo
// quando é genuinamente necessário (divergência estrutural real). Em
// modo ao vivo, nada é restaurado aqui: o próprio refreshChart() já
// reancora a visão ao vivo logo em seguida, normalmente.
function resetSeriesPreservingView(series, chart, nextMapped) {
  let savedRange = null;
  if (chart && CHART_STATE.followLive === false) {
    try {
      savedRange = chart.timeScale().getVisibleRange();
    } catch (err) {
      savedRange = null;
    }
  }
  series.setData(nextMapped);
  if (savedRange) {
    try {
      chart.timeScale().setVisibleRange(savedRange);
    } catch (err) {
      // Melhor esforço -- uma falha aqui nunca pode interromper o resto
      // de refreshChart() (mesmo padrão já usado em saveCurrentSymbolViewState()).
    }
  }
}

function applyCandlesToSeries(series, mapFn, prevCandles, nextCandles, chart) {
  const prevMapped = prevCandles.map(mapFn);
  const nextMapped = nextCandles.map(mapFn);
  const plan = classifyCandlesUpdate(prevMapped, nextMapped);
  if (plan.reset) {
    resetSeriesPreservingView(series, chart, nextMapped);
    return;
  }
  plan.updates.forEach((point) => series.update(point));
}

async function refreshChartSymbolOptions() {
  const select = $("chart-symbol-select");
  if (select.dataset.loaded === "1") return;
  const { symbols } = await getJSON("/api/symbols");
  clearChildren(select);
  symbols.forEach((symbol) => {
    const opt = document.createElement("option");
    opt.value = symbol;
    opt.textContent = symbolDisplayName(symbol);
    select.appendChild(opt);
  });
  if (symbols.length) select.dataset.loaded = "1";
}

async function refreshChart() {
  if (typeof LightweightCharts === "undefined") return;
  // Fase 3.6, item 2: pausa quando a aba está oculta -- retomando sem
  // perder a posição quando ela volta a ficar visível (mesmo padrão já
  // usado em frontend/shadow.html).
  if (document.hidden) return;
  try {
    await refreshChartSymbolOptions();
    initChartControlsOnce();
    const select = $("chart-symbol-select");
    const symbol = select.value;
    if (!symbol) return;

    const requestedSymbol = symbol;
    const body = await getJSON(`/api/chart-data?symbol=${encodeURIComponent(symbol)}&limit=1500`);
    // Guarda contra corrida de troca de símbolo (item 11 da matriz de
    // testes): se o usuário trocou de símbolo enquanto esta resposta
    // estava a caminho, descarta -- nunca aplica dados do símbolo errado.
    if (select.value !== requestedSymbol) return;

    const isNewSymbol = ensureChart(symbol);
    // Fase 3.2: banner do modo EFETIVO e aviso de dados sintéticos --
    // ambos vindos do backend, nunca deduzidos aqui.
    const banner = $("chart-banner");
    if (banner && body.chart_banner) banner.textContent = body.chart_banner;
    const disclaimer = $("chart-data-disclaimer");
    if (disclaimer) {
      if (body.data_disclaimer) {
        disclaimer.textContent = body.data_disclaimer;
        disclaimer.hidden = false;
      } else {
        disclaimer.textContent = "";
        disclaimer.hidden = true;
      }
    }

    // Fase 3.2: mercado e estratégia SEMPRE rotulados lado a lado, nunca
    // um "TF" ambíguo que pudesse ser confundido com o outro.
    $("chart-timeframe").textContent = `Mercado: ${describeTimeframe(body.market_data_timeframe || body.timeframe)}`;
    $("chart-strategy-timeframe").textContent = `Estratégia: ${describeTimeframe(body.strategy_timeframe)}`;
    const warm = body.warmup || {};
    const warmupChip = $("chart-warmup");
    warmupChip.textContent = warm.required != null
      ? `Aquecimento: ${fmtInt(warm.have)}/${fmtInt(warm.required)} ${warm.ready ? "(pronto)" : "(aquecendo)"}`
      : "Aquecimento: N/D";
    warmupChip.classList.remove("state-pill", "state-ok", "state-warn", "state-neutral");
    if (warm.required != null) {
      warmupChip.classList.add("state-pill", warm.ready ? "state-ok" : "state-warn");
    }

    const healthStatus = (body.symbol_health || {}).status;
    const statusChip = $("chart-status");
    statusChip.textContent = `Status: ${SYMBOL_HEALTH_LABELS[healthStatus] || "indisponível"}`;
    statusChip.classList.remove("state-pill", "state-ok", "state-warn", "state-bad", "state-neutral");
    // Fase 3.6, item 7: distinguir saudável/degradado/indisponível --
    // nunca ambos exibidos com a mesma cor neutra do chip padrão.
    const HEALTH_STATE_CLASS = {
      SAUDAVEL: "state-ok", DEGRADADO: "state-warn",
      PARADO: "state-bad", ENCERRANDO: "state-bad", INICIANDO: "state-neutral",
    };
    statusChip.classList.add("state-pill", HEALTH_STATE_CLASS[healthStatus] || "state-neutral");
    $("chart-visual-price").textContent = body.visual_price != null
      ? `Preço: ${body.visual_price.toFixed(2)} (${body.visual_price_source === "forming_candle" ? "ao vivo, candle em formação" : "último fechamento"})`
      : "Preço: indisponível";
    const formingChip = $("chart-forming-indicator");
    if (formingChip) {
      formingChip.hidden = body.visual_price_source !== "forming_candle";
    }
    CHART_STATE.lastSuccessAt = Date.now();
    const lastUpdatedChip = $("chart-last-updated");
    if (lastUpdatedChip) {
      lastUpdatedChip.textContent = `Atualizado: ${new Date().toLocaleTimeString("pt-BR")}`;
      lastUpdatedChip.classList.remove("stale-state");
    }

    // Fase 3.6, itens 1/2: nunca setData() incondicional -- só quando a
    // base anterior realmente mudou (primeira carga, troca de símbolo,
    // gap). Candle novo ou candle em formação alterado usam update(),
    // que nunca move o viewport.
    const candles = sanitizeCandles(body.candles);
    const prevCandles = CHART_STATE.lastCandles;
    applyCandlesToSeries(CHART_STATE.candleSeries,
      (c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }),
      prevCandles, candles, CHART_STATE.chart);
    applyCandlesToSeries(CHART_STATE.volumeSeries,
      (c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? CHART_UP_COLOR : CHART_DOWN_COLOR }),
      prevCandles, candles, CHART_STATE.chart);
    CHART_STATE.lastCandles = candles;

    const cfg = body.strategy_config || { fast_period: 9, slow_period: 21 };
    // Fase 3.2: as médias desenhadas são as da ESTRATÉGIA -- calculadas
    // sobre os candles estratégicos COMPLETOS (nunca sobre os de 1 minuto,
    // que produziriam uma linha que a estratégia nunca enxergou, e nunca
    // sobre um bucket parcial). São médias SIMPLES (SMA), jamais EMA.
    const strategyCandles = (body.strategy_candles || []).filter((c) => c.complete);
    const smaSource = strategyCandles.map((c) => ({
      time: Math.floor(Date.parse(c.open_time) / 1000), close: c.close,
    }));
    const prevSmaFast = CHART_STATE.lastSmaSource || [];
    const nextSmaFast = computeSMA(smaSource, cfg.fast_period);
    const nextSmaSlow = computeSMA(smaSource, cfg.slow_period);
    applyCandlesToSeries(CHART_STATE.smaFastSeries, (p) => p, CHART_STATE.lastSmaFast || [], nextSmaFast, CHART_STATE.chart);
    applyCandlesToSeries(CHART_STATE.smaSlowSeries, (p) => p, CHART_STATE.lastSmaSlow || [], nextSmaSlow, CHART_STATE.chart);
    CHART_STATE.lastSmaFast = nextSmaFast;
    CHART_STATE.lastSmaSlow = nextSmaSlow;

    const markers = (body.recent_signals || []).map((s) => ({
      time: s.time,
      position: s.direction === "BUY" ? "belowBar" : "aboveBar",
      color: s.direction === "BUY" ? CHART_UP_COLOR : CHART_DOWN_COLOR,
      shape: s.direction === "BUY" ? "arrowUp" : "arrowDown",
      text: `${translateDirection(s.direction)}${s.order_status ? ` (${ORDER_STATUS_LABELS[s.order_status] || s.order_status})` : ""}`,
      rawDirection: translateDirection(s.direction),
    }));
    CHART_STATE.candleSeries.setMarkers(markers);
    CHART_STATE.lastMarkers = markers;

    applyPositionOverlay(body.position, body.visual_price);
    renderStrategyPanel(body);

    // Fase 3.6: nunca reancora a janela visível enquanto o usuário está
    // explorando o histórico -- só na primeira carga do símbolo (restaura
    // a posição salva desta sessão, se houver, senão abre nos candles
    // mais recentes) ou quando o modo ao vivo está ativo.
    if (isNewSymbol) {
      const savedRange = CHART_STATE.viewStateBySymbol.get(symbol);
      if (savedRange) {
        CHART_STATE.chart.timeScale().setVisibleRange(savedRange);
        CHART_STATE.followLive = false;
        setChartLiveIndicator(true);
      } else {
        applyChartWindow(CHART_STATE.activeWindow);
      }
    } else if (CHART_STATE.followLive) {
      applyChartWindow(CHART_STATE.activeWindow);
    }
    // Modo exploração (followLive === false, símbolo já carregado): não
    // toca o range -- os dados foram atualizados via update() acima, sem
    // nenhum efeito colateral na posição/zoom do usuário.

    const loading = $("chart-loading");
    if (loading) loading.remove();
    const errorEl = $("chart-error");
    if (errorEl) errorEl.remove();
  } catch (err) {
    // Falha do gráfico nunca derruba o restante do painel (item 16 da
    // matriz de testes) -- Promise.all em refreshAll() nunca vê esta
    // rejeição.
    const container = $("chart-container");
    if (container && !document.getElementById("chart-error")) {
      const msg = document.createElement("div");
      msg.id = "chart-error";
      msg.className = "chart-message chart-error error-state";
      msg.textContent = "Erro ao carregar o gráfico -- os demais painéis continuam funcionando normalmente.";
      container.appendChild(msg);
    }
    // Fase 3.6, item 7: se já havia um gráfico carregado com sucesso
    // antes desta falha, o horário exibido passa a ser explicitamente
    // marcado como desatualizado -- nunca fica parado sem indicar isso.
    const lastUpdatedChip = $("chart-last-updated");
    if (lastUpdatedChip && CHART_STATE.lastSuccessAt) {
      lastUpdatedChip.classList.add("stale-state");
    }
  }
}

async function refreshRisk() {
  const rows = await getJSON("/api/risk-evaluations?limit=20");
  setRows(
    document.querySelector("#risk-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      { text: r.approved ? "APROVADO" : "REJEITADO", className: r.approved ? "positive" : "negative" },
      r.reason,
    ])
  );
}

async function refreshAI() {
  const rows = await getJSON("/api/ai-recommendations?limit=20");
  setRows(
    document.querySelector("#ai-table tbody"),
    rows.map((r) => [
      new Date(r.created_at).toLocaleString("pt-BR"),
      r.symbol,
      translateDirection(r.recommendation),
      r.confidence.toFixed(2),
      r.reasoning_summary,
    ])
  );
}

async function refreshFailures() {
  const rows = await getJSON("/api/failures?limit=20");
  setRows(
    document.querySelector("#failures-table tbody"),
    rows.map((r) => [new Date(r.created_at).toLocaleString("pt-BR"), r.kind, r.detail])
  );
}

async function refreshEquityCurve() {
  const points = await getJSON("/api/equity-curve");
  const canvas = $("equity-canvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) {
    ctx.fillStyle = "#8b96ab";
    ctx.fillText("Sem operações encerradas ainda.", 10, h / 2);
    return;
  }
  const values = points.map((p) => p.equity);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = 10;
  const scaleX = (w - 2 * pad) / (points.length - 1);
  const scaleY = max === min ? 1 : (h - 2 * pad) / (max - min);

  ctx.strokeStyle = "#60a5fa";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = pad + i * scaleX;
    const y = h - pad - (p.equity - min) * scaleY;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// Fase 3.6, item 6: "Por que nenhuma operação foi aberta?" -- SEMPRE com
// dados reais (sinais encontrados, quantos passaram, maior cobertura
// observada, cobertura mínima exigida, motivo dominante), nunca
// afirmando lucratividade/validação/calibração sem evidência.
// Correção pós-auditoria (item 1/2 da segunda rodada): os TOTAIS agora
// vêm sempre de /api/metrics -- contagens CANÔNICAS, direto do banco
// (repo.signal_counts/rejection_reasons/cost_gate_stats), nunca de um
// endpoint paginado como /api/risk-evaluations?limit=N lido como se fosse
// o total. Esse exato erro já tinha sido identificado e corrigido no
// backend na Fase 3.3 (ver comentário em app/api/routes_dashboard.py:
// "foi assim que a auditoria da Fase 3.3 reportou 200 sinais quando eram
// 257") -- e este painel repetiu o mesmo erro no frontend. A amostra
// paginada (`/api/risk-evaluations?limit=200`) só é usada agora para
// DECOMPOR tipo/sessão das aprovações recentes, nunca para nenhum total.
const REJECTION_CHECK_LABELS = {
  kill_switch_engaged: "Bloqueio de emergência ativo",
  trading_blocked: "Novas entradas pausadas pelo operador",
  state_not_ambiguous: "Estado ambíguo em relação à corretora (reconciliação)",
  data_reception_recent: "Dados de mercado desatualizados",
  clock_synced: "Relógio dessincronizado com a corretora",
  api_failures_ok: "Falhas repetidas de comunicação com a corretora",
  reconciliation_not_stale: "Reconciliação com a corretora atrasada",
  operational_state_active: "Novas entradas não autorizadas (aguardando ativação do operador)",
  engine_not_degraded: "Motor de mercado degradado",
  cooldown_expired: "Em período de espera após perdas (cooldown)",
  actionable_signal: "Sinal não acionável (AGUARDAR)",
  signal_is_fresh: "Sinal desatualizado no momento da avaliação",
  stop_loss_present: "Stop-loss obrigatório ausente no sinal",
  daily_loss_within_limit: "Limite de perda diária atingido",
  concurrent_positions_ok: "Limite de posições simultâneas atingido",
  exposure_room_available: "Capital em posições no limite (Exposição)",
  position_size_positive: "Tamanho de posição calculado como zero",
  minimum_order_notional_ok: "Valor da ordem abaixo do mínimo permitido",
  cost_coverage_ok: "Cobertura de custo insuficiente (Filtro de viabilidade da operação)",
  position_exists: "Não havia posição aberta para fechar",
  close_side_valid: "Lado de fechamento inválido",
  qty_positive: "Quantidade de fechamento inválida",
  qty_within_position: "Quantidade de fechamento maior que a posição aberta",
  "(checks ilegivel)": "Registro de decisão corrompido/ilegível",
  "(motivo nao registrado)": "Motivo não registrado",
};
function rejectionCheckLabel(checkName) {
  if (!checkName) return "Motivo não registrado";
  return REJECTION_CHECK_LABELS[checkName] || `Outro motivo (${checkName})`;
}

// Distingue ABERTURA de ENCERRAMENTO sem nenhuma coluna nova no banco:
// só a abertura (RiskEngine.evaluate) roda o filtro de custos e SEMPRE
// grava checks.cost_gate (mesmo desligado, como {"applied": false}); o
// encerramento (RiskEngine.evaluate_close -- chamado tanto por stop/alvo
// quanto por fechamento de posição oposta) nunca consulta o gate de
// custos e por isso nunca grava essa chave. Comprovado lendo
// app/risk/engine.py (evaluate() sempre seta checks["cost_gate"];
// evaluate_close() nunca).
function isEntryEvaluation(checks) {
  return !!checks && Object.prototype.hasOwnProperty.call(checks, "cost_gate");
}

// Mesma lógica de app/persistence/repo.py::rejection_reasons() (o motivo
// é o primeiro check cujo valor é EXATAMENTE `false` -- nunca um objeto
// como `cost_gate`, que é sempre verdadeiro/truthy mesmo quando o gate
// bloqueou), agora calculada no cliente porque esta correção passou a
// classificar o motivo SEPARADAMENTE por tipo (entrada vs. encerramento
// -- nunca misturados, item 1 da 3ª auditoria). A ordem das chaves no
// `checks` vem do MESMO JSON persistido que o backend já lê com
// `json.loads` -- ordem de inserção preservada nos dois lados.
function dominantCheckFailure(checks) {
  if (!checks || typeof checks !== "object") return "(checks ilegivel)";
  const failed = Object.keys(checks).filter((k) => checks[k] === false);
  return failed.length ? failed[0] : "(motivo nao registrado)";
}

// Correção pós-auditoria (3ª rodada, item 2): "amostra de 200" era um
// número arbitrário da paginação do endpoint, sem relação com o total
// real -- por isso a comparação "15 de 200" contra o histórico de 565
// confundia populações diferentes. Agora a busca é DIMENSIONADA pelo
// total canônico (rejection_reasons.evaluations_total, de /api/metrics):
// sempre que o histórico cabe dentro do teto de segurança abaixo, a
// "amostra" passa a ser 100% do histórico -- nunca mais uma fração não
// identificada dele. Só históricos genuinamente maiores que o teto (não
// o caso desta operação, com algumas centenas de avaliações) caem de
// volta numa amostra -- e nesse caso o painel avisa isso explicitamente.
const WHY_NO_TRADE_EVAL_FETCH_CAP = 5000;
// "Poucas" ORDENS DE ABERTURA EXECUTADAS nesta sessão -- correção
// pós-auditoria (4a rodada): o limiar é sobre execução real, não sobre
// aprovação (uma aprovação não garante execução). Deliberadamente baixo
// (só distingue "zero" de "uma ou duas" de "vários", não é uma
// classificação estatística) -- ver título dinâmico abaixo.
const WHY_NO_TRADE_FEW_ORDERS_THRESHOLD = 3;

async function refreshWhyNoTrades() {
  const box = $("why-no-trade-box");
  const titleEl = $("why-no-trade-title");
  if (!box) return;

  const metrics = await getJSON("/api/metrics");
  const signalCounts = metrics.signal_counts || {};
  const rejectionInfo = metrics.rejection_reasons || {};
  const costGate = metrics.cost_gate || {};
  const dist = costGate.coverage_distribution || {};
  const evaluationsTotal = rejectionInfo.evaluations_total || 0;
  const approvedTotal = rejectionInfo.approved_total || 0;

  const evalFetchLimit = Math.min(evaluationsTotal + 20, WHY_NO_TRADE_EVAL_FETCH_CAP);
  const isCompleteHistory = evalFetchLimit >= evaluationsTotal;
  const ordersFetchLimit = Math.min(Math.max(approvedTotal * 2 + 50, 200), WHY_NO_TRADE_EVAL_FETCH_CAP);

  const [sessionInfo, allEvals, allOrders, positions] = await Promise.all([
    getJSON("/api/session"),
    getJSON(`/api/risk-evaluations?limit=${evalFetchLimit}`),
    getJSON(`/api/orders?limit=${ordersFetchLimit}`),
    getJSON("/api/positions"),
  ]);

  const sessionStartedAt = sessionInfo ? new Date(sessionInfo.started_at) : null;

  // Item 1 da auditoria: cada avaliação pertence a EXATAMENTE uma
  // população (abertura OU encerramento) -- nunca somadas na mesma
  // contagem, nunca o motivo de recusa de uma misturado com o da outra.
  let entryTotal = 0, entryApproved = 0, entryRejected = 0, entryApprovedInSession = 0;
  let closeTotal = 0, closeApproved = 0, closeRejected = 0, closeApprovedInSession = 0;
  const entryRejectReasons = new Map();
  const closeRejectReasons = new Map();
  let oldestEvalAt = null, newestEvalAt = null;

  allEvals.forEach((e) => {
    const isEntry = isEntryEvaluation(e.checks);
    const isCurrent = sessionStartedAt ? new Date(e.created_at) >= sessionStartedAt : false;
    if (!oldestEvalAt || e.created_at < oldestEvalAt) oldestEvalAt = e.created_at;
    if (!newestEvalAt || e.created_at > newestEvalAt) newestEvalAt = e.created_at;
    const reasonMap = isEntry ? entryRejectReasons : closeRejectReasons;
    if (isEntry) entryTotal++; else closeTotal++;
    if (e.approved) {
      if (isEntry) { entryApproved++; if (isCurrent) entryApprovedInSession++; }
      else { closeApproved++; if (isCurrent) closeApprovedInSession++; }
    } else {
      if (isEntry) entryRejected++; else closeRejected++;
      const reason = dominantCheckFailure(e.checks);
      reasonMap.set(reason, (reasonMap.get(reason) || 0) + 1);
    }
  });

  function topReason(map) {
    let best = null, bestN = 0;
    map.forEach((n, r) => { if (n > bestN) { best = r; bestN = n; } });
    return best ? { reason: best, count: bestN } : null;
  }
  const entryDominant = topReason(entryRejectReasons);
  const closeDominant = topReason(closeRejectReasons);

  const openOrdersFilled = allOrders.filter((o) => !o.is_close && o.status === "FILLED").length;
  const closeOrdersFilled = allOrders.filter((o) => o.is_close && o.status === "FILLED").length;
  // Correção pós-auditoria (4a rodada, item 1): o título NUNCA pode
  // afirmar quantas operações foram ABERTAS usando "entradas aprovadas"
  // -- aprovação é uma autorização, não a operação em si (ver o próprio
  // texto da auditoria: "isso preserva a verdade caso futuramente uma
  // aprovação não resulte em execução"). Sempre a ORDEM DE ABERTURA
  // efetivamente EXECUTADA (status FILLED, is_close=false) NESTA sessão
  // -- nunca uma ordem de ENCERRAMENTO (is_close=true nunca conta aqui,
  // fechar uma posição não é abrir uma operação nova).
  const openOrdersFilledInSession = allOrders.filter((o) => {
    if (o.is_close || o.status !== "FILLED") return false;
    return sessionStartedAt ? new Date(o.created_at) >= sessionStartedAt : false;
  }).length;
  const positionsOpenCount = Array.isArray(positions) ? positions.length : 0;

  // Item 1 da auditoria: título NUNCA fixo -- "nenhuma operação foi
  // aberta" só aparece quando os dados REALMENTE mostram zero ORDENS DE
  // ABERTURA EXECUTADAS nesta sessão; caso contrário o título reflete
  // isso. "Entradas aprovadas" continua exibido separadamente abaixo
  // (nunca removido), mas nunca mais decide o título sozinho.
  if (titleEl) {
    if (openOrdersFilledInSession === 0) {
      titleEl.textContent = "Por que nenhuma operação foi aberta nesta sessão?";
    } else if (openOrdersFilledInSession < WHY_NO_TRADE_FEW_ORDERS_THRESHOLD) {
      titleEl.textContent = "Por que poucas operações foram abertas nesta sessão?";
    } else {
      titleEl.textContent = "Funil de decisões e operações";
    }
  }

  clearChildren(box);

  // Item 3 da auditoria (2ª rodada) + item 2 (3ª rodada): nunca misturar
  // silenciosamente escopos -- janela temporal, sessão e se os números
  // são o histórico COMPLETO ou uma amostra ficam explícitos aqui, antes
  // de qualquer número.
  const scopeNote = document.createElement("p");
  scopeNote.className = "why-no-trade-scope";
  const scopeCoverage = isCompleteHistory
    ? `100% do histórico de avaliações de risco (${fmtInt(allEvals.length)} de ${fmtInt(evaluationsTotal)} -- nenhuma amostragem)`
    : `amostra dos ${fmtInt(allEvals.length)} registros mais recentes de ${fmtInt(evaluationsTotal)} totais (histórico maior que o teto de segurança de ${fmtInt(WHY_NO_TRADE_EVAL_FETCH_CAP)} buscados de uma vez)`;
  const windowSpan = oldestEvalAt && newestEvalAt
    ? `, de ${new Date(oldestEvalAt).toLocaleString("pt-BR")} a ${new Date(newestEvalAt).toLocaleString("pt-BR")} (${CHART_TZ_LABEL})`
    : "";
  scopeNote.textContent = sessionStartedAt
    ? `Escopo: ${scopeCoverage}${windowSpan}. Sessão atual: ${sessionInfo.session_uid.slice(0, 8)}, iniciada em ${sessionStartedAt.toLocaleString("pt-BR")} (${CHART_TZ_LABEL}).`
    : `Escopo: ${scopeCoverage}${windowSpan}. Nenhuma sessão operacional ativa no momento.`;
  box.appendChild(scopeNote);

  kvRow(box, "Sinais de compra/venda recebidos (abertura + encerramento, histórico completo)", fmtInt(signalCounts.actionable_signals_total));

  const entryHeading = document.createElement("p");
  entryHeading.className = "why-no-trade-scope";
  entryHeading.textContent = "Abertura (entradas):";
  box.appendChild(entryHeading);
  kvRow(box, "Avaliações de entrada", fmtInt(entryTotal));
  kvRow(box, "Entradas aprovadas", fmtInt(entryApproved), entryApproved > 0 ? "positive" : "");
  kvRow(box, "Entradas recusadas", fmtInt(entryRejected));
  kvRow(box, "Ordens de abertura executadas (status EXECUTADA)", fmtInt(openOrdersFilled));
  kvRow(box, "Motivo dominante de recusa de entrada", entryDominant ? `${rejectionCheckLabel(entryDominant.reason)} (${fmtInt(entryDominant.count)}/${fmtInt(entryRejected)})` : "Nenhuma recusa de entrada registrada");

  const closeHeading = document.createElement("p");
  closeHeading.className = "why-no-trade-scope";
  closeHeading.textContent = "Encerramento (saídas):";
  box.appendChild(closeHeading);
  kvRow(box, "Avaliações de encerramento", fmtInt(closeTotal));
  kvRow(box, "Encerramentos aprovados", fmtInt(closeApproved), closeApproved > 0 ? "positive" : "");
  kvRow(box, "Encerramentos recusados", fmtInt(closeRejected));
  kvRow(box, "Ordens de encerramento executadas (status EXECUTADA)", fmtInt(closeOrdersFilled));
  kvRow(box, "Motivo dominante de recusa de encerramento", closeDominant ? `${rejectionCheckLabel(closeDominant.reason)} (${fmtInt(closeDominant.count)}/${fmtInt(closeRejected)})` : "Nenhuma recusa de encerramento registrada");

  const stateHeading = document.createElement("p");
  stateHeading.className = "why-no-trade-scope";
  stateHeading.textContent = `Estado atual: ${fmtInt(positionsOpenCount)} posição(ões) aberta(s). Nesta sessão: ${fmtInt(entryApprovedInSession)} entrada(s) aprovada(s), ${fmtInt(closeApprovedInSession)} encerramento(s) aprovado(s).`;
  box.appendChild(stateHeading);

  kvRow(box, "Maior cobertura observada no histórico (Cost Coverage)", dist.max != null ? fmtRatio(dist.max) : "Não disponível");
  kvRow(box, "Cobertura média observada no histórico (Cost Coverage)", costGate.avg_coverage_ratio_at_entry != null ? fmtRatio(costGate.avg_coverage_ratio_at_entry) : "Não disponível");
  kvRow(box, "Cobertura mínima exigida agora (Minimum Cost Coverage Ratio)", dist.required_ratio != null ? fmtRatio(dist.required_ratio) : "Não disponível");

  kvRow(box, "Aprovações do Laboratório de Estratégias (Shadow Mode)", "0 -- por construção: o laboratório nunca grava nesta tabela (fica em ai_recommendations, tabela separada)");

  const configNote = document.createElement("p");
  configNote.className = "why-no-trade-scope";
  configNote.textContent = "A cobertura mínima exigida é lida AO VIVO da configuração deste processo em execução agora -- nunca um valor em cache, hardcoded ou herdado de outra instância/ambiente.";
  box.appendChild(configNote);

  const caveat = document.createElement("p");
  caveat.className = "why-no-trade-caveat";
  caveat.textContent = "Este resumo descreve o comportamento observado do filtro de viabilidade da operação (Cost Gate) -- não afirma que a estratégia é lucrativa, validada estatisticamente ou bem calibrada.";
  box.appendChild(caveat);
}

async function refreshAll() {
  await Promise.all([
    refreshState(), refreshMetrics(), refreshPortfolioSummary(), refreshPositionsTable(),
    refreshSignals(), refreshRisk(), refreshAI(), refreshFailures(), refreshEquityCurve(),
    refreshSession(), refreshOrders(), refreshCosts(), refreshSymbolsSummary(),
    refreshChart(), refreshWhyNoTrades(),
  ]);
}

$("btn-kill").addEventListener("click", async () => {
  const res = await getJSON("/api/kill-switch/engage", { method: "POST" });
  if (res.mensagem) $("status-message").textContent = res.mensagem;
  refreshAll();
});
$("btn-unkill").addEventListener("click", async () => {
  const res = await getJSON("/api/kill-switch/disengage", { method: "POST" });
  if (res.mensagem) $("status-message").textContent = res.mensagem;
  refreshAll();
});
$("btn-activate").addEventListener("click", async () => {
  // Confirmação explícita antes de ativar operação Demo (item 7.9).
  if (!window.confirm("Confirma a ativação de novas entradas? A estratégia poderá abrir novas posições.")) return;
  const res = await getJSON("/api/operational-state/activate", { method: "POST" });
  if (res.mensagem) $("status-message").textContent = res.mensagem;
  refreshAll();
});
$("btn-pause").addEventListener("click", async () => {
  const res = await getJSON("/api/operational-state/pause", { method: "POST" });
  if (res.mensagem) $("status-message").textContent = res.mensagem;
  refreshAll();
});

refreshAll();
setInterval(refreshAll, 2000);
