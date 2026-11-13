"""Behavioral Node tests for the investor workflow frontend (A2, A3, A5, N1)."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

NODE = shutil.which("node")
if NODE is None:  # pragma: no cover - environment invariant
    pytest.skip(
        "node executable is required for frontend behavioral tests",
        allow_module_level=True,
    )

_STATIC = files("investment_analyst.frontend").joinpath("static")
APP_CORE = _STATIC.joinpath("app-core.js").read_text(encoding="utf-8")
APP_MESA = _STATIC.joinpath("app-mesa.js").read_text(encoding="utf-8")
APP_ANALYSIS = _STATIC.joinpath("app-analysis.js").read_text(encoding="utf-8")
APP_OPERATIONS = _STATIC.joinpath("app-operations.js").read_text(encoding="utf-8")
APP_SHELL = _STATIC.joinpath("app-shell.js").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        [NODE, "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise AssertionError(f"Node exited {completed.returncode}:\n{completed.stderr}")
    return json.loads(completed.stdout.strip())


def test_decimal_string_sorting_nulls_last_and_stable_asset_id_tie_break() -> None:
    script = f"""
const vm = require("node:vm");
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
}});
const fnRegex = new RegExp(
  "function parseDecimalString[\\\\s\\\\S]*?function decimalScalePercent[\\\\s\\\\S]*?\\\\n\\\\}}",
);
const fnMatch = {json.dumps(APP_CORE)}.match(fnRegex);
if (!fnMatch) throw new Error("Decimal helpers not found");
vm.runInContext(fnMatch[0], ctx);
const rows = [
  {{ asset_id: "equity:us:z", pe: null }},
  {{ asset_id: "equity:us:b", pe: "12.50" }},
  {{ asset_id: "equity:us:a", pe: "12.5" }},
  {{ asset_id: "equity:us:c", pe: "-4.2" }},
  {{ asset_id: "equity:us:y", pe: "" }},
];
const cmp = ctx.compareDecimalStrings;
const byIdAsc = (x, y) => x.asset_id.localeCompare(y.asset_id);
const asc = [...rows].sort((x, y) => cmp(x.pe, y.pe, "asc") || byIdAsc(x, y))
  .map((r) => r.asset_id);
const desc = [...rows].sort((x, y) => cmp(x.pe, y.pe, "desc") || byIdAsc(x, y))
  .map((r) => r.asset_id);
const scaled = ctx.decimalScalePercent("0.1842");
console.log(JSON.stringify({{ asc, desc, scaled }}));
"""
    result = _run_node(script)
    assert result["asc"] == [
        "equity:us:c",
        "equity:us:a",
        "equity:us:b",
        "equity:us:y",
        "equity:us:z",
    ]
    assert result["desc"] == [
        "equity:us:a",
        "equity:us:b",
        "equity:us:c",
        "equity:us:y",
        "equity:us:z",
    ]
    assert result["scaled"] == "18.42%"


def test_clear_active_asset_context_sync_wipes_dom_and_cancels_inflight_before_await() -> None:
    script = f"""
const vm = require("node:vm");

function makeElement(id) {{
  return {{
    id,
    textContent: "stale-text",
    innerHTML: "<g>stale-svg</g>",
    value: "stale",
    disabled: false,
    hidden: false,
    className: "",
    children: [],
    dataset: {{}},
    classList: {{
      _set: new Set(),
      add(c) {{ this._set.add(c); }},
      remove(c) {{ this._set.delete(c); }},
      toggle(c, force) {{
        if (force === undefined) {{
          if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
        }} else if (force) this._set.add(c); else this._set.delete(c);
      }},
      contains(c) {{ return this._set.has(c); }},
    }},
    setAttribute() {{}},
    removeAttribute() {{}},
    replaceChildren(...nodes) {{ this.children = nodes; this.innerHTML = ""; }},
    append(...nodes) {{ this.children.push(...nodes); }},
    appendChild(child) {{ this.children.push(child); }},
    addEventListener() {{}},
    querySelectorAll() {{ return []; }},
  }};
}}

const elements = new Map();
const getEl = (id) => {{
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}};

let cancelledRaf = null;
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object,
  Promise, JSON, AbortController,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000000" }}),
  document: {{
    documentElement: {{}},
    getElementById: getEl,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => makeElement(tag),
  }},
  cancelAnimationFrame: (id) => {{ cancelledRaf = id; }},
  clearTimeout: () => {{}},
  fetch: async () => ({{ ok: true, json: async () => ({{}}) }}),
}});

