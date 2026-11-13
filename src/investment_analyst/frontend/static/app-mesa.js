"use strict";

// UI-6: Mesa's "Novedades" layer, reglas analíticas family. Reuses
// /api/v1/candidate-notifications exactly as the revisar/sistema panels
// already do; this is a second, independent read into Mesa's own
// container, never the same DOM id as those boards.
let mesaAnalyticalNewsRequestSequence = 0;

function mesaContextualNavigationItem(className, accessibleLabel, request, content) {
  const item = createContextualNavigationButton(
    "",
    request,
    "alert-inbox-item mesa-contextual-item " + className,
  );
  item.setAttribute("aria-label", accessibleLabel);
  const copy = createElement("span", "mesa-contextual-copy");
  copy.append(createElement("strong", "mesa-contextual-title", content.title));
  if (content.assetLabel) {
    copy.append(createElement("span", "mesa-contextual-asset", content.assetLabel));
  }
  const meta = createElement("span", "mesa-contextual-meta");
  meta.append(
    createElement("time", "", content.timestamp),
    createElement("span", `alert-inbox-status ${content.statusClass}`, content.statusLabel),
  );
  item.append(copy, meta);
  return item;
}

function mesaNotificationTitle(notification, fallback) {
  return notification.payload?.rule_name_es
    || notification.payload?.operating_title
    || notification.title
    || fallback;
}

function mesaNotificationStatusLabel(status) {
  return MESA_CAZATIBURONES_STATUS_LABELS[status]
    || MESA_INCIDENT_STATUS_LABELS[status]
    || status
    || "Sin estado";
}

function renderMesaAnalyticalNews(payload) {
  const list = byId("mesa-news-analytical-list");
  const count = byId("mesa-news-analytical-count");
  const items = Array.isArray(payload.items) ? payload.items : [];
  const total = Number.isInteger(payload.total) ? payload.total : items.length;
  const pendingCount = Number.isInteger(payload.pending_count) ? payload.pending_count : 0;
  count.textContent = `Bandeja: ${formatInteger(total)} · ${formatInteger(pendingCount)} sin acuse`;
  list.replaceChildren();
  if (items.length === 0) {
    list.append(createElement("p", "", "Sin candidatos nuevos de reglas analíticas."));
    return;
  }
  for (const view of items.slice(0, 5)) {
    const notification = view.item;
    const title = mesaNotificationTitle(notification, "Candidato analítico");
    const assetLabel = assetDisplayLabel(notification.asset_id);
    const item = mesaContextualNavigationItem(
      "mesa-analytical-item",
      `Abrir ${title} · ${assetLabel}`,
      { board: "revisar", family: "candidate", id: notification.candidate_id },
      {
        title,
        assetLabel,
        statusClass: view.status,
        statusLabel: mesaNotificationStatusLabel(view.status),
        timestamp: formatInstant(notification.created_at),
      },
    );
    list.append(item);
  }
}

async function loadMesaAnalyticalNews() {
  const sequence = ++mesaAnalyticalNewsRequestSequence;
  const list = byId("mesa-news-analytical-list");
  list.setAttribute("aria-busy", "true");
  try {
    const payload = await api("/api/v1/candidate-notifications");
    if (sequence !== mesaAnalyticalNewsRequestSequence) return;
    renderMesaAnalyticalNews(payload);
  } catch (error) {
    if (sequence !== mesaAnalyticalNewsRequestSequence) return;
    list.replaceChildren(
      createElement("p", "", `No se pudieron consultar las novedades: ${error.message}`),
    );
  } finally {
    if (sequence === mesaAnalyticalNewsRequestSequence) list.setAttribute("aria-busy", "false");
  }
}

let mesaInstitutionalNewsRequestSequence = 0;
let mesaActivityNewsRequestSequence = 0;

const MESA_CAZATIBURONES_STATUS_LABELS = Object.freeze({
  pending: "Pendiente de acuse",
  acknowledged: "Acusada",
});

function renderMesaCazatiburonesNews(family, payload) {
  const list = byId(`mesa-news-${family}-list`);
  const count = byId(`mesa-news-${family}-count`);
  const items = Array.isArray(payload.items) ? payload.items : [];
  list.replaceChildren();
  if (!payload.enabled) {
    count.textContent = "Bandeja no configurada";
    list.append(renderAbsenceMark("blocked", "Bloqueada", "La outbox no está configurada en el servicio"));
    return;
  }
  const total = Number.isInteger(payload.total) ? payload.total : items.length;
  const pendingCount = Number.isInteger(payload.pending_count) ? payload.pending_count : 0;
  count.textContent = `Bandeja: ${formatInteger(total)} · ${formatInteger(pendingCount)} sin acuse`;
  if (total === 0) {
    list.append(createElement("p", "", "Sin novedades en esta bandeja."));
    return;
  }
  if (payload.truncated) {
    const returned = Number.isInteger(payload.returned) ? payload.returned : items.length;
    list.append(createElement("p", "", `Se muestran ${formatInteger(returned)} de ${formatInteger(total)} novedades.`));
  }
  for (const view of items.slice(0, 5)) {
    const notification = view.item;
    const title = mesaNotificationTitle(
      notification,
      family === "institutional" ? "Actividad institucional" : "Actividad declarada",
    );
    const assetLabel = assetDisplayLabel(notification.asset_id);
    const item = mesaContextualNavigationItem(
      "mesa-cazatiburones-" + family + "-item",
      `Abrir ${title} · ${assetLabel}`,
      { board: "cazatiburones", family, id: notification.asset_id },
      {
        title,
        assetLabel,
        statusClass: view.status,
        statusLabel: mesaNotificationStatusLabel(view.status),
        timestamp: formatInstant(notification.created_at),
      },
    );
    list.append(item);
  }
}

async function loadMesaCazatiburonesNews(family, request) {
  const sequence = family === "institutional"
    ? ++mesaInstitutionalNewsRequestSequence
    : ++mesaActivityNewsRequestSequence;
  const list = byId(`mesa-news-${family}-list`);
  list.setAttribute("aria-busy", "true");
  try {
    const payload = await request();
    if (sequence !== (family === "institutional" ? mesaInstitutionalNewsRequestSequence : mesaActivityNewsRequestSequence)) return;
    renderMesaCazatiburonesNews(family, payload);
  } catch (error) {
    if (sequence !== (family === "institutional" ? mesaInstitutionalNewsRequestSequence : mesaActivityNewsRequestSequence)) return;
    list.replaceChildren(
      createElement("p", "", `No se pudieron consultar las novedades: ${error.message}`),
    );
  } finally {
    if (sequence === (family === "institutional" ? mesaInstitutionalNewsRequestSequence : mesaActivityNewsRequestSequence)) {
      list.setAttribute("aria-busy", "false");
    }
  }
}

function loadMesaCazatiburonesNewsFamilies() {
  void loadMesaCazatiburonesNews("institutional", () =>
    api("/api/v1/cazatiburones/notifications?family=institutional&limit=5"),
  );
  void loadMesaCazatiburonesNews("activity", () =>
    api("/api/v1/cazatiburones/notifications?family=activity&limit=5"),
  );
}

// UI-6: Mesa's "Qué está roto" layer. A read-only, five-row projection of
// /api/alerts -- no acknowledge/transition actions here; those stay on the
// revisar board's own alert-inbox. Mesa is consulted, never written to,
// except for the relocated preferences panel.
let mesaIncidentsRequestSequence = 0;

const MESA_INCIDENT_STATUS_LABELS = Object.freeze({
  new: "Nueva",
  seen: "Vista",
  dismissed: "Descartada",
  resolved: "Resuelta",
  silenced: "Silenciada",
});

function renderMesaIncidents(payload) {
  const list = byId("mesa-incidents-list");
  const events = Array.isArray(payload.events) ? payload.events : [];
  list.replaceChildren();
  if (events.length === 0) {
    list.append(createElement("p", "", "No hay incidencias operativas registradas."));
    return;
  }
  for (const event of events.slice(0, 5)) {
    const title = event.title || "Incidencia operativa";
    const item = mesaContextualNavigationItem(
      "mesa-incident-item",
      "Abrir incidencia " + title,
      { board: "revisar", family: "alert", id: event.alert_id },
      {
        title,
        assetLabel: "",
        statusClass: event.status,
        statusLabel: MESA_INCIDENT_STATUS_LABELS[event.status] || event.status,
        timestamp: formatInstant(event.last_activated_at),
      },
    );
    list.append(item);
  }
}

