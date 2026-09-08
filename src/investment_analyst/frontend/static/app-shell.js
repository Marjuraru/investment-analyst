"use strict";

const NEW_YORK_WEEKDAY_ORDER = Object.freeze(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]);

const NEW_YORK_DATE_PARTS_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: NEW_YORK_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function newYorkWallClockDateParts(instant) {
  const parts = Object.fromEntries(
    NEW_YORK_DATE_PARTS_FORMATTER.formatToParts(instant)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
  };
}

// Resolves the UTC instant (epoch ms) of a New York wall-clock date/time by
// re-checking which offset the guess actually lands in, instead of assuming
// a fixed UTC offset -- correct on both sides of a DST transition, where the
// New York civil day is 23h or 25h long rather than 24h.
function newYorkWallClockToInstant(year, month, day, hour, minute) {
  let guessMs = Date.UTC(year, month - 1, day, hour, minute);
  const desiredMs = Date.UTC(year, month - 1, day, hour, minute);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const observed = newYorkWallClockDateParts(guessMs);
    const observedMs = Date.UTC(
      observed.year,
      observed.month - 1,
      observed.day,
      observed.hour,
      observed.minute,
    );
    const driftMs = desiredMs - observedMs;
    if (driftMs === 0) break;
    guessMs += driftMs;
  }
  return guessMs;
}

// Real elapsed minutes from `now` to a New York wall-clock target that is
// `daysAhead` calendar days out, at `targetMinutesSinceMidnight` local time.
// Uses the actual UTC instant of that target (see newYorkWallClockToInstant)
// rather than `daysAhead * 24 * 60`, so a countdown spanning a DST
// transition does not drift by an hour.
function minutesUntilNewYorkWallClock(now, daysAhead, targetMinutesSinceMidnight) {
  const today = newYorkWallClockDateParts(now);
  const targetInstantMs = newYorkWallClockToInstant(
    today.year,
    today.month,
    today.day + daysAhead,
    Math.floor(targetMinutesSinceMidnight / 60),
    targetMinutesSinceMidnight % 60,
  );
  return Math.round((targetInstantMs - now.getTime()) / 60_000);
}

