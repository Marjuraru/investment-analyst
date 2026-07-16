"use strict";

const LOCALE = "es-PE";
const DEFAULT_TIME_ZONE = "America/Lima";

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

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
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
  if (["ready", "succeeded", "complete", "available"].includes(value)) return "good";
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

function numericValue(value) {
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
    byId("report-frequency").value = latest.request.fundamental_frequency;
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
  const reportArea = byId("report-area");
  reportArea.classList.remove("hidden");
  const tone = statusTone(report.status);
  badge(byId("report-status"), translated(report.status, STATUS_LABELS, report.status), tone);
  renderDiagnostic(byId("market-report"), "Mercado", "market", report.market, report);
  renderDiagnostic(byId("fundamental-report"), "Fundamentales", "fundamental", report.fundamental, report);

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
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setButtonBusy(button, false, "Ejecutando…", "Ejecutar actualización");
  }
});

async function queryReport() {
  const button = byId("report-button");
  const reportArea = byId("report-area");
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
  await queryReport();
});

byId("refresh-overview").addEventListener("click", refreshOverview);

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
  await refreshOverview();
  await queryReport();
}

initialize();
