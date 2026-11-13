from __future__ import annotations

import json
import os
import re
import subprocess
from importlib.resources import files
from pathlib import Path

from investment_analyst.analytics.valuation.models import ValuationReasonCode

STATIC_DIR = Path(str(files("investment_analyst"))) / "frontend" / "static"
ROOT_DIR = Path(str(files("investment_analyst"))).parent.parent
APP_CORE = (STATIC_DIR / "app-core.js").read_text(encoding="utf-8")
APP_ANALYSIS = (STATIC_DIR / "app-analysis.js").read_text(encoding="utf-8")
APP_TECHNICAL = (STATIC_DIR / "app-technical.js").read_text(encoding="utf-8")
APP_OPERATIONS = (STATIC_DIR / "app-operations.js").read_text(encoding="utf-8")
APP_MESA = (STATIC_DIR / "app-mesa.js").read_text(encoding="utf-8")
APP_CAZATIBURONES = (STATIC_DIR / "app-cazatiburones.js").read_text(encoding="utf-8")
APP_SHELL = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")

_DOM_PREAMBLE = """
const vm = require("vm");

function createFakeDom() {
  const elements = new Map();
  let activeElement = null;
  class FakeClassList {
    constructor(node) { this.node = node; this.items = new Set(); }
    add(...cls) { for (const c of cls) if (c) this.items.add(c); }
    remove(...cls) { for (const c of cls) this.items.delete(c); }
    toggle(c, force) {
      if (force === undefined) {
        if (this.items.has(c)) { this.items.delete(c); return false; }
        this.items.add(c); return true;
      }
      if (force) this.items.add(c); else this.items.delete(c);
      return Boolean(force);
    }
    contains(c) { return this.items.has(c); }
    toString() { return Array.from(this.items).join(" "); }
  }
  class FakeNode {
    constructor(tagName, id = "") {
      this.tagName = String(tagName || "div").toUpperCase();
      this.id = id;
      this.className = "";
      this.classList = new FakeClassList(this);
      this.dataset = {};
      this.attributes = new Map();
      this.children = [];
      this.listeners = new Map();
      this.value = "";
      this.type = "";
      this.title = "";
      this.colSpan = 1;
      this.disabled = false;
      this.hidden = false;
      this.open = false;
      this.parentElement = null;
      this._text = "";
    }
    set className(v) {
      this._className = String(v || "");
      if (this.classList) {
        this.classList.items = new Set(this._className.split(/\\s+/).filter(Boolean));
      }
    }
    get className() {
      return this.classList ? Array.from(this.classList.items).join(" ") : (this._className || "");
    }
    set textContent(v) {
      this._text = v === null || v === undefined ? "" : String(v);
      this.children = [];
    }
    get textContent() {
      if (this.children.length) return this.children.map((c) => nodeText(c)).join("");
      return this._text;
    }
    setAttribute(k, v) {
      this.attributes.set(k, String(v));
      if (k === "id") {
        this.id = String(v);
        elements.set(this.id, this);
      }
    }
    getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
    removeAttribute(k) { this.attributes.delete(k); }
    append(...nodes) {
      for (const n of nodes) {
        if (n === null || n === undefined) continue;
        const child = typeof n === "string"
          ? { textContent: n, children: [], parentElement: this, attributes: new Map() }
          : n;
        if (child && typeof child === "object") child.parentElement = this;
        this.children.push(child);
      }
    }
    appendChild(n) { this.append(n); return n; }
    prepend(...nodes) { this.append(...nodes); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    addEventListener(type, fn) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(fn);
    }
    dispatchEvent(evt) {
      const e = { currentTarget: this, target: this, preventDefault() {}, ...evt };
      for (const fn of this.listeners.get(e.type) || []) fn(e);
      return true;
    }
    click() { this.dispatchEvent({ type: "click" }); }
    focus() { activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatchEvent({ type: "close" }); }
    querySelector(sel) {
      const triggerMatch = /\\[data-detail-trigger-for="([^"]+)"\\]/.exec(sel);
      if (triggerMatch) {
        return findFirst(this, (n) => n.dataset?.detailTriggerFor === triggerMatch[1]);
      }
      if (sel.includes(".mesa-investor-actions")) return getEl("mesa-investor-actions");
      return null;
    }
    querySelectorAll() { return []; }
    contains(other) {
      let cur = other;
      while (cur) {
        if (cur === this) return true;
        cur = cur.parentElement;
      }
      return false;
    }
  }
  function findFirst(root, predicate) {
    for (const child of root.children || []) {
      if (predicate(child)) return child;
      const nested = findFirst(child, predicate);
      if (nested) return nested;
    }
    return null;
  }
  function nodeText(node) {
    if (!node) return "";
    if (typeof node === "string") return node;
    if (node.children && node.children.length) {
      return node.children.map((c) => nodeText(c)).filter(Boolean).join(" | ");
    }
    return node._text || node.textContent || "";
  }
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeNode("div", id));
    return elements.get(id);
  };
  const bodyNode = new FakeNode("body", "__body__");
  return {
    FakeNode,
    elements,
    getEl,
    bodyNode,
    nodeText,
    findFirst,
    getActiveElement: () => activeElement,
    setActiveElement: (el) => { activeElement = el; },
  };
}
"""


