"""Integrated Node behavioral tests for INVESTOR-UI-CONSISTENCY (A1-A5, I1, N1)."""

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
APP_SHELL = _STATIC.joinpath("app-shell.js").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        [NODE, "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(f"Node exited {completed.returncode}:\n{completed.stderr}")
    return json.loads(completed.stdout.strip())


_DOM_PREAMBLE = r"""
const vm = require("node:vm");

function createFakeDom() {
  const elements = new Map();
  class FakeClassList {
    constructor(el) { this.el = el; this.set = new Set(); }
    add(...cls) { cls.forEach((c) => c && this.set.add(c)); }
    remove(...cls) { cls.forEach((c) => this.set.delete(c)); }
    toggle(c, force) {
      if (force === undefined) {
        if (this.set.has(c)) this.set.delete(c);
        else this.set.add(c);
      } else if (force) this.set.add(c);
      else this.set.delete(c);
    }
    contains(c) { return this.set.has(c); }
  }
  class FakeNode {
    constructor(tag = "div", id = "") {
      this.tagName = String(tag).toUpperCase();
      this.id = id;
      this.className = "";
      this.value = "";
      this.type = "";
      this.textContent = "";
      this.disabled = false;
      this.hidden = false;
      this.colSpan = 1;
      this.open = false;
      this.dataset = {};
      this.style = {};
      this.attrs = {};
      this.children = [];
      this.parentElement = null;
      this.listeners = new Map();
      this.classList = new FakeClassList(this);
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    removeAttribute(k) { delete this.attrs[k]; }
    append(...nodes) {
      for (const n of nodes) {
        if (n === null || n === undefined) continue;
        const child = typeof n === "string"
          ? { textContent: n, children: [], parentElement: this }
          : n;
        if (child && typeof child === "object") child.parentElement = this;
        this.children.push(child);
      }
    }
    appendChild(n) { this.append(n); return n; }
    prepend(...nodes) { this.append(...nodes); }
    replaceChildren(...nodes) {
      this.children = [];
      this.append(...nodes);
    }
    insertAdjacentElement(_pos, node) {
      if (node.id && !elements.has(node.id)) elements.set(node.id, node);
      if (this.parentElement) this.parentElement.append(node);
    }
    addEventListener(type, fn) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(fn);
    }
    dispatchEvent(evt) {
      const e = { currentTarget: this, target: this, preventDefault() {}, ...evt };
      for (const fn of this.listeners.get(e.type) || []) fn(e);
      return true;
    }
    click() {
      this.dispatchEvent({ type: "click" });
    }
    querySelectorAll() { return []; }
    querySelector(sel) {
      if (sel.includes(".mesa-investor-actions")) return getEl("mesa-investor-actions");
      return null;
    }
  }
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeNode("div", id));
    return elements.get(id);
  };
  const nodeText = (node) => {
    if (!node) return "";
    if (typeof node === "string") return node;
    if (node.children && node.children.length) {
      return (node.textContent || "") + node.children.map(nodeText).join(" | ");
    }
    return node.textContent || "";
  };
  return { elements, FakeNode, getEl, nodeText };
}
"""


def test_a1_catalog_to_mesa_identity_preferences_reasons_and_navigation() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const fetchUrls = [];
const navLog = [];
const abortedCurrentBeforeResolve = [];
const coreSrc = {json.dumps(APP_CORE)};
const mesaSrc = {json.dumps(APP_MESA)};
const shellSrc = {json.dumps(APP_SHELL)};

const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    getElementById: dom.getEl,
    querySelectorAll: () => [],
    querySelector: (sel) => (
      sel.includes(".mesa-investor-actions") ? dom.getEl("mesa-investor-actions") : null
    ),
    createElement: (tag) => new dom.FakeNode(tag),
  }},
  fetch: async (url) => {{
    fetchUrls.push(String(url));
    return {{
      ok: true,
      status: 200,
      text: async () => JSON.stringify({{
        schema_version: "investor-overview-v1",
        known_at: "2026-10-20T00:00:00Z",
        catalog_version: "v1",
        catalog_sha256: "sha-1",
        windows: {{
          market_start: "2026-10-01",
          market_end: "2026-10-20",
          fundamental_start: "2024-01-01",
          fundamental_end: "2026-10-20",
        }},
        assets: [
          {{
            asset_id: "crypto:coinbase:ada-usd",
            symbol: "ADA-USD",
            market: {{
              currency: "USD",
              close: {{ state: "missing", value: null, reason_codes: ["no_bar_in_window"] }},
              change_1d: {{ state: "missing", value: null, reason_codes: ["no_bar_in_window"] }},
            }},
            fundamental: {{
              score: {{ state: "not_applicable", value: null, reason_codes: [] }},
            }},
            valuation: {{
              price_to_earnings: {{ state: "not_applicable", value: null, reason_codes: [] }},
            }},
          }},
          {{
            asset_id: "equity:us:googl",
            symbol: "GOOGL",
            market: {{
              currency: "USD",
              close: {{
                state: "available", value: "165.40", reference_at: "2026-10-19T00:00:00Z",
              }},
              change_1d: {{ state: "available", value: "0.0125" }},
            }},
            fundamental: {{
              verdict: "favorable",
              score: {{ state: "available", value: "82" }},
              net_margin: {{ state: "available", value: "0.2400" }},
              liabilities_to_assets: {{ state: "available", value: "0.35" }},
            }},
            valuation: {{
              annual_period_end: "2025-12-31",
              price_to_earnings: {{
                state: "missing",
                value: null,
                reason_codes: ["no_annual_diagnostic_at_known_at"],
              }},
            }},
          }},
          {{
            asset_id: "etf:us:spy",
            symbol: "SPY",
            market: {{
              currency: "USD",
              close: {{
                state: "available", value: "580.10", reference_at: "2026-10-19T00:00:00Z",
              }},
              change_1d: {{ state: "available", value: "-0.0050" }},
            }},
            fundamental: {{
              score: {{ state: "not_applicable", value: null, reason_codes: [] }},
            }},
            valuation: {{
              price_to_earnings: {{ state: "not_applicable", value: null, reason_codes: [] }},
            }},
          }},
        ],
      }}),
    }};
  }},
}});

