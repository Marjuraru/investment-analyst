"""Unit and Node VM behavioral tests for INVESTOR-NAVIGATION-STATE."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

INDEX_HTML = (
    files("investment_analyst.frontend").joinpath("static/index.html").read_text(encoding="utf-8")
)
APP_CORE = (
    files("investment_analyst.frontend").joinpath("static/app-core.js").read_text(encoding="utf-8")
)
APP_SHELL = (
    files("investment_analyst.frontend").joinpath("static/app-shell.js").read_text(encoding="utf-8")
)
APP_MESA = (
    files("investment_analyst.frontend").joinpath("static/app-mesa.js").read_text(encoding="utf-8")
)
APP_ANALYSIS = (
    files("investment_analyst.frontend")
    .joinpath("static/app-analysis.js")
    .read_text(encoding="utf-8")
)
APP_OPERATIONS = (
    files("investment_analyst.frontend")
    .joinpath("static/app-operations.js")
    .read_text(encoding="utf-8")
)
APP_TECHNICAL = (
    files("investment_analyst.frontend")
    .joinpath("static/app-technical.js")
    .read_text(encoding="utf-8")
)
APP_CAZATIBURONES = (
    files("investment_analyst.frontend")
    .joinpath("static/app-cazatiburones.js")
    .read_text(encoding="utf-8")
)
STYLES_MESA = (
    files("investment_analyst.frontend")
    .joinpath("static/styles-mesa.css")
    .read_text(encoding="utf-8")
)
STYLES_SHELL = (
    files("investment_analyst.frontend")
    .joinpath("static/styles-shell.css")
    .read_text(encoding="utf-8")
)


def _function_source(source: str, name: str) -> str:
    marker_async = f"async function {name}("
    marker_sync = f"function {name}("
    if marker_async in source:
        start = source.index(marker_async)
    elif marker_sync in source:
        start = source.index(marker_sync)
    else:
        raise AssertionError(f"function {name} not found")
    brace = source.index("{\n", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


def test_static_contracts_for_navigation_invalidation_matrix_and_controls() -> None:
    assert 'id="mesa-revalidate-btn"' in INDEX_HTML
    assert "Actualizar Mesa" in INDEX_HTML
    assert ".mesa-revalidation-notice" in STYLES_MESA
    assert ".view-preview-verifying" in STYLES_SHELL
    assert "NAVIGATION_INVALIDATION_MATRIX" in APP_CORE
    for event_name in (
        "asset_select",
        "known_at_change",
        "workspace_change",
        "generation_change",
        "market_range_or_settings_change",
        "fundamental_frequency_change",
        "valuation_date_or_filing_change",
        "operation_success",
        "operation_uncertain",
        "preferences_change",
    ):
        assert event_name in APP_CORE
    board_loads_block = APP_SHELL.split("const BOARD_DEFERRED_LOADS = Object.freeze({")[1].split(
        "});"
    )[0]
    assert "refreshOverview" not in board_loads_block
    assert "resetCazatiburonesNavigationContext" in APP_CAZATIBURONES
    assert "resetMarketComparisonResults" in APP_TECHNICAL
    assert "resetDailyMarketPatterns" in APP_TECHNICAL
    assert 'invalidateNavigationDependencies("operation_success"' in APP_ANALYSIS
    assert 'invalidateNavigationDependencies("operation_uncertain"' in APP_ANALYSIS
    assert 'invalidateNavigationDependencies("operation_success"' in APP_OPERATIONS
    assert 'invalidateNavigationDependencies("operation_uncertain"' in APP_OPERATIONS
    assert "Vista anterior; verificando" in APP_ANALYSIS


def test_navigation_matrix_global_cut_and_local_view_preview_v2_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for JavaScript navigation state verification")

    matrix_start = APP_CORE.index("const NAVIGATION_INVALIDATION_MATRIX = Object.freeze({")
    matrix_end = (
        APP_CORE.index("});\n\nfunction invalidateNavigationDependencies", matrix_start) + 3
    )
    matrix_source = APP_CORE[matrix_start:matrix_end]

    core_funcs = "\n\n".join(
        _function_source(APP_CORE, name)
        for name in (
            "invalidateNavigationDependencies",
            "updateReadRuntimeContext",
            "buildViewPreviewKey",
            "clearViewPreviews",
            "rememberViewPreview",
            "loadViewPreview",
            "markInvestorOverviewForRevalidation",
            "resetInvestorOverviewCache",
        )
    )

    script = f"""
