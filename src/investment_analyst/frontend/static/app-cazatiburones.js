"use strict";

// Cazatiburones (UI-4): three separate read-only presentations over
// already-integrated point-in-time endpoints (issue 159), sharing the
// single global known_at cut (control id report-known-at) and a local row
// selection -- this board never changes the global asset, never fabricates
// its own cut, never adds a second asset selector, and never issues anything
// but a GET read. asset_document and
// filer_document, insider and beneficial, and every 13F row stay in
// disjoint containers with independent counts: nothing here is combined
// into an effective portfolio, a score or a signal.
let cazatiburonesRequestSequence = 0;
let cazatiburonesSelectedAssetId = null;

function cazatiburonesBoardIsActive() {
  const section = byId("board-cazatiburones");
  return section !== null && !section.hidden;
}

function cazatiburonesSelectedPresentation() {
  const presentation = marketAssets[cazatiburonesSelectedAssetId];
  return presentation && presentation.hasFundamentals && presentation.fundamentalMode === "corporate"
    ? presentation
    : null;
}

function renderCazatiburonesDetailHeader() {
  const presentation = marketAssets[cazatiburonesSelectedAssetId];
  if (!presentation) return;
  const header = byId("cazatiburones-detail-header");
  header.hidden = false;
  header.classList.remove("hidden");
  byId("cazatiburones-detail-asset").textContent =
    `${presentation.symbol} · ${presentation.name}`;
  byId("cazatiburones-detail-empty").classList.add("hidden");
}

function resetCazatiburonesDetail() {
  cazatiburonesRequestSequence += 1;
  cazatiburonesSelectedAssetId = null;
  const header = byId("cazatiburones-detail-header");
  header.classList.add("hidden");
  header.hidden = true;
  byId("cazatiburones-detail-asset").textContent = "Sin activo seleccionado";
  byId("cazatiburones-detail-empty").classList.remove("hidden");
  const content = byId("cazatiburones-detail-content");
  content.classList.add("hidden");
  content.hidden = true;
  const notApplicable = byId("cazatiburones-not-applicable");
  notApplicable.classList.add("hidden");
  notApplicable.replaceChildren();
}

function closeCazatiburonesDetail() {
  resetCazatiburonesDetail();
  byId("cazatiburones-universe-search").focus();
}

function selectCazatiburonesUniverseAsset(assetId) {
  if (!marketAssets[assetId]) return false;
  cazatiburonesSelectedAssetId = assetId;
  renderCazatiburonesDetailHeader();
  byId("cazatiburones-not-applicable").classList.add("hidden");
  byId("cazatiburones-not-applicable").replaceChildren();
  void loadCazatiburonesBoard(assetId);
  return true;
}

const CAZATIBURONES_UNIVERSE_FAMILIES = Object.freeze([
  Object.freeze({ key: "insider", label: "Insiders" }),
  Object.freeze({ key: "beneficial", label: "Propiedad beneficiaria" }),
  Object.freeze({ key: "institutional", label: "Institucional 13F" }),
]);

let cazatiburonesUniverseRequestSequence = 0;
let cazatiburonesUniverseSnapshot = null;
const cazatiburonesUniverseFilters = {
  search: "",
  family: "all",
  evidence: "all",
};

function normalizeCazatiburonesUniverseSearch(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase(LOCALE);
}

function cazatiburonesUniverseCapabilityMarkup(capability, reason) {
  if (capability === "supported") return createElement("span", "cazatiburones-universe-state", "Disponible");
  if (capability === "not_applicable") {
    return renderAbsenceMark("not-applicable", "No aplica", reason);
  }
  if (capability === "not_configured" || capability === "not_implemented") {
    return renderAbsenceMark("blocked", "Bloqueada", reason || "Capacidad no configurada");
  }
  return renderAbsenceMark("not-evaluable", "No evaluable", reason || "Capacidad no evaluable");
}

function cazatiburonesUniverseEvidenceMarkup(evidence, reason) {
  if (evidence === "present") return createElement("span", "cazatiburones-universe-state", "Presente");
  if (evidence === "not_queried") {
    return renderAbsenceMark("missing", "No consultada", reason || "La evidencia no fue consultada");
  }
  if (evidence === "missing") return renderAbsenceMark("missing", "Sin evidencia", reason);
  return renderAbsenceMark("not-evaluable", "No evaluable", reason || "Estado de evidencia no evaluable");
}

