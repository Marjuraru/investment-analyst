"use strict";

// UI-6: Mesa's "Novedades" layer, reglas analíticas family. Reuses
// /api/v1/candidate-notifications exactly as the revisar/sistema panels
// already do; this is a second, independent read into Mesa's own
// container, never the same DOM id as those boards.
let mesaAnalyticalNewsRequestSequence = 0;

function mesaContextualNavigationItem(className, label, request) {
  const item = createContextualNavigationButton(
    label,
    request,
    "alert-inbox-item mesa-contextual-item " + className,
  );
  item.setAttribute("aria-label", label);
  return item;
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
    const item = mesaContextualNavigationItem(
      "mesa-analytical-item",
      "Abrir candidato " + notification.candidate_id,
      { board: "revisar", family: "candidate", id: notification.candidate_id },
    );
    item.append(
      createElement("strong", "", `${notification.rule_id} · ${notification.asset_id}`),
      createElement("time", "", formatInstant(notification.created_at)),
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
  for (const view of items) {
    const notification = view.item;
    const item = mesaContextualNavigationItem(
      "mesa-cazatiburones-" + family + "-item",
      "Abrir " + family + " para " + notification.asset_id,
      { board: "cazatiburones", family, id: notification.asset_id },
    );
    item.append(
      createElement("strong", "", `${notification.rule_id} · ${notification.asset_id}`),
      createElement("span", "alert-inbox-status", MESA_CAZATIBURONES_STATUS_LABELS[view.status] || view.status),
      createElement("time", "", formatInstant(notification.created_at)),
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
    const item = mesaContextualNavigationItem(
      "mesa-incident-item",
      "Abrir incidencia " + event.alert_id,
      { board: "revisar", family: "alert", id: event.alert_id },
    );
    item.append(
      createElement("strong", "", event.title),
      createElement(
        "span",
        `alert-inbox-status ${event.status}`,
        MESA_INCIDENT_STATUS_LABELS[event.status] || event.status,
      ),
      createElement("time", "", formatInstant(event.last_activated_at)),
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
let mesaUniverseCoverageRequestSequence = 0;

function mesaCoverageWindowFromKnownAt(knownAtIso) {
  const cut = new Date(knownAtIso);
  const endMs = Date.UTC(cut.getUTCFullYear(), cut.getUTCMonth(), cut.getUTCDate()) - 86_400_000;
  const startMs = endMs - MESA_COVERAGE_WINDOW_DAYS * 86_400_000;
  const toDateString = (ms) => new Date(ms).toISOString().slice(0, 10);
  return { start: toDateString(startMs), end: toDateString(endMs) };
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
  body.replaceChildren();
  for (const asset of payload.assets || []) {
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
      "Abrir activo " + (asset.symbol || asset.asset_id || "sin identidad"),
    );
    assetButton.append(
      createElement("strong", "", asset.symbol || asset.asset_id),
      document.createElement("br"),
      createElement("small", "", asset.name || "Activo sin nombre"),
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
        "Abrir " + label + " para " + (asset.symbol || asset.asset_id || "activo sin identidad"),
      );
      domainButton.innerHTML = mesaUniverseCellMarkup(
        coverage.capability,
        coverage.evidence,
        coverage.reference_age_days ?? coverage.latest_input_age_days ?? null,
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
  renderMesaBvlRegistrySummary(payload);
}

function renderMesaUniverseAbsentTable(mark) {
  const body = byId("mesa-universe-table-body");
  body.replaceChildren();
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.append(mark);
  row.append(cell);
  body.append(row);
}

async function loadMesaUniverseCoverage() {
  const sequence = ++mesaUniverseCoverageRequestSequence;
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
    fundamental_start: coverageWindow.start,
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
  } finally {
    if (sequence === mesaUniverseCoverageRequestSequence) body.setAttribute("aria-busy", "false");
  }
}