const NAVIGATION_REVALIDATION_WINDOW_MS = 60_000;
const VIEW_CONTEXT_STORAGE_KEY = "investment-analyst-view-context-v1";
const VIEW_CONTEXT_PREVIEW_PREFIX = "investment-analyst-view-preview-v1:";
const VIEW_CONTEXT_PREVIEW_SCHEMA_VERSION = "local-view-preview-v2";
const VIEW_CONTEXT_MAX_ENTRIES = 8;
const VIEW_CONTEXT_MAX_BYTES = 2 * 1024 * 1024;
const VIEW_CONTEXT_PREVIEW_MAX_ENTRIES = 5;
const VIEW_CONTEXT_PREVIEW_MAX_BYTES = 1536 * 1024;
const VIEW_CONTEXT_PREVIEW_TOTAL_MAX_BYTES = 2 * 1024 * 1024;
let viewPreviewSequence = 0;

{matrix_source}

let currentWorkspaceId = "ws-1";
let readRuntimeGeneration = "gen-1";
let selectedMarketAsset = "equity:us:aapl";
let selectedFundamentalFrequency = "annual";
let activoBoardRequestSequence = 0;
let marketChartRequestSequence = 0;
let fundamentalTrendRequestSequence = 0;
let fundamentalResearchRequestSequence = 0;
let valuationRequestSequence = 0;
let listedCompanyReportRequest = 0;
let cryptoDerivativesRequest = 0;
const loadedBoardIds = new Set([
  "mesa",
  "activo",
  "tecnico",
  "revisar",
  "cazatiburones",
  "sistema",
]);
const activeAssetSubtabKeys = new Map([
  ["mercado", "mercado-key"],
  ["fundamentales", "fund-key"],
  ["valoracion", "val-key"],
  ["analisis", "analisis-key"],
]);
const investorOverviewState = {{
  contextKey: "ctx-1",
  knownAt: "2026-10-22T00:00:00Z",
  workspaceId: "ws-1",
  generation: "gen-1",
  catalogVersion: "v1",
  catalogSha256: "sha-1",
  windows: null,
  approxBytes: 100,
  needsRevalidation: false,
  assetFetchedAt: new Map([["equity:us:aapl", 1000]]),
  assetByteSizes: new Map([["equity:us:aapl", 100]]),
  byAssetId: new Map([["equity:us:aapl", {{ asset_id: "equity:us:aapl" }}]]),
}};

let mesaResetCalls = 0;
let mesaMarkCalls = 0;
let cazaContextResetCalls = 0;
let tecnicoResetCalls = 0;

function resetMesaNavigationContext() {{ mesaResetCalls += 1; }}
function markMesaForRevalidation() {{ mesaMarkCalls += 1; }}
function resetCazatiburonesNavigationContext() {{ cazaContextResetCalls += 1; }}
function resetMarketComparisonResults() {{ tecnicoResetCalls += 1; }}
function resetDailyMarketPatterns() {{}}
function invalidateScreeningOutcomes() {{}}
function resetAnnualHorizons() {{}}
function clearActiveAssetContextSync() {{ activeAssetSubtabKeys.clear(); }}
function invalidateValuationFilingSelection() {{}}
function invalidateDeferredBoardLoads() {{ loadedBoardIds.clear(); }}

const storageMap = new Map();
const sessionStorage = {{
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
  get length() {{ return storageMap.size; }},
  key: (i) => Array.from(storageMap.keys())[i] ?? null,
}};

function byId(id) {{
  if (id === "report-known-at") return {{ value: "2026-10-22T00:00:00Z" }};
  return null;
}}

{core_funcs}

// 1. Asset selection preserves global known_at, Mesa, and Cazatiburones
const cutBefore = byId("report-known-at").value;
invalidateNavigationDependencies("asset_select", {{ assetId: "equity:us:msft" }});
if (byId("report-known-at").value !== cutBefore) {{
  throw new Error("asset_select mutated global known_at");
}}
if (!loadedBoardIds.has("mesa") || !loadedBoardIds.has("cazatiburones")) {{
  throw new Error("asset_select invalidated Mesa or Cazatiburones universe");
}}
if (loadedBoardIds.has("activo") || loadedBoardIds.has("tecnico")) {{
  throw new Error("asset_select did not invalidate Activo and Tecnico");
}}
if (activeAssetSubtabKeys.size !== 0) {{
  throw new Error("asset_select did not clear activeAssetSubtabKeys");
}}
if (mesaResetCalls !== 0) {{
  throw new Error("asset_select unexpectedly reset Mesa");
}}