def _run_node(script: str) -> dict:
    clean_env = {k: v for k, v in os.environ.items() if "PROXY" not in k.upper()}
    proc = subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        env=clean_env,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed ({proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_decimal_formatting_neutral_tones_and_revisar_explore() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const ctx = vm.createContext({{
  dom, console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder, BigInt, Error, RegExp, Intl,
  window: {{ addEventListener() {{}}, location: {{ hash: "" }}, innerWidth: 1366 }},
  history: {{ replaceState() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    body: dom.bodyNode,
    getElementById: dom.getEl,
    get activeElement() {{ return dom.getActiveElement(); }},
    createElement: (tag) => new dom.FakeNode(tag),
    createTextNode: (t) => String(t),
    querySelectorAll: () => [],
    addEventListener() {{}},
  }},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
}});
vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_ANALYSIS)}, ctx);
vm.runInContext({json.dumps(APP_TECHNICAL)}, ctx);
vm.runInContext({json.dumps(APP_OPERATIONS)}, ctx);
vm.runInContext({json.dumps(APP_MESA)}, ctx);
vm.runInContext({json.dumps(APP_CAZATIBURONES)}, ctx);
vm.runInContext({json.dumps(APP_SHELL)}, ctx);

vm.runInContext(`
const prices = {{
  carry: formatDecimalPrice("199.995", "USD"),
  roll: formatDecimalPrice("999.999", "USD"),
  subDollar: formatDecimalPrice("0.2234", "USD"),
  subDollarZeroTrim: formatDecimalPrice("0.50000", "USD"),
  tinyNonZero: formatDecimalPrice("0.0000000012", "USD"),
  tinyNeg: formatDecimalPrice("-0.0000000012", "USD"),
  exactZero: formatDecimalPrice("0", "USD"),
}};
const percents = {{
  tinyNeg: formatDecimalPercentFromRatio("-0.00004", {{ decimals: 2, signed: true }}),
  tinyPos: formatDecimalPercentFromRatio("0.00004", {{ decimals: 2, signed: true }}),
  exactZero: formatDecimalPercentFromRatio("0", {{ decimals: 2, signed: true }}),
  carryPct: formatDecimalPercentFromRatio("0.99999", {{ decimals: 2, signed: true }}),
}};
const ratios = {{
  lev: formatDecimalRatio("1.235", 2),
  pe: formatDecimalMultiple("24.565", 2),
  score: formatDecimalScore("82.36", 1),
  billions: formatUsdBillions("123450000000"),
  signedPp: formatSignedNumber("0.145", {{ shiftLeft: -2, maximumFractionDigits: 1 }}),
  relSma: computeDecimalRelativeRatioString("100.000001", "100"),
}};
const drawdownEl = dom.getEl("snapshot-range-drawdown");
setNeutralSignedPercentage(drawdownEl, "-0.12345", "Máximo drawdown");

const triggerBtn = new dom.FakeNode("button", "trigger-exact");
triggerBtn.focus();
openExactDetailDialog({{
  title: "AAPL — Exactitud",
  subtitle: "2026-11-13T12:00:00Z",
  rows: [["Último cierre", "$ 199.995001"]],
  returnFocusTo: triggerBtn,
}});
const dialogOpen = dom.getEl("exact-detail-dialog").open;
closeExactDetailDialog();
const focusRestored = dom.getActiveElement() === triggerBtn;

// Test Revisar explore formatting & sameIsoTimestamp equivalence
dom.getEl("report-known-at").value = "2026-11-13T12:00:00Z";
marketAssets = {{
  "equity:us:aapl": {{
    assetId: "equity:us:aapl",
    symbol: "AAPL",
    name: "Apple Inc.",
    assetClass: "equity",
    exchange: "NASDAQ",
    hasCorporateValuation: true,
  }},
}};
investorOverviewState.knownAt = "2026-11-13T12:00:00+00:00";
investorOverviewState.contextKey = "ctx-1";
investorOverviewState.byAssetId.set("equity:us:aapl", {{
  asset_id: "equity:us:aapl",
  symbol: "AAPL",
  name: "Apple Inc.",
  valuation: {{ price_to_earnings: {{ state: "available", value: "24.56789" }} }},
  fundamental: {{
    net_margin: {{ state: "available", value: "0.241234" }},
    liabilities_to_assets: {{ state: "available", value: "0.6184" }},
  }},
}});
dom.getEl("review-explore-pe-max").value = "30";
renderRevisarExploreResults();
const exploreMetricsEl = dom.findFirst(
  dom.getEl("review-explore-results"),
  (n) => n.className.includes("review-explore-metrics"),
);
console.log(JSON.stringify({{
  prices,
  percents,
  ratios,
  drawdownClass: drawdownEl.className,
  drawdownText: drawdownEl.textContent,
  dialogOpen,
  dialogClosed: !dom.getEl("exact-detail-dialog").open,
  focusRestored,
  exploreText: exploreMetricsEl ? exploreMetricsEl.textContent : "",
  exploreTitle: exploreMetricsEl ? exploreMetricsEl.title : "",
}}));
`, ctx);
"""
    res = _run_node(script)
    assert res["prices"] == {
        "carry": "$ 200.00",
        "roll": "$ 1,000.00",
        "subDollar": "$ 0.2234",
        "subDollarZeroTrim": "$ 0.50",
        "tinyNonZero": "$ <0.00000001",
        "tinyNeg": "-$ <0.00000001",
        "exactZero": "$ 0.00",
    }
    assert res["percents"] == {
        "tinyNeg": "-<0.01%",
        "tinyPos": "+<0.01%",
        "exactZero": "0.00%",
        "carryPct": "+100.00%",
    }
    assert res["ratios"]["lev"] == "1.24"
    assert "×" not in res["ratios"]["lev"]
    assert res["ratios"]["pe"] == "24.57×"
    assert res["ratios"]["score"] == "82.4"
    assert res["ratios"]["billions"] == "$ 123.5 mil M"
    assert res["ratios"]["signedPp"] == "+14.5"
    assert res["ratios"]["relSma"] != "0" and float(res["ratios"]["relSma"]) > 0
    assert res["drawdownClass"] == "stat-move neutral"
    assert res["drawdownText"] == "-12.35%"
    assert res["dialogOpen"] is True
    assert res["dialogClosed"] is True
    assert res["focusRestored"] is True
    assert "P/E: 24.57×" in res["exploreText"]
    assert "Margen neto: +24.12%" in res["exploreText"]
    assert "Pasivos/Activos: 0.62" in res["exploreText"]
    assert "Pasivos/Activos: 0.62×" not in res["exploreText"]
    assert "0.241234" in res["exploreTitle"]


def test_same_iso_timestamp_translations_and_reason_completeness() -> None:
    overview_src = (
        ROOT_DIR / "src" / "investment_analyst" / "application" / "investor_overview_service.py"
    ).read_text(encoding="utf-8")
    overview_reasons = sorted(set(re.findall(r'_absent\(\s*"[^"]+"\s*,\s*"([^"]+)"', overview_src)))
    valuation_reasons = [member.value for member in ValuationReasonCode]
    all_expected_reasons = sorted(set(overview_reasons + valuation_reasons))

    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const ctx = vm.createContext({{
  dom, console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder,
  window: {{ addEventListener() {{}}, location: {{ hash: "", search: "" }}, innerWidth: 1366 }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    body: dom.bodyNode,
    getElementById: dom.getEl,
    createElement: (tag) => new dom.FakeNode(tag),
    querySelectorAll: () => [],
    addEventListener() {{}},
  }},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
}});
vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_ANALYSIS)}, ctx);
vm.runInContext({json.dumps(APP_TECHNICAL)}, ctx);
vm.runInContext({json.dumps(APP_OPERATIONS)}, ctx);
vm.runInContext({json.dumps(APP_MESA)}, ctx);
vm.runInContext({json.dumps(APP_CAZATIBURONES)}, ctx);
vm.runInContext({json.dumps(APP_SHELL)}, ctx);
const resultJson = vm.runInContext(`
const expected = {json.dumps(all_expected_reasons)};
const isoChecks = {{
  zVsMs: sameIsoTimestamp("2026-11-13T12:00:00Z", "2026-11-13T12:00:00.000Z"),
  zVsOffset: sameIsoTimestamp("2026-11-13T12:00:00Z", "2026-11-13T12:00:00+00:00"),
  microDiff: sameIsoTimestamp("2026-11-13T12:00:00.123456Z", "2026-11-13T12:00:00.123457Z"),
  secDiff: sameIsoTimestamp("2026-11-13T12:00:00Z", "2026-11-13T12:00:01Z"),
}};
const missingReasons = expected.filter((code) => !(code in INVESTOR_OVERVIEW_ABSENCE_REASONS));
const fallbackText = translateOverviewAbsenceReason("unmapped_future_reason");
const shareBasisText = translateOverviewAbsenceReason("share_basis_unavailable");
const classes = {{
  equity: translateAssetClass("equity"),
  etf: translateAssetClass("etf"),
  crypto: translateAssetClass("crypto"),
  unknown: translateAssetClass("unknown_kind"),
}};
byId("report-known-at").value = "2026-11-13T12:00:00Z";
renderActivoCompactHeader({{
  asset_id: "equity:us:aapl",
  symbol: "AAPL",
  known_at: "",
  market: {{
    currency: "USD",
    close: {{ state: "available", value: "210.50", reference_at: "2026-11-12T00:00:00Z" }},
  }},
  fundamental: {{ verdict: "favorable", score: {{ state: "available", value: "85.0" }} }},
  valuation: {{
    annual_period_end: "2025-09-27",
    price_to_earnings: {{ state: "available", value: "30.0" }},
  }},
}}, "2026-11-13T12:00:00Z");
JSON.stringify({{
  isoChecks,
  missingReasons,
  fallbackText,
  shareBasisText,
  classes,
  headerText: byId("asset-compact-header").textContent,
}});
`, ctx);
console.log(resultJson);
"""
    res = _run_node(script)
    assert res["isoChecks"] == {
        "zVsMs": True,
        "zVsOffset": True,
        "microDiff": False,
        "secDiff": False,
    }
    assert res["missingReasons"] == []
    assert res["fallbackText"] == "Detalle no disponible"
    assert res["shareBasisText"] == "Base entre título y acción no disponible"
    assert res["classes"] == {
        "equity": "Acción",
        "etf": "ETF",
        "crypto": "Cripto",
        "unknown": "Activo",
    }
    assert "Sin corte" not in res["headerText"]
    assert "Resumen al corte:" in res["headerText"]