function cazatiburonesUniverseFieldMarkup(value, formatter, reason) {
  if (value === null || value === undefined) {
    return renderAbsenceMark("missing", "Sin evidencia", reason);
  }
  return createElement("span", "figure", formatter(value));
}

function cazatiburonesUniverseRowsFromPayload(payload) {
  const rows = [];
  for (const asset of payload.assets) {
    for (const family of CAZATIBURONES_UNIVERSE_FAMILIES) {
      const state = asset[family.key] && typeof asset[family.key] === "object"
        ? asset[family.key]
        : {};
      rows.push(Object.freeze({
        assetId: asset.asset_id,
        symbol: asset.symbol,
        name: asset.name,
        family: family.key,
        familyLabel: family.label,
        capability: state.capability,
        evidence: state.evidence,
        statements: state.statements,
        latestAvailableAt: state.latest_available_at,
        latestAgeDays: state.latest_age_days,
        notEvaluableReason: state.not_evaluable_reason,
      }));
    }
  }
  return rows;
}

function cazatiburonesUniverseRowMatches(row) {
  const search = cazatiburonesUniverseFilters.search;
  const searchable = normalizeCazatiburonesUniverseSearch(
    `${row.symbol || ""} ${row.name || ""} ${row.assetId || ""}`,
  );
  const searchMatches = !search || searchable.includes(normalizeCazatiburonesUniverseSearch(search));
  const family = cazatiburonesUniverseFilters.family;
  const familyMatches = family === "all" || cazatiburonesNotificationFamilyMatches(row, family);
  const evidenceMatches = cazatiburonesUniverseFilters.evidence === "all"
    || row.evidence === cazatiburonesUniverseFilters.evidence;
  return searchMatches && familyMatches && evidenceMatches;
}

function navigateCazatiburonesUniverseAsset(assetId) {
  selectCazatiburonesUniverseAsset(assetId);
}

function selectCazatiburonesNotificationTarget(assetId, family) {
  const normalizedAssetId = String(assetId ?? "").trim();
  const normalizedFamily = String(family ?? "").trim();
  if (!marketAssets[normalizedAssetId]) {
    return contextualNavigationUnavailable("el activo ya no está en el catálogo vigente.");
  }
  if (!["activity", "institutional"].includes(normalizedFamily)) {
    return contextualNavigationUnavailable("la familia de Cazatiburones no está disponible.");
  }
  cazatiburonesUniverseFilters.search = "";
  cazatiburonesUniverseFilters.family = normalizedFamily;
  byId("cazatiburones-universe-search").value = "";
  byId("cazatiburones-universe-family").value = normalizedFamily;
  renderCazatiburonesUniverseRows();
  return selectCazatiburonesUniverseAsset(normalizedAssetId);
}

function cazatiburonesNotificationFamilyMatches(row, family) {
  return family === "activity"
    ? ["insider", "beneficial"].includes(row.family)
    : row.family === family;
}