// 2. Preferences and operation events mark Mesa for in-place revalidation
invalidateNavigationDependencies("preferences_change");
if (!loadedBoardIds.has("mesa") || mesaMarkCalls !== 1) {{
  throw new Error("preferences_change did not mark Mesa for in-place revalidation");
}}
invalidateNavigationDependencies("operation_success", {{ operation: "manual_refresh" }});
if (!loadedBoardIds.has("mesa") || !investorOverviewState.needsRevalidation) {{
  throw new Error("operation_success did not preserve Mesa marker while requesting revalidation");
}}

// 3. local-view-preview-v2 key structure and <= 5 entries cap
const key1 = buildViewPreviewKey("market-chart", {{
  assetId: "equity:us:aapl",
  knownAt: "2026-10-22T00:00:00Z",
  period: "6M",
  interval: "1d",
  smaWindows: "5,20,50",
  marketAsOf: "2026-10-21T20:00:00Z",
}});
if (
  !key1.startsWith("local-view-preview-v2|") ||
  !key1.includes("ws=ws-1") ||
  !key1.includes("gen=gen-1") ||
  !key1.includes("endpoint=market-chart") ||
  !key1.includes("asset=equity:us:aapl") ||
  !key1.includes("known_at=2026-10-22T00:00:00Z") ||
  !key1.includes("period=6M")
) {{
  throw new Error("Invalid local-view-preview-v2 key format: " + key1);
}}

for (let i = 0; i < 7; i += 1) {{
  const k = buildViewPreviewKey("market-chart", {{
    assetId: `equity:us:asset-${{i}}`,
    knownAt: "2026-10-22T00:00:00Z",
    period: "6M",
  }});
  if (!rememberViewPreview(k, {{ idx: i }})) {{
    throw new Error("Failed to store preview entry " + i);
  }}
}}
const storedPreviewKeys = Array.from(storageMap.keys()).filter((k) =>
  k.startsWith(VIEW_CONTEXT_PREVIEW_PREFIX)
);
if (storedPreviewKeys.length !== 5) {{
  throw new Error(`Expected <= 5 preview entries, got ${{storedPreviewKeys.length}}`);
}}
const evictedKey = buildViewPreviewKey("market-chart", {{
  assetId: "equity:us:asset-0",
  knownAt: "2026-10-22T00:00:00Z",
  period: "6M",
}});
if (loadViewPreview(evictedKey) !== null) {{
  throw new Error("Oldest preview entry was not evicted at cap 5");
}}

// 4. Workspace or generation change invalidates dependencies and purges previews
updateReadRuntimeContext({{
  workspace_id: "ws-2",
  read_runtime: {{ generation: "gen-2" }},
}});
const remainingAfterWsChange = Array.from(storageMap.keys()).filter((k) =>
  k.startsWith(VIEW_CONTEXT_PREVIEW_PREFIX)
);
if (remainingAfterWsChange.length !== 0) {{
  throw new Error("Workspace/generation change did not purge local-view-preview-v2 entries");
}}
if (mesaResetCalls < 1 || cazaContextResetCalls < 1) {{
  throw new Error("Workspace/generation change did not reset Mesa/Cazatiburones contexts");
}}

console.log(JSON.stringify({{ passed: true }}));
"""
    result = subprocess.run(
        [node, "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is True


def test_mesa_60s_retention_and_in_place_revalidation_preserves_dom_rows_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for JavaScript Mesa retention and revalidation smoke")

    core_slice = APP_CORE[APP_CORE.index("function parseDecimalString") :]
    mesa_slice = APP_MESA[APP_MESA.index("const MESA_INVESTOR_BATCH_SIZE") :]

    script = f"""
const LOCALE = "es-PE";
const NAVIGATION_REVALIDATION_WINDOW_MS = 60_000;
const INVESTOR_OVERVIEW_BATCH_SIZE = 10;
const INVESTOR_OVERVIEW_MAX_SNAPSHOT = 100;
const INVESTOR_OVERVIEW_MAX_BYTES = 2 * 1024 * 1024;
let currentClockMs = 1_000_000;
const RealDate = Date;
globalThis.Date = class extends RealDate {{
  constructor(...args) {{
    if (args.length === 0) super(currentClockMs);
    else super(...args);
  }}
  static now() {{ return currentClockMs; }}
}};

