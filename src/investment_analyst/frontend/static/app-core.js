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

// Categorical/decorative: --compare-series-1..5 (read here) and
// --series-sma-5/20/50 below stay outside UI-3's warm-canvas repalette on
// purpose. They exist to stay mutually DISTINGUISHABLE from each other on
// the same chart; collapsing five comparison lines or three SMA windows
// into shades of one warm hue would destroy the exact legibility they
// exist for. --series-close is not exempt -- it is the single primary
// price line, so it carries the warm accent language like every other
// singular UI accent.
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

const SMA_COLOR_PROPERTY_NAMES = Object.freeze(["--series-sma-5", "--series-sma-20", "--series-sma-50"]);

// applyChartSettings() below pins each of these three custom properties as
// an INLINE style on <html> so the chart SVG (which reads them via
// var(--series-sma-N)) responds immediately to a settings change. That
// inline value has higher cascade specificity than tokens.css's
// :root[data-theme] rule, so a naive getComputedStyle() read here would
// see the last-applied color -- default or customized, old theme or new
// -- instead of the active theme's own value. Temporarily clearing the
// inline override, reading the resulting pure cascade value, then
// restoring whatever was there is the only way to read "what this theme
// actually declares" independently of chart-settings history.
function captureDefaultSmaColors() {
  const inlineStyle = document.documentElement.style;
  const savedInlineValues = SMA_COLOR_PROPERTY_NAMES.map((name) => inlineStyle.getPropertyValue(name));
  for (const name of SMA_COLOR_PROPERTY_NAMES) inlineStyle.removeProperty(name);
  DEFAULT_SMA_COLORS = {
    shortColor: designToken("--series-sma-5"),
    longColor: designToken("--series-sma-20"),
    thirdColor: designToken("--series-sma-50"),
  };
  SMA_COLOR_PROPERTY_NAMES.forEach((name, index) => {
    if (savedInlineValues[index]) inlineStyle.setProperty(name, savedInlineValues[index]);
  });
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
const BVL_SESSION_PERIODS = Object.freeze({
  summer: Object.freeze({ open: 8 * 60 + 30, close: 14 * 60 + 50, label: "08:30–14:50" }),
  winter: Object.freeze({ open: 9 * 60 + 30, close: 15 * 60 + 50, label: "09:30–15:50" }),
});
const BVL_SESSION_STATES = Object.freeze({
  weekend: Object.freeze({ label: "Fuera de sesión · fin de semana", tone: "neutral" }),
  before: Object.freeze({ label: "Antes de apertura regular", tone: "neutral" }),
  open: Object.freeze({ label: "Dentro de sesión regular", tone: "open" }),
  after: Object.freeze({ label: "Después del cierre regular", tone: "neutral" }),
});
const LIMA_WEEKDAY_ORDER = Object.freeze(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]);
const LIMA_DATE_PARTS_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: DEFAULT_TIME_ZONE,
  weekday: "short",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
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
let reviewCandidateItems = [];
let reviewAlertItems = [];
let reviewSelection = null;
let reviewSelectionMissing = false;

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
let selectMarketAssetForNavigation = null;
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
let marketChartRequestSequence = 0;
let fundamentalTrendRequestSequence = 0;
let fundamentalResearchRequestSequence = 0;
let activoBoardRequestSequence = 0;
let boardDataReady = false;
const loadedBoardIds = new Set();
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
    renderKnownAtCut(byId("report-known-at").value.trim());
    marketChartPayload = null;
    marketChartViewport = null;
    marketChartDrag = null;
    resetValuation();
    resetCryptoDerivatives();
    // An asset switch starts at the market surface, as the former sidebar
    // navigation did; capability changes still use the same deterministic
    // fallback in selectAssetSubtab().
    activeAssetSubtabId = "mercado";
    applySelectedMarketAsset();
    applyChartSettings();

    // The former asset-specific fan-out (`presentation.hasFundamentals`,
    // `queryReport()`, and its companion queries) is intentionally centralized
    // in the visible-board dispatcher below.
    invalidateDeferredBoardLoads();
    activateBoard(boardIdFromLocationHash(), { focus: false });
  }

  selectMarketAssetForNavigation = selectComboboxOption;

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

const ASSET_SCOPE_BOARD_IDS = new Set(["activo"]);
let activeAssetSubtabId = "mercado";

function assetSubtabButtons() {
  return [...document.querySelectorAll(".asset-subtab")];
}

function assetSubtabIsAvailable(button) {
  return Boolean(button) && !button.classList.contains("hidden") && !button.disabled;
}

function selectAssetSubtab(requestedId) {
  const buttons = assetSubtabButtons();
  const marketButton = buttons.find((button) => button.dataset.assetSubtab === "mercado");
  const requestedButton = buttons.find((button) => button.dataset.assetSubtab === requestedId);
  const selectedButton = assetSubtabIsAvailable(requestedButton)
    ? requestedButton
    : marketButton;
  if (!selectedButton) return;

  activeAssetSubtabId = selectedButton.dataset.assetSubtab;
  for (const button of buttons) {
    const selected = button === selectedButton;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    const section = byId(button.getAttribute("aria-controls"));
    if (section) section.hidden = !selected;
  }
}

function renderAssetScopeForBoard(boardId) {
  const scopeBar = byId("asset-scope-bar");
  const shouldShow = ASSET_SCOPE_BOARD_IDS.has(boardId);
  scopeBar.hidden = !shouldShow;
  if (!shouldShow) return;

  const activeBoard = byId(`board-${boardId}`);
  if (activeBoard) activeBoard.prepend(scopeBar);
  const subtabs = byId("asset-subtabs");
  subtabs.hidden = boardId !== "activo";
  if (boardId === "activo") selectAssetSubtab(activeAssetSubtabId);
}

function initializeAssetSubtabs() {
  const buttons = assetSubtabButtons();
  for (const button of buttons) {
    button.addEventListener("click", () => {
      selectAssetSubtab(button.dataset.assetSubtab);
    });
    button.addEventListener("keydown", (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const available = buttons.filter(assetSubtabIsAvailable);
      const currentIndex = available.indexOf(button);
      const nextIndex = event.key === "Home"
        ? 0
        : event.key === "End"
          ? available.length - 1
          : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + available.length) % available.length;
      const next = available[nextIndex];
      selectAssetSubtab(next.dataset.assetSubtab);
      next.focus();
    });
  }
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
  selectAssetSubtab(activeAssetSubtabId);
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
  byId("asset-refresh-title").textContent = presentation.refreshLabel;
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
    if (
      request !== cryptoDerivativesRequest
      || assetId !== selectedMarketAsset
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    cryptoDerivativesPayload = payload;
    renderCryptoDerivatives(payload);
  } catch (error) {
    if (
      request !== cryptoDerivativesRequest
      || assetId !== selectedMarketAsset
      || knownAt !== byId("report-known-at").value.trim()
    ) return;
    cryptoDerivativesPayload = null;
    byId("crypto-derivatives-coverage").textContent = "No disponible";
    byId("crypto-derivatives-status").textContent = error.message;
  } finally {
    if (
      request === cryptoDerivativesRequest
      && assetId === selectedMarketAsset
      && knownAt === byId("report-known-at").value.trim()
    ) {
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
  const knownAt = String(effectiveKnownAt ?? "").trim();
  const formatted = knownAt && !Number.isNaN(Date.parse(knownAt)) ? knownAt : null;
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
