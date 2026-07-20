"use strict";

const LOCALE = "es-PE";
const DEFAULT_TIME_ZONE = "America/Lima";
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const THEME_STORAGE_KEY = "investment-analyst-theme-v1";
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
const FUNDAMENTAL_CHART_WIDTH = 900;
const FUNDAMENTAL_CHART_HEIGHT = 330;
const FUNDAMENTAL_CHART_LAYOUT = Object.freeze({
  left: 66,
  right: 20,
  top: 24,
  bottom: 276,
});

const MARKET_PERIOD_LABELS = Object.freeze({
  "1m": "1 mes",
  "3m": "3 meses",
  "6m": "6 meses",
  "1y": "1 año",
  "2y": "2 años",
  "5y": "5 años",
  max: "Máx.",
});

const MARKET_RESOLUTION_PRESENTATION = Object.freeze({
  daily: Object.freeze({ singular: "sesión", plural: "sesiones", adjective: "diarios" }),
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
    label: "Retorno de la última sesión",
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
  "fundamental.research.gross_margin": Object.freeze({
    label: "Margen bruto",
    group: "Rentabilidad",
    kind: "percentage",
  }),
  "fundamental.research.operating_margin": Object.freeze({
    label: "Margen operativo",
    group: "Rentabilidad",
    kind: "percentage",
  }),
  "fundamental.research.net_margin": Object.freeze({
    label: "Margen neto",
    group: "Rentabilidad",
    kind: "percentage",
  }),
  "fundamental.research.operating_cash_flow_margin": Object.freeze({
    label: "Margen de flujo operativo",
    group: "Rentabilidad",
    kind: "percentage",
  }),
  "fundamental.research.free_cash_flow_margin": Object.freeze({
    label: "Margen de FCF",
    group: "Rentabilidad",
    kind: "percentage",
  }),
  "fundamental.research.operating_cash_flow_to_net_income": Object.freeze({
    label: "Flujo operativo / beneficio neto",
    group: "Calidad de beneficios",
    kind: "multiple",
  }),
  "fundamental.research.free_cash_flow_to_net_income": Object.freeze({
    label: "FCF / beneficio neto",
    group: "Calidad de beneficios",
    kind: "multiple",
  }),
  "fundamental.research.free_cash_flow": Object.freeze({
    label: "Flujo de caja libre",
    group: "Caja y reinversión",
    kind: "currency",
  }),
  "fundamental.research.capex_to_operating_cash_flow": Object.freeze({
    label: "Capex / flujo operativo",
    group: "Caja y reinversión",
    kind: "percentage",
  }),
  "fundamental.research.research_and_development_to_revenue": Object.freeze({
    label: "R&D / ingresos",
    group: "Caja y reinversión",
    kind: "percentage",
  }),
  "fundamental.research.selling_general_and_administrative_to_revenue": Object.freeze({
    label: "SG&A / ingresos",
    group: "Caja y reinversión",
    kind: "percentage",
  }),
  "fundamental.research.share_based_compensation_to_revenue": Object.freeze({
    label: "Stock-based compensation / ingresos",
    group: "Caja y reinversión",
    kind: "percentage",
  }),
  "fundamental.research.current_ratio": Object.freeze({
    label: "Current ratio",
    group: "Liquidez",
    kind: "multiple",
  }),
  "fundamental.research.cash_ratio": Object.freeze({
    label: "Cash ratio",
    group: "Liquidez",
    kind: "multiple",
  }),
  "fundamental.research.working_capital": Object.freeze({
    label: "Capital de trabajo",
    group: "Liquidez",
    kind: "currency",
  }),
  "fundamental.research.net_liquid_assets": Object.freeze({
    label: "Activos líquidos netos",
    group: "Liquidez",
    kind: "currency",
  }),
  "fundamental.research.shareholder_distributions": Object.freeze({
    label: "Dividendos + recompras",
    group: "Accionista",
    kind: "currency",
  }),
  "fundamental.research.shareholder_distributions_to_free_cash_flow": Object.freeze({
    label: "Distribuciones / FCF",
    group: "Accionista",
    kind: "percentage",
  }),
});

