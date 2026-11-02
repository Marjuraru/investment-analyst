"use strict";

function populateMarketComparisonAssets() {
  const benchmark = byId("comparison-benchmark");
  const assets = byId("comparison-assets");
  benchmark.replaceChildren();
  assets.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Selecciona una referencia";
  placeholder.disabled = true;
  placeholder.selected = true;
  benchmark.append(placeholder);
  for (const presentation of Object.values(marketAssets)) {
    const label = `${presentation.symbol} · ${presentation.name}`;
    const benchmarkOption = document.createElement("option");
    benchmarkOption.value = presentation.assetId;
    benchmarkOption.textContent = label;
    benchmark.append(benchmarkOption);
  }
  benchmark.value = "";
  syncComparisonAssetOptions({ initial: true });
}

function comparisonSelectedAssets() {
  return [...byId("comparison-assets").selectedOptions].map((option) => option.value);
}

function comparisonAssetLabel(assetId) {
  const presentation = marketAssets[assetId];
  return presentation ? `${presentation.symbol} · ${presentation.name}` : assetId;
}

function comparisonSelectionStatus(message) {
  byId("comparison-selection-status").textContent = message;
}

function renderComparisonSelectedAssets() {
  const container = byId("comparison-selected-assets");
  const benchmarkId = byId("comparison-benchmark").value;
  const selected = comparisonSelectedAssets();
  container.replaceChildren();
  if (!benchmarkId) {
    comparisonSelectionStatus("Selecciona una referencia y al menos un segundo activo.");
    return;
  }
  for (const assetId of selected) {
    const chip = createElement("span", "comparison-selected-chip");
    chip.append(createElement("span", "comparison-selected-chip-label", comparisonAssetLabel(assetId)));
    if (assetId === benchmarkId) {
      chip.append(createElement("span", "comparison-selected-chip-lock", "Referencia"));
    } else {
      const remove = createElement("button", "comparison-chip-remove", "Quitar");
      remove.type = "button";
      remove.setAttribute("aria-label", `Quitar ${comparisonAssetLabel(assetId)}`);
      remove.addEventListener("click", () => {
        const option = [...byId("comparison-assets").options].find((candidate) => candidate.value === assetId);
        if (!option) return;
        option.selected = false;
        renderComparisonSelectedAssets();
        comparisonSelectionStatus(`${formatInteger(comparisonSelectedAssets().length)} de 5 activos seleccionados.`);
      });
      chip.append(remove);
    }
    container.append(chip);
  }
  comparisonSelectionStatus(
    `${formatInteger(selected.length)} de 5 activos seleccionados · ${marketAssets[benchmarkId]?.quoteCurrency || "moneda no disponible"}.`,
  );
}