def test_mesa_batch_503_partial_recovery_retry_cancel_cut_abort_and_absence() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const testState = {{ fetchMode: "batch2_503" }};
const fetchCalls = [];
const activatedBoards = [];

const ctx = vm.createContext({{
  dom, testState, console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object,
  Promise, JSON, AbortController, Symbol, TextEncoder, Error, RegExp, Intl,
  setTimeout, clearTimeout,
  window: {{
    addEventListener() {{}},
    location: {{ hash: "#mesa", search: "" }},
    innerWidth: 1366,
  }},
  history: {{ replaceState() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    body: dom.bodyNode,
    getElementById: dom.getEl,
    get activeElement() {{ return dom.getActiveElement(); }},
    createElement: (tag) => new dom.FakeNode(tag),
    querySelectorAll: () => [],
    addEventListener() {{}},
  }},
  fetch: async (url, options) => {{
    fetchCalls.push({{ url: String(url), aborted: Boolean(options?.signal?.aborted) }});
    const params = new URLSearchParams(String(url).split("?")[1] || "");
    const ids = params.getAll("asset_id");
    if (testState.fetchMode === "first_503") {{
      return {{
        ok: false,
        status: 503,
        json: async () => ({{ detail: "Service unavailable /tmp/secret/db" }}),
      }};
    }}
    if (testState.fetchMode === "batch2_503" && fetchCalls.length === 2) {{
      return {{
        ok: false,
        status: 503,
        json: async () => ({{ code: "operational_error", detail: "database lock /var/db" }}),
      }};
    }}
    if (testState.fetchMode === "abort_on_second" && fetchCalls.length === 2) {{
      return new Promise((_, reject) => {{
        options.signal.addEventListener("abort", () => {{
          const err = new Error("Aborted");
          err.name = "AbortError";
          reject(err);
        }});
      }});
    }}
    return {{
      ok: true,
      status: 200,
      json: async () => ({{
        schema_version: "investor-overview-v1",
        known_at: params.get("known_at"),
        windows: {{
          market_start: "2026-11-01",
          market_end: "2026-11-12",
          fundamental_start: "2024-01-01",
          fundamental_end: "2025-12-31",
        }},
        assets: ids.map((id) => ({{
          asset_id: id,
          symbol: id.split(":").pop().toUpperCase(),
          name: id,
          asset_class: "equity",
          exchange: "NASDAQ",
          has_corporate_valuation: true,
          market: {{
            currency: "USD",
            close: id.endsWith("0")
              ? {{ state: "missing", value: null, reason_codes: ["no_bar_in_window"] }}
              : {{ state: "available", value: "100.50", reference_at: "2026-11-12T00:00:00Z" }},
            change_1d: {{ state: "available", value: "0.0125" }},
          }},
          fundamental: {{
            score: {{ state: "available", value: "80.0" }},
            verdict: "favorable",
            net_margin: {{ state: "available", value: "0.2" }},
            liabilities_to_assets: {{ state: "available", value: "0.5" }},
          }},
          valuation: {{
            price_to_earnings: {{ state: "available", value: "20.0" }},
          }},
          limitations: [],
        }})),
      }}),
    }};
  }},
}});
vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_ANALYSIS)}, ctx);
vm.runInContext({json.dumps(APP_TECHNICAL)}, ctx);
vm.runInContext({json.dumps(APP_OPERATIONS)}, ctx);
vm.runInContext({json.dumps(APP_MESA)}, ctx);
vm.runInContext({json.dumps(APP_CAZATIBURONES)}, ctx);
vm.runInContext({json.dumps(APP_SHELL)}, ctx);

