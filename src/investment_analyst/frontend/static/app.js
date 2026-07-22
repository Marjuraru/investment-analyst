"use strict";

const LOCALE = "es-PE";
const DEFAULT_TIME_ZONE = "America/Lima";
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const THEME_STORAGE_KEY = "investment-analyst-theme-v1";
const CHART_SETTINGS_STORAGE_KEY = "investment-analyst-chart-settings-v1";
const DEFAULT_CHART_SETTINGS = Object.freeze({
  shortWindow: 5,
  longWindow: 20,
  thirdWindow: 50,
  shortColor: "#e29951",
  longColor: "#a695df",
  thirdColor: "#d778aa",
  priceScale: "linear",
  chartType: "line",
  interval: "auto",
});
const CHART_WIDTH = 1000;
const CHART_HEIGHT = 360;
const CHART_LAYOUT = Object.freeze({
  left: 70,
  right: 24,
  top: 16,
  priceBottom: 235,
  volumeTop: 271,
  bottom: 333,
});
const MINIMUM_CHART_VIEW_POINTS = 8;
const FUNDAMENTAL_CHART_WIDTH = 900;
const FUNDAMENTAL_CHART_HEIGHT = 330;
const FUNDAMENTAL_CHART_LAYOUT = Object.freeze({
  left: 66,
  right: 20,
  top: 24,
  bottom: 276,
});

const MARKET_CHART_PERIOD = "max";

const MARKET_RESOLUTION_PRESENTATION = Object.freeze({
  daily: Object.freeze({ singular: "día", plural: "días", adjective: "diarios" }),
  weekly: Object.freeze({ singular: "semana", plural: "semanas", adjective: "semanales" }),
  monthly: Object.freeze({ singular: "mes", plural: "meses", adjective: "mensuales" }),
});

const STATUS_LABELS = Object.freeze({
  ready: "Listo",
  incomplete: "Incompleto",
  running: "En ejecución",
  degraded: "Requiere atención",
  succeeded: "Correcta",
  failed: "Fallida",
  skipped: "Omitida",
  complete: "Completo",
  partial: "Parcial",
  unavailable: "No disponible",
  available: "Disponible",
  not_found: "No encontrado",
  initial: "Inicial",
  incremental: "Incremental",
  already_current: "Ya estaba actualizado",
  backfill: "Ampliación histórica",
  full: "Rango completo",
  auto: "Automática",
});

const VERDICT_LABELS = Object.freeze({
  positive: "Positivo",
  neutral: "Neutral",
  negative: "Negativo",
  insufficient_data: "Datos insuficientes",
});

const QUALITY_LABELS = Object.freeze({
  valid: "Válida",
  delayed: "Con retraso",
  partial: "Parcial",
  suspect: "Requiere revisión",
});

const METRIC_PRESENTATION = Object.freeze({
  "market.history.relative_volume": Object.freeze({
    label: "Volumen relativo",
    kind: "multiple",
  }),
  "market.history.rolling_daily_volatility": Object.freeze({
    label: "Volatilidad diaria móvil",
    kind: "percentage",
  }),
  "market.history.simple_return_1d": Object.freeze({
    label: "Variación diaria",
    kind: "percentage",
  }),
  "market.history.sma": Object.freeze({
    label: "Media móvil simple (SMA)",
    kind: "currency",
  }),
  "fundamental.liabilities_to_assets": Object.freeze({
    label: "Pasivos sobre activos",
    kind: "percentage",
  }),
  "fundamental.liabilities_to_equity": Object.freeze({
    label: "Pasivos sobre patrimonio",
    kind: "multiple",
  }),
  "fundamental.net_income_yoy_change_rate": Object.freeze({
    label: "Variación interanual del resultado neto",
    kind: "percentage",
  }),
  "fundamental.net_margin": Object.freeze({
    label: "Margen neto",
    kind: "percentage",
  }),
  "fundamental.revenue_yoy_growth": Object.freeze({
    label: "Crecimiento interanual de ingresos",
    kind: "percentage",
  }),
});

const FUNDAMENTAL_RESEARCH_PRESENTATION = Object.freeze({
  "fundamental.research.asset_turnover": Object.freeze({
    label: "Rotación de activos",
    kind: "multiple",
  }),
  "fundamental.research.current_financial_debt": Object.freeze({
    label: "Deuda financiera corriente",
    kind: "currency",
  }),
  "fundamental.research.current_financial_debt_share": Object.freeze({
    label: "Vencimiento corriente / deuda",
    kind: "percentage",
  }),
  "fundamental.research.diluted_eps": Object.freeze({
    label: "EPS diluido",
    kind: "currency_per_share",
  }),
  "fundamental.research.revenue_per_diluted_share": Object.freeze({
    label: "Ingresos / acción diluida",
    kind: "currency_per_share",
  }),
  "fundamental.research.free_cash_flow_per_diluted_share": Object.freeze({
    label: "FCF / acción diluida",
    kind: "currency_per_share",
  }),
  "fundamental.research.diluted_shares": Object.freeze({
    label: "Acciones diluidas promedio",
    kind: "shares",
  }),
  "fundamental.research.shares_outstanding": Object.freeze({
    label: "Acciones en circulación",
    kind: "shares",
  }),
  "fundamental.research.effective_tax_rate": Object.freeze({
    label: "Tasa fiscal efectiva",
    kind: "percentage",
  }),
  "fundamental.research.financial_debt": Object.freeze({
    label: "Deuda financiera",
    kind: "currency",
  }),
  "fundamental.research.financial_debt_to_assets": Object.freeze({
    label: "Deuda financiera / activos",
    kind: "percentage",
  }),
  "fundamental.research.financial_debt_to_equity": Object.freeze({
    label: "Deuda financiera / patrimonio",
    kind: "multiple",
  }),
  "fundamental.research.financial_debt_to_free_cash_flow": Object.freeze({
    label: "Deuda financiera / FCF",
    kind: "multiple",
  }),
  "fundamental.research.fixed_asset_turnover": Object.freeze({
    label: "Rotación de activos fijos",
    kind: "multiple",
  }),
  "fundamental.research.gross_margin": Object.freeze({
    label: "Margen bruto",
    kind: "percentage",
  }),
  "fundamental.research.operating_margin": Object.freeze({
    label: "Margen operativo",
    kind: "percentage",
  }),
  "fundamental.research.net_margin": Object.freeze({
    label: "Margen neto",
    kind: "percentage",
  }),
  "fundamental.research.operating_cash_flow_margin": Object.freeze({
    label: "Margen de flujo operativo",
    kind: "percentage",
  }),
  "fundamental.research.free_cash_flow_margin": Object.freeze({
    label: "Margen de FCF",
    kind: "percentage",
  }),
  "fundamental.research.operating_cash_flow_to_net_income": Object.freeze({
    label: "Flujo operativo / beneficio neto",
    kind: "multiple",
  }),
  "fundamental.research.free_cash_flow_to_net_income": Object.freeze({
    label: "FCF / beneficio neto",
    kind: "multiple",
  }),
  "fundamental.research.free_cash_flow": Object.freeze({
    label: "Flujo de caja libre",
    kind: "currency",
  }),
  "fundamental.research.capex_to_operating_cash_flow": Object.freeze({
    label: "Capex / flujo operativo",
    kind: "percentage",
  }),
  "fundamental.research.research_and_development_to_revenue": Object.freeze({
    label: "R&D / ingresos",
    kind: "percentage",
  }),
  "fundamental.research.selling_general_and_administrative_to_revenue": Object.freeze({
    label: "SG&A / ingresos",
    kind: "percentage",
  }),
  "fundamental.research.share_based_compensation_to_revenue": Object.freeze({
    label: "Stock-based compensation / ingresos",
    kind: "percentage",
  }),
  "fundamental.research.current_ratio": Object.freeze({
    label: "Current ratio",
    kind: "multiple",
  }),
  "fundamental.research.cash_ratio": Object.freeze({
    label: "Cash ratio",
    kind: "multiple",
  }),
  "fundamental.research.working_capital": Object.freeze({
    label: "Capital de trabajo",
    kind: "currency",
  }),
  "fundamental.research.net_liquid_assets": Object.freeze({
    label: "Activos líquidos netos",
    kind: "currency",
  }),
  "fundamental.research.interest_coverage": Object.freeze({
    label: "Cobertura de intereses",
    kind: "multiple",
  }),
  "fundamental.research.lease_liabilities": Object.freeze({
    label: "Pasivos por arrendamientos",
    kind: "currency",
  }),
  "fundamental.research.net_debt": Object.freeze({
    label: "Deuda financiera neta",
    kind: "currency",
  }),
  "fundamental.research.net_debt_to_free_cash_flow": Object.freeze({
    label: "Deuda neta / FCF",
    kind: "multiple",
  }),
  "fundamental.research.return_on_assets_ending_balance": Object.freeze({
    label: "Rentabilidad sobre activos",
    kind: "percentage",
  }),
  "fundamental.research.return_on_equity_ending_balance": Object.freeze({
    label: "Rentabilidad sobre patrimonio",
    kind: "percentage",
  }),
  "fundamental.research.return_on_invested_capital_ending_balance": Object.freeze({
    label: "ROIC aproximado",
    kind: "percentage",
  }),
  "fundamental.research.shareholder_distributions": Object.freeze({
    label: "Dividendos + recompras",
    kind: "currency",
  }),
  "fundamental.research.shareholder_distributions_to_free_cash_flow": Object.freeze({
    label: "Distribuciones / FCF",
    kind: "percentage",
  }),
  "fundamental.research.total_financial_obligations": Object.freeze({
    label: "Deuda + arrendamientos",
    kind: "currency",
  }),
});

const LIMITATION_TRANSLATIONS = new Map([
  [
    "Market and fundamental diagnostics remain independent; no combined score, verdict, confidence, quality, recommendation, or ranking is calculated.",
    "Los diagnósticos de mercado y fundamentales son independientes; no se calcula una puntuación, veredicto, confianza, calidad, recomendación ni clasificación combinada.",
  ],
  [
    "Apple market data uses Alpaca Market Data IEX daily bars with adjustment all; IEX is single-exchange coverage and is not consolidated SIP coverage.",
    "El mercado usa barras diarias de Alpaca Market Data IEX con ajuste total; IEX cubre un solo mercado y no equivale a SIP consolidado.",
  ],
  [
    "Apple fundamental data comes from official SEC EDGAR submissions and company facts.",
    "Los datos fundamentales de Apple provienen de Submissions y Company Facts oficiales de SEC EDGAR.",
  ],
  [
    "Diagnostic confidence describes evidence coverage under deterministic rules; it is not a calibrated probability.",
    "La confianza describe la cobertura de evidencia bajo reglas deterministas; no es una probabilidad calibrada.",
  ],
  [
    "This report is descriptive analytical output, not financial advice, and it does not execute operations.",
    "Este reporte es un análisis descriptivo, no constituye asesoramiento financiero y no ejecuta operaciones.",
  ],
]);

const ISSUE_TRANSLATIONS = new Map([
  ["no operational run has been recorded", "Todavía no existe una ejecución operativa registrada."],
  ["the latest operational run failed", "La última actualización operativa falló."],
  [
    "the latest run was interrupted before completion",
    "La última actualización fue interrumpida antes de terminar.",
  ],
  ["the latest scheduled attempt was interrupted", "La última ejecución programada fue interrumpida."],
  ["the latest scheduled attempt failed", "La última ejecución programada falló."],
  ["workspace storage directory is missing", "Falta el directorio de almacenamiento del espacio de datos."],
  ["workspace database is missing", "Falta la base de datos del espacio de datos."],
  ["raw storage directory is missing", "Falta el directorio de evidencia original."],
  ["Parquet export directory is missing", "Falta el directorio de exportación Parquet."],
]);

const ERROR_MESSAGES = Object.freeze({
  invalid_request: "La solicitud contiene datos inválidos. Revisa las fechas, la zona horaria y la frecuencia.",
  invalid_json: "La solicitud no pudo interpretarse correctamente.",
  query_failed: "No fue posible construir el análisis para el corte solicitado.",
  run_active: "Ya existe una actualización en curso para este espacio de datos.",
  operational_error: "La operación local no está disponible. Revisa el estado del espacio de datos.",
  unexpected_error: "La interfaz local encontró un error inesperado.",
});

const byId = (id) => document.getElementById(id);
let operationalIssues = [];
let marketChartPayload = null;
let marketChartViewport = null;
let marketChartRenderFrame = null;
let marketChartDrag = null;
let selectedChartPoint = -1;
let selectedFundamentalFrequency = "quarterly";
let fundamentalTrendPayload = null;
let fundamentalResearchPayload = null;
let fundamentalBusyCount = 0;
let reportPayload = null;
let chartSettings = { ...DEFAULT_CHART_SETTINGS };
const chartSeriesVisibility = {
  "sma-5": true,
  "sma-20": true,
  "sma-50": true,
  volume: true,
};