function newYorkRegularSessionState(now) {
  const parts = Object.fromEntries(
    NEW_YORK_SESSION_PARTS_FORMATTER.formatToParts(now)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  if (parts.weekday === "Sat" || parts.weekday === "Sun") {
    return NYSE_SESSION_STATES.weekend;
  }
  const minutes = Number(parts.hour) * 60 + Number(parts.minute);
  if (minutes < NYSE_CORE_OPEN_MINUTES) {
    return NYSE_SESSION_STATES.before;
  }
  if (minutes < NYSE_CORE_CLOSE_MINUTES) {
    return NYSE_SESSION_STATES.open;
  }
  return NYSE_SESSION_STATES.after;
}

// Minutes until the next regular-session boundary (open or close), consuming
// only NYSE_CORE_OPEN_MINUTES/NYSE_CORE_CLOSE_MINUTES as already declared.
// Regular session only: no holiday or early-close calendar is modeled.
function newYorkRegularSessionRemainingMinutes(now) {
  const parts = Object.fromEntries(
    NEW_YORK_SESSION_PARTS_FORMATTER.formatToParts(now)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  const minutesSinceMidnight = Number(parts.hour) * 60 + Number(parts.minute);
  const weekdayIndex = NEW_YORK_WEEKDAY_ORDER.indexOf(parts.weekday);
  if (parts.weekday === "Sat" || parts.weekday === "Sun") {
    const daysToMonday = parts.weekday === "Sat" ? 2 : 1;
    return {
      toward: "open",
      minutes: minutesUntilNewYorkWallClock(now, daysToMonday, NYSE_CORE_OPEN_MINUTES),
    };
  }
  if (minutesSinceMidnight < NYSE_CORE_OPEN_MINUTES) {
    return { toward: "open", minutes: NYSE_CORE_OPEN_MINUTES - minutesSinceMidnight };
  }
  if (minutesSinceMidnight < NYSE_CORE_CLOSE_MINUTES) {
    return { toward: "close", minutes: NYSE_CORE_CLOSE_MINUTES - minutesSinceMidnight };
  }
  const daysToNextOpen = weekdayIndex === 5 /* Fri */ ? 3 : 1;
  return {
    toward: "open",
    minutes: minutesUntilNewYorkWallClock(now, daysToNextOpen, NYSE_CORE_OPEN_MINUTES),
  };
}

function formatSessionCountdown(totalMinutes) {
  const days = Math.floor(totalMinutes / (24 * 60));
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60);
  const minutes = totalMinutes % 60;
  const segments = [];
  if (days > 0) segments.push(`${days} d`);
  if (days > 0 || hours > 0) segments.push(`${hours} h`);
  segments.push(`${minutes} min`);
  return segments.join(" ");
}

function limaWallClockDateParts(instant) {
  const parts = Object.fromEntries(
    LIMA_DATE_PARTS_FORMATTER.formatToParts(instant)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    weekday: parts.weekday,
  };
}

function nthSundayOfMonth(year, month, occurrence) {
  const firstDay = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  return 1 + ((7 - firstDay) % 7) + (occurrence - 1) * 7;
}

function bvlSessionPeriodForDate(parts) {
  const date = Date.UTC(parts.year, parts.month - 1, parts.day);
  const summerStart = Date.UTC(
    parts.year,
    2,
    nthSundayOfMonth(parts.year, 3, 2),
  );
  const winterStart = Date.UTC(
    parts.year,
    10,
    nthSundayOfMonth(parts.year, 11, 1),
  );
  return date >= summerStart && date < winterStart
    ? BVL_SESSION_PERIODS.summer
    : BVL_SESSION_PERIODS.winter;
}

function bvlRegularSessionState(now) {
  const parts = limaWallClockDateParts(now);
  const period = bvlSessionPeriodForDate(parts);
  if (parts.weekday === "Sat" || parts.weekday === "Sun") {
    return { ...BVL_SESSION_STATES.weekend, period };
  }
  const minutes = parts.hour * 60 + parts.minute;
  if (minutes < period.open) return { ...BVL_SESSION_STATES.before, period };
  if (minutes < period.close) return { ...BVL_SESSION_STATES.open, period };
  return { ...BVL_SESSION_STATES.after, period };
}

function renderMarketClocks(now = new Date()) {
  const instant = now.toISOString();
  for (const definition of MARKET_CLOCK_DEFINITIONS) {
    const formatters = MARKET_CLOCK_FORMATTERS.get(definition.timeZone);
    const timeElement = byId(definition.timeElementId);
    timeElement.dateTime = instant;
    timeElement.textContent = formatters.time.format(now);
    byId(definition.dateElementId).textContent = formatters.date.format(now);
  }
  const bvlSession = bvlRegularSessionState(now);
  const bvlStatus = byId("bvl-session-status");
  const bvlDot = byId("bvl-session-dot");
  bvlStatus.replaceChildren(bvlDot, document.createTextNode(bvlSession.label));
  bvlStatus.className = `market-session-status ${bvlSession.tone}`;
  byId("bvl-session-remaining").textContent = `${bvlSession.period.label} America/Lima`;
  const session = newYorkRegularSessionState(now);
  const status = byId("nyse-session-status");
  const dot = byId("nyse-session-dot");
  status.replaceChildren(dot, document.createTextNode(session.label));
  status.className = `market-session-status ${session.tone}`;
  const remaining = newYorkRegularSessionRemainingMinutes(now);
  const countdown = formatSessionCountdown(remaining.minutes);
  byId("nyse-session-remaining").textContent =
    remaining.toward === "open" ? `Abre en ${countdown}` : `Cierra en ${countdown}`;
}

let marketClockTimer = null;
let overviewTimer = null;
let overviewRequestActive = false;
let overviewFailureCount = 0;

function startMarketClocks() {
  if (marketClockTimer !== null) {
    window.clearTimeout(marketClockTimer);
    marketClockTimer = null;
  }
  const now = new Date();
  renderMarketClocks(now);
  if (!document.hidden) {
    const delay = MARKET_CLOCK_REFRESH_MS - (now.getTime() % MARKET_CLOCK_REFRESH_MS) + 25;
    marketClockTimer = window.setTimeout(startMarketClocks, delay);
  }
}

function scheduleOverviewRefresh() {
  if (overviewTimer !== null) {
    window.clearTimeout(overviewTimer);
    overviewTimer = null;
  }
  if (document.hidden) return;
  const delay = Math.min(
    OVERVIEW_REFRESH_MS * (2 ** overviewFailureCount),
    OVERVIEW_MAX_BACKOFF_MS,
  );
  overviewTimer = window.setTimeout(
    () => refreshOverview({ manual: false }),
    delay,
  );
}

async function refreshOverview({ manual = false } = {}) {
  if (overviewRequestActive) return;
  overviewRequestActive = true;
  const button = byId("refresh-overview");
  if (manual) setButtonBusy(button, true, "Verificando…", "Verificar");
  try {
    applyOverview(await api("/api/v1/overview"));
    overviewFailureCount = 0;
  } catch (error) {
    overviewFailureCount += 1;
    if (manual) setMessage(error.message, true);
    badge(byId("health-badge"), "Sin conexión", "bad");
  } finally {
    overviewRequestActive = false;
    if (manual) setButtonBusy(button, false, "Verificando…", "Verificar");
    scheduleOverviewRefresh();
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

byId("refresh-overview").addEventListener(
  "click",
  () => refreshOverview({ manual: true }),
);
byId("asset-preferences-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveAssetPreferences();
});
document.addEventListener("visibilitychange", () => {
  startMarketClocks();
  if (document.hidden) {
    if (overviewTimer !== null) window.clearTimeout(overviewTimer);
    overviewTimer = null;
  } else {
    refreshOverview({ manual: false });
  }
});
byId("export-market-csv").addEventListener("click", exportMarketCsv);
byId("export-fundamental-csv").addEventListener("click", exportFundamentalCsv);
byId("export-fundamental-research-csv").addEventListener(
  "click",
  exportFundamentalResearchCsv,
);
byId("query-valuation").addEventListener("click", queryValuation);
byId("query-valuation-history").addEventListener("click", queryValuationHistory);
byId("query-valuation-history-rule").addEventListener("click", queryValuationHistoryRule);
byId("export-valuation-json").addEventListener("click", exportValuationJson);
byId("export-valuation-history-json").addEventListener("click", exportValuationHistoryJson);
byId("export-valuation-history-rule-json").addEventListener("click", exportValuationHistoryRuleJson);
byId("valuation-history-metric").addEventListener("change", () => {
  if (valuationHistoryPayload) renderValuationHistory(valuationHistoryPayload, { preserveSelection: true });
});
byId("export-report-json").addEventListener("click", exportReportJson);
byId("chart-data-disclosure").addEventListener("toggle", (event) => {
  if (event.currentTarget.open && marketChartPayload?.points) {
    renderChartTable(visibleMarketChartPoints());
  }
});
byId("market-chart").addEventListener("wheel", handleMarketChartWheel, { passive: false });
byId("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  const previousDefaults = DEFAULT_SMA_COLORS;
  applyTheme(next);
  persistTheme(next);
  captureDefaultSmaColors();
  // Only follow the new theme's SMA colors if the user never customized
  // them away from the previous theme's defaults; an explicit user choice
  // is never overwritten by a theme switch.
  const usingPreviousThemeDefaults =
    chartSettings.shortColor === previousDefaults.shortColor &&
    chartSettings.longColor === previousDefaults.longColor &&
    chartSettings.thirdColor === previousDefaults.thirdColor;
  if (usingPreviousThemeDefaults) {
    chartSettings = { ...chartSettings, ...DEFAULT_SMA_COLORS };
    applyChartSettings();
    persistChartSettings();
    if (marketChartPayload !== null) {
      renderMarketChart(marketChartPayload, { preserveViewport: true });
    }
  }
});



for (const button of document.querySelectorAll(".series-toggle")) {
  button.addEventListener("click", () => {
    const series = button.dataset.series;
    chartSeriesVisibility[series] = !chartSeriesVisibility[series];
    button.setAttribute("aria-pressed", String(chartSeriesVisibility[series]));
    applySeriesVisibility();
  });
}

for (const button of document.querySelectorAll(".chart-type-button")) {
  button.addEventListener("click", () => {
    if (button.dataset.chartType === chartSettings.chartType) return;
    chartSettings = { ...chartSettings, chartType: button.dataset.chartType };
    applyChartSettings();
    persistChartSettings();
    if (marketChartPayload !== null) {
      renderMarketChart(marketChartPayload, { preserveViewport: true });
    }
  });
}

byId("chart-interval").addEventListener("change", async (event) => {
  chartSettings = { ...chartSettings, interval: event.target.value };
  applySelectedMarketAsset();
  applyChartSettings();
  persistChartSettings();
  await queryMarketChart();
});

byId("chart-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const candidate = normalizeChartSettings({
    shortWindow: byId("sma-short-window").valueAsNumber,
    longWindow: byId("sma-long-window").valueAsNumber,
    thirdWindow: byId("sma-third-window").valueAsNumber,
    bollingerWindow: byId("bollinger-window").valueAsNumber,
    bollingerMultiplier: byId("bollinger-multiplier").value,
    shortColor: byId("sma-short-color").value,
    longColor: byId("sma-long-color").value,
    thirdColor: byId("sma-third-color").value,
    priceScale: byId("chart-price-scale").value,
    chartType: chartSettings.chartType,
    interval: chartSettings.interval,
  });
  const error = byId("chart-settings-error");
  if (candidate === null) {
    error.textContent =
      "Usa ventanas enteras ordenadas y Bollinger 2–400 con multiplicador exacto positivo.";
    error.classList.remove("hidden");
    return;
  }
  error.classList.add("hidden");
  const requiresDataRefresh =
    candidate.shortWindow !== chartSettings.shortWindow ||
    candidate.longWindow !== chartSettings.longWindow ||
    candidate.thirdWindow !== chartSettings.thirdWindow ||
    candidate.bollingerWindow !== chartSettings.bollingerWindow ||
    candidate.bollingerMultiplier !== chartSettings.bollingerMultiplier ||
    candidate.interval !== chartSettings.interval;
  chartSettings = candidate;
  applyChartSettings();
  persistChartSettings();
  byId("chart-settings").open = false;
  if (requiresDataRefresh || marketChartPayload === null) await queryMarketChart();
  else renderMarketChart(marketChartPayload, { preserveViewport: true });
});