vm.runInContext(coreSrc.slice(0, coreSrc.indexOf("function setButtonBusy")), ctx);
vm.runInContext(mesaSrc.slice(mesaSrc.indexOf("const MESA_INVESTOR_BATCH_SIZE")), ctx);

// Wire contextual navigation + destination validation from app-shell.js
const navStart = shellSrc.indexOf("const BOARD_REGISTRY = Object.freeze([");
const navEnd = shellSrc.indexOf("// UI-5: the only board-to-request graph.");
vm.runInContext(shellSrc.slice(navStart, navEnd), ctx);

ctx.fetchUrls = fetchUrls;
ctx.navLog = navLog;
ctx.abortedCurrentBeforeResolve = abortedCurrentBeforeResolve;
ctx.nodeText = dom.nodeText;
dom.getEl("report-known-at").value = "2026-10-20T00:00:00Z";
ctx.byId = dom.getEl;
ctx.createElement = (tag, className = "", text = "") => {{
  const el = new dom.FakeNode(tag);
  el.className = className;
  el.textContent = text;
  return el;
}};
ctx.setMessage = (msg, err) => navLog.push({{ msg, err }});
ctx.activateBoard = (b) => navLog.push({{ activated: b }});
ctx.selectAssetSubtab = (s) => navLog.push({{ subtab: s }});
ctx.selectMarketAssetForNavigation = async (id) => {{
  navLog.push({{ selected: id }});
  return true;
}};
ctx.clearActiveAssetContextSync = () => abortedCurrentBeforeResolve.push("CLEARED");