function renderComparisonAssetOptions(filter = "") {
  const listbox = byId("comparison-asset-options");
  const selected = new Set(comparisonSelectedAssets());
  const normalizedFilter = filter.trim().toLocaleLowerCase(LOCALE);
  listbox.replaceChildren();
  comparisonAssetOptionIndex = -1;
  const options = [...byId("comparison-assets").options].filter((option) => {
    if (selected.has(option.value)) return false;
    return !normalizedFilter || option.textContent.toLocaleLowerCase(LOCALE).includes(normalizedFilter);
  });
  if (options.length === 0) {
    listbox.append(createElement("li", "comparison-asset-option-empty", "No se encontraron activos compatibles."));
    return;
  }
  for (const [index, option] of options.entries()) {
    const item = createElement("li", "comparison-asset-option", option.textContent);
    item.id = `comparison-asset-option-${option.value.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
    item.dataset.value = option.value;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", "false");
    item.addEventListener("mouseenter", () => {
      comparisonAssetOptionIndex = index;
      updateComparisonAssetOptionFocus();
    });
    item.addEventListener("click", () => selectComparisonAsset(option.value));
    listbox.append(item);
  }
}

let comparisonAssetOptionIndex = -1;

function updateComparisonAssetOptionFocus() {
  const options = [...byId("comparison-asset-options").querySelectorAll('[role="option"]')];
  options.forEach((option, index) => option.classList.toggle("active", index === comparisonAssetOptionIndex));
  const input = byId("comparison-asset-search");
  if (comparisonAssetOptionIndex >= 0 && options[comparisonAssetOptionIndex]) {
    input.setAttribute("aria-activedescendant", options[comparisonAssetOptionIndex].id);
    options[comparisonAssetOptionIndex].scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
}

function openComparisonAssetPicker() {
  const input = byId("comparison-asset-search");
  const listbox = byId("comparison-asset-options");
  input.setAttribute("aria-expanded", "true");
  listbox.hidden = false;
  renderComparisonAssetOptions(input.value);
}

function closeComparisonAssetPicker() {
  const input = byId("comparison-asset-search");
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  byId("comparison-asset-options").hidden = true;
  comparisonAssetOptionIndex = -1;
}

function selectComparisonAsset(assetId) {
  const option = [...byId("comparison-assets").options].find((candidate) => candidate.value === assetId);
  if (!option) return;
  const selected = comparisonSelectedAssets();
  if (selected.includes(assetId)) return;
  if (selected.length >= 5) {
    comparisonSelectionStatus("La muestra admite como máximo cinco activos.");
    return;
  }
  option.selected = true;
  byId("comparison-asset-search").value = "";
  closeComparisonAssetPicker();
  renderComparisonSelectedAssets();
}

function syncComparisonAssetOptions({ initial = false } = {}) {
  const benchmarkId = byId("comparison-benchmark").value;
  const benchmarkPresentation = marketAssets[benchmarkId];
  const selectedBefore = comparisonSelectedAssets();
  const compatible = Object.values(marketAssets).filter(
    (presentation) => presentation.quoteCurrency === benchmarkPresentation?.quoteCurrency,
  );
  const compatibleIds = new Set(compatible.map((presentation) => presentation.assetId));
  const retainedIds = [benchmarkId, ...selectedBefore.filter((assetId) => assetId !== benchmarkId)]
    .filter((assetId, index, values) => compatibleIds.has(assetId) && values.indexOf(assetId) === index)
    .slice(0, 5);
  const assets = byId("comparison-assets");
  assets.replaceChildren();
  for (const presentation of compatible) {
    const option = document.createElement("option");
    option.value = presentation.assetId;
    option.textContent = comparisonAssetLabel(presentation.assetId);
    option.selected = retainedIds.includes(presentation.assetId);
    assets.append(option);
  }
  renderComparisonSelectedAssets();
  if (!initial && selectedBefore.some((assetId) => !retainedIds.includes(assetId))) {
    comparisonSelectionStatus("Se retiraron los activos incompatibles; la muestra conserva una sola moneda.");
  }
}

const comparisonAssetSearch = byId("comparison-asset-search");
comparisonAssetSearch.addEventListener("focus", openComparisonAssetPicker);
comparisonAssetSearch.addEventListener("input", () => {
  openComparisonAssetPicker();
  renderComparisonAssetOptions(comparisonAssetSearch.value);
});
comparisonAssetSearch.addEventListener("keydown", (event) => {
  const options = [...byId("comparison-asset-options").querySelectorAll('[role="option"]')];
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!options.length) return;
    comparisonAssetOptionIndex = event.key === "ArrowDown"
      ? (comparisonAssetOptionIndex + 1) % options.length
      : (comparisonAssetOptionIndex - 1 + options.length) % options.length;
    updateComparisonAssetOptionFocus();
  } else if (event.key === "Enter") {
    event.preventDefault();
    if (options[comparisonAssetOptionIndex]) selectComparisonAsset(options[comparisonAssetOptionIndex].dataset.value);
    else if (options.length === 1) selectComparisonAsset(options[0].dataset.value);
  } else if (event.key === "Escape") {
    event.preventDefault();
    closeComparisonAssetPicker();
  }
});

document.addEventListener("click", (event) => {
  if (!byId("comparison-asset-picker").contains(event.target)) closeComparisonAssetPicker();
});

function comparisonPercent(value) {
  const parsed = numericValue(value);
  return parsed === null ? "—" : formatRangeChange(parsed);
}

function renderMarketComparison(payload) {
  if (payload.schema_version !== "market-multi-asset-comparison-v1" || payload.traceability_verified !== true) {
    throw new Error("La comparación local no respetó su contrato versionado.");
  }
  const results = byId("comparison-results");
  const cards = byId("comparison-cards");
  const chart = byId("comparison-chart");
  const palette = COMPARISON_PALETTE;
  chart.replaceChildren();
  const svg = document.createElementNS(SVG_NAMESPACE, "svg");
  svg.setAttribute("viewBox", "0 0 800 230");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Cierres normalizados a base 100 para la muestra común");
  const dates = payload.common_dates;
  const values = payload.series.flatMap((series) => series.points.map((point) => numericValue(point.normalized_close)));
  const minimum = Math.min(...values.filter((value) => value !== null));
  const maximum = Math.max(...values.filter((value) => value !== null));
  const span = maximum - minimum || 1;
  for (const [index, series] of payload.series.entries()) {
    const line = document.createElementNS(SVG_NAMESPACE, "polyline");
    const points = series.points.map((point, pointIndex) => {
      const x = 35 + (pointIndex * 740) / Math.max(1, dates.length - 1);
      const y = 205 - ((numericValue(point.normalized_close) - minimum) * 175) / span;
      return `${x},${y}`;
    });
    line.setAttribute("points", points.join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", palette[index]);
    line.setAttribute("stroke-width", "2.5");
    const title = document.createElementNS(SVG_NAMESPACE, "title");
    title.textContent = `${marketAssets[series.asset_id]?.symbol || series.asset_id}: base 100`;
    line.append(title);
    svg.append(line);
  }
  chart.append(svg);
  cards.replaceChildren();
  for (const series of payload.series) {
    const card = document.createElement("article");
    card.className = "comparison-card";
    const identity = marketAssets[series.asset_id];
    const notApplicableMark = renderAbsenceMark("not-applicable", "No aplica", "Activo de referencia").outerHTML;
    // The backend contract declares three statuses -- available, unavailable,
    // not_applicable (comparison_models.py) -- not just a binary
    // applicable/not_applicable split. "unavailable" means the metric was
    // attempted but could not be computed with the common coverage (never a
    // structural non-fit like the benchmark itself), so it renders through
    // the reusable "not-evaluable" absence mark rather than falling through
    // to comparisonPercent()'s bare "—" for a null value.
    const correlationUnavailableMark = renderAbsenceMark(
      "not-evaluable",
      "No evaluable",
      "Correlación no calculable con la cobertura común disponible",
    ).outerHTML;
    const betaUnavailableMark = renderAbsenceMark(
      "not-evaluable",
      "No evaluable",
      "Beta no calculable con la cobertura común disponible",
    ).outerHTML;
    // Every comparison figure below -- including the absence-mark branches,
    // which style themselves -- ends up tabular/monospace/right-aligned:
    // numeric branches are wrapped in the shared .figure utility class.
    const correlation =
      series.metrics.correlation_status === "not_applicable"
        ? notApplicableMark
        : series.metrics.correlation_status === "unavailable"
          ? correlationUnavailableMark
          : `<span class="figure">${comparisonPercent(series.metrics.correlation_to_benchmark)}</span>`;
    const beta =
      series.metrics.beta_status === "not_applicable"
        ? notApplicableMark
        : series.metrics.beta_status === "unavailable"
          ? betaUnavailableMark
          : `<span class="figure">${series.metrics.beta_to_benchmark ?? "No disponible"}</span>`;
    card.innerHTML = `<p class="eyebrow">${identity?.symbol || series.asset_id}</p><h3>${identity?.name || series.asset_id}</h3><dl><dt>Retorno total</dt><dd><span class="figure">${comparisonPercent(series.metrics.total_return)}</span></dd><dt>Drawdown máximo</dt><dd><span class="figure">${comparisonPercent(series.metrics.maximum_drawdown)}</span></dd><dt>Volatilidad diaria</dt><dd><span class="figure">${comparisonPercent(series.metrics.daily_volatility)}</span></dd><dt>Correlación</dt><dd>${correlation}</dd><dt>Beta</dt><dd>${beta}</dd></dl>`;
    const identityLink = createContextualNavigationButton(
      identity?.symbol || series.asset_id,
      { board: "activo", assetId: series.asset_id, subtab: "mercado" },
      "comparison-asset-link",
    );
    identityLink.setAttribute(
      "aria-label",
      "Abrir activo " + (identity?.symbol || series.asset_id) + " en Activo",
    );
    card.querySelector(".eyebrow").replaceChildren(identityLink);
    cards.append(card);
  }
  byId("comparison-json").textContent = JSON.stringify(payload, null, 2);
  byId("comparison-status").textContent = `${payload.common_dates.length} fechas UTC comunes · ${payload.quote_currency} · corte ${formatInstant(payload.known_at)}.`;
  results.classList.remove("hidden");
}

async function queryMarketComparison() {
  const assets = comparisonSelectedAssets();
  const benchmark = byId("comparison-benchmark").value;
  if (!benchmark || !assets.includes(benchmark) || assets.length < 2 || assets.length > 5) {
    throw new Error("Selecciona entre dos y cinco activos, incluida la referencia.");
  }
  const sequence = ++marketComparisonRequestSequence;
  const results = byId("comparison-results");
  results.setAttribute("aria-busy", "true");
  byId("comparison-submit").disabled = true;
  byId("comparison-status").textContent = "Construyendo la muestra común local…";
  const parameters = new URLSearchParams({
    benchmark_id: benchmark,
    start: byId("comparison-start").value,
    end: byId("comparison-end").value,
    known_at: byId("report-known-at").value.trim(),
  });
  for (const assetId of assets) parameters.append("asset_id", assetId);
  try {
    const payload = await api(`/api/v1/market-comparison?${parameters.toString()}`);
    if (sequence !== marketComparisonRequestSequence) return;
    renderMarketComparison(payload);
  } catch (error) {
    if (sequence !== marketComparisonRequestSequence) return;
    byId("comparison-status").textContent = error.message;
  } finally {
    if (sequence === marketComparisonRequestSequence) {
      results.setAttribute("aria-busy", "false");
      byId("comparison-submit").disabled = false;
    }
  }
}

const DAILY_PATTERN_STATE_LABELS = Object.freeze({
  positive: "Positivo",
  negative: "Negativo",
  neutral: "Neutral",
  unavailable: "No disponible",
});

const DAILY_PATTERN_TYPE_LABELS = Object.freeze({
  macd_cross_positive: "MACD cruza a positivo",
  macd_cross_negative: "MACD cruza a negativo",
  rsi_cross_above_30: "RSI cruza sobre 30",
});

let dailyPatternsRequestSequence = 0;
let dailyPatternsAbortController = null;
let dailyPatternsContextKey = null;

function dailyPatternsCutDate() {
  const raw = byId("report-known-at").value.trim();
  if (!raw) return null;
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.valueOf())) return null;
  return new Date(Date.UTC(parsed.getUTCFullYear(), parsed.getUTCMonth(), parsed.getUTCDate()));
}

function prepareDailyPatternsRange() {
  const cut = dailyPatternsCutDate();
  const start = byId("daily-patterns-start");
  const end = byId("daily-patterns-end");
  if (!cut) {
    start.value = "";
    end.value = "";
    return;
  }
  start.value = new Date(cut.getTime() - 89 * 86_400_000).toISOString().slice(0, 10);
  end.value = cut.toISOString().slice(0, 10);
}

function dailyPatternsFigure(value) {
  const figure = createElement("span", "figure", formatNumber(value, { maximumFractionDigits: 6 }));
  figure.title = String(value);
  return figure;
}

function appendDailyPatternsRow(container, label, value) {
  container.append(createElement("dt", "", label));
  const cell = document.createElement("dd");
  if (typeof value === "string") cell.textContent = value;
  else cell.append(value);
  container.append(cell);
}

function resetDailyMarketPatterns() {
  const key = `${selectedMarketAsset || ""}|${byId("report-known-at").value.trim()}`;
  if (key === dailyPatternsContextKey) return;
  dailyPatternsContextKey = key;
  dailyPatternsRequestSequence += 1;
  if (dailyPatternsAbortController) {
    dailyPatternsAbortController.abort();
    dailyPatternsAbortController = null;
  }
  const results = byId("daily-patterns-results");
  results.classList.add("hidden");
  results.setAttribute("aria-busy", "false");
  byId("daily-patterns-current").replaceChildren();
  byId("daily-patterns-segments").replaceChildren();
  byId("daily-patterns-events").replaceChildren();
  byId("daily-patterns-coverage").textContent = "";
  byId("daily-patterns-limitations").replaceChildren();
  byId("daily-patterns-json").textContent = "";
  byId("daily-patterns-status").textContent =
    "Sin consultar. Elige un rango inclusivo de hasta 180 días y consulta al corte global.";
  byId("daily-patterns-submit").disabled = false;
  prepareDailyPatternsRange();
}

function renderDailyPatternsCurrent(payload) {
  const container = byId("daily-patterns-current");
  container.replaceChildren();
  if (!payload.points.length) {
    container.append(
      renderAbsenceMark(
        "missing",
        "Sin períodos observados",
        "El rango no contiene cierres diarios disponibles al corte",
      ),
    );
    return;
  }
  const point = payload.points[payload.points.length - 1];
  container.append(
    createElement(
      "p",
      "patterns-current-heading",
      `${formatCalendarDate(point.as_of)} · ${DAILY_PATTERN_STATE_LABELS[point.macd_state] || point.macd_state}`,
    ),
  );
  const list = document.createElement("dl");
  list.className = "patterns-evidence";
  appendDailyPatternsRow(list, "Cierre", dailyPatternsFigure(point.close));
  appendDailyPatternsRow(list, "Disponibilidad de la evidencia", formatInstant(point.evidence_available_at));
  appendDailyPatternsRow(
    list,
    "RSI(14)",
    point.rsi.status === "available"
      ? dailyPatternsFigure(point.rsi.value)
      : renderAbsenceMark("missing", "Sin RSI elegible", point.rsi.reason),
  );
  appendDailyPatternsRow(
    list,
    "Histograma MACD(12,26,9)",
    point.macd_histogram.status === "available"
      ? dailyPatternsFigure(point.macd_histogram.value)
      : renderAbsenceMark("missing", "Sin histograma elegible", point.macd_histogram.reason),
  );
  appendDailyPatternsRow(list, "Estado MACD", DAILY_PATTERN_STATE_LABELS[point.macd_state]);
  container.append(list);
}

function renderDailyPatternsSegments(payload) {
  const container = byId("daily-patterns-segments");
  container.replaceChildren();
  if (!payload.segments.length) {
    container.append(
      renderAbsenceMark(
        "missing",
        "Sin tramos evaluables",
        "El histograma MACD no está disponible en los períodos observados",
      ),
    );
    return;
  }
  const table = document.createElement("table");
  table.className = "patterns-segments-table";
  const caption = createElement(
    "caption",
    "",
    "Tramos de estado MACD con períodos observados y censura de borde.",
  );
  table.append(caption);
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Estado", "Inicio", "Fin", "Períodos", "Duración observada", "Borde"]) {
    const cell = createElement("th", "", label);
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const segment of payload.segments) {
    const row = document.createElement("tr");
    row.append(createElement("td", `patterns-state-${segment.state}`, DAILY_PATTERN_STATE_LABELS[segment.state]));
    row.append(createElement("td", "", formatCalendarDate(segment.start_as_of)));
    row.append(createElement("td", "", formatCalendarDate(segment.end_as_of)));
    row.append(createElement("td", "figure", formatInteger(segment.observed_periods)));
    const days = segment.duration_seconds / 86_400;
    row.append(
      createElement(
        "td",
        "patterns-duration",
        `${formatInteger(segment.duration_seconds)} s · ${formatNumber(days, { maximumFractionDigits: 2 })} días UTC`,
      ),
    );
    const border = segment.left_censored && segment.right_censored
      ? "Ambos"
      : segment.left_censored
        ? "Izquierdo"
        : segment.right_censored
          ? "Derecho"
          : "No";
    row.append(createElement("td", "", border));
    body.append(row);
  }
  table.append(body);
  container.append(table);
}

function renderDailyPatternsEvents(payload) {
  const container = byId("daily-patterns-events");
  container.replaceChildren();
  if (!payload.events.length) {
    const evaluable = payload.points.some(
      (point) => point.macd_histogram.status === "available" || point.rsi.status === "available",
    );
    container.append(
      evaluable
        ? createElement(
            "p",
            "patterns-empty",
            "Sin cruces en el rango consultado; la ausencia de cruce no implica continuidad futura.",
          )
        : renderAbsenceMark(
            "missing",
            "Sin evidencia evaluable",
            "No hay métricas RSI o MACD elegibles en los períodos observados",
          ),
    );
    return;
  }
  const list = document.createElement("ol");
  list.className = "patterns-event-list";
  for (const event of payload.events) {
    const item = document.createElement("li");
    item.className = "patterns-event-item";
    item.append(
      createElement(
        "p",
        "patterns-event-title",
        DAILY_PATTERN_TYPE_LABELS[event.pattern_type] || event.pattern_type,
      ),
    );
    item.append(
      createElement(
        "p",
        "patterns-event-dates",
        `${formatCalendarDate(event.previous_as_of)} → ${formatCalendarDate(event.current_as_of)} · disponible ${formatInstant(event.evidence_available_at)}`,
      ),
    );
    const values = createElement("p", "patterns-event-values");
    values.append(
      dailyPatternsFigure(event.previous_value),
      document.createTextNode(" → "),
      dailyPatternsFigure(event.current_value),
      document.createTextNode(` · ${event.comparison}`),
    );
    item.append(values);
    const details = document.createElement("details");
    details.className = "patterns-event-evidence";
    details.append(createElement("summary", "", "Evidencia exacta"));
    details.append(
      createElement("p", "patterns-technical", `Métrica previa: ${event.previous_metric_result_id}`),
      createElement("p", "patterns-technical", `Métrica actual: ${event.current_metric_result_id}`),
      createElement("p", "patterns-technical", `Cierre previo: ${event.previous_close_observation_id}`),
      createElement("p", "patterns-technical", `Cierre actual: ${event.current_close_observation_id}`),
    );
    item.append(details);
    list.append(item);
  }
  container.append(list);
}

function renderDailyMarketPatterns(payload) {
  if (
    payload.schema_version !== "daily-market-patterns-v1"
    || payload.algorithm_version !== "daily-market-patterns-v1-decimal34"
  ) {
    throw new Error("La reconstrucción diaria no respetó su contrato versionado.");
  }
  const presentation = marketAssets[payload.asset_id];
  const label = presentation
    ? `${presentation.symbol} · ${presentation.name}`
    : payload.asset_id;
  const coverage = payload.coverage;
  byId("daily-patterns-summary").textContent =
    `${label} · ${payload.request.start_date} a ${payload.request.end_date} inclusive · corte ${formatInstant(payload.request.known_at)} · ${formatInteger(coverage.point_count)} períodos observados · ${formatInteger(coverage.close_periods)} cierres seleccionados de ${formatInteger(coverage.close_observations_read)} filas leídas.`;
  renderDailyPatternsCurrent(payload);
  renderDailyPatternsSegments(payload);
  renderDailyPatternsEvents(payload);
  byId("daily-patterns-coverage").textContent =
    `Cobertura: RSI ${formatInteger(coverage.rsi_available_points)} disponibles y ${formatInteger(coverage.rsi_missing_points)} ausentes; histograma MACD ${formatInteger(coverage.macd_available_points)} disponibles y ${formatInteger(coverage.macd_missing_points)} ausentes; ${formatInteger(coverage.discarded_close_revisions)} revisiones de cierre descartadas; ${formatInteger(coverage.metric_periods_without_close)} períodos con métricas sin cierre compatible; ${formatInteger(coverage.segment_count)} tramos y ${formatInteger(coverage.event_count)} cruces; máxima disponibilidad de evidencia ${coverage.maximum_evidence_available_at ? formatInstant(coverage.maximum_evidence_available_at) : "sin evidencia"}.`;
  const limitations = byId("daily-patterns-limitations");
  limitations.replaceChildren();
  for (const limitation of payload.limitations) {
    limitations.append(createElement("li", "", limitation));
  }
  byId("daily-patterns-json").textContent = JSON.stringify(payload, null, 2);
  byId("daily-patterns-status").textContent = payload.points.length
    ? `Reconstrucción descriptiva al corte con ${formatInteger(payload.points.length)} períodos observados. Los valores visibles se redondean; el detalle técnico conserva el Decimal exacto.`
    : "El rango no contiene cierres disponibles al corte; la ausencia es explícita y no se completa con otro período.";
  byId("daily-patterns-results").classList.remove("hidden");
}

async function queryDailyMarketPatterns(event) {
  if (event) event.preventDefault();
  const assetId = selectedMarketAsset;
  const cut = byId("report-known-at").value.trim();
  const start = byId("daily-patterns-start").value;
  const end = byId("daily-patterns-end").value;
  if (!assetId) {
    byId("daily-patterns-status").textContent = "Selecciona un activo del catálogo antes de consultar.";
    return;
  }
  if (!start || !end) {
    byId("daily-patterns-status").textContent = "Indica un inicio y un fin inclusivos para el rango.";
    return;
  }
  const sequence = ++dailyPatternsRequestSequence;
  if (dailyPatternsAbortController) dailyPatternsAbortController.abort();
  const controller = new AbortController();
  dailyPatternsAbortController = controller;
  const results = byId("daily-patterns-results");
  results.setAttribute("aria-busy", "true");
  byId("daily-patterns-submit").disabled = true;
  byId("daily-patterns-status").textContent = "Reconstruyendo estados y cruces al corte…";
  const parameters = new URLSearchParams({
    asset_id: assetId,
    start,
    end,
    known_at: cut,
  });
  try {
    const payload = await api(`/api/v1/market-patterns?${parameters.toString()}`, {
      signal: controller.signal,
    });
    if (sequence !== dailyPatternsRequestSequence || controller.signal.aborted) return;
    if (
      assetId !== selectedMarketAsset
      || cut !== byId("report-known-at").value.trim()
    ) {
      return;
    }
    renderDailyMarketPatterns(payload);
  } catch (error) {
    if (sequence !== dailyPatternsRequestSequence || controller.signal.aborted) return;
    byId("daily-patterns-status").textContent = error.message;
  } finally {
    if (sequence === dailyPatternsRequestSequence) {
      results.setAttribute("aria-busy", "false");
      byId("daily-patterns-submit").disabled = false;
    }
  }
}

byId("daily-patterns-form").addEventListener("submit", (event) => {
  void queryDailyMarketPatterns(event);
});
prepareDailyPatternsRange();

const ML_REPORT_SCHEMA = "ml-research-report-view-v1";
const ML_RUN_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ML_SCORER_LABELS = Object.freeze({
  logistic_regression: "Regresión logística",
  hist_gradient_boosting: "Histogram gradient boosting",
  half_probability: "p = 0.5",
  training_class_prior: "Prior de clase",
});
const ML_HOLDOUT_TEXT = Object.freeze({
  not_evaluated: "Holdout no evaluado; no se infieren métricas, filas ni clases. Predictor no promovido.",
  confirmed_research_edge: "Mejora retrospectiva condicional en holdout; no autoriza uso operativo ni promoción.",
  holdout_not_confirmed: "Holdout no confirmado; el resultado no promueve un predictor operativo.",
});
let mlResearchRequestSequence = 0;
let mlResearchAbortController = null;
let mlResearchContextKey = null;

function mlResearchRunId() {
  return byId("ml-research-run-id").value.trim().toLowerCase();
}

function mlResearchRunIdValid(value) {
  return ML_RUN_ID_PATTERN.test(value);
}

function mlResearchScorerLabel(value) {
  return ML_SCORER_LABELS[value] || String(value);
}

function mlResearchPercent(value) {
  const parsed = numericValue(value);
  return parsed === null ? "—" : formatNumber(parsed, {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function mlResearchFigure(value) {
  const figure = createElement("span", "figure", formatNumber(value, { maximumFractionDigits: 6 }));
  figure.title = value === null || value === undefined ? "UNKNOWN" : String(value);
  return figure;
}

function mlResearchTable(headers, rows) {
  const table = createElement("table", "patterns-segments-table ml-research-table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of headers) {
    const cell = createElement("th", "", label);
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tableRow = document.createElement("tr");
    for (const value of row) {
      const cell = document.createElement("td");
      if (typeof value === "string" || typeof value === "number") cell.textContent = String(value);
      else if (value === null || value === undefined) cell.textContent = "—";
      else cell.append(value);
      tableRow.append(cell);
    }
    body.append(tableRow);
  }
  table.append(body);
  return table;
}

function mlResearchComparison(development, model, reference) {
  return development.pooled_comparisons.find(
    (item) => item.model === model && item.reference === reference,
  ) || null;
}

function renderMlResearchModels(payload) {
  const container = byId("ml-research-models");
  container.replaceChildren();
  if (!payload.development) {
    container.append(
      renderAbsenceMark(
        "missing",
        "Sin evaluación de desarrollo",
        payload.insufficiency_reason || "El artefacto no contiene métricas de desarrollo",
      ),
    );
    return;
  }
  const rows = payload.development.pooled_scores.slice(0, 4).map((score) => {
    const vsHalf = mlResearchComparison(payload.development, score.scorer, "half_probability");
    const vsPrior = mlResearchComparison(payload.development, score.scorer, "training_class_prior");
    return [
      mlResearchScorerLabel(score.scorer),
      mlResearchFigure(score.brier),
      vsHalf ? mlResearchPercent(vsHalf.relative_improvement) : "—",
      vsPrior ? mlResearchPercent(vsPrior.relative_improvement) : "—",
      mlResearchFigure(score.rows),
    ];
  });
  container.append(
    mlResearchTable(
      ["Modelo o referencia", "Brier", "Δ vs p = 0.5", "Δ vs prior", "Filas OOS"],
      rows,
    ),
  );
}

function renderMlResearchComparisons(payload) {
  const container = byId("ml-research-comparisons");
  container.replaceChildren();
  if (!payload.development) {
    container.append(
      renderAbsenceMark(
        "missing",
        "Sin comparaciones",
        "No se calcularon intervalos ni criterios sin muestra suficiente",
      ),
    );
    return;
  }
  const rows = payload.development.pooled_comparisons.slice(0, 4).map((item) => [
    mlResearchScorerLabel(item.model),
    mlResearchScorerLabel(item.reference),
    mlResearchPercent(item.relative_improvement),
    mlResearchFigure(item.lower_bound),
    item.improved_at_least_five_percent ? "Sí" : "No",
    item.lower_bound_positive ? "Sí" : "No",
  ]);
  container.append(
    mlResearchTable(
      ["Modelo", "Referencia", "Mejora relativa", "Límite inferior", "¿≥ 5 %?", "¿Límite > 0?"],
      rows,
    ),
  );
}

function renderMlResearchFolds(payload) {
  const container = byId("ml-research-folds");
  container.replaceChildren();
  if (!payload.development) {
    container.append(
      renderAbsenceMark("missing", "Sin folds", "El artefacto insuficiente no ejecutó folds"),
    );
    return;
  }
  const rows = payload.development.folds.slice(0, 3).map((fold) => {
    const score = (scorer) => {
      const item = fold.scores.find((candidate) => candidate.scorer === scorer);
      return item ? mlResearchFigure(item.brier) : "—";
    };
    return [
      formatInteger(fold.fold),
      formatInteger(fold.train_rows),
      formatInteger(fold.purged_excluded),
      formatInteger(fold.embargo_excluded),
      formatInteger(fold.evaluation_rows),
      `${formatInteger(fold.evaluation_positives)} / ${formatInteger(fold.evaluation_negatives)}`,
      score("logistic_regression"),
      score("hist_gradient_boosting"),
      score("half_probability"),
      score("training_class_prior"),
    ];
  });
  container.append(
    mlResearchTable(
      [
        "Fold",
        "Train",
        "Purge",
        "Embargo",
        "Eval",
        "Pos / Neg",
        "Logística",
        "HGB",
        "p = 0.5",
        "Prior",
      ],
      rows,
    ),
  );
}

function renderMlResearchCalibration(payload) {
  const container = byId("ml-research-calibration");
  container.replaceChildren();
  if (!payload.development) {
    container.append(
      renderAbsenceMark(
        "missing",
        "Sin calibración",
        "No hay bins observados sin muestra suficiente",
      ),
    );
    return;
  }
  for (const table of payload.development.calibration.slice(0, 2)) {
    const rows = table.bins.slice(0, 10).map((bin) => [
      formatInteger(bin.index),
      `${bin.lower_bound}–${bin.upper_bound}${bin.includes_upper ? " (incl.)" : ""}`,
      formatInteger(bin.count),
      bin.count === 0 ? "—" : mlResearchFigure(bin.mean_probability),
      bin.count === 0 ? "—" : mlResearchFigure(bin.observed_frequency),
    ]);
    container.append(
      createElement("p", "patterns-event-title", mlResearchScorerLabel(table.scorer)),
      mlResearchTable(
        ["Bin", "Rango", "Filas", "Probabilidad media", "Frecuencia observada"],
        rows,
      ),
    );
  }
}

function mlResearchDecision(payload) {
  if (payload.artifact.status === "insufficient_evaluation_sample") {
    return { label: "Muestra insuficiente", tone: "warn" };
  }
  if (payload.development?.decision === "no_demonstrated_edge") {
    return { label: "No se demostró ventaja", tone: "neutral" };
  }
  if (payload.development?.decision === "candidate_selected") {
    return { label: "Candidato seleccionado en desarrollo", tone: "warn" };
  }
  return { label: "Estado no reconocido", tone: "bad" };
}

function mlResearchHoldoutText(payload) {
  if (!payload.holdout) {
    return "Holdout no disponible para este artefacto; no se infieren métricas ni clases.";
  }
  return ML_HOLDOUT_TEXT[payload.holdout.status] || "Estado de holdout no reconocido.";
}

function mlResearchCriterionText(payload) {
  if (payload.artifact.status === "insufficient_evaluation_sample") {
    return "Sin muestra suficiente no hay métricas, comparaciones ni criterio evaluado; la ausencia es explícita.";
  }
  const minimum = mlResearchPercent(
    payload.protocol.criteria.min_relative_brier_improvement,
  );
  const base =
    `Brier: menor es mejor. El criterio exigió una mejora relativa ≥ ${minimum} frente a ambas referencias` +
    ` y ambos límites inferiores > 0.`;
  if (payload.development?.decision === "no_demonstrated_edge") {
    return `${base} El criterio no se cumplió y no se promueve ningún predictor.`;
  }
  return `${base} El criterio de desarrollo se cumplió; el holdout se representa por separado.`;
}

function resetMlResearchReport() {
  const key = `${selectedMarketAsset || ""}|${mlResearchRunId()}`;
  if (key === mlResearchContextKey) return;
  mlResearchContextKey = key;
  mlResearchRequestSequence += 1;
  if (mlResearchAbortController) {
    mlResearchAbortController.abort();
    mlResearchAbortController = null;
  }
  const results = byId("ml-research-results");
  results.classList.add("hidden");
  results.setAttribute("aria-busy", "false");
  byId("ml-research-models").replaceChildren();
  byId("ml-research-comparisons").replaceChildren();
  byId("ml-research-folds").replaceChildren();
  byId("ml-research-calibration").replaceChildren();
  byId("ml-research-limitations").replaceChildren();
  byId("ml-research-json").textContent = "";
  byId("ml-research-range").textContent = "";
  byId("ml-research-evidence").textContent = "";
  byId("ml-research-holdout").textContent = "";
  byId("ml-research-decision").textContent = "";
  byId("ml-research-decision").className = "badge neutral";
  byId("ml-research-status").textContent =
    "Sin consultar. Pega el run_id del recibo de evaluación.";
  byId("ml-research-submit").disabled = false;
}

function renderMlResearchReport(payload, { assetId, runId }) {
  if (
    payload.schema_version !== ML_REPORT_SCHEMA
    || payload.provenance?.asset_id !== assetId
    || payload.artifact?.run_id !== runId
  ) {
    throw new Error("La evaluación ML no respetó su contrato versionado o cambió de identidad.");
  }
  const presentation = marketAssets[assetId];
  const assetLabel = presentation
    ? `${presentation.symbol} · ${presentation.name}`
    : payload.provenance.asset_id;
  const decision = mlResearchDecision(payload);
  byId("ml-research-range").textContent =
    `${assetLabel} · ${payload.protocol.decision_start} a ${payload.protocol.decision_end} inclusive` +
    ` · ${formatInteger(payload.protocol.decision_rows)} decisiones` +
    ` · artefacto ${payload.artifact.status}` +
    ` · creado ${formatInstant(payload.artifact.created_at)}` +
    ` · run ${payload.artifact.run_id}`;
  const badgeTarget = byId("ml-research-decision");
  badgeTarget.textContent = decision.label;
  badgeTarget.className = `badge ${decision.tone}`;
  byId("ml-research-holdout").textContent = mlResearchHoldoutText(payload);
  byId("ml-research-criterion").textContent = mlResearchCriterionText(payload);
  renderMlResearchModels(payload);
  renderMlResearchComparisons(payload);
  renderMlResearchFolds(payload);
  renderMlResearchCalibration(payload);
  const limitations = byId("ml-research-limitations");
  limitations.replaceChildren();
  for (const limitation of payload.protocol.limitations) {
    limitations.append(createElement("li", "", limitation));
  }
  limitations.append(createElement("li", "", payload.provenance.source_id_note));
  const criteria = payload.protocol.criteria;
  const environment = payload.artifact.environment;
  byId("ml-research-evidence").textContent =
    `Referencias exactas: ${criteria.references.map(mlResearchScorerLabel).join(" y ")}.` +
    ` Quantiles unilaterales ${criteria.development_lower_quantile} (desarrollo) y` +
    ` ${criteria.holdout_lower_quantile} (holdout); tolerancia de empate ${criteria.tie_tolerance}.` +
    ` Artefacto: ${formatInteger(payload.artifact.artifact_bytes)} B,` +
    ` ${formatInteger(payload.artifact.prediction_rows)} filas OOS selladas;` +
    ` fits ${payload.development ? formatInteger(payload.development.development_fits) : "—"} de desarrollo y` +
    ` ${payload.holdout ? formatInteger(payload.holdout.holdout_fits) : "—"} de holdout.` +
    ` Entorno: Python ${environment.python_version}, scikit-learn ${environment.scikit_learn_version},` +
    ` numpy ${environment.numpy_version}, scipy ${environment.scipy_version},` +
    ` duckdb ${environment.duckdb_version}, BLAS threads ${formatInteger(environment.blas_threads)}.`;
  byId("ml-research-json").textContent = JSON.stringify(payload, null, 2);
  byId("ml-research-results").classList.remove("hidden");
}

async function queryMlResearchReport(event) {
  if (event) event.preventDefault();
  const assetId = selectedMarketAsset;
  const runId = mlResearchRunId();
  if (!assetId) {
    byId("ml-research-status").textContent =
      "Selecciona un activo del catálogo antes de consultar.";
    return;
  }
  if (!mlResearchRunIdValid(runId)) {
    byId("ml-research-status").textContent =
      "El run_id debe ser un UUID canónico del recibo de evaluación.";
    return;
  }
  const sequence = ++mlResearchRequestSequence;
  if (mlResearchAbortController) mlResearchAbortController.abort();
  const controller = new AbortController();
  mlResearchAbortController = controller;
  const results = byId("ml-research-results");
  results.classList.add("hidden");
  results.setAttribute("aria-busy", "true");
  byId("ml-research-submit").disabled = true;
  byId("ml-research-status").textContent = "Verificando el artefacto sellado…";
  const parameters = new URLSearchParams({ asset_id: assetId, run_id: runId });
  try {
    const payload = await api(`/api/v1/research/ml-report?${parameters.toString()}`, {
      signal: controller.signal,
    });
    if (
      sequence !== mlResearchRequestSequence
      || controller.signal.aborted
      || assetId !== selectedMarketAsset
      || runId !== mlResearchRunId()
    ) {
      return;
    }
    renderMlResearchReport(payload, { assetId, runId });
    byId("ml-research-status").textContent =
      "Evaluación verificada desde el artefacto sellado; no se reejecutó el entrenamiento.";
  } catch (error) {
    if (sequence !== mlResearchRequestSequence || controller.signal.aborted) return;
    results.classList.add("hidden");
    byId("ml-research-status").textContent = error.message;
  } finally {
    if (sequence === mlResearchRequestSequence) {
      results.setAttribute("aria-busy", "false");
      byId("ml-research-submit").disabled = false;
    }
  }
}

function initializeMlResearchReport() {
  const requested = new URLSearchParams(window.location.search).get("ml_run_id") || "";
  const normalized = requested.trim().toLowerCase();
  if (mlResearchRunIdValid(normalized)) {
    byId("ml-research-run-id").value = normalized;
  }
  resetMlResearchReport();
  byId("ml-research-form").addEventListener("submit", (event) => {
    void queryMlResearchReport(event);
  });
  byId("ml-research-run-id").addEventListener("input", () => {
    resetMlResearchReport();
  });
}

initializeMlResearchReport();