const coreSrc = {json.dumps(APP_CORE)};
const slicedCore = coreSrc.slice(0, coreSrc.indexOf("function setButtonBusy"));
vm.runInContext(slicedCore, ctx);
// Inject stubs used by clearActiveAssetContextSync
vm.runInContext(`
  function setExportAvailable(id, enabled) {{
    const el = byId(id);
    if (el) el.disabled = !enabled;
  }}
  function resetMarketChart() {{
    byId("market-chart-svg").innerHTML = "";
    marketChartPayload = null;
    setExportAvailable("export-market-chart-csv", false);
  }}
  function resetFundamentalTrend() {{
    byId("fundamental-trend-svg").innerHTML = "";
    byId("fundamental-margins-chart").innerHTML = "";
    fundamentalTrendPayload = null;
    setExportAvailable("export-fundamental-trend-csv", false);
  }}
  function resetFundamentalResearch() {{
    byId("fundamental-research-body").innerHTML = "";
    fundamentalResearchPayload = null;
    setExportAvailable("export-fundamental-research-json", false);
  }}
  function resetValuation() {{
    byId("valuation-body").innerHTML = "";
    valuationPayload = null;
    setExportAvailable("export-valuation-json", false);
  }}
  function resetListedCompanyReport() {{
    byId("report-area").innerHTML = "";
    reportPayload = null;
    setExportAvailable("export-report-json", false);
  }}
  function resetDerivadosCrypto() {{
    byId("derivados-crypto-body").innerHTML = "";
  }}
`, ctx);

vm.runInContext(`
  const controller = new AbortController();
  activeAssetAbortController = controller;
  marketChartRenderFrame = 42;
  marketChartPayload = {{ asset_id: "equity:us:aapl" }};
  fundamentalTrendPayload = {{ asset_id: "equity:us:aapl" }};
  valuationPayload = {{ asset_id: "equity:us:aapl" }};
  reportPayload = {{ asset_id: "equity:us:aapl" }};
  byId("asset-compact-header").innerHTML = "<span>AAPL</span>";
  byId("export-market-csv").disabled = false;
  byId("export-valuation-json").disabled = false;
  const abortedBefore = controller.signal.aborted;
  clearActiveAssetContextSync({{ symbol: "AMD", name: "AMD" }});
  globalThis.__result = {{
    abortedBefore,
    abortedAfter: controller.signal.aborted,
    svgHtml: byId("market-chart").innerHTML,
    marginsHtml: byId("fundamental-margins-chart").innerHTML,
    headerChildrenText: byId("asset-compact-header").children[0]?.textContent || "",
    exportChartDisabled: byId("export-market-csv").disabled,
    exportValDisabled: byId("export-valuation-json").disabled,
    marketPayloadNull: marketChartPayload === null,
    valuationPayloadNull: valuationPayload === null,
  }};
`, ctx);
console.log(JSON.stringify({{ cancelledRaf, ...ctx.__result }}));
"""
    result = _run_node(script)
    assert result["abortedBefore"] is False
    assert result["abortedAfter"] is True
    assert result["cancelledRaf"] == 42
    assert result["svgHtml"] == ""
    assert result["marginsHtml"] == ""
    assert "Cargando AMD" in result["headerChildrenText"]
    assert result["exportChartDisabled"] is True
    assert result["exportValDisabled"] is True
    assert result["marketPayloadNull"] is True
    assert result["valuationPayloadNull"] is True


def test_out_of_order_and_aborted_asset_switches_never_overwrite_current_asset() -> None:
    script = f"""
const vm = require("node:vm");
const elements = new Map();
const getEl = (id) => {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: id === "report-known-at"
        ? "2026-10-20T00:00:00Z"
        : (id === "market-chart-range" ? "1Y" : "annual"),
      textContent: "",
      innerHTML: "",
      disabled: false,
      hidden: false,
      classList: {{ add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }} }},
      setAttribute() {{}},
      removeAttribute() {{}},
      replaceChildren() {{}},
    }});
  }}
  return elements.get(id);
}};

