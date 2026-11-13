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
  loadingAssetIds: new Set(),
  failedAssetIds: new Map(),
  cancelledAssetIds: new Set(),
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
  not_configured: "Sin configurar",
  not_supported: "No soportado",
  insufficient_data: "Datos insuficientes",
});

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

function renderInvestorValueCell(val, options = {}) {
  const wrap = createElement("div", "mesa-value-cell");
  if (!val) {
    if (options.rowState === "loading") {
      wrap.append(createElement("span", "mesa-value-pending", "Consultando…"));
      return wrap;
    }
    if (options.rowState === "error") {
      wrap.append(createElement("span", "mesa-value-error", "Error al consultar"));
      if (options.rowError) {
        wrap.append(createElement("small", "mesa-value-sub", options.rowError));
      }
      return wrap;
    }
    if (options.rowState === "cancelled") {
      wrap.append(createElement("span", "mesa-value-cancelled", "Cancelado"));
      return wrap;
    }
    wrap.append(createElement("span", "mesa-value-pending", "Pendiente"));
    return wrap;
  }
  if (val.state === "available" && val.value !== null) {
    const formatted = options.percent
      ? decimalScalePercent(val.value, 2)
      : `${val.value}${options.unitSuffix || ""}`;
    wrap.append(createElement("strong", "mesa-value-main", formatted));
    if (options.subtext) {
      wrap.append(createElement("small", "mesa-value-sub", options.subtext));
    } else if (val.reference_at) {
      wrap.append(createElement("small", "mesa-value-sub", String(val.reference_at).slice(0, 10)));
    }
    return wrap;
  }
  const label = MESA_VALUE_STATE_LABELS[val.state] || val.state;
  const rawReason = Array.isArray(val.reason_codes) && val.reason_codes.length ? val.reason_codes[0] : "";
  const reason = typeof translateOverviewAbsenceReason === "function"
    ? translateOverviewAbsenceReason(rawReason)
    : rawReason;
  wrap.append(createElement("span", `mesa-value-absent state-${val.state}`, label));
  if (reason) wrap.append(createElement("small", "mesa-value-sub", reason));
  if (options.knownAtLabel) {
    wrap.append(createElement("small", "mesa-value-sub", `Corte ${options.knownAtLabel}`));
  }
  return wrap;
}

