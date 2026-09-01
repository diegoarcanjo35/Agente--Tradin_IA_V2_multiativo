// Correction v1.2 #7: NEVER use innerHTML with data that came from the
// backend (justificativas, motivos, resumos de IA, mensagens de erro) --
// all of it is inserted via textContent / DOM element creation only, so an
// externally-supplied string (e.g. from a future real AI provider) can
// never be interpreted as markup or an executable event handler.
const $ = (id) => document.getElementById(id);

const DIRECTION_LABELS = { BUY: "COMPRA", SELL: "VENDA", HOLD: "AGUARDAR" };
const MODE_LABELS = {
  REPLAY: "REPLAY (simulado)",
  PAPER_LOCAL: "PAPER_LOCAL (simulado)",
  PAPER_LIVE: "PAPER_LIVE (dados reais, execução simulada)",
  BYBIT_DEMO: "BYBIT_DEMO (Bybit Demo Trading)",
};
const OPERATIONAL_STATE_LABELS = {
  INICIALIZANDO: "INICIALIZANDO",
  OBSERVANDO: "OBSERVANDO (novas entradas desativadas)",
  ATIVO: "ATIVO (novas entradas autorizadas)",
  PAUSADO: "PAUSADO (novas entradas desativadas)",
  BLOQUEADO: "BLOQUEADO",
  ENCERRANDO: "ENCERRANDO",
};
const POLL_STATUS_LABELS = {
  INICIANDO: "INICIANDO",
  SAUDAVEL: "SAUDÁVEL",
  DEGRADADO: "DEGRADADO (falha recente, tentando recuperar)",
  PARADO: "PARADO (heartbeat vencido ou tarefa morta)",
  ENCERRANDO: "ENCERRANDO",
};
const ORDER_STATUS_LABELS = {
  PENDING_SUBMIT: "AGUARDANDO ENVIO", SUBMITTED: "ENVIADA", PARTIALLY_FILLED: "PARCIALMENTE PREENCHIDA",
  FILLED: "PREENCHIDA", CANCEL_PENDING: "CANCELAMENTO PENDENTE", CANCELLED: "CANCELADA",
  REJECTED: "REJEITADA", UNKNOWN: "DESCONHECIDA",
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
// vira o tooltip nativo do navegador (acessível, sem componente extra).
function statCard(container, label, valueText, opts = {}) {
  const card = document.createElement("div");
  card.className = "stat-card" + (opts.cardClass ? ` ${opts.cardClass}` : "");

  const labelEl = document.createElement("span");
  labelEl.className = "stat-label";
  labelEl.textContent = label;
  if (opts.title) labelEl.title = opts.title;

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
  $("chip-trading").textContent = `OPERAÇÕES: ${s.trading_blocked ? "BLOQUEADAS" : "ATIVAS"}`;
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
    // Preço: só disponível quando há posição aberta marcada a mercado
    // agora -- nunca o preço de outro símbolo, nunca inventado.
    const price = openPosition && typeof openPosition.mark_price === "number"
      ? openPosition.mark_price.toFixed(2) : "N/D";
    const positionText = p
      ? `${translateDirection(p.side)} ${p.qty.toFixed(6)} @ ${p.avg_entry_price.toFixed(2)}`
      : "sem posição";

    const lastSignal = lastSignals[i];
    const lastSignalText = lastSignal ? translateDirection(lastSignal.direction) : "N/D";

    return [
      symbol,
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
const CHART_STATE = {
  chart: null, candleSeries: null, volumeSeries: null,
  smaFastSeries: null, smaSlowSeries: null, symbol: null,
  priceLines: [], zoneEls: [], lastCandles: [], activeWindow: "all",
};

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
    return;
  }
  stateChip.textContent = position.side === "BUY" ? "COMPRADO" : "VENDIDO";

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

function ensureChart(symbol) {
  if (CHART_STATE.chart && CHART_STATE.symbol === symbol) return;
  if (CHART_STATE.chart) {
    CHART_STATE.chart.remove();
    CHART_STATE.chart = null;
  }
  const container = $("chart-container");
  clearChildren(container);
  CHART_STATE.priceLines = [];
  CHART_STATE.zoneEls = [];

  const chart = LightweightCharts.createChart(container, {
    layout: { background: { color: "#0c1120" }, textColor: "#e6e9f0" },
    grid: { vertLines: { color: "#1c2333" }, horzLines: { color: "#1c2333" } },
    rightPriceScale: { borderColor: "#26314a" },
    timeScale: { borderColor: "#26314a", timeVisible: true, secondsVisible: false },
    autoSize: true,
  });
  const candleSeries = chart.addCandlestickSeries({
    upColor: CHART_UP_COLOR, downColor: CHART_DOWN_COLOR,
    borderVisible: false, wickUpColor: CHART_UP_COLOR, wickDownColor: CHART_DOWN_COLOR,
  });
  const volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" }, priceScaleId: "volume",
  });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  const smaFastSeries = chart.addLineSeries({ color: "#fbbf24", lineWidth: 1, title: "SMA rápida" });
  const smaSlowSeries = chart.addLineSeries({ color: "#60a5fa", lineWidth: 1, title: "SMA lenta" });

  chart.timeScale().subscribeVisibleTimeRangeChange(repositionPriceZones);
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(repositionPriceZones).observe(container);
  }

  CHART_STATE.chart = chart;
  CHART_STATE.candleSeries = candleSeries;
  CHART_STATE.volumeSeries = volumeSeries;
  CHART_STATE.smaFastSeries = smaFastSeries;
  CHART_STATE.smaSlowSeries = smaSlowSeries;
  CHART_STATE.symbol = symbol;
}