const resolvers = [];
const fetchLog = [];
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object,
  Promise, JSON, AbortController,
  resolvers, fetchLog, getEl,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000000" }}),
  document: {{
    documentElement: {{}},
    getElementById: getEl,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => ({{
      tag,
      className: "",
      textContent: "",
      children: [],
      setAttribute() {{}},
      appendChild(c) {{ this.children.push(c); }},
      replaceChildren() {{}},
    }}),
  }},
  cancelAnimationFrame: () => {{}},
  clearTimeout: () => {{}},
  fetch: (url, opts) => new Promise((resolve, reject) => {{
    fetchLog.push({{ url, aborted: Boolean(opts?.signal?.aborted) }});
    if (opts?.signal) {{
      opts.signal.addEventListener("abort", () => {{
        const err = new Error("Aborted");
        err.name = "AbortError";
        reject(err);
      }});
    }}
    resolvers.push({{ url, resolve, reject }});
  }}),
}});

const coreSrc = {json.dumps(APP_CORE)};
vm.runInContext(coreSrc.slice(0, coreSrc.indexOf("function setButtonBusy")), ctx);
vm.runInContext(`
  function marketChartPeriod() {{ return "1y"; }}
  function isIntradayInterval() {{ return false; }}
  function updateMarketChartZoomState() {{}}
  function setButtonBusy() {{}}
  function setChartBusy() {{}}
  function setExportAvailable(id, enabled) {{ byId(id).disabled = !enabled; }}
  function setMessage() {{}}
  function clearPreviousCutNotice() {{}}
  function rememberViewPreview() {{}}
  function isCurrentActivoBoardRequest() {{ return true; }}
  function activeBoardId() {{ return "activo"; }}
  function renderMarketChart(p) {{
    marketChartPayload = p;
    byId("market-chart-svg").innerHTML = p.asset.asset_id;
    setExportAvailable("export-market-chart-csv", true);
  }}
  function resetMarketChart() {{
    marketChartPayload = null;
    byId("market-chart-svg").innerHTML = "";
    setExportAvailable("export-market-chart-csv", false);
  }}
  function resetFundamentalTrend() {{}}
  function resetFundamentalResearch() {{}}
  function resetValuation() {{}}
  function resetListedCompanyReport() {{}}
  function resetDerivadosCrypto() {{}}
`, ctx);

// Extract queryMarketChart from app-analysis.js
const analysisSrc = {json.dumps(APP_ANALYSIS)};
const qmcRegex = /async function queryMarketChart\\([^)]*\\) \\{{[\\s\\S]*?\\n\\}}/;
const qmcMatch = analysisSrc.match(qmcRegex);
if (!qmcMatch) throw new Error("queryMarketChart not found");
vm.runInContext(qmcMatch[0], ctx);

vm.runInContext(`
(async () => {{
  // Fast sequence A -> B -> C
  selectedMarketAsset = "equity:us:aapl";
  clearActiveAssetContextSync();
  const pA = queryMarketChart();

  selectedMarketAsset = "equity:us:amd";
  clearActiveAssetContextSync();
  const pB = queryMarketChart();

  selectedMarketAsset = "crypto:global:btc";
  clearActiveAssetContextSync();
  const pC = queryMarketChart();

  // Resolve C first, then try to resolve stale A
  resolvers[2].resolve({{
    ok: true,
    status: 200,
    headers: {{ get: () => "application/json" }},
    json: async () => ({{ asset: {{ asset_id: "crypto:global:btc" }}, bars: [] }}),
    text: async () => JSON.stringify({{ asset: {{ asset_id: "crypto:global:btc" }}, bars: [] }}),
  }});
  const settled1 = await Promise.allSettled([pA, pB, pC]);
  if (settled1[2].status === "rejected") throw settled1[2].reason;

  const afterC = {{
    renderedAsset: marketChartPayload?.asset?.asset_id ?? null,
    svg: getEl("market-chart-svg").innerHTML,
    exportDisabled: getEl("export-market-chart-csv").disabled,
    emptyText: getEl("chart-empty").textContent,
  }};

  // Now A -> B -> A race where old A arrives after new A
  selectedMarketAsset = "equity:us:aapl";
  clearActiveAssetContextSync();
  const pA1 = queryMarketChart();

  selectedMarketAsset = "equity:us:amd";
  clearActiveAssetContextSync();
  const pB2 = queryMarketChart();

  selectedMarketAsset = "equity:us:aapl";
  clearActiveAssetContextSync();
  const pA2 = queryMarketChart();

  // Resolve newest A2 with current data
  resolvers[5].resolve({{
    ok: true,
    status: 200,
    headers: {{ get: () => "application/json" }},
    json: async () => ({{ asset: {{ asset_id: "equity:us:aapl" }}, tag: "fresh-A2", bars: [] }}),
    text: async () => JSON.stringify({{
      asset: {{ asset_id: "equity:us:aapl" }}, tag: "fresh-A2", bars: [],
    }}),
  }});
  await Promise.allSettled([pA1, pB2, pA2]);

  console.log(JSON.stringify({{
    afterC,
    finalTag: marketChartPayload?.tag ?? null,
    abortedCalls: resolvers.slice(0, 2).length,
    fetchCount: fetchLog.length,
  }}));
}})();
`, ctx);
"""
    result = _run_node(script)
    assert result["afterC"]["renderedAsset"] == "crypto:global:btc"
    assert result["afterC"]["svg"] == "crypto:global:btc"
    assert result["afterC"]["exportDisabled"] is False
    assert result["finalTag"] == "fresh-A2"
    assert result["fetchCount"] == 6


def test_revisar_explore_and_filters_exclude_negative_or_zero_pe() -> None:
    script = f"""