function ensureMesaRetryButton() {
  return byId("mesa-retry-load-btn");
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

  const statusEl = byId("mesa-investor-status");
  if (statusEl) {
    if (mesaInvestorUiState.batchStatus === "error" && mesaInvestorUiState.batchError) {
      statusEl.textContent = `Error al cargar Mesa: ${mesaInvestorUiState.batchError} · Cargados ${loadedCount}/${filtered.length}`;
    } else if (mesaInvestorUiState.batchStatus === "cancelled") {
      statusEl.textContent = `Carga cancelada · Cargados ${loadedCount}/${filtered.length} · Mostrando ${visible.length} de ${filtered.length}`;
    } else if (mesaInvestorUiState.loadingAssetIds.size > 0 || mesaInvestorUiState.loadingAll) {
      statusEl.textContent = `Consultando lote… Cargados ${loadedCount}/${filtered.length} · Mostrando ${visible.length} de ${filtered.length}`;
    } else {
      statusEl.textContent = `Datos cargados ${loadedCount}/${filtered.length} · Mostrando ${visible.length} de ${filtered.length}`;
    }
  }
  const winEl = byId("mesa-investor-window");
  if (winEl) {
    if (investorOverviewState.windows && investorOverviewState.knownAt === knownAt) {
      const w = investorOverviewState.windows;
      winEl.textContent = `Corte efectivo: ${knownAtShort} · Ventana mercado ${w.market_start}..${w.market_end} · fundamental anual ${w.fundamental_start}..${w.fundamental_end}`;
    } else {
      winEl.textContent = knownAt
        ? `Corte efectivo: ${knownAtShort}`
        : "Sin corte known_at definido";
    }
  }
  const showMoreBtn = byId("mesa-show-more-btn");
  if (showMoreBtn) {
    showMoreBtn.classList.toggle("hidden", visible.length >= filtered.length);
  }
  const loadAllBtn = byId("mesa-load-all-btn");
  const cancelBtn = byId("mesa-cancel-load-btn");
  const retryBtn = ensureMesaRetryButton();
  const isBusy = mesaInvestorUiState.loadingAll || mesaInvestorUiState.loadingAssetIds.size > 0;
  if (loadAllBtn) {
    loadAllBtn.disabled = isBusy || loadedCount >= Math.min(filtered.length, MESA_INVESTOR_MAX_UNIVERSE);
  }
  if (cancelBtn) {
    cancelBtn.classList.toggle("hidden", !isBusy);
  }
  if (retryBtn) {
    const hasRecoverable = Boolean(
      mesaInvestorUiState.batchError
      || mesaInvestorUiState.failedAssetIds.size > 0
      || mesaInvestorUiState.cancelledAssetIds.size > 0,
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
    const rowError = assetId ? mesaInvestorUiState.failedAssetIds.get(assetId) : "Identificador de activo inválido.";
    const rowState = !assetId || rowError
      ? "error"
      : mesaInvestorUiState.loadingAssetIds.has(assetId)
        ? "loading"
        : mesaInvestorUiState.cancelledAssetIds.has(assetId)
          ? "cancelled"
          : "pending";
    const tr = document.createElement("tr");
    tr.dataset.assetId = assetId;

    // 1. Activo / clase
    const tdAsset = document.createElement("td");
    const assetBtn = createContextualNavigationButton(
      `${entry.symbol} · ${entry.name}`,
      { board: "activo", assetId, subtab: "mercado" },
      "link-button mesa-asset-link",
    );
    tdAsset.append(assetBtn, createElement("small", "mesa-value-sub", `${entry.assetClass} · ${entry.exchange || "UNKNOWN"}`));

    // 2. Último cierre conocido
    const tdClose = document.createElement("td");
    tdClose.append(
      renderInvestorValueCell(ov?.market?.close, {
        unitSuffix: ov?.market?.currency ? ` ${ov.market.currency}` : "",
        rowState,
        rowError,
        knownAtLabel: knownAtShort,
      }),
    );

    // 3. Var 1d
    const tdChange = document.createElement("td");
    tdChange.append(
      renderInvestorValueCell(ov?.market?.change_1d, {
        percent: true,
        rowState,
        rowError,
      }),
    );

    // 4. Diagnóstico fundamental
    const tdScore = document.createElement("td");
    if (ov?.fundamental?.score?.state === "available") {
      const scoreVal = ov.fundamental.score.value;
      const verdict = MESA_VERDICT_LABELS[ov.fundamental.verdict] || ov.fundamental.verdict || "";
      const wrap = createElement("div", "mesa-score-cell");
      wrap.append(createElement("strong", "", `${scoreVal}/100 ${verdict ? `· ${verdict}` : ""}`));
      const bar = createElement("div", "mesa-score-bar");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(scoreVal));
      const fill = createElement("span", "mesa-score-fill");
      const clamped = Math.max(0, Math.min(100, Number(scoreVal) || 0));
      fill.style.width = `${clamped}%`;
      bar.append(fill);
      wrap.append(bar);
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
    const peSub = ov?.valuation?.annual_period_end ? String(ov.valuation.annual_period_end).slice(0, 10) : "";
    tdPe.append(
      renderInvestorValueCell(ov?.valuation?.price_to_earnings, {
        subtext: peSub,
        rowState,
        rowError,
      }),
    );

    // 6. Detalle / acciones
    const tdActions = document.createElement("td");
    const expandBtn = createElement(
      "button",
      "button secondary compact",
      mesaInvestorUiState.expandedAssetIds.has(assetId) ? "Ocultar" : "Detalle",
    );
    expandBtn.type = "button";
    expandBtn.addEventListener("click", () => {
      if (!assetId) return;
      if (mesaInvestorUiState.expandedAssetIds.has(assetId)) {
        mesaInvestorUiState.expandedAssetIds.delete(assetId);
      } else {
        mesaInvestorUiState.expandedAssetIds.add(assetId);
      }
      renderMesaInvestorTable();
    });
    tdActions.append(expandBtn);

    tr.append(tdAsset, tdClose, tdChange, tdScore, tdPe, tdActions);
    body.append(tr);

    if (mesaInvestorUiState.expandedAssetIds.has(assetId) && ov) {
      const detailTr = document.createElement("tr");
      detailTr.className = "mesa-investor-detail-row";
      const detailTd = document.createElement("td");
      detailTd.colSpan = 6;
      const grid = createElement("div", "mesa-detail-grid");
      grid.append(
        createElement("span", "", `P/B: ${ov.valuation?.price_to_book?.value ?? (MESA_VALUE_STATE_LABELS[ov.valuation?.price_to_book?.state] || "—")}`),
        createElement("span", "", `EV/EBITDA: ${ov.valuation?.enterprise_value_to_ebitda?.value ?? (MESA_VALUE_STATE_LABELS[ov.valuation?.enterprise_value_to_ebitda?.state] || "—")}`),
        createElement("span", "", `Margen neto: ${ov.fundamental?.net_margin?.value ? decimalScalePercent(ov.fundamental.net_margin.value, 2) : (MESA_VALUE_STATE_LABELS[ov.fundamental?.net_margin?.state] || "—")}`),
        createElement("span", "", `Pasivos/Activos: ${ov.fundamental?.liabilities_to_assets?.value ?? (MESA_VALUE_STATE_LABELS[ov.fundamental?.liabilities_to_assets?.state] || "—")}`),
      );
      if (Array.isArray(ov.limitations) && ov.limitations.length) {
        grid.append(createElement("small", "mesa-detail-limitations", `Limitaciones: ${ov.limitations.join(" · ")}`));
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
    void loadMesaInvestorVisibleBatch().catch(() => {});
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

async function loadMesaInvestorVisibleBatch() {
  const knownAt = byId("report-known-at")?.value.trim() || "";
  if (!knownAt) return;
  abortMesaInvestorRequests();
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
  const visible = visibleEntries.map((e) => e.assetId);
  const missing = visible.filter((id) => !getCachedInvestorOverviewAsset(id, knownAt));
  if (missing.length === 0) {
    mesaInvestorUiState.batchStatus = "ready";
    mesaInvestorUiState.batchError = null;
    renderMesaInvestorTable();
    return;
  }
  mesaInvestorUiState.batchStatus = "loading";
  mesaInvestorUiState.batchError = null;
  for (const id of missing) {
    mesaInvestorUiState.failedAssetIds.delete(id);
    mesaInvestorUiState.cancelledAssetIds.delete(id);
  }
  try {
    for (let i = 0; i < missing.length; i += MESA_INVESTOR_BATCH_SIZE) {
      if (seq !== mesaInvestorUiState.requestSequence || mesaInvestorUiState.cancelRequested) return;
      const chunk = missing.slice(i, i + MESA_INVESTOR_BATCH_SIZE);
      for (const id of chunk) mesaInvestorUiState.loadingAssetIds.add(id);
      renderMesaInvestorTable();
      try {
        await fetchInvestorOverviewBatch(chunk, knownAt, { signal: controller?.signal });
      } catch (error) {
        for (const id of chunk) mesaInvestorUiState.loadingAssetIds.delete(id);
        if (error?.name === "AbortError") {
          if (seq === mesaInvestorUiState.requestSequence) {
            for (const id of chunk) mesaInvestorUiState.cancelledAssetIds.add(id);
            mesaInvestorUiState.batchStatus = "cancelled";
            renderMesaInvestorTable();
          }
          return;
        }
        if (seq !== mesaInvestorUiState.requestSequence) return;
        for (const id of chunk) {
          mesaInvestorUiState.failedAssetIds.set(id, error?.message || "Error al consultar");
        }
        mesaInvestorUiState.batchStatus = "error";
        mesaInvestorUiState.batchError = error?.message || "No se pudo cargar el lote de Mesa.";
        renderMesaInvestorTable();
        throw error;
      }
      for (const id of chunk) mesaInvestorUiState.loadingAssetIds.delete(id);
      if (seq !== mesaInvestorUiState.requestSequence) return;
      renderMesaInvestorTable();
    }
    if (seq === mesaInvestorUiState.requestSequence) {
      mesaInvestorUiState.batchStatus = "ready";
      renderMesaInvestorTable();
    }
  } finally {
    if (mesaInvestorUiState.abortController === controller) {
      mesaInvestorUiState.abortController = null;
    }
  }
}

async function loadMesaInvestorSelectedUniverse() {
  const knownAt = byId("report-known-at")?.value.trim() || "";
  if (!knownAt || mesaInvestorUiState.loadingAll) return;
  abortMesaInvestorRequests();
  const seq = mesaInvestorUiState.requestSequence;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  mesaInvestorUiState.abortController = controller;
  mesaInvestorUiState.loadingAll = true;
  mesaInvestorUiState.cancelRequested = false;
  mesaInvestorUiState.batchStatus = "loading";
  mesaInvestorUiState.batchError = null;
  renderMesaInvestorTable();
  try {
    const selectedEntries = mesaFilteredCatalogEntries().slice(0, MESA_INVESTOR_MAX_UNIVERSE);
    const invalidEntry = selectedEntries.find((e) => typeof e.assetId !== "string" || !e.assetId.trim());
    if (invalidEntry) {
      mesaInvestorUiState.batchStatus = "error";
      mesaInvestorUiState.batchError = "El catálogo contiene una entrada sin identificador válido.";
      return;
    }
    const targets = selectedEntries
      .map((e) => e.assetId)
      .filter((id) => !getCachedInvestorOverviewAsset(id, knownAt));
    for (const id of targets) {
      mesaInvestorUiState.failedAssetIds.delete(id);
      mesaInvestorUiState.cancelledAssetIds.delete(id);
    }
    for (let i = 0; i < targets.length; i += MESA_INVESTOR_BATCH_SIZE) {
      if (seq !== mesaInvestorUiState.requestSequence || mesaInvestorUiState.cancelRequested) {
        for (const id of targets.slice(i)) mesaInvestorUiState.cancelledAssetIds.add(id);
        mesaInvestorUiState.batchStatus = "cancelled";
        break;
      }
      const chunk = targets.slice(i, i + MESA_INVESTOR_BATCH_SIZE);
      for (const id of chunk) mesaInvestorUiState.loadingAssetIds.add(id);
      renderMesaInvestorTable();
      try {
        await fetchInvestorOverviewBatch(chunk, knownAt, { signal: controller?.signal });
      } catch (error) {
        for (const id of chunk) mesaInvestorUiState.loadingAssetIds.delete(id);
        if (error?.name === "AbortError") {
          for (const id of targets.slice(i)) mesaInvestorUiState.cancelledAssetIds.add(id);
          if (seq === mesaInvestorUiState.requestSequence) {
            mesaInvestorUiState.batchStatus = "cancelled";
          }
          break;
        }
        if (seq === mesaInvestorUiState.requestSequence) {
          for (const id of chunk) {
            mesaInvestorUiState.failedAssetIds.set(id, error?.message || "Error al consultar");
          }
          mesaInvestorUiState.batchStatus = "error";
          mesaInvestorUiState.batchError = error?.message || "No se pudo cargar el universo.";
        }
        throw error;
      }
      for (const id of chunk) mesaInvestorUiState.loadingAssetIds.delete(id);
      if (seq !== mesaInvestorUiState.requestSequence) break;
      renderMesaInvestorTable();
    }
    if (seq === mesaInvestorUiState.requestSequence && mesaInvestorUiState.batchStatus === "loading") {
      mesaInvestorUiState.batchStatus = "ready";
    }
  } finally {
    mesaInvestorUiState.loadingAssetIds.clear();
    mesaInvestorUiState.loadingAll = false;
    mesaInvestorUiState.cancelRequested = false;
    if (mesaInvestorUiState.abortController === controller) {
      mesaInvestorUiState.abortController = null;
    }
    renderMesaInvestorTable();
  }
}

async function loadMesaInvestorOverview() {
  bindMesaInvestorControls();
  renderMesaInvestorTable();
  await loadMesaInvestorVisibleBatch();
}

function renderActivoCompactHeader(ov) {
  const container = byId("asset-compact-header");
  if (!container || !ov) return;
  container.replaceChildren();
  const knownAt = typeof formatKnownAtLabel === "function" ? formatKnownAtLabel(ov.known_at) : (ov.known_at || "");
  const grid = createElement("div", "asset-compact-grid");
  const marketCard = createElement("div", "asset-compact-domain");
  marketCard.append(
    createElement("small", "", `Último cierre conocido · corte ${knownAt}`),
    renderInvestorValueCell(ov.market?.close, {
      unitSuffix: ov.market?.currency ? ` ${ov.market.currency}` : "",
      knownAtLabel: knownAt,
    }),
  );
  const fundCard = createElement("div", "asset-compact-domain");
  fundCard.append(
    createElement("small", "", `Diagnóstico fundamental · corte ${knownAt}`),
    ov.fundamental?.score?.state === "available"
      ? createElement("strong", "", `${ov.fundamental.score.value}/100 · ${MESA_VERDICT_LABELS[ov.fundamental.verdict] || ov.fundamental.verdict || ""}`)
      : renderInvestorValueCell(ov.fundamental?.score, { knownAtLabel: knownAt }),
  );
  const valCard = createElement("div", "asset-compact-domain");
  valCard.append(
    createElement("small", "", `P/E anual · corte ${knownAt}`),
    renderInvestorValueCell(ov.valuation?.price_to_earnings, {
      subtext: ov.valuation?.annual_period_end ? String(ov.valuation.annual_period_end).slice(0, 10) : "",
      knownAtLabel: knownAt,
    }),
  );
  grid.append(marketCard, fundCard, valCard);
  container.append(grid);
}