function renderCazatiburonesUniverseRows() {
  const tableScroll = byId("cazatiburones-universe-table-scroll");
  const empty = byId("cazatiburones-universe-empty");
  const error = byId("cazatiburones-universe-error");
  const body = byId("cazatiburones-universe-rows");
  const filterStatus = byId("cazatiburones-universe-filter-status");
  body.replaceChildren();
  tableScroll.classList.add("hidden");
  empty.classList.add("hidden");
  error.classList.add("hidden");
  if (!cazatiburonesUniverseSnapshot) {
    filterStatus.textContent = "El índice aún no tiene evidencia cargada.";
    return;
  }
  const rows = cazatiburonesUniverseSnapshot.rows.filter(cazatiburonesUniverseRowMatches);
  if (rows.length === 0) {
    empty.textContent = cazatiburonesUniverseSnapshot.rows.length === 0
      ? "El índice no devolvió activos para el corte seleccionado."
      : "Ninguna fila coincide con los filtros locales.";
    empty.classList.remove("hidden");
    filterStatus.textContent = cazatiburonesUniverseSnapshot.rows.length === 0
      ? "El índice está vacío al corte seleccionado."
      : "Los filtros se aplican sólo sobre la evidencia cargada; no se hizo una nueva consulta.";
    return;
  }
  for (const row of rows) {
    const tableRow = document.createElement("tr");
    const assetCell = document.createElement("th");
    assetCell.scope = "row";
    const assetButton = createElement(
      "button",
      "cazatiburones-universe-asset-button",
      `${row.symbol || row.assetId} · ${row.name || "Activo sin nombre"}`,
    );
    assetButton.type = "button";
    assetButton.setAttribute(
      "aria-label",
      `Seleccionar activo ${row.symbol || row.assetId} · ${row.name || "Activo sin nombre"}`,
    );
    assetButton.addEventListener("click", () => navigateCazatiburonesUniverseAsset(row.assetId));
    assetCell.append(assetButton);
    tableRow.append(assetCell);
    tableRow.append(createElement("td", "cazatiburones-universe-family-cell", row.familyLabel));
    const capabilityCell = document.createElement("td");
    capabilityCell.append(cazatiburonesUniverseCapabilityMarkup(row.capability, row.notEvaluableReason));
    tableRow.append(capabilityCell);
    const evidenceCell = document.createElement("td");
    evidenceCell.append(cazatiburonesUniverseEvidenceMarkup(row.evidence, row.notEvaluableReason));
    tableRow.append(evidenceCell);
    const statementsCell = document.createElement("td");
    statementsCell.append(
      cazatiburonesUniverseFieldMarkup(
        row.statements,
        (value) => formatInteger(value),
        "El contrato no suministró declaraciones",
      ),
    );
    tableRow.append(statementsCell);
    const latestCell = document.createElement("td");
    latestCell.append(
      row.latestAvailableAt === null || row.latestAvailableAt === undefined
        ? renderAbsenceMark("missing", "Sin evidencia", "Sin fecha disponible en el contrato")
        : createElement("span", "figure", formatInstant(row.latestAvailableAt)),
    );
    tableRow.append(latestCell);
    const ageCell = document.createElement("td");
    ageCell.append(
      row.latestAgeDays === null || row.latestAgeDays === undefined
        ? renderAbsenceMark("missing", "Sin evidencia", "Sin antigüedad disponible en el contrato")
        : createElement("span", "figure", `${formatInteger(row.latestAgeDays)} días`),
    );
    tableRow.append(ageCell);
    body.append(tableRow);
  }
  tableScroll.classList.remove("hidden");
  filterStatus.textContent = "Índice cargado; los filtros son locales y conservan el orden recibido.";
}

function renderCazatiburonesUniverseLoading() {
  const panel = byId("cazatiburones-universe-index");
  panel.setAttribute("aria-busy", "true");
  byId("cazatiburones-universe-summary").textContent = "Consultando índice al corte vigente…";
  byId("cazatiburones-universe-loading").classList.remove("hidden");
  byId("cazatiburones-universe-empty").classList.add("hidden");
  byId("cazatiburones-universe-error").classList.add("hidden");
  byId("cazatiburones-universe-table-scroll").classList.add("hidden");
  byId("cazatiburones-universe-filter-status").textContent = "Esperando la evidencia del corte global vigente.";
}

function renderCazatiburonesUniverseError(message) {
  const panel = byId("cazatiburones-universe-index");
  panel.setAttribute("aria-busy", "false");
  byId("cazatiburones-universe-loading").classList.add("hidden");
  byId("cazatiburones-universe-empty").classList.add("hidden");
  byId("cazatiburones-universe-table-scroll").classList.add("hidden");
  const error = byId("cazatiburones-universe-error");
  error.textContent = message;
  error.classList.remove("hidden");
  byId("cazatiburones-universe-filter-status").textContent = "El índice no está disponible; las lecturas detalladas conservan su propio estado.";
}

function renderCazatiburonesUniverseSnapshot(snapshot) {
  const panel = byId("cazatiburones-universe-index");
  panel.setAttribute("aria-busy", "false");
  byId("cazatiburones-universe-loading").classList.add("hidden");
  byId("cazatiburones-universe-error").classList.add("hidden");
  byId("cazatiburones-universe-summary").textContent =
    `Índice disponible al corte ${formatInstant(snapshot.knownAt)} · familias separadas`;
  renderCazatiburonesUniverseRows();
}

