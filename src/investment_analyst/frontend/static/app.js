"use strict";

const LOCALE = "es-PE";
const DEFAULT_TIME_ZONE = "America/Lima";
const NEW_YORK_TIME_ZONE = "America/New_York";
const MARKET_CLOCK_REFRESH_MS = 60_000;
const OVERVIEW_REFRESH_MS = 30_000;
const OVERVIEW_MAX_BACKOFF_MS = 5 * 60_000;
const NYSE_CORE_OPEN_MINUTES = 9 * 60 + 30;
const NYSE_CORE_CLOSE_MINUTES = 16 * 60;
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const THEME_STORAGE_KEY = "investment-analyst-theme-v1";
const CHART_SETTINGS_STORAGE_KEY = "investment-analyst-chart-settings-v1";

// Every rendered color, including JS-driven SVG strokes, resolves from the
// tokens declared once in tokens.css: no color literal lives in this file.
function designToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const COMPARISON_PALETTE = Object.freeze([
  "--compare-series-1",
  "--compare-series-2",
  "--compare-series-3",
  "--compare-series-4",
  "--compare-series-5",
].map(designToken));

const DEFAULT_CHART_SETTINGS = Object.freeze({
  shortWindow: 5,
  longWindow: 20,
  thirdWindow: 50,
  bollingerWindow: 20,
  bollingerMultiplier: "2",
  priceScale: "linear",
  chartType: "candlestick",
  interval: "auto",
});

// The three default SMA line colors resolve to whichever theme is active
// when they are read, so they must be captured only after initializeTheme()
// restores the persisted theme -- not at module load, when <html> still
// carries only its static data-theme="dark" default. captureDefaultSmaColors()
// is called from initialize() right after initializeTheme(); until then this
// holds a throwaway pre-theme snapshot that initializeChartSettings() always
// overwrites before anything is rendered.
let DEFAULT_SMA_COLORS = {
  shortColor: designToken("--series-sma-5"),
  longColor: designToken("--series-sma-20"),
  thirdColor: designToken("--series-sma-50"),
};

function captureDefaultSmaColors() {
  DEFAULT_SMA_COLORS = {
    shortColor: designToken("--series-sma-5"),
    longColor: designToken("--series-sma-20"),
    thirdColor: designToken("--series-sma-50"),
  };
}
const CHART_WIDTH = 1000;
const CHART_HEIGHT = 360;
const CHART_LAYOUT = Object.freeze({
  left: 70,
  right: 24,
  top: 16,
  priceBottom: 235,
  volumeTop: 271,
  bottom: 333,
});
const MINIMUM_CHART_VIEW_POINTS = 8;
const FUNDAMENTAL_CHART_WIDTH = 900;
const FUNDAMENTAL_CHART_HEIGHT = 330;
const FUNDAMENTAL_CHART_LAYOUT = Object.freeze({
  left: 66,
  right: 20,
  top: 24,
  bottom: 276,
});
const MARKET_CLOCK_DEFINITIONS = Object.freeze([
  Object.freeze({
    timeElementId: "lima-clock",
    dateElementId: "lima-clock-date",
    timeZone: DEFAULT_TIME_ZONE,
  }),
  Object.freeze({
    timeElementId: "new-york-clock",
    dateElementId: "new-york-clock-date",
    timeZone: NEW_YORK_TIME_ZONE,
  }),
]);
const MARKET_CLOCK_FORMATTERS = new Map(
  MARKET_CLOCK_DEFINITIONS.map((definition) => [
    definition.timeZone,
    Object.freeze({
      time: new Intl.DateTimeFormat(LOCALE, {
        timeZone: definition.timeZone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }),
      date: new Intl.DateTimeFormat(LOCALE, {
        timeZone: definition.timeZone,
        weekday: "long",
        day: "2-digit",
        month: "long",
      }),
    }),
  ]),
);
const NEW_YORK_SESSION_PARTS_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: NEW_YORK_TIME_ZONE,
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const NYSE_SESSION_STATES = Object.freeze({
  weekend: Object.freeze({ label: "Fuera de sesión · fin de semana", tone: "neutral" }),
  before: Object.freeze({ label: "Antes de apertura regular", tone: "neutral" }),
  open: Object.freeze({ label: "Dentro de sesión regular", tone: "open" }),
  after: Object.freeze({ label: "Después del cierre regular", tone: "neutral" }),
});

const MARKET_CHART_PERIOD_BY_INTERVAL = Object.freeze({
  auto: "1y",
  "1d": "1y",
  "1w": "5y",
  "1mo": "max",
});
const DAILY_MARKET_INTERVALS = Object.freeze([
  Object.freeze({ value: "auto", label: "Automático · último año" }),
  Object.freeze({ value: "1d", label: "1 día · último año" }),
  Object.freeze({ value: "1w", label: "1 semana · últimos 5 años" }),
  Object.freeze({ value: "1mo", label: "1 mes · historial completo" }),
]);
const BTC_INTRADAY_INTERVALS = Object.freeze([
  Object.freeze({ value: "1m", label: "1 min · últimas 24 h" }),
  Object.freeze({ value: "5m", label: "5 min · últimas 24 h" }),
  Object.freeze({ value: "15m", label: "15 min · últimas 24 h" }),
  Object.freeze({ value: "30m", label: "30 min · últimas 24 h" }),
  Object.freeze({ value: "45m", label: "45 min · últimas 24 h" }),
  Object.freeze({ value: "1h", label: "1 hora · últimas 24 h" }),
  Object.freeze({ value: "2h", label: "2 horas · últimas 24 h" }),
  Object.freeze({ value: "4h", label: "4 horas · últimas 24 h" }),
  Object.freeze({ value: "5h", label: "5 horas · últimas 24 h" }),
]);
const BTC_INTRADAY_INTERVAL_VALUES = new Set(
  BTC_INTRADAY_INTERVALS.map((interval) => interval.value),
);
const MARKET_CHART_PERIOD_LABELS = Object.freeze({
  "1y": "Último año",
  "5y": "Últimos cinco años",
  max: "Historial completo",
  "24h": "Últimas 24 horas",
});
let marketAssets = Object.freeze({});
let assetPreferencesSnapshot = null;
let marketComparisonRequestSequence = 0;

const MARKET_RESOLUTION_PRESENTATION = Object.freeze({
  daily: Object.freeze({ singular: "día", plural: "días", adjective: "diarios" }),
  weekly: Object.freeze({ singular: "semana", plural: "semanas", adjective: "semanales" }),
  monthly: Object.freeze({ singular: "mes", plural: "meses", adjective: "mensuales" }),
  "1m": Object.freeze({ singular: "minuto", plural: "minutos", adjective: "de 1 minuto" }),
  "5m": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 5 minutos" }),
  "15m": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 15 minutos" }),
  "30m": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 30 minutos" }),
  "45m": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 45 minutos" }),
  "1h": Object.freeze({ singular: "hora", plural: "horas", adjective: "de 1 hora" }),
  "2h": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 2 horas" }),
  "4h": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 4 horas" }),
  "5h": Object.freeze({ singular: "intervalo", plural: "intervalos", adjective: "de 5 horas" }),
});

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
    label: "Variación diaria",
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