byId("chart-settings-reset").addEventListener("click", async () => {
  const requiresDataRefresh =
    chartSettings.shortWindow !== DEFAULT_CHART_SETTINGS.shortWindow ||
    chartSettings.longWindow !== DEFAULT_CHART_SETTINGS.longWindow ||
    chartSettings.thirdWindow !== DEFAULT_CHART_SETTINGS.thirdWindow ||
    chartSettings.bollingerWindow !== DEFAULT_CHART_SETTINGS.bollingerWindow ||
    chartSettings.bollingerMultiplier !== DEFAULT_CHART_SETTINGS.bollingerMultiplier ||
    chartSettings.interval !== DEFAULT_CHART_SETTINGS.interval;
  chartSettings = { ...DEFAULT_CHART_SETTINGS, ...DEFAULT_SMA_COLORS };
  byId("chart-settings-error").classList.add("hidden");
  applyChartSettings();
  persistChartSettings();
  byId("chart-settings").open = false;
  if (requiresDataRefresh || marketChartPayload === null) await queryMarketChart();
  else renderMarketChart(marketChartPayload, { preserveViewport: true });
});

for (const button of document.querySelectorAll(".frequency-button")) {
  button.addEventListener("click", async () => {
    selectFundamentalFrequency(button.dataset.frequency);
    await Promise.all([
      queryFundamentalTrend(),
      queryFundamentalResearch(),
      ...(marketAssetPresentation().hasFundamentals ? [queryReport()] : []),
    ]);
  });
}