vm.runInContext(`
const descriptors = [
  {{
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
  }},
  {{
    asset_id: "etf:us:spy",
    symbol: "SPY",
    name: "SPDR S&P 500 ETF",
    asset_class: "etf",
    exchange: "ARCA",
    quote_currency: "USD",
    provider: "alpaca",
    provider_identifier: "SPY",
    source_id: "alpaca-market-data:iex:spy:daily-bars:adjustment-all",
    chart_schema_version: "v1",
    volume_unit: "shares",
    default_market_start: "2024-01-01",
    analysis: {{ family: "etf", market_mode: "daily_etf", fundamental_mode: "none" }},
    has_fundamentals: false,
    has_corporate_valuation: false,
    supports_crypto_derivatives: false,
    fundamental_frequencies: [],
  }},
  {{
    asset_id: "crypto:coinbase:ada-usd",
    symbol: "ADA-USD",
    name: "Cardano",
    asset_class: "crypto",
    exchange: "COINBASE",
    quote_currency: "USD",
    provider: "coinbase",
    provider_identifier: "ADA-USD",
    source_id: "coinbase:ada-usd:daily",
    chart_schema_version: "v1",
    volume_unit: "units",
    default_market_start: "2024-01-01",
    analysis: {{ family: "cryptoasset", market_mode: "daily_crypto", fundamental_mode: "none" }},
    has_fundamentals: false,
    has_corporate_valuation: false,
    supports_crypto_derivatives: false,
    fundamental_frequencies: [],
  }},
];

marketAssets = Object.freeze({{
  "equity:us:googl": marketAssetFromDescriptor(descriptors[0]),
  "etf:us:spy": marketAssetFromDescriptor(descriptors[1]),
  "crypto:coinbase:ada-usd": marketAssetFromDescriptor(descriptors[2]),
}});
assetPreferencesSnapshot = {{
  assets: [
    {{ asset_id: "equity:us:googl", watchlist: true, favorite: true }},
    {{ asset_id: "etf:us:spy", watchlist: true, favorite: false }},
    {{ asset_id: "crypto:coinbase:ada-usd", watchlist: false, favorite: false }},
  ],
}};

(async () => {{
  await loadMesaInvestorOverview();
  const initialFetches = fetchUrls.length;
  const rowsAfterLoad = byId("mesa-investor-table-body").children.map((r) => ({{
    id: r.dataset.assetId,
    text: nodeText(r),
  }}));

  mesaInvestorUiState.watchlist = "favorite";
  renderMesaInvestorTable();
  const favoriteIds = byId("mesa-investor-table-body").children.map((r) => r.dataset.assetId);

  mesaInvestorUiState.watchlist = "all";
  mesaInvestorUiState.search = "cardano";
  renderMesaInvestorTable();
  const searchIds = byId("mesa-investor-table-body").children.map((r) => r.dataset.assetId);
  const fetchesAfterLocalFilters = fetchUrls.length;

  navigateToContext({{ board: "activo", assetId: "missing:asset", subtab: "mercado" }});
  const clearedOnInvalid = [...abortedCurrentBeforeResolve];

  mesaInvestorUiState.search = "";
  mesaInvestorUiState.expandedAssetIds.add("equity:us:googl");
  mesaInvestorUiState.expandedAssetIds.add("crypto:coinbase:ada-usd");
  renderMesaInvestorTable();
  const detailText = nodeText(byId("mesa-investor-table-body"));

  console.log(JSON.stringify({{
    fetchUrls,
    initialFetches,
    fetchesAfterLocalFilters,
    rowsAfterLoad,
    favoriteIds,
    searchIds,
    clearedOnInvalid,
    detailText,
    navLog,
    windowText: byId("mesa-investor-window").textContent,
    googlExchange: marketAssets["equity:us:googl"].exchange,
  }}));
}})();
`, ctx);
"""
    result = _run_node(script)
    assert result["initialFetches"] == 1
    assert result["fetchesAfterLocalFilters"] == 1
    assert "undefined" not in result["fetchUrls"][0]
    assert "asset_id=equity%3Aus%3Agoogl" in result["fetchUrls"][0]
    assert result["googlExchange"] == "NASDAQ"
    row_by_id = {row["id"]: row["text"] for row in result["rowsAfterLoad"]}
    assert "GOOGL" in row_by_id["equity:us:googl"]
    assert "NASDAQ" not in row_by_id["equity:us:googl"]
    assert "No aplica" not in row_by_id["etf:us:spy"]
    assert "No aplica" not in row_by_id["crypto:coinbase:ada-usd"]
    assert "—" in row_by_id["equity:us:googl"]
    assert "—" in row_by_id["etf:us:spy"]
    assert "—" in row_by_id["crypto:coinbase:ada-usd"]
    assert "NASDAQ" in result["detailText"]
    assert "Sin diagnóstico anual elegible al corte" in result["detailText"]
    assert "COINBASE" in result["detailText"]
    assert "Sin barras elegibles para este corte/ventana" in result["detailText"]
    assert result["favoriteIds"] == ["equity:us:googl"]
    assert result["searchIds"] == ["crypto:coinbase:ada-usd"]
    assert result["clearedOnInvalid"] == []
    assert any(item.get("err") is True for item in result["navLog"])
    assert "Ver fuentes y método" in result["detailText"]
    assert "Corte efectivo:" in result["windowText"]


def test_a2_a3_n1_coalescing_abort_concurrency_cap_and_error_recovery() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const fetchLog = [];
const coreSrc = {json.dumps(APP_CORE)};
const mesaSrc = {json.dumps(APP_MESA)};
const shellSrc = {json.dumps(APP_SHELL)};

let inFlightActivo = 0;
let peakActivo = 0;

const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    getElementById: dom.getEl,
    querySelectorAll: () => [],
    querySelector: (sel) => (
      sel.includes(".mesa-investor-actions") ? dom.getEl("mesa-investor-actions") : null
    ),
    createElement: (tag) => new dom.FakeNode(tag),
  }},
}});

const sharedResolvers = [];
ctx.fetch = (url, opts) => {{
  const idx = fetchLog.length;
  const entry = {{ url: String(url), aborted: false }};
  fetchLog.push(entry);
  if (opts?.signal) {{
    opts.signal.addEventListener("abort", () => {{
      entry.aborted = true;
    }});
  }}
  return new Promise((resolve, reject) => {{
    if (opts?.signal) {{
      opts.signal.addEventListener("abort", () => {{
        const err = new Error("Aborted");
        err.name = "AbortError";
        reject(err);
      }});
    }}
    sharedResolvers[idx] = {{ resolve, reject }};
  }});
}};

vm.runInContext(coreSrc.slice(0, coreSrc.indexOf("function setButtonBusy")), ctx);
vm.runInContext(mesaSrc.slice(mesaSrc.indexOf("const MESA_INVESTOR_BATCH_SIZE")), ctx);
const subtabStart = shellSrc.indexOf("async function loadActivoCurrentSubtab");
const subtabEnd = shellSrc.indexOf("function isCurrentActivoBoardRequest");
vm.runInContext(shellSrc.slice(subtabStart, subtabEnd + 260), ctx);

ctx.fetchLog = fetchLog;
ctx.sharedResolvers = sharedResolvers;
ctx.nodeText = dom.nodeText;
ctx.setTimeout = setTimeout;
dom.getEl("report-known-at").value = "2026-10-20T00:00:00Z";
ctx.byId = dom.getEl;
ctx.createElement = (tag, className = "", text = "") => {{
  const el = new dom.FakeNode(tag);
  el.className = className;
  el.textContent = text;
  return el;
}};

vm.runInContext(`
let inFlightActivo = 0;
let peakActivo = 0;

(async () => {{
  const c1 = new AbortController();
  const c2 = new AbortController();
  const p1 = api("/api/v1/shared-test", {{ signal: c1.signal }});
  const p2 = api("/api/v1/shared-test", {{ signal: c2.signal }});
  c1.abort();
  let c1Aborted = false;
  try {{ await p1; }} catch (err) {{ c1Aborted = err.name === "AbortError"; }}
  const sharedAbortedWhileC2Alive = fetchLog[0].aborted;
  sharedResolvers[0].resolve({{
    ok: true,
    status: 200,
    text: async () => JSON.stringify({{ ok: true }}),
  }});
  const p2Result = await p2;

  const c3 = new AbortController();
  const p3 = api("/api/v1/shared-abort-all", {{ signal: c3.signal }});
  c3.abort();
  let c3Aborted = false;
  try {{ await p3; }} catch (err) {{ c3Aborted = err.name === "AbortError"; }}
  const underlyingAbortedOnLast = fetchLog[1].aborted;

  selectedMarketAsset = "equity:us:googl";
  activeAssetSubtabId = "fundamentales";
  selectedFundamentalFrequency = "annual";
  activoBoardRequestSequence = 1;
  marketAssets = {{
    "equity:us:googl": {{ hasFundamentals: true, supportsCryptoDerivatives: false }},
  }};
  marketAssetPresentation = () => marketAssets["equity:us:googl"];
  loadActivoCompactHeader = async () => runWithActivoConcurrencyLimit(async () => {{
    inFlightActivo += 1;
    peakActivo = Math.max(peakActivo, inFlightActivo);
    await new Promise((r) => setTimeout(r, 15));
    inFlightActivo -= 1;
  }});
  let failTrendOnce = true;
  queryFundamentalTrend = async () => runWithActivoConcurrencyLimit(async () => {{
    inFlightActivo += 1;
    peakActivo = Math.max(peakActivo, inFlightActivo);
    await new Promise((r) => setTimeout(r, 15));
    inFlightActivo -= 1;
    if (failTrendOnce) {{
      failTrendOnce = false;
      throw new Error("503 temporal");
    }}
  }});
  queryFundamentalResearch = async () => runWithActivoConcurrencyLimit(async () => {{
    inFlightActivo += 1;
    peakActivo = Math.max(peakActivo, inFlightActivo);
    await new Promise((r) => setTimeout(r, 15));
    inFlightActivo -= 1;
  }});

  await loadActivoCurrentSubtab();
  const keyAfterFailure = activeAssetSubtabKeys.has("fundamentales");
  await loadActivoCurrentSubtab();
  const keyAfterRetry = activeAssetSubtabKeys.get("fundamentales");

  createContextualNavigationButton = (label, _req, cls = "") => createElement("button", cls, label);
  fetch = async () => ({{
    ok: false,
    status: 503,
    text: async () => JSON.stringify({{
      error: {{ code: "unavailable", message: "Servicio temporalmente no disponible" }},
    }}),
  }});
  marketAssets = {{
    "equity:us:googl": marketAssetFromDescriptor({{
      asset_id: "equity:us:googl",
      symbol: "GOOGL",
      name: "Alphabet Inc.",
      asset_class: "equity",
      exchange: "NASDAQ",
      quote_currency: "USD",
      source_id: "alpaca",
      chart_schema_version: "v1",
      volume_unit: "shares",
      default_market_start: "2024-01-01",
      analysis: {{
        family: "listed_company", market_mode: "daily", fundamental_mode: "corporate",
      }},
      has_fundamentals: true,
      has_corporate_valuation: true,
      fundamental_frequencies: ["annual"],
    }}),
  }};
  let mesaErrorCaught = false;
  try {{
    await loadMesaInvestorVisibleBatch();
  }} catch {{
    mesaErrorCaught = true;
  }}
  const failedRowText = nodeText(byId("mesa-investor-table-body"));
  const bannerHidden = byId("mesa-investor-banner").classList.contains("hidden");
  const retryHidden = byId("mesa-banner-retry-btn").classList.contains("hidden");

  console.log(JSON.stringify({{
    c1Aborted,
    sharedAbortedWhileC2Alive,
    p2Result,
    c3Aborted,
    underlyingAbortedOnLast,
    peakActivo,
    keyAfterFailure,
    keyAfterRetry,
    mesaErrorCaught,
    failedRowText,
    bannerHidden,
    retryHidden,
  }}));
}})();
`, ctx);
"""
    result = _run_node(script)
    assert result["c1Aborted"] is True
    assert result["sharedAbortedWhileC2Alive"] is False
    assert result["p2Result"] == {"ok": True}
    assert result["c3Aborted"] is True
    assert result["underlyingAbortedOnLast"] is True
    assert result["peakActivo"] <= 2
    assert result["keyAfterFailure"] is False
    assert isinstance(result["keyAfterRetry"], str) and "equity:us:googl" in result["keyAfterRetry"]
    assert result["mesaErrorCaught"] is True
    assert "Error" in result["failedRowText"]
    assert "Pendiente" not in result["failedRowText"]
    assert result["bannerHidden"] is False
    assert result["retryHidden"] is False