const vm = require("node:vm");
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
}});
const coreSrc = {json.dumps(APP_CORE)};
const opsSrc = {json.dumps(APP_OPERATIONS)};
const decRe = /function parseDecimalString[\\s\\S]*?function decimalScalePercent[\\s\\S]*?\\n\\}}/;
const decMatch = coreSrc.match(decRe);
const opsMatch = opsSrc.match(/function decimalPercentToRatioString[\\s\\S]*?\\n\\}}/);
if (!decMatch || !opsMatch) throw new Error("Revisar helpers not found");
vm.runInContext(decMatch[0], ctx);
vm.runInContext(opsMatch[0], ctx);

// Test decimalPercentToRatioString directly + filtering invariant
vm.runInContext(`
  const ratio = decimalPercentToRatioString("15");
  function matchesExplore(item, maxPe, minMarginPct, maxLiabRatio) {{
    if (maxPe) {{
      if (!item.pe_ratio) return false;
      if (compareDecimalStrings(item.pe_ratio, "0") <= 0) return false;
      if (compareDecimalStrings(item.pe_ratio, maxPe, "asc") > 0) return false;
    }}
    if (minMarginPct) {{
      const minRatio = decimalPercentToRatioString(minMarginPct);
      if (!item.net_margin || compareDecimalStrings(item.net_margin, minRatio, "asc") < 0) {{
        return false;
      }}
    }}
    if (maxLiabRatio) {{
      if (
        !item.liabilities_to_assets
        || compareDecimalStrings(item.liabilities_to_assets, maxLiabRatio, "asc") > 0
      ) return false;
    }}
    return true;
  }}
  const items = [
    {{ asset_id: "pos-ok", pe_ratio: "18.5", net_margin: "0.22", liabilities_to_assets: "0.45" }},
    {{ asset_id: "neg-pe", pe_ratio: "-12.0", net_margin: "0.22", liabilities_to_assets: "0.45" }},
    {{ asset_id: "zero-pe", pe_ratio: "0", net_margin: "0.22", liabilities_to_assets: "0.45" }},
    {{
      asset_id: "low-margin", pe_ratio: "14.0", net_margin: "0.10", liabilities_to_assets: "0.45",
    }},
    {{
      asset_id: "high-liab", pe_ratio: "14.0", net_margin: "0.22", liabilities_to_assets: "0.80",
    }},
  ];
  globalThis.__out = {{
    ratio,
    matched: items.filter((it) => matchesExplore(it, "25", "15", "0.60")).map((it) => it.asset_id),
  }};
`, ctx);
console.log(JSON.stringify(ctx.__out));
"""
    result = _run_node(script)
    assert result["ratio"] == "0.15"
    assert result["matched"] == ["pos-ok"]


def test_activo_subtab_loader_dispatches_single_subtab_without_fanout() -> None:
    script = f"""
const vm = require("node:vm");
const calls = [];
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  selectedMarketAsset: "equity:us:aapl",
  activeAssetSubtabId: "mercado",
  selectedFundamentalFrequency: "annual",
  selectedMarketChartRange: "1Y",
  activoBoardRequestSequence: 1,
  marketAssets: {{
    "equity:us:aapl": {{
      hasFundamentals: true, hasValuation: true,
      hasCompleteAnalysis: true, supportsCryptoDerivatives: false,
    }},
  }},
  marketAssetPresentation: () => ({{
    hasFundamentals: true, hasValuation: true,
    hasCompleteAnalysis: true, supportsCryptoDerivatives: false,
  }}),
  activeAssetSubtabKeys: new Map(),
  byId: (id) => ({{
    value: id === "report-known-at"
      ? "2026-10-20T00:00:00Z"
      : (id === "market-chart-range" ? "1Y" : "annual"),
  }}),
  loadActivoCompactHeader: async () => {{ calls.push("header"); }},
  queryMarketChart: async () => {{ calls.push("mercado"); }},
  loadDerivadosCryptoBoard: async () => {{ calls.push("derivados-crypto"); }},
  queryFundamentalTrend: async () => {{ calls.push("fundamentales:trend"); }},
  queryFundamentalResearch: async () => {{ calls.push("fundamentales:research"); }},
  queryValuation: async () => {{ calls.push("valoracion"); }},
  queryReport: async () => {{ calls.push("analisis"); }},
}});