function applyTheme(theme) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  document.querySelector('meta[name="theme-color"]').content =
    selected === "dark" ? "#0b111c" : "#f3f5f7";
  const button = byId("theme-toggle");
  button.textContent = selected === "dark" ? "Tema claro" : "Tema oscuro";
  button.setAttribute("aria-pressed", String(selected === "dark"));
  button.setAttribute(
    "aria-label",
    selected === "dark" ? "Cambiar al tema claro" : "Cambiar al tema oscuro",
  );
}

function initializeTheme() {
  let stored = null;
  try {
    stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
  applyTheme(stored === "light" ? "light" : "dark");
}

function persistTheme(theme) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
}

function normalizeChartSettings(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  const shortWindow = Number(candidate.shortWindow);
  const longWindow = Number(candidate.longWindow);
  const thirdWindow = Number(candidate.thirdWindow ?? DEFAULT_CHART_SETTINGS.thirdWindow);
  const thirdColor = candidate.thirdColor ?? DEFAULT_CHART_SETTINGS.thirdColor;
  const priceScale = candidate.priceScale === undefined ? "linear" : candidate.priceScale;
  const chartType = candidate.chartType === undefined ? "line" : candidate.chartType;
  const interval = candidate.interval === undefined ? "auto" : candidate.interval;
  const colorPattern = /^#[0-9a-f]{6}$/i;
  if (
    !Number.isInteger(shortWindow) ||
    !Number.isInteger(longWindow) ||
    !Number.isInteger(thirdWindow) ||
    shortWindow < 2 ||
    shortWindow > 200 ||
    longWindow < 3 ||
    longWindow > 399 ||
    thirdWindow < 4 ||
    thirdWindow > 400 ||
    shortWindow >= longWindow ||
    longWindow >= thirdWindow ||
    !colorPattern.test(candidate.shortColor) ||
    !colorPattern.test(candidate.longColor) ||
    !colorPattern.test(thirdColor) ||
    !["linear", "logarithmic"].includes(priceScale) ||
    !["line", "candlestick"].includes(chartType) ||
    !["auto", "1d", "1w", "1mo"].includes(interval)
  ) {
    return null;
  }
  return {
    shortWindow,
    longWindow,
    thirdWindow,
    shortColor: candidate.shortColor.toLowerCase(),
    longColor: candidate.longColor.toLowerCase(),
    thirdColor: thirdColor.toLowerCase(),
    priceScale,
    chartType,
    interval,
  };
}

function updateSmaLabels() {
  const labels = {
    "sma-short-legend-label": `SMA ${chartSettings.shortWindow}`,
    "sma-long-legend-label": `SMA ${chartSettings.longWindow}`,
    "sma-third-legend-label": `SMA ${chartSettings.thirdWindow}`,
    "chart-point-sma-short-label": `SMA ${chartSettings.shortWindow}`,
    "chart-point-sma-long-label": `SMA ${chartSettings.longWindow}`,
    "chart-point-sma-third-label": `SMA ${chartSettings.thirdWindow}`,
    "snapshot-sma-short-distance-label": `Dist. SMA ${chartSettings.shortWindow}`,
    "snapshot-sma-long-distance-label": `Dist. SMA ${chartSettings.longWindow}`,
    "snapshot-sma-third-distance-label": `Dist. SMA ${chartSettings.thirdWindow}`,
    "chart-latest-sma-short-label": `SMA ${chartSettings.shortWindow}`,
    "chart-latest-sma-long-label": `SMA ${chartSettings.longWindow}`,
    "chart-latest-sma-third-label": `SMA ${chartSettings.thirdWindow}`,
    "chart-table-sma-short-label": `SMA ${chartSettings.shortWindow}`,
    "chart-table-sma-long-label": `SMA ${chartSettings.longWindow}`,
    "chart-table-sma-third-label": `SMA ${chartSettings.thirdWindow}`,
  };
  for (const [id, label] of Object.entries(labels)) byId(id).textContent = label;
}