const FUNDAMENTAL_RESEARCH_PRESENTATION = Object.freeze({
  "fundamental.research.asset_turnover": Object.freeze({
    label: "Rotación de activos",
    kind: "multiple",
  }),
  "fundamental.research.current_financial_debt": Object.freeze({
    label: "Deuda financiera corriente",
    kind: "currency",
  }),
  "fundamental.research.current_financial_debt_share": Object.freeze({
    label: "Vencimiento corriente / deuda",
    kind: "percentage",
  }),
  "fundamental.research.diluted_eps": Object.freeze({
    label: "EPS diluido",
    kind: "currency_per_share",
  }),
  "fundamental.research.revenue_per_diluted_share": Object.freeze({
    label: "Ingresos / acción diluida",
    kind: "currency_per_share",
  }),
  "fundamental.research.free_cash_flow_per_diluted_share": Object.freeze({
    label: "FCF / acción diluida",
    kind: "currency_per_share",
  }),
  "fundamental.research.diluted_shares": Object.freeze({
    label: "Acciones diluidas promedio",
    kind: "shares",
  }),
  "fundamental.research.shares_outstanding": Object.freeze({
    label: "Acciones en circulación",
    kind: "shares",
  }),
  "fundamental.research.effective_tax_rate": Object.freeze({
    label: "Tasa fiscal efectiva",
    kind: "percentage",
  }),
  "fundamental.research.financial_debt": Object.freeze({
    label: "Deuda financiera",
    kind: "currency",
  }),
  "fundamental.research.financial_debt_to_assets": Object.freeze({
    label: "Deuda financiera / activos",
    kind: "percentage",
  }),
  "fundamental.research.financial_debt_to_equity": Object.freeze({
    label: "Deuda financiera / patrimonio",
    kind: "multiple",
  }),
  "fundamental.research.financial_debt_to_free_cash_flow": Object.freeze({
    label: "Deuda financiera / FCF",
    kind: "multiple",
  }),
  "fundamental.research.fixed_asset_turnover": Object.freeze({
    label: "Rotación de activos fijos",
    kind: "multiple",
  }),
  "fundamental.research.gross_margin": Object.freeze({
    label: "Margen bruto",
    kind: "percentage",
  }),
  "fundamental.research.operating_margin": Object.freeze({
    label: "Margen operativo",
    kind: "percentage",
  }),
  "fundamental.research.net_margin": Object.freeze({
    label: "Margen neto",
    kind: "percentage",
  }),
  "fundamental.research.operating_cash_flow_margin": Object.freeze({
    label: "Margen de flujo operativo",
    kind: "percentage",
  }),
  "fundamental.research.free_cash_flow_margin": Object.freeze({
    label: "Margen de FCF",
    kind: "percentage",
  }),
  "fundamental.research.operating_cash_flow_to_net_income": Object.freeze({
    label: "Flujo operativo / beneficio neto",
    kind: "multiple",
  }),
  "fundamental.research.free_cash_flow_to_net_income": Object.freeze({
    label: "FCF / beneficio neto",
    kind: "multiple",
  }),
  "fundamental.research.free_cash_flow": Object.freeze({
    label: "Flujo de caja libre",
    kind: "currency",
  }),
  "fundamental.research.capex_to_operating_cash_flow": Object.freeze({
    label: "Capex / flujo operativo",
    kind: "percentage",
  }),
  "fundamental.research.research_and_development_to_revenue": Object.freeze({
    label: "R&D / ingresos",
    kind: "percentage",
  }),
  "fundamental.research.selling_general_and_administrative_to_revenue": Object.freeze({
    label: "SG&A / ingresos",
    kind: "percentage",
  }),
  "fundamental.research.share_based_compensation_to_revenue": Object.freeze({
    label: "Stock-based compensation / ingresos",
    kind: "percentage",
  }),
  "fundamental.research.current_ratio": Object.freeze({
    label: "Current ratio",
    kind: "multiple",
  }),
  "fundamental.research.cash_ratio": Object.freeze({
    label: "Cash ratio",
    kind: "multiple",
  }),
  "fundamental.research.working_capital": Object.freeze({
    label: "Capital de trabajo",
    kind: "currency",
  }),
  "fundamental.research.net_liquid_assets": Object.freeze({
    label: "Activos líquidos netos",
    kind: "currency",
  }),
  "fundamental.research.interest_coverage": Object.freeze({
    label: "Cobertura de intereses",
    kind: "multiple",
  }),
  "fundamental.research.lease_liabilities": Object.freeze({
    label: "Pasivos por arrendamientos",
    kind: "currency",
  }),
  "fundamental.research.net_debt": Object.freeze({
    label: "Deuda financiera neta",
    kind: "currency",
  }),
  "fundamental.research.net_debt_to_free_cash_flow": Object.freeze({
    label: "Deuda neta / FCF",
    kind: "multiple",
  }),
  "fundamental.research.return_on_assets_ending_balance": Object.freeze({
    label: "Rentabilidad sobre activos",
    kind: "percentage",
  }),
  "fundamental.research.return_on_equity_ending_balance": Object.freeze({
    label: "Rentabilidad sobre patrimonio",
    kind: "percentage",
  }),
  "fundamental.research.return_on_invested_capital_ending_balance": Object.freeze({
    label: "ROIC aproximado",
    kind: "percentage",
  }),
  "fundamental.research.shareholder_distributions": Object.freeze({
    label: "Dividendos + recompras",
    kind: "currency",
  }),
  "fundamental.research.shareholder_distributions_to_free_cash_flow": Object.freeze({
    label: "Distribuciones / FCF",
    kind: "percentage",
  }),
  "fundamental.research.total_financial_obligations": Object.freeze({
    label: "Deuda + arrendamientos",
    kind: "currency",
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
  rule_conflict: "La regla cambió desde que se abrió. Vuelve a cargarla antes de guardar.",
  asset_preferences_conflict:
    "La selección cambió desde que se abrió. Se recargó el estado vigente; revisa antes de guardar.",
  backtest_unavailable:
    "No hay evidencia point-in-time compatible para este replay en el activo seleccionado.",
  known_at_too_early:
    "El corte elegido es anterior a la evidencia de mercado recién obtenida.",
  market_refresh_failed: "No fue posible actualizar el activo desde su proveedor. Inténtalo nuevamente.",
  market_intraday_refresh_failed:
    "No fue posible actualizar las velas intradía de BTC-USD. Los datos diarios ya guardados no se pierden.",
  operational_error: "La operación local no está disponible. Revisa el estado del espacio de datos.",
  unexpected_error: "La interfaz local encontró un error inesperado.",
});

const byId = (id) => document.getElementById(id);
let operationalIssues = [];
let marketChartPayload = null;
let marketChartViewport = null;
let marketChartRenderFrame = null;
let marketChartDrag = null;
let selectedChartPoint = -1;
let selectedMarketAsset = "equity:us:aapl";
const marketStartByAsset = new Map();
const knownAtByAsset = new Map();
let selectedFundamentalFrequency = "quarterly";
let screeningRuleSnapshot = null;
let fundamentalTrendPayload = null;
let fundamentalResearchPayload = null;
let valuationPayload = null;
let valuationHistoryPayload = null;
let valuationRulePayload = null;
let cryptoDerivativesPayload = null;
let cryptoDerivativesRequest = 0;
let fundamentalBusyCount = 0;
let reportPayload = null;
let listedCompanyReportRequest = 0;
let chartSettings = { ...DEFAULT_CHART_SETTINGS, ...DEFAULT_SMA_COLORS };
const chartSeriesVisibility = {
  "sma-5": true,
  "sma-20": true,
  "sma-50": true,
  bollinger: true,
  volume: true,
};

function marketAssetPresentation() {
  const presentation = marketAssets[selectedMarketAsset];
  if (!presentation) throw new Error("El activo seleccionado no pertenece al catálogo disponible.");
  return presentation;
}

function marketAssetFromDescriptor(descriptor) {
  const assetClassLabels = {
    equity: "Análisis de acciones",
    etf: "Análisis de ETF",
    crypto: "Análisis de criptoactivos",
  };
  const providerLabel = descriptor.provider === "alpaca"
    ? "Alpaca Market Data · IEX parcial"
    : descriptor.provider === "coinbase"
      ? "Coinbase Exchange · mercado 24/7"
      : descriptor.provider;
  const volumeLabel = descriptor.asset_class === "etf"
    ? "participaciones"
    : descriptor.asset_class === "crypto"
      ? descriptor.volume_unit
      : "acciones";
  return Object.freeze({
    assetId: descriptor.asset_id,
    symbol: descriptor.symbol,
    name: descriptor.name,
    quoteCurrency: descriptor.quote_currency,
    breadcrumb: `${assetClassLabels[descriptor.asset_class] || "Análisis de mercado"} / ${descriptor.exchange}`,
    meta: `${descriptor.exchange} · ${descriptor.quote_currency}${descriptor.source_id.includes("iex") ? " · IEX parcial" : descriptor.source_id.includes("coinbase") ? " · mercado 24/7" : ""}`,
    sourceId: descriptor.source_id,
    schemaVersion: descriptor.chart_schema_version,
    intradaySourceId: descriptor.intraday_source_id,
    intradaySchemaVersion: descriptor.intraday_schema_version,
    volumeUnit: descriptor.volume_unit,
    volumeLabel,
    assetClass: descriptor.asset_class,
    defaultMarketStart: descriptor.default_market_start,
    analysisFamily: descriptor.analysis.family,
    marketMode: descriptor.analysis.market_mode,
    fundamentalMode: descriptor.analysis.fundamental_mode,
    hasFundamentals: descriptor.has_fundamentals,
    hasCorporateValuation: descriptor.has_corporate_valuation,
    supportsCryptoDerivatives: descriptor.supports_crypto_derivatives === true,
    fundamentalFrequencies: descriptor.fundamental_frequencies,
    refreshLabel: `Actualizar ${descriptor.symbol}`,
    refreshSource: descriptor.has_fundamentals
      ? `${providerLabel} · ${descriptor.provider_identifier} + SEC EDGAR`
      : `${providerLabel} · ${descriptor.provider_identifier}`,
  });
}

async function loadMarketAssets() {
  const payload = await api("/api/market-assets");
  if (
    payload.schema_version !== "market-asset-universe-v5"
    || !Array.isArray(payload.assets)
    || payload.assets.length === 0
  ) {
    throw new Error("El catálogo de mercado no tiene un contrato compatible.");
  }
  marketAssets = Object.freeze(
    Object.fromEntries(
      payload.assets.map((descriptor) => [
        descriptor.asset_id,
        marketAssetFromDescriptor(descriptor),
      ]),
    ),
  );
  if (!marketAssets[selectedMarketAsset]) selectedMarketAsset = payload.assets[0].asset_id;
  marketStartByAsset.clear();
  for (const [assetId, presentation] of Object.entries(marketAssets)) {
    marketStartByAsset.set(assetId, presentation.defaultMarketStart);
  }
  const input = byId("market-asset-search");
  const listbox = byId("market-asset-listbox");
  const groups = {
    equity: { label: "Acciones", options: [] },
    etf: { label: "ETF", options: [] },
    crypto: { label: "Cripto", options: [] },
  };
  payload.assets.forEach((descriptor) => {
    const cls = descriptor.asset_class;
    if (groups[cls]) {
      groups[cls].options.push(descriptor);
    }
  });

  window.comboboxData = [];
  for (const group of Object.values(groups)) {
    if (group.options.length > 0) {
      window.comboboxData.push({ type: "group", label: group.label });
      group.options.forEach(opt => {
        window.comboboxData.push({ type: "option", data: opt });
      });
    }
  }

  input.value = marketAssets[selectedMarketAsset].symbol + " — " + marketAssets[selectedMarketAsset].name;

  function renderListbox(filter = "") {
    listbox.replaceChildren();
    const lowerFilter = filter.toLowerCase();
    let hasOptions = false;
    let currentGroupLi = null;
    let groupHasOptions = false;

    window.comboboxData.forEach((item, index) => {
      if (item.type === "group") {
        if (currentGroupLi && !groupHasOptions) {
           currentGroupLi.remove();
        }
        currentGroupLi = document.createElement("li");
        currentGroupLi.className = "combobox-group";
        currentGroupLi.role = "presentation";
        currentGroupLi.textContent = item.label;
        listbox.appendChild(currentGroupLi);
        groupHasOptions = false;
      } else {
        const text = `${item.data.symbol} — ${item.data.name}`;
        if (!filter || text.toLowerCase().includes(lowerFilter)) {
          const li = document.createElement("li");
          li.className = "combobox-option";
          li.role = "option";
          li.id = `combobox-option-${item.data.asset_id.replace(/:/g, "-")}`;
          li.dataset.value = item.data.asset_id;
          li.textContent = text;
          if (item.data.asset_id === selectedMarketAsset && !filter) {
            li.setAttribute("aria-selected", "true");
          }
          li.addEventListener("click", () => {
            selectComboboxOption(item.data.asset_id);
          });
          listbox.appendChild(li);
          groupHasOptions = true;
          hasOptions = true;
        }
      }
    });
    if (currentGroupLi && !groupHasOptions) {
       currentGroupLi.remove();
    }
    if (!hasOptions) {
      const li = document.createElement("li");
      li.className = "combobox-empty";
      li.role = "presentation";
      li.textContent = "No se encontraron activos.";
      listbox.appendChild(li);
    }
  }

  function openListbox() {
    input.setAttribute("aria-expanded", "true");
    listbox.hidden = false;
    renderListbox(input.value !== (marketAssets[selectedMarketAsset].symbol + " — " + marketAssets[selectedMarketAsset].name) ? input.value : "");
  }

  function closeListbox() {
    input.setAttribute("aria-expanded", "false");
    listbox.hidden = true;
    input.removeAttribute("aria-activedescendant");
    const active = listbox.querySelector(".combobox-option.active");
    if (active) active.classList.remove("active");
  }

  async function selectComboboxOption(assetId) {
    if (!marketAssets[assetId] || assetId === selectedMarketAsset) {
      closeListbox();
      input.value = marketAssets[assetId].symbol + " — " + marketAssets[assetId].name;
      return;
    }
    input.value = marketAssets[assetId].symbol + " — " + marketAssets[assetId].name;
    closeListbox();
    marketStartByAsset.set(selectedMarketAsset, byId("market-start").value);
    knownAtByAsset.set(selectedMarketAsset, byId("report-known-at").value.trim());
    resetListedCompanyReport();
    selectedMarketAsset = assetId;
    byId("report-known-at").value = knownAtByAsset.get(assetId) || new Date().toISOString();
    marketChartPayload = null;
    marketChartViewport = null;
    marketChartDrag = null;
    resetValuation();
    resetCryptoDerivatives();
    applySelectedMarketAsset();
    applyChartSettings();
    const activeFundamentalLink = document.querySelector(
      (
        ".nav-link.active[data-fundamental-only], "
        + ".nav-link.active[data-valuation-only], "
        + ".nav-link.active[data-complete-analysis-only]"
      ),
    );
    if (activeFundamentalLink) {
      activeFundamentalLink.classList.remove("active");
      activeFundamentalLink.removeAttribute("aria-current");
      const marketLink = document.querySelector('.nav-link[href="#mercado"]');
      if (marketLink) {
        marketLink.classList.add("active");
        marketLink.setAttribute("aria-current", "page");
      }
    }

    const presentation = marketAssetPresentation();
    await Promise.all([
      queryMarketChart(),
      ...(presentation.hasFundamentals
        ? [queryFundamentalTrend(), queryFundamentalResearch(), queryReport()]
        : []),
    ]);
  }

  input.addEventListener("focus", openListbox);
  input.addEventListener("input", () => {
    openListbox();
  });

  input.addEventListener("keydown", (e) => {
    if (listbox.hidden && e.key !== "Escape" && e.key !== "Tab") {
      openListbox();
    }
    const options = Array.from(listbox.querySelectorAll(".combobox-option"));
    if (!options.length) return;

    let activeIndex = options.findIndex(opt => opt.classList.contains("active"));

    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (activeIndex < options.length - 1) activeIndex++;
      else activeIndex = 0;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (activeIndex > 0) activeIndex--;
      else activeIndex = options.length - 1;
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (activeIndex >= 0) {
        selectComboboxOption(options[activeIndex].dataset.value);
      } else if (options.length === 1) {
        selectComboboxOption(options[0].dataset.value);
      }
      return;
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeListbox();
      input.value = marketAssets[selectedMarketAsset].symbol + " — " + marketAssets[selectedMarketAsset].name;
      return;
    } else {
      return;
    }

    options.forEach(opt => opt.classList.remove("active"));
    if (activeIndex >= 0) {
      options[activeIndex].classList.add("active");
      input.setAttribute("aria-activedescendant", options[activeIndex].id);
      options[activeIndex].scrollIntoView({ block: "nearest" });
    }
  });

  document.addEventListener("click", (e) => {
    if (!byId("asset-combobox-container").contains(e.target)) {
      if (!listbox.hidden) {
        closeListbox();
        input.value = marketAssets[selectedMarketAsset].symbol + " — " + marketAssets[selectedMarketAsset].name;
      }
    }
  });
}

function preferenceToggle(asset, kind, label) {
  const wrapper = createElement("label", "preference-toggle");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.dataset.assetId = asset.asset_id;
  input.dataset.preferenceKind = kind;
  input.checked = Boolean(asset[kind]);
  input.disabled = !asset.available || (kind !== "watchlist" && !asset.watchlist);
  const accessible = createElement(
    "span",
    "visually-hidden",
    `${label}: ${asset.symbol}`,
  );
  wrapper.append(input, accessible);
  return wrapper;
}

function prioritizeAssetSelector(payload) {
  const ordered = payload.assets.filter((asset) => asset.available && marketAssets[asset.asset_id]);
  const groups = {
    equity: { label: "Acciones", options: [] },
    etf: { label: "ETF", options: [] },
    crypto: { label: "Cripto", options: [] },
  };
  ordered.forEach((asset) => {
    const cls = marketAssets[asset.asset_id]?.assetClass || "equity";
    if (groups[cls]) {
      groups[cls].options.push({
        asset_id: asset.asset_id,
        symbol: asset.favorite ? `★ ${asset.symbol}` : asset.symbol,
        name: asset.name
      });
    }
  });

  window.comboboxData = [];
  for (const group of Object.values(groups)) {
    if (group.options.length > 0) {
      window.comboboxData.push({ type: "group", label: group.label });
      group.options.forEach(opt => {
        window.comboboxData.push({ type: "option", data: opt });
      });
    }
  }
}

function renderAssetPreferences(payload) {
  if (
    payload.schema_version !== "asset-preferences-view-v1"
    || !Array.isArray(payload.assets)
  ) {
    throw new Error("Las preferencias locales no tienen un contrato compatible.");
  }
  assetPreferencesSnapshot = payload;
  const list = byId("asset-preferences-list");
  list.replaceChildren();
  for (const asset of payload.assets) {
    const row = createElement(
      "div",
      `preference-row${asset.available ? "" : " unavailable"}`,
    );
    row.dataset.assetId = asset.asset_id;
    const identity = createElement("div", "preference-asset");
    identity.append(
      createElement("strong", "", `${asset.favorite ? "★ " : ""}${asset.symbol} — ${asset.name}`),
      createElement(
        "small",
        "",
        asset.available
          ? `${asset.provider} · ${asset.frequencies.join(", ")} · ${asset.source_ids.length} fuentes`
          : "No disponible en el catálogo actual · fuera de ejecución",
      ),
    );
    const watchlist = preferenceToggle(asset, "watchlist", "Watchlist");
    const favorite = preferenceToggle(asset, "favorite", "Favorito");
    const scheduled = preferenceToggle(asset, "scheduled_refresh", "Actualización programada");
    row.append(identity, watchlist, favorite, scheduled);
    const watchlistInput = watchlist.querySelector("input");
    watchlistInput.addEventListener("change", () => {
      const enabled = watchlistInput.checked && asset.available;
      for (const dependent of [favorite, scheduled]) {
        const input = dependent.querySelector("input");
        input.disabled = !enabled;
        if (!enabled) input.checked = false;
      }
    });
    list.append(row);
  }
  byId("asset-preferences-summary").textContent =
    `${formatInteger(payload.watchlist_count)} en watchlist · `
    + `${formatInteger(payload.favorite_count)} favoritos · `
    + `${formatInteger(payload.scheduled_asset_count)} programados efectivos`;
  const revisionStatus = payload.source === "persisted"
    ? `Revisión ${payload.revision_id.slice(0, 8)} · ${formatInstant(payload.created_at)}`
    : "Valores efectivos de la configuración CLI; aún no se escribió una revisión.";
  byId("asset-preferences-status").textContent = payload.scheduler_enabled
    ? revisionStatus
    : `${revisionStatus} Scheduler desactivado; la selección programada se conserva.`;
  prioritizeAssetSelector(payload);
}

async function loadAssetPreferences() {
  renderAssetPreferences(await api("/api/v1/asset-preferences"));
}

function preferenceEntriesFromForm() {
  return [...document.querySelectorAll("#asset-preferences-list .preference-row")]
    .map((row) => {
      const input = (kind) => row.querySelector(`[data-preference-kind="${kind}"]`).checked;
      return {
        asset_id: row.dataset.assetId,
        watchlist: input("watchlist"),
        favorite: input("favorite"),
        scheduled_refresh: input("scheduled_refresh"),
      };
    })
    .sort((left, right) => left.asset_id.localeCompare(right.asset_id));
}

async function saveAssetPreferences() {
  if (assetPreferencesSnapshot === null) return;
  const button = byId("save-asset-preferences");
  setButtonBusy(button, true, "Guardando…", "Guardar preferencias");
  try {
    const payload = await api("/api/v1/asset-preferences", {
      method: "PUT",
      body: JSON.stringify({
        schema_version: "asset-preferences-update-v1",
        expected_revision_id: assetPreferencesSnapshot.revision_id,
        expected_fingerprint: assetPreferencesSnapshot.fingerprint,
        entries: preferenceEntriesFromForm(),
      }),
    });
    renderAssetPreferences(payload);
    byId("asset-preferences-status").textContent = payload.scheduler_enabled
      ? `Preferencias guardadas · ${formatInteger(payload.scheduled_job_count)} trabajos activos`
      : "Preferencias guardadas · scheduler desactivado; 0 trabajos efectivos";
    await refreshOverview({ manual: false });
  } catch (error) {
    if (error.code === "asset_preferences_conflict") await loadAssetPreferences();
    byId("asset-preferences-status").textContent = error.message;
  } finally {
    setButtonBusy(button, false, "Guardando…", "Guardar preferencias");
  }
}

function isIntradayInterval(value = chartSettings.interval) {
  const presentation = marketAssets[selectedMarketAsset];
  return Boolean(presentation?.intradaySourceId)
    && BTC_INTRADAY_INTERVAL_VALUES.has(value);
}

function marketChartPeriod() {
  if (isIntradayInterval()) return "24h";
  return MARKET_CHART_PERIOD_BY_INTERVAL[chartSettings.interval] || "1y";
}

function marketChartPeriodLabel(period) {
  return MARKET_CHART_PERIOD_LABELS[period] || "Rango consultado";
}

function applySelectedMarketAsset() {
  const presentation = marketAssetPresentation();
  if (!presentation.intradaySourceId && BTC_INTRADAY_INTERVAL_VALUES.has(chartSettings.interval)) {
    chartSettings = { ...chartSettings, interval: DEFAULT_CHART_SETTINGS.interval };
  }
  const intervalSelect = byId("chart-interval");
  const intervals = presentation.intradaySourceId
    ? [...BTC_INTRADAY_INTERVALS, ...DAILY_MARKET_INTERVALS]
    : DAILY_MARKET_INTERVALS;
  intervalSelect.replaceChildren(
    ...intervals.map((interval) => {
      const option = document.createElement("option");
      option.value = interval.value;
      option.textContent = interval.label;
      return option;
    }),
  );
  intervalSelect.value = chartSettings.interval;
  byId("asset-symbol").textContent = presentation.symbol;
  byId("asset-name").textContent = presentation.name;
  byId("asset-name").title = presentation.name;
  byId("asset-avatar-text").textContent = presentation.symbol.charAt(0);
  byId("asset-meta").textContent = presentation.meta;
  byId("asset-price").textContent = "—";
  byId("asset-daily-change").textContent = "—";
  byId("asset-daily-change").className = "asset-change neutral";
  byId("market-chart-symbol").textContent = presentation.symbol;
  byId("market-chart").setAttribute(
    "aria-label",
    `Gráfico histórico interactivo de ${presentation.symbol}. Usa la rueda del mouse para cambiar el zoom, arrastra horizontalmente para desplazar la vista y usa las flechas para recorrer fechas.`,
  );
  byId("fundamental-chart-symbol").textContent = presentation.symbol;
  byId("chart-point-volume-label").textContent = `Volumen (${presentation.volumeLabel})`;
  byId("snapshot-volume-label").textContent = `Volumen (${presentation.volumeLabel})`;
  byId("chart-table-volume-label").textContent = `Volumen (${presentation.volumeLabel})`;
  document.title = `${presentation.name} (${presentation.symbol}) · Investment Analyst`;
  for (const element of document.querySelectorAll("[data-fundamental-only]")) {
    element.classList.toggle("hidden", !presentation.hasFundamentals);
  }
  for (const element of document.querySelectorAll("[data-valuation-only]")) {
    element.classList.toggle("hidden", !presentation.hasCorporateValuation);
  }
  for (const element of document.querySelectorAll("[data-complete-run-only]")) {
    element.classList.toggle("hidden", !presentation.hasFundamentals);
  }
  for (const element of document.querySelectorAll("[data-fundamental-run-only]")) {
    element.classList.toggle("hidden", !presentation.hasFundamentals);
  }
  for (const element of document.querySelectorAll("[data-complete-analysis-only]")) {
    element.classList.toggle("hidden", !presentation.hasFundamentals);
  }
  for (const element of document.querySelectorAll("[data-crypto-derivatives-only]")) {
    element.classList.toggle("hidden", !presentation.supportsCryptoDerivatives);
  }
  const supportedFrequencies = new Set(presentation.fundamentalFrequencies);
  if (
    presentation.hasFundamentals
    && !supportedFrequencies.has(selectedFundamentalFrequency)
  ) {
    selectFundamentalFrequency(presentation.fundamentalFrequencies[0]);
  }
  for (const button of document.querySelectorAll(".frequency-button")) {
    button.classList.toggle(
      "hidden",
      presentation.hasFundamentals
        && !supportedFrequencies.has(button.dataset.frequency),
    );
  }
  for (const selectId of ["report-frequency", "run-frequency"]) {
    for (const option of byId(selectId).options) {
      option.disabled = presentation.hasFundamentals
        && !supportedFrequencies.has(option.value);
    }
    if (
      presentation.hasFundamentals
      && !supportedFrequencies.has(byId(selectId).value)
    ) {
      byId(selectId).value = presentation.fundamentalFrequencies[0];
    }
  }
  byId("operacion-titulo").textContent = presentation.refreshLabel;
  byId("run-source-label").textContent = presentation.refreshSource;
  byId("run-note").textContent = presentation.hasFundamentals
    ? "Mercado y SEC se actualizan de forma serial e independiente; un fallo SEC no revierte el mercado persistido."
    : isIntradayInterval()
      ? "Actualiza primero el histórico diario y después las últimas 24 horas intradía."
      : `Solo actualiza mercado ${presentation.symbol}; no simula fundamentales ni ejecuta operaciones.`;
  byId("run-button").textContent = presentation.hasFundamentals
    ? "Ejecutar actualización"
    : presentation.refreshLabel;
  byId("market-start").value = marketStartByAsset.get(selectedMarketAsset);
}

function resetCryptoDerivatives() {
  cryptoDerivativesPayload = null;
  cryptoDerivativesRequest += 1;
  byId("crypto-derivatives-panel").open = false;
  byId("crypto-derivatives-content").setAttribute("aria-busy", "false");
  byId("crypto-derivatives-coverage").textContent = "Sin consultar";
  byId("crypto-derivatives-status").textContent = "Abre este panel para consultar evidencia local.";
  byId("crypto-derivatives-context").textContent = "La consulta se carga bajo demanda con el mismo corte visible.";
  for (const identifier of [
    "derivatives-funding-168h",
    "derivatives-funding-direction",
    "derivatives-dvol-7d",
    "derivatives-dvol-direction",
    "derivatives-open-interest",
    "derivatives-current-funding",
    "derivatives-spread",
    "derivatives-diagnostic-status",
    "derivatives-range",
    "derivatives-known-at",
    "derivatives-source-ids",
    "derivatives-traceability",
    "derivatives-missing",
    "derivatives-limitations",
  ]) byId(identifier).textContent = "—";
  byId("derivatives-evidence").textContent = "Sin evidencia cargada.";
}

function cryptoDerivativesRange(knownAt) {
  const cutoff = new Date(knownAt);
  if (Number.isNaN(cutoff.valueOf())) throw new Error("El corte debe ser ISO 8601 con zona.");
  const end = cutoff.toISOString().slice(0, 10);
  cutoff.setUTCDate(cutoff.getUTCDate() - 89);
  return { start: cutoff.toISOString().slice(0, 10), end };
}

function formatDerivativeValue(value, unit = "") {
  if (!value || typeof value.value !== "string") return "No disponible";
  const numeric = numericValue(value.value);
  if (numeric === null) return value.value;
  const suffix = unit || value.unit || "";
  if (suffix === "bps") return `${formatNumber(numeric, { maximumFractionDigits: 2 })} bps`;
  if (suffix === "percent" || suffix === "%") {
    return formatNumber(numeric, { style: "percent", maximumFractionDigits: 4 });
  }
  return `${formatNumber(numeric, { maximumFractionDigits: 6 })}${suffix ? ` ${suffix}` : ""}`;
}

function derivativeDirection(value) {
  const labels = {
    positive: "Positiva",
    negative: "Negativa",
    zero: "Sin variación",
    rising: "En aumento",
    falling: "En descenso",
    unchanged: "Sin variación",
    unavailable: "No disponible",
  };
  return labels[value] || "No disponible";
}

function renderCryptoDerivatives(payload) {
  if (
    payload?.schema_version !== "crypto-derivatives-query-result-v1"
    || payload.asset_id !== selectedMarketAsset
    || !payload.diagnostic
    || !payload.coverage
  ) throw new Error("La evidencia de derivados no coincide con el activo o el contrato solicitado.");
  const diagnostic = payload.diagnostic;
  const coverage = payload.coverage;
  byId("derivatives-funding-168h").textContent = formatDerivativeValue(diagnostic.funding_sum_168h);
  byId("derivatives-funding-direction").textContent = derivativeDirection(diagnostic.funding_direction);
  byId("derivatives-dvol-7d").textContent = formatDerivativeValue(diagnostic.dvol_change_7d);
  byId("derivatives-dvol-direction").textContent = derivativeDirection(diagnostic.dvol_direction);
  byId("derivatives-open-interest").textContent = formatDerivativeValue(diagnostic.latest_open_interest);
  const currentFunding = formatDerivativeValue(diagnostic.latest_current_funding);
  const funding8h = formatDerivativeValue(diagnostic.latest_funding_8h);
  byId("derivatives-current-funding").textContent = `${currentFunding} / ${funding8h}`;
  byId("derivatives-spread").textContent = formatDerivativeValue(diagnostic.latest_spread_bps, "bps");
  byId("derivatives-diagnostic-status").textContent = translated(
    diagnostic.status,
    STATUS_LABELS,
    diagnostic.status,
  );
  byId("crypto-derivatives-coverage").textContent = translated(
    diagnostic.status,
    STATUS_LABELS,
    diagnostic.status,
  );
  byId("derivatives-range").textContent = `${coverage.requested_start} – ${coverage.requested_end}`;
  byId("derivatives-known-at").textContent = formatInstant(payload.known_at);
  byId("derivatives-source-ids").textContent = payload.source_ids.join(" · ");
  byId("derivatives-traceability").textContent = payload.traceability_verified ? "Verificada" : "No verificada";
  byId("derivatives-missing").textContent = diagnostic.missing_requirements.length
    ? diagnostic.missing_requirements.join(" · ")
    : "Ninguno";
  byId("derivatives-limitations").textContent = diagnostic.limitations.length
    ? diagnostic.limitations.join(" · ")
    : "No se declararon limitaciones adicionales.";
  byId("derivatives-evidence").textContent = JSON.stringify(
    {
      diagnostic_id: diagnostic.diagnostic_id,
      observation_ids: diagnostic.observation_ids,
      metric_result_ids: diagnostic.metric_result_ids,
      raw_record_ids: payload.raw_record_ids,
    },
    null,
    2,
  );
  byId("crypto-derivatives-context").textContent = `${marketAssetPresentation().symbol} · Deribit · corte ${formatInstant(payload.known_at)}`;
  byId("crypto-derivatives-status").textContent = `${formatInteger(coverage.funding_observation_count)} observaciones de financiación · ${formatInteger(coverage.dvol_observation_count)} DVOL · ${formatInteger(coverage.summary_snapshot_count)} snapshots de resumen.`;
}

async function queryCryptoDerivatives() {
  const assetId = selectedMarketAsset;
  const knownAt = byId("report-known-at").value.trim();
  const presentation = marketAssets[assetId];
  if (!presentation?.supportsCryptoDerivatives) return;
  const request = ++cryptoDerivativesRequest;
  const content = byId("crypto-derivatives-content");
  content.setAttribute("aria-busy", "true");
  byId("crypto-derivatives-status").textContent = "Consultando evidencia local de derivados…";
  try {
    const range = cryptoDerivativesRange(knownAt);
    const parameters = new URLSearchParams({ asset_id: assetId, ...range, known_at: knownAt });
    const payload = await api(`/api/v1/crypto-derivatives?${parameters.toString()}`);
    if (request !== cryptoDerivativesRequest || assetId !== selectedMarketAsset) return;
    cryptoDerivativesPayload = payload;
    renderCryptoDerivatives(payload);
  } catch (error) {
    if (request !== cryptoDerivativesRequest || assetId !== selectedMarketAsset) return;
    cryptoDerivativesPayload = null;
    byId("crypto-derivatives-coverage").textContent = "No disponible";
    byId("crypto-derivatives-status").textContent = error.message;
  } finally {
    if (request === cryptoDerivativesRequest && assetId === selectedMarketAsset) {
      content.setAttribute("aria-busy", "false");
    }
  }
}

function applyTheme(theme) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  document.querySelector('meta[name="theme-color"]').content = designToken("--canvas");
  const button = byId("theme-toggle");
  button.title = selected === "dark" ? "Tema claro" : "Tema oscuro";
  button.setAttribute("aria-pressed", String(selected === "dark"));
  button.setAttribute(
    "aria-label",
    selected === "dark" ? "Cambiar al tema claro" : "Cambiar al tema oscuro",
  );
}

