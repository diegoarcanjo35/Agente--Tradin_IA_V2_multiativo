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

async function refreshCosts() {
  const c = await getJSON("/api/costs");
  const box = $("costs-box");
  clearChildren(box);
  kvRow(box, "Taxas acumuladas", fmtNumber(c.fees_total));
  kvRow(box, "Slippage médio (USD)", fmtNumber(c.slippage_avg_usd));
  kvRow(box, "Slippage total (USD)", fmtNumber(c.slippage_total_usd));
  kvRow(box, "Ordens com preço de referência conhecido", c.priced_orders_count);
}

async function refreshMetrics() {
  const m = await getJSON("/api/metrics");

  const metricsBox = $("metrics-box");
  clearChildren(metricsBox);
  kvRow(metricsBox, "Operações encerradas", m.closed_trades_count);
  kvRow(metricsBox, "Taxa de acerto", fmtNumber(m.win_rate, 3));
  kvRow(metricsBox, "Payoff", fmtNumber(m.payoff));
  kvRow(metricsBox, "Expectativa", fmtNumber(m.expectancy));

  const pnlBox = $("pnl-box");
  clearChildren(pnlBox);
  kvRow(pnlBox, "Lucro bruto", fmtNumber(m.gross_profit), pnlClass(m.gross_profit));
  kvRow(pnlBox, "Prejuízo bruto", fmtNumber(m.gross_loss), pnlClass(m.gross_loss));
  kvRow(pnlBox, "Lucro líquido", fmtNumber(m.net_profit), pnlClass(m.net_profit));
  kvRow(pnlBox, "Comissões", fmtNumber(m.commissions));
  kvRow(pnlBox, "Taxa de financiamento (Funding)", fmtNumber(m.funding));

  const riskBox = $("risk-metrics-box");
  clearChildren(riskBox);
  kvRow(riskBox, "Fator de lucro (Profit Factor)", fmtNumber(m.profit_factor));
  kvRow(riskBox, "Rebaixamento máx. ($) (Drawdown)", fmtNumber(m.max_drawdown_money));
  kvRow(riskBox, "Rebaixamento máx. (%) (Drawdown)", fmtNumber(m.max_drawdown_pct));
  kvRow(riskBox, "Retorno/Rebaixamento", fmtNumber(m.return_over_drawdown));
  kvRow(riskBox, "Exposição (USD)", fmtNumber(m.exposure_usd));
}

async function refreshAccount() {
  const positions = await getJSON("/api/positions");
  const totalExposure = positions.reduce((acc, p) => acc + p.qty * p.avg_entry_price, 0);

  const accountBox = $("account-box");
  clearChildren(accountBox);
  kvRow(accountBox, "Saldo inicial (demo)", "1000.00");
  kvRow(accountBox, "Posições abertas", positions.length);
  kvRow(accountBox, "Exposição aberta", totalExposure.toFixed(2));

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

// Fase 3 multiativo: um card por símbolo configurado -- preço/último candle
// (via posição ou métricas mais recentes disponíveis), defasagem/saúde,
// sinal, posição, exposição e PnL. Constrói tudo via createElement/
// textContent (nunca innerHTML), mesmo padrão de kvRow/buildRow acima.
const SYMBOL_HEALTH_LABELS = {
  INICIANDO: "INICIANDO", SAUDAVEL: "SAUDÁVEL",
  DEGRADADO: "DEGRADADO", PARADO: "PARADO", ENCERRANDO: "ENCERRANDO",
};

async function refreshSymbolsSummary() {
  const [symbolsResp, state, positions, metrics] = await Promise.all([
    getJSON("/api/symbols"), getJSON("/api/state"), getJSON("/api/positions"), getJSON("/api/metrics"),
  ]);
  const symbols = symbolsResp.symbols || [];
  const health = (state.symbols_health && state.symbols_health.per_symbol) || {};
  const perSymbolMetrics = metrics.per_symbol || {};
  const positionsBySymbol = {};
  positions.forEach((p) => { positionsBySymbol[p.symbol] = p; });

  const box = $("symbols-summary-box");
  clearChildren(box);

  symbols.forEach((symbol) => {
    const wrapper = document.createElement("div");
    wrapper.className = "symbol-summary-card";

    const title = document.createElement("h3");
    title.textContent = symbol;
    wrapper.appendChild(title);

    const h = health[symbol] || {};
    const healthLabel = SYMBOL_HEALTH_LABELS[h.status] || h.status || "indisponível";
    const healthy = h.status === "SAUDAVEL";
    kvRow(wrapper, "Saúde", healthLabel, healthy ? "" : "negative");
    kvRow(wrapper, "Falhas consecutivas", h.consecutive_failures != null ? h.consecutive_failures : 0);
    kvRow(wrapper, "Lacuna de dados", h.has_gap ? "SIM" : "não", h.has_gap ? "negative" : "");

    const p = positionsBySymbol[symbol];
    if (p) {
      kvRow(wrapper, "Posição", `${translateDirection(p.side)} ${p.qty.toFixed(6)} @ ${p.avg_entry_price.toFixed(2)}`);
      kvRow(wrapper, "Exposição (USD)", (p.qty * p.avg_entry_price).toFixed(2));
    } else {
      kvRow(wrapper, "Posição", "nenhuma posição aberta");
    }

    const m = perSymbolMetrics[symbol];
    if (m) {
      kvRow(wrapper, "PnL líquido", fmtNumber(m.net_profit), pnlClass(m.net_profit));
      kvRow(wrapper, "Operações encerradas", m.closed_trades_count);
    }

    box.appendChild(wrapper);
  });

  if (symbols.length === 0) {
    kvRow(box, "Símbolos", "nenhum símbolo configurado");
  }
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
    refreshState(), refreshMetrics(), refreshAccount(), refreshSignals(),
    refreshRisk(), refreshAI(), refreshFailures(), refreshEquityCurve(),
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