function initializeCazatiburonesUniverseFilters() {
  byId("cazatiburones-universe-search").addEventListener("input", (event) => {
    cazatiburonesUniverseFilters.search = event.target.value;
    renderCazatiburonesUniverseRows();
  });
  byId("cazatiburones-universe-family").addEventListener("change", (event) => {
    cazatiburonesUniverseFilters.family = event.target.value;
    renderCazatiburonesUniverseRows();
  });
  byId("cazatiburones-universe-evidence").addEventListener("change", (event) => {
    cazatiburonesUniverseFilters.evidence = event.target.value;
    renderCazatiburonesUniverseRows();
  });
  byId("cazatiburones-universe-clear-filters").addEventListener("click", () => {
    cazatiburonesUniverseFilters.search = "";
    cazatiburonesUniverseFilters.family = "all";
    cazatiburonesUniverseFilters.evidence = "all";
    byId("cazatiburones-universe-search").value = "";
    byId("cazatiburones-universe-family").value = "all";
    byId("cazatiburones-universe-evidence").value = "all";
    renderCazatiburonesUniverseRows();
    byId("cazatiburones-universe-search").focus();
  });
  byId("cazatiburones-open-asset").addEventListener("click", () => {
    if (!cazatiburonesSelectedAssetId) {
      setMessage("Selecciona un activo local antes de abrir Activo.", true);
      return;
    }
    void navigateToContext({
      board: "activo", assetId: cazatiburonesSelectedAssetId, subtab: "mercado",
    });
  });
  byId("cazatiburones-detail-close").addEventListener("click", closeCazatiburonesDetail);
}