async function loadMesaIncidents() {
  const sequence = ++mesaIncidentsRequestSequence;
  const list = byId("mesa-incidents-list");
  list.setAttribute("aria-busy", "true");
  try {
    const payload = await api("/api/alerts?limit=5");
    if (sequence !== mesaIncidentsRequestSequence) return;
    renderMesaIncidents(payload);
  } catch (error) {
    if (sequence !== mesaIncidentsRequestSequence) return;
    list.replaceChildren(
      createElement("p", "", `No se pudieron consultar las incidencias: ${error.message}`),
    );
  } finally {
    if (sequence === mesaIncidentsRequestSequence) list.setAttribute("aria-busy", "false");
  }
}

// UI-6: Mesa's "Universo" layer -- the first consumer of
// universe-coverage-v1. The query window is derived deterministically from
// the single global known_at cut: end is the last UTC day fully elapsed at
// the cut, start is 365 days before it -- the exact span this layer shows
// literally next to the matrix, never a second hidden constant. Freshness
// reuses that same 365-day span: `present` evidence within it renders "Al
// día", anything older (or of unknown age) renders "Vencida".
const MESA_COVERAGE_WINDOW_DAYS = 365;
const MESA_FUNDAMENTAL_COVERAGE_WINDOW_DAYS = 3660;
let mesaUniverseCoverageRequestSequence = 0;
let mesaUniverseCoveragePayload = null;
let mesaUniverseVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;

function mesaCoverageWindowFromKnownAt(knownAtIso) {
  const cut = new Date(knownAtIso);
  const endMs = Date.UTC(cut.getUTCFullYear(), cut.getUTCMonth(), cut.getUTCDate()) - 86_400_000;
  const startMs = endMs - MESA_COVERAGE_WINDOW_DAYS * 86_400_000;
  const fundamentalStartMs = endMs - MESA_FUNDAMENTAL_COVERAGE_WINDOW_DAYS * 86_400_000;
  const toDateString = (ms) => new Date(ms).toISOString().slice(0, 10);
  return {
    start: toDateString(startMs),
    end: toDateString(endMs),
    fundamentalStart: toDateString(fundamentalStartMs),
  };
}

function renderMesaUniverseWindow(coverageWindow) {
  byId("mesa-universe-window").textContent =
    `Ventana consultada: ${coverageWindow.start} a ${coverageWindow.end} · frecuencia anual`;
}

const MESA_MATRIX_STATE_LABELS = Object.freeze({
  fresh: "Al día",
  overdue: "Vencida",
  missing: "Sin evidencia",
  blocked: "Bloqueada",
  "not-applicable": "No aplica",
});

const MESA_VALUATION_REASON_LABELS = Object.freeze({
  share_basis_unavailable: "Base entre título y acción no disponible",
  security_unit_mismatch: "Unidad del título incompatible",
  corporate_action_basis_unavailable: "Base de acción corporativa no demostrable",
  price_unavailable: "Precio no disponible en el corte",
  price_ambiguous: "Revisión de precio ambigua",
  fundamentals_unavailable: "Ejercicio anual no disponible en el corte",
  fundamental_revision_ambiguous: "Revisión fundamental ambigua",
  period_mismatch: "Períodos incompatibles",
  source_mismatch: "Fuentes incompatibles",
  frequency_mismatch: "Frecuencias incompatibles",
  accounting_basis_mismatch: "Taxonomías contables incompatibles",
  currency_mismatch: "Monedas incompatibles; no se aplica FX",
  unit_mismatch: "Unidades financieras incompatibles",
  missing_input: "Falta un input oficial requerido",
  invalid_denominator: "Denominador nulo, negativo o inválido",
  ebitda_unavailable: "D&A anual oficial compatible no disponible",
});

function mesaCoverageExplanation(domainKey, coverage) {
  if (domainKey === "fundamentals" && coverage.capability === "not_implemented") {
    return "Frecuencia no implementada para este emisor";
  }
  if (domainKey === "corporate_valuation" && coverage.evidence === "missing") {
    const codes = Array.isArray(coverage.reason_codes) ? coverage.reason_codes : [];
    const reasons = codes
      .map((code) => MESA_VALUATION_REASON_LABELS[code])
      .filter((label) => typeof label === "string");
    if (reasons.length) return reasons.join(" · ");
  }
  return "";
}

function applyMesaCoverageExplanation(container, explanation) {
  if (!explanation) return;
  const mark = container.querySelector(".mesa-matrix-state");
  if (!mark) return;
  const base = mark.getAttribute("aria-label") || "";
  const label = base ? `${base} · ${explanation}` : explanation;
  mark.setAttribute("aria-label", label);
  mark.setAttribute("title", label);
  const hidden = mark.querySelector(".visually-hidden");
  if (hidden) hidden.textContent = label;
}

const MESA_ASSET_CLASS_LABELS = Object.freeze({
  equity: "Acción",
  etf: "ETF",
  crypto: "Cripto",
});

function mesaMatrixStateMarkup(state) {
  const label = MESA_MATRIX_STATE_LABELS[state];
  return `<span class="mesa-matrix-state mesa-matrix-state-${state}" role="img" aria-label="${label}" title="${label}"><span class="mesa-matrix-mark mesa-matrix-${state}" aria-hidden="true"></span><span class="visually-hidden">${label}</span></span>`;
}

function mesaUniverseCellMarkup(capability, evidence, ageDays) {
  if (capability === "not_applicable") {
    return mesaMatrixStateMarkup("not-applicable");
  }
  if (capability === "not_configured" || capability === "not_implemented") {
    return mesaMatrixStateMarkup("blocked");
  }
  if (evidence === "missing" || evidence === "not_queried") {
    return mesaMatrixStateMarkup("missing");
  }
  if (typeof ageDays === "number" && ageDays <= MESA_COVERAGE_WINDOW_DAYS) {
    return mesaMatrixStateMarkup("fresh");
  }
  return mesaMatrixStateMarkup("overdue");
}

const MESA_COVERAGE_CAPABILITY_KEYS = Object.freeze([
  "market",
  "fundamentals",
  "corporate_valuation",
]);

function mesaAssetDomainLabel(assetClass) {
  const label = MESA_ASSET_CLASS_LABELS[assetClass];
  if (!label) throw new Error(`Clase de activo no representable: ${assetClass}`);
  return label;
}

function mesaLatestEvidenceTimestamp(asset) {
  const timestamps = MESA_COVERAGE_CAPABILITY_KEYS.flatMap((key) => {
    const coverage = asset[key];
    return [coverage.reference_at, coverage.latest_input_available_at];
  }).filter((value) => typeof value === "string" && !Number.isNaN(new Date(value).valueOf()));
  return timestamps.sort((left, right) => new Date(right).valueOf() - new Date(left).valueOf())[0] ?? null;
}

function mesaLatestEvidenceMarkup(asset) {
  const timestamp = mesaLatestEvidenceTimestamp(asset);
  if (!timestamp) {
    return renderAbsenceMark("missing", "Sin evidencia", "Sin referencia temporal en los dominios consultados").outerHTML;
  }
  return `<time datetime="${timestamp}">${formatInstant(timestamp)}</time>`;
}

const MESA_BVL_SUMMARY_KEYS = Object.freeze([
  "applicable",
  "present",
  "missing",
  "not-queried",
  "not-configured",
  "not-implemented",
  "not-applicable",
]);

function resetMesaBvlRegistrySummary() {
  for (const key of MESA_BVL_SUMMARY_KEYS) byId(`mesa-bvl-${key}`).textContent = "—";
}