vm.runInContext(`
const assets25 = {{}};
for (let i = 0; i < 25; i += 1) {{
  const sym = "A" + String(i).padStart(2, "0");
  assets25["equity:us:" + sym.toLowerCase()] = {{
    assetId: "equity:us:" + sym.toLowerCase(),
    symbol: sym,
    name: "Asset " + sym,
    assetClass: "equity",
    exchange: "NASDAQ",
    hasCorporateValuation: true,
    quoteCurrency: "USD",
  }};
}}
marketAssets = assets25;
catalogContext = {{ version: "v-test", sha256: "sha-test" }};
dom.getEl("report-known-at").value = "2026-11-13T12:00:00Z";
activateBoard = (board) => activatedBoards.push(board);
`, ctx);
ctx.activatedBoards = activatedBoards;

(async () => {{
  // 1. Legitimate absence on initial visible batch does NOT retry or show error banner
  testState.fetchMode = "ok";
  await ctx.loadMesaInvestorVisibleBatch();
  const visibleFetchesAfterAbsence = fetchCalls.length;
  const bannerHiddenAfterAbsence = dom.getEl("mesa-investor-banner").classList.contains("hidden");

  // Reset cache for batch 2 503 scenario on 25 assets
  fetchCalls.length = 0;
  ctx.resetInvestorOverviewCache();
  testState.fetchMode = "batch2_503";
  let failedCaught = false;
  try {{
    await ctx.loadMesaInvestorSelectedUniverse();
  }} catch {{
    failedCaught = true;
  }}
  const callsAfterBatch2Error = fetchCalls.map((c) => c.url);
  const bannerText = dom.getEl("mesa-investor-banner-text").textContent;
  const bannerDetail = dom.getEl("mesa-investor-banner-detail").textContent;
  const toolbarRetryHiddenOnBanner = dom.getEl("mesa-retry-load-btn").classList.contains("hidden");
  const bannerRetryHidden = dom.getEl("mesa-banner-retry-btn").classList.contains("hidden");
  vm.runInContext("mesaInvestorUiState.visibleLimit = 25; renderMesaInvestorTable();", ctx);
  const tableHtmlText = dom.nodeText(dom.getEl("mesa-investor-table-body"));

  // System CTA switches to sistema
  ctx.bindMesaInvestorControls();
  dom.getEl("mesa-banner-system-btn").click();

  // Retry fetches ONLY pending 15 in <=10 batches and deduplicates concurrent click
  fetchCalls.length = 0;
  testState.fetchMode = "ok";
  const p1 = ctx.retryMesaPendingAssets();
  const p2 = ctx.retryMesaPendingAssets();
  await Promise.all([p1, p2]);
  const retryChunkSizes = fetchCalls.map(
    (c) => new URLSearchParams(c.url.split("?")[1]).getAll("asset_id").length,
  );
  const bannerHiddenAfterRetry = dom.getEl("mesa-investor-banner").classList.contains("hidden");

  // 1st batch 503 leaves 0 loaded, marks Error + Carga pausada
  fetchCalls.length = 0;
  ctx.resetInvestorOverviewCache();
  testState.fetchMode = "first_503";
  try {{ await ctx.loadMesaInvestorSelectedUniverse(); }} catch {{}}
  const callsOnFirst503 = fetchCalls.length;
  const firstBannerText = dom.getEl("mesa-investor-banner-text").textContent;

  // Cancel mid-flight stops next batch & cut change aborts stale sequence
  fetchCalls.length = 0;
  ctx.resetInvestorOverviewCache();
  testState.fetchMode = "abort_on_second";
  const inFlightCancel = ctx.loadMesaInvestorSelectedUniverse();
  await new Promise((r) => setTimeout(r, 15));
  dom.getEl("mesa-cancel-load-btn").click();
  await inFlightCancel;
  const cancelCallCount = fetchCalls.length;
  const cancelStatusText = dom.getEl("mesa-investor-status").textContent;

  // Cut change while chunk 2 is paused/in-flight stops stale sequence without clobbering active cut
  fetchCalls.length = 0;
  ctx.resetInvestorOverviewCache();
  testState.fetchMode = "abort_on_second";
  const oldCut = "2026-11-13T12:00:00Z";
  const newCut = "2026-11-14T12:00:00Z";
  dom.getEl("report-known-at").value = oldCut;
  const stalePromise = ctx.loadMesaInvestorSelectedUniverse();
  await new Promise((r) => setTimeout(r, 15));
  dom.getEl("report-known-at").value = newCut;
  testState.fetchMode = "ok";
  ctx.abortMesaInvestorRequests();
  await stalePromise;
  await ctx.loadMesaInvestorVisibleBatch();
  const activeCutHasQueried = vm.runInContext("mesaInvestorUiState.hasQueriedCut", ctx);

  // Escape in expanded detail row collapses row & restores focus
  const expandBtn = dom.findFirst(
    dom.getEl("mesa-investor-table-body"),
    (n) => n.dataset?.detailTriggerFor === "equity:us:a01",
  );
  expandBtn.click();
  const detailTr = dom.findFirst(
    dom.getEl("mesa-investor-table-body"),
    (n) => n.className === "mesa-investor-detail-row",
  );
  detailTr.dispatchEvent({{ type: "keydown", key: "Escape" }});
  const detailStillPresent = Boolean(
    dom.findFirst(
      dom.getEl("mesa-investor-table-body"),
      (n) => n.className === "mesa-investor-detail-row",
    ),
  );
  const focusReturnedAsset = dom.getActiveElement()?.dataset?.detailTriggerFor || "";

  // Revisar subtab switching
  ctx.selectRevisarSubtab("explore");
  const exploreHiddenAfterSelect = dom.getEl("review-explore-panel").hidden;
  const candHiddenAfterSelect = dom.getEl("review-master-detail").hidden;

  console.log(JSON.stringify({{
    visibleFetchesAfterAbsence,
    bannerHiddenAfterAbsence,
    failedCaught,
    batch2CallCount: callsAfterBatch2Error.length,
    bannerText,
    bannerDetail,
    toolbarRetryHiddenOnBanner,
    bannerRetryHidden,
    hasErrorPill: tableHtmlText.includes("Error"),
    hasPausedPill: tableHtmlText.includes("Carga pausada"),
    activatedBoards,
    retryChunkSizes,
    bannerHiddenAfterRetry,
    callsOnFirst503,
    firstBannerText,
    cancelCallCount,
    cancelStatusText,
    activeCutHasQueried,
    detailStillPresent,
    focusReturnedAsset,
    exploreHiddenAfterSelect,
    candHiddenAfterSelect,
  }}));
}})();
"""
    res = _run_node(script)
    assert res["visibleFetchesAfterAbsence"] == 1
    assert res["bannerHiddenAfterAbsence"] is True
    assert res["failedCaught"] is True
    assert res["batch2CallCount"] == 2
    assert (
        res["bannerText"] == "No se pudo completar la consulta local. 10 cargados, 15 sin consultar"
    )
    assert "HTTP 503" in res["bannerDetail"]
    assert "/var/db" not in res["bannerDetail"]
    assert res["toolbarRetryHiddenOnBanner"] is True
    assert res["bannerRetryHidden"] is False
    assert res["hasErrorPill"] is True
    assert res["hasPausedPill"] is True
    assert res["activatedBoards"] == ["sistema"]
    assert res["retryChunkSizes"] == [10, 5]
    assert res["bannerHiddenAfterRetry"] is True
    assert res["callsOnFirst503"] == 1
    assert (
        res["firstBannerText"]
        == "No se pudo completar la consulta local. 0 cargados, 25 sin consultar"
    )
    assert res["cancelCallCount"] == 2
    assert "cancelada" in res["cancelStatusText"].lower()
    assert res["activeCutHasQueried"] == "2026-11-14T12:00:00Z"
    assert res["detailStillPresent"] is False
    assert res["focusReturnedAsset"] == "equity:us:a01"
    assert res["exploreHiddenAfterSelect"] is False
    assert res["candHiddenAfterSelect"] is True


def test_cazatiburones_loading_and_error_suppress_stale_sin_consultar() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder, Error, RegExp, Intl,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    body: dom.bodyNode,
    getElementById: dom.getEl,
    createElement: (tag) => new dom.FakeNode(tag),
    querySelectorAll: () => [],
    addEventListener() {{}},
  }},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
}});
vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_CAZATIBURONES)}, ctx);
vm.runInContext(`
renderCazatiburonesUniverseLoading();
const colStatusEl = byId("cazatiburones-universe-collection-status");
const loadingCollectionText = colStatusEl.textContent;
const loadingCollectionHidden = colStatusEl.classList.contains("hidden");
renderCazatiburonesUniverseError("Fallo de red");
const errorCollectionHidden = colStatusEl.classList.contains("hidden");
console.log(JSON.stringify({{
  loadingCollectionText,
  loadingCollectionHidden,
  errorCollectionHidden,
}}));
`, ctx);
"""
    res = _run_node(script)
    assert res["loadingCollectionText"] != "Sin consultar."
    assert res["loadingCollectionHidden"] is True
    assert res["errorCollectionHidden"] is True