function applyChartSettings() {
  byId("sma-short-window").value = String(chartSettings.shortWindow);
  byId("sma-long-window").value = String(chartSettings.longWindow);
  byId("sma-third-window").value = String(chartSettings.thirdWindow);
  byId("sma-short-color").value = chartSettings.shortColor;
  byId("sma-long-color").value = chartSettings.longColor;
  byId("sma-third-color").value = chartSettings.thirdColor;
  byId("chart-price-scale").value = chartSettings.priceScale;
  byId("chart-interval").value = chartSettings.interval;
  byId("chart-settings-summary").textContent =
    chartSettings.priceScale === "logarithmic"
      ? "Indicadores · Logarítmica"
      : "Indicadores · Lineal";
  document.documentElement.style.setProperty("--series-sma-5", chartSettings.shortColor);
  document.documentElement.style.setProperty("--series-sma-20", chartSettings.longColor);
  document.documentElement.style.setProperty("--series-sma-50", chartSettings.thirdColor);
  for (const button of document.querySelectorAll(".chart-type-button")) {
    const active = button.dataset.chartType === chartSettings.chartType;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  const candlesticks = chartSettings.chartType === "candlestick";
  byId("price-series-legend-label").textContent = candlesticks ? "Velas" : "Cierre";
  byId("price-series-swatch").className =
    `legend-swatch ${candlesticks ? "candles" : "close"}`;
  updateSmaLabels();
}

function initializeChartSettings() {
  let stored = null;
  try {
    stored = window.localStorage.getItem(CHART_SETTINGS_STORAGE_KEY);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
  if (stored !== null) {
    try {
      chartSettings = normalizeChartSettings(JSON.parse(stored)) || {
        ...DEFAULT_CHART_SETTINGS,
      };
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      chartSettings = { ...DEFAULT_CHART_SETTINGS };
    }
  }
  applyChartSettings();
}

function persistChartSettings() {
  try {
    window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY, JSON.stringify(chartSettings));
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function setExportAvailable(id, available) {
  byId(id).disabled = !available;
}

function csvCell(value) {
  let text = "";
  if (value !== null && value !== undefined) {
    text = typeof value === "string" ? value : JSON.stringify(value);
  }
  return `"${text.replaceAll('"', '""')}"`;
}

function csvDocument(columns, rows) {
  const lines = [columns, ...rows].map((row) => row.map(csvCell).join(","));
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

function safeFilePart(value) {
  const normalized = String(value || "sin-corte")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "sin-corte";
}

function downloadText(filename, content, mediaType) {
  const blob = new Blob([content], { type: `${mediaType};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.hidden = true;
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function marketCsvRows(chart, points = chart.points || []) {
  return points.map((point) => [
    chart.schema_version,
    chart.asset_id,
    chart.source_id,
    chart.known_at,
    chart.period,
    chart.resolution,
    chart.resolution_policy_version,
    point.resolution,
    point.period_start_timestamp,
    point.timestamp,
    point.bar_available_at,
    point.source_session_count,
    point.open,
    point.high,
    point.low,
    point.close,
    point.volume,
    point.trade_count,
    point.vwap,
    point.quality,
    point.raw_record_ids,
    point.open_observation_id,
    point.high_observation_id,
    point.low_observation_id,
    point.close_observation_id,
    point.volume_input_observation_ids,
    point.trade_count_input_observation_ids,
    point.vwap_input_observation_ids,
    point.aggregation_algorithm_version,
    point.short_sma?.value,
    point.short_sma?.window,
    point.short_sma?.resolution,
    point.short_sma?.available_at,
    point.short_sma?.algorithm_version,
    point.short_sma?.input_observation_ids,
    point.long_sma?.value,
    point.long_sma?.window,
    point.long_sma?.resolution,
    point.long_sma?.available_at,
    point.long_sma?.algorithm_version,
    point.long_sma?.input_observation_ids,
    point.third_sma?.value,
    point.third_sma?.window,
    point.third_sma?.resolution,
    point.third_sma?.available_at,
    point.third_sma?.algorithm_version,
    point.third_sma?.input_observation_ids,
    chart.traceability_verified,
  ]);
}

function exportMarketCsv() {
  const points = visibleMarketChartPoints();
  if (!marketChartPayload || !points.length) return;
  const columns = [
    "schema_version",
    "asset_id",
    "source_id",
    "known_at",
    "period",
    "resolution",
    "resolution_policy_version",
    "point_resolution",
    "period_start_timestamp",
    "timestamp",
    "bar_available_at",
    "source_session_count",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
    "quality",
    "raw_record_ids",
    "open_observation_id",
    "high_observation_id",
    "low_observation_id",
    "close_observation_id",
    "volume_input_observation_ids",
    "trade_count_input_observation_ids",
    "vwap_input_observation_ids",
    "aggregation_algorithm_version",
    "short_sma_value",
    "short_sma_window",
    "short_sma_resolution",
    "short_sma_available_at",
    "short_sma_algorithm_version",
    "short_sma_input_observation_ids",
    "long_sma_value",
    "long_sma_window",
    "long_sma_resolution",
    "long_sma_available_at",
    "long_sma_algorithm_version",
    "long_sma_input_observation_ids",
    "third_sma_value",
    "third_sma_window",
    "third_sma_resolution",
    "third_sma_available_at",
    "third_sma_algorithm_version",
    "third_sma_input_observation_ids",
    "traceability_verified",
  ];
  const filename = `aapl-mercado-${safeFilePart(marketChartPayload.period)}-${safeFilePart(marketChartPayload.known_at)}.csv`;
  downloadText(
    filename,
    csvDocument(columns, marketCsvRows(marketChartPayload, points)),
    "text/csv",
  );
}

function fundamentalCsvRows(trend) {
  const rows = [];
  for (const period of trend.periods || []) {
    for (const fact of period.facts || []) {
      rows.push([
        trend.schema_version,
        trend.asset_id,
        trend.source_id,
        trend.known_at,
        trend.frequency,
        trend.period_limit,
        period.period_end,
        period.frequency,
        period.latest_available_at,
        period.is_complete,
        period.available_fields,
        period.missing_fields,
        fact.field_name,
        fact.value,
        fact.unit,
        fact.period_start,
        fact.period_end,
        fact.available_at,
        fact.normalized_at,
        fact.fiscal_year,
        fact.fiscal_period,
        fact.form,
        fact.taxonomy,
        fact.tag,
        fact.accession_number,
        fact.record_key,
        fact.raw_record_id,
        fact.observation_id,
        fact.superseded_count,
        trend.traceability_verified,
      ]);
    }
  }
  return rows;
}

function exportFundamentalCsv() {
  if (!fundamentalTrendPayload?.periods?.length) return;
  const columns = [
    "schema_version",
    "asset_id",
    "source_id",
    "known_at",
    "requested_frequency",
    "period_limit",
    "period_end",
    "period_frequency",
    "latest_available_at",
    "period_is_complete",
    "available_fields",
    "missing_fields",
    "field_name",
    "value",
    "unit",
    "fact_period_start",
    "fact_period_end",
    "fact_available_at",
    "normalized_at",
    "fiscal_year",
    "fiscal_period",
    "form",
    "taxonomy",
    "tag",
    "accession_number",
    "record_key",
    "raw_record_id",
    "observation_id",
    "superseded_count",
    "traceability_verified",
  ];
  const filename = `aapl-fundamentales-${safeFilePart(fundamentalTrendPayload.frequency)}-${safeFilePart(fundamentalTrendPayload.known_at)}.csv`;
  downloadText(
    filename,
    csvDocument(columns, fundamentalCsvRows(fundamentalTrendPayload)),
    "text/csv",
  );
}

function fundamentalResearchCsvRows(payload) {
  const historyPayload = payload.history || payload;
  const research = historyPayload.research || historyPayload;
  const histories = new Map(
    (historyPayload.series || []).map((history) => [history.metric_key, history]),
  );
  const rows = [];
  for (const period of research.periods || []) {
    for (const metric of period.metrics || []) {
      const statistics = histories.get(metric.metric_key)?.statistics || {};
      rows.push([
        research.schema_version,
        research.asset_id,
        research.source_id,
        research.request?.known_at,
        research.request?.frequency,
        period.period_end,
        metric.metric_key,
        metric.display_name_es,
        metric.value,
        metric.unit,
        metric.available_at,
        metric.formula,
        metric.algorithm_version,
        metric.limitations,
        metric.inputs,
        statistics.point_count,
        statistics.latest_change_from_previous_available,
        statistics.latest_change_rate_from_previous_available,
        statistics.horizon_change,
        statistics.horizon_change_rate,
        statistics.compound_annual_growth_rate,
        statistics.minimum,
        statistics.maximum,
        statistics.arithmetic_mean,
        statistics.range,
        statistics.algorithm_version,
        research.traceability_verified,
      ]);
    }
  }
  return rows;
}

function exportFundamentalResearchCsv() {
  const historyPayload = fundamentalResearchPayload?.history || fundamentalResearchPayload;
  const research = historyPayload?.research || historyPayload;
  if (!research?.periods?.length) return;
  const columns = [
    "schema_version",
    "asset_id",
    "source_id",
    "known_at",
    "frequency",
    "period_end",
    "metric_key",
    "display_name_es",
    "value",
    "unit",
    "available_at",
    "formula",
    "algorithm_version",
    "limitations",
    "inputs",
    "history_point_count",
    "latest_change_from_previous_available",
    "latest_change_rate_from_previous_available",
    "horizon_change",
    "horizon_change_rate",
    "compound_annual_growth_rate",
    "history_minimum",
    "history_maximum",
    "history_arithmetic_mean",
    "history_range",
    "history_algorithm_version",
    "traceability_verified",
  ];
  const request = research.request || {};
  const filename = `aapl-metricas-fundamentales-${safeFilePart(request.frequency)}-${safeFilePart(request.known_at)}.csv`;
  downloadText(
    filename,
    csvDocument(columns, fundamentalResearchCsvRows(fundamentalResearchPayload)),
    "text/csv",
  );
}

function exportReportJson() {
  if (!reportPayload) return;
  const filename = `aapl-reporte-${safeFilePart(reportPayload.query?.known_at)}.json`;
  downloadText(filename, `${JSON.stringify(reportPayload, null, 2)}\n`, "application/json");
}

function translated(value, dictionary, fallback = value) {
  return dictionary[value] || fallback;
}

function setMessage(message, isError = false) {
  const target = byId("global-message");
  target.textContent = message;
  target.classList.toggle("error", isError);
  target.classList.toggle("hidden", !message);
}

function badge(target, value, tone) {
  target.textContent = value;
  target.className = `badge ${tone}`;
}

function statusTone(value) {
  if (["ready", "succeeded", "complete", "available", "valid"].includes(value)) return "good";
  if (["running", "partial", "incomplete"].includes(value)) return "warn";
  if (["failed", "degraded", "unavailable", "not_found"].includes(value)) return "bad";
  return "neutral";
}

function formatInstant(value, timeZone = DEFAULT_TIME_ZONE) {
  if (!value) return "Sin registro";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  try {
    return new Intl.DateTimeFormat(LOCALE, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone,
    }).format(parsed);
  } catch (error) {
    if (error instanceof RangeError) return formatInstant(value, DEFAULT_TIME_ZONE);
    throw error;
  }
}

function formatCalendarDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat(LOCALE, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}

function marketResolution(value) {
  return MARKET_RESOLUTION_PRESENTATION[value] || MARKET_RESOLUTION_PRESENTATION.daily;
}

function formatMarketInterval(point) {
  const end = formatCalendarDate(point.timestamp);
  const interval =
    !point.period_start_timestamp || point.period_start_timestamp === point.timestamp
      ? end
      : `${formatCalendarDate(point.period_start_timestamp)}–${end}`;
  return point.calendar_interval_closed ? interval : `${interval} · En curso`;
}

function numericValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatNumber(value, options = {}) {
  const parsed = numericValue(value);
  if (parsed === null) return String(value ?? "—");
  return new Intl.NumberFormat(LOCALE, options).format(parsed);
}

function formatInteger(value) {
  return formatNumber(value, { maximumFractionDigits: 0 });
}

function formatScore(value) {
  return formatNumber(value, { maximumFractionDigits: 1 });
}

function formatConfidence(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return formatNumber(parsed, {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  });
}

function formatMetricValue(metric) {
  const presentation = METRIC_PRESENTATION[metric.metric_key];
  const kind = presentation?.kind;
  const parsed = numericValue(metric.value);
  if (parsed === null) return `${metric.value} ${metric.unit}`;
  if (kind === "percentage") {
    return formatNumber(parsed, {
      style: "percent",
      minimumFractionDigits: 0,
      maximumFractionDigits: 2,
    });
  }
  if (kind === "multiple") {
    return `${formatNumber(parsed, { maximumFractionDigits: 2 })}×`;
  }
  if (kind === "currency" || metric.unit === "USD") {
    return formatNumber(parsed, {
      style: "currency",
      currency: "USD",
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  return `${formatNumber(parsed, { maximumFractionDigits: 4 })} ${metric.unit}`;
}

function metricContext(metric) {
  const parameters = metric.parameters || {};
  const parts = [];
  if (parameters.window) {
    const windowLabel = Number(parameters.window) === 1
      ? "1 día con datos"
      : `${parameters.window} días con datos`;
    parts.push(`Ventana: ${windowLabel}`);
  }
  if (parameters.comparison === "year_over_year") parts.push("Comparación interanual");
  if (parameters.comparison === "same_period") parts.push("Mismo período");
  if (parameters.fiscal_period && parameters.fiscal_year) {
    parts.push(`${parameters.fiscal_period} · FY ${parameters.fiscal_year}`);
  }
  return parts.join(" · ") || "Cálculo determinista";
}

function formatAge(days) {
  if (!Number.isInteger(days) || days < 0) return "—";
  if (days === 0) return "Hoy";
  if (days === 1) return "Hace 1 día";
  return `Hace ${formatInteger(days)} días`;
}

function formatCurrency(value) {
  return formatNumber(value, {
    style: "currency",
    currency: "USD",
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatCompactVolume(value) {
  return formatNumber(value, {
    notation: "compact",
    compactDisplay: "short",
    maximumFractionDigits: 1,
  });
}

function formatRangeChange(value) {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatNumber(value, {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
}

function setSignedPercentage(target, value, title) {
  const parsed = numericValue(value);
  target.textContent = parsed === null ? "—" : formatRangeChange(parsed);
  target.className = `stat-move ${parsed > 0 ? "positive" : parsed < 0 ? "negative" : "neutral"}`;
  target.title = value === null || value === undefined ? "" : `${title}: ${value}`;
}

function formatMultiple(value) {
  const parsed = numericValue(value);
  return parsed === null ? "—" : `${formatNumber(parsed, { maximumFractionDigits: 2 })}×`;
}

function formatUnsignedPercentage(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return formatNumber(parsed, {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

function svgElement(tag, attributes = {}, text) {
  const element = document.createElementNS(SVG_NAMESPACE, tag);
  for (const [name, value] of Object.entries(attributes)) {
    element.setAttribute(name, String(value));
  }
  if (text !== undefined) element.textContent = text;
  return element;
}

function chartX(index, count) {
  const width = CHART_WIDTH - CHART_LAYOUT.left - CHART_LAYOUT.right;
  if (count <= 1) return CHART_LAYOUT.left + width / 2;
  return CHART_LAYOUT.left + (index / (count - 1)) * width;
}

function marketChartViewportBounds() {
  const total = marketChartPayload?.points?.length || 0;
  if (!marketChartViewport || total === 0) return { start: 0, end: total };
  const start = Math.max(0, Math.min(total - 1, marketChartViewport.start));
  const end = Math.max(start + 1, Math.min(total, marketChartViewport.end));
  return { start, end };
}

function visibleMarketChartPoints() {
  const points = marketChartPayload?.points || [];
  const { start, end } = marketChartViewportBounds();
  return points.slice(start, end);
}

function marketChartIsZoomed() {
  const total = marketChartPayload?.points?.length || 0;
  const { start, end } = marketChartViewportBounds();
  return total > 0 && (start > 0 || end < total);
}

function updateMarketChartZoomControl() {
  const zoomed = marketChartIsZoomed();
  byId("chart-zoom-reset").disabled = !zoomed;
  byId("market-chart").classList.toggle("is-zoomed", zoomed);
}

function renderZoomedMarketChart() {
  if (marketChartPayload === null) return;
  if (marketChartRenderFrame !== null) {
    window.cancelAnimationFrame(marketChartRenderFrame);
    marketChartRenderFrame = null;
  }
  renderMarketChart(marketChartPayload, { preserveViewport: true });
}

function scheduleZoomedMarketChart() {
  if (marketChartRenderFrame !== null) return;
  marketChartRenderFrame = window.requestAnimationFrame(() => {
    marketChartRenderFrame = null;
    if (marketChartPayload !== null) {
      renderMarketChart(marketChartPayload, { preserveViewport: true });
    }
  });
}

function zoomMarketChart(direction, anchorRatio = 0.5) {
  const total = marketChartPayload?.points?.length || 0;
  const minimum = Math.min(MINIMUM_CHART_VIEW_POINTS, total);
  if (total <= minimum || direction === 0) return false;
  const { start, end } = marketChartViewportBounds();
  const currentCount = end - start;
  const boundedAnchor = Math.max(0, Math.min(1, anchorRatio));
  let nextCount;
  if (direction < 0) {
    nextCount = Math.max(minimum, Math.floor(currentCount * 0.82));
    if (nextCount === currentCount && currentCount > minimum) nextCount -= 1;
  } else {
    nextCount = Math.min(total, Math.ceil(currentCount * 1.22));
    if (nextCount === currentCount && currentCount < total) nextCount += 1;
  }
  if (nextCount === currentCount) return false;
  if (nextCount === total) {
    marketChartViewport = null;
  } else {
    const anchorIndex = start + boundedAnchor * Math.max(currentCount - 1, 0);
    const desiredStart = Math.round(
      anchorIndex - boundedAnchor * Math.max(nextCount - 1, 0),
    );
    const nextStart = Math.max(0, Math.min(total - nextCount, desiredStart));
    marketChartViewport = { start: nextStart, end: nextStart + nextCount };
  }
  scheduleZoomedMarketChart();
  return true;
}

function resetMarketChartZoom() {
  if (!marketChartIsZoomed()) return false;
  marketChartDrag = null;
  byId("market-chart").classList.remove("is-panning");
  marketChartViewport = null;
  renderZoomedMarketChart();
  return true;
}

function handleMarketChartWheel(event) {
  if (!marketChartPayload?.points?.length || event.deltaY === 0) return;
  if (event.cancelable) event.preventDefault();
  const host = byId("market-chart");
  const bounds = host.getBoundingClientRect();
  const logicalX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH;
  const anchorRatio =
    (logicalX - CHART_LAYOUT.left) /
    (CHART_WIDTH - CHART_LAYOUT.left - CHART_LAYOUT.right);
  zoomMarketChart(event.deltaY, anchorRatio);
}

function panMarketChart(clientX) {
  if (!marketChartDrag || !marketChartIsZoomed()) return false;
  const total = marketChartPayload?.points?.length || 0;
  const hostWidth = byId("market-chart").getBoundingClientRect().width;
  const plotWidth =
    hostWidth * ((CHART_WIDTH - CHART_LAYOUT.left - CHART_LAYOUT.right) / CHART_WIDTH);
  const deltaPoints = Math.round(
    ((marketChartDrag.startClientX - clientX) / Math.max(plotWidth, 1)) *
      marketChartDrag.pointCount,
  );
  const nextStart = Math.max(
    0,
    Math.min(total - marketChartDrag.pointCount, marketChartDrag.startViewport + deltaPoints),
  );
  if (nextStart === marketChartViewportBounds().start) return false;
  marketChartViewport = {
    start: nextStart,
    end: nextStart + marketChartDrag.pointCount,
  };
  scheduleZoomedMarketChart();
  return true;
}

function endMarketChartDrag(event) {
  if (!marketChartDrag || marketChartDrag.pointerId !== event.pointerId) return;
  const host = byId("market-chart");
  if (host.hasPointerCapture(event.pointerId)) host.releasePointerCapture(event.pointerId);
  marketChartDrag = null;
  host.classList.remove("is-panning");
}

function pathData(values, yPosition) {
  let drawing = false;
  const commands = [];
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    if (value === null) {
      drawing = false;
      continue;
    }
    const command = drawing ? "L" : "M";
    commands.push(`${command}${chartX(index, values.length).toFixed(2)},${yPosition(value).toFixed(2)}`);
    drawing = true;
  }
  return commands.join(" ");
}

function appendCandlesticks(svg, values, yPosition) {
  const plotWidth = CHART_WIDTH - CHART_LAYOUT.left - CHART_LAYOUT.right;
  const candleWidth = Math.max(
    0.7,
    Math.min(11, (plotWidth / Math.max(values.length, 1)) * 0.62),
  );
  const paths = {
    positive: { wicks: [], bodies: [] },
    negative: { wicks: [], bodies: [] },
    neutral: { wicks: [], bodies: [] },
  };
  const ongoing = { wicks: [], bodies: [] };
  values.forEach((item, index) => {
    const tone = item.close > item.open ? "positive" : item.close < item.open ? "negative" : "neutral";
    const x = chartX(index, values.length);
    const highY = yPosition(item.high);
    const lowY = yPosition(item.low);
    const openY = yPosition(item.open);
    const closeY = yPosition(item.close);
    let top = Math.min(openY, closeY);
    let bottom = Math.max(openY, closeY);
    if (bottom - top < 1.2) {
      const center = (top + bottom) / 2;
      top = center - 0.6;
      bottom = center + 0.6;
    }
    const left = x - candleWidth / 2;
    const right = x + candleWidth / 2;
    const wick = `M${x.toFixed(2)},${highY.toFixed(2)}V${lowY.toFixed(2)}`;
    const body = `M${left.toFixed(2)},${top.toFixed(2)}H${right.toFixed(2)}V${bottom.toFixed(2)}H${left.toFixed(2)}Z`;
    paths[tone].wicks.push(wick);
    paths[tone].bodies.push(body);
    if (!item.calendarIntervalClosed) {
      ongoing.wicks.push(wick);
      ongoing.bodies.push(body);
    }
  });
  for (const [tone, commands] of Object.entries(paths)) {
    if (!commands.wicks.length) continue;
    svg.append(
      svgElement("path", {
        class: `candlestick-wicks ${tone}`,
        d: commands.wicks.join(""),
        "aria-hidden": "true",
      }),
      svgElement("path", {
        class: `candlestick-bodies ${tone}`,
        d: commands.bodies.join(""),
        "aria-hidden": "true",
      }),
    );
  }
  if (ongoing.wicks.length) {
    svg.append(
      svgElement("path", {
        class: "candlestick-current-wicks",
        d: ongoing.wicks.join(""),
        "aria-hidden": "true",
      }),
      svgElement("path", {
        class: "candlestick-current-bodies",
        d: ongoing.bodies.join(""),
        "aria-hidden": "true",
      }),
    );
  }
}

function chartValues(points) {
  return points.map((point) => {
    const open = numericValue(point.open);
    const high = numericValue(point.high);
    const low = numericValue(point.low);
    const close = numericValue(point.close);
    const volume = numericValue(point.volume);
    const shortSma = point.short_sma ? numericValue(point.short_sma.value) : null;
    const longSma = point.long_sma ? numericValue(point.long_sma.value) : null;
    const thirdSma = point.third_sma ? numericValue(point.third_sma.value) : null;
    if (
      open === null ||
      high === null ||
      low === null ||
      close === null ||
      volume === null ||
      (point.short_sma && shortSma === null) ||
      (point.long_sma && longSma === null) ||
      (point.third_sma && thirdSma === null)
    ) {
      throw new Error("El histórico contiene un valor que no puede representarse en el gráfico.");
    }
    if (typeof point.calendar_interval_closed !== "boolean") {
      throw new Error("El histórico no identifica si el intervalo de calendario está cerrado.");
    }
    return {
      open,
      high,
      low,
      close,
      volume,
      shortSma,
      longSma,
      thirdSma,
      calendarIntervalClosed: point.calendar_interval_closed,
    };
  });
}

function addPriceGrid(svg, minimum, maximum, yPosition, inverseScale) {
  const grid = svgElement("g", { class: "chart-grid", "aria-hidden": "true" });
  for (let index = 0; index < 5; index += 1) {
    const ratio = index / 4;
    const scaledValue = maximum - (maximum - minimum) * ratio;
    const value = inverseScale(scaledValue);
    const y = yPosition(value);
    grid.append(
      svgElement("line", {
        x1: CHART_LAYOUT.left,
        x2: CHART_WIDTH - CHART_LAYOUT.right,
        y1: y,
        y2: y,
      }),
      svgElement(
        "text",
        { x: CHART_LAYOUT.left - 10, y: y + 4, "text-anchor": "end" },
        formatNumber(value, { maximumFractionDigits: 2 }),
      ),
    );
  }
  svg.appendChild(grid);
}

function addDateAxis(svg, points) {
  const axis = svgElement("g", { class: "chart-date-axis", "aria-hidden": "true" });
  const labelCount = Math.min(points.length, 5);
  const indexes = new Set();
  for (let label = 0; label < labelCount; label += 1) {
    indexes.add(Math.round((label / Math.max(labelCount - 1, 1)) * (points.length - 1)));
  }
  for (const index of indexes) {
    axis.appendChild(
      svgElement(
        "text",
        { x: chartX(index, points.length), y: CHART_HEIGHT - 7, "text-anchor": "middle" },
        formatCalendarDate(points[index].timestamp),
      ),
    );
  }
  svg.appendChild(axis);
}

function renderChartSvg(points, resolution) {
  const values = chartValues(points);
  const prices = [];
  let maximumVolume = 1;
  for (const item of values) {
    if (chartSettings.chartType === "candlestick") prices.push(item.high, item.low);
    else prices.push(item.close);
    if (item.shortSma !== null) prices.push(item.shortSma);
    if (item.longSma !== null) prices.push(item.longSma);
    if (item.thirdSma !== null) prices.push(item.thirdSma);
    maximumVolume = Math.max(maximumVolume, item.volume);
  }
  const scalePrice = chartSettings.priceScale === "logarithmic" ? Math.log : (value) => value;
  const inverseScale =
    chartSettings.priceScale === "logarithmic" ? Math.exp : (value) => value;
  let minimum = Number.POSITIVE_INFINITY;
  let maximum = Number.NEGATIVE_INFINITY;
  for (const price of prices) {
    if (chartSettings.priceScale === "logarithmic" && price <= 0) {
      throw new Error("La escala logarítmica requiere precios mayores que cero.");
    }
    const scaledPrice = scalePrice(price);
    minimum = Math.min(minimum, scaledPrice);
    maximum = Math.max(maximum, scaledPrice);
  }
  const span = maximum - minimum || Math.max(Math.abs(maximum) * 0.02, 1);
  minimum -= span * 0.08;
  maximum += span * 0.08;
  const priceHeight = CHART_LAYOUT.priceBottom - CHART_LAYOUT.top;
  const yPrice = (value) =>
    CHART_LAYOUT.top +
    ((maximum - scalePrice(value)) / (maximum - minimum)) * priceHeight;
  const volumeHeight = CHART_LAYOUT.bottom - CHART_LAYOUT.volumeTop;
  const yVolume = (value) => CHART_LAYOUT.bottom - (value / maximumVolume) * volumeHeight;

  const svg = svgElement("svg", {
    class: "market-chart-svg",
    viewBox: `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`,
    role: "img",
    "aria-labelledby": "market-chart-svg-title market-chart-svg-description",
  });
  const resolutionText = marketResolution(resolution);
  svg.append(
    svgElement(
      "title",
      { id: "market-chart-svg-title" },
      `Histórico con puntos ${resolutionText.adjective} de Apple`,
    ),
    svgElement(
      "desc",
      { id: "market-chart-svg-description" },
      `${chartSettings.chartType === "candlestick" ? "Velas OHLC" : "Línea de cierre"}, SMA de ${chartSettings.shortWindow}, ${chartSettings.longWindow} y ${chartSettings.thirdWindow} ${resolutionText.plural}, escala ${chartSettings.priceScale === "logarithmic" ? "logarítmica" : "lineal"} y barras de volumen.`,
    ),
  );
  addPriceGrid(svg, minimum, maximum, yPrice, inverseScale);

  const volumeGroup = svgElement("g", { class: "volume-bars", "aria-hidden": "true" });
  const plotWidth = CHART_WIDTH - CHART_LAYOUT.left - CHART_LAYOUT.right;
  const barWidth = Math.max(1.5, Math.min(12, (plotWidth / Math.max(points.length, 1)) * 0.68));
  const volumeCommands = [];
  values.forEach((item, index) => {
    const x = chartX(index, values.length);
    const y = Math.min(yVolume(item.volume), CHART_LAYOUT.bottom - 0.8);
    volumeCommands.push(
      `M${x.toFixed(2)},${CHART_LAYOUT.bottom.toFixed(2)}V${y.toFixed(2)}`,
    );
  });
  volumeGroup.appendChild(
    svgElement("path", {
      d: volumeCommands.join(""),
      "stroke-width": barWidth.toFixed(2),
    }),
  );
  svg.appendChild(volumeGroup);

  if (chartSettings.chartType === "candlestick") {
    appendCandlesticks(svg, values, yPrice);
  }
  const series = [
    ...(chartSettings.chartType === "line"
      ? [["chart-line close-line", values.map((item) => item.close)]]
      : []),
    ["chart-line sma-five-line", values.map((item) => item.shortSma)],
    ["chart-line sma-twenty-line", values.map((item) => item.longSma)],
    ["chart-line sma-fifty-line", values.map((item) => item.thirdSma)],
  ];
  for (const [className, seriesValues] of series) {
    svg.appendChild(
      svgElement("path", {
        class: className,
        d: pathData(seriesValues, yPrice),
        "aria-hidden": "true",
      }),
    );
  }

  svg.append(
    svgElement("line", {
      id: "chart-selection-line",
      class: "chart-selection-line",
      x1: 0,
      x2: 0,
      y1: CHART_LAYOUT.top,
      y2: CHART_LAYOUT.bottom,
      "aria-hidden": "true",
    }),
    svgElement("circle", {
      id: "chart-selection-close",
      class: "chart-selection-point close",
      r: 5,
      "aria-hidden": "true",
    }),
    svgElement("circle", {
      id: "chart-selection-sma-5",
      class: "chart-selection-point sma-five",
      r: 4,
      "aria-hidden": "true",
    }),
    svgElement("circle", {
      id: "chart-selection-sma-20",
      class: "chart-selection-point sma-twenty",
      r: 4,
      "aria-hidden": "true",
    }),
    svgElement("circle", {
      id: "chart-selection-sma-50",
      class: "chart-selection-point sma-fifty",
      r: 4,
      "aria-hidden": "true",
    }),
  );
  addDateAxis(svg, points);

  const host = byId("market-chart");
  host.replaceChildren(svg);
  applySeriesVisibility();
  host.onpointerdown = (event) => {
    if (event.button !== 0 || !marketChartIsZoomed()) return;
    const { start, end } = marketChartViewportBounds();
    marketChartDrag = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startViewport: start,
      pointCount: end - start,
    };
    host.setPointerCapture(event.pointerId);
    host.classList.add("is-panning");
    host.focus({ preventScroll: true });
    event.preventDefault();
  };
  host.onpointermove = (event) => {
    if (marketChartDrag?.pointerId === event.pointerId) {
      panMarketChart(event.clientX);
      event.preventDefault();
      return;
    }
    const bounds = host.getBoundingClientRect();
    const logicalX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH;
    const plotRatio = (logicalX - CHART_LAYOUT.left) / plotWidth;
    const index = Math.round(plotRatio * Math.max(points.length - 1, 0));
    updateChartSelection(
      Math.max(0, Math.min(points.length - 1, index)),
      points,
      values,
      yPrice,
    );
  };
  host.onpointerup = endMarketChartDrag;
  host.onpointercancel = endMarketChartDrag;
  host.onkeydown = (event) => {
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomMarketChart(-1);
      return;
    }
    if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomMarketChart(1);
      return;
    }
    if (event.key === "0") {
      event.preventDefault();
      resetMarketChartZoom();
      return;
    }
    let next = selectedChartPoint;
    if (event.key === "ArrowLeft") next -= 1;
    else if (event.key === "ArrowRight") next += 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = points.length - 1;
    else return;
    event.preventDefault();
    updateChartSelection(
      Math.max(0, Math.min(points.length - 1, next)),
      points,
      values,
      yPrice,
    );
  };
  updateChartSelection(points.length - 1, points, values, yPrice);
}

function applySeriesVisibility() {
  const svg = document.querySelector(".market-chart-svg");
  if (!svg) return;
  for (const [series, visible] of Object.entries(chartSeriesVisibility)) {
    svg.classList.toggle(`hide-${series}`, !visible);
  }
}

function setSelectionPoint(id, x, value, yPosition) {
  const element = byId(id);
  if (value === null) {
    element.setAttribute("visibility", "hidden");
    return;
  }
  element.removeAttribute("visibility");
  element.setAttribute("cx", x.toFixed(2));
  element.setAttribute("cy", yPosition(value).toFixed(2));
}

function updateChartSelection(index, points, values, yPosition) {
  if (!points.length || !values[index]) return;
  selectedChartPoint = index;
  const point = points[index];
  const value = values[index];
  const x = chartX(index, points.length);
  const line = byId("chart-selection-line");
  line.setAttribute("x1", x.toFixed(2));
  line.setAttribute("x2", x.toFixed(2));
  setSelectionPoint("chart-selection-close", x, value.close, yPosition);
  setSelectionPoint("chart-selection-sma-5", x, value.shortSma, yPosition);
  setSelectionPoint("chart-selection-sma-20", x, value.longSma, yPosition);
  setSelectionPoint("chart-selection-sma-50", x, value.thirdSma, yPosition);
  byId("chart-point-date").textContent = formatMarketInterval(point);
  byId("chart-point-open").textContent = formatCurrency(point.open);
  byId("chart-point-high").textContent = formatCurrency(point.high);
  byId("chart-point-low").textContent = formatCurrency(point.low);
  byId("chart-point-close").textContent = formatCurrency(point.close);
  byId("chart-point-sma-5").textContent = point.short_sma
    ? formatCurrency(point.short_sma.value)
    : "En calentamiento";
  byId("chart-point-sma-20").textContent = point.long_sma
    ? formatCurrency(point.long_sma.value)
    : "En calentamiento";
  byId("chart-point-sma-50").textContent = point.third_sma
    ? formatCurrency(point.third_sma.value)
    : "En calentamiento";
  byId("chart-point-volume").textContent = `${formatInteger(point.volume)} acciones`;
}

function renderChartTable(points) {
  const body = byId("chart-table-body");
  body.replaceChildren();
  for (const point of points) {
    const row = document.createElement("tr");
    const values = [
      formatMarketInterval(point),
      formatCurrency(point.open),
      formatCurrency(point.high),
      formatCurrency(point.low),
      formatCurrency(point.close),
      point.vwap !== null ? formatCurrency(point.vwap) : "—",
      point.short_sma ? formatCurrency(point.short_sma.value) : "—",
      point.long_sma ? formatCurrency(point.long_sma.value) : "—",
      point.third_sma ? formatCurrency(point.third_sma.value) : "—",
      formatInteger(point.volume),
      point.trade_count !== null ? formatInteger(point.trade_count) : "—",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement(index === 0 ? "th" : "td");
      if (index === 0) cell.scope = "row";
      cell.textContent = value;
      row.appendChild(cell);
    });
    row.title = `OHLC exacto: ${point.open} / ${point.high} / ${point.low} / ${point.close}; volumen exacto: ${point.volume}`;
    body.appendChild(row);
  }
}

function chartStatistic(chart, metricKey) {
  return (chart.latest_statistics || []).find((item) => item.metric_key === metricKey) || null;
}

function renderMarketSnapshot(chart, latestSession, latestPoint) {
  const oneDayReturn = chartStatistic(chart, "market.history.simple_return_1d");
  const volatility = chartStatistic(chart, "market.history.rolling_daily_volatility");
  const relativeVolume = chartStatistic(chart, "market.history.relative_volume");
  const range = chart.range_statistics || {};
  const close = numericValue(latestSession.close);
  const shortSma = latestPoint.short_sma ? numericValue(latestPoint.short_sma.value) : null;
  const longSma = latestPoint.long_sma ? numericValue(latestPoint.long_sma.value) : null;
  const thirdSma = latestPoint.third_sma ? numericValue(latestPoint.third_sma.value) : null;

  setSignedPercentage(byId("snapshot-return-1d"), oneDayReturn?.value, "Retorno exacto");
  setSignedPercentage(
    byId("snapshot-range-return"),
    range.return_rate,
    "Retorno exacto del rango",
  );
  setSignedPercentage(
    byId("snapshot-range-cagr"),
    range.compound_annual_growth_rate,
    "CAGR exacto del rango",
  );
  setSignedPercentage(
    byId("snapshot-range-drawdown"),
    range.maximum_drawdown_rate,
    "Máximo drawdown exacto basado en cierres",
  );
  setSignedPercentage(
    byId("snapshot-sma-5-distance"),
    close !== null && shortSma ? close / shortSma - 1 : null,
    `Distancia mostrada a SMA ${chartSettings.shortWindow}`,
  );
  setSignedPercentage(
    byId("snapshot-sma-20-distance"),
    close !== null && longSma ? close / longSma - 1 : null,
    `Distancia mostrada a SMA ${chartSettings.longWindow}`,
  );
  setSignedPercentage(
    byId("snapshot-sma-50-distance"),
    close !== null && thirdSma ? close / thirdSma - 1 : null,
    `Distancia mostrada a SMA ${chartSettings.thirdWindow}`,
  );

  byId("snapshot-vwap").textContent =
    latestSession.vwap !== null ? formatCurrency(latestSession.vwap) : "—";
  byId("snapshot-open").textContent = formatCurrency(latestSession.open);
  byId("snapshot-day-range").textContent = `${formatCurrency(latestSession.high)} / ${formatCurrency(latestSession.low)}`;
  byId("snapshot-volume").textContent = formatCompactVolume(latestSession.volume);
  byId("snapshot-volume").title = `${formatInteger(latestSession.volume)} acciones`;
  byId("snapshot-trades").textContent = latestSession.trade_count !== null
    ? formatInteger(latestSession.trade_count)
    : "—";
  byId("snapshot-volatility").textContent = volatility
    ? formatUnsignedPercentage(volatility.value)
    : "—";
  byId("snapshot-volatility").title = volatility ? `Valor exacto: ${volatility.value}` : "";
  byId("snapshot-relative-volume").textContent = relativeVolume
    ? formatMultiple(relativeVolume.value)
    : "—";
  byId("snapshot-relative-volume").title = relativeVolume
    ? `Valor exacto: ${relativeVolume.value}`
    : "";
  byId("snapshot-range-high").textContent = range.high ? formatCurrency(range.high) : "—";
  byId("snapshot-range-low").textContent = range.low ? formatCurrency(range.low) : "—";
  byId("snapshot-quality").textContent = translated(
    latestSession.quality,
    QUALITY_LABELS,
    latestSession.quality,
  );
  byId("snapshot-quality").className = `quality-chip ${statusTone(latestSession.quality)}`;
}

function resetMarketSnapshot() {
  const ids = [
    "snapshot-return-1d",
    "snapshot-vwap",
    "snapshot-open",
    "snapshot-day-range",
    "snapshot-volume",
    "snapshot-trades",
    "snapshot-volatility",
    "snapshot-relative-volume",
    "snapshot-sma-5-distance",
    "snapshot-sma-20-distance",
    "snapshot-sma-50-distance",
    "snapshot-range-return",
    "snapshot-range-cagr",
    "snapshot-range-drawdown",
    "snapshot-range-high",
    "snapshot-range-low",
    "chart-visible-sessions",
    "chart-latest-sma-5",
    "chart-latest-sma-20",
    "chart-latest-sma-50",
  ];
  for (const id of ids) byId(id).textContent = "—";
  byId("snapshot-quality").textContent = "—";
  byId("snapshot-quality").className = "quality-chip";
}

function renderMarketChart(chart, { preserveViewport = false } = {}) {
  if (marketChartRenderFrame !== null) {
    window.cancelAnimationFrame(marketChartRenderFrame);
    marketChartRenderFrame = null;
  }
  if (
    chart.period !== MARKET_CHART_PERIOD ||
    !Array.isArray(chart.sma_windows) ||
    chart.sma_windows.length !== 3 ||
    chart.sma_windows[0] !== chartSettings.shortWindow ||
    chart.sma_windows[1] !== chartSettings.longWindow ||
    chart.sma_windows[2] !== chartSettings.thirdWindow ||
    chart.interval !== chartSettings.interval
  ) {
    throw new Error("El gráfico local no respetó la configuración de medias móviles solicitada.");
  }
  const payloadChanged = marketChartPayload !== chart;
  marketChartPayload = chart;
  if (!preserveViewport || payloadChanged) marketChartViewport = null;
  selectedChartPoint = -1;
  const allPoints = chart.points || [];
  const points = visibleMarketChartPoints();
  setExportAvailable("export-market-csv", points.length > 0);
  updateMarketChartZoomControl();
  const empty = byId("chart-empty");
  if (!allPoints.length) {
    resetMarketSnapshot();
    empty.textContent = "No hay precios disponibles para este corte histórico.";
    empty.classList.remove("hidden");
    byId("market-chart").replaceChildren();
    byId("chart-table-body").replaceChildren();
    byId("chart-status").textContent = `Corte: ${formatInstant(chart.known_at)} · sin precios disponibles.`;
    return;
  }
  empty.classList.add("hidden");
  const latestPoint = allPoints[allPoints.length - 1];
  const latest = chart.latest_session || latestPoint;
  const oneDayReturn = chartStatistic(chart, "market.history.simple_return_1d");
  const dailyChange = numericValue(oneDayReturn?.value);
  byId("chart-latest-close").textContent = formatCurrency(latest.close);
  byId("chart-latest-date").textContent = `Cierre del ${formatCalendarDate(latest.timestamp)}`;
  const change = byId("chart-range-change");
  change.textContent = `${formatRangeChange(dailyChange)} variación diaria`;
  change.className = `chart-change ${dailyChange > 0 ? "positive" : dailyChange < 0 ? "negative" : "neutral"}`;
  byId("chart-latest-sma-5").textContent = latestPoint.short_sma
    ? formatCurrency(latestPoint.short_sma.value)
    : "—";
  byId("chart-latest-sma-20").textContent = latestPoint.long_sma
    ? formatCurrency(latestPoint.long_sma.value)
    : "—";
  byId("chart-latest-sma-50").textContent = latestPoint.third_sma
    ? formatCurrency(latestPoint.third_sma.value)
    : "—";
  byId("chart-visible-sessions").textContent = formatInteger(chart.coverage.selected_sessions);
  renderMarketSnapshot(chart, latest, latestPoint);
  const coverageStart = formatCalendarDate(chart.coverage.earliest_available_timestamp);
  const coverageEnd = formatCalendarDate(chart.coverage.latest_available_timestamp);
  const resolutionText = marketResolution(chart.resolution);
  byId("chart-point-period-label").textContent = resolutionText.singular;
  byId("chart-keyboard-hint").textContent =
    "Rueda: zoom · Arrastrar: desplazar · ← → recorrer";
  byId("market-chart").setAttribute(
    "aria-label",
    `Gráfico histórico interactivo de AAPL con puntos ${resolutionText.adjective}. Usa la rueda del mouse o las teclas más y menos para cambiar el zoom, arrastra horizontalmente para desplazar la vista, cero para restablecerla y las flechas para recorrer los puntos.`,
  );
  byId("chart-data-caption").textContent = `Puntos ${resolutionText.adjective} visibles en el gráfico, ordenados cronológicamente`;
  const currentInterval = latestPoint.calendar_interval_closed ? "" : " · último intervalo en curso";
  const viewportStatus = marketChartIsZoomed()
    ? ` · mostrando ${formatInteger(points.length)} de ${formatInteger(allPoints.length)} puntos`
    : "";
  byId("chart-status").textContent = `Historial completo: ${formatInteger(chart.coverage.selected_sessions)} días con datos en ${formatInteger(chart.coverage.displayed_points)} puntos ${resolutionText.adjective}${viewportStatus} · fechas ${coverageStart}–${coverageEnd} · ${formatInteger(chart.coverage.discarded_revisions)} revisiones descartadas${currentInterval} · corte ${formatInstant(chart.known_at)}`;
  renderChartSvg(points, chart.resolution);
  const disclosure = byId("chart-data-disclosure");
  if (disclosure.open) renderChartTable(points);
  else byId("chart-table-body").replaceChildren();
}

function setChartBusy(busy) {
  byId("market-chart-card").setAttribute("aria-busy", String(busy));
  for (const button of document.querySelectorAll(".chart-type-button")) button.disabled = busy;
  byId("chart-interval").disabled = busy;
  if (busy) byId("chart-zoom-reset").disabled = true;
  else updateMarketChartZoomControl();
  for (const control of document.querySelectorAll("#chart-settings-form input, #chart-settings-form select, #chart-settings-form button")) {
    control.disabled = busy;
  }
}

async function queryMarketChart() {
  marketChartDrag = null;
  byId("market-chart").classList.remove("is-panning");
  setChartBusy(true);
  setExportAvailable("export-market-csv", false);
  byId("chart-status").textContent = "Consultando el histórico local…";
  const parameters = new URLSearchParams({
    known_at: byId("report-known-at").value.trim(),
    period: MARKET_CHART_PERIOD,
    interval: chartSettings.interval,
    short_sma_window: String(chartSettings.shortWindow),
    long_sma_window: String(chartSettings.longWindow),
    third_sma_window: String(chartSettings.thirdWindow),
  });
  try {
    renderMarketChart(await api(`/api/market-chart?${parameters.toString()}`));
  } catch (error) {
    marketChartPayload = null;
    marketChartViewport = null;
    updateMarketChartZoomControl();
    setExportAvailable("export-market-csv", false);
    byId("market-chart").replaceChildren();
    byId("chart-table-body").replaceChildren();
    const empty = byId("chart-empty");
    empty.textContent = error.message;
    empty.classList.remove("hidden");
    byId("chart-status").textContent = "El gráfico no pudo construirse para el corte solicitado.";
  } finally {
    setChartBusy(false);
  }
}

function formatUsdBillions(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return `$ ${formatNumber(parsed / 1_000_000_000, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  })} mil M`;
}

function formatSharesBillions(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return `${formatNumber(parsed / 1_000_000_000, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })} mil M`;
}

function formatCurrencyPerShare(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return formatNumber(parsed, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

function formatFundamentalResearchValue(metric) {
  const presentation = FUNDAMENTAL_RESEARCH_PRESENTATION[metric.metric_key];
  const parsed = numericValue(metric.value);
  if (parsed === null) return `${metric.value} ${metric.unit}`;
  if (presentation?.kind === "percentage") {
    return formatNumber(parsed, {
      style: "percent",
      minimumFractionDigits: 0,
      maximumFractionDigits: 1,
    });
  }
  if (presentation?.kind === "multiple") {
    return `${formatNumber(parsed, { maximumFractionDigits: 2 })}×`;
  }
  if (presentation?.kind === "currency_per_share") {
    return formatCurrencyPerShare(metric.value);
  }
  if (presentation?.kind === "shares") {
    return formatSharesBillions(metric.value);
  }
  if (presentation?.kind === "currency" || metric.unit === "USD") {
    return formatUsdBillions(metric.value);
  }
  return formatNumber(parsed, { maximumFractionDigits: 2 });
}

function formatSignedNumber(value, options = {}) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return `${parsed > 0 ? "+" : ""}${formatNumber(parsed, options)}`;
}

function fundamentalResearchTrend(metric, history, frequency) {
  const statistics = history?.statistics;
  if (!statistics || statistics.point_count < 2) {
    return { text: "Sin comparación histórica", direction: null };
  }
  const presentation = FUNDAMENTAL_RESEARCH_PRESENTATION[metric.metric_key];
  let comparison = null;
  let text = "Sin comparación histórica";
  if (
    ["USD", "shares", "USD/shares"].includes(metric.unit) &&
    frequency === "annual" &&
    statistics.compound_annual_growth_rate !== null &&
    statistics.compound_annual_growth_rate !== undefined
  ) {
    comparison = numericValue(statistics.compound_annual_growth_rate);
    text = `CAGR ${formatRangeChange(comparison)}`;
  } else if (
    ["USD", "shares", "USD/shares"].includes(metric.unit) &&
    statistics.latest_change_rate_from_previous_available !== null &&
    statistics.latest_change_rate_from_previous_available !== undefined
  ) {
    comparison = numericValue(statistics.latest_change_rate_from_previous_available);
    text = `Vs. período anterior ${formatRangeChange(comparison)}`;
  } else {
    const delta = numericValue(statistics.latest_change_from_previous_available);
    if (delta === null) return { text, direction: null };
    comparison = delta;
    if (presentation?.kind === "percentage") {
      text = `Vs. período anterior ${formatSignedNumber(delta * 100, {
        maximumFractionDigits: 1,
      })} pp`;
    } else if (presentation?.kind === "multiple") {
      text = `Vs. período anterior ${formatSignedNumber(delta, {
        maximumFractionDigits: 2,
      })}×`;
    } else if (presentation?.kind === "currency_per_share") {
      text = `Cambio ${formatSignedNumber(delta, {
        maximumFractionDigits: 2,
      })} USD/acción`;
    } else if (presentation?.kind === "shares") {
      text = `Cambio ${formatSignedNumber(delta / 1_000_000, {
        maximumFractionDigits: 1,
      })} M acciones`;
    } else {
      text = `Cambio ${formatUsdBillions(delta)}`;
    }
  }
  const direction = comparison > 0 ? "increase" : comparison < 0 ? "decrease" : "unchanged";
  const arrow = direction === "increase" ? "↑" : direction === "decrease" ? "↓" : "→";
  return { text: `${arrow} ${text}`, direction };
}

function fundamentalResearchMetricCard(metricKey, metric, definition, history, frequency) {
  const presentation = FUNDAMENTAL_RESEARCH_PRESENTATION[metricKey];
  const card = createElement(
    "article",
    `fundamental-research-metric${metric ? "" : " unavailable"}`,
  );
  card.setAttribute("role", "listitem");
  const label = createElement(
    "span",
    "fundamental-research-metric-name",
    presentation?.label || metric?.display_name_es || definition?.display_name_es || metricKey,
  );
  const value = createElement(
    "strong",
    "fundamental-research-metric-value",
    metric ? formatFundamentalResearchValue(metric) : "—",
  );
  const trendDetails = metric
    ? fundamentalResearchTrend(metric, history, frequency)
    : { text: "Sin datos", direction: null };
  const trend = createElement(
    "small",
    "fundamental-research-metric-change",
    trendDetails.text,
  );
  if (trendDetails.direction) trend.classList.add(trendDetails.direction);
  trend.title = trendDetails.direction
    ? "El color y la flecha indican dirección del cambio, no una valoración de calidad."
    : "";
  if (metric) {
    value.title = `Valor exacto: ${metric.value} ${metric.unit}`;
    const statistics = history?.statistics;
    const historyTitle = statistics
      ? ` · media exacta ${statistics.arithmetic_mean} · rango exacto ${statistics.range}`
      : "";
    card.title = `${metric.formula} · disponible ${formatInstant(metric.available_at)}${historyTitle}`;
    const parsed = numericValue(metric.value);
    if (parsed !== null && parsed < 0) value.classList.add("negative");
  } else {
    card.title = definition
      ? `Sin inputs suficientes para ${definition.formula}`
      : "Métrica no disponible para este período";
  }
  card.append(label, value, trend);
  return card;
}

function fundamentalResearchAuditItem(metric, history) {
  const presentation = FUNDAMENTAL_RESEARCH_PRESENTATION[metric.metric_key];
  const item = createElement("article", "fundamental-research-audit-item");
  const heading = createElement("div", "fundamental-research-audit-heading");
  const title = createElement(
    "strong",
    "",
    presentation?.label || metric.display_name_es,
  );
  const exact = createElement(
    "span",
    "fundamental-research-exact-value",
    `${metric.value} ${metric.unit}`,
  );
  heading.append(title, exact);

  const formula = createElement("code", "fundamental-research-formula", metric.formula);
  const metadata = createElement(
    "small",
    "fundamental-research-audit-meta",
    `${metric.algorithm_version} · disponible ${formatInstant(metric.available_at)}`,
  );
  const inputs = createElement("ul", "fundamental-research-inputs");
  for (const input of metric.inputs || []) {
    const row = document.createElement("li");
    const name = createElement("span", "", input.role.replaceAll("_", " "));
    const evidence = createElement(
      "code",
      "",
      `${input.value} ${input.unit} · ${input.observation_id}`,
    );
    evidence.title = `${input.field_name} · disponible ${input.available_at}`;
    row.append(name, evidence);
    inputs.appendChild(row);
  }
  item.append(heading, formula, metadata);
  if (history?.statistics) {
    const statistics = history.statistics;
    const summary = createElement("dl", "fundamental-research-history-statistics");
    const previousChange = numericValue(
      statistics.latest_change_from_previous_available,
    );
    const previousDirection =
      previousChange === null
        ? null
        : previousChange > 0
          ? "increase"
          : previousChange < 0
            ? "decrease"
            : "unchanged";
    const previousArrow =
      previousDirection === "increase"
        ? "↑"
        : previousDirection === "decrease"
          ? "↓"
          : previousDirection === "unchanged"
            ? "→"
            : "";
    const values = [
      ["Puntos", String(statistics.point_count), null],
      ["Media exacta", `${statistics.arithmetic_mean} ${metric.unit}`, null],
      ["Rango exacto", `${statistics.range} ${metric.unit}`, null],
      [
        "Cambio anterior",
        previousChange === null
          ? "No calculable"
          : `${previousArrow} ${statistics.latest_change_from_previous_available} ${metric.unit}`,
        previousDirection,
      ],
      [
        "CAGR",
        statistics.compound_annual_growth_rate === null
          ? "No calculable"
          : statistics.compound_annual_growth_rate,
        null,
      ],
    ];
    for (const [name, value, direction] of values) {
      const row = document.createElement("div");
      const output = createElement("dd", "", value);
      if (direction) output.classList.add("fundamental-history-change", direction);
      row.append(createElement("dt", "", name), output);
      summary.appendChild(row);
    }
    item.appendChild(summary);
  }
  item.appendChild(inputs);
  return item;
}

function resetFundamentalResearch() {
  byId("fundamental-research-grid").replaceChildren();
  byId("fundamental-research-audit").replaceChildren();
  byId("fundamental-research-context").textContent = "Sin métricas disponibles";
  byId("fundamental-research-coverage").textContent = "—";
  byId("fundamental-research-coverage").className = "quality-chip";
}

function resetCompanyProfile() {
  byId("company-profile-title").textContent = "Clasificación no determinada";
  byId("company-profile-status").textContent = "Evidencia insuficiente";
  byId("company-profile-status").className = "quality-chip warn";
  byId("company-profile-explanation").textContent = "Sin evidencia disponible.";
  byId("company-profile-categories").replaceChildren();
  byId("company-profile-requirements-summary").textContent =
    "Datos necesarios para clasificar";
  byId("company-profile-requirements-list").replaceChildren();
}

function renderCompanyProfile(classification) {
  if (!classification) {
    resetCompanyProfile();
    return;
  }
  const selected = (classification.categories || []).find(
    (category) => category.category_key === classification.selected_category,
  );
  byId("company-profile-title").textContent =
    selected?.display_name_es || "Clasificación no determinada";
  const status = byId("company-profile-status");
  status.textContent = selected ? "Clasificación disponible" : "Evidencia insuficiente";
  status.className = `quality-chip ${selected ? "good" : "warn"}`;
  byId("company-profile-explanation").textContent = classification.explanation_es;
  byId("company-profile-categories").replaceChildren(
    ...(classification.categories || []).map((category) => {
      const chip = createElement(
        "span",
        `company-profile-category${
          category.category_key === classification.selected_category ? " selected" : ""
        }`,
        category.display_name_es,
      );
      chip.title = category.description_es;
      return chip;
    }),
  );
  const requirements = classification.missing_requirements || [];
  byId("company-profile-requirements-summary").textContent = selected
    ? "Evidencia utilizada para clasificar"
    : `${formatInteger(requirements.length)} datos necesarios para clasificar`;
  byId("company-profile-requirements-list").replaceChildren(
    ...requirements.map((requirement) => createElement("li", "", requirement)),
  );
}

function renderFundamentalResearch(payload) {
  fundamentalResearchPayload = payload;
  const historyPayload = payload.history || payload;
  const research = historyPayload.research || historyPayload;
  const histories = new Map(
    (historyPayload.series || []).map((history) => [history.metric_key, history]),
  );
  renderCompanyProfile(payload.classification);
  const periods = research.periods || [];
  const empty = byId("fundamental-research-empty");
  setExportAvailable("export-fundamental-research-csv", periods.length > 0);
  if (!periods.length) {
    resetFundamentalResearch();
    empty.textContent = "No hay métricas fundamentales disponibles para este corte histórico.";
    empty.classList.remove("hidden");
    return;
  }

  empty.classList.add("hidden");
  const latest = periods[periods.length - 1];
  const metrics = new Map((latest.metrics || []).map((metric) => [metric.metric_key, metric]));
  const definitions = new Map(
    (research.definitions || []).map((definition) => [definition.metric_key, definition]),
  );
  const grid = byId("fundamental-research-grid");
  grid.replaceChildren();
  for (const section of payload.sections || []) {
    const group = createElement("section", "fundamental-research-group");
    const heading = createElement("h4", "", section.definition.display_name_es);
    heading.title = section.definition.scope_es;
    group.appendChild(heading);
    const values = createElement("div", "fundamental-research-group-grid");
    values.setAttribute("role", "list");
    for (const reference of section.definition.metric_references || []) {
      values.appendChild(
        fundamentalResearchMetricCard(
          reference.metric_key,
          metrics.get(reference.metric_key),
          definitions.get(reference.metric_key),
          histories.get(reference.metric_key),
          research.request?.frequency,
        ),
      );
      values.lastElementChild.title = `${reference.relevance_es} ${
        values.lastElementChild.title || ""
      }`.trim();
    }
    group.appendChild(values);
    grid.appendChild(group);
  }

  const audit = byId("fundamental-research-audit");
  audit.replaceChildren(
    ...[...metrics.values()].map((metric) =>
      fundamentalResearchAuditItem(metric, histories.get(metric.metric_key)),
    ),
  );
  const frequency = research.request?.frequency === "annual" ? "Anual" : "Trimestral";
  byId("fundamental-research-context").textContent = `${frequency} · ${formatInteger(
    periods.length,
  )} períodos · cierre ${formatCalendarDate(latest.period_end)}`;
  const coverage = byId("fundamental-research-coverage");
  coverage.textContent = `${formatInteger(payload.coverage.latest_period_metrics)}/${formatInteger(
    payload.coverage.expected_metrics,
  )} métricas`;
  coverage.className = `quality-chip ${
    payload.coverage.latest_period_metrics === payload.coverage.expected_metrics ? "good" : "warn"
  }`;
}

function factsByField(period) {
  return new Map((period?.facts || []).map((fact) => [fact.field_name, fact]));
}

function fundamentalPeriodLabel(period, short = false) {
  const reference = period?.facts?.[0];
  const fiscalYear = reference?.fiscal_year;
  const fiscalPeriod = reference?.fiscal_period;
  if (fiscalYear && fiscalPeriod) {
    return short ? `${fiscalPeriod} ${String(fiscalYear).slice(-2)}` : `${fiscalPeriod} · FY ${fiscalYear}`;
  }
  return formatCalendarDate(period?.period_end);
}

function renderFundamentalChart(periods) {
  const points = periods.map((period) => {
    const facts = factsByField(period);
    return {
      period,
      revenue: numericValue(facts.get("fundamental.revenue")?.value),
      netIncome: numericValue(facts.get("fundamental.net_income")?.value),
    };
  });
  const values = points
    .flatMap((point) => [point.revenue, point.netIncome])
    .filter((value) => value !== null);
  if (!values.length) {
    byId("fundamental-chart").replaceChildren();
    return;
  }

  const maximum = Math.max(...values, 1);
  const minimum = Math.min(...values, 0);
  const span = maximum - minimum || 1;
  const plotWidth =
    FUNDAMENTAL_CHART_WIDTH - FUNDAMENTAL_CHART_LAYOUT.left - FUNDAMENTAL_CHART_LAYOUT.right;
  const plotHeight = FUNDAMENTAL_CHART_LAYOUT.bottom - FUNDAMENTAL_CHART_LAYOUT.top;
  const groupWidth = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.min(30, Math.max(8, groupWidth * 0.28));
  const yPosition = (value) =>
    FUNDAMENTAL_CHART_LAYOUT.top + ((maximum - value) / span) * plotHeight;
  const zeroY = yPosition(0);

  const svg = svgElement("svg", {
    class: "fundamental-chart-svg",
    viewBox: `0 0 ${FUNDAMENTAL_CHART_WIDTH} ${FUNDAMENTAL_CHART_HEIGHT}`,
    role: "img",
    "aria-labelledby": "fundamental-chart-title fundamental-chart-description",
  });
  svg.append(
    svgElement("title", { id: "fundamental-chart-title" }, "Evolución fundamental de Apple"),
    svgElement(
      "desc",
      { id: "fundamental-chart-description" },
      "Barras de ingresos y resultado neto por período fiscal, expresadas en miles de millones de dólares.",
    ),
  );

  const grid = svgElement("g", { class: "fundamental-grid", "aria-hidden": "true" });
  for (let index = 0; index < 5; index += 1) {
    const ratio = index / 4;
    const value = maximum - span * ratio;
    const y = FUNDAMENTAL_CHART_LAYOUT.top + plotHeight * ratio;
    grid.append(
      svgElement("line", {
        x1: FUNDAMENTAL_CHART_LAYOUT.left,
        x2: FUNDAMENTAL_CHART_WIDTH - FUNDAMENTAL_CHART_LAYOUT.right,
        y1: y,
        y2: y,
      }),
      svgElement(
        "text",
        { x: FUNDAMENTAL_CHART_LAYOUT.left - 10, y: y + 4, "text-anchor": "end" },
        formatNumber(value / 1_000_000_000, { maximumFractionDigits: 0 }),
      ),
    );
  }
  svg.appendChild(grid);

  const bars = svgElement("g", { class: "fundamental-bars" });
  points.forEach((point, index) => {
    const center = FUNDAMENTAL_CHART_LAYOUT.left + groupWidth * (index + 0.5);
    const series = [
      ["revenue", point.revenue, center - barWidth - 2, "Ingresos"],
      ["net-income", point.netIncome, center + 2, "Resultado neto"],
    ];
    for (const [className, value, x, label] of series) {
      if (value === null) continue;
      const valueY = yPosition(value);
      const y = Math.min(valueY, zeroY);
      const rectangle = svgElement("rect", {
        class: `fundamental-bar ${className}`,
        x: x.toFixed(2),
        y: y.toFixed(2),
        width: barWidth.toFixed(2),
        height: Math.max(Math.abs(zeroY - valueY), 1).toFixed(2),
      });
      rectangle.appendChild(
        svgElement(
          "title",
          {},
          `${fundamentalPeriodLabel(point.period)} · ${label}: ${formatUsdBillions(value)}`,
        ),
      );
      bars.appendChild(rectangle);
    }
    bars.appendChild(
      svgElement(
        "text",
        {
          class: "fundamental-period-label",
          x: center,
          y: FUNDAMENTAL_CHART_LAYOUT.bottom + 24,
          "text-anchor": "middle",
        },
        fundamentalPeriodLabel(point.period, true),
      ),
    );
  });
  svg.appendChild(bars);
  byId("fundamental-chart").replaceChildren(svg);
}

function setFundamentalFact(id, fact) {
  const target = byId(id);
  target.textContent = fact ? formatUsdBillions(fact.value) : "—";
  target.title = fact ? `Valor exacto: ${fact.value} ${fact.unit}` : "";
}

function renderFundamentalTable(periods) {
  const body = byId("fundamental-table-body");
  body.replaceChildren();
  for (const period of periods) {
    const facts = factsByField(period);
    const fields = [
      "fundamental.revenue",
      "fundamental.net_income",
      "fundamental.assets",
      "fundamental.liabilities",
      "fundamental.stockholders_equity",
    ];
    const row = document.createElement("tr");
    const published = period.latest_available_at;
    const values = [
      fundamentalPeriodLabel(period),
      ...fields.map((field) => {
        const fact = facts.get(field);
        return fact ? formatUsdBillions(fact.value) : "—";
      }),
      formatCalendarDate(published),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement(index === 0 ? "th" : "td");
      if (index === 0) cell.scope = "row";
      cell.textContent = value;
      row.appendChild(cell);
    });
    row.title = fields
      .map((field) => `${field}: ${facts.get(field)?.value ?? "no disponible"}`)
      .join("; ");
    body.appendChild(row);
  }
}

function resetFundamentalTrend() {
  for (const id of [
    "fundamental-revenue",
    "fundamental-net-income",
    "fundamental-assets",
    "fundamental-liabilities",
    "fundamental-equity",
    "fundamental-form",
  ]) {
    byId(id).textContent = "—";
  }
  byId("fundamental-latest-context").textContent = "Sin período fundamental disponible";
  byId("fundamental-completeness").textContent = "—";
  byId("fundamental-completeness").className = "quality-chip";
}

function renderFundamentalTrend(trend) {
  fundamentalTrendPayload = trend;
  const periods = trend.periods || [];
  setExportAvailable("export-fundamental-csv", periods.length > 0);
  const empty = byId("fundamental-empty");
  if (!periods.length) {
    resetFundamentalTrend();
    empty.textContent = "No hay períodos fundamentales disponibles para este corte histórico.";
    empty.classList.remove("hidden");
    byId("fundamental-chart").replaceChildren();
    byId("fundamental-table-body").replaceChildren();
    byId("fundamental-status").textContent = `Corte: ${formatInstant(trend.known_at)} · sin períodos disponibles.`;
    return;
  }
  empty.classList.add("hidden");
  const latest = periods[periods.length - 1];
  const facts = factsByField(latest);
  setFundamentalFact("fundamental-revenue", facts.get("fundamental.revenue"));
  setFundamentalFact("fundamental-net-income", facts.get("fundamental.net_income"));
  setFundamentalFact("fundamental-assets", facts.get("fundamental.assets"));
  setFundamentalFact("fundamental-liabilities", facts.get("fundamental.liabilities"));
  setFundamentalFact("fundamental-equity", facts.get("fundamental.stockholders_equity"));
  const forms = [...new Set(latest.facts.map((fact) => fact.form).filter(Boolean))];
  byId("fundamental-form").textContent = forms.join(" / ") || "—";
  byId("fundamental-latest-context").textContent = `${fundamentalPeriodLabel(latest)} · cierre ${formatCalendarDate(latest.period_end)}`;
  const completeness = byId("fundamental-completeness");
  completeness.textContent = latest.is_complete ? "Completo" : "Incompleto";
  completeness.className = `quality-chip ${latest.is_complete ? "good" : "warn"}`;
  byId("fundamental-status").textContent = `${formatInteger(trend.coverage.periods_returned)} períodos · ${formatInteger(trend.coverage.observations_selected)} hechos seleccionados de ${formatInteger(trend.coverage.observations_examined)} observaciones examinadas · corte ${formatInstant(trend.known_at)}`;
  renderFundamentalChart(periods);
  renderFundamentalTable(periods);
}

function renderFundamentalRatios(section) {
  const targets = new Map([
    ["fundamental.net_margin", "fundamental-net-margin"],
    ["fundamental.revenue_yoy_growth", "fundamental-revenue-growth"],
    ["fundamental.liabilities_to_assets", "fundamental-liabilities-assets"],
    ["fundamental.net_income_yoy_change_rate", "fundamental-income-growth"],
  ]);
  for (const id of targets.values()) byId(id).textContent = "—";
  for (const metric of section?.metrics || []) {
    const id = targets.get(metric.metric_key);
    if (!id) continue;
    const target = byId(id);
    target.textContent = formatMetricValue(metric);
    target.title = `Valor exacto: ${metric.value} ${metric.unit}`;
  }
}

function setFundamentalBusy(busy) {
  fundamentalBusyCount = Math.max(0, fundamentalBusyCount + (busy ? 1 : -1));
  const active = fundamentalBusyCount > 0;
  byId("fundamental-trend-card").setAttribute("aria-busy", String(active));
  byId("fundamental-research-panel").setAttribute("aria-busy", String(active));
  byId("company-profile").setAttribute("aria-busy", String(active));
  for (const button of document.querySelectorAll(".frequency-button")) button.disabled = active;
}

function selectFundamentalFrequency(frequency) {
  selectedFundamentalFrequency = frequency === "annual" ? "annual" : "quarterly";
  byId("report-frequency").value = selectedFundamentalFrequency;
  for (const button of document.querySelectorAll(".frequency-button")) {
    const active = button.dataset.frequency === selectedFundamentalFrequency;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
}

async function queryFundamentalTrend() {
  setFundamentalBusy(true);
  setExportAvailable("export-fundamental-csv", false);
  byId("fundamental-status").textContent = "Consultando fundamentales locales…";
  const parameters = new URLSearchParams({
    known_at: byId("report-known-at").value.trim(),
    frequency: selectedFundamentalFrequency,
  });
  try {
    renderFundamentalTrend(await api(`/api/fundamental-trend?${parameters.toString()}`));
  } catch (error) {
    fundamentalTrendPayload = null;
    setExportAvailable("export-fundamental-csv", false);
    resetFundamentalTrend();
    byId("fundamental-chart").replaceChildren();
    byId("fundamental-table-body").replaceChildren();
    const empty = byId("fundamental-empty");
    empty.textContent = error.message;
    empty.classList.remove("hidden");
    byId("fundamental-status").textContent = "La tendencia fundamental no pudo construirse.";
  } finally {
    setFundamentalBusy(false);
  }
}

async function queryFundamentalResearch() {
  setFundamentalBusy(true);
  setExportAvailable("export-fundamental-research-csv", false);
  const parameters = new URLSearchParams({
    known_at: byId("report-known-at").value.trim(),
    frequency: selectedFundamentalFrequency,
  });
  try {
    renderFundamentalResearch(
      await api(`/api/fundamental-analysis?${parameters.toString()}`),
    );
  } catch (error) {
    fundamentalResearchPayload = null;
    setExportAvailable("export-fundamental-research-csv", false);
    resetFundamentalResearch();
    resetCompanyProfile();
    const empty = byId("fundamental-research-empty");
    empty.textContent = error.message;
    empty.classList.remove("hidden");
  } finally {
    setFundamentalBusy(false);
  }
}

function localizedIssue(issue) {
  return ISSUE_TRANSLATIONS.get(issue) || issue;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = payload.error || {};
    const message = ERROR_MESSAGES[error.code] || error.message || `Error HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function setButtonBusy(button, busy, busyLabel, idleLabel) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.textContent = busy ? busyLabel : idleLabel;
}

function applyOverview(payload) {
  const operational = payload.operational;
  const workspace = operational.workspace;
  const latest = operational.latest_run;
  const scheduler = payload.scheduler;

  badge(
    byId("health-badge"),
    translated(operational.status, STATUS_LABELS, operational.status),
    statusTone(operational.status),
  );
  byId("workspace-status").textContent = translated(workspace.status, STATUS_LABELS, workspace.status);
  byId("workspace-counts").textContent = `${formatInteger(workspace.counts.observations)} observaciones · ${formatInteger(workspace.counts.metric_results)} métricas`;

  byId("run-status").textContent = latest
    ? translated(latest.status, STATUS_LABELS, latest.status)
    : "Sin registro operativo";
  byId("run-time").textContent = latest
    ? formatInstant(latest.completed_at || latest.started_at)
    : "Datos históricos disponibles";
  byId("traceability-status").textContent = latest?.traceability_verified
    ? "Verificada"
    : "Sin verificación reciente";
  byId("known-at-status").textContent = latest?.effective_known_at
    ? `Corte: ${formatInstant(latest.effective_known_at)}`
    : "—";

  if (scheduler.enabled) {
    const config = scheduler.config;
    byId("schedule-status").textContent = scheduler.due
      ? "Pendiente"
      : `Diaria · ${config.run_at}`;
    byId("schedule-next").textContent = `Próxima: ${formatInstant(scheduler.next_run_at, config.timezone)}`;
  } else {
    byId("schedule-status").textContent = "Desactivada";
    byId("schedule-next").textContent = "Solo actualización manual";
  }

  if (latest?.effective_known_at) byId("report-known-at").value = latest.effective_known_at;
  if (["quarterly", "annual"].includes(latest?.request?.fundamental_frequency)) {
    selectFundamentalFrequency(latest.request.fundamental_frequency);
  }
  operationalIssues = [...(operational.issues || []), ...(scheduler.issues || [])].map(localizedIssue);
  setMessage(operationalIssues.join(" · "), operationalIssues.length > 0);
}

async function refreshOverview() {
  const button = byId("refresh-overview");
  setButtonBusy(button, true, "Actualizando…", "Actualizar estado");
  try {
    applyOverview(await api("/api/overview"));
  } catch (error) {
    setMessage(error.message, true);
    badge(byId("health-badge"), "Sin conexión", "bad");
  } finally {
    setButtonBusy(button, false, "Actualizando…", "Actualizar estado");
  }
}

function appendMetadata(list, label, value) {
  const wrapper = createElement("div");
  wrapper.append(createElement("dt", "", label), createElement("dd", "", value ?? "—"));
  list.appendChild(wrapper);
}

function diagnosticSummary(mode, diagnostic, report) {
  const verdict = translated(diagnostic.verdict, VERDICT_LABELS, diagnostic.verdict).toLocaleLowerCase(LOCALE);
  if (mode === "market") {
    return `Las reglas deterministas describen una condición de mercado ${verdict} al ${formatCalendarDate(diagnostic.as_of)}. La lectura usa datos diarios IEX y no representa una recomendación.`;
  }
  const frequency = report.query?.fundamental_frequency === "annual" ? "anual" : "trimestral";
  return `Las reglas deterministas describen una condición fundamental ${verdict} para el período ${frequency} terminado el ${formatCalendarDate(diagnostic.as_of)}. La confianza refleja cobertura y vigencia, no probabilidad.`;
}

function renderUnavailable(target, title, section) {
  const heading = createElement("div", "diagnostic-heading");
  const titleGroup = createElement("div");
  titleGroup.append(createElement("p", "", "DIMENSIÓN INDEPENDIENTE"), createElement("h3", "", title));
  heading.append(titleGroup, createElement("span", "diagnostic-verdict insufficient_data", "No disponible"));
  const message = createElement(
    "p",
    "empty-state",
    "No existe un diagnóstico elegible para el corte y las fechas de referencia solicitadas. Ajusta los filtros o actualiza las fuentes.",
  );
  target.append(heading, message);
  target.dataset.status = section.status;
}

function renderDiagnostic(target, title, mode, section, report) {
  target.replaceChildren();
  if (section.status !== "available" || !section.diagnostic) {
    renderUnavailable(target, title, section);
    return;
  }

  const diagnostic = section.diagnostic;
  const heading = createElement("div", "diagnostic-heading");
  const titleGroup = createElement("div");
  titleGroup.append(
    createElement("p", "", mode === "market" ? "DIMENSIÓN DE MERCADO" : "DIMENSIÓN FUNDAMENTAL"),
    createElement("h3", "", title),
  );
  const verdict = createElement(
    "span",
    `diagnostic-verdict ${diagnostic.verdict}`,
    translated(diagnostic.verdict, VERDICT_LABELS, diagnostic.verdict),
  );
  heading.append(titleGroup, verdict);

  const body = createElement("div", "diagnostic-body");
  const scoreRow = createElement("div", "score-row");
  const scoreBlock = createElement("div", "score-block");
  scoreBlock.appendChild(createElement("span", "", "Puntuación independiente"));
  const scoreValue = createElement("p", "score-value");
  scoreValue.append(document.createTextNode(formatScore(diagnostic.final_score)));
  scoreValue.appendChild(createElement("small", "", " / 100"));
  scoreBlock.appendChild(scoreValue);
  const confidenceBlock = createElement("div", "confidence-block");
  confidenceBlock.append(
    createElement("span", "", "Cobertura de evidencia"),
    createElement("strong", "", formatConfidence(diagnostic.confidence)),
  );
  scoreRow.append(scoreBlock, confidenceBlock);

  const summary = createElement("p", "diagnostic-summary", diagnosticSummary(mode, diagnostic, report));

  const metadata = createElement("dl", "diagnostic-meta");
  appendMetadata(metadata, "Calidad", translated(diagnostic.quality, QUALITY_LABELS, diagnostic.quality));
  appendMetadata(metadata, "Referencia", formatCalendarDate(diagnostic.as_of));
  appendMetadata(
    metadata,
    "Evidencia publicada",
    formatAge(section.freshness?.availability_age_days),
  );

  const metricsHeading = createElement("div", "metrics-heading");
  metricsHeading.append(
    createElement("h4", "", "Métricas utilizadas"),
    createElement("span", "", `${formatInteger((section.metrics || []).length)} valores`),
  );
  const metrics = createElement("ul", "metric-list");
  for (const metric of section.metrics || []) {
    const presentation = METRIC_PRESENTATION[metric.metric_key];
    const item = createElement("li", "metric-item");
    const description = createElement("div");
    description.append(
      createElement("span", "metric-name", presentation?.label || metric.display_name),
      createElement("small", "metric-context", metricContext(metric)),
    );
    const value = createElement("strong", "metric-value", formatMetricValue(metric));
    value.title = `Valor exacto: ${metric.value} ${metric.unit}`;
    item.append(description, value);
    metrics.appendChild(item);
  }
  if (!metrics.childElementCount) {
    metrics.appendChild(createElement("li", "empty-state", "No se resolvieron métricas para este diagnóstico."));
  }

  body.append(scoreRow, summary, metadata, metricsHeading, metrics);
  target.append(heading, body);
  target.dataset.status = section.status;
}

function renderReport(report) {
  reportPayload = report;
  setExportAvailable("export-report-json", true);
  const reportArea = byId("report-area");
  reportArea.classList.remove("hidden");
  const tone = statusTone(report.status);
  badge(byId("report-status"), translated(report.status, STATUS_LABELS, report.status), tone);
  renderDiagnostic(byId("market-report"), "Mercado", "market", report.market, report);
  renderDiagnostic(byId("fundamental-report"), "Fundamentales", "fundamental", report.fundamental, report);
  renderFundamentalRatios(report.fundamental);

  const traceability = report.traceability;
  byId("report-traceability").textContent = traceability.verified
    ? `Trazabilidad verificada sobre ${formatInteger(traceability.diagnostics_examined)} diagnósticos y ${formatInteger(traceability.metric_results_examined)} resultados métricos examinados.`
    : "La trazabilidad del resultado no pudo verificarse.";

  const limitations = byId("report-limitations");
  limitations.replaceChildren();
  for (const text of report.limitations || []) {
    limitations.appendChild(createElement("li", "", LIMITATION_TRANSLATIONS.get(text) || text));
  }
  byId("report-json").textContent = JSON.stringify(report, null, 2);
}

byId("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("run-button");
  setButtonBusy(button, true, "Ejecutando…", "Ejecutar actualización");
  setMessage("La actualización puede tardar. SEC se consulta en cada ejecución.");
  const knownAt = byId("run-known-at").value.trim();
  const payload = {
    asset_id: "equity:us:aapl",
    market_start: byId("market-start").value,
    market_end: byId("market-end").value,
    fundamental_frequency: byId("run-frequency").value,
    refresh_mode: byId("refresh-mode").value,
    requested_known_at: knownAt || null,
    require_complete: byId("require-complete").checked,
  };
  try {
    const state = await api("/api/run", { method: "POST", body: JSON.stringify(payload) });
    const runStatus = translated(state.status, STATUS_LABELS, state.status);
    const refreshMode = translated(state.refresh_mode, STATUS_LABELS, state.refresh_mode);
    setMessage(`${runStatus}. Actualización de mercado: ${refreshMode}. Trazabilidad verificada.`);
    if (state.effective_known_at) byId("report-known-at").value = state.effective_known_at;
    await refreshOverview();
    await queryReport();
    await Promise.all([
      queryMarketChart(),
      queryFundamentalTrend(),
      queryFundamentalResearch(),
    ]);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setButtonBusy(button, false, "Ejecutando…", "Ejecutar actualización");
  }
});

async function queryReport() {
  const button = byId("report-button");
  const reportArea = byId("report-area");
  reportPayload = null;
  setExportAvailable("export-report-json", false);
  setButtonBusy(button, true, "Consultando…", "Consultar análisis");
  reportArea.setAttribute("aria-busy", "true");
  const parameters = new URLSearchParams({
    known_at: byId("report-known-at").value.trim(),
    fundamental_frequency: byId("report-frequency").value,
  });
  if (byId("market-as-of").value) parameters.set("market_as_of", byId("market-as-of").value);
  if (byId("fundamental-as-of").value) {
    parameters.set("fundamental_as_of", byId("fundamental-as-of").value);
  }
  try {
    renderReport(await api(`/api/report?${parameters.toString()}`));
    setMessage(operationalIssues.join(" · "), operationalIssues.length > 0);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    reportArea.setAttribute("aria-busy", "false");
    setButtonBusy(button, false, "Consultando…", "Consultar análisis");
  }
}

byId("report-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  selectFundamentalFrequency(byId("report-frequency").value);
  await queryReport();
  await Promise.all([
    queryMarketChart(),
    queryFundamentalTrend(),
    queryFundamentalResearch(),
  ]);
});

byId("refresh-overview").addEventListener("click", refreshOverview);
byId("export-market-csv").addEventListener("click", exportMarketCsv);
byId("export-fundamental-csv").addEventListener("click", exportFundamentalCsv);
byId("export-fundamental-research-csv").addEventListener(
  "click",
  exportFundamentalResearchCsv,
);
byId("export-report-json").addEventListener("click", exportReportJson);
byId("chart-data-disclosure").addEventListener("toggle", (event) => {
  if (event.currentTarget.open && marketChartPayload?.points) {
    renderChartTable(visibleMarketChartPoints());
  }
});
byId("chart-zoom-reset").addEventListener("click", resetMarketChartZoom);
byId("market-chart").addEventListener("wheel", handleMarketChartWheel, { passive: false });
byId("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  persistTheme(next);
});

for (const button of document.querySelectorAll(".series-toggle")) {
  button.addEventListener("click", () => {
    const series = button.dataset.series;
    chartSeriesVisibility[series] = !chartSeriesVisibility[series];
    button.setAttribute("aria-pressed", String(chartSeriesVisibility[series]));
    applySeriesVisibility();
  });
}

for (const button of document.querySelectorAll(".chart-type-button")) {
  button.addEventListener("click", () => {
    if (button.dataset.chartType === chartSettings.chartType) return;
    chartSettings = { ...chartSettings, chartType: button.dataset.chartType };
    applyChartSettings();
    persistChartSettings();
    if (marketChartPayload !== null) {
      renderMarketChart(marketChartPayload, { preserveViewport: true });
    }
  });
}

byId("chart-interval").addEventListener("change", async (event) => {
  chartSettings = { ...chartSettings, interval: event.target.value };
  applyChartSettings();
  persistChartSettings();
  await queryMarketChart();
});

byId("chart-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const candidate = normalizeChartSettings({
    shortWindow: byId("sma-short-window").valueAsNumber,
    longWindow: byId("sma-long-window").valueAsNumber,
    thirdWindow: byId("sma-third-window").valueAsNumber,
    shortColor: byId("sma-short-color").value,
    longColor: byId("sma-long-color").value,
    thirdColor: byId("sma-third-color").value,
    priceScale: byId("chart-price-scale").value,
    chartType: chartSettings.chartType,
    interval: chartSettings.interval,
  });
  const error = byId("chart-settings-error");
  if (candidate === null) {
    error.textContent =
      "Usa ventanas enteras y ordenadas: rápida 2–200, media 3–399 y lenta 4–400.";
    error.classList.remove("hidden");
    return;
  }
  error.classList.add("hidden");
  const requiresDataRefresh =
    candidate.shortWindow !== chartSettings.shortWindow ||
    candidate.longWindow !== chartSettings.longWindow ||
    candidate.thirdWindow !== chartSettings.thirdWindow ||
    candidate.interval !== chartSettings.interval;
  chartSettings = candidate;
  applyChartSettings();
  persistChartSettings();
  byId("chart-settings").open = false;
  if (requiresDataRefresh || marketChartPayload === null) await queryMarketChart();
  else renderMarketChart(marketChartPayload, { preserveViewport: true });
});