def test_a3_cache_limits_100_rows_2mib_and_catalog_cut_invalidation() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const coreSrc = {json.dumps(APP_CORE)};
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    getElementById: dom.getEl,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => new dom.FakeNode(tag),
  }},
  fetch: async () => ({{ ok: true, status: 200, text: async () => "{{}}" }}),
}});
vm.runInContext(coreSrc.slice(0, coreSrc.indexOf("function setButtonBusy")), ctx);
dom.getEl("report-known-at").value = "2026-10-20T00:00:00Z";
ctx.byId = dom.getEl;

vm.runInContext(`
(async () => {{
  fetch = async (url) => {{
    const u = new URLSearchParams(String(url).split("?")[1]);
    const ids = u.getAll("asset_id");
    return {{
      ok: true,
      status: 200,
      text: async () => JSON.stringify({{
        schema_version: "investor-overview-v1",
        known_at: "2026-10-20T00:00:00Z",
        catalog_version: "v1",
        catalog_sha256: "sha-a",
        windows: {{}},
        assets: ids.map((id) => ({{ asset_id: id, pad: "x".repeat(50) }})),
      }}),
    }};
  }};
  const allIds = Array.from(
    {{ length: 110 }},
    (_, i) => "equity:us:a" + String(i).padStart(3, "0"),
  );
  await fetchInvestorOverviewBatch(allIds, "2026-10-20T00:00:00Z");
  const sizeAfter110 = investorOverviewState.byAssetId.size;

  fetch = async () => ({{
    ok: true,
    status: 200,
    text: async () => JSON.stringify({{
      schema_version: "investor-overview-v1",
      known_at: "2026-10-20T00:00:00Z",
      catalog_version: "v2",
      catalog_sha256: "sha-b",
      windows: {{}},
      assets: [{{ asset_id: "equity:us:new1" }}],
    }}),
  }});
  let catalogMismatchMsg = "";
  try {{
    await fetchInvestorOverviewBatch(["equity:us:new1"], "2026-10-20T00:00:00Z");
  }} catch (err) {{
    catalogMismatchMsg = err.message;
  }}

  resetInvestorOverviewCache();
  fetch = async () => ({{
    ok: true,
    status: 200,
    text: async () => JSON.stringify({{
      schema_version: "investor-overview-v1",
      known_at: "2026-10-20T00:00:00Z",
      catalog_version: "v2",
      catalog_sha256: "sha-b",
      windows: {{}},
      assets: [{{ asset_id: "equity:us:other" }}],
    }}),
  }});
  let mismatchMsg = "";
  try {{
    await fetchInvestorOverviewBatch(["equity:us:req"], "2026-10-20T00:00:00Z");
  }} catch (err) {{
    mismatchMsg = err.message;
  }}

  resetInvestorOverviewCache();
  fetch = async (url) => {{
    const u = new URLSearchParams(String(url).split("?")[1]);
    const id = u.getAll("asset_id")[0];
    return {{
      ok: true,
      status: 200,
      text: async () => JSON.stringify({{
        schema_version: "investor-overview-v1",
        known_at: "2026-10-20T00:00:00Z",
        catalog_version: "v2",
        catalog_sha256: "sha-b",
        windows: {{}},
        assets: [{{ asset_id: id, blob: "b".repeat(900 * 1024) }}],
      }}),
    }};
  }};
  for (const id of ["equity:us:b1", "equity:us:b2", "equity:us:b3"]) {{
    await fetchInvestorOverviewBatch([id], "2026-10-20T00:00:00Z");
  }}
  const sizeAfter3Large = investorOverviewState.byAssetId.size;
  const approxBytes = investorOverviewState.approxBytes;

  console.log(JSON.stringify({{
    sizeAfter110,
    catalogMismatchMsg,
    mismatchMsg,
    sizeAfter3Large,
    approxBytes,
  }}));
}})();
`, ctx);
"""
    result = _run_node(script)
    assert result["sizeAfter110"] == 100
    assert "catálogo cambió" in result["catalogMismatchMsg"]
    assert "inesperados" in result["mismatchMsg"]
    assert result["sizeAfter3Large"] <= 2
    assert result["approxBytes"] <= 2 * 1024 * 1024