function initializeTheme() {
  let stored = null;
  try {
    stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
  applyTheme(stored === "light" ? "light" : "dark");
}

function persistTheme(theme) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
}

function normalizeChartSettings(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  const shortWindow = Number(candidate.shortWindow);
  const longWindow = Number(candidate.longWindow);
  const thirdWindow = Number(candidate.thirdWindow ?? DEFAULT_CHART_SETTINGS.thirdWindow);
  const bollingerWindow = Number(candidate.bollingerWindow ?? DEFAULT_CHART_SETTINGS.bollingerWindow);
  const bollingerMultiplier = String(
    candidate.bollingerMultiplier ?? DEFAULT_CHART_SETTINGS.bollingerMultiplier,
  );
  const thirdColor = candidate.thirdColor ?? DEFAULT_SMA_COLORS.thirdColor;
  const priceScale = candidate.priceScale === undefined ? "linear" : candidate.priceScale;
  const chartType =
    candidate.chartType === undefined ? DEFAULT_CHART_SETTINGS.chartType : candidate.chartType;
  const interval = candidate.interval === undefined ? "auto" : candidate.interval;
  const colorPattern = /^#[0-9a-f]{6}$/i;
  if (
    !Number.isInteger(shortWindow) ||
    !Number.isInteger(longWindow) ||
    !Number.isInteger(thirdWindow) ||
    shortWindow < 2 ||
    shortWindow > 200 ||
    longWindow < 3 ||
    longWindow > 399 ||
    thirdWindow < 4 ||
    thirdWindow > 400 ||
    !Number.isInteger(bollingerWindow) ||
    bollingerWindow < 2 ||
    bollingerWindow > 400 ||
    !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(bollingerMultiplier) ||
    Number(bollingerMultiplier) <= 0 ||
    Number(bollingerMultiplier) > 100 ||
    shortWindow >= longWindow ||
    longWindow >= thirdWindow ||
    !colorPattern.test(candidate.shortColor) ||
    !colorPattern.test(candidate.longColor) ||
    !colorPattern.test(thirdColor) ||
    !["linear", "logarithmic"].includes(priceScale) ||
    !["line", "candlestick"].includes(chartType) ||
    ![
      "auto",
      "1d",
      "1w",
      "1mo",
      ...BTC_INTRADAY_INTERVAL_VALUES,
    ].includes(interval)
  ) {
    return null;
  }
  return {
    shortWindow,
    longWindow,
    thirdWindow,
    bollingerWindow,
    bollingerMultiplier,
    shortColor: candidate.shortColor.toLowerCase(),
    longColor: candidate.longColor.toLowerCase(),
    thirdColor: thirdColor.toLowerCase(),
    priceScale,
    chartType,
    interval,
  };
}