function renderMesaBvlRegistrySummary(payload) {
  const counts = Object.fromEntries(MESA_BVL_SUMMARY_KEYS.map((key) => [key, 0]));
  for (const asset of payload.assets || []) {
    const coverage = asset.bvl_registry;
    if (coverage.capability === "not_applicable") {
      counts["not-applicable"] += 1;
      continue;
    }
    counts.applicable += 1;
    if (coverage.capability === "not_configured") {
      counts["not-configured"] += 1;
      continue;
    }
    if (coverage.capability === "not_implemented") {
      counts["not-implemented"] += 1;
      continue;
    }
    if (coverage.evidence === "present") counts.present += 1;
    if (coverage.evidence === "missing") counts.missing += 1;
    if (coverage.evidence === "not_queried") counts["not-queried"] += 1;
  }
  for (const key of MESA_BVL_SUMMARY_KEYS) {
    byId(`mesa-bvl-${key}`).textContent = formatInteger(counts[key]);
  }
  byId("mesa-bvl-applicable").textContent = `${formatInteger(counts.applicable)} aplicables`;
}

function renderMesaUniverseMatrix(payload) {
  const body = byId("mesa-universe-table-body");
  mesaUniverseCoveragePayload = payload;
  body.replaceChildren();
  const assets = Array.isArray(payload.assets) ? payload.assets : [];
  const visibleCount = progressiveCollectionVisibleCount(
    assets.length,
    mesaUniverseVisibleCount,
  );
  mesaUniverseVisibleCount = visibleCount;
  for (const asset of assets.slice(0, visibleCount)) {
    const assetLabel = assetDisplayLabel(asset.asset_id);
    const row = document.createElement("tr");
    const assetCell = document.createElement("th");
    assetCell.scope = "row";
    const assetButton = createContextualNavigationButton(
      "",
      { board: "activo", assetId: asset.asset_id, subtab: "mercado" },
      "mesa-universe-asset-button",
    );
    assetButton.setAttribute(
      "aria-label",
      "Abrir activo " + assetLabel,
    );
    assetButton.append(
      createElement("strong", "", assetLabel),
    );
    assetCell.append(assetButton);
    row.append(assetCell);
    row.append(createElement("td", "", mesaAssetDomainLabel(asset.asset_class)));
    const domainSubtabs = {
      market: ["mercado", "Mercado"],
      fundamentals: ["fundamentales", "Fundamentales"],
      corporate_valuation: ["valoracion", "Valoración"],
    };
    for (const key of MESA_COVERAGE_CAPABILITY_KEYS) {
      const coverage = asset[key] || {};
      const [subtab, label] = domainSubtabs[key];
      const domainCell = document.createElement("td");
      const domainButton = createContextualNavigationButton(
        "",
        { board: "activo", assetId: asset.asset_id, subtab },
        "mesa-universe-domain-button",
      );
      domainButton.setAttribute(
        "aria-label",
        "Abrir " + label + " para " + assetLabel,
      );
      domainButton.innerHTML = mesaUniverseCellMarkup(
        coverage.capability,
        coverage.evidence,
        coverage.reference_age_days ?? coverage.latest_input_age_days ?? null,
      );
      applyMesaCoverageExplanation(
        domainButton,
        mesaCoverageExplanation(key, coverage),
      );
      domainCell.append(domainButton);
      row.append(domainCell);
    }
    const evidenceCell = document.createElement("td");
    evidenceCell.innerHTML = mesaLatestEvidenceMarkup(asset);
    row.append(evidenceCell);
    body.append(row);
  }
  if (!body.childElementCount) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.append(renderAbsenceMark("missing", "Sin evidencia", "Sin activos en el catálogo devuelto"));
    row.append(cell);
    body.append(row);
  }
  renderProgressiveCollectionControls(
    byId("mesa-universe-collection-status"),
    byId("mesa-universe-collection-controls"),
    assets.length,
    visibleCount,
    {
      label: "activos",
      onMore: () => {
        mesaUniverseVisibleCount += PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderMesaUniverseMatrix(mesaUniverseCoveragePayload);
      },
      onLess: () => {
        mesaUniverseVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderMesaUniverseMatrix(mesaUniverseCoveragePayload);
      },
    },
  );
  renderMesaBvlRegistrySummary(payload);
}

function renderMesaUniverseAbsentTable(mark) {
  mesaUniverseCoveragePayload = null;
  const body = byId("mesa-universe-table-body");
  body.replaceChildren();
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.append(mark);
  row.append(cell);
  body.append(row);
  renderProgressiveCollectionControls(
    byId("mesa-universe-collection-status"),
    byId("mesa-universe-collection-controls"),
    0,
    0,
  );
}