initializeAssetSubtabs();

byId("valuation-nav-link").addEventListener("click", () => {
  if (valuationPayload === null) void queryValuation();
});

byId("candidate-notification-panel").addEventListener("toggle", (event) => {
  if (event.currentTarget.open) void loadCandidateNotifications();
});

byId("screening-rules-panel").addEventListener("toggle", (event) => {
  if (event.currentTarget.open) void loadScreeningRules();
});

byId("crypto-derivatives-panel").addEventListener("toggle", (event) => {
  if (event.currentTarget.open && cryptoDerivativesPayload === null) void queryCryptoDerivatives();
});

byId("report-known-at").addEventListener("change", () => {
  resetListedCompanyReport();
  invalidateDeferredBoardLoads();
  if (marketAssetPresentation().supportsCryptoDerivatives && byId("crypto-derivatives-panel").open) {
    cryptoDerivativesPayload = null;
    void queryCryptoDerivatives();
  }
  activateBoard(boardIdFromLocationHash(), { focus: false });
});
byId("market-comparison-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await queryMarketComparison();
  } catch (error) {
    byId("comparison-status").textContent = error.message;
  }
});

byId("comparison-benchmark").addEventListener("change", () => {
  syncComparisonAssetOptions();
});

const yesterday = new Date();
yesterday.setUTCDate(yesterday.getUTCDate() - 1);
byId("market-end").value = yesterday.toISOString().slice(0, 10);
byId("valuation-date").value = yesterday.toISOString().slice(0, 10);
byId("valuation-history-start").value = `${yesterday.getUTCFullYear() - 3}-01-01`;
byId("valuation-history-end").value = yesterday.toISOString().slice(0, 10);
byId("report-known-at").value = new Date().toISOString();
byId("comparison-end").value = yesterday.toISOString().slice(0, 10);
const comparisonStart = new Date(yesterday);
comparisonStart.setUTCFullYear(comparisonStart.getUTCFullYear() - 1);
byId("comparison-start").value = comparisonStart.toISOString().slice(0, 10);