function updateSmaLabels() {
  const labels = {
    "sma-short-legend-label": `SMA ${chartSettings.shortWindow}`,
    "sma-long-legend-label": `SMA ${chartSettings.longWindow}`,
    "sma-third-legend-label": `SMA ${chartSettings.thirdWindow}`,
    "chart-point-sma-short-label": `SMA ${chartSettings.shortWindow}`,
    "chart-point-sma-long-label": `SMA ${chartSettings.longWindow}`,
    "chart-point-sma-third-label": `SMA ${chartSettings.thirdWindow}`,
    "snapshot-sma-short-distance-label": `Dist. SMA ${chartSettings.shortWindow}`,
    "snapshot-sma-long-distance-label": `Dist. SMA ${chartSettings.longWindow}`,
    "snapshot-sma-third-distance-label": `Dist. SMA ${chartSettings.thirdWindow}`,
    "chart-latest-sma-short-label": `SMA ${chartSettings.shortWindow}`,
    "chart-latest-sma-long-label": `SMA ${chartSettings.longWindow}`,
    "chart-latest-sma-third-label": `SMA ${chartSettings.thirdWindow}`,
    "chart-table-sma-short-label": `SMA ${chartSettings.shortWindow}`,
    "chart-table-sma-long-label": `SMA ${chartSettings.longWindow}`,
    "chart-table-sma-third-label": `SMA ${chartSettings.thirdWindow}`,
  };
  for (const [id, label] of Object.entries(labels)) byId(id).textContent = label;
}