async function loadCazatiburonesUniverseIndex() {
  const sequence = ++cazatiburonesUniverseRequestSequence;
  const knownAt = byId("report-known-at").value.trim();
  cazatiburonesUniverseSnapshot = null;
  renderCazatiburonesUniverseLoading();
  if (!knownAt) {
    if (sequence !== cazatiburonesUniverseRequestSequence) return;
    renderCazatiburonesUniverseError("El corte global no está disponible para consultar el índice.");
    return;
  }
  const parameters = new URLSearchParams({ known_at: knownAt });
  try {
    const payload = await api(`/api/v1/cazatiburones/universe-activity?${parameters.toString()}`);
    if (
      sequence !== cazatiburonesUniverseRequestSequence
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    if (
      payload.schema_version !== "cazatiburones-universe-activity-v1"
      || !Array.isArray(payload.assets)
    ) throw new Error("El índice universe-wide no tiene un contrato compatible.");
    cazatiburonesUniverseSnapshot = Object.freeze({
      knownAt,
      rows: cazatiburonesUniverseRowsFromPayload(payload),
    });
    renderCazatiburonesUniverseSnapshot(cazatiburonesUniverseSnapshot);
  } catch (error) {
    if (
      sequence !== cazatiburonesUniverseRequestSequence
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    cazatiburonesUniverseSnapshot = null;
    renderCazatiburonesUniverseError(error.message || "No fue posible consultar el índice universe-wide.");
  }
}

// Every optional descriptive field that can be legitimately absent from a
// declared-activity or document-timeline record renders through the
// declared absence grammar -- never a bare "0", "—" or empty cell.
function cazatiburonesFieldOrAbsence(value, formatter = String) {
  return value === null || value === undefined
    ? renderAbsenceMark("missing", "Sin evidencia").outerHTML
    : formatter(value);
}

function cazatiburonesMetricMarkup(metric) {
  if (metric.status === "available") {
    return `<span class="figure">${formatNumber(metric.value)}</span>`;
  }
  return renderAbsenceMark(
    metric.status === "missing" ? "missing" : "not-evaluable",
    metric.status === "missing" ? "Sin evidencia" : "No evaluable",
  ).outerHTML;
}

function cazatiburonesComparisonMarkup(status) {
  if (status === "not_evaluable") {
    return renderAbsenceMark(
      "not-evaluable",
      "No evaluable",
      "Comparación no evaluable con la cobertura común disponible",
    ).outerHTML;
  }
  return `<span>${status === "discontinuous" ? "Discontinua" : "Disponible"}</span>`;
}

function renderCazatiburonesFeatureGroup(containerId, label, features) {
  const container = byId(containerId);
  container.replaceChildren();
  container.append(createElement("strong", "cazatiburones-feature-group-title", label));
  if (features.length === 0) {
    container.append(
      renderAbsenceMark("missing", "Sin evidencia", `Sin ${label.toLowerCase()} declarados`),
    );
    return;
  }
  for (const feature of features) {
    const row = createElement("div", "cazatiburones-row");
    const metricsMarkup = feature.metrics
      .map((metric) => `<dt>${metric.key}</dt><dd>${cazatiburonesMetricMarkup(metric)}</dd>`)
      .join("");
    row.innerHTML =
      `<p class="eyebrow">${feature.form} · ${feature.participant_cik}</p>` +
      `<dl>` +
      `<dt>Naturaleza declarada</dt><dd>${cazatiburonesFieldOrAbsence(feature.declared_nature)}</dd>` +
      `<dt>Título del valor</dt><dd>${cazatiburonesFieldOrAbsence(feature.security_title)}</dd>` +
      `<dt>Tabla</dt><dd>${cazatiburonesFieldOrAbsence(feature.table)}</dd>` +
      `<dt>Fecha del evento</dt><dd>${cazatiburonesFieldOrAbsence(feature.event_date, formatCalendarDate)}</dd>` +
      `<dt>Disponible desde</dt><dd>${formatInstant(feature.available_at)}</dd>` +
      `<dt>Comparación</dt><dd>${cazatiburonesComparisonMarkup(feature.comparison_status)}</dd>` +
      `${metricsMarkup}` +
      `</dl>`;
    container.append(row);
  }
}

function renderCazatiburonesDeclaredActivity(payload) {
  renderCazatiburonesFeatureGroup(
    "cazatiburones-insider-features",
    "Actividad de insiders declarada",
    payload.insider_features,
  );
  renderCazatiburonesFeatureGroup(
    "cazatiburones-beneficial-features",
    "Propiedad beneficiaria declarada",
    payload.beneficial_features,
  );
  byId("cazatiburones-declared-activity-summary").innerHTML =
    `<span class="figure">${formatInteger(payload.total_statements)}</span> declaraciones` +
    (payload.truncated ? " · resultado truncado" : "");
}

function renderCazatiburonesInstitutionalObservations(payload, offset, limit) {
  const container = byId("cazatiburones-institutional-observations-rows");
  container.replaceChildren();
  if (payload.observations.length === 0) {
    container.append(
      renderAbsenceMark("missing", "Sin evidencia", "Sin observaciones institucionales 13F"),
    );
  } else {
    for (const view of payload.observations) {
      const row = createElement("div", "cazatiburones-row");
      row.innerHTML =
        `<p class="eyebrow">${view.report.manager_cik} · ${view.report.report_id}</p>` +
        `<dl>` +
        `<dt>CUSIP</dt><dd>${view.row.cusip}</dd>` +
        `<dt>Campo</dt><dd>${view.observation.field_name}</dd>` +
        `<dt>Valor as-filed</dt><dd><span class="figure">${formatNumber(view.observation.value)}</span></dd>` +
        `<dt>Disponible desde</dt><dd>${formatInstant(view.observation.available_at)}</dd>` +
        `</dl>`;
      container.append(row);
    }
  }
  byId("cazatiburones-institutional-observations-summary").innerHTML =
    `<span class="figure">${formatInteger(payload.observations.length)}</span> de ` +
    `<span class="figure">${formatInteger(payload.total_matching)}</span> filas as-filed ` +
    `· página offset <span class="figure">${formatInteger(offset)}</span> ` +
    `límite <span class="figure">${formatInteger(limit)}</span>` +
    (payload.truncated ? " · truncado" : "");
}

function renderCazatiburonesDocumentTimeline(payload) {
  const assetContainer = byId("cazatiburones-timeline-asset-document");
  const filerContainer = byId("cazatiburones-timeline-filer-document");
  assetContainer.replaceChildren();
  filerContainer.replaceChildren();
  assetContainer.append(
    createElement("strong", "cazatiburones-feature-group-title", "Documentos del activo"),
  );
  filerContainer.append(
    createElement("strong", "cazatiburones-feature-group-title", "Documentos del emisor"),
  );
  const entriesByFamily = { asset_document: [], filer_document: [] };
  if (payload.state !== "missing") {
    for (const entry of payload.entries) entriesByFamily[entry.family].push(entry);
  }
  for (const [family, container] of [
    ["asset_document", assetContainer],
    ["filer_document", filerContainer],
  ]) {
    const entries = entriesByFamily[family];
    if (entries.length === 0) {
      container.append(
        renderAbsenceMark("missing", "Sin evidencia", "Sin revisiones documentales SEC"),
      );
      continue;
    }
    for (const entry of entries) {
      const row = createElement("div", "cazatiburones-row");
      row.innerHTML =
        `<p class="eyebrow">${entry.form} · ${entry.accession}</p>` +
        `<dl>` +
        `<dt>Fecha de presentación</dt><dd>${formatCalendarDate(entry.filing_date)}</dd>` +
        `<dt>Fecha del reporte</dt><dd>${cazatiburonesFieldOrAbsence(entry.report_date, formatCalendarDate)}</dd>` +
        `<dt>Aceptado</dt><dd>${formatInstant(entry.accepted_at)}</dd>` +
        `<dt>Disponible desde</dt><dd>${formatInstant(entry.available_at)}</dd>` +
        `<dt>Enmienda</dt><dd>${entry.is_amendment ? "Sí" : "No"}</dd>` +
        `<dt>SHA-256</dt><dd>${entry.content_sha256}</dd>` +
        `<dt>Fuente</dt><dd>${entry.source_url}</dd>` +
        `</dl>`;
      container.append(row);
    }
  }
  // The four coverage counters (matched_count, returned_count,
  // legacy_records_excluded, truncated) are always present in the contract,
  // including under state: "missing" -- they must stay visible there too,
  // never hidden behind the absence phrasing.
  byId("cazatiburones-document-timeline-summary").innerHTML =
    (payload.state === "missing" ? "Sin documentos SEC para el activo y corte seleccionados · " : "") +
    `<span class="figure">${formatInteger(payload.returned_count)}</span> de ` +
    `<span class="figure">${formatInteger(payload.matched_count)}</span> revisiones` +
    ` · <span class="figure">${formatInteger(payload.legacy_records_excluded)}</span> legado excluido` +
    (payload.truncated ? " · truncado" : "");
}

async function loadCazatiburonesBoard(assetId = cazatiburonesSelectedAssetId) {
  const sequence = ++cazatiburonesRequestSequence;
  if (!assetId || assetId !== cazatiburonesSelectedAssetId) return;
  const knownAt = byId("report-known-at").value.trim();
  const notApplicable = byId("cazatiburones-not-applicable");
  const content = byId("cazatiburones-detail-content");
  if (!knownAt) {
    content.classList.add("hidden");
    content.hidden = true;
    notApplicable.replaceChildren(
      renderAbsenceMark("missing", "Sin evidencia", "El corte global no está disponible"),
    );
    notApplicable.classList.remove("hidden");
    return;
  }
  if (!cazatiburonesSelectedPresentation()) {
    content.classList.add("hidden");
    content.hidden = true;
    notApplicable.replaceChildren(
      renderAbsenceMark(
        "not-applicable",
        "No aplica",
        "El activo local no tiene presentación SEC corporativa habilitada",
      ),
    );
    notApplicable.classList.remove("hidden");
    return;
  }
  notApplicable.classList.add("hidden");
  notApplicable.replaceChildren();
  content.classList.remove("hidden");
  content.hidden = false;
  byId("cazatiburones-declared-activity-summary").textContent = "Consultando…";
  byId("cazatiburones-institutional-observations-summary").textContent = "Consultando…";
  byId("cazatiburones-document-timeline-summary").textContent = "Consultando…";
  const offset = 0;
  const limit = 200;
  const featureParameters = new URLSearchParams({ asset_id: assetId, known_at: knownAt });
  const observationParameters = new URLSearchParams({
    asset_id: assetId,
    known_at: knownAt,
    offset: String(offset),
    limit: String(limit),
  });
  const timelineParameters = new URLSearchParams({ known_at: knownAt });
  timelineParameters.append("asset_id", assetId);
  try {
    const [declaredActivity, institutionalObservations, documentTimeline] = await Promise.all([
      api(`/api/v1/cazatiburones/declared-activity?${featureParameters.toString()}`),
      api(`/api/v1/cazatiburones/institutional-observations?${observationParameters.toString()}`),
      api(`/api/v1/sec-document-timeline?${timelineParameters.toString()}`),
    ]);
    if (
      sequence !== cazatiburonesRequestSequence
      || assetId !== cazatiburonesSelectedAssetId
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    renderCazatiburonesDeclaredActivity(declaredActivity);
    renderCazatiburonesInstitutionalObservations(institutionalObservations, offset, limit);
    renderCazatiburonesDocumentTimeline(documentTimeline);
  } catch (error) {
    if (
      sequence !== cazatiburonesRequestSequence
      || assetId !== cazatiburonesSelectedAssetId
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    byId("cazatiburones-declared-activity-summary").textContent = error.message;
    byId("cazatiburones-institutional-observations-summary").textContent = error.message;
    byId("cazatiburones-document-timeline-summary").textContent = error.message;
  }
}