def test_mesa_100_rows_2mib_bounds_and_latency_under_100ms() -> None:
    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const ctx = vm.createContext({{
  dom, console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder, Error, RegExp, Intl,
  performance: {{ now: () => Number(process.hrtime.bigint()) / 1e6 }},
  process,
  window: {{ addEventListener() {{}}, location: {{ hash: "", search: "" }}, innerWidth: 1366 }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    body: dom.bodyNode,
    getElementById: dom.getEl,
    createElement: (tag) => new dom.FakeNode(tag),
    querySelectorAll: () => [],
    addEventListener() {{}},
  }},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
}});
vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_ANALYSIS)}, ctx);
vm.runInContext({json.dumps(APP_TECHNICAL)}, ctx);
vm.runInContext({json.dumps(APP_OPERATIONS)}, ctx);
vm.runInContext({json.dumps(APP_MESA)}, ctx);
vm.runInContext({json.dumps(APP_CAZATIBURONES)}, ctx);
vm.runInContext({json.dumps(APP_SHELL)}, ctx);
vm.runInContext(`
const assets100 = {{}};
const cut = "2026-11-13T12:00:00Z";
dom.getEl("report-known-at").value = cut;
catalogContext = {{ version: "v100", sha256: "sha100" }};
const batchAssets = [];
for (let i = 0; i < 100; i += 1) {{
  const id = "equity:us:s" + String(i).padStart(3, "0");
  assets100[id] = {{
    assetId: id,
    symbol: "S" + i,
    name: "Asset " + i,
    assetClass: i % 3 === 0 ? "etf" : i % 3 === 1 ? "crypto" : "equity",
    exchange: "NASDAQ",
    hasCorporateValuation: true,
    quoteCurrency: "USD",
  }};
  batchAssets.push({{
    asset_id: id,
    symbol: "S" + i,
    name: "Asset " + i,
    asset_class: assets100[id].assetClass,
    exchange: "NASDAQ",
    market: {{
      currency: "USD",
      close: {{
        state: "available",
        value: String(100 + i) + ".1234",
        reference_at: "2026-11-12T00:00:00Z",
      }},
      change_1d: {{
        state: "available",
        value: (i % 2 === 0 ? "" : "-") + "0.0" + String(i).padStart(2, "0") + "1",
      }},
    }},
    fundamental: {{
      score: {{ state: "available", value: String(50 + (i % 50)) + ".5" }},
      verdict: "neutral",
    }},
    valuation: {{
      price_to_earnings: {{ state: "available", value: String(10 + i) + ".456" }},
    }},
  }});
}}
marketAssets = assets100;
investorOverviewState.knownAt = cut;
investorOverviewState.contextKey = "v100|" + cut;
for (const item of batchAssets) {{
  investorOverviewState.byAssetId.set(item.asset_id, item);
  const b = approximateJsonBytes(item);
  investorOverviewState.assetByteSizes.set(item.asset_id, b);
  investorOverviewState.approxBytes += b;
}}
mesaInvestorUiState.visibleLimit = 100;
mesaInvestorUiState.sortBy = "pe";
mesaInvestorUiState.sortDir = "desc";
const timings = [];
for (let run = 0; run < 5; run += 1) {{
  const t0 = performance.now();
  renderMesaInvestorTable();
  const dt = performance.now() - t0;
  timings.push(dt);
}}
console.log(JSON.stringify({{
  cachedCount: investorOverviewState.byAssetId.size,
  approxBytes: investorOverviewState.approxBytes,
  renderedRows: dom.getEl("mesa-investor-table-body").children.length,
  maxMs: Math.max(...timings),
}}));
`, ctx);
"""
    res = _run_node(script)
    assert res["cachedCount"] == 100
    assert res["approxBytes"] <= 2 * 1024 * 1024
    assert res["renderedRows"] == 100
    assert res["maxMs"] <= 100.0