function applyChartSettings() {
  byId("sma-short-window").value = String(chartSettings.shortWindow);
  byId("sma-long-window").value = String(chartSettings.longWindow);
  byId("sma-third-window").value = String(chartSettings.thirdWindow);
  byId("bollinger-window").value = String(chartSettings.bollingerWindow);
  byId("bollinger-multiplier").value = chartSettings.bollingerMultiplier;
  byId("sma-short-color").value = chartSettings.shortColor;
  byId("sma-long-color").value = chartSettings.longColor;
  byId("sma-third-color").value = chartSettings.thirdColor;
  byId("chart-price-scale").value = chartSettings.priceScale;
  byId("chart-interval").value = chartSettings.interval;
  const intraday = isIntradayInterval();
  byId("chart-settings-summary").textContent = intraday
    ? chartSettings.priceScale === "logarithmic"
      ? "Escala · Logarítmica"
      : "Escala · Lineal"
    : chartSettings.priceScale === "logarithmic"
      ? "Indicadores · Logarítmica"
      : "Indicadores · Lineal";
  document.documentElement.style.setProperty("--series-sma-5", chartSettings.shortColor);
  document.documentElement.style.setProperty("--series-sma-20", chartSettings.longColor);
  document.documentElement.style.setProperty("--series-sma-50", chartSettings.thirdColor);
  for (const button of document.querySelectorAll(".chart-type-button")) {
    const active = button.dataset.chartType === chartSettings.chartType;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  for (const element of document.querySelectorAll("[data-sma-only]")) {
    element.classList.toggle("hidden", intraday);
  }
  const candlesticks = chartSettings.chartType === "candlestick";
  byId("price-series-legend-label").textContent = candlesticks ? "Velas" : "Cierre";
  byId("price-series-swatch").className =
    `legend-swatch ${candlesticks ? "candles" : "close"}`;
  updateSmaLabels();
}

function initializeChartSettings() {
  // Always start from the current-theme defaults: captureDefaultSmaColors()
  // has already run in initialize(), right after initializeTheme(), so this
  // base is correct even when nothing is stored below.
  chartSettings = { ...DEFAULT_CHART_SETTINGS, ...DEFAULT_SMA_COLORS };
  let stored = null;
  try {
    stored = window.localStorage.getItem(CHART_SETTINGS_STORAGE_KEY);
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
  if (stored !== null) {
    try {
      chartSettings = normalizeChartSettings(JSON.parse(stored)) || {
        ...DEFAULT_CHART_SETTINGS,
        ...DEFAULT_SMA_COLORS,
      };
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      chartSettings = { ...DEFAULT_CHART_SETTINGS, ...DEFAULT_SMA_COLORS };
    }
  }
  if (BTC_INTRADAY_INTERVAL_VALUES.has(chartSettings.interval)) {
    chartSettings = { ...chartSettings, interval: DEFAULT_CHART_SETTINGS.interval };
  }
  applyChartSettings();
}

function persistChartSettings() {
  try {
    const storedSettings = BTC_INTRADAY_INTERVAL_VALUES.has(chartSettings.interval)
      ? { ...chartSettings, interval: DEFAULT_CHART_SETTINGS.interval }
      : chartSettings;
    window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY, JSON.stringify(storedSettings));
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

// One of the five reusable absence-grammar marks: missing, not-evaluable,
// not-applicable, overdue or blocked. Each combines a shape (border style
// and icon glyph, see .absence-mark rules in tokens.css/styles.css) with a
// declared label and, for a blocked source, its declared reason — never a
// bare dash, an empty cell or a zero.
// The known_at cut is a permanent, visually distinct global control: it is
// updated here for both the collapsed traceability detail and the
// always-visible topbar chip, so every view shares the same declared cut.
function renderKnownAtCut(effectiveKnownAt) {
  const formatted = effectiveKnownAt ? formatInstant(effectiveKnownAt) : null;
  const detail = byId("known-at-status");
  const header = byId("known-at-cut-value");
  detail.replaceChildren();
  header.replaceChildren();
  if (formatted) {
    detail.textContent = `Corte: ${formatted}`;
    header.textContent = formatted;
  } else {
    detail.append(renderAbsenceMark("missing", "Sin evidencia", "Sin ejecución completa registrada"));
    header.append(renderAbsenceMark("missing", "Sin evidencia"));
  }
}

function renderAbsenceMark(kind, label, reason) {
  const mark = createElement("span", `absence-mark ${kind}`);
  mark.setAttribute("role", "status");
  mark.append(createElement("span", "absence-mark-icon"));
  mark.append(createElement("span", "absence-mark-label", label));
  if (reason) {
    mark.append(createElement("span", "absence-mark-reason", `· ${reason}`));
    mark.setAttribute("aria-label", `${label}: ${reason}`);
  } else {
    mark.setAttribute("aria-label", label);
  }
  return mark;
}

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
  if (["ready", "succeeded", "complete", "available", "valid"].includes(value)) return "good";
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

function formatMarketTimestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat(LOCALE, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }).format(parsed);
}

function marketResolution(value) {
  return MARKET_RESOLUTION_PRESENTATION[value] || MARKET_RESOLUTION_PRESENTATION.daily;
}

function formatMarketInterval(point) {
  const intraday = BTC_INTRADAY_INTERVAL_VALUES.has(point.resolution);
  const end = intraday
    ? formatMarketTimestamp(point.timestamp)
    : formatCalendarDate(point.timestamp);
  const interval =
    !point.period_start_timestamp || point.period_start_timestamp === point.timestamp
      ? end
      : `${intraday ? formatMarketTimestamp(point.period_start_timestamp) : formatCalendarDate(point.period_start_timestamp)}–${end}`;
  if (point.interval_complete === false) return `${interval} · Incompleto`;
  return point.calendar_interval_closed ? interval : `${interval} · En curso`;
}

function numericValue(value) {
  if (value === null || value === undefined || value === "") return null;
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
    const windowLabel = Number(parameters.window) === 1
      ? "1 día con datos"
      : `${parameters.window} días con datos`;
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

function formatCurrency(value) {
  return formatNumber(value, {
    style: "currency",
    currency: "USD",
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatCompactVolume(value) {
  return formatNumber(value, {
    notation: "compact",
    compactDisplay: "short",
    maximumFractionDigits: 1,
  });
}

function formatMarketVolume(value, { compact = false, includeUnit = true } = {}) {
  const presentation = marketAssetPresentation();
  const formatted = compact
    ? formatCompactVolume(value)
    : presentation.volumeUnit === "BTC"
      ? formatNumber(value, { maximumFractionDigits: 2 })
      : formatInteger(value);
  return includeUnit ? `${formatted} ${presentation.volumeLabel}` : formatted;
}

function formatRangeChange(value) {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatNumber(value, {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
}

function setSignedPercentage(target, value, title) {
  const parsed = numericValue(value);
  target.textContent = parsed === null ? "—" : formatRangeChange(parsed);
  target.className = `stat-move ${parsed > 0 ? "positive" : parsed < 0 ? "negative" : "neutral"}`;
  target.title = value === null || value === undefined ? "" : `${title}: ${value}`;
}

function formatMultiple(value) {
  const parsed = numericValue(value);
  return parsed === null ? "—" : `${formatNumber(parsed, { maximumFractionDigits: 2 })}×`;
}

function formatUnsignedPercentage(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "—";
  return formatNumber(parsed, {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
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

async function queryMarketChart() {
  marketChartDrag = null;
  byId("market-chart").classList.remove("is-panning");
  setChartBusy(true);
  setExportAvailable("export-market-csv", false);
  byId("chart-status").textContent = "Consultando el histórico local…";
  const requestedPeriod = marketChartPeriod();
  const intraday = isIntradayInterval();
  const parameters = new URLSearchParams({
    asset_id: selectedMarketAsset,
    known_at: byId("report-known-at").value.trim(),
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
    renderMarketChart(intraday ? normalizeBtcIntradayChart(payload) : payload);
  } catch (error) {
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
    setChartBusy(false);
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
      const output = createElement("dd", "", value);
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

async function queryFundamentalTrend() {
  setFundamentalBusy(true);
  setExportAvailable("export-fundamental-csv", false);
  byId("fundamental-status").textContent = "Consultando fundamentales locales…";
  const parameters = new URLSearchParams({
    asset_id: selectedMarketAsset,
    known_at: byId("report-known-at").value.trim(),
    frequency: selectedFundamentalFrequency,
  });
  try {
    renderFundamentalTrend(await api(`/api/fundamental-trend?${parameters.toString()}`));
  } catch (error) {
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

async function queryFundamentalResearch() {
  setFundamentalBusy(true);
  setExportAvailable("export-fundamental-research-csv", false);
  const parameters = new URLSearchParams({
    asset_id: selectedMarketAsset,
    known_at: byId("report-known-at").value.trim(),
    frequency: selectedFundamentalFrequency,
  });
  try {
    renderFundamentalResearch(
      await api(`/api/fundamental-analysis?${parameters.toString()}`),
    );
  } catch (error) {
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

function renderValuationHistory(payload, { preserveSelection = false } = {}) {
  valuationHistoryPayload = payload;
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
      entry.append(createElement("dt", "", label), createElement("dd", "", value));
      description.append(entry);
    }
    summary.append(description);
    const table = createElement("table", "valuation-history-table");
    const caption = createElement("caption", "", `${series.metric_key} · ${series.unit}`);
    const header = document.createElement("thead");
    header.innerHTML = "<tr><th>Fecha</th><th>Valor exacto</th><th>Resultado</th></tr>";
    const body = document.createElement("tbody");
    for (const point of series.points) {
      const row = document.createElement("tr");
      row.append(
        createElement("td", "", point.valuation_date),
        createElement("td", "", point.value),
        createElement("td", "", point.result_id),
      );
      body.append(row);
    }
    table.append(caption, header, body);
    target.append(table);
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
  const entries = [
    ["Fórmula", "(menores + 0.5 × iguales) / N; Decimal34"],
    ["Percentil", payload.empirical_percentile ?? "No definido"],
    ["Puntos previos", `${payload.coverage.prior_points} / ${payload.coverage.required_prior_points}`],
    ["Conteos", `${payload.lower_count} menores, ${payload.equal_count} iguales, ${payload.greater_count} mayores`],
  ];
  const list = createElement("dl", "valuation-history-statistics");
  for (const [name, value] of entries) {
    const entry = document.createElement("div");
    entry.append(createElement("dt", "", name), createElement("dd", "", value));
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

function localizedIssue(issue) {
  if (issue.endsWith(": latest scheduled job failed")) {
    return `${issue.slice(0, -": latest scheduled job failed".length)}: falló la actualización más reciente`;
  }
  if (issue.endsWith(": daily retry budget exhausted")) {
    return `${issue.slice(0, -": daily retry budget exhausted".length)}: se agotaron los reintentos diarios`;
  }
  if (issue.endsWith(": prior scheduled job failed")) {
    return `${issue.slice(0, -": prior scheduled job failed".length)}: falló la actualización anterior`;
  }
  if (issue.endsWith(": interrupted scheduled job")) {
    return `${issue.slice(0, -": interrupted scheduled job".length)}: actualización interrumpida`;
  }
  if (issue.endsWith(": provider check is stale")) {
    return `${issue.slice(0, -": provider check is stale".length)}: evidencia desactualizada`;
  }
  if (issue.endsWith(": latest coverage is incomplete")) {
    return `${issue.slice(0, -": latest coverage is incomplete".length)}: cobertura incompleta`;
  }
  if (issue === "operational alert monitor could not persist its result") {
    return "El monitor de alertas no pudo guardar su evaluación.";
  }
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
    const failure = new Error(message);
    failure.code = error.code;
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

function setButtonBusy(button, busy, busyLabel, idleLabel) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  if (!button.classList.contains("icon-button")) {
    button.textContent = busy ? busyLabel : idleLabel;
  }
}

function applyOverview(payload) {
  if (payload.schema_version === "operational-overview-snapshot-v1") {
    badge(
      byId("health-badge"),
      translated(payload.operational_status, STATUS_LABELS, payload.operational_status),
      statusTone(payload.operational_status),
    );
    byId("workspace-status").textContent = translated(
      payload.workspace_status,
      STATUS_LABELS,
      payload.workspace_status,
    );
    byId("workspace-counts").textContent = "Resumen operativo compacto";
    byId("run-status").textContent = payload.latest_run_status
      ? translated(payload.latest_run_status, STATUS_LABELS, payload.latest_run_status)
      : "Sin registro operativo";
    byId("run-time").textContent = "Sin lectura de historial";
    byId("traceability-status").textContent = "Sin verificación reciente";
    renderKnownAtCut(null);
    if (!payload.scheduler_enabled) {
      byId("schedule-status").textContent = "Desactivada";
      byId("schedule-next").textContent = "Solo actualización manual";
      return;
    }
    if (payload.scheduled_blocked_count > 0 || payload.scheduled_failed_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_blocked_count || payload.scheduled_failed_count)} con fallo`;
    } else if (payload.scheduled_retry_wait_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_retry_wait_count)} esperando reintento`;
    } else if (payload.scheduled_incomplete_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_incomplete_count)} incompletos`;
    } else if (payload.scheduled_stale_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_stale_count)} desactualizados`;
    } else if (payload.scheduled_running_count > 0) {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_running_count)} en curso`;
    } else {
      byId("schedule-status").textContent = `${formatInteger(payload.scheduled_current_count)} actuales de ${formatInteger(payload.scheduled_job_count)}`;
    }
    byId("schedule-next").textContent = payload.scheduled_next_retry_at
      ? `Reintento: ${formatInstant(payload.scheduled_next_retry_at)}`
      : payload.scheduled_next_run_at
        ? formatInstant(payload.scheduled_next_run_at)
        : "Sin próxima ejecución";
    return;
  }
  const operational = payload.operational;
  const workspace = operational.workspace;
  const latest = operational.latest_run;
  const scheduler = payload.scheduler;
  const alerts = payload.alerts || { enabled: false };
  const candidates = payload.candidates || { enabled: false };
  const notifications = payload.notifications || { enabled: false };

  badge(
    byId("health-badge"),
    translated(operational.status, STATUS_LABELS, operational.status),
    statusTone(operational.status),
  );
  byId("workspace-status").textContent = translated(workspace.status, STATUS_LABELS, workspace.status);
  byId("workspace-counts").textContent = `${formatInteger(workspace.counts.observations)} obs. · ${formatInteger(workspace.counts.metric_results)} métricas`;

  byId("run-status").textContent = latest
    ? translated(latest.status, STATUS_LABELS, latest.status)
    : "Sin registro operativo";
  byId("run-time").textContent = latest
    ? formatInstant(latest.completed_at || latest.started_at)
    : "Datos históricos disponibles";
  byId("traceability-status").textContent = latest?.traceability_verified
    ? "Verificada"
    : "Sin verificación reciente";
  renderKnownAtCut(latest?.effective_known_at);

  if (scheduler.enabled) {
    if (Array.isArray(scheduler.jobs)) {
      const total = scheduler.jobs.length;
      if (scheduler.failed_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.failed_count)} con fallo`;
      } else if (scheduler.incomplete_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.incomplete_count)} incompletos`;
      } else if (scheduler.stale_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.stale_count)} desactualizados`;
      } else if (scheduler.due_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.due_count)} pendientes`;
      } else if (scheduler.running_count > 0) {
        byId("schedule-status").textContent = `${formatInteger(scheduler.running_count)} en curso`;
      } else {
        byId("schedule-status").textContent = `${formatInteger(total)} trabajos automáticos`;
      }
      const nextJob = scheduler.jobs
        .slice()
        .sort((left, right) => left.next_run_at.localeCompare(right.next_run_at))[0];
      byId("schedule-next").textContent = formatInstant(
        scheduler.next_run_at,
        nextJob?.definition?.timezone || DEFAULT_TIME_ZONE,
      );
    } else {
      const config = scheduler.config;
      const scheduleStatus = byId("schedule-status");
      scheduleStatus.replaceChildren();
      if (scheduler.due) {
        scheduleStatus.append(
          renderAbsenceMark("overdue", "Vencida", `Próxima ejecución programada: ${formatInstant(scheduler.next_run_at, config.timezone)}`),
        );
      } else {
        scheduleStatus.textContent = `Automática · ${config.run_at}`;
      }
      byId("schedule-next").textContent = formatInstant(scheduler.next_run_at, config.timezone);
    }
  } else {
    byId("schedule-status").textContent = "Desactivada";
    byId("schedule-next").textContent = "Solo actualización manual";
  }

  if (alerts.enabled) {
    byId("alert-status").textContent = alerts.new_count > 0
      ? `${formatInteger(alerts.new_count)} nuevas`
      : "Sin alertas";
    byId("alert-latest").textContent = alerts.latest_alert_at
      ? formatInstant(alerts.latest_alert_at)
      : "Modo silencioso";
    byId("alert-inbox-summary").textContent = alerts.alert_count > 0
      ? `${formatInteger(alerts.alert_count)} registradas · modo silencioso`
      : "Sin incidencias · modo silencioso";
  } else {
    byId("alert-status").textContent = "Desactivadas";
    byId("alert-latest").textContent = "Monitor no configurado";
    byId("alert-inbox-summary").textContent = "Monitor no configurado";
  }

  if (candidates.enabled) {
    byId("candidate-status").textContent = candidates.new_count > 0
      ? `${formatInteger(candidates.new_count)} nuevos`
      : "Sin candidatos";
    byId("candidate-latest").textContent = candidates.latest_candidate_at
      ? formatInstant(candidates.latest_candidate_at)
      : `${formatInteger(candidates.result_count)} evaluaciones`;
    byId("candidate-inbox-summary").textContent = candidates.candidate_count > 0
      ? `${formatInteger(candidates.candidate_count)} registrados · modo silencioso`
      : `${formatInteger(candidates.result_count)} evaluaciones · sin candidatos`;
  } else {
    byId("candidate-status").textContent = "Desactivados";
    byId("candidate-latest").textContent = "Monitor no configurado";
    byId("candidate-inbox-summary").textContent = "Monitor no configurado";
  }

  if (notifications.enabled) {
    byId("candidate-notification-summary").textContent = notifications.pending_count > 0
      ? `${formatInteger(notifications.pending_count)} pendientes de ${formatInteger(notifications.total)}`
      : `${formatInteger(notifications.total)} entregadas localmente`;
  } else {
    byId("candidate-notification-summary").textContent = "Outbox local no configurado";
  }

  if (latest?.effective_known_at) byId("report-known-at").value = latest.effective_known_at;
  if (["quarterly", "annual"].includes(latest?.request?.fundamental_frequency)) {
    selectFundamentalFrequency(latest.request.fundamental_frequency);
  }
  operationalIssues = [...(operational.issues || []), ...(scheduler.issues || [])].map(localizedIssue);
  setMessage(operationalIssues.join(" · "), operationalIssues.length > 0);
}

function renderAlertInbox(payload) {
  const inbox = byId("alert-inbox");
  inbox.replaceChildren();
  if (!Array.isArray(payload.events) || payload.events.length === 0) {
    inbox.append(createElement("p", "", "No hay alertas operativas registradas."));
    return;
  }
  for (const event of payload.events) {
    const item = createElement("article", "alert-inbox-item");
    const statusLabels = {
      new: "Nueva",
      seen: "Vista",
      dismissed: "Descartada",
      resolved: "Resuelta",
      silenced: "Silenciada",
    };
    const status = createElement(
      "span",
      `alert-inbox-status ${event.status}`,
      statusLabels[event.status] || event.status,
    );
    item.append(
      createElement("strong", "", event.title),
      status,
      createElement("p", "", event.message),
      createElement("time", "", formatInstant(event.last_activated_at)),
    );
    const actions = createElement("div", "alert-inbox-actions");
    const availableActions = event.status === "new"
      ? [["seen", "Marcar vista"], ["dismissed", "Descartar"], ["resolved", "Resolver"]]
      : event.status === "seen"
        ? [["dismissed", "Descartar"], ["resolved", "Resolver"]]
        : ["dismissed", "silenced"].includes(event.status)
          ? [["resolved", "Resolver"]]
          : [];
    for (const [target, label] of availableActions) {
      const button = createElement("button", "alert-action-button", label);
      button.type = "button";
      button.addEventListener("click", () => transitionAlert(event.alert_id, target, button));
      actions.append(button);
    }
    if (availableActions.length > 0) item.append(actions);
    inbox.append(item);
  }
}

async function transitionAlert(alertId, status, button) {
  button.disabled = true;
  try {
    await api("/api/alerts/transition", {
      method: "POST",
      body: JSON.stringify({ alert_id: alertId, status }),
    });
    await Promise.all([loadAlertInbox(), refreshOverview()]);
  } catch (error) {
    setMessage(`No se pudo actualizar la alerta: ${error.message}`, true);
    button.disabled = false;
  }
}

async function loadAlertInbox() {
  const inbox = byId("alert-inbox");
  inbox.setAttribute("aria-busy", "true");
  try {
    renderAlertInbox(await api("/api/alerts?limit=50"));
  } catch (error) {
    inbox.replaceChildren(
      createElement("p", "", `No se pudo consultar la bandeja: ${error.message}`),
    );
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

const SCREENING_STATE_LABELS = Object.freeze({
  draft: "Borrador · solo replay",
  silent: "Monitoreo silencioso",
  active: "Activa · bandeja local",
  paused: "Pausada",
});

const SCREENING_OPERATOR_LABELS = Object.freeze({
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
  eq: "=",
});

function screeningField(labelText, control) {
  const label = createElement("label", "field screening-rule-field");
  label.append(createElement("span", "", labelText), control);
  return label;
}

function screeningRulePayload(configuration, form, sourceRule = null) {
  const rule = sourceRule || configuration.rule;
  if (sourceRule) {
    return {
      schema_version: "analytical-rule-configuration-update-v1",
      rule_id: configuration.rule.rule_id,
      expected_fingerprint: configuration.fingerprint,
      state: sourceRule.state,
      confirmations_required: sourceRule.confirmations_required,
      cooldown_seconds: sourceRule.cooldown_seconds,
      conditions: sourceRule.conditions.map((condition) => ({
        condition_id: condition.condition_id,
        threshold: condition.threshold,
        exit_threshold: condition.exit_threshold,
      })),
    };
  }
  const state = form.querySelector("[data-screening-state]")?.value;
  const confirmations = Number(
    form.querySelector("[data-screening-confirmations]")?.value,
  );
  const cooldownHours = Number(
    form.querySelector("[data-screening-cooldown-hours]")?.value,
  );
  if (
    !Object.hasOwn(SCREENING_STATE_LABELS, state)
    || !Number.isInteger(confirmations)
    || confirmations < 1
    || confirmations > 20
    || !Number.isFinite(cooldownHours)
    || cooldownHours < 0
  ) {
    throw new Error("Revisa el estado, las confirmaciones y la espera configurada.");
  }
  const controls = new Map(
    Array.from(form.querySelectorAll("[data-screening-condition]")).map((control) => [
      control.dataset.screeningCondition,
      control,
    ]),
  );
  const conditions = rule.conditions.map((condition) => {
    const group = controls.get(condition.condition_id);
    if (!group) throw new Error("La edición no coincide con el contrato de la regla.");
    const threshold = group.querySelector("[data-screening-threshold]")?.value.trim();
    const exitValue = group.querySelector("[data-screening-exit-threshold]")?.value.trim();
    if (!threshold) throw new Error("Cada condición requiere un umbral.");
    return {
      condition_id: condition.condition_id,
      threshold,
      exit_threshold: exitValue || null,
    };
  });
  return {
    schema_version: "analytical-rule-configuration-update-v1",
    rule_id: configuration.rule.rule_id,
    expected_fingerprint: configuration.fingerprint,
    state,
    confirmations_required: confirmations,
    cooldown_seconds: Math.round(cooldownHours * 3600),
    conditions,
  };
}

function renderScreeningBacktest(container, payload) {
  container.replaceChildren();
  const evaluated = payload.evaluations.length;
  const range = `${formatCalendarDate(payload.first_known_at)}–${formatCalendarDate(payload.last_known_at)}`;
  const grid = createElement("div", "screening-backtest-grid");
  const metrics = [
    ["Cortes", `${formatInteger(evaluated)} de ${formatInteger(payload.total_available_cuts)}`],
    [
      "Coincidencias",
      `${formatInteger(payload.matched_count)} · ${formatUnsignedPercentage(payload.match_rate)}`,
    ],
    ["Candidatos simulados", formatInteger(payload.candidate_activation_count)],
    ["No evaluables", formatInteger(payload.not_evaluable_count)],
  ];
  for (const [label, value] of metrics) {
    const item = createElement("span", "screening-backtest-metric");
    item.append(createElement("small", "", label), createElement("strong", "", value));
    grid.append(item);
  }
  container.append(
    grid,
    createElement(
      "small",
      "screening-backtest-range",
      `${range}${payload.truncated ? " · muestra limitada a los cortes más recientes" : ""}`,
    ),
    createElement(
      "small",
      "screening-backtest-limitation",
      "Replay descriptivo: no mide rentabilidad posterior ni precisión predictiva.",
    ),
  );
}

async function runScreeningBacktest(configuration, container, button) {
  setButtonBusy(button, true, "Calculando…", `Replay de ${marketAssets[selectedMarketAsset]?.symbol || "activo"}`);
  container.replaceChildren(createElement("p", "", "Leyendo snapshots persistidos…"));
  try {
    const query = new URLSearchParams({
      rule_id: configuration.rule.rule_id,
      asset_id: selectedMarketAsset,
      max_cuts: "200",
    });
    renderScreeningBacktest(
      container,
      await api(`/api/screening-backtest?${query.toString()}`),
    );
  } catch (error) {
    container.replaceChildren(
      createElement("p", "screening-rule-error", error.message),
    );
  } finally {
    setButtonBusy(
      button,
      false,
      "Calculando…",
      `Replay de ${marketAssets[selectedMarketAsset]?.symbol || "activo"}`,
    );
  }
}

async function updateScreeningRule(configuration, form, button, sourceRule = null) {
  const idleLabel = sourceRule ? "Restaurar valores iniciales" : "Guardar regla";
  setButtonBusy(button, true, "Guardando…", idleLabel);
  try {
    const payload = screeningRulePayload(configuration, form, sourceRule);
    const outcome = await api("/api/screening-rules/update", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage(
      outcome.changed
        ? "Regla versionada. Se aplicará a la próxima evidencia nueva."
        : "La regla ya tenía esos valores.",
      false,
    );
    await loadScreeningRules();
  } catch (error) {
    setMessage(`No se pudo guardar la regla: ${error.message}`, true);
    button.disabled = false;
  }
}

function renderScreeningRule(configuration) {
  const { rule, default_rule: defaultRule } = configuration;
  const card = createElement("article", "screening-rule-card");
  const heading = createElement("div", "screening-rule-heading");
  const title = createElement("div");
  title.append(
    createElement("strong", "", rule.name_es),
    createElement(
      "small",
      "",
      `${rule.domain === "market" ? "Mercado" : "Fundamentales"} · v${rule.rule_version}`,
    ),
  );
  heading.append(
    title,
    createElement(
      "span",
      `screening-rule-badge${configuration.customized ? " customized" : ""}`,
      configuration.customized ? "Personalizada" : "Inicial",
    ),
  );
  const form = createElement("form", "screening-rule-form");
  const state = document.createElement("select");
  state.dataset.screeningState = "true";
  for (const [value, label] of Object.entries(SCREENING_STATE_LABELS)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = rule.state === value;
    state.append(option);
  }
  const confirmations = document.createElement("input");
  confirmations.type = "number";
  confirmations.min = "1";
  confirmations.max = "20";
  confirmations.step = "1";
  confirmations.value = String(rule.confirmations_required);
  confirmations.dataset.screeningConfirmations = "true";
  const cooldown = document.createElement("input");
  cooldown.type = "number";
  cooldown.min = "0";
  cooldown.max = "8760";
  cooldown.step = "0.25";
  cooldown.value = String(rule.cooldown_seconds / 3600);
  cooldown.dataset.screeningCooldownHours = "true";
  const controls = createElement("div", "screening-rule-controls");
  controls.append(
    screeningField("Estado", state),
    screeningField("Confirmaciones", confirmations),
    screeningField("Espera (horas)", cooldown),
  );
  const conditionGrid = createElement("div", "screening-condition-grid");
  for (const condition of rule.conditions) {
    const group = createElement("fieldset", "screening-condition");
    group.dataset.screeningCondition = condition.condition_id;
    const legend = createElement(
      "legend",
      "",
      `${condition.label_es} ${SCREENING_OPERATOR_LABELS[condition.operator] || condition.operator}`,
    );
    const threshold = document.createElement("input");
    threshold.type = "text";
    threshold.inputMode = "decimal";
    threshold.value = condition.threshold;
    threshold.dataset.screeningThreshold = "true";
    group.append(legend, screeningField("Umbral de entrada", threshold));
    const exitThreshold = document.createElement("input");
    exitThreshold.type = "text";
    exitThreshold.inputMode = "decimal";
    exitThreshold.value = condition.exit_threshold ?? "";
    exitThreshold.dataset.screeningExitThreshold = "true";
    group.append(screeningField("Umbral de salida", exitThreshold));
    if (condition.unit === "ratio") {
      group.append(
        createElement(
          "small",
          "",
          condition.metric_key === "market.history.relative_volume"
            ? "Múltiplo; por ejemplo, 1.5×."
            : "Ratio decimal; por ejemplo, 0.60 = 60%.",
        ),
      );
    }
    conditionGrid.append(group);
  }
  const actions = createElement("div", "screening-rule-actions");
  const save = createElement("button", "button primary compact", "Guardar regla");
  save.type = "submit";
  const reset = createElement(
    "button",
    "button secondary compact",
    "Restaurar valores iniciales",
  );
  reset.type = "button";
  reset.disabled = !configuration.customized;
  const backtest = createElement(
    "button",
    "button secondary compact",
    `Replay de ${marketAssets[selectedMarketAsset]?.symbol || "activo"}`,
  );
  backtest.type = "button";
  actions.append(save, reset, backtest);
  const replay = createElement("div", "screening-backtest-result");
  replay.append(createElement("p", "", "Replay aún no ejecutado para el activo seleccionado."));
  form.append(controls, conditionGrid, actions, replay);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void updateScreeningRule(configuration, form, save);
  });
  reset.addEventListener("click", () => {
    void updateScreeningRule(configuration, form, reset, defaultRule);
  });
  backtest.addEventListener("click", () => {
    void runScreeningBacktest(configuration, replay, backtest);
  });
  card.append(heading, form);
  return card;
}

function renderScreeningRules(payload) {
  const container = byId("screening-rules");
  container.replaceChildren();
  if (!Array.isArray(payload.configurations) || payload.configurations.length === 0) {
    container.append(createElement("p", "", "No hay reglas de screening configuradas."));
    byId("screening-rules-summary").textContent = "Registro no configurado";
    return;
  }
  byId("screening-rules-summary").textContent =
    `${formatInteger(payload.configurations.length)} reglas · ${formatInteger(payload.total_revisions)} revisiones locales`;
  for (const configuration of payload.configurations) {
    container.append(renderScreeningRule(configuration));
  }
}

async function loadScreeningRules() {
  const container = byId("screening-rules");
  container.setAttribute("aria-busy", "true");
  try {
    screeningRuleSnapshot = await api("/api/screening-rules");
    renderScreeningRules(screeningRuleSnapshot);
  } catch (error) {
    container.replaceChildren(
      createElement("p", "screening-rule-error", `No se pudieron cargar las reglas: ${error.message}`),
    );
  } finally {
    container.setAttribute("aria-busy", "false");
  }
}

function formatCandidateCondition(condition, definition) {
  if (condition.state === "not_evaluable") return `${definition.label_es}: sin evidencia`;
  const parsed = numericValue(condition.observed_value);
  if (parsed === null) return `${definition.label_es}: —`;
  const percentageKeys = new Set([
    "fundamental.liabilities_to_assets",
    "fundamental.net_margin",
    "fundamental.revenue_yoy_growth",
  ]);
  const value = percentageKeys.has(condition.metric_key)
    ? formatUnsignedPercentage(parsed)
    : condition.unit === "ratio"
      ? `${formatNumber(parsed, { maximumFractionDigits: 2 })}×`
      : `${formatNumber(parsed, { maximumFractionDigits: 2 })} ${condition.unit}`;
  return `${definition.label_es}: ${value}`;
}

function renderCandidateInbox(payload) {
  const inbox = byId("candidate-inbox");
  inbox.replaceChildren();
  if (!Array.isArray(payload.items) || payload.items.length === 0) {
    inbox.append(createElement("p", "", "No hay candidatos analíticos registrados."));
    return;
  }
  const statusLabels = {
    new: "Nuevo",
    seen: "Visto",
    dismissed: "Descartado",
    resolved: "Resuelto",
    silenced: "Silenciado",
  };
  for (const itemPayload of payload.items) {
    const { event, result } = itemPayload;
    const assetLabel = marketAssets[result.asset_id]?.symbol || result.asset_id;
    const item = createElement("article", "alert-inbox-item candidate-inbox-item");
    item.append(
      createElement("strong", "", `${result.rule.name_es} · ${assetLabel}`),
      createElement(
        "span",
        `alert-inbox-status ${event.status}`,
        statusLabels[event.status] || event.status,
      ),
    );
    const conditions = createElement("ul", "candidate-condition-list");
    result.conditions.forEach((condition, index) => {
      const definition = result.rule.conditions[index];
      conditions.append(
        createElement(
          "li",
          condition.state,
          formatCandidateCondition(condition, definition),
        ),
      );
    });
    item.append(
      conditions,
      createElement(
        "span",
        "candidate-meta",
        `${formatCalendarDate(event.as_of)} · ${formatInteger(event.confirmations)} confirmación${event.confirmations === 1 ? "" : "es"}`,
      ),
      createElement("time", "", formatInstant(event.activated_at)),
    );
    const actions = createElement("div", "alert-inbox-actions");
    const availableActions = event.status === "new"
      ? [["seen", "Marcar visto"], ["dismissed", "Descartar"], ["resolved", "Resolver"]]
      : event.status === "seen"
        ? [["dismissed", "Descartar"], ["resolved", "Resolver"]]
        : ["dismissed", "silenced"].includes(event.status)
          ? [["resolved", "Resolver"]]
          : [];
    for (const [target, label] of availableActions) {
      const button = createElement("button", "alert-action-button", label);
      button.type = "button";
      button.addEventListener(
        "click",
        () => transitionCandidate(event.candidate_id, target, button),
      );
      actions.append(button);
    }
    if (availableActions.length > 0) item.append(actions);
    inbox.append(item);
  }
}

async function transitionCandidate(candidateId, status, button) {
  button.disabled = true;
  try {
    await api("/api/candidates/transition", {
      method: "POST",
      body: JSON.stringify({ candidate_id: candidateId, status }),
    });
    await Promise.all([loadCandidateInbox(), refreshOverview()]);
  } catch (error) {
    setMessage(`No se pudo actualizar el candidato: ${error.message}`, true);
    button.disabled = false;
  }
}

async function loadCandidateInbox() {
  const inbox = byId("candidate-inbox");
  inbox.setAttribute("aria-busy", "true");
  try {
    renderCandidateInbox(await api("/api/candidates?limit=50"));
  } catch (error) {
    inbox.replaceChildren(
      createElement("p", "", `No se pudo consultar la bandeja: ${error.message}`),
    );
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

function renderCandidateNotifications(payload) {
  const inbox = byId("candidate-notifications");
  inbox.replaceChildren();
  if (!Array.isArray(payload.items) || payload.items.length === 0) {
    inbox.append(createElement("p", "", "No hay notificaciones locales pendientes."));
    return;
  }
  for (const view of payload.items) {
    const notification = view.item;
    const item = createElement("article", "alert-inbox-item candidate-inbox-item");
    item.append(
      createElement("strong", "", `${notification.rule_id} · ${notification.asset_id}`),
      createElement("p", "", `Candidato local ${notification.candidate_id}`),
      createElement("time", "", formatInstant(notification.created_at)),
    );
    item.append(
      createElement(
        "span",
        `alert-inbox-status ${view.status}`,
        view.status === "acknowledged" ? "Entrega confirmada" : "Pendiente",
      ),
    );
    if (view.status === "acknowledged") {
      inbox.append(item);
      continue;
    }
    const button = createElement("button", "alert-action-button", "Confirmar entrega");
    button.type = "button";
    button.addEventListener("click", () => acknowledgeCandidateNotification(notification.notification_id, button));
    const actions = createElement("div", "alert-inbox-actions");
    actions.append(button);
    item.append(actions);
    inbox.append(item);
  }
}

async function acknowledgeCandidateNotification(notificationId, button) {
  button.disabled = true;
  try {
    await api("/api/v1/candidate-notifications/acknowledge", {
      method: "POST",
      body: JSON.stringify({ notification_id: notificationId }),
    });
    await Promise.all([loadCandidateNotifications(), refreshOverview()]);
  } catch (error) {
    setMessage(`No se pudo confirmar la notificación: ${error.message}`, true);
    button.disabled = false;
  }
}

async function loadCandidateNotifications() {
  const inbox = byId("candidate-notifications");
  inbox.setAttribute("aria-busy", "true");
  try {
    renderCandidateNotifications(await api("/api/v1/candidate-notifications"));
  } catch (error) {
    inbox.replaceChildren(
      createElement("p", "", `No se pudieron consultar las notificaciones: ${error.message}`),
    );
  } finally {
    inbox.setAttribute("aria-busy", "false");
  }
}

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

function renderMarketClocks(now = new Date()) {
  const instant = now.toISOString();
  for (const definition of MARKET_CLOCK_DEFINITIONS) {
    const formatters = MARKET_CLOCK_FORMATTERS.get(definition.timeZone);
    const timeElement = byId(definition.timeElementId);
    timeElement.dateTime = instant;
    timeElement.textContent = formatters.time.format(now);
    byId(definition.dateElementId).textContent = formatters.date.format(now);
  }
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
      await Promise.all([
        queryMarketChart(),
        ...(presentation.hasFundamentals
          ? [queryFundamentalTrend(), queryFundamentalResearch()]
          : []),
      ]);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setButtonBusy(button, false, "Ejecutando…", idleLabel);
  }
});

async function queryReport() {
  const button = byId("report-button");
  const reportArea = byId("report-area");
  const assetId = selectedMarketAsset;
  const presentation = marketAssets[assetId];
  if (!presentation?.hasFundamentals) {
    resetListedCompanyReport();
    return;
  }
  const request = ++listedCompanyReportRequest;
  reportPayload = null;
  setExportAvailable("export-report-json", false);
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
    parameters.set("asset_id", assetId);
    const report = await api(`/api/listed-company-report?${parameters.toString()}`);
    if (
      request !== listedCompanyReportRequest
      || assetId !== selectedMarketAsset
      || report?.asset?.asset_id !== assetId
    ) return;
    renderReport(report);
    setMessage(operationalIssues.join(" · "), operationalIssues.length > 0);
  } catch (error) {
    if (request !== listedCompanyReportRequest || assetId !== selectedMarketAsset) return;
    setMessage(error.message, true);
  } finally {
    if (request === listedCompanyReportRequest && assetId === selectedMarketAsset) {
      reportArea.setAttribute("aria-busy", "false");
      setButtonBusy(button, false, "Consultando…", "Consultar análisis");
    }
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

for (const link of document.querySelectorAll(".nav-link")) {
  link.addEventListener("click", () => {
    for (const candidate of document.querySelectorAll(".nav-link")) {
      candidate.classList.toggle("active", candidate === link);
      if (candidate === link) candidate.setAttribute("aria-current", "page");
      else candidate.removeAttribute("aria-current");
    }
  });
}

byId("valuation-nav-link").addEventListener("click", () => {
  if (valuationPayload === null) void queryValuation();
});

byId("alert-inbox-panel").addEventListener("toggle", (event) => {
  if (event.currentTarget.open) void loadAlertInbox();
});

byId("candidate-inbox-panel").addEventListener("toggle", (event) => {
  if (event.currentTarget.open) void loadCandidateInbox();
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
  if (marketAssetPresentation().supportsCryptoDerivatives && byId("crypto-derivatives-panel").open) {
    cryptoDerivativesPayload = null;
    void queryCryptoDerivatives();
  }
});

function populateMarketComparisonAssets() {
  const benchmark = byId("comparison-benchmark");
  const assets = byId("comparison-assets");
  const selectedCurrency = marketAssetPresentation().quoteCurrency;
  benchmark.replaceChildren();
  assets.replaceChildren();
  for (const presentation of Object.values(marketAssets)) {
    if (presentation.quoteCurrency !== selectedCurrency) continue;
    const label = `${presentation.symbol} · ${presentation.name}`;
    const benchmarkOption = document.createElement("option");
    benchmarkOption.value = presentation.assetId;
    benchmarkOption.textContent = label;
    benchmark.append(benchmarkOption);
    const assetOption = document.createElement("option");
    assetOption.value = presentation.assetId;
    assetOption.textContent = label;
    assetOption.selected = presentation.assetId === selectedMarketAsset;
    assets.append(assetOption);
  }
  benchmark.value = selectedMarketAsset;
  const firstPeer = [...assets.options].find((option) => option.value !== selectedMarketAsset);
  if (firstPeer) firstPeer.selected = true;
}

function comparisonSelectedAssets() {
  return [...byId("comparison-assets").selectedOptions].map((option) => option.value);
}

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
    // Every comparison figure below -- including the absence-mark branches,
    // which style themselves -- ends up tabular/monospace/right-aligned:
    // numeric branches are wrapped in the shared .figure utility class.
    const correlation = series.metrics.correlation_status === "not_applicable"
      ? notApplicableMark
      : `<span class="figure">${comparisonPercent(series.metrics.correlation_to_benchmark)}</span>`;
    const beta = series.metrics.beta_status === "not_applicable"
      ? notApplicableMark
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
  if (!assets.includes(benchmark)) assets.unshift(benchmark);
  if (assets.length < 2 || assets.length > 5) {
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

byId("market-comparison-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await queryMarketComparison();
  } catch (error) {
    byId("comparison-status").textContent = error.message;
  }
});

byId("comparison-benchmark").addEventListener("change", () => {
  for (const option of byId("comparison-assets").options) {
    if (option.value === byId("comparison-benchmark").value) option.selected = true;
  }
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

async function initialize() {
  initializeTheme();
  captureDefaultSmaColors();
  await loadMarketAssets();
  await loadAssetPreferences();
  initializeChartSettings();
  applySelectedMarketAsset();
  populateMarketComparisonAssets();
  startMarketClocks();
  await refreshOverview();
  await Promise.all([
    queryReport(),
    queryMarketChart(),
    queryFundamentalTrend(),
    queryFundamentalResearch(),
  ]);
}

initialize();