let currentWorkspaceId = "ws-1";
let readRuntimeGeneration = "gen-1";
let selectedMarketAsset = "equity:us:aapl";
const marketAssets = {{
  "equity:us:aapl": {{
    assetId: "equity:us:aapl",
    symbol: "AAPL",
    name: "Apple Inc.",
    assetClass: "equity",
    exchange: "NASDAQ",
    quoteCurrency: "USD",
    hasFundamentals: true,
    hasCorporateValuation: true,
  }},
  "equity:us:msft": {{
    assetId: "equity:us:msft",
    symbol: "MSFT",
    name: "Microsoft Corp.",
    assetClass: "equity",
    exchange: "NASDAQ",
    quoteCurrency: "USD",
    hasFundamentals: true,
    hasCorporateValuation: true,
  }},
}};
function makeInboxState() {{
  return {{
    snapshot: null,
    lastSuccessAt: 0,
    lastAttemptAt: 0,
    needsRevalidation: false,
    revalidating: false,
    revalidationError: null,
    inFlightPromise: null,
    contextKey: "",
  }};
}}
const mesaQuickInboxesState = {{
  analytical: makeInboxState(),
  institutional: makeInboxState(),
  activity: makeInboxState(),
}};
const investorOverviewState = {{
  contextKey: null,
  knownAt: null,
  workspaceId: null,
  generation: null,
  catalogVersion: null,
  catalogSha256: null,
  windows: null,
  approxBytes: 0,
  needsRevalidation: false,
  assetFetchedAt: new Map(),
  assetByteSizes: new Map(),
  byAssetId: new Map(),
}};

function makeElement(tagName, className = "", text = "") {{
  return {{
    tagName: String(tagName || "div").toUpperCase(),
    className: className || "",
    textContent: text === undefined || text === null ? "" : String(text),
    value: "",
    disabled: false,
    hidden: false,
    id: "",
    title: "",
    colSpan: 1,
    dataset: {{}},
    attributes: {{}},
    children: [],
    listeners: {{}},
    classList: {{
      _set: new Set(String(className || "").split(/\\s+/).filter(Boolean)),
      add(...tokens) {{ tokens.forEach((t) => this._set.add(t)); }},
      remove(...tokens) {{ tokens.forEach((t) => this._set.delete(t)); }},
      toggle(token, force) {{
        if (force === undefined) {{
          if (this._set.has(token)) {{ this._set.delete(token); return false; }}
          this._set.add(token); return true;
        }}
        if (force) this._set.add(token); else this._set.delete(token);
        return Boolean(force);
      }},
      contains(token) {{ return this._set.has(token); }},
    }},
    setAttribute(k, v) {{ this.attributes[k] = String(v); }},
    getAttribute(k) {{ return this.attributes[k] ?? null; }},
    removeAttribute(k) {{ delete this.attributes[k]; }},
    append(...nodes) {{
      for (const n of nodes) {{
        if (n !== null && n !== undefined) {{
          this.children.push(typeof n === "string" ? makeElement("#text", "", n) : n);
        }}
      }}
    }},
    appendChild(n) {{ this.append(n); return n; }},
    replaceChildren(...nodes) {{ this.children = []; this.append(...nodes); }},
    addEventListener(type, fn) {{ this.listeners[type] = fn; }},
    querySelector() {{ return null; }},
    querySelectorAll() {{ return []; }},
  }};
}}
function createElement(tag, className, text) {{ return makeElement(tag, className, text); }}

const elements = new Map();
for (const id of [
  "report-known-at",
  "mesa-investor-table-body",
  "mesa-investor-status",
  "mesa-investor-window",
  "mesa-investor-banner",
  "mesa-investor-banner-text",
  "mesa-investor-banner-detail",
  "mesa-banner-retry-btn",
  "mesa-banner-system-btn",
  "mesa-revalidation-status",
  "mesa-search",
  "mesa-filter-class",
  "mesa-filter-watchlist",
  "mesa-filter-verdict",
  "mesa-show-more-btn",
  "mesa-load-all-btn",
  "mesa-cancel-load-btn",
  "mesa-retry-load-btn",
  "mesa-revalidate-btn",
]) {{
  const el = makeElement("div");
  el.id = id;
  elements.set(id, el);
}}
elements.get("report-known-at").value = "2026-10-22T00:00:00Z";
elements.get("mesa-filter-class").value = "all";
elements.get("mesa-filter-watchlist").value = "all";
elements.get("mesa-filter-verdict").value = "all";

