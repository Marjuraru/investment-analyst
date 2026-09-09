"use strict";

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
  const assetName = safeFilePart(marketAssetPresentation().symbol.toLocaleLowerCase("es"));
  const filename = `${assetName}-mercado-${safeFilePart(marketChartPayload.period)}-${safeFilePart(marketChartPayload.known_at)}.csv`;
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
  const filename = `${safeFilePart(marketAssetPresentation().symbol)}-fundamentales-${safeFilePart(fundamentalTrendPayload.frequency)}-${safeFilePart(fundamentalTrendPayload.known_at)}.csv`;
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
  const filename = `${safeFilePart(marketAssetPresentation().symbol)}-metricas-fundamentales-${safeFilePart(request.frequency)}-${safeFilePart(request.known_at)}.csv`;
  downloadText(
    filename,
    csvDocument(columns, fundamentalResearchCsvRows(fundamentalResearchPayload)),
    "text/csv",
  );
}

function exportReportJson() {
  if (!reportPayload) return;
  const filename = `${safeFilePart(reportPayload.asset?.symbol || marketAssetPresentation().symbol)}-reporte-${safeFilePart(reportPayload.query?.known_at)}.json`;
  downloadText(filename, `${JSON.stringify(reportPayload, null, 2)}\n`, "application/json");
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

function updateMarketChartZoomState() {
  const zoomed = marketChartIsZoomed();
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
    const bollinger = point.bollinger || null;
    const bollingerUpper = bollinger ? numericValue(bollinger.upper) : null;
    const bollingerLower = bollinger ? numericValue(bollinger.lower) : null;
    if (
      open === null ||
      high === null ||
      low === null ||
      close === null ||
      volume === null ||
      (point.short_sma && shortSma === null) ||
      (point.long_sma && longSma === null) ||
      (point.third_sma && thirdSma === null)
      || (bollinger && (bollingerUpper === null || bollingerLower === null))
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
      bollingerUpper,
      bollingerLower,
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

function addDateAxis(svg, points, resolution) {
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
        BTC_INTRADAY_INTERVAL_VALUES.has(resolution)
          ? formatMarketTimestamp(points[index].timestamp)
          : formatCalendarDate(points[index].timestamp),
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
    if (item.bollingerUpper !== null) prices.push(item.bollingerUpper);
    if (item.bollingerLower !== null) prices.push(item.bollingerLower);
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
  const asset = marketAssetPresentation();
  const intraday = isIntradayInterval();
  svg.append(
    svgElement(
      "title",
      { id: "market-chart-svg-title" },
      `Histórico con puntos ${resolutionText.adjective} de ${asset.name}`,
    ),
    svgElement(
      "desc",
      { id: "market-chart-svg-description" },
      intraday
        ? `${chartSettings.chartType === "candlestick" ? "Velas OHLC" : "Línea de cierre"} ${resolutionText.adjective}, escala ${chartSettings.priceScale === "logarithmic" ? "logarítmica" : "lineal"} y barras de volumen.`
        : `${chartSettings.chartType === "candlestick" ? "Velas OHLC" : "Línea de cierre"}, SMA de ${chartSettings.shortWindow}, ${chartSettings.longWindow} y ${chartSettings.thirdWindow} ${resolutionText.plural}, escala ${chartSettings.priceScale === "logarithmic" ? "logarítmica" : "lineal"} y barras de volumen.`,
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
    ["chart-line bollinger-upper-line", values.map((item) => item.bollingerUpper)],
    ["chart-line bollinger-lower-line", values.map((item) => item.bollingerLower)],
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
  addDateAxis(svg, points, resolution);

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
    : isIntradayInterval()
      ? "No aplica"
      : "En calentamiento";
  byId("chart-point-sma-20").textContent = point.long_sma
    ? formatCurrency(point.long_sma.value)
    : isIntradayInterval()
      ? "No aplica"
      : "En calentamiento";
  byId("chart-point-sma-50").textContent = point.third_sma
    ? formatCurrency(point.third_sma.value)
    : isIntradayInterval()
      ? "No aplica"
      : "En calentamiento";
  byId("chart-point-bollinger").textContent = point.bollinger
    ? `Sup. ${formatCurrency(point.bollinger.upper)} · Media ${formatCurrency(point.bollinger.middle)} · Inf. ${formatCurrency(point.bollinger.lower)}`
    : isIntradayInterval()
      ? "No aplica"
      : "En calentamiento";
  byId("chart-point-volume").textContent = formatMarketVolume(point.volume);
  byId("chart-point-volume").title = `Valor exacto: ${point.volume} ${marketAssetPresentation().volumeLabel}`;
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
      point.bollinger
        ? `${point.bollinger.lower} / ${point.bollinger.middle} / ${point.bollinger.upper}`
        : "—",
      formatMarketVolume(point.volume, { includeUnit: false }),
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

function normalizeBtcIntradayChart(payload) {
  const asset = marketAssetPresentation();
  if (
    payload?.schema_version !== asset.intradaySchemaVersion ||
    payload.asset_id !== selectedMarketAsset ||
    payload.source_id !== asset.intradaySourceId ||
    payload.interval !== chartSettings.interval ||
    payload.lookback_hours !== 24 ||
    payload.traceability_verified !== true ||
    !Array.isArray(payload.bars)
  ) {
    throw new Error("La respuesta intradía local no coincide con la consulta solicitada.");
  }
  const points = payload.bars.map((bar) => {
    if (
      bar.asset_id !== payload.asset_id ||
      bar.source_id !== payload.source_id ||
      bar.interval !== payload.interval ||
      typeof bar.interval_complete !== "boolean"
    ) {
      throw new Error("Una vela intradía está fuera del contrato solicitado.");
    }
    return {
      resolution: bar.interval,
      period_start_timestamp: bar.period_start,
      timestamp: bar.period_start,
      period_end_timestamp: bar.period_end,
      bar_available_at: bar.available_at,
      source_session_count: bar.source_bar_count,
      calendar_interval_closed: true,
      interval_complete: bar.interval_complete,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.volume,
      trade_count: bar.trade_count,
      vwap: bar.vwap,
      quality: bar.quality,
      raw_record_ids: bar.raw_record_ids,
      open_observation_id: bar.open_observation_id,
      high_observation_id: bar.high_observation_id,
      low_observation_id: bar.low_observation_id,
      close_observation_id: bar.close_observation_id,
      volume_input_observation_ids: bar.volume_input_observation_ids,
      trade_count_input_observation_ids: bar.trade_count_input_observation_ids,
      vwap_input_observation_ids: bar.vwap_input_observation_ids,
      short_sma: null,
      long_sma: null,
      third_sma: null,
      aggregation_algorithm_version: bar.aggregation_algorithm_version,
    };
  });
  return {
    schema_version: payload.schema_version,
    asset_id: payload.asset_id,
    source_id: payload.source_id,
    known_at: payload.known_at,
    period: "24h",
    interval: payload.interval,
    resolution: payload.interval,
    resolution_policy_version: "fixed-utc-intraday-v1",
    sma_windows: [
      chartSettings.shortWindow,
      chartSettings.longWindow,
      chartSettings.thirdWindow,
    ],
    volume_unit: "BTC",
    points,
    latest_session: points.at(-1) || null,
    latest_statistics: [],
    range_statistics: {},
    coverage: {
      selected_sessions: payload.source_bar_count,
      displayed_points: points.length,
      discarded_revisions: 0,
      earliest_selected_timestamp: points[0]?.timestamp || null,
      latest_selected_timestamp: points.at(-1)?.timestamp || null,
    },
    intraday_coverage: {
      complete_interval_count: payload.complete_interval_count,
      incomplete_interval_count: payload.incomplete_interval_count,
      start: payload.start,
      end: payload.end,
    },
    traceability_verified: payload.traceability_verified,
  };
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
  byId("snapshot-volume").textContent = formatMarketVolume(latestSession.volume, {
    compact: true,
  });
  byId("snapshot-volume").title = `Valor exacto: ${latestSession.volume} ${marketAssetPresentation().volumeLabel}`;
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
    "chart-point-bollinger",
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
  const asset = marketAssetPresentation();
  const requestedPeriod = marketChartPeriod();
  const intraday = isIntradayInterval();
  const expectedSourceId = intraday ? asset.intradaySourceId : asset.sourceId;
  const expectedSchemaVersion = intraday ? asset.intradaySchemaVersion : asset.schemaVersion;
  if (
    chart.asset_id !== selectedMarketAsset ||
    chart.source_id !== expectedSourceId ||
    chart.schema_version !== expectedSchemaVersion ||
    chart.volume_unit !== asset.volumeUnit ||
    chart.period !== requestedPeriod ||
    !Array.isArray(chart.sma_windows) ||
    chart.sma_windows.length !== 3 ||
    chart.sma_windows[0] !== chartSettings.shortWindow ||
    chart.sma_windows[1] !== chartSettings.longWindow ||
    chart.sma_windows[2] !== chartSettings.thirdWindow ||
    (!intraday && (
      chart.bollinger_window !== chartSettings.bollingerWindow
      || chart.bollinger_multiplier !== chartSettings.bollingerMultiplier
    )) ||
    chart.interval !== chartSettings.interval ||
    chart.traceability_verified !== true
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
  updateMarketChartZoomState();
  const empty = byId("chart-empty");
  if (!allPoints.length) {
    resetMarketSnapshot();
    byId("chart-latest-close").textContent = "—";
    byId("chart-range-change").textContent = "—";
    byId("chart-range-change").className = "chart-change neutral";
    byId("chart-latest-date").textContent = "Sin datos locales para el corte seleccionado";
    empty.textContent = intraday
      ? "No hay velas intradía locales para este corte. Usa «Actualizar BTC-USD» para importar las últimas 24 horas."
      : "No hay historial de precios para este corte.";
    empty.classList.remove("hidden");
    byId("market-chart-card").classList.add("is-empty-state");
    byId("market-chart").replaceChildren();
    byId("chart-table-body").replaceChildren();
    resetMarketSnapshot();
    byId("chart-latest-close").textContent = "—";
    byId("chart-range-change").textContent = "—";
    byId("chart-range-change").className = "chart-change neutral";
    byId("chart-latest-date").textContent = "No fue posible consultar este activo";
    byId("chart-status").textContent = `Corte: ${formatInstant(chart.known_at)} · sin precios disponibles.`;
    return;
  }
  byId("market-chart-card").classList.remove("is-empty-state");
  empty.classList.add("hidden");
  const latestPoint = allPoints[allPoints.length - 1];
  const latest = chart.latest_session || latestPoint;
  const oneDayReturn = chartStatistic(chart, "market.history.simple_return_1d");
  const dailyChange = numericValue(oneDayReturn?.value);
  byId("chart-latest-close").textContent = formatCurrency(latest.close);
  byId("chart-latest-date").textContent = intraday
    ? `Apertura de vela: ${formatMarketTimestamp(latest.timestamp)} UTC`
    : `Cierre del ${formatCalendarDate(latest.timestamp)}`;
  const change = byId("chart-range-change");
  change.textContent = intraday
    ? `${formatInteger(chart.coverage.displayed_points)} velas locales`
    : `${formatRangeChange(dailyChange)} variación diaria`;
  change.className = `chart-change ${dailyChange > 0 ? "positive" : dailyChange < 0 ? "negative" : "neutral"}`;

  // Update asset header
  byId("asset-price").textContent = formatCurrency(latest.close);
  byId("asset-daily-change").textContent = intraday
    ? `${formatInteger(chart.coverage.displayed_points)} velas locales`
    : `${formatRangeChange(dailyChange)} diaria`;
  byId("asset-daily-change").className = `asset-change ${dailyChange > 0 ? "positive" : dailyChange < 0 ? "negative" : "neutral"}`;
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
  byId("chart-visible-sessions-label").textContent = intraday
    ? "Minutos fuente"
    : "Días con datos";
  const periodLabel = marketChartPeriodLabel(chart.period);
  byId("snapshot-range-title").textContent = periodLabel;
  renderMarketSnapshot(chart, latest, latestPoint);
  const coverageStart = intraday
    ? formatMarketTimestamp(chart.coverage.earliest_selected_timestamp)
    : formatCalendarDate(chart.coverage.earliest_selected_timestamp);
  const coverageEnd = intraday
    ? formatMarketTimestamp(chart.coverage.latest_selected_timestamp)
    : formatCalendarDate(chart.coverage.latest_selected_timestamp);
  const resolutionText = marketResolution(chart.resolution);
  byId("chart-point-period-label").textContent = resolutionText.singular;
  byId("market-chart").setAttribute(
    "aria-label",
    `Gráfico histórico interactivo de ${asset.symbol} con puntos ${resolutionText.adjective}. Usa la rueda del mouse o las teclas más y menos para cambiar el zoom, arrastra horizontalmente para desplazar la vista, cero para restablecerla y las flechas para recorrer los puntos.`,
  );
  byId("chart-data-caption").textContent = `Puntos ${resolutionText.adjective} visibles en el gráfico, ordenados cronológicamente`;
  const currentInterval = latestPoint.calendar_interval_closed ? "" : " · último intervalo en curso";
  const viewportStatus = marketChartIsZoomed()
    ? ` · mostrando ${formatInteger(points.length)} de ${formatInteger(allPoints.length)} puntos`
    : "";
  byId("chart-status").textContent = intraday
    ? `${periodLabel}: ${formatInteger(chart.coverage.selected_sessions)} minutos fuente en ${formatInteger(chart.coverage.displayed_points)} velas ${resolutionText.adjective}${viewportStatus} · ${formatInteger(chart.intraday_coverage.complete_interval_count)} completas y ${formatInteger(chart.intraday_coverage.incomplete_interval_count)} incompletas · ${coverageStart}–${coverageEnd} UTC · corte ${formatInstant(chart.known_at)}`
    : `${periodLabel}: ${formatInteger(chart.coverage.selected_sessions)} días con datos en ${formatInteger(chart.coverage.displayed_points)} puntos ${resolutionText.adjective}${viewportStatus} · fechas ${coverageStart}–${coverageEnd} · ${formatInteger(chart.coverage.discarded_revisions)} revisiones descartadas${currentInterval} · corte ${formatInstant(chart.known_at)}`;
  renderChartSvg(points, chart.resolution);
  const disclosure = byId("chart-data-disclosure");
  if (disclosure.open) renderChartTable(points);
  else byId("chart-table-body").replaceChildren();
}

function setChartBusy(busy) {
  byId("market-chart-card").setAttribute("aria-busy", String(busy));
  for (const button of document.querySelectorAll(".chart-type-button")) button.disabled = busy;
  byId("chart-interval").disabled = busy;
  updateMarketChartZoomState();
  for (const control of document.querySelectorAll("#chart-settings-form input, #chart-settings-form select, #chart-settings-form button")) {
    control.disabled = busy;
  }
}

async function queryMarketChart(deferredRequest = null) {
  const request = ++marketChartRequestSequence;
  const assetId = deferredRequest?.assetId ?? selectedMarketAsset;
  const knownAt = deferredRequest?.knownAt ?? byId("report-known-at").value.trim();
  const isLatestRequest = () => request === marketChartRequestSequence;
  const isCurrentRequest = () =>
    isLatestRequest()
    && assetId === selectedMarketAsset
    && knownAt === byId("report-known-at").value.trim()
    && (!deferredRequest || isCurrentActivoBoardRequest(deferredRequest));
  if (!isCurrentRequest()) return;
  marketChartDrag = null;
  byId("market-chart").classList.remove("is-panning");
  setChartBusy(true);
  setExportAvailable("export-market-csv", false);
  byId("chart-status").textContent = "Consultando el histórico local…";
  const requestedPeriod = marketChartPeriod();
  const intraday = isIntradayInterval();
  const parameters = new URLSearchParams({
    asset_id: assetId,
    known_at: knownAt,
    interval: chartSettings.interval,
  });
  if (!intraday) {
    parameters.set("period", requestedPeriod);
    parameters.set("short_sma_window", String(chartSettings.shortWindow));
    parameters.set("long_sma_window", String(chartSettings.longWindow));
    parameters.set("third_sma_window", String(chartSettings.thirdWindow));
    parameters.set("bollinger_window", String(chartSettings.bollingerWindow));
    parameters.set("bollinger_multiplier", chartSettings.bollingerMultiplier);
  }
  try {
    const payload = await api(
      `${intraday ? "/api/market-intraday" : "/api/market-chart"}?${parameters.toString()}`,
    );
    if (!isCurrentRequest()) return;
    renderMarketChart(intraday ? normalizeBtcIntradayChart(payload) : payload);
  } catch (error) {
    if (!isCurrentRequest()) return;
    marketChartPayload = null;
    marketChartViewport = null;
    updateMarketChartZoomState();
    setExportAvailable("export-market-csv", false);
    byId("market-chart").replaceChildren();
    byId("chart-table-body").replaceChildren();
    const empty = byId("chart-empty");
    empty.textContent = error.message;
    if (intraday && error.message.includes("No historical bars")) {
      empty.textContent =
        "No hay velas intradía locales para este corte. Usa «Actualizar BTC-USD» para importar las últimas 24 horas.";
    }
    empty.classList.remove("hidden");
    byId("chart-status").textContent = "El gráfico no pudo construirse para el corte solicitado.";
  } finally {
    if (isLatestRequest()) setChartBusy(false);
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
      const output = createElement("dd", "figure", value);
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
  const evidence = classification.evidence || [];
  byId("company-profile-requirements-summary").textContent = selected
    ? `${formatInteger(evidence.length)} series anuales utilizadas`
    : `${formatInteger(requirements.length)} datos necesarios para clasificar`;
  byId("company-profile-requirements-list").replaceChildren(
    ...(selected
      ? evidence.map((item) =>
          createElement(
            "li",
            "",
            `${FUNDAMENTAL_RESEARCH_PRESENTATION[item.metric_key]?.label || item.metric_key}: `
              + `CAGR ${formatRangeChange(numericValue(item.compound_annual_growth_rate))} · `
              + `${formatInteger(item.point_count)} períodos`,
          ),
        )
      : requirements.map((requirement) => createElement("li", "", requirement))),
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
  const unavailableGrid = byId("unavailable-metrics-grid");
  if (unavailableGrid) unavailableGrid.replaceChildren();
  let unavailableCount = 0;

  for (const section of payload.sections || []) {
    const group = createElement("section", "fundamental-research-group");
    const heading = createElement("h4", "", section.definition.display_name_es);
    heading.title = section.definition.scope_es;
    group.appendChild(heading);
    const values = createElement("div", "fundamental-research-group-grid");
    values.setAttribute("role", "list");
    let hasAvailable = false;

    for (const reference of section.definition.metric_references || []) {
      const metric = metrics.get(reference.metric_key);
      const card = fundamentalResearchMetricCard(
        reference.metric_key,
        metric,
        definitions.get(reference.metric_key),
        histories.get(reference.metric_key),
        research.request?.frequency,
      );
      card.title = `${reference.relevance_es} ${card.title || ""}`.trim();

      if (!metric) {
        if (unavailableGrid) unavailableGrid.appendChild(card);
        unavailableCount++;
      } else {
        values.appendChild(card);
        hasAvailable = true;
      }
    }
    if (hasAvailable) {
      group.appendChild(values);
      grid.appendChild(group);
    }
  }

  const unavailableDisclosure = byId("unavailable-metrics-disclosure");
  if (unavailableDisclosure) {
    if (unavailableCount > 0) {
      unavailableDisclosure.classList.remove("hidden");
      byId("unavailable-metrics-summary").textContent = `Métricas no disponibles (${unavailableCount})`;
    } else {
      unavailableDisclosure.classList.add("hidden");
    }
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
    svgElement(
      "title",
      { id: "fundamental-chart-title" },
      `Evolución fundamental de ${marketAssetPresentation().name}`,
    ),
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

async function queryFundamentalTrend(deferredRequest = null) {
  const request = ++fundamentalTrendRequestSequence;
  const assetId = deferredRequest?.assetId ?? selectedMarketAsset;
  const knownAt = deferredRequest?.knownAt ?? byId("report-known-at").value.trim();
  const frequency = selectedFundamentalFrequency;
  const isCurrentRequest = () =>
    request === fundamentalTrendRequestSequence
    && assetId === selectedMarketAsset
    && knownAt === byId("report-known-at").value.trim()
    && frequency === selectedFundamentalFrequency
    && (!deferredRequest || isCurrentActivoBoardRequest(deferredRequest));
  if (!isCurrentRequest()) return;
  setFundamentalBusy(true);
  setExportAvailable("export-fundamental-csv", false);
  byId("fundamental-status").textContent = "Consultando fundamentales locales…";
  const parameters = new URLSearchParams({
    asset_id: assetId,
    known_at: knownAt,
    frequency,
  });
  try {
    const payload = await api(`/api/fundamental-trend?${parameters.toString()}`);
    if (!isCurrentRequest()) return;
    renderFundamentalTrend(payload);
  } catch (error) {
    if (!isCurrentRequest()) return;
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

async function queryFundamentalResearch(deferredRequest = null) {
  const request = ++fundamentalResearchRequestSequence;
  const assetId = deferredRequest?.assetId ?? selectedMarketAsset;
  const knownAt = deferredRequest?.knownAt ?? byId("report-known-at").value.trim();
  const frequency = selectedFundamentalFrequency;
  const isCurrentRequest = () =>
    request === fundamentalResearchRequestSequence
    && assetId === selectedMarketAsset
    && knownAt === byId("report-known-at").value.trim()
    && frequency === selectedFundamentalFrequency
    && (!deferredRequest || isCurrentActivoBoardRequest(deferredRequest));
  if (!isCurrentRequest()) return;
  setFundamentalBusy(true);
  setExportAvailable("export-fundamental-research-csv", false);
  const parameters = new URLSearchParams({
    asset_id: assetId,
    known_at: knownAt,
    frequency,
  });
  try {
    const payload = await api(`/api/fundamental-analysis?${parameters.toString()}`);
    if (!isCurrentRequest()) return;
    renderFundamentalResearch(payload);
  } catch (error) {
    if (!isCurrentRequest()) return;
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

const VALUATION_STATUS_LABELS = Object.freeze({
  evaluated: "Calculada",
  partial: "Parcial",
  not_evaluable: "No evaluable",
  not_applicable: "No aplica",
});

const VALUATION_REASON_LABELS = Object.freeze({
  asset_not_applicable: "No aplica al tipo de activo",
  market_not_configured: "Mercado no configurado",
  fundamentals_not_configured: "Fundamentales no configurados",
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

// Reason codes produced when a market or fundamentals provider is not wired
// for this asset's class. The same condition would fire for any catalog
// asset without a configured daily-market or fundamentals source.
const BLOCKED_VALUATION_REASON_CODES = Object.freeze(
  new Set(["market_not_configured", "fundamentals_not_configured"]),
);

function valuationAbsenceKind(metric) {
  if (metric.status === "not_applicable") return "not-applicable";
  if (BLOCKED_VALUATION_REASON_CODES.has(metric.reason_code)) return "blocked";
  return "not-evaluable";
}

function valuationDisplayValue(metric, definition) {
  if (metric.status !== "evaluated") {
    const reason = VALUATION_REASON_LABELS[metric.reason_code] || metric.reason_code || "Sin evidencia suficiente";
    return renderAbsenceMark(valuationAbsenceKind(metric), VALUATION_STATUS_LABELS[metric.status] || metric.status, reason);
  }
  if (definition.unit === "USD") {
    return createElement(
      "span",
      "figure",
      formatNumber(metric.value, {
        style: "currency",
        currency: "USD",
        currencyDisplay: "narrowSymbol",
        notation: "compact",
        maximumFractionDigits: 2,
      }),
    );
  }
  if (definition.unit === "percentage") return createElement("span", "figure", formatUnsignedPercentage(metric.value));
  return createElement("span", "figure", formatMultiple(metric.value));
}

function resetValuation() {
  valuationPayload = null;
  byId("valuation-metrics").replaceChildren();
  byId("valuation-context").textContent =
    "La consulta se carga bajo demanda desde evidencia local.";
  byId("valuation-status").textContent =
    "Selecciona una fecha o abre esta sección para consultar.";
  byId("valuation-coverage").textContent = "Sin consultar";
  byId("valuation-coverage").className = "quality-chip";
  byId("valuation-price-context").textContent = "—";
  byId("valuation-period-context").textContent = "—";
  byId("valuation-filing-context").textContent = "—";
  byId("valuation-unit-context").textContent = "—";
  byId("valuation-evidence").textContent = "Sin evidencia cargada.";
  setExportAvailable("export-valuation-json", false);
  valuationHistoryPayload = null;
  valuationHistoryVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  const historySelector = byId("valuation-history-metric");
  historySelector.replaceChildren(createElement("option", "", "Todas"));
  historySelector.disabled = true;
  byId("valuation-history-summary").replaceChildren();
  byId("valuation-history-series").replaceChildren();
  byId("valuation-history-status").textContent = "No cargada.";
  setExportAvailable("export-valuation-history-json", false);
  valuationRulePayload = null;
  byId("valuation-rule-status").textContent = "No evaluada.";
  byId("valuation-rule-result").replaceChildren();
  byId("valuation-rule-evidence").textContent = "Sin evaluación cargada.";
  setExportAvailable("export-valuation-history-rule-json", false);
}

function valuationHistorySeriesKey(series) {
  return [series.metric_key, series.algorithm_version, series.unit, series.security_basis_version].join("|");
}

function selectedValuationHistorySeries(payload) {
  const selected = byId("valuation-history-metric").value;
  return (payload.series || []).filter(
    (series) => !selected || valuationHistorySeriesKey(series) === selected,
  );
}

function renderValuationHistory(
  payload,
  { preserveSelection = false, preserveWindow = false } = {},
) {
  valuationHistoryPayload = payload;
  if (!preserveWindow) valuationHistoryVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
  const selector = byId("valuation-history-metric");
  const priorSelection = preserveSelection ? selector.value : "";
  selector.replaceChildren(createElement("option", "", "Todas"));
  for (const series of payload.series || []) {
    const option = createElement("option", "", `${series.metric_key} · ${series.unit}`);
    option.value = valuationHistorySeriesKey(series);
    option.selected = option.value === priorSelection;
    selector.append(option);
  }
  selector.disabled = !payload.series?.length;
  const summary = byId("valuation-history-summary");
  summary.replaceChildren();
  const target = byId("valuation-history-series");
  target.replaceChildren();
  for (const series of selectedValuationHistorySeries(payload)) {
    const points = Array.isArray(series.points) ? series.points : [];
    const statistics = series.statistics;
    const description = createElement("dl", "valuation-history-statistics");
    for (const [label, value] of [
      ["Puntos", formatInteger(statistics.count)],
      ["Primero / último", `${statistics.first_value} / ${statistics.last_value}`],
      ["Mínimo / máximo", `${statistics.minimum} / ${statistics.maximum}`],
      ["Media Decimal", statistics.arithmetic_mean],
      ["Cambio previo", statistics.previous_change ?? "No definido"],
      ["Cambio horizonte", statistics.horizon_change ?? "No definido"],
    ]) {
      const entry = document.createElement("div");
      entry.append(createElement("dt", "", label), createElement("dd", "figure", value));
      description.append(entry);
    }
    summary.append(description);
    const table = createElement("table", "valuation-history-table");
    const caption = createElement("caption", "", `${series.metric_key} · ${series.unit}`);
    const header = document.createElement("thead");
    header.innerHTML = "<tr><th>Fecha</th><th>Valor exacto</th><th>Resultado</th></tr>";
    const body = document.createElement("tbody");
    const visibleCount = progressiveCollectionVisibleCount(
      points.length,
      valuationHistoryVisibleCount,
    );
    valuationHistoryVisibleCount = Math.max(valuationHistoryVisibleCount, visibleCount);
    for (const point of points.slice(0, visibleCount)) {
      const row = document.createElement("tr");
      row.append(
        createElement("td", "", point.valuation_date),
        createElement("td", "figure", point.value),
        createElement("td", "", point.result_id),
      );
      body.append(row);
    }
    table.append(caption, header, body);
    const status = createElement("p", "collection-status", "");
    const controls = createElement("div", "collection-controls");
    target.append(table, status, controls);
    renderProgressiveCollectionControls(
      status,
      controls,
      points.length,
      visibleCount,
      {
        label: "puntos",
        onMore: () => {
          valuationHistoryVisibleCount = visibleCount + PROGRESSIVE_COLLECTION_PAGE_SIZE;
          renderValuationHistory(payload, { preserveSelection: true, preserveWindow: true });
        },
        onLess: () => {
          valuationHistoryVisibleCount = PROGRESSIVE_COLLECTION_PAGE_SIZE;
          renderValuationHistory(payload, { preserveSelection: true, preserveWindow: true });
        },
      },
    );
  }
  byId("valuation-history-status").textContent = `${formatInteger(payload.coverage.returned_points)} puntos materializados; lectura local sin backfill.`;
  setExportAvailable("export-valuation-history-json", Boolean(payload.series?.length));
}

async function queryValuationHistory() {
  if (!marketAssetPresentation().hasCorporateValuation) return;
  const button = byId("query-valuation-history");
  setButtonBusy(button, true, "Consultando…", "Cargar historia");
  try {
    const parameters = new URLSearchParams({
      asset_id: selectedMarketAsset,
      known_at: byId("report-known-at").value.trim(),
      start_date: byId("valuation-history-start").value,
      end_date: byId("valuation-history-end").value,
      basis: "latest_annual",
      limit: "250",
    });
    renderValuationHistory(await api(`/api/v1/valuation-history?${parameters.toString()}`));
  } catch (error) {
    byId("valuation-history-status").textContent = error.message;
  } finally {
    setButtonBusy(button, false, "Consultando…", "Cargar historia");
  }
}

function exportValuationHistoryJson() {
  if (!valuationHistoryPayload) return;
  downloadText(
    `${safeFilePart(marketAssetPresentation().symbol)}-historia-valoracion-${safeFilePart(valuationHistoryPayload.request.end_date)}.json`,
    `${JSON.stringify(valuationHistoryPayload, null, 2)}\n`,
    "application/json",
  );
}

function renderValuationHistoryRule(payload) {
  valuationRulePayload = payload;
  const result = byId("valuation-rule-result");
  result.replaceChildren();
  const label = payload.status === "met" ? "Cumple la regla configurada" : payload.status === "not_met" ? "No cumple la regla configurada" : "No evaluable con la cobertura disponible";
  result.append(createElement("p", "", label));
  // "Fórmula" and "Conteos" are prose (a formula description, a sentence
  // summarizing three counts), not exact-Decimal figures, so they keep
  // the plain dd; the other two are single numeric values.
  const entries = [
    ["Fórmula", "(menores + 0.5 × iguales) / N; Decimal34", false],
    ["Percentil", payload.empirical_percentile ?? "No definido", true],
    ["Puntos previos", `${payload.coverage.prior_points} / ${payload.coverage.required_prior_points}`, true],
    ["Conteos", `${payload.lower_count} menores, ${payload.equal_count} iguales, ${payload.greater_count} mayores`, false],
  ];
  const list = createElement("dl", "valuation-history-statistics");
  for (const [name, value, isFigure] of entries) {
    const entry = document.createElement("div");
    entry.append(createElement("dt", "", name), createElement("dd", isFigure ? "figure" : "", value));
    list.append(entry);
  }
  result.append(list);
  byId("valuation-rule-status").textContent = "Lectura local de evidencia materializada; no es señal ni recomendación.";
  byId("valuation-rule-evidence").textContent = JSON.stringify(payload, null, 2);
  setExportAvailable("export-valuation-history-rule-json", true);
}

async function queryValuationHistoryRule() {
  if (!marketAssetPresentation().hasCorporateValuation) return;
  const button = byId("query-valuation-history-rule");
  setButtonBusy(button, true, "Evaluando…", "Evaluar regla");
  try {
    const parameters = new URLSearchParams({
      asset_id: selectedMarketAsset, known_at: byId("report-known-at").value.trim(),
      start_date: byId("valuation-history-start").value, end_date: byId("valuation-history-end").value,
      basis: "latest_annual", rule_id: "valuation.history.user-threshold", rule_version: "v1",
      name: "Regla histórica configurada", limitations: "Describe evidencia materializada; no predice retornos.",
      metric_key: byId("valuation-rule-metric").value.trim(), operator: byId("valuation-rule-operator").value,
      threshold: byId("valuation-rule-threshold").value.trim(), minimum_prior_points: byId("valuation-rule-minimum").value,
    });
    renderValuationHistoryRule(await api(`/api/v1/valuation-history-rule?${parameters.toString()}`));
  } catch (error) {
    byId("valuation-rule-status").textContent = error.message;
  } finally {
    setButtonBusy(button, false, "Evaluando…", "Evaluar regla");
  }
}

function exportValuationHistoryRuleJson() {
  if (!valuationRulePayload) return;
  downloadText(`${safeFilePart(marketAssetPresentation().symbol)}-regla-valoracion-${safeFilePart(valuationRulePayload.request.end_date)}.json`, `${JSON.stringify(valuationRulePayload, null, 2)}\n`, "application/json");
}

function renderValuation(payload) {
  valuationPayload = payload;
  const definitions = new Map(
    (payload.definitions || []).map((definition) => [definition.metric_key, definition]),
  );
  const metrics = byId("valuation-metrics");
  metrics.replaceChildren();
  for (const metric of payload.metrics || []) {
    const definition = definitions.get(metric.metric_key);
    if (!definition) continue;
    const card = createElement("article", `valuation-metric ${metric.status}`);
    const header = createElement("div", "valuation-metric-heading");
    header.append(
      createElement("strong", "", definition.display_name_es),
      createElement(
        "span",
        `quality-chip ${metric.status === "evaluated" ? "good" : "warn"}`,
        VALUATION_STATUS_LABELS[metric.status] || metric.status,
      ),
    );
    const value = createElement("p", `valuation-metric-value ${metric.status}`);
    value.append(valuationDisplayValue(metric, definition));
    if (metric.value !== null && metric.value !== undefined) {
      value.title = `Valor exacto: ${metric.value} ${definition.unit}`;
    }
    const formula = createElement("small", "valuation-formula", definition.formula);
    const evidence = createElement(
      "small",
      "valuation-input-summary",
      metric.status === "evaluated"
        ? `${formatInteger(metric.input_observation_ids.length)} inputs · disponible ${formatInstant(metric.available_at)}`
        : `${formatInteger(metric.input_observation_ids.length)} inputs elegibles`,
    );
    card.append(header, value, formula, evidence);
    metrics.append(card);
  }
  const coverage = payload.coverage;
  const coverageChip = byId("valuation-coverage");
  coverageChip.textContent = `${formatInteger(coverage.evaluated)}/${formatInteger(coverage.total)} calculadas`;
  coverageChip.className = `quality-chip ${payload.status === "evaluated" ? "good" : "warn"}`;
  byId("valuation-context").textContent =
    `${marketAssetPresentation().symbol} · corte ${formatInstant(payload.known_at)} · ${VALUATION_STATUS_LABELS[payload.status] || payload.status}`;
  byId("valuation-status").textContent =
    `${formatInteger(coverage.evaluated)} calculadas · ${formatInteger(coverage.not_evaluable)} no evaluables · lectura local sin proveedores.`;
  byId("valuation-price-context").textContent = payload.valuation_as_of
    ? `${formatCalendarDate(payload.valuation_as_of)} · ${formatInteger(payload.price_age_days)} días de antigüedad · ${payload.price_source_id}`
    : "No disponible en el corte";
  byId("valuation-period-context").textContent = payload.annual_period_end
    ? `${formatCalendarDate(payload.annual_period_start)}–${formatCalendarDate(payload.annual_period_end)} · ${payload.fiscal_period || "FY"}`
    : "No disponible en el corte";
  byId("valuation-filing-context").textContent = payload.filing_accepted_at
    ? `${payload.filing_form || "Filing"} ${payload.filing_accession_number || ""} · ${formatInstant(payload.filing_accepted_at)}`
    : "No disponible en el corte";
  byId("valuation-unit-context").textContent = payload.security_basis
    ? `${payload.price_currency}/${payload.report_currency} · ${payload.security_basis.basis} · factor exacto ${payload.security_basis.market_units_per_reported_share}`
    : "Base de título no disponible";
  byId("valuation-evidence").textContent = JSON.stringify(payload, null, 2);
  setExportAvailable("export-valuation-json", true);
}

async function queryValuation() {
  if (!marketAssetPresentation().hasCorporateValuation) return;
  const card = byId("valuation-card");
  const button = byId("query-valuation");
  card.setAttribute("aria-busy", "true");
  setButtonBusy(button, true, "Consultando…", "Cargar valoración");
  byId("valuation-status").textContent = "Reconstruyendo la valoración desde evidencia local…";
  const parameters = new URLSearchParams({
    asset_id: selectedMarketAsset,
    known_at: byId("report-known-at").value.trim(),
    valuation_date: byId("valuation-date").value,
    basis: "latest_annual",
  });
  try {
    renderValuation(await api(`/api/v1/valuation?${parameters.toString()}`));
  } catch (error) {
    resetValuation();
    byId("valuation-status").textContent = error.message;
  } finally {
    card.setAttribute("aria-busy", "false");
    setButtonBusy(button, false, "Consultando…", "Cargar valoración");
  }
}

function exportValuationJson() {
  if (!valuationPayload) return;
  downloadText(
    `${safeFilePart(marketAssetPresentation().symbol)}-valoracion-${safeFilePart(valuationPayload.request.valuation_date)}.json`,
    `${JSON.stringify(valuationPayload, null, 2)}\n`,
    "application/json",
  );
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

function resetListedCompanyReport() {
  reportPayload = null;
  listedCompanyReportRequest += 1;
  setExportAvailable("export-report-json", false);
  const reportArea = byId("report-area");
  reportArea.classList.add("hidden");
  reportArea.setAttribute("aria-busy", "false");
  byId("report-status").replaceChildren();
  byId("market-report").replaceChildren();
  byId("fundamental-report").replaceChildren();
  byId("report-traceability").textContent = "";
  byId("report-limitations").replaceChildren();
  byId("report-json").textContent = "";
}

byId("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("run-button");
  const presentation = marketAssetPresentation();
  const refreshIntraday = isIntradayInterval();
  const idleLabel = presentation.hasFundamentals
    ? "Ejecutar actualización"
    : presentation.refreshLabel;
  marketStartByAsset.set(selectedMarketAsset, byId("market-start").value);
  setButtonBusy(button, true, "Ejecutando…", idleLabel);
  setMessage(
    presentation.hasFundamentals
      ? "La actualización puede tardar. SEC se consulta en cada ejecución."
      : refreshIntraday
        ? "Actualizando primero el histórico diario y después 24 horas de velas intradía…"
        : `Actualizando velas diarias y estadísticas de ${presentation.symbol}…`,
  );
  const knownAt = byId("run-known-at").value.trim();
  try {
      const payload = {
        asset_id: selectedMarketAsset,
        market_start: byId("market-start").value,
        market_end: byId("market-end").value,
        refresh_mode: byId("refresh-mode").value,
        requested_known_at: knownAt || null,
      };
      const summary = await api("/api/market-refresh", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      let effectiveKnownAt = summary.effective_known_at;
      let intradaySummary = null;
      if (refreshIntraday) {
        intradaySummary = await api("/api/market-intraday-refresh", {
          method: "POST",
          body: JSON.stringify({
            asset_id: selectedMarketAsset,
            hours: 24,
            requested_end: null,
          }),
        });
        effectiveKnownAt = intradaySummary.retrieved_at;
      }
      let fundamentalSummary = null;
      if (presentation.hasFundamentals) {
        fundamentalSummary = await api("/api/fundamental-refresh", {
          method: "POST",
          body: JSON.stringify({
            asset_id: selectedMarketAsset,
            frequency: byId("run-frequency").value,
            requested_known_at: knownAt || null,
          }),
        });
        effectiveKnownAt = fundamentalSummary.effective_known_at;
      }
      byId("report-known-at").value = effectiveKnownAt;
      knownAtByAsset.set(selectedMarketAsset, effectiveKnownAt);
      const mode = translated(
        summary.refresh_plan.mode,
        STATUS_LABELS,
        summary.refresh_plan.mode,
      );
      const intradayText = intradaySummary
        ? ` ${formatInteger(intradaySummary.candles_received)} velas de 1 minuto procesadas; `
          + `${formatInteger(intradaySummary.raw_records_created)} nuevas y `
          + `${formatInteger(intradaySummary.raw_records_reused)} reutilizadas.`
        : "";
      const dailyBars = summary.candles_received ?? summary.bars_received ?? 0;
      const fundamentalText = fundamentalSummary
        ? ` ${formatInteger(fundamentalSummary.metric_results_created)} métricas fundamentales `
          + `y ${formatInteger(fundamentalSummary.diagnostics_created)} diagnóstico procesados.`
        : "";
      setMessage(
        `${mode}. ${formatInteger(dailyBars)} velas diarias procesadas; `
        + `${formatInteger(summary.metric_results_created)} métricas nuevas.${intradayText} `
        + `${fundamentalText} Trazabilidad verificada.`,
      );
      resetListedCompanyReport();
      invalidateDeferredBoardLoads();
      activateBoard(boardIdFromLocationHash(), { focus: false });
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setButtonBusy(button, false, "Ejecutando…", idleLabel);
  }
});

async function queryReport() {
  const deferredRequest = arguments[0] || null;
  const button = byId("report-button");
  const reportArea = byId("report-area");
  const assetId = deferredRequest?.assetId ?? selectedMarketAsset;
  const knownAt = deferredRequest?.knownAt ?? byId("report-known-at").value.trim();
  if (deferredRequest && !isCurrentActivoBoardRequest(deferredRequest)) return;
  const presentation = marketAssets[assetId];
  if (!presentation?.hasFundamentals) {
    resetListedCompanyReport();
    return;
  }
  const request = ++listedCompanyReportRequest;
  const isLatestRequest = () => request === listedCompanyReportRequest;
  const isCurrentRequest = () =>
    isLatestRequest()
    && assetId === selectedMarketAsset
    && knownAt === byId("report-known-at").value.trim()
    && (!deferredRequest || isCurrentActivoBoardRequest(deferredRequest));
  reportPayload = null;
  setExportAvailable("export-report-json", false);
  setButtonBusy(button, true, "Consultando…", "Consultar análisis");
  reportArea.setAttribute("aria-busy", "true");
  const parameters = new URLSearchParams({
    known_at: knownAt,
    fundamental_frequency: byId("report-frequency").value,
  });
  if (byId("market-as-of").value) parameters.set("market_as_of", byId("market-as-of").value);
  if (byId("fundamental-as-of").value) {
    parameters.set("fundamental_as_of", byId("fundamental-as-of").value);
  }
  try {
    parameters.set("asset_id", assetId);
    const report = await api(`/api/listed-company-report?${parameters.toString()}`);
    if (
      request !== listedCompanyReportRequest
      || assetId !== selectedMarketAsset
      || knownAt !== byId("report-known-at").value.trim()
      || (deferredRequest && !isCurrentActivoBoardRequest(deferredRequest))
      || report?.asset?.asset_id !== assetId
    ) return;
    renderReport(report);
    setMessage(operationalIssues.join(" · "), operationalIssues.length > 0);
  } catch (error) {
    if (!isCurrentRequest()) return;
    setMessage(error.message, true);
  } finally {
    if (isCurrentRequest()) {
      reportArea.setAttribute("aria-busy", "false");
      setButtonBusy(button, false, "Consultando…", "Consultar análisis");
    }
  }
}