const FUNDAMENTAL_RESEARCH_GROUPS = Object.freeze([
  "Rentabilidad",
  "Calidad de beneficios",
  "Caja y reinversión",
  "Liquidez",
  "Accionista",
]);

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
let selectedChartPeriod = "6m";
let marketChartPayload = null;
let selectedChartPoint = -1;
let selectedFundamentalFrequency = "quarterly";
let fundamentalTrendPayload = null;
let fundamentalResearchPayload = null;
let fundamentalBusyCount = 0;
let reportPayload = null;
const chartSeriesVisibility = {
  "sma-5": true,
  "sma-20": true,
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

function marketCsvRows(chart) {
  return (chart.points || []).map((point) => [
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
    point.sma_5?.value,
    point.sma_5?.window,
    point.sma_5?.resolution,
    point.sma_5?.available_at,
    point.sma_5?.algorithm_version,
    point.sma_5?.input_observation_ids,
    point.sma_20?.value,
    point.sma_20?.window,
    point.sma_20?.resolution,
    point.sma_20?.available_at,
    point.sma_20?.algorithm_version,
    point.sma_20?.input_observation_ids,
    chart.traceability_verified,
  ]);
}

function exportMarketCsv() {
  if (!marketChartPayload?.points?.length) return;
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
    "sma_5_value",
    "sma_5_window",
    "sma_5_resolution",
    "sma_5_available_at",
    "sma_5_algorithm_version",
    "sma_5_input_observation_ids",
    "sma_20_value",
    "sma_20_window",
    "sma_20_resolution",
    "sma_20_available_at",
    "sma_20_algorithm_version",
    "sma_20_input_observation_ids",
    "traceability_verified",
  ];
  const filename = `aapl-mercado-${safeFilePart(marketChartPayload.period)}-${safeFilePart(marketChartPayload.known_at)}.csv`;
  downloadText(filename, csvDocument(columns, marketCsvRows(marketChartPayload)), "text/csv");
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
  const research = payload.research || payload;
  const histories = new Map(
    (payload.series || []).map((history) => [history.metric_key, history]),
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
  const research = fundamentalResearchPayload?.research || fundamentalResearchPayload;
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
  if (!point.period_start_timestamp || point.period_start_timestamp === point.timestamp) return end;
  return `${formatCalendarDate(point.period_start_timestamp)}–${end}`;
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
    const windowLabel = Number(parameters.window) === 1 ? "1 sesión" : `${parameters.window} sesiones`;
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

function chartValues(points) {
  return points.map((point) => {
    const close = numericValue(point.close);
    const volume = numericValue(point.volume);
    const sma5 = point.sma_5 ? numericValue(point.sma_5.value) : null;
    const sma20 = point.sma_20 ? numericValue(point.sma_20.value) : null;
    if (close === null || volume === null || (point.sma_5 && sma5 === null) || (point.sma_20 && sma20 === null)) {
      throw new Error("El histórico contiene un valor que no puede representarse en el gráfico.");
    }
    return { close, volume, sma5, sma20 };
  });
}

function addPriceGrid(svg, minimum, maximum, yPosition) {
  const grid = svgElement("g", { class: "chart-grid", "aria-hidden": "true" });
  for (let index = 0; index < 5; index += 1) {
    const ratio = index / 4;
    const value = maximum - (maximum - minimum) * ratio;
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
  const prices = values.flatMap((item) => [item.close, item.sma5, item.sma20]).filter(Number.isFinite);
  let minimum = Math.min(...prices);
  let maximum = Math.max(...prices);
  const span = maximum - minimum || Math.max(Math.abs(maximum) * 0.02, 1);
  minimum -= span * 0.08;
  maximum += span * 0.08;
  const priceHeight = CHART_LAYOUT.priceBottom - CHART_LAYOUT.top;
  const yPrice = (value) => CHART_LAYOUT.top + ((maximum - value) / (maximum - minimum)) * priceHeight;
  const maximumVolume = Math.max(...values.map((item) => item.volume), 1);
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
      `Líneas de cierre, SMA de 5 y 20 ${resolutionText.plural}, con barras de volumen.`,
    ),
  );
  addPriceGrid(svg, minimum, maximum, yPrice);

  const volumeGroup = svgElement("g", { class: "volume-bars", "aria-hidden": "true" });
  const plotWidth = CHART_WIDTH - CHART_LAYOUT.left - CHART_LAYOUT.right;
  const barWidth = Math.max(1.5, Math.min(12, (plotWidth / Math.max(points.length, 1)) * 0.68));
  values.forEach((item, index) => {
    const x = chartX(index, values.length) - barWidth / 2;
    const y = yVolume(item.volume);
    volumeGroup.appendChild(
      svgElement("rect", {
        x: x.toFixed(2),
        y: y.toFixed(2),
        width: barWidth.toFixed(2),
        height: Math.max(CHART_LAYOUT.bottom - y, 0.8).toFixed(2),
      }),
    );
  });
  svg.appendChild(volumeGroup);

  const series = [
    ["chart-line close-line", values.map((item) => item.close)],
    ["chart-line sma-five-line", values.map((item) => item.sma5)],
    ["chart-line sma-twenty-line", values.map((item) => item.sma20)],
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
  );
  addDateAxis(svg, points);

  const host = byId("market-chart");
  host.replaceChildren(svg);
  applySeriesVisibility();
  host.onpointermove = (event) => {
    const bounds = host.getBoundingClientRect();
    const logicalX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH;
    const plotRatio = (logicalX - CHART_LAYOUT.left) / plotWidth;
    const index = Math.round(plotRatio * Math.max(points.length - 1, 0));
    updateChartSelection(Math.max(0, Math.min(points.length - 1, index)), values, yPrice);
  };
  host.onkeydown = (event) => {
    let next = selectedChartPoint;
    if (event.key === "ArrowLeft") next -= 1;
    else if (event.key === "ArrowRight") next += 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = points.length - 1;
    else return;
    event.preventDefault();
    updateChartSelection(Math.max(0, Math.min(points.length - 1, next)), values, yPrice);
  };
  updateChartSelection(points.length - 1, values, yPrice);
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

function updateChartSelection(index, values, yPosition) {
  const points = marketChartPayload?.points || [];
  if (!points.length || !values[index]) return;
  selectedChartPoint = index;
  const point = points[index];
  const value = values[index];
  const x = chartX(index, points.length);
  const line = byId("chart-selection-line");
  line.setAttribute("x1", x.toFixed(2));
  line.setAttribute("x2", x.toFixed(2));
  setSelectionPoint("chart-selection-close", x, value.close, yPosition);
  setSelectionPoint("chart-selection-sma-5", x, value.sma5, yPosition);
  setSelectionPoint("chart-selection-sma-20", x, value.sma20, yPosition);
  byId("chart-point-date").textContent = formatMarketInterval(point);
  byId("chart-point-open").textContent = formatCurrency(point.open);
  byId("chart-point-high").textContent = formatCurrency(point.high);
  byId("chart-point-low").textContent = formatCurrency(point.low);
  byId("chart-point-close").textContent = formatCurrency(point.close);
  byId("chart-point-sma-5").textContent = point.sma_5 ? formatCurrency(point.sma_5.value) : "En calentamiento";
  byId("chart-point-sma-20").textContent = point.sma_20 ? formatCurrency(point.sma_20.value) : "En calentamiento";
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
      point.sma_5 ? formatCurrency(point.sma_5.value) : "—",
      point.sma_20 ? formatCurrency(point.sma_20.value) : "—",
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
  const sma5 = latestPoint.sma_5 ? numericValue(latestPoint.sma_5.value) : null;
  const sma20 = latestPoint.sma_20 ? numericValue(latestPoint.sma_20.value) : null;

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
    close !== null && sma5 ? close / sma5 - 1 : null,
    "Distancia mostrada a SMA 5",
  );
  setSignedPercentage(
    byId("snapshot-sma-20-distance"),
    close !== null && sma20 ? close / sma20 - 1 : null,
    "Distancia mostrada a SMA 20",
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
    "snapshot-range-return",
    "snapshot-range-cagr",
    "snapshot-range-drawdown",
    "snapshot-range-high",
    "snapshot-range-low",
    "chart-visible-sessions",
    "chart-latest-sma-5",
    "chart-latest-sma-20",
  ];
  for (const id of ids) byId(id).textContent = "—";
  byId("snapshot-quality").textContent = "—";
  byId("snapshot-quality").className = "quality-chip";
}

function renderMarketChart(chart) {
  marketChartPayload = chart;
  selectedChartPoint = -1;
  const points = chart.points || [];
  setExportAvailable("export-market-csv", points.length > 0);
  const empty = byId("chart-empty");
  if (!points.length) {
    resetMarketSnapshot();
    empty.textContent = "No hay sesiones de mercado disponibles para este corte histórico.";
    empty.classList.remove("hidden");
    byId("market-chart").replaceChildren();
    byId("chart-table-body").replaceChildren();
    byId("chart-status").textContent = `Corte: ${formatInstant(chart.known_at)} · sin sesiones disponibles.`;
    return;
  }
  empty.classList.add("hidden");
  const latestPoint = points[points.length - 1];
  const latest = chart.latest_session || latestPoint;
  const oneDayReturn = chartStatistic(chart, "market.history.simple_return_1d");
  const dailyChange = numericValue(oneDayReturn?.value);
  byId("chart-latest-close").textContent = formatCurrency(latest.close);
  byId("chart-latest-date").textContent = `Cierre del ${formatCalendarDate(latest.timestamp)}`;
  const change = byId("chart-range-change");
  change.textContent = `${formatRangeChange(dailyChange)} última sesión`;
  change.className = `chart-change ${dailyChange > 0 ? "positive" : dailyChange < 0 ? "negative" : "neutral"}`;
  byId("chart-latest-sma-5").textContent = latestPoint.sma_5
    ? formatCurrency(latestPoint.sma_5.value)
    : "—";
  byId("chart-latest-sma-20").textContent = latestPoint.sma_20
    ? formatCurrency(latestPoint.sma_20.value)
    : "—";
  byId("chart-visible-sessions").textContent = formatInteger(chart.coverage.selected_sessions);
  renderMarketSnapshot(chart, latest, latestPoint);
  const coverageStart = formatCalendarDate(chart.coverage.earliest_available_timestamp);
  const coverageEnd = formatCalendarDate(chart.coverage.latest_available_timestamp);
  const periodLabel = MARKET_PERIOD_LABELS[chart.period] || chart.period;
  const resolutionText = marketResolution(chart.resolution);
  byId("chart-point-period-label").textContent = resolutionText.singular;
  byId("chart-keyboard-hint").textContent = `← → para recorrer ${resolutionText.plural}`;
  byId("market-chart").setAttribute(
    "aria-label",
    `Gráfico histórico interactivo de AAPL con puntos ${resolutionText.adjective}. Usa las flechas izquierda y derecha para recorrerlos.`,
  );
  byId("chart-data-caption").textContent = `Puntos ${resolutionText.adjective} incluidos en el gráfico, ordenados cronológicamente`;
  byId("chart-status").textContent = `${periodLabel}: ${formatInteger(chart.coverage.selected_sessions)} sesiones fuente en ${formatInteger(chart.coverage.displayed_points)} puntos ${resolutionText.adjective} · ${formatInteger(chart.coverage.total_available_sessions)} sesiones locales · cobertura ${coverageStart}–${coverageEnd} · ${formatInteger(chart.coverage.discarded_revisions)} revisiones descartadas · corte ${formatInstant(chart.known_at)}.`;
  renderChartSvg(points, chart.resolution);
  const disclosure = byId("chart-data-disclosure");
  if (disclosure.open) renderChartTable(points);
  else byId("chart-table-body").replaceChildren();
}

function setChartBusy(busy) {
  byId("market-chart-card").setAttribute("aria-busy", String(busy));
  for (const button of document.querySelectorAll(".period-button")) button.disabled = busy;
}

async function queryMarketChart() {
  setChartBusy(true);
  setExportAvailable("export-market-csv", false);
  byId("chart-status").textContent = "Consultando el histórico local…";
  const parameters = new URLSearchParams({
    known_at: byId("report-known-at").value.trim(),
    period: selectedChartPeriod,
  });
  try {
    renderMarketChart(await api(`/api/market-chart?${parameters.toString()}`));
  } catch (error) {
    marketChartPayload = null;
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

function formatFundamentalResearchTrend(metric, history, frequency) {
  const statistics = history?.statistics;
  if (!statistics || statistics.point_count < 2) return "Sin comparación histórica";
  const presentation = FUNDAMENTAL_RESEARCH_PRESENTATION[metric.metric_key];
  if (
    metric.unit === "USD" &&
    frequency === "annual" &&
    statistics.compound_annual_growth_rate !== null &&
    statistics.compound_annual_growth_rate !== undefined
  ) {
    return `CAGR ${formatRangeChange(
      numericValue(statistics.compound_annual_growth_rate),
    )}`;
  }
  if (
    metric.unit === "USD" &&
    statistics.latest_change_rate_from_previous_available !== null &&
    statistics.latest_change_rate_from_previous_available !== undefined
  ) {
    return `Vs. período anterior ${formatRangeChange(
      numericValue(statistics.latest_change_rate_from_previous_available),
    )}`;
  }
  const delta = statistics.latest_change_from_previous_available;
  if (delta === null || delta === undefined) return "Sin comparación histórica";
  if (presentation?.kind === "percentage") {
    return `Vs. período anterior ${formatSignedNumber(numericValue(delta) * 100, {
      maximumFractionDigits: 1,
    })} pp`;
  }
  if (presentation?.kind === "multiple") {
    return `Vs. período anterior ${formatSignedNumber(delta, {
      maximumFractionDigits: 2,
    })}×`;
  }
  return `Cambio ${formatUsdBillions(delta)}`;
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
  const trend = createElement(
    "small",
    "fundamental-research-metric-change",
    metric ? formatFundamentalResearchTrend(metric, history, frequency) : "Sin datos",
  );
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
    const values = [
      ["Puntos", String(statistics.point_count)],
      ["Media exacta", `${statistics.arithmetic_mean} ${metric.unit}`],
      ["Rango exacto", `${statistics.range} ${metric.unit}`],
      [
        "Cambio anterior",
        statistics.latest_change_from_previous_available === null
          ? "No calculable"
          : `${statistics.latest_change_from_previous_available} ${metric.unit}`,
      ],
      [
        "CAGR",
        statistics.compound_annual_growth_rate === null
          ? "No calculable"
          : statistics.compound_annual_growth_rate,
      ],
    ];
    for (const [name, value] of values) {
      const row = document.createElement("div");
      row.append(
        createElement("dt", "", name),
        createElement("dd", "", value),
      );
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

function renderFundamentalResearch(payload) {
  fundamentalResearchPayload = payload;
  const research = payload.research || payload;
  const histories = new Map(
    (payload.series || []).map((history) => [history.metric_key, history]),
  );
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
  for (const groupName of FUNDAMENTAL_RESEARCH_GROUPS) {
    const group = createElement("section", "fundamental-research-group");
    group.appendChild(createElement("h4", "", groupName));
    const values = createElement("div", "fundamental-research-group-grid");
    values.setAttribute("role", "list");
    for (const [metricKey, presentation] of Object.entries(
      FUNDAMENTAL_RESEARCH_PRESENTATION,
    )) {
      if (presentation.group !== groupName) continue;
      values.appendChild(
        fundamentalResearchMetricCard(
          metricKey,
          metrics.get(metricKey),
          definitions.get(metricKey),
          histories.get(metricKey),
          research.request?.frequency,
        ),
      );
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
  coverage.textContent = `${formatInteger(metrics.size)}/${formatInteger(
    Object.keys(FUNDAMENTAL_RESEARCH_PRESENTATION).length,
  )} métricas`;
  coverage.className = `quality-chip ${
    metrics.size === Object.keys(FUNDAMENTAL_RESEARCH_PRESENTATION).length ? "good" : "warn"
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
  byId("fundamental-status").textContent = `${formatInteger(trend.coverage.periods_returned)} períodos · ${formatInteger(trend.coverage.observations_selected)} hechos seleccionados de ${formatInteger(trend.coverage.observations_examined)} observaciones examinadas · corte ${formatInstant(trend.known_at)}.`;
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
      await api(`/api/fundamental-research-history?${parameters.toString()}`),
    );
  } catch (error) {
    fundamentalResearchPayload = null;
    setExportAvailable("export-fundamental-research-csv", false);
    resetFundamentalResearch();
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
    renderChartTable(marketChartPayload.points);
  }
});
byId("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  persistTheme(next);
});

for (const button of document.querySelectorAll(".period-button")) {
  button.addEventListener("click", async () => {
    selectedChartPeriod = button.dataset.period;
    for (const candidate of document.querySelectorAll(".period-button")) {
      const active = candidate === button;
      candidate.classList.toggle("active", active);
      candidate.setAttribute("aria-pressed", String(active));
    }
    await queryMarketChart();
  });
}

for (const button of document.querySelectorAll(".series-toggle")) {
  button.addEventListener("click", () => {
    const series = button.dataset.series;
    chartSeriesVisibility[series] = !chartSeriesVisibility[series];
    button.setAttribute("aria-pressed", String(chartSeriesVisibility[series]));
    applySeriesVisibility();
  });
}

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
  await refreshOverview();
  await Promise.all([
    queryReport(),
    queryMarketChart(),
    queryFundamentalTrend(),
    queryFundamentalResearch(),
  ]);
}

initialize();