function byId(id) {{ return elements.get(id) || null; }}
const document = {{
  createElement(tag) {{ return makeElement(tag); }},
  createDocumentFragment() {{ return makeElement("#fragment"); }},
  querySelectorAll() {{ return []; }},
}};
function createAbortError(msg = "Aborted") {{
  const err = new Error(msg);
  err.name = "AbortError";
  return err;
}}
function formatDecimalPrice(raw, currency = "USD") {{ return `${{currency}} ${{raw}}`; }}
function formatDecimalPercentFromRatio(raw) {{ return `${{raw}}`; }}
function formatDecimalScore(raw) {{ return `${{raw}}`; }}
function formatDecimalMultiple(raw) {{ return `${{raw}}x`; }}
function formatInteger(raw) {{ return String(raw); }}
function formatKnownAtLabel(raw) {{ return String(raw); }}
function translateAssetClass(cls) {{ return String(cls); }}
function translateOverviewAbsenceReason(r) {{ return String(r); }}
function formatOverviewAbsenceDetail(r) {{
  return {{ label: String(r), technicalCode: String(r) }};
}}
function createContextualNavigationButton(label) {{ return makeElement("button", "", label); }}

{core_slice}
{mesa_slice}

let apiCalls = 0;
let failNextApi = false;
let priceBase = 150;
async function api(path) {{
  apiCalls += 1;
  if (failNextApi) {{
    throw new Error("Fallo simulado de red");
  }}
  const url = new URL("http://127.0.0.1:8765" + path);
  const requestedIds = url.searchParams.getAll("asset_id");
  const knownAt = url.searchParams.get("known_at");
  const avail = (val, refAt = undefined) => ({{
    state: "available",
    value: String(val),
    reference_at: refAt,
    reason_codes: [],
  }});
  return {{
    schema_version: "investor-overview-v1",
    known_at: knownAt,
    catalog_version: "cat-v1",
    catalog_sha256: "sha-cat-1",
    windows: {{ short_days: 30, medium_days: 90, long_days: 252 }},
    assets: requestedIds.map((id, idx) => ({{
      asset_id: id,
      symbol: id.endsWith("aapl") ? "AAPL" : "MSFT",
      name: id.endsWith("aapl") ? "Apple Inc." : "Microsoft Corp.",
      asset_class: "equity",
      exchange: "NASDAQ",
      market: {{
        currency: "USD",
        close: avail(priceBase + idx * 50, "2026-10-21T20:00:00Z"),
        change_1d: avail("0.012"),
        return_short: avail("0.05"),
        return_medium: avail("0.10"),
        return_long: avail("0.20"),
      }},
      fundamental: {{
        verdict: "favorable",
        score: avail("82.5"),
        revenue_growth: avail("0.08"),
        net_margin: avail("0.24"),
      }},
      valuation: {{
        annual_period_end: "2026-09-30",
        price_to_earnings: avail("28.4"),
        ev_to_ebitda: avail("21.0"),
        fcf_yield: avail("0.035"),
      }},
    }})),
  }};
}}

