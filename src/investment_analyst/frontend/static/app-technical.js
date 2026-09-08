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
