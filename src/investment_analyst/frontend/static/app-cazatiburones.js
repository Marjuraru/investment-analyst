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
let cazatiburonesSelectedNotificationContext = null;
let cazatiburonesLastObservationsPayload = null;
const cazatiburonesProgressiveVisibleCounts = new Map();

const CAZATIBURONES_FIELD_LABELS = Object.freeze({
  value_thousands_usd: "Valor reportado (miles USD)",
  shares_or_principal_amount: "Acciones o monto principal",
  shares_or_principal_type: "Tipo de posición",
  put_call: "Opción Put/Call",
  investment_discretion: "Discreción de inversión",
});

const CAZATIBURONES_UNIT_LABELS = Object.freeze({
  thousands_usd: "miles USD",
  shares: "acciones",
  usd: "USD",
});

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

function clearCazatiburonesSelectedContext({ hidePanel = true } = {}) {
  cazatiburonesSelectedNotificationContext = null;
  if (hidePanel) {
    const panel = byId("cazatiburones-selected-context");
    if (panel) {
      panel.classList.add("hidden");
      panel.hidden = true;
    }
  }
}

function resetCazatiburonesDetail() {
  cazatiburonesRequestSequence += 1;
  cazatiburonesSelectedAssetId = null;
  cazatiburonesLastObservationsPayload = null;
  clearCazatiburonesSelectedContext();
  cazatiburonesProgressiveVisibleCounts.clear();
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
  if (
    cazatiburonesSelectedNotificationContext
    && cazatiburonesSelectedNotificationContext.asset_id !== assetId
  ) {
    clearCazatiburonesSelectedContext();
  }
  cazatiburonesSelectedAssetId = assetId;
  cazatiburonesLastObservationsPayload = null;
  cazatiburonesProgressiveVisibleCounts.clear();
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
let cazatiburonesUniverseVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
let cazatiburonesUniversePendingContextAssetId = null;
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
    const mark = renderAbsenceMark("not-applicable", "No aplica", reason);
    return typeof wrapValueAbsenceMark === "function"
      ? wrapValueAbsenceMark(mark, "not-applicable", reason)
      : mark;
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

function selectCazatiburonesNotificationTarget(assetId, family, context = null) {
  const normalizedAssetId = String(assetId ?? "").trim();
  const normalizedFamily = String(family ?? "").trim();
  if (!marketAssets[normalizedAssetId]) {
    return contextualNavigationUnavailable("el activo ya no está en el catálogo vigente.");
  }
  if (!["activity", "institutional"].includes(normalizedFamily)) {
    return contextualNavigationUnavailable("la familia de Cazatiburones no está disponible.");
  }
  cazatiburonesSelectedNotificationContext = context && typeof context === "object"
    ? { ...context, family: normalizedFamily, asset_id: normalizedAssetId }
    : { family: normalizedFamily, asset_id: normalizedAssetId };
  cazatiburonesUniverseFilters.search = "";
  cazatiburonesUniverseFilters.family = normalizedFamily;
  cazatiburonesUniversePendingContextAssetId = normalizedAssetId;
  byId("cazatiburones-universe-search").value = "";
  byId("cazatiburones-universe-family").value = normalizedFamily;
  renderCazatiburonesUniverseRows();
  return selectCazatiburonesUniverseAsset(normalizedAssetId);
}

function resetCazatiburonesUniverseWindow({ preservePendingContext = false } = {}) {
  cazatiburonesUniverseVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  if (!preservePendingContext) cazatiburonesUniversePendingContextAssetId = null;
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
  const collectionStatus = byId("cazatiburones-universe-collection-status");
  body.replaceChildren();
  tableScroll.classList.add("hidden");
  empty.classList.add("hidden");
  error.classList.add("hidden");
  collectionStatus.classList.remove("hidden");
  if (!cazatiburonesUniverseSnapshot) {
    filterStatus.textContent = "El índice aún no tiene evidencia cargada.";
    renderProgressiveCollectionControls(
      collectionStatus,
      byId("cazatiburones-universe-collection-controls"),
      0,
      0,
      { label: "filas" },
    );
    return;
  }
  const rows = cazatiburonesUniverseSnapshot.rows.filter(cazatiburonesUniverseRowMatches);
  const pendingAssetId = cazatiburonesUniversePendingContextAssetId;
  const selectedIndex = pendingAssetId
    ? rows.findIndex((row) => row.assetId === pendingAssetId)
    : -1;
  const visibleCount = progressiveCollectionVisibleCount(
    rows.length,
    cazatiburonesUniverseVisibleCount,
    selectedIndex,
  );
  cazatiburonesUniverseVisibleCount = visibleCount;
  cazatiburonesUniversePendingContextAssetId = null;
  if (rows.length === 0) {
    empty.textContent = cazatiburonesUniverseSnapshot.rows.length === 0
      ? "El índice no devolvió activos para el corte seleccionado."
      : "Ninguna fila coincide con los filtros locales.";
    empty.classList.remove("hidden");
    filterStatus.textContent = cazatiburonesUniverseSnapshot.rows.length === 0
      ? "El índice está vacío al corte seleccionado."
      : "Los filtros se aplican sólo sobre la evidencia cargada; no se hizo una nueva consulta.";
    renderProgressiveCollectionControls(
      byId("cazatiburones-universe-collection-status"),
      byId("cazatiburones-universe-collection-controls"),
      0,
      0,
      { label: "filas" },
    );
    return;
  }
  for (const row of rows.slice(0, visibleCount)) {
    const assetLabel = assetDisplayLabel(row.assetId);
    const tableRow = document.createElement("tr");
    const assetCell = document.createElement("th");
    assetCell.scope = "row";
    const assetButton = createElement(
      "button",
      "cazatiburones-universe-asset-button",
      assetLabel,
    );
    assetButton.type = "button";
    assetButton.setAttribute(
      "aria-label",
      `Seleccionar activo ${assetLabel}`,
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
  renderProgressiveCollectionControls(
    byId("cazatiburones-universe-collection-status"),
    byId("cazatiburones-universe-collection-controls"),
    rows.length,
    visibleCount,
    {
      label: "filas",
      onMore: () => {
        cazatiburonesUniverseVisibleCount += PROGRESSIVE_COLLECTION_PAGE_SIZE;
        renderCazatiburonesUniverseRows();
      },
      onLess: () => {
        resetCazatiburonesUniverseWindow();
        renderCazatiburonesUniverseRows();
      },
    },
  );
}

function renderCazatiburonesUniverseLoading() {
  const panel = byId("cazatiburones-universe-index");
  panel.setAttribute("aria-busy", "true");
  byId("cazatiburones-universe-summary").textContent = "Consultando índice al corte vigente…";
  byId("cazatiburones-universe-loading").classList.remove("hidden");
  byId("cazatiburones-universe-empty").classList.add("hidden");
  byId("cazatiburones-universe-error").classList.add("hidden");
  byId("cazatiburones-universe-table-scroll").classList.add("hidden");
  const collectionStatus = byId("cazatiburones-universe-collection-status");
  collectionStatus.textContent = "Consultando el índice…";
  collectionStatus.classList.add("hidden");
  byId("cazatiburones-universe-collection-controls").replaceChildren();
  byId("cazatiburones-universe-filter-status").textContent = "Esperando la evidencia del corte global vigente.";
}

function renderCazatiburonesUniverseError(message) {
  const panel = byId("cazatiburones-universe-index");
  panel.setAttribute("aria-busy", "false");
  byId("cazatiburones-universe-loading").classList.add("hidden");
  byId("cazatiburones-universe-empty").classList.add("hidden");
  byId("cazatiburones-universe-table-scroll").classList.add("hidden");
  const collectionStatus = byId("cazatiburones-universe-collection-status");
  collectionStatus.textContent = "Índice no disponible.";
  collectionStatus.classList.add("hidden");
  byId("cazatiburones-universe-collection-controls").replaceChildren();
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
  resetCazatiburonesUniverseWindow({ preservePendingContext: true });
  renderCazatiburonesUniverseRows();
}

function initializeCazatiburonesUniverseFilters() {
  byId("cazatiburones-universe-search").addEventListener("input", (event) => {
    cazatiburonesUniverseFilters.search = event.target.value;
    resetCazatiburonesUniverseWindow();
    renderCazatiburonesUniverseRows();
  });
  byId("cazatiburones-universe-family").addEventListener("change", (event) => {
    cazatiburonesUniverseFilters.family = event.target.value;
    resetCazatiburonesUniverseWindow();
    renderCazatiburonesUniverseRows();
  });
  byId("cazatiburones-universe-evidence").addEventListener("change", (event) => {
    cazatiburonesUniverseFilters.evidence = event.target.value;
    resetCazatiburonesUniverseWindow();
    renderCazatiburonesUniverseRows();
  });
  byId("cazatiburones-universe-clear-filters").addEventListener("click", () => {
    cazatiburonesUniverseFilters.search = "";
    cazatiburonesUniverseFilters.family = "all";
    cazatiburonesUniverseFilters.evidence = "all";
    byId("cazatiburones-universe-search").value = "";
    byId("cazatiburones-universe-family").value = "all";
    byId("cazatiburones-universe-evidence").value = "all";
    resetCazatiburonesUniverseWindow();
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
  byId("cazatiburones-jump-evidence")?.addEventListener("click", () => {
    const disclosure = byId("cazatiburones-evidence-disclosure");
    if (disclosure) {
      disclosure.open = true;
      disclosure.scrollIntoView?.({ block: "start" });
      disclosure.querySelector?.("summary")?.focus?.();
    }
  });
  byId("cazatiburones-context-clear")?.addEventListener("click", () => {
    clearCazatiburonesSelectedContext();
    if (cazatiburonesLastObservationsPayload) {
      renderCazatiburonesInstitutionalObservations(cazatiburonesLastObservationsPayload, 0, 200);
    }
  });
  byId("cazatiburones-load-full-btn")?.addEventListener("click", () => {
    void loadCazatiburonesFullObservations();
  });
}

async function loadCazatiburonesUniverseIndex() {
  const sequence = ++cazatiburonesUniverseRequestSequence;
  const knownAt = byId("report-known-at").value.trim();
  cazatiburonesUniverseSnapshot = null;
  resetCazatiburonesUniverseWindow();
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
    const text = typeof formatDecimalRatio === "function"
      ? formatDecimalRatio(metric.value, 4, { trimTrailingZeros: true })
      : formatNumber(metric.value);
    const exactTitle = metric.value !== null && metric.value !== undefined ? ` title="Valor exacto: ${metric.value}"` : "";
    return `<span class="figure"${exactTitle}>${text}</span>`;
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

function cazatiburonesEscapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function cazatiburonesMetricLabel(key) {
  if (typeof CAZATIBURONES_NOTIFICATION_METRIC_LABELS === "object" && CAZATIBURONES_NOTIFICATION_METRIC_LABELS[key]) {
    return CAZATIBURONES_NOTIFICATION_METRIC_LABELS[key];
  }
  return key || "Métrica declarada";
}

function cazatiburonesObservationMatchesContext(view, ctx) {
  if (!view || !ctx || ctx.asset_id !== cazatiburonesSelectedAssetId) return false;
  const obsId = view.observation?.observation_id;
  if (obsId && Array.isArray(ctx.input_observation_ids) && ctx.input_observation_ids.includes(obsId)) {
    return "exact_id";
  }
  if (ctx.family === "institutional" && ctx.manager_cik && view.report?.manager_cik === ctx.manager_cik) {
    const ctxPeriod = ctx.report_period ? String(ctx.report_period).slice(0, 10) : "";
    const rowPeriod = String(
      view.report?.report_period || view.report?.report_of_date || view.observation?.period_end || "",
    ).slice(0, 10);
    if (!ctxPeriod || !rowPeriod || rowPeriod === ctxPeriod) {
      return "manager_period";
    }
  }
  return false;
}

function renderCazatiburonesSelectedContext(observations = []) {
  const panel = byId("cazatiburones-selected-context");
  const titleEl = byId("cazatiburones-context-titulo");
  const summaryEl = byId("cazatiburones-context-summary");
  const bodyEl = byId("cazatiburones-context-body");
  if (!panel || !bodyEl) return;
  const ctx = cazatiburonesSelectedNotificationContext;
  if (!ctx || ctx.asset_id !== cazatiburonesSelectedAssetId) {
    panel.classList.add("hidden");
    panel.hidden = true;
    return;
  }
  panel.classList.remove("hidden");
  panel.hidden = false;
  const described = typeof describeCazatiburonesNotice === "function"
    ? describeCazatiburonesNotice(ctx, ctx.family)
    : {
      familyLabel: ctx.family === "institutional" ? "Institucional 13F" : "Actividad declarada",
      title: ctx.rule_id || "Declaración seleccionada",
      label: ctx.rule_id || "Declaración SEC",
      formattedValue: ctx.value ?? "Sin dato",
      actor: ctx.manager_cik ? `Gestor 13F CIK ${ctx.manager_cik}` : "Participante SEC",
      period: ctx.report_period || "",
      assetLabel: assetDisplayLabel(ctx.asset_id),
    };
  if (titleEl) titleEl.textContent = described.title || "Declaración seleccionada";
  const knownAtText = ctx.available_at || ctx.created_at
    ? formatInstant(ctx.available_at || ctx.created_at)
    : "Sin fecha";
  if (summaryEl) {
    summaryEl.textContent = `${described.familyLabel} · conocido ${knownAtText}`;
  }

  let exactMatches = 0;
  let heuristicMatches = 0;
  for (const view of observations) {
    const matchKind = cazatiburonesObservationMatchesContext(view, ctx);
    if (matchKind === "exact_id") exactMatches += 1;
    else if (matchKind === "manager_period") heuristicMatches += 1;
  }

  let matchNote = "";
  if (ctx.family === "institutional") {
    if (exactMatches > 0) {
      matchNote = `Coincidencia exacta localizada y resaltada (${formatInteger(exactMatches)} fila${exactMatches === 1 ? "" : "s"}).`;
    } else if (heuristicMatches > 0) {
      matchNote = `Se resaltan ${formatInteger(heuristicMatches)} observaciones del mismo gestor/período en la ventana actual; las notificaciones 13F resumen cambios entre períodos.`;
    } else if (observations.length > 0) {
      matchNote = `La ventana resumen actual (${formatInteger(observations.length)} filas) no aísla exactamente la observación de esta notificación; puedes pulsar "Cargar detalle completo 13F" o revisar la actividad abajo.`;
    } else {
      matchNote = "Sin filas 13F cargadas para cruzar directamente en este corte.";
    }
  } else {
    matchNote = "Selección enfocada sobre actividad declarada del activo; revisa los registros de insiders/propiedad beneficiaria y la evidencia documental.";
  }

  const dl = document.createElement("dl");
  const appendPair = (term, val) => {
    dl.append(createElement("dt", "", term), createElement("dd", "", val));
  };
  appendPair("Quién declaró", described.actor);
  appendPair("Qué declaró", described.label);
  appendPair("Valor o cambio", described.formattedValue || "Ver detalle abajo");
  if (described.period) appendPair("Período / evento", described.period);
  appendPair("Cuándo se conoció", knownAtText);

  const noteEl = createElement("p", "collection-status", matchNote);
  const techDetails = document.createElement("details");
  techDetails.className = "cazatiburones-tech-details";
  techDetails.append(createElement("summary", "", "Campos técnicos de la notificación"));
  const techDl = document.createElement("dl");
  const appendTech = (term, val) => {
    techDl.append(createElement("dt", "", term), createElement("dd", "", String(val ?? "—")));
  };
  appendTech("notification_id", ctx.notification_id || "—");
  appendTech("rule_id", ctx.rule_id || "—");
  appendTech("metric_key", ctx.metric_key || "—");
  appendTech("cusip", ctx.cusip || "—");
  appendTech(
    "input_observation_ids",
    Array.isArray(ctx.input_observation_ids) && ctx.input_observation_ids.length
      ? ctx.input_observation_ids.join(", ")
      : "—",
  );
  techDetails.append(techDl);

  bodyEl.replaceChildren(dl, noteEl, techDetails);
}

function renderCazatiburonesFeatureGroup(containerId, label, features) {
  const container = byId(containerId);
  const collection = Array.isArray(features) ? features : [];
  const render = () => {
    container.replaceChildren();
    container.append(createElement("strong", "cazatiburones-feature-group-title", label));
    if (collection.length === 0) {
      container.append(
        renderAbsenceMark(
          "missing",
          "No hay declaraciones elegibles en este corte",
          `Sin ${label.toLowerCase()} elegibles en este corte`,
        ),
      );
      return;
    }
    const visibleCount = progressiveCollectionVisibleCount(
      collection.length,
      cazatiburonesProgressiveVisibleCounts.get(containerId),
    );
    cazatiburonesProgressiveVisibleCounts.set(containerId, visibleCount);
    for (const feature of collection.slice(0, visibleCount)) {
      const row = createElement("div", "cazatiburones-row");
      const actorTitle = feature.participant_name
        ? `${feature.participant_name} (CIK ${feature.participant_cik})`
        : `Participante SEC CIK ${feature.participant_cik}`;
      const metricsMarkup = (Array.isArray(feature.metrics) ? feature.metrics : [])
        .map((metric) => {
          const metricLabel = cazatiburonesEscapeHtml(cazatiburonesMetricLabel(metric.key));
          return `<dt title="${cazatiburonesEscapeHtml(metric.key)}">${metricLabel}</dt><dd>${cazatiburonesMetricMarkup(metric)}</dd>`;
        })
        .join("");
      const eventDateText = cazatiburonesFieldOrAbsence(feature.event_date, formatCalendarDate);
      const declaredNature = cazatiburonesFieldOrAbsence(feature.declared_nature);
      const securityTitle = cazatiburonesFieldOrAbsence(feature.security_title);
      const tableText = cazatiburonesFieldOrAbsence(feature.table);
      row.innerHTML =
        `<p class="eyebrow">${cazatiburonesEscapeHtml(actorTitle)} · ${cazatiburonesEscapeHtml(feature.form)}</p>` +
        `<dl>` +
        `<dt>Naturaleza declarada</dt><dd>${declaredNature}</dd>` +
        `<dt>Valor declarado</dt><dd>${securityTitle}</dd>` +
        `<dt>Fecha del evento</dt><dd>${eventDateText}</dd>` +
        `<dt>Conocido desde</dt><dd>${cazatiburonesEscapeHtml(formatInstant(feature.available_at))}</dd>` +
        `<dt>Comparación</dt><dd>${cazatiburonesComparisonMarkup(feature.comparison_status)}</dd>` +
        `${metricsMarkup}` +
        `</dl>` +
        `<details class="cazatiburones-tech-details"><summary>Campos técnicos</summary>` +
        `<dl><dt>Tabla</dt><dd>${tableText}</dd><dt>CIK</dt><dd>${cazatiburonesEscapeHtml(feature.participant_cik)}</dd></dl>` +
        `</details>`;
      container.append(row);
    }
    const status = createElement("p", "collection-status", "");
    const controls = createElement("div", "collection-controls");
    container.append(status, controls);
    renderProgressiveCollectionControls(
      status,
      controls,
      collection.length,
      visibleCount,
      {
        label: "registros",
        onMore: () => {
          cazatiburonesProgressiveVisibleCounts.set(
            containerId,
            visibleCount + PROGRESSIVE_COLLECTION_PAGE_SIZE,
          );
          render();
        },
        onLess: () => {
          cazatiburonesProgressiveVisibleCounts.set(
            containerId,
            PROGRESSIVE_COLLECTION_PAGE_SIZE,
          );
          render();
        },
      },
    );
  };
  render();
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

function renderCazatiburonesInstitutionalObservations(payload, offset, limit, { isFull = false } = {}) {
  const container = byId("cazatiburones-institutional-observations-rows");
  const fullBtn = byId("cazatiburones-load-full-btn");
  const observations = Array.isArray(payload.observations) ? payload.observations : [];
  cazatiburonesLastObservationsPayload = payload;
  if (fullBtn) {
    fullBtn.classList.toggle("hidden", observations.length === 0 && !Number(payload.total_matching));
    fullBtn.disabled = Boolean(isFull);
    fullBtn.textContent = isFull ? "Detalle completo 13F cargado" : "Cargar detalle completo 13F";
  }
  const render = () => {
    container.replaceChildren();
    if (observations.length === 0) {
      container.append(
        renderAbsenceMark(
          "missing",
          "No hay declaraciones elegibles en este corte",
          "Sin observaciones institucionales 13F elegibles en este corte",
        ),
      );
    } else {
      let visibleCount = progressiveCollectionVisibleCount(
        observations.length,
        cazatiburonesProgressiveVisibleCounts.get("institutional-observations"),
      );
      const firstMatchIdx = observations.findIndex((view) =>
        Boolean(cazatiburonesObservationMatchesContext(view, cazatiburonesSelectedNotificationContext)),
      );
      if (firstMatchIdx >= visibleCount) {
        visibleCount = Math.min(observations.length, firstMatchIdx + PROGRESSIVE_COLLECTION_PAGE_SIZE);
      }
      cazatiburonesProgressiveVisibleCounts.set("institutional-observations", visibleCount);
      for (const view of observations.slice(0, visibleCount)) {
        const isMatch = Boolean(
          cazatiburonesObservationMatchesContext(view, cazatiburonesSelectedNotificationContext),
        );
        const row = createElement("div", `cazatiburones-row${isMatch ? " is-target-match" : ""}`);
        const compatibilityNotes = Array.isArray(view.compatibility_notes)
          ? view.compatibility_notes
          : [];
        const managerLabel = view.report?.manager_name
          ? `${view.report.manager_name} (CIK ${view.report.manager_cik})`
          : `Gestor 13F CIK ${view.report?.manager_cik || "—"}`;
        const fieldName = view.observation?.field_name || "";
        const fieldLabel = CAZATIBURONES_FIELD_LABELS[fieldName] || fieldName || "Valor 13F";
        const unitKey = view.observation?.unit || "";
        const unitLabel = CAZATIBURONES_UNIT_LABELS[unitKey] || unitKey;
        const formattedVal = view.observation?.value !== null && view.observation?.value !== undefined
          ? `${formatDecimalRatio(view.observation.value, 4, { trimTrailingZeros: true })}${unitLabel ? ` ${unitLabel}` : ""}`
          : renderAbsenceMark("missing", "Sin evidencia").outerHTML;
        const periodRaw = view.report?.report_period || view.report?.report_of_date || view.observation?.period_end;
        const periodText = periodRaw ? formatCalendarDate(periodRaw) : null;
        const issuerLabel = view.row?.name_of_issuer || view.row?.title_of_class || assetDisplayLabel(cazatiburonesSelectedAssetId);
        row.innerHTML =
          `<p class="eyebrow">${cazatiburonesEscapeHtml(managerLabel)} · ${cazatiburonesEscapeHtml(fieldLabel)}</p>` +
          `<dl>` +
          `<dt>Emisor / clase</dt><dd>${cazatiburonesEscapeHtml(issuerLabel)}</dd>` +
          `<dt>Declarado (as-filed)</dt><dd><span class="figure" title="Valor exacto: ${cazatiburonesEscapeHtml(view.observation?.value)} ${cazatiburonesEscapeHtml(unitKey)}">${formattedVal}</span></dd>` +
          (periodText ? `<dt>Período del informe</dt><dd>${cazatiburonesEscapeHtml(periodText)}</dd>` : "") +
          `<dt>Conocido desde</dt><dd>${cazatiburonesEscapeHtml(formatInstant(view.observation?.available_at))}</dd>` +
          `</dl>` +
          `<details class="cazatiburones-tech-details"><summary>Campos técnicos</summary>` +
          `<dl>` +
          `<dt>CUSIP</dt><dd>${cazatiburonesEscapeHtml(view.row?.cusip || "—")}</dd>` +
          `<dt>Report ID</dt><dd>${cazatiburonesEscapeHtml(view.report?.report_id || "—")}</dd>` +
          `<dt>Campo</dt><dd>${cazatiburonesEscapeHtml(fieldName || "—")}</dd>` +
          `<dt>Unidad</dt><dd>${cazatiburonesEscapeHtml(unitKey || "—")}</dd>` +
          `</dl>` +
          (compatibilityNotes.length > 0
            ? `<p class="collection-status">Compatibilidad: ${cazatiburonesEscapeHtml(compatibilityNotes.join(", "))}</p>`
            : "") +
          `</details>`;
        container.append(row);
      }
      const renderedCount = cazatiburonesProgressiveVisibleCounts.get("institutional-observations");
      const status = createElement("p", "collection-status", "");
      const controls = createElement("div", "collection-controls");
      container.append(status, controls);
      renderProgressiveCollectionControls(
        status,
        controls,
        observations.length,
        renderedCount,
        {
          label: "observaciones",
          onMore: () => {
            cazatiburonesProgressiveVisibleCounts.set(
              "institutional-observations",
              renderedCount + PROGRESSIVE_COLLECTION_PAGE_SIZE,
            );
            render();
          },
          onLess: () => {
            cazatiburonesProgressiveVisibleCounts.set(
              "institutional-observations",
              PROGRESSIVE_COLLECTION_PAGE_SIZE,
            );
            render();
          },
        },
      );
    }
  };
  render();
  renderCazatiburonesSelectedContext(observations);
  const latestAvailable = observations
    .map((view) => view?.observation?.available_at)
    .filter(Boolean)
    .sort()
    .pop();
  const chronoNote = latestAvailable ? ` · último disponible ${formatInstant(latestAvailable)}` : "";
  const modeNote = isFull ? "detalle completo" : "resumen";
  byId("cazatiburones-institutional-observations-summary").innerHTML =
    `<span class="figure">${formatInteger(observations.length)}</span> de ` +
    `<span class="figure">${formatInteger(payload.total_matching)}</span> filas as-filed ` +
    `(${modeNote}) · offset <span class="figure">${formatInteger(offset)}</span> ` +
    `límite <span class="figure">${formatInteger(limit)}</span>` +
    chronoNote +
    (payload.truncated ? " · truncado" : "");
}

function renderCazatiburonesTimelineGroup(container, title, entries, key) {
  const render = () => {
    container.replaceChildren(
      createElement("strong", "cazatiburones-feature-group-title", title),
    );
    if (entries.length === 0) {
      container.append(
        renderAbsenceMark(
          "missing",
          "No hay declaraciones elegibles en este corte",
          "Sin revisiones documentales SEC elegibles en este corte",
        ),
      );
      return;
    }
    const visibleCount = progressiveCollectionVisibleCount(
      entries.length,
      cazatiburonesProgressiveVisibleCounts.get(key),
    );
    cazatiburonesProgressiveVisibleCounts.set(key, visibleCount);
    for (const entry of entries.slice(0, visibleCount)) {
      const row = createElement("div", "cazatiburones-row");
      row.innerHTML =
        `<p class="eyebrow">${cazatiburonesEscapeHtml(entry.form)} · ${cazatiburonesEscapeHtml(formatCalendarDate(entry.filing_date))}</p>` +
        `<dl>` +
        `<dt>Presentación SEC</dt><dd>${cazatiburonesEscapeHtml(formatCalendarDate(entry.filing_date))}</dd>` +
        `<dt>Período del informe</dt><dd>${cazatiburonesFieldOrAbsence(entry.report_date, formatCalendarDate)}</dd>` +
        `<dt>Aceptado</dt><dd>${cazatiburonesEscapeHtml(formatInstant(entry.accepted_at))}</dd>` +
        `<dt>Conocido desde</dt><dd>${cazatiburonesEscapeHtml(formatInstant(entry.available_at))}</dd>` +
        `<dt>Enmienda</dt><dd>${entry.is_amendment ? "Sí" : "No"}</dd>` +
        `</dl>` +
        `<details class="cazatiburones-tech-details"><summary>Campos técnicos</summary>` +
        `<dl><dt>Accession</dt><dd>${cazatiburonesEscapeHtml(entry.accession)}</dd>` +
        `<dt>SHA-256</dt><dd>${cazatiburonesEscapeHtml(entry.content_sha256)}</dd>` +
        `<dt>Fuente</dt><dd>${cazatiburonesEscapeHtml(entry.source_url)}</dd></dl>` +
        `</details>`;
      container.append(row);
    }
    const status = createElement("p", "collection-status", "");
    const controls = createElement("div", "collection-controls");
    container.append(status, controls);
    renderProgressiveCollectionControls(
      status,
      controls,
      entries.length,
      visibleCount,
      {
        label: "documentos",
        onMore: () => {
          cazatiburonesProgressiveVisibleCounts.set(
            key,
            visibleCount + PROGRESSIVE_COLLECTION_PAGE_SIZE,
          );
          render();
        },
        onLess: () => {
          cazatiburonesProgressiveVisibleCounts.set(key, PROGRESSIVE_COLLECTION_PAGE_SIZE);
          render();
        },
      },
    );
  };
  render();
}

function renderCazatiburonesDocumentTimeline(payload) {
  const assetContainer = byId("cazatiburones-timeline-asset-document");
  const filerContainer = byId("cazatiburones-timeline-filer-document");
  const entriesByFamily = { asset_document: [], filer_document: [] };
  if (payload.state !== "missing" && Array.isArray(payload.entries)) {
    for (const entry of payload.entries) {
      if (entriesByFamily[entry.family]) entriesByFamily[entry.family].push(entry);
    }
  }
  renderCazatiburonesTimelineGroup(
    assetContainer,
    "Documentos del activo",
    entriesByFamily.asset_document,
    "timeline-asset-document",
  );
  renderCazatiburonesTimelineGroup(
    filerContainer,
    "Documentos del emisor",
    entriesByFamily.filer_document,
    "timeline-filer-document",
  );
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

async function loadCazatiburonesFullObservations() {
  const assetId = cazatiburonesSelectedAssetId;
  const knownAt = byId("report-known-at").value.trim();
  const fullBtn = byId("cazatiburones-load-full-btn");
  if (!assetId || !knownAt) return;
  const offset = 0;
  const limit = 200;
  const parameters = new URLSearchParams({
    asset_id: assetId,
    known_at: knownAt,
    offset: String(offset),
    limit: String(limit),
    projection: "full",
  });
  if (fullBtn) {
    fullBtn.disabled = true;
    fullBtn.textContent = "Cargando detalle completo…";
  }
  try {
    const payload = await api(`/api/v1/cazatiburones/institutional-observations?${parameters.toString()}`);
    if (assetId !== cazatiburonesSelectedAssetId || knownAt !== byId("report-known-at").value.trim()) return;
    renderCazatiburonesInstitutionalObservations(payload, offset, limit, { isFull: true });
  } catch (error) {
    if (fullBtn) {
      fullBtn.disabled = false;
      fullBtn.textContent = "Reintentar detalle completo 13F";
    }
    byId("cazatiburones-institutional-observations-summary").textContent = error.message;
  }
}

async function loadCazatiburonesBoard(assetId = cazatiburonesSelectedAssetId) {
  const sequence = ++cazatiburonesRequestSequence;
  if (!assetId || assetId !== cazatiburonesSelectedAssetId) return;
  const knownAt = byId("report-known-at").value.trim();
  cazatiburonesProgressiveVisibleCounts.clear();
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
  renderCazatiburonesSelectedContext([]);
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
    projection: "summary",
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