async function run() {{
  // 1. Initial load at t = 1_000_000
  await loadMesaInvestorOverview();
  if (apiCalls !== 1) throw new Error("Expected 1 API call on initial load, got " + apiCalls);

  // Expand AAPL detail row and capture DOM row references
  mesaInvestorUiState.expandedAssetIds.add("equity:us:aapl");
  renderMesaInvestorTable();
  const tbody = byId("mesa-investor-table-body");
  const assetRowsBefore = tbody.children.filter((tr) => tr.dataset?.assetId);
  if (assetRowsBefore.length !== 2) {{
    throw new Error("Expected 2 asset rows, got " + assetRowsBefore.length);
  }}

  // 2. Within 60s window (t + 30s): reopening Mesa makes 0 API calls and preserves DOM rows
  currentClockMs += 30_000;
  await loadMesaInvestorOverview();
  if (apiCalls !== 1) {{
    throw new Error("Expected 0 new API calls within 60s window, got " + apiCalls);
  }}
  const assetRowsWithinWindow = tbody.children.filter((tr) => tr.dataset?.assetId);
  if (
    assetRowsWithinWindow[0] !== assetRowsBefore[0] ||
    assetRowsWithinWindow[1] !== assetRowsBefore[1]
  ) {{
    throw new Error("DOM rows were recreated within 60s window");
  }}

  // 3. After 60s window (t + 65s): reopening Mesa revalidates in place, preserves DOM rows
  currentClockMs += 35_000;
  priceBase = 200;
  await loadMesaInvestorOverview();
  if (apiCalls !== 2) {{
    throw new Error("Expected 1 revalidation API call after 60s, got " + apiCalls);
  }}
  const assetRowsAfterReval = tbody.children.filter((tr) => tr.dataset?.assetId);
  if (
    assetRowsAfterReval[0] !== assetRowsBefore[0] ||
    assetRowsAfterReval[1] !== assetRowsBefore[1]
  ) {{
    throw new Error("In-place revalidation replaced DOM row nodes instead of updating in place");
  }}
  if (!mesaInvestorUiState.expandedAssetIds.has("equity:us:aapl")) {{
    throw new Error("In-place revalidation lost expandedAssetIds");
  }}

  // 4. Simulated failure during in-place revalidation preserves existing DOM rows + error
  currentClockMs += 65_000;
  failNextApi = true;
  await loadMesaInvestorOverview();
  const assetRowsAfterFailedReval = tbody.children.filter((tr) => tr.dataset?.assetId);
  if (
    assetRowsAfterFailedReval[0] !== assetRowsBefore[0] ||
    !mesaInvestorUiState.revalidationError
  ) {{
    throw new Error("Failed in-place revalidation did not preserve snapshot + compact error");
  }}

  console.log(JSON.stringify({{ passed: true, apiCalls }}));
}}
run().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    result = subprocess.run(
        [node, "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["apiCalls"] == 3


def test_overview_single_flight_and_deferred_board_superseded_guard_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for JavaScript shell navigation verification")

    shell_funcs = "\n\n".join(
        _function_source(APP_SHELL, name)
        for name in (
            "refreshOverview",
            "loadDeferredBoardData",
        )
    )

    script = f"""
let overviewTimer = null;
let overviewRequestActive = false;
let overviewInFlightPromise = null;
let overviewFailureCount = 0;
let boardDataReady = true;
const loadedBoardIds = new Set();
const boardActivationSequences = new Map();
let activoBoardRequestSequence = 0;
let selectedMarketAsset = "equity:us:aapl";
const document = {{ hidden: false }};

function scheduleOverviewRefresh() {{}}
function setButtonBusy() {{}}
function applyOverview() {{}}
function updateReadRuntimeContext() {{}}
function boardIdFromLocationHash() {{ return "activo"; }}
function setMessage() {{}}
function badge() {{}}
function byId(id) {{
  if (id === "report-known-at") return {{ value: "2026-10-22T00:00:00Z" }};
  return null;
}}

let overviewApiCalls = 0;
async function api(path) {{
  if (path === "/api/v1/overview") {{
    overviewApiCalls += 1;
    await new Promise((r) => setTimeout(r, 15));
    return {{ schema_version: "operational-overview-snapshot-v1" }};
  }}
  return {{}};
}}

let rejectFirstTecnico = null;
let tecnicoCallCount = 0;
const BOARD_DEFERRED_LOADS = {{
  tecnico: [
    () => {{
      tecnicoCallCount += 1;
      if (tecnicoCallCount === 1) {{
        return new Promise((_, reject) => {{
          rejectFirstTecnico = reject;
        }});
      }}
      return Promise.resolve("ok");
    }},
  ],
}};

{shell_funcs}

async function run() {{
  // 1. Concurrent refreshOverview calls share a single in-flight promise
  const [ov1, ov2] = await Promise.all([
    refreshOverview({{ manual: false }}),
    refreshOverview({{ manual: true }}),
  ]);
  if (overviewApiCalls !== 1 || !ov1 || ov1 !== ov2) {{
    throw new Error("refreshOverview did not share single in-flight promise");
  }}

  // 2. Superseded deferred board load does not delete loadedBoardIds of a newer load
  loadDeferredBoardData("tecnico");
  await Promise.resolve();
  if (!loadedBoardIds.has("tecnico")) {{
    throw new Error("tecnico should be marked loaded in-flight");
  }}
  // Simulate invalidation + second activation before first finishes
  loadedBoardIds.delete("tecnico");
  loadDeferredBoardData("tecnico");
  await Promise.resolve();
  // Now reject the first superseded load
  rejectFirstTecnico(new Error("superseded failure"));
  await new Promise((r) => setTimeout(r, 20));
  if (!loadedBoardIds.has("tecnico")) {{
    throw new Error("Superseded failure deleted newer loadedBoardIds marker");
  }}

  console.log(JSON.stringify({{ passed: true, overviewApiCalls }}));
}}
run().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    result = subprocess.run(
        [node, "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["overviewApiCalls"] == 1