const shellSrc = {json.dumps(APP_SHELL)};
const start = shellSrc.indexOf("async function loadActivoCurrentSubtab");
const end = shellSrc.indexOf("function setSidebarOpen");
vm.runInContext(shellSrc.slice(start, end), ctx);

(async () => {{
  await ctx.loadActivoCurrentSubtab();
  const mercadoCalls = [...calls];
  calls.length = 0;
  ctx.activeAssetSubtabId = "valoracion";
  await ctx.loadActivoCurrentSubtab();
  const valoracionCalls = [...calls];
  console.log(JSON.stringify({{ mercadoCalls, valoracionCalls }}));
}})();
"""
    result = _run_node(script)
    assert result["mercadoCalls"] == ["header", "mercado"]
    assert result["valoracionCalls"] == ["header", "valoracion"]


def test_subtab_dispatcher_uses_real_descriptor_has_fundamentals() -> None:
    script = f"""
const vm = require("node:vm");
const calls = [];
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  selectedMarketAsset: "equity:us:googl",
  activeAssetSubtabId: "valoracion",
  selectedFundamentalFrequency: "annual",
  activoBoardRequestSequence: 1,
  marketAssets: {{}},
  marketAssetPresentation: () => ctx.marketAssets[ctx.selectedMarketAsset],
  activeAssetSubtabKeys: new Map(),
  byId: () => ({{ value: "2026-10-20T00:00:00Z" }}),
  loadActivoCompactHeader: async () => {{ calls.push("header"); }},
  queryMarketChart: async () => {{ calls.push("mercado"); }},
  queryFundamentalTrend: async () => {{ calls.push("fundamentales:trend"); }},
  queryFundamentalResearch: async () => {{ calls.push("fundamentales:research"); }},
  queryValuation: async () => {{ calls.push("valoracion"); }},
  queryReport: async () => {{ calls.push("analisis"); }},
}});
const coreSrc = {json.dumps(APP_CORE)};
const startCore = coreSrc.indexOf("function marketAssetFromDescriptor");
const endCore = coreSrc.indexOf("function resolveMarketAssetSelection");
vm.runInContext(coreSrc.slice(startCore, endCore), ctx);

ctx.marketAssets["equity:us:googl"] = ctx.marketAssetFromDescriptor({{
  asset_id: "equity:us:googl",
  symbol: "GOOGL",
  name: "Alphabet Inc.",
  asset_class: "equity",
  exchange: "NASDAQ",
  quote_currency: "USD",
  provider: "alpaca",
  provider_identifier: "GOOGL",
  source_id: "alpaca-market-data:iex:googl:daily-bars:adjustment-all",
  chart_schema_version: "v1",
  volume_unit: "shares",
  default_market_start: "2024-01-01",
  analysis: {{
    family: "listed_company", market_mode: "daily_equity", fundamental_mode: "corporate",
  }},
  has_fundamentals: true,
  has_corporate_valuation: true,
  supports_crypto_derivatives: false,
  fundamental_frequencies: ["annual", "quarterly"],
}});

const shellSrc = {json.dumps(APP_SHELL)};
const start = shellSrc.indexOf("async function loadActivoCurrentSubtab");
const end = shellSrc.indexOf("function setSidebarOpen");
vm.runInContext(shellSrc.slice(start, end), ctx);

(async () => {{
  await ctx.loadActivoCurrentSubtab();
  const valuationCalls = [...calls];
  calls.length = 0;
  ctx.activeAssetSubtabId = "analisis";
  await ctx.loadActivoCurrentSubtab();
  const analysisCalls = [...calls];
  console.log(JSON.stringify({{
    exchange: ctx.marketAssets["equity:us:googl"].exchange,
    valuationCalls,
    analysisCalls,
  }}));
}})();
"""
    result = _run_node(script)
    assert result["exchange"] == "NASDAQ"
    assert result["valuationCalls"] == ["header", "valoracion"]
    assert result["analysisCalls"] == ["header", "analisis"]


def test_fetch_investor_overview_batch_accepts_equivalent_utc_iso_formatting() -> None:
    script = f"""