async function loadMesaUniverseCoverage() {
  const sequence = ++mesaUniverseCoverageRequestSequence;
  mesaUniverseCoveragePayload = null;
  mesaUniverseVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  const knownAt = byId("report-known-at").value.trim();
  if (!knownAt) {
    renderMesaUniverseWindow({ start: "—", end: "—" });
    resetMesaBvlRegistrySummary();
    renderMesaUniverseAbsentTable(
      renderAbsenceMark("missing", "Sin evidencia", "Sin corte known_at establecido"),
    );
    return;
  }
  const coverageWindow = mesaCoverageWindowFromKnownAt(knownAt);
  renderMesaUniverseWindow(coverageWindow);
  const parameters = new URLSearchParams({
    known_at: knownAt,
    market_start: coverageWindow.start,
    market_end: coverageWindow.end,
    fundamental_start: coverageWindow.fundamentalStart,
    fundamental_end: coverageWindow.end,
    frequency: "annual",
  });
  const body = byId("mesa-universe-table-body");
  body.setAttribute("aria-busy", "true");
  try {
    const payload = await api(`/api/v1/universe-coverage?${parameters.toString()}`);
    if (
      sequence !== mesaUniverseCoverageRequestSequence
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    renderMesaUniverseMatrix(payload);
  } catch (error) {
    if (
      sequence !== mesaUniverseCoverageRequestSequence
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    resetMesaBvlRegistrySummary();
    renderMesaUniverseAbsentTable(createElement("span", "", error.message));
    throw error;
  } finally {
    if (sequence === mesaUniverseCoverageRequestSequence) body.setAttribute("aria-busy", "false");
  }
}

const MESA_INVESTOR_BATCH_SIZE = 10;
const MESA_INVESTOR_MAX_UNIVERSE = 100;
const mesaInvestorUiState = {
  search: "",
  assetClass: "all",
  watchlist: "all",
  verdict: "all",
  sortBy: "name",
  sortDir: "asc",
  visibleLimit: MESA_INVESTOR_BATCH_SIZE,
  expandedAssetIds: new Set(),
  loadingAll: false,
  cancelRequested: false,
  requestSequence: 0,
  controlsBound: false,
  abortController: null,
  batchStatus: "idle",
  batchError: null,
  pausedReason: null,
  hasQueriedCut: "",
  lastRequestedScope: "visible",
  loadingAssetIds: new Set(),
  failedAssetIds: new Map(),
  cancelledAssetIds: new Set(),
  pausedAssetIds: new Set(),
};

function abortMesaInvestorRequests() {
  mesaInvestorUiState.requestSequence += 1;
  if (mesaInvestorUiState.abortController) {
    try {
      mesaInvestorUiState.abortController.abort();
    } catch {}
    mesaInvestorUiState.abortController = null;
  }
  mesaInvestorUiState.loadingAll = false;
  mesaInvestorUiState.cancelRequested = false;
  mesaInvestorUiState.loadingAssetIds.clear();
}

const MESA_VERDICT_LABELS = Object.freeze({
  favorable: "Favorable",
  neutral: "Neutral",
  unfavorable: "Desfavorable",
});

const MESA_VALUE_STATE_LABELS = Object.freeze({
  available: "Disponible",
  missing: "Sin datos",
  not_applicable: "No aplica",
  not_configured: "Sin config.",
  not_supported: "No soportado",
  insufficient_data: "Insuficiente",
});

function sanitizeMesaErrorMessage(error) {
  if (!error) return "Servicio local temporalmente no disponible.";
  const status = Number(error.status);
  const code = String(error.code || "").trim();
  if (status === 503 || code === "operational_error") {
    return "HTTP 503 · Servicio o espacio de datos temporalmente no disponible.";
  }
  if (error.name === "TypeError") {
    return "Sin conexión con la interfaz local.";
  }
  let raw = String(error.message || "").replace(/[\r\n]+/g, " ").trim();
  raw = raw.replace(/(?:\/[A-Za-z0-9._-]+){2,}/g, "[ruta]").replace(/[A-Za-z]:\\[^\s]+/g, "[ruta]");
  if (raw.length > 120) raw = `${raw.slice(0, 117)}…`;
  if (Number.isFinite(status) && status >= 400 && !raw.includes(`HTTP ${status}`)) {
    return raw ? `HTTP ${status} · ${raw}` : `HTTP ${status}`;
  }
  return raw || "No se pudo completar la consulta local.";
}

function mesaCatalogEntries() {
  return Object.values(marketAssets || {}).sort((a, b) => {
    const byName = String(a.name || a.symbol || "").localeCompare(String(b.name || b.symbol || ""), LOCALE);
    if (byName !== 0) return byName;
    return String(a.assetId || "").localeCompare(String(b.assetId || ""));
  });
}

function mesaFilteredCatalogEntries() {
  const query = mesaInvestorUiState.search.trim().toLowerCase();
  const knownAt = byId("report-known-at")?.value.trim() || "";
  return mesaCatalogEntries().filter((entry) => {
    const assetId = typeof entry.assetId === "string" ? entry.assetId : "";
    if (mesaInvestorUiState.assetClass !== "all" && entry.assetClass !== mesaInvestorUiState.assetClass) {
      return false;
    }
    if (mesaInvestorUiState.watchlist !== "all") {
      const prefs = typeof assetPreferenceFlags === "function"
        ? assetPreferenceFlags(assetId)
        : { inWatchlist: false, isFavorite: false };
      if (mesaInvestorUiState.watchlist === "watchlist" && !prefs.inWatchlist) return false;
      if (mesaInvestorUiState.watchlist === "favorite" && !prefs.isFavorite) return false;
    }
    if (query) {
      const hay = `${entry.symbol} ${entry.name} ${assetId}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    if (mesaInvestorUiState.verdict !== "all") {
      const ov = assetId ? getCachedInvestorOverviewAsset(assetId, knownAt) : null;
      const verdict = ov?.fundamental?.verdict || null;
      if (mesaInvestorUiState.verdict === "applicable") {
        if (!verdict) return false;
      } else if (verdict !== mesaInvestorUiState.verdict) {
        return false;
      }
    }
    return true;
  });
}

function mesaMetricRawDecimal(ov, key) {
  if (!ov) return null;
  if (key === "close") return ov.market?.close?.state === "available" ? ov.market.close.value : null;
  if (key === "change_1d") {
    return ov.market?.change_1d?.state === "available" ? ov.market.change_1d.value : null;
  }
  if (key === "score") {
    return ov.fundamental?.score?.state === "available" ? ov.fundamental.score.value : null;
  }
  if (key === "pe") {
    return ov.valuation?.price_to_earnings?.state === "available"
      ? ov.valuation.price_to_earnings.value
      : null;
  }
  return null;
}

function mesaSortedEntries(entries) {
  const knownAt = byId("report-known-at")?.value.trim() || "";
  const { sortBy, sortDir } = mesaInvestorUiState;
  const dir = sortDir === "desc" ? -1 : 1;
  return [...entries].sort((a, b) => {
    const idA = String(a.assetId || "");
    const idB = String(b.assetId || "");
    if (sortBy === "name") {
      const cmp = String(a.symbol || a.name).localeCompare(String(b.symbol || b.name), LOCALE) * dir;
      if (cmp !== 0) return cmp;
      return idA.localeCompare(idB);
    }
    const ovA = idA ? getCachedInvestorOverviewAsset(idA, knownAt) : null;
    const ovB = idB ? getCachedInvestorOverviewAsset(idB, knownAt) : null;
    const valA = mesaMetricRawDecimal(ovA, sortBy);
    const valB = mesaMetricRawDecimal(ovB, sortBy);
    if (valA === null && valB === null) {
      return idA.localeCompare(idB);
    }
    if (valA === null) return 1;
    if (valB === null) return -1;
    const cmp = compareDecimalStrings(valA, valB) * dir;
    if (cmp !== 0) return cmp;
    return idA.localeCompare(idB);
  });
}

function mesaOverviewRowPriceDate(ov) {
  if (ov?.market?.close?.state !== "available" || !ov.market.close.reference_at) return null;
  const text = String(ov.market.close.reference_at).trim();
  return text.length >= 10 ? text.slice(0, 10) : null;
}

function mesaBriefStateLabel(val) {
  if (!val) return "Sin datos";
  return MESA_VALUE_STATE_LABELS[val.state] || "Sin datos";
}

function mesaFullAbsenceDescription(val) {
  if (!val || val.state === "available") return "";
  const base = mesaBriefStateLabel(val);
  const rawReason = Array.isArray(val.reason_codes) && val.reason_codes.length ? val.reason_codes[0] : "";
  if (!rawReason) return base;
  const detail = typeof formatOverviewAbsenceDetail === "function"
    ? formatOverviewAbsenceDetail(rawReason)
    : { label: translateOverviewAbsenceReason(rawReason), technicalCode: rawReason };
  return detail.technicalCode ? `${base}: ${detail.label} (${detail.technicalCode})` : `${base}: ${detail.label}`;
}

function mesaPendingStatusPill(rowState, rowError = "") {
  if (rowState === "loading") {
    return createElement("span", "mesa-status-pill is-warn", "Consultando…");
  }
  if (rowState === "error") {
    const pill = createElement("span", "mesa-status-pill is-error", "Error");
    if (rowError) {
      pill.title = rowError;
      pill.setAttribute("aria-label", `Error al consultar: ${rowError}`);
    }
    return pill;
  }
  if (rowState === "paused") {
    const pill = createElement("span", "mesa-status-pill is-paused", "Carga pausada");
    if (rowError) {
      pill.title = rowError;
      pill.setAttribute("aria-label", `Carga pausada: ${rowError}`);
    }
    return pill;
  }
  if (rowState === "cancelled") {
    return createElement("span", "mesa-status-pill", "Cancelado");
  }
  return createElement("span", "mesa-status-pill", "Pendiente");
}

function renderInvestorValueCell(val, options = {}) {
  const wrap = createElement("div", "mesa-value-cell");
  if (!val) {
    wrap.append(mesaPendingStatusPill(options.rowState || "pending", options.rowError || ""));
    return wrap;
  }
  if (val.state === "available" && val.value !== null) {
    const formatted = typeof options.formatValue === "function"
      ? options.formatValue(val.value)
      : options.percent
        ? formatDecimalPercentFromRatio(val.value, { decimals: 2, signed: true })
        : `${val.value}${options.unitSuffix || ""}`;
    let toneClass = "";
    if (options.signedTone) {
      const parsed = typeof parseDecimalValueForDisplay === "function"
        ? parseDecimalValueForDisplay(val.value)
        : null;
      if (parsed && parsed.sign > 0) toneClass = " is-positive";
      else if (parsed && parsed.sign < 0) toneClass = " is-negative";
    }
    const primary = createElement("span", `mesa-value-primary${toneClass}`, formatted);
    primary.title = typeof formatExactDecimalText === "function"
      ? `Exacto: ${formatExactDecimalText(val.value, options.unitSuffix?.trim() || "")}`
      : String(val.value);
    wrap.append(primary);
    if (options.isDifferentPriceDate && options.priceDate) {
      const badgeEl = createElement("span", "mesa-date-badge", "Otra fecha");
      badgeEl.title = `Fecha de cierre: ${options.priceDate}`;
      badgeEl.setAttribute("aria-label", `Fecha de cierre distinta: ${options.priceDate}`);
      wrap.append(badgeEl);
    }
    return wrap;
  }
  const brief = mesaBriefStateLabel(val);
  const full = mesaFullAbsenceDescription(val);
  const pill = createElement("span", `mesa-status-pill state-${val.state}`, brief);
  if (full) {
    pill.title = full;
    pill.setAttribute("aria-label", full);
  }
  wrap.append(pill);
  return wrap;
}

function ensureMesaRetryButton() {
  return byId("mesa-retry-load-btn");
}

function formatMesaExactDetailValue(val, kind, currency = "USD") {
  if (!val) return "Pendiente de consulta";
  if (val.state === "available" && val.value !== null) {
    let display = String(val.value);
    if (kind === "price") display = formatDecimalPrice(val.value, currency);
    else if (kind === "percent") display = formatDecimalPercentFromRatio(val.value, { decimals: 2, signed: true });
    else if (kind === "multiple") display = formatDecimalMultiple(val.value, 2);
    else if (kind === "ratio") display = formatDecimalRatio(val.value, 2);
    else if (kind === "score") display = `${formatDecimalScore(val.value, 1)}/100`;
    const exact = formatExactDecimalText(val.value, kind === "price" ? currency : "");
    return exact && exact !== display ? `${display} (exacto: ${exact})` : display;
  }
  return mesaFullAbsenceDescription(val) || mesaBriefStateLabel(val);
}

function renderMesaInvestorTable() {
  const body = byId("mesa-investor-table-body");
  if (!body) return;
  const knownAt = byId("report-known-at")?.value.trim() || "";
  const knownAtShort = typeof formatKnownAtLabel === "function" ? formatKnownAtLabel(knownAt) : knownAt;
  const filtered = mesaSortedEntries(mesaFilteredCatalogEntries());
  const visible = filtered.slice(0, mesaInvestorUiState.visibleLimit);
  const loadedCount = filtered.filter(
    (e) => Boolean(e.assetId && getCachedInvestorOverviewAsset(e.assetId, knownAt)),
  ).length;
  const unqueriedCount = Math.max(0, filtered.length - loadedCount);

  const visiblePriceDates = new Set();
  let latestPriceDate = null;
  for (const entry of visible) {
    const ov = entry.assetId ? getCachedInvestorOverviewAsset(entry.assetId, knownAt) : null;
    const refDate = mesaOverviewRowPriceDate(ov);
    if (refDate) {
      visiblePriceDates.add(refDate);
      if (!latestPriceDate || refDate > latestPriceDate) latestPriceDate = refDate;
    }
  }

  const isBusy = mesaInvestorUiState.loadingAll || mesaInvestorUiState.loadingAssetIds.size > 0;
  const hasPausedOrFailed = Boolean(
    mesaInvestorUiState.failedAssetIds.size > 0
    || mesaInvestorUiState.pausedAssetIds.size > 0
    || (mesaInvestorUiState.batchStatus === "error" && mesaInvestorUiState.batchError),
  );

  const statusEl = byId("mesa-investor-status");
  if (statusEl) {
    if (isBusy) {
      statusEl.textContent = `Consultando lote… Cargados ${loadedCount}/${filtered.length} · Mostrando ${visible.length}`;
    } else if (hasPausedOrFailed) {
      statusEl.textContent = `Consulta incompleta · ${loadedCount} cargados, ${unqueriedCount} sin consultar`;
    } else if (mesaInvestorUiState.batchStatus === "cancelled") {
      statusEl.textContent = `Carga cancelada · Cargados ${loadedCount}/${filtered.length}`;
    } else {
      statusEl.textContent = `Mostrando ${visible.length} de ${filtered.length} · Cargados ${loadedCount}`;
    }
  }

  const bannerEl = byId("mesa-investor-banner");
  const bannerTextEl = byId("mesa-investor-banner-text");
  const bannerDetailEl = byId("mesa-investor-banner-detail");
  const bannerRetryBtn = byId("mesa-banner-retry-btn");
  const bannerSystemBtn = byId("mesa-banner-system-btn");
  if (bannerEl) {
    const showBanner = !isBusy && hasPausedOrFailed;
    bannerEl.classList.toggle("hidden", !showBanner);
    bannerEl.classList.toggle("is-error", mesaInvestorUiState.batchStatus === "error");
    if (showBanner) {
      if (bannerTextEl) {
        bannerTextEl.textContent = `No se pudo completar la consulta local. ${loadedCount} cargados, ${unqueriedCount} sin consultar`;
      }
      const detail = mesaInvestorUiState.batchError || mesaInvestorUiState.pausedReason || "";
      if (bannerDetailEl) {
        bannerDetailEl.textContent = detail;
        bannerDetailEl.classList.toggle("hidden", !detail);
      }
      if (bannerRetryBtn) bannerRetryBtn.classList.remove("hidden");
      if (bannerSystemBtn) bannerSystemBtn.classList.remove("hidden");
    }
  }

  const winEl = byId("mesa-investor-window");
  if (winEl) {
    const hasOverviewWindow = Boolean(
      investorOverviewState.windows
      && sameIsoTimestamp(investorOverviewState.knownAt, knownAt),
    );
    const priceDateSummary = visiblePriceDates.size > 1
      ? `Fechas distintas · últ. cierre ${latestPriceDate}`
      : latestPriceDate
        ? `Últ. cierre ${latestPriceDate}`
        : "";
    if (hasOverviewWindow) {
      const w = investorOverviewState.windows;
      winEl.textContent = `Corte efectivo: ${knownAtShort} (America/Lima)${priceDateSummary ? ` · ${priceDateSummary}` : ""}`;
      winEl.title = `Corte UTC: ${knownAt} · Ventana mercado ${w.market_start}..${w.market_end} · fundamental anual ${w.fundamental_start}..${w.fundamental_end}`;
      winEl.setAttribute("aria-label", `${winEl.textContent}. ${winEl.title}`);
    } else {
      winEl.textContent = knownAt
        ? `Corte efectivo: ${knownAtShort} (America/Lima)`
        : "Sin corte known_at definido";
      winEl.title = knownAt ? `Corte UTC: ${knownAt}` : "";
    }
  }

  const showMoreBtn = byId("mesa-show-more-btn");
  if (showMoreBtn) {
    showMoreBtn.classList.toggle("hidden", visible.length >= filtered.length);
  }
  const loadAllBtn = byId("mesa-load-all-btn");
  const cancelBtn = byId("mesa-cancel-load-btn");
  const retryBtn = ensureMesaRetryButton();
  if (loadAllBtn) {
    loadAllBtn.disabled = isBusy || loadedCount >= Math.min(filtered.length, MESA_INVESTOR_MAX_UNIVERSE);
  }
  if (cancelBtn) {
    cancelBtn.classList.toggle("hidden", !isBusy);
  }
  if (retryBtn) {
    const hasRecoverable = Boolean(
      !hasPausedOrFailed
      && (mesaInvestorUiState.batchError
        || mesaInvestorUiState.failedAssetIds.size > 0
        || mesaInvestorUiState.pausedAssetIds.size > 0
        || mesaInvestorUiState.cancelledAssetIds.size > 0),
    );
    retryBtn.classList.toggle("hidden", isBusy || !hasRecoverable);
  }

  body.replaceChildren();
  if (visible.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "Ningún activo coincide con los filtros actuales.";
    tr.append(td);
    body.append(tr);
    return;
  }

  for (const entry of visible) {
    const assetId = typeof entry.assetId === "string" ? entry.assetId : "";
    const ov = assetId ? getCachedInvestorOverviewAsset(assetId, knownAt) : null;
    const rowError = assetId
      ? (mesaInvestorUiState.failedAssetIds.get(assetId) || mesaInvestorUiState.pausedReason || "")
      : "Identificador de activo inválido.";
    const rowState = !assetId || mesaInvestorUiState.failedAssetIds.has(assetId)
      ? "error"
      : mesaInvestorUiState.loadingAssetIds.has(assetId)
        ? "loading"
        : mesaInvestorUiState.pausedAssetIds.has(assetId)
          ? "paused"
          : mesaInvestorUiState.cancelledAssetIds.has(assetId)
            ? "cancelled"
            : "pending";
    const rowPriceDate = mesaOverviewRowPriceDate(ov);
    const isDifferentPriceDate = Boolean(
      rowPriceDate && latestPriceDate && rowPriceDate !== latestPriceDate,
    );

    const tr = document.createElement("tr");
    tr.dataset.assetId = assetId;

    // 1. Activo / clase traducida
    const tdAsset = document.createElement("td");
    const assetCell = createElement("div", "mesa-asset-cell");
    const line1 = createElement("div", "mesa-asset-line1");
    const assetBtn = createContextualNavigationButton(
      entry.symbol,
      { board: "activo", assetId, subtab: "mercado" },
      "mesa-asset-ticker-btn",
    );
    assetBtn.title = `${entry.symbol} — ${entry.name}`;
    assetBtn.setAttribute("aria-label", `Abrir ${entry.symbol}, ${entry.name}`);
    line1.append(assetBtn);
    const classLabel = typeof translateAssetClass === "function"
      ? translateAssetClass(entry.assetClass)
      : entry.assetClass;
    const exchangeLabel = entry.exchange || "Sin bolsa";
    const line2 = createElement("div", "mesa-asset-line2");
    const nameSpan = createElement("span", "mesa-asset-name", entry.name || entry.symbol);
    nameSpan.title = `${entry.name || entry.symbol} (${assetId})`;
    const subSpan = createElement("span", "mesa-asset-submeta", `· ${classLabel} · ${exchangeLabel}`);
    line2.append(nameSpan, subSpan);
    assetCell.append(line1, line2);
    tdAsset.append(assetCell);

    // 2. Último cierre conocido
    const tdClose = document.createElement("td");
    tdClose.className = "is-numeric";
    const rowCurrency = ov?.market?.currency || entry.quoteCurrency || "USD";
    tdClose.append(
      renderInvestorValueCell(ov?.market?.close, {
        formatValue: (raw) => formatDecimalPrice(raw, rowCurrency),
        unitSuffix: ` ${rowCurrency}`,
        isDifferentPriceDate,
        priceDate: rowPriceDate,
        rowState,
        rowError,
      }),
    );

    // 3. Var 1d
    const tdChange = document.createElement("td");
    tdChange.className = "is-numeric";
    tdChange.append(
      renderInvestorValueCell(ov?.market?.change_1d, {
        formatValue: (raw) => formatDecimalPercentFromRatio(raw, { decimals: 2, signed: true }),
        signedTone: true,
        rowState,
        rowError,
      }),
    );

    // 4. Diagnóstico fundamental
    const tdScore = document.createElement("td");
    tdScore.className = "is-numeric";
    if (ov?.fundamental?.score?.state === "available" && ov.fundamental.score.value !== null) {
      const scoreVal = ov.fundamental.score.value;
      const verdictKey = ov.fundamental.verdict || "";
      const verdict = MESA_VERDICT_LABELS[verdictKey] || verdictKey;
      const wrap = createElement("div", "mesa-score-cell");
      const scorePrimary = createElement("strong", "mesa-value-primary", formatDecimalScore(scoreVal, 1));
      scorePrimary.title = `Score exacto: ${formatExactDecimalText(scoreVal)}/100`;
      wrap.append(scorePrimary, createElement("span", "mesa-value-sub", "/100"));
      if (verdict) {
        const verdictTone = verdictKey === "favorable"
          ? " is-good"
          : verdictKey === "unfavorable"
            ? " is-error"
            : "";
        wrap.append(createElement("span", `mesa-status-pill${verdictTone}`, verdict));
      }
      tdScore.append(wrap);
    } else {
      tdScore.append(
        renderInvestorValueCell(ov?.fundamental?.score, {
          rowState,
          rowError,
        }),
      );
    }

    // 5. P/E anual
    const tdPe = document.createElement("td");
    tdPe.className = "is-numeric";
    tdPe.append(
      renderInvestorValueCell(ov?.valuation?.price_to_earnings, {
        formatValue: (raw) => formatDecimalMultiple(raw, 2),
        rowState,
        rowError,
      }),
    );

    // 6. Detalle
    const tdActions = document.createElement("td");
    const isExpanded = mesaInvestorUiState.expandedAssetIds.has(assetId);
    const expandBtn = createElement(
      "button",
      "mesa-detail-btn",
      isExpanded ? "Ocultar" : "Detalle",
    );
    expandBtn.type = "button";
    expandBtn.setAttribute("aria-expanded", String(isExpanded));
    expandBtn.setAttribute("aria-label", `${isExpanded ? "Ocultar" : "Ver"} detalle de ${entry.symbol}`);
    expandBtn.dataset.detailTriggerFor = assetId;
    expandBtn.addEventListener("click", () => {
      if (!assetId) return;
      if (mesaInvestorUiState.expandedAssetIds.has(assetId)) {
        mesaInvestorUiState.expandedAssetIds.delete(assetId);
      } else {
        mesaInvestorUiState.expandedAssetIds.add(assetId);
      }
      renderMesaInvestorTable();
      const restored = body.querySelector?.(`[data-detail-trigger-for="${assetId}"]`);
      if (restored && typeof restored.focus === "function") restored.focus();
    });
    tdActions.append(expandBtn);

    tr.append(tdAsset, tdClose, tdChange, tdScore, tdPe, tdActions);
    body.append(tr);

    if (isExpanded) {
      const detailTr = document.createElement("tr");
      detailTr.className = "mesa-investor-detail-row";
      const detailTd = document.createElement("td");
      detailTd.colSpan = 6;
      const grid = createElement("div", "mesa-detail-grid");
      if (ov) {
        const verdictText = ov.fundamental?.verdict
          ? ` (${MESA_VERDICT_LABELS[ov.fundamental.verdict] || ov.fundamental.verdict})`
          : "";
        grid.append(
          createElement("span", "", `${entry.name} (${assetId}) · ${classLabel} · ${exchangeLabel}`),
          createElement("span", "", `Cierre: ${formatMesaExactDetailValue(ov.market?.close, "price", rowCurrency)}${rowPriceDate ? ` · fecha precio ${rowPriceDate}` : ""}`),
          createElement("span", "", `Var. 1d: ${formatMesaExactDetailValue(ov.market?.change_1d, "percent")}`),
          createElement("span", "", `Fund. anual: ${formatMesaExactDetailValue(ov.fundamental?.score, "score")}${verdictText}`),
          createElement("span", "", `P/E anual: ${formatMesaExactDetailValue(ov.valuation?.price_to_earnings, "multiple")}${ov.valuation?.annual_period_end ? ` · ejercicio ${String(ov.valuation.annual_period_end).slice(0, 10)}` : ""}`),
          createElement("span", "", `P/B: ${formatMesaExactDetailValue(ov.valuation?.price_to_book, "multiple")}`),
          createElement("span", "", `EV/EBITDA: ${formatMesaExactDetailValue(ov.valuation?.enterprise_value_to_ebitda, "multiple")}`),
          createElement("span", "", `Margen neto: ${formatMesaExactDetailValue(ov.fundamental?.net_margin, "percent")}`),
          createElement("span", "", `Pasivos/Activos: ${formatMesaExactDetailValue(ov.fundamental?.liabilities_to_assets, "ratio")}`),
          createElement("span", "", `Corte UTC: ${knownAt || investorOverviewState.knownAt || "—"}`),
        );
        if (Array.isArray(ov.limitations) && ov.limitations.length) {
          grid.append(createElement("small", "mesa-detail-limitations", `Limitaciones: ${ov.limitations.join(" · ")}`));
        }
      } else {
        grid.append(
          createElement("span", "", `${entry.name} (${assetId}) · ${classLabel} · ${exchangeLabel}`),
          createElement("span", "", `Estado: ${rowState === "error" ? `Error (${rowError || "consulta fallida"})` : rowState === "paused" ? `Carga pausada (${rowError || "lote anterior falló"})` : rowState === "cancelled" ? "Cancelado" : "Pendiente"}`),
        );
      }
      const links = createElement("div", "mesa-detail-links");
      links.append(
        createContextualNavigationButton(
          "Abrir activo",
          { board: "activo", assetId, subtab: "mercado" },
          "button primary compact",
        ),
        createContextualNavigationButton(
          "Ver fuentes y método",
          { board: "activo", assetId, subtab: entry.hasCorporateValuation ? "valoracion" : "mercado" },
          "button secondary compact",
        ),
      );
      if (!ov && (rowState === "error" || rowState === "paused" || rowState === "cancelled")) {
        const retryOne = createElement("button", "button secondary compact", "Reintentar pendientes");
        retryOne.type = "button";
        retryOne.addEventListener("click", () => {
          void retryMesaPendingAssets().catch(() => {});
        });
        links.append(retryOne);
      }
      detailTr.addEventListener?.("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          mesaInvestorUiState.expandedAssetIds.delete(assetId);
          renderMesaInvestorTable();
          const trigger = body.querySelector?.(`[data-detail-trigger-for="${assetId}"]`);
          if (trigger && typeof trigger.focus === "function") trigger.focus();
        }
      });
      detailTd.append(grid, links);
      detailTr.append(detailTd);
      body.append(detailTr);
    }
  }
}

function bindMesaInvestorControls() {
  if (mesaInvestorUiState.controlsBound) return;
  mesaInvestorUiState.controlsBound = true;
  byId("mesa-retry-load-btn")?.addEventListener("click", () => {
    void retryMesaPendingAssets().catch(() => {});
  });
  byId("mesa-banner-retry-btn")?.addEventListener("click", () => {
    void retryMesaPendingAssets().catch(() => {});
  });
  byId("mesa-banner-system-btn")?.addEventListener("click", () => {
    if (typeof activateBoard === "function") {
      activateBoard("sistema", { focus: true });
    }
  });
  byId("mesa-search")?.addEventListener("input", (e) => {
    mesaInvestorUiState.search = e.target.value;
    mesaInvestorUiState.visibleLimit = MESA_INVESTOR_BATCH_SIZE;
    renderMesaInvestorTable();
  });
  byId("mesa-filter-class")?.addEventListener("change", (e) => {
    mesaInvestorUiState.assetClass = e.target.value;
    mesaInvestorUiState.visibleLimit = MESA_INVESTOR_BATCH_SIZE;
    renderMesaInvestorTable();
  });
  byId("mesa-filter-watchlist")?.addEventListener("change", (e) => {
    mesaInvestorUiState.watchlist = e.target.value;
    mesaInvestorUiState.visibleLimit = MESA_INVESTOR_BATCH_SIZE;
    renderMesaInvestorTable();
  });
  byId("mesa-filter-verdict")?.addEventListener("change", (e) => {
    mesaInvestorUiState.verdict = e.target.value;
    mesaInvestorUiState.visibleLimit = MESA_INVESTOR_BATCH_SIZE;
    renderMesaInvestorTable();
  });
  for (const btn of document.querySelectorAll(".mesa-sort-btn")) {
    btn.addEventListener("click", () => {
      const col = btn.dataset.mesaSort;
      if (mesaInvestorUiState.sortBy === col) {
        mesaInvestorUiState.sortDir = mesaInvestorUiState.sortDir === "asc" ? "desc" : "asc";
      } else {
        mesaInvestorUiState.sortBy = col;
        mesaInvestorUiState.sortDir = "asc";
      }
      renderMesaInvestorTable();
    });
  }
  byId("mesa-show-more-btn")?.addEventListener("click", async () => {
    mesaInvestorUiState.visibleLimit += MESA_INVESTOR_BATCH_SIZE;
    renderMesaInvestorTable();
    try {
      await loadMesaInvestorVisibleBatch();
    } catch {}
  });
  byId("mesa-load-all-btn")?.addEventListener("click", () => {
    void loadMesaInvestorSelectedUniverse().catch(() => {});
  });
  byId("mesa-cancel-load-btn")?.addEventListener("click", () => {
    mesaInvestorUiState.cancelRequested = true;
    mesaInvestorUiState.batchStatus = "cancelled";
    for (const id of mesaInvestorUiState.loadingAssetIds) {
      mesaInvestorUiState.cancelledAssetIds.add(id);
    }
    mesaInvestorUiState.loadingAssetIds.clear();
    if (mesaInvestorUiState.abortController) {
      try {
        mesaInvestorUiState.abortController.abort();
      } catch {}
    }
    renderMesaInvestorTable();
  });
}

async function runMesaBatchSequence(targetIds, knownAt, { seq, controller } = {}) {
  mesaInvestorUiState.hasQueriedCut = knownAt;
  if (!Array.isArray(targetIds) || targetIds.length === 0) {
    mesaInvestorUiState.batchStatus = "ready";
    mesaInvestorUiState.batchError = null;
    mesaInvestorUiState.pausedReason = null;
    renderMesaInvestorTable();
    return;
  }
  mesaInvestorUiState.batchStatus = "loading";
  mesaInvestorUiState.batchError = null;
  mesaInvestorUiState.pausedReason = null;
  for (const id of targetIds) {
    mesaInvestorUiState.failedAssetIds.delete(id);
    mesaInvestorUiState.cancelledAssetIds.delete(id);
    mesaInvestorUiState.pausedAssetIds.delete(id);
  }
  try {
    for (let i = 0; i < targetIds.length; i += MESA_INVESTOR_BATCH_SIZE) {
      if (
        seq !== mesaInvestorUiState.requestSequence
        || mesaInvestorUiState.cancelRequested
        || controller?.signal?.aborted
      ) {
        if (seq === mesaInvestorUiState.requestSequence) {
          for (const id of targetIds.slice(i)) {
            if (!getCachedInvestorOverviewAsset(id, knownAt)) {
              mesaInvestorUiState.cancelledAssetIds.add(id);
            }
          }
          mesaInvestorUiState.batchStatus = "cancelled";
        }
        return;
      }
      const chunk = targetIds.slice(i, i + MESA_INVESTOR_BATCH_SIZE);
      for (const id of chunk) mesaInvestorUiState.loadingAssetIds.add(id);
      renderMesaInvestorTable();
      try {
        await fetchInvestorOverviewBatch(chunk, knownAt, { signal: controller?.signal });
      } catch (error) {
        for (const id of chunk) mesaInvestorUiState.loadingAssetIds.delete(id);
        if (error?.name === "AbortError") {
          if (seq === mesaInvestorUiState.requestSequence) {
            for (const id of targetIds.slice(i)) {
              if (!getCachedInvestorOverviewAsset(id, knownAt)) {
                mesaInvestorUiState.cancelledAssetIds.add(id);
              }
            }
            mesaInvestorUiState.batchStatus = "cancelled";
            renderMesaInvestorTable();
          }
          return;
        }
        if (seq !== mesaInvestorUiState.requestSequence) return;
        const safeMsg = sanitizeMesaErrorMessage(error);
        for (const id of chunk) {
          if (!getCachedInvestorOverviewAsset(id, knownAt)) {
            mesaInvestorUiState.failedAssetIds.set(id, safeMsg);
          }
        }
        for (const id of targetIds.slice(i + chunk.length)) {
          if (!getCachedInvestorOverviewAsset(id, knownAt)) {
            mesaInvestorUiState.pausedAssetIds.add(id);
          }
        }
        mesaInvestorUiState.pausedReason = safeMsg;
        mesaInvestorUiState.batchStatus = "error";
        mesaInvestorUiState.batchError = safeMsg;
        renderMesaInvestorTable();
        throw error;
      }
      for (const id of chunk) {
        mesaInvestorUiState.loadingAssetIds.delete(id);
        mesaInvestorUiState.failedAssetIds.delete(id);
        mesaInvestorUiState.pausedAssetIds.delete(id);
        mesaInvestorUiState.cancelledAssetIds.delete(id);
      }
      if (seq !== mesaInvestorUiState.requestSequence) return;
      renderMesaInvestorTable();
    }
    if (seq === mesaInvestorUiState.requestSequence && mesaInvestorUiState.batchStatus === "loading") {
      mesaInvestorUiState.batchStatus = "ready";
      mesaInvestorUiState.batchError = null;
      mesaInvestorUiState.pausedReason = null;
    }
  } finally {
    if (seq === mesaInvestorUiState.requestSequence) {
      mesaInvestorUiState.loadingAssetIds.clear();
      mesaInvestorUiState.loadingAll = false;
      mesaInvestorUiState.cancelRequested = false;
      if (mesaInvestorUiState.abortController === controller) {
        mesaInvestorUiState.abortController = null;
      }
      renderMesaInvestorTable();
    }
  }
}

async function loadMesaInvestorVisibleBatch() {
  const knownAt = byId("report-known-at")?.value.trim() || "";
  if (!knownAt) return;
  abortMesaInvestorRequests();
  mesaInvestorUiState.lastRequestedScope = "visible";
  const seq = mesaInvestorUiState.requestSequence;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  mesaInvestorUiState.abortController = controller;
  const visibleEntries = mesaSortedEntries(mesaFilteredCatalogEntries())
    .slice(0, mesaInvestorUiState.visibleLimit);
  const invalidEntry = visibleEntries.find((e) => typeof e.assetId !== "string" || !e.assetId.trim());
  if (invalidEntry) {
    mesaInvestorUiState.batchStatus = "error";
    mesaInvestorUiState.batchError = "El catálogo contiene una entrada sin identificador válido.";
    renderMesaInvestorTable();
    throw new Error(mesaInvestorUiState.batchError);
  }
  const missing = visibleEntries
    .map((e) => e.assetId)
    .filter((id) => !getCachedInvestorOverviewAsset(id, knownAt));
  await runMesaBatchSequence(missing, knownAt, { seq, controller });
}

async function loadMesaInvestorSelectedUniverse() {
  const knownAt = byId("report-known-at")?.value.trim() || "";
  if (!knownAt || mesaInvestorUiState.loadingAll) return;
  abortMesaInvestorRequests();
  mesaInvestorUiState.lastRequestedScope = "selected";
  const seq = mesaInvestorUiState.requestSequence;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  mesaInvestorUiState.abortController = controller;
  mesaInvestorUiState.loadingAll = true;
  mesaInvestorUiState.cancelRequested = false;
  const selectedEntries = mesaFilteredCatalogEntries().slice(0, MESA_INVESTOR_MAX_UNIVERSE);
  const invalidEntry = selectedEntries.find((e) => typeof e.assetId !== "string" || !e.assetId.trim());
  if (invalidEntry) {
    mesaInvestorUiState.loadingAll = false;
    mesaInvestorUiState.batchStatus = "error";
    mesaInvestorUiState.batchError = "El catálogo contiene una entrada sin identificador válido.";
    renderMesaInvestorTable();
    return;
  }
  const targets = selectedEntries
    .map((e) => e.assetId)
    .filter((id) => !getCachedInvestorOverviewAsset(id, knownAt));
  await runMesaBatchSequence(targets, knownAt, { seq, controller });
}

async function retryMesaPendingAssets() {
  const knownAt = byId("report-known-at")?.value.trim() || "";
  if (!knownAt || mesaInvestorUiState.loadingAll || mesaInvestorUiState.loadingAssetIds.size > 0) {
    return;
  }
  abortMesaInvestorRequests();
  const seq = mesaInvestorUiState.requestSequence;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  mesaInvestorUiState.abortController = controller;
  mesaInvestorUiState.cancelRequested = false;
  const isSelectedScope = mesaInvestorUiState.lastRequestedScope === "selected"
    || mesaInvestorUiState.pausedAssetIds.size > 0;
  if (isSelectedScope) mesaInvestorUiState.loadingAll = true;
  const candidates = isSelectedScope
    ? mesaSortedEntries(mesaFilteredCatalogEntries()).slice(0, MESA_INVESTOR_MAX_UNIVERSE)
    : mesaSortedEntries(mesaFilteredCatalogEntries()).slice(0, mesaInvestorUiState.visibleLimit);
  const pendingSet = new Set([
    ...mesaInvestorUiState.failedAssetIds.keys(),
    ...mesaInvestorUiState.pausedAssetIds,
    ...mesaInvestorUiState.cancelledAssetIds,
  ]);
  const targets = [];
  for (const entry of candidates) {
    const id = typeof entry.assetId === "string" ? entry.assetId : "";
    if (!id || getCachedInvestorOverviewAsset(id, knownAt)) continue;
    if (pendingSet.size === 0 || pendingSet.has(id) || isSelectedScope) {
      targets.push(id);
    }
  }
  for (const id of pendingSet) {
    if (id && !getCachedInvestorOverviewAsset(id, knownAt) && !targets.includes(id)) {
      targets.push(id);
    }
  }
  await runMesaBatchSequence(targets, knownAt, { seq, controller });
}

async function loadMesaInvestorOverview() {
  bindMesaInvestorControls();
  const knownAt = byId("report-known-at")?.value.trim() || "";
  if (
    knownAt
    && sameIsoTimestamp(mesaInvestorUiState.hasQueriedCut, knownAt)
    && (mesaInvestorUiState.failedAssetIds.size > 0
      || mesaInvestorUiState.pausedAssetIds.size > 0
      || mesaInvestorUiState.batchStatus === "error")
  ) {
    renderMesaInvestorTable();
    return;
  }
  renderMesaInvestorTable();
  await loadMesaInvestorVisibleBatch();
}

function renderActivoCompactHeader(ov, cutIso = null) {
  const container = byId("asset-compact-header");
  if (!container || !ov) return;
  container.replaceChildren();
  const resolvedCut = String(
    cutIso
    || ov.known_at
    || investorOverviewState.knownAt
    || byId("report-known-at")?.value?.trim()
    || "",
  ).trim();
  const knownAtShort = resolvedCut && typeof formatKnownAtLabel === "function"
    ? formatKnownAtLabel(resolvedCut)
    : (resolvedCut || "Sin corte");
  const metaBar = createElement("div", "asset-compact-meta-bar");
  const cutLabel = createElement("span", "", `Resumen al corte: ${knownAtShort} (America/Lima)`);
  if (resolvedCut) {
    cutLabel.title = `Corte UTC: ${resolvedCut}`;
    cutLabel.setAttribute("aria-label", `Resumen al corte ${knownAtShort}, UTC ${resolvedCut}`);
  }
  metaBar.append(cutLabel);
  if (ov.valuation?.annual_period_end) {
    metaBar.append(createElement("span", "", `Últ. anual: ${String(ov.valuation.annual_period_end).slice(0, 10)}`));
  }

  const grid = createElement("div", "asset-compact-grid");
  const marketCurrency = ov.market?.currency || "USD";
  const marketCard = createElement("div", "asset-compact-domain");
  const priceDate = ov.market?.close?.reference_at ? String(ov.market.close.reference_at).slice(0, 10) : "";
  marketCard.append(
    createElement("small", "", `Último cierre${priceDate ? ` (${priceDate})` : ""}`),
    renderInvestorValueCell(ov.market?.close, {
      formatValue: (raw) => formatDecimalPrice(raw, marketCurrency),
      unitSuffix: ` ${marketCurrency}`,
    }),
  );
  const fundCard = createElement("div", "asset-compact-domain");
  fundCard.append(createElement("small", "", "Diagnóstico fundamental"));
  if (ov.fundamental?.score?.state === "available" && ov.fundamental.score.value !== null) {
    const verdictKey = ov.fundamental.verdict || "";
    const verdict = MESA_VERDICT_LABELS[verdictKey] || verdictKey;
    const scoreWrap = createElement("div", "mesa-score-cell");
    scoreWrap.append(
      createElement("strong", "mesa-value-primary", `${formatDecimalScore(ov.fundamental.score.value, 1)}/100`),
    );
    if (verdict) {
      const tone = verdictKey === "favorable" ? " is-good" : verdictKey === "unfavorable" ? " is-error" : "";
      scoreWrap.append(createElement("span", `mesa-status-pill${tone}`, verdict));
    }
    fundCard.append(scoreWrap);
  } else {
    fundCard.append(renderInvestorValueCell(ov.fundamental?.score));
  }
  const valCard = createElement("div", "asset-compact-domain");
  valCard.append(
    createElement("small", "", "P/E anual"),
    renderInvestorValueCell(ov.valuation?.price_to_earnings, {
      formatValue: (raw) => formatDecimalMultiple(raw, 2),
    }),
  );
  grid.append(marketCard, fundCard, valCard);
  container.append(metaBar, grid);
}