byId("sidebar-toggle").addEventListener("click", () => {
  const sidebar = byId("app-sidebar");
  const workspace = document.querySelector(".workspace");
  const toggle = byId("sidebar-toggle");

  sidebar.classList.toggle("collapsed");
  workspace.classList.toggle("sidebar-collapsed");

  const isCollapsed = sidebar.classList.contains("collapsed");
  toggle.setAttribute("aria-expanded", String(!isCollapsed));
  toggle.setAttribute("aria-label", isCollapsed ? "Expandir navegación" : "Colapsar navegación");
});

// Board shell (UI-2): the single source of truth for the six-board
// navigation, routing and built/not-built state. Nothing else declares a
// board id, label or not-built reason -- renderBoardNav() and
// renderNotBuiltBoards() below read exclusively from this array, so the
// nav, the routing table and the not-built grammar can never drift apart.
const BOARD_REGISTRY = Object.freeze([
  { id: "mesa", label: "Mesa", icon: "grid", built: true },
  { id: "activo", label: "Activo", icon: "trending", built: true },
  { id: "tecnico", label: "Técnico", icon: "bars", built: true },
  { id: "revisar", label: "Revisar", icon: "inbox", built: true },
  { id: "cazatiburones", label: "Cazatiburones", icon: "eye", built: true },
  { id: "sistema", label: "Sistema", icon: "gear", built: true },
]);

// UI-5: the only board-to-request graph. Function references keep the
// declared graph and the activation dispatcher together without introducing
// another endpoint, parameter or transport contract.
const BOARD_DEFERRED_LOADS = Object.freeze({
  mesa: Object.freeze([
    refreshOverview,
    loadMesaAnalyticalNews,
    loadMesaCazatiburonesNewsFamilies,
    loadMesaIncidents,
    loadMesaUniverseCoverage,
  ]),
  activo: Object.freeze([
    queryReport,
    queryMarketChart,
    queryFundamentalTrend,
    queryFundamentalResearch,
  ]),
  tecnico: Object.freeze([]),
  revisar: Object.freeze([loadCandidateInbox, loadAlertInbox]),
  cazatiburones: Object.freeze([loadCazatiburonesUniverseIndex, loadCazatiburonesBoard]),
  sistema: Object.freeze([]),
});

const DEFAULT_BOARD_ID = BOARD_REGISTRY[0].id;

function isCurrentActivoBoardRequest(deferredRequest) {
  return (
    deferredRequest.sequence === activoBoardRequestSequence
    && deferredRequest.assetId === selectedMarketAsset
    && deferredRequest.knownAt === byId("report-known-at").value.trim()
  );
}

function invalidateDeferredBoardLoads() {
  loadedBoardIds.clear();
  activoBoardRequestSequence += 1;
  marketChartRequestSequence += 1;
  fundamentalTrendRequestSequence += 1;
  fundamentalResearchRequestSequence += 1;
  cryptoDerivativesRequest += 1;
  mesaInstitutionalNewsRequestSequence += 1;
  mesaActivityNewsRequestSequence += 1;
  mesaUniverseCoverageRequestSequence += 1;
  cazatiburonesUniverseRequestSequence += 1;
  cazatiburonesRequestSequence += 1;
}

function loadDeferredBoardData(boardId) {
  if (!boardDataReady || loadedBoardIds.has(boardId)) return;
  const loaders = BOARD_DEFERRED_LOADS[boardId];
  if (!loaders) return;
  loadedBoardIds.add(boardId);
  if (boardId === "activo") {
    const deferredRequest = Object.freeze({
      sequence: ++activoBoardRequestSequence,
      assetId: selectedMarketAsset,
      knownAt: byId("report-known-at").value.trim(),
    });
    for (const load of loaders) void load(deferredRequest);
    return;
  }
  for (const load of loaders) void load();
}