byId("chart-settings-reset").addEventListener("click", async () => {
  const requiresDataRefresh =
    chartSettings.shortWindow !== DEFAULT_CHART_SETTINGS.shortWindow ||
    chartSettings.longWindow !== DEFAULT_CHART_SETTINGS.longWindow ||
    chartSettings.thirdWindow !== DEFAULT_CHART_SETTINGS.thirdWindow ||
    chartSettings.interval !== DEFAULT_CHART_SETTINGS.interval;
  chartSettings = { ...DEFAULT_CHART_SETTINGS };
  byId("chart-settings-error").classList.add("hidden");
  applyChartSettings();
  persistChartSettings();
  byId("chart-settings").open = false;
  if (requiresDataRefresh || marketChartPayload === null) await queryMarketChart();
  else renderMarketChart(marketChartPayload, { preserveViewport: true });
});

for (const button of document.querySelectorAll(".frequency-button")) {
  button.addEventListener("click", async () => {
    selectFundamentalFrequency(button.dataset.frequency);
    await Promise.all([
      queryFundamentalTrend(),
      queryFundamentalResearch(),
      queryReport(),
    ]);
  });
}

for (const link of document.querySelectorAll(".nav-link")) {
  link.addEventListener("click", () => {
    for (const candidate of document.querySelectorAll(".nav-link")) {
      candidate.classList.toggle("active", candidate === link);
      if (candidate === link) candidate.setAttribute("aria-current", "page");
      else candidate.removeAttribute("aria-current");
    }
  });
}

const yesterday = new Date();
yesterday.setUTCDate(yesterday.getUTCDate() - 1);
byId("market-end").value = yesterday.toISOString().slice(0, 10);
byId("report-known-at").value = new Date().toISOString();

async function initialize() {
  initializeTheme();
  initializeChartSettings();
  await refreshOverview();
  await Promise.all([
    queryReport(),
    queryMarketChart(),
    queryFundamentalTrend(),
    queryFundamentalResearch(),
  ]);
}

initialize();