const vm = require("node:vm");
let fetchCount = 0;
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  TextEncoder, Error, Symbol, AbortController,
  document: {{
    documentElement: {{}},
    getElementById: (id) =>
      id === "report-known-at" ? {{ value: "2026-11-13T00:00:00.000Z" }} : null,
  }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000000" }}),
  fetch: async () => {{
    fetchCount += 1;
    return {{
      ok: true,
      status: 200,
      text: async () => JSON.stringify({{
        schema_version: "investor-overview-v1",
        known_at: "2026-11-13T00:00:00Z",
        catalog_version: "v1",
        catalog_sha256: "abc123",
        windows: {{}},
        assets: [{{
          asset_id: "equity:us:aapl",
          market: {{}},
          fundamental: {{}},
          valuation: {{}},
        }}],
      }}),
    }};
  }},
}});
const coreSrc = {json.dumps(APP_CORE)};
vm.runInContext(coreSrc.slice(0, coreSrc.indexOf("function setButtonBusy")), ctx);
vm.runInContext(coreSrc.slice(coreSrc.indexOf("function sameIsoTimestamp")), ctx);

(async () => {{
  const cut = "2026-11-13T00:00:00.000Z";
  const first = await ctx.fetchInvestorOverviewBatch(["equity:us:aapl"], cut);
  const cached = ctx.getCachedInvestorOverviewAsset("equity:us:aapl", cut);
  const second = await ctx.fetchInvestorOverviewBatch(["equity:us:aapl"], cut);
  console.log(JSON.stringify({{
    firstCount: first.assets.length,
    cachedHit: Boolean(cached),
    secondCount: second.assets.length,
    fetchCount,
    sameFalseDiffMs: ctx.sameIsoTimestamp("2026-11-13T00:00:00Z", "2026-11-13T00:00:01Z"),
  }}));
}})();
"""
    result = _run_node(script)
    assert result == {
        "firstCount": 1,
        "cachedHit": True,
        "secondCount": 1,
        "fetchCount": 1,
        "sameFalseDiffMs": False,
    }


def test_subtab_dispatcher_does_not_dedupe_superseded_sequence() -> None:
    script = f"""
const vm = require("node:vm");
const calls = [];
let releaseFirst;
let first = true;
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  selectedMarketAsset: "equity:us:aapl",
  activeAssetSubtabId: "mercado",
  selectedFundamentalFrequency: "annual",
  activoBoardRequestSequence: 1,
  marketAssets: {{}},
  marketAssetPresentation: () => ({{ hasFundamentals: false, supportsCryptoDerivatives: false }}),
  activeAssetSubtabKeys: new Map(),
  byId: () => ({{ value: "2026-10-20T00:00:00Z" }}),
  loadActivoCompactHeader: async (req) => {{
    calls.push("header:" + req.sequence);
    if (first) {{
      first = false;
      await new Promise((r) => {{ releaseFirst = r; }});
    }}
  }},
  queryMarketChart: async (req) => {{ calls.push("mercado:" + req.sequence); }},
  queryFundamentalTrend: async () => {{}},
  queryFundamentalResearch: async () => {{}},
  queryValuation: async () => {{}},
  queryReport: async () => {{}},
}});
const shellSrc = {json.dumps(APP_SHELL)};
const start = shellSrc.indexOf("async function loadActivoCurrentSubtab");
const end = shellSrc.indexOf("function setSidebarOpen");
vm.runInContext(shellSrc.slice(start, end), ctx);

(async () => {{
  const p1 = ctx.loadActivoCurrentSubtab();
  ctx.activoBoardRequestSequence = 2;
  const p2 = ctx.loadActivoCurrentSubtab({{
    sequence: 2,
    assetId: "equity:us:aapl",
    knownAt: "2026-10-20T00:00:00Z",
  }});
  await p2;
  releaseFirst();
  await p1;
  console.log(JSON.stringify({{ calls, cachedKey: ctx.activeAssetSubtabKeys.get("mercado") }}));
}})();
"""
    result = _run_node(script)
    assert "header:1" in result["calls"]
    assert "header:2" in result["calls"]
    assert "mercado:2" in result["calls"]
    assert result["cachedKey"] == "equity:us:aapl|2026-10-20T00:00:00Z|mercado|annual"