function applyChartWindow(windowKey) {
  CHART_STATE.activeWindow = windowKey;
  document.querySelectorAll(".chart-window-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.window === windowKey);
  });
  if (!CHART_STATE.chart || !CHART_STATE.lastCandles.length) return;
  if (windowKey === "all") {
    CHART_STATE.chart.timeScale().fitContent();
    return;
  }
  const seconds = chartWindowSeconds(windowKey);
  const lastTime = CHART_STATE.lastCandles[CHART_STATE.lastCandles.length - 1].time;
  CHART_STATE.chart.timeScale().setVisibleRange({ from: lastTime - seconds, to: lastTime + 60 });
}

let chartControlsInitialized = false;

function initChartControlsOnce() {
  if (chartControlsInitialized) return;
  chartControlsInitialized = true;
  document.querySelectorAll(".chart-window-btn").forEach((btn) => {
    btn.addEventListener("click", () => applyChartWindow(btn.dataset.window));
  });
}

async function refreshChartSymbolOptions() {
  const select = $("chart-symbol-select");
  if (select.dataset.loaded === "1") return;
  const { symbols } = await getJSON("/api/symbols");
  clearChildren(select);
  symbols.forEach((symbol) => {
    const opt = document.createElement("option");
    opt.value = symbol;
    opt.textContent = symbol;
    select.appendChild(opt);
  });
  if (symbols.length) select.dataset.loaded = "1";
}

async function refreshChart() {
  if (typeof LightweightCharts === "undefined") return;
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

    ensureChart(symbol);
    $("chart-timeframe").textContent = `TF: ${body.timeframe}`;
    $("chart-status").textContent = `Status: ${SYMBOL_HEALTH_LABELS[(body.symbol_health || {}).status] || "indisponível"}`;
    $("chart-visual-price").textContent = body.visual_price != null
      ? `Preço: ${body.visual_price.toFixed(2)} (${body.visual_price_source === "forming_candle" ? "ao vivo (visual)" : "último fechamento"})`
      : "Preço: indisponível";

    const candles = body.candles;
    CHART_STATE.lastCandles = candles;
    CHART_STATE.candleSeries.setData(candles.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })));
    CHART_STATE.volumeSeries.setData(candles.map((c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? CHART_UP_COLOR : CHART_DOWN_COLOR })));
    const cfg = body.strategy_config || { fast_period: 9, slow_period: 21 };
    CHART_STATE.smaFastSeries.setData(computeSMA(candles, cfg.fast_period));
    CHART_STATE.smaSlowSeries.setData(computeSMA(candles, cfg.slow_period));

    const markers = (body.recent_signals || []).map((s) => ({
      time: s.time,
      position: s.direction === "BUY" ? "belowBar" : "aboveBar",
      color: s.direction === "BUY" ? CHART_UP_COLOR : CHART_DOWN_COLOR,
      shape: s.direction === "BUY" ? "arrowUp" : "arrowDown",
      text: `${s.direction}${s.order_status ? ` (${ORDER_STATUS_LABELS[s.order_status] || s.order_status})` : ""}`,
    }));
    CHART_STATE.candleSeries.setMarkers(markers);

    applyPositionOverlay(body.position, body.visual_price);
    if (CHART_STATE.activeWindow === "all") CHART_STATE.chart.timeScale().fitContent();
    else applyChartWindow(CHART_STATE.activeWindow);

    const loading = $("chart-loading");
    if (loading) loading.remove();
  } catch (err) {
    // Falha do gráfico nunca derruba o restante do painel (item 16 da
    // matriz de testes) -- Promise.all em refreshAll() nunca vê esta
    // rejeição.
    const container = $("chart-container");
    if (container && !document.getElementById("chart-error")) {
      const msg = document.createElement("div");
      msg.id = "chart-error";
      msg.className = "chart-message";
      msg.textContent = "Erro ao carregar o gráfico -- os demais painéis continuam funcionando normalmente.";
      container.appendChild(msg);
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

async function refreshAll() {
  await Promise.all([
    refreshState(), refreshMetrics(), refreshPortfolioSummary(), refreshPositionsTable(),
    refreshSignals(), refreshRisk(), refreshAI(), refreshFailures(), refreshEquityCurve(),
    refreshSession(), refreshOrders(), refreshCosts(), refreshSymbolsSummary(),
    refreshChart(),
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