// Feather-style stroke icon paths, one per BOARD_REGISTRY.icon value. "grid"
// and "gear" are copied verbatim from the pre-existing Resumen/Operación
// nav-link glyphs they replace; the rest are new but follow the same
// viewBox/stroke convention as every other inline icon in this file.
const BOARD_NAV_ICON_PATHS = Object.freeze({
  grid: '<rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect>',
  trending: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>',
  bars: '<line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line>',
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>',
  gear: '<circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>',
});

function isKnownBoardId(boardId) {
  return BOARD_REGISTRY.some((board) => board.id === boardId);
}

function boardIdFromLocationHash() {
  const raw = window.location.hash.replace(/^#/, "");
  return isKnownBoardId(raw) ? raw : DEFAULT_BOARD_ID;
}

function renderBoardNav() {
  const nav = byId("board-nav");
  nav.replaceChildren();
  for (const board of BOARD_REGISTRY) {
    const link = document.createElement("a");
    link.className = "nav-link board-nav-link";
    link.href = `#${board.id}`;
    link.dataset.board = board.id;
    if (!board.built) link.title = `${board.label} · aún no construido`;
    link.innerHTML =
      `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ` +
      `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
      `${BOARD_NAV_ICON_PATHS[board.icon]}</svg>` +
      `<span class="nav-text">${board.label}</span>`;
    nav.append(link);
  }
}

// cazatiburones is the only not-built board in this Work Block, but this
// stays generic over every BOARD_REGISTRY entry with built === false so a
// future not-built board never needs a second code path.
function renderNotBuiltBoards() {
  for (const board of BOARD_REGISTRY) {
    if (board.built) continue;
    const container = byId(`board-${board.id}-not-built`);
    if (!container) continue;
    const wrapper = createElement("div", "board-not-built");
    wrapper.setAttribute("role", "status");
    const icon = createElement("span", "board-not-built-icon");
    icon.setAttribute("aria-hidden", "true");
    const copy = createElement("div", "board-not-built-copy");
    copy.append(
      createElement("strong", "board-not-built-label", `${board.label} · aún no construido`),
      createElement("p", "board-not-built-reason", board.reason),
    );
    wrapper.append(icon, copy);
    container.replaceChildren(wrapper);
  }
}

// activateBoard() owns board visibility, board-nav state and placement of
// the one asset scope bar. The tab selector itself neither mutates the
// location hash nor dispatches a board activation.
function activateBoard(boardId, { focus = true } = {}) {
  const resolvedId = isKnownBoardId(boardId) ? boardId : DEFAULT_BOARD_ID;
  for (const board of BOARD_REGISTRY) {
    const section = byId(`board-${board.id}`);
    if (section) section.hidden = board.id !== resolvedId;
  }
  for (const link of document.querySelectorAll(".board-nav-link")) {
    const isActive = link.dataset.board === resolvedId;
    link.classList.toggle("active", isActive);
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  renderAssetScopeForBoard(resolvedId);
  if (window.location.hash.replace(/^#/, "") !== resolvedId) {
    history.replaceState(null, "", `#${resolvedId}`);
  }
  const activeSection = byId(`board-${resolvedId}`);
  if (focus && activeSection) activeSection.focus({ preventScroll: true });
  if (boardDataReady) loadDeferredBoardData(resolvedId);
}

function initializeBoardShell() {
  renderBoardNav();
  renderNotBuiltBoards();
  activateBoard(boardIdFromLocationHash(), { focus: false });
  for (const link of document.querySelectorAll(".board-nav-link")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      activateBoard(link.dataset.board);
    });
  }
  window.addEventListener("hashchange", () => {
    activateBoard(boardIdFromLocationHash(), { focus: false });
  });
}

async function initialize() {
  initializeBoardShell();
  initializeTheme();
  captureDefaultSmaColors();
  await loadMarketAssets();
  await loadAssetPreferences();
  initializeChartSettings();
  applySelectedMarketAsset();
  initializeCazatiburonesUniverseFilters();
  boardDataReady = true;
  // A deep link waits for the catalog and then uses the same canonical
  // board-to-loaders matrix as every other board. Cazatiburones therefore
  // dispatches its index and detail reads together without a second trigger.
  loadDeferredBoardData(boardIdFromLocationHash());
  populateMarketComparisonAssets();
  startMarketClocks();
}
