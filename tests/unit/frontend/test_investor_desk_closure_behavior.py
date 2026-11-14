"""Integrated Node behavioral & contract tests for INVESTOR-DESK-CLOSURE (A1-A6, N1)."""

from __future__ import annotations

import json
import re
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
INDEX_HTML = _STATIC.joinpath("index.html").read_text(encoding="utf-8")
STYLES_MESA = _STATIC.joinpath("styles-mesa.css").read_text(encoding="utf-8")
STYLES_ANALYSIS = _STATIC.joinpath("styles-analysis.css").read_text(encoding="utf-8")
APP_CORE = _STATIC.joinpath("app-core.js").read_text(encoding="utf-8")
APP_MESA = _STATIC.joinpath("app-mesa.js").read_text(encoding="utf-8")
APP_ANALYSIS = _STATIC.joinpath("app-analysis.js").read_text(encoding="utf-8")
APP_OPERATIONS = _STATIC.joinpath("app-operations.js").read_text(encoding="utf-8")
APP_CAZA = _STATIC.joinpath("app-cazatiburones.js").read_text(encoding="utf-8")
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
      this._className = "";
      this.value = "";
      this.type = "";
      this.textContent = "";
      this.innerHTML = "";
      this.disabled = false;
      this.hidden = false;
      this.colSpan = 1;
      this.open = false;
      this.title = "";
      this.dataset = {};
      this.style = {};
      this.attrs = {};
      this.children = [];
      this.parentElement = null;
      this.listeners = new Map();
      this.classList = new FakeClassList(this);
    }
    set className(val) {
      this._className = String(val || "");
      this.classList.set = new Set(this._className.split(/\s+/).filter(Boolean));
    }
    get className() { return this._className; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    removeAttribute(k) { delete this.attrs[k]; }
    append(...nodes) {
      for (const n of nodes) {
        if (n === null || n === undefined) continue;
        const child = typeof n === "string"
          ? { textContent: n, children: [], attrs: {}, className: "", parentElement: this }
          : n;
        if (child && typeof child === "object") child.parentElement = this;
        this.children.push(child);
      }
    }
    appendChild(n) { this.append(n); return n; }
    prepend(...nodes) { this.append(...nodes); }
    replaceChildren(...nodes) {
      this.children = [];
      this.innerHTML = "";
      this.append(...nodes);
    }
    addEventListener(type, fn) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(fn);
    }
    dispatchEvent(ev) {
      const event = typeof ev === "string" ? { type: ev } : ev;
      if (!event.preventDefault) event.preventDefault = () => {};
      if (!event.currentTarget) event.currentTarget = this;
      for (const fn of this.listeners.get(event.type) || []) fn(event);
    }
    click() {
      if (this.disabled) return;
      this.dispatchEvent({ type: "click", currentTarget: this });
    }
    focus() {}
    scrollIntoView() {}
    querySelectorAll(sel) {
      const isClass = sel.startsWith(".");
      const cls = isClass ? sel.slice(1) : "";
      const upperTag = !isClass ? sel.toUpperCase() : "";
      const out = [];
      const walk = (n) => {
        if (!n || typeof n !== "object") return;
        const matched = isClass
          ? n.classList?.contains(cls)
            || String(n.className || "").split(/\s+/).includes(cls)
          : n.tagName === upperTag;
        if (matched) out.push(n);
        for (const ch of n.children || []) walk(ch);
      };
      walk(this);
      return out;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    get childElementCount() { return this.children.length; }
    get outerHTML() {
      const cls = this.className ? ` class="${this.className}"` : "";
      const ttl = this.title ? ` title="${this.title}"` : "";
      const aria = this.attrs["aria-label"]
        ? ` aria-label="${this.attrs["aria-label"]}"`
        : "";
      const body = this.innerHTML || nodeText(this);
      const tag = this.tagName.toLowerCase();
      return `<${tag}${cls}${ttl}${aria}>${body}</${tag}>`;
    }
  }
  function getEl(id) {
    if (!elements.has(id)) elements.set(id, new FakeNode("div", id));
    return elements.get(id);
  }
  function nodeText(node) {
    if (!node) return "";
    if (typeof node === "string") return node;
    if (node.innerHTML) return node.innerHTML;
    const own = node.textContent ? [node.textContent] : [];
    const nested = (node.children || []).map(nodeText).filter(Boolean);
    return [...own, ...nested].join(" | ");
  }
  return { getEl, FakeNode, nodeText };
}
"""


def test_a1_mesa_layout_compact_date_token_zero_vs_absence_and_errors() -> None:
    assert "grid-template-columns: minmax(640px, 7fr) minmax(280px, 3fr);" in STYLES_MESA
    assert "@media (max-width: 1190px)" in STYLES_MESA
    assert ".mesa-aside {\n    order: -1;" in STYLES_MESA
    assert ".mesa-date-diff-token {" in STYLES_MESA
    assert "position: absolute;" in STYLES_MESA
    assert "text-overflow: ellipsis;" in STYLES_MESA
    assert "@media (pointer: coarse)" in STYLES_MESA
    assert "mesa-news-disclosure" not in INDEX_HTML
    assert 'id="mesa-quick-panel"' in INDEX_HTML

    core_part = json.dumps(APP_CORE[: APP_CORE.index("function setButtonBusy")])
    mesa_part = json.dumps(APP_MESA[APP_MESA.index("const MESA_INVESTOR_BATCH_SIZE") :])
    nav_start = APP_SHELL.index("const BOARD_REGISTRY = Object.freeze([")
    nav_end = APP_SHELL.index("// UI-5: the only board-to-request graph.")
    nav_part = json.dumps(APP_SHELL[nav_start:nav_end])

    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const overviewPayload = {{
  schema_version: "investor-overview-v1",
  known_at: "2026-11-12T00:00:00Z",
  catalog_version: "v1",
  catalog_sha256: "sha-1",
  windows: {{
    market_start: "2026-10-01",
    market_end: "2026-11-12",
    fundamental_start: "2024-01-01",
    fundamental_end: "2025-12-31",
  }},
  assets: [
    {{
      asset_id: "equity:us:aapl",
      symbol: "AAPL",
      market: {{
        currency: "USD",
        close: {{
          state: "available", value: "0.00", reference_at: "2026-11-12T00:00:00Z"
        }},
        change_1d: {{ state: "available", value: "0.0000" }},
      }},
      fundamental: {{
        score: {{ state: "available", value: "0.0" }}, verdict: "neutral"
      }},
      valuation: {{ price_to_earnings: {{ state: "available", value: "0.00" }} }},
    }},
    {{
      asset_id: "equity:us:bvn",
      symbol: "BVN",
      market: {{
        currency: "USD",
        close: {{
          state: "available", value: "12.50", reference_at: "2026-11-10T00:00:00Z"
        }},
        change_1d: {{ state: "available", value: "-0.0150" }},
      }},
      fundamental: {{
        score: {{
          state: "missing", value: null, reason_codes: ["no_annual_diagnostic_at_known_at"]
        }}
      }},
      valuation: {{
        price_to_earnings: {{
          state: "missing", value: null, reason_codes: ["no_annual_diagnostic_at_known_at"]
        }}
      }},
    }},
    {{
      asset_id: "etf:us:spy",
      symbol: "SPY",
      market: {{
        currency: "USD",
        close: {{
          state: "available", value: "580.10", reference_at: "2026-11-12T00:00:00Z"
        }},
        change_1d: {{ state: "available", value: "0.0050" }},
      }},
      fundamental: {{
        score: {{ state: "not_applicable", value: null, reason_codes: [] }}
      }},
      valuation: {{
        price_to_earnings: {{ state: "not_applicable", value: null, reason_codes: [] }}
      }},
    }},
  ],
}};

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
  fetch: async () => ({{
    ok: true,
    status: 200,
    text: async () => JSON.stringify(overviewPayload),
  }}),
}});

vm.runInContext({core_part}, ctx);
vm.runInContext({mesa_part}, ctx);
vm.runInContext({nav_part}, ctx);

ctx.byId = dom.getEl;
ctx.createElement = (tag, className = "", text = "") => {{
  const el = new dom.FakeNode(tag);
  el.className = className;
  el.textContent = text;
  return el;
}};

vm.runInContext(`
const mkDesc = (asset_id, symbol, name, asset_class, exchange, hasFund) => ({{
  asset_id, symbol, name, asset_class, exchange, quote_currency: "USD",
  provider: "alpaca", provider_identifier: symbol,
  source_id: "alpaca:" + symbol, chart_schema_version: "v1", volume_unit: "shares",
  default_market_start: "2024-01-01",
  analysis: {{
    family: hasFund ? "listed_company" : "etf",
    market_mode: "daily_equity",
    fundamental_mode: hasFund ? "corporate" : "none",
  }},
  has_fundamentals: hasFund, has_corporate_valuation: hasFund, supports_crypto_derivatives: false,
  fundamental_frequencies: hasFund ? ["annual"] : [],
}});
marketAssets = Object.freeze({{
  "equity:us:aapl": marketAssetFromDescriptor(
    mkDesc("equity:us:aapl", "AAPL", "Apple Inc.", "equity", "NASDAQ", true)
  ),
  "equity:us:bvn": marketAssetFromDescriptor(
    mkDesc("equity:us:bvn", "BVN", "Compania de Minas Buenaventura", "equity", "NYSE", true)
  ),
  "etf:us:spy": marketAssetFromDescriptor(
    mkDesc("etf:us:spy", "SPY", "SPDR S&P 500 ETF", "etf", "ARCA", false)
  ),
}});
`, ctx);
dom.getEl("report-known-at").value = "2026-11-12T00:00:00Z";

(async () => {{
  await ctx.loadMesaInvestorOverview();
  const children = dom.getEl("mesa-investor-table-body").children;
  const rows = Object.fromEntries(children.map((r) => [r.dataset.assetId, dom.nodeText(r)]));
  const bvnCloseNode = children
    .find((r) => r.dataset.assetId === "equity:us:bvn")
    .querySelectorAll(".is-date-diff")[0];
  const spyDashNodes = children
    .find((r) => r.dataset.assetId === "etf:us:spy")
    .querySelectorAll(".is-not-applicable");
  const errorPill = ctx.renderInvestorValueCell(null, {{
    rowState: "error", rowError: "timeout"
  }});
  const pausedPill = ctx.renderInvestorValueCell(null, {{
    rowState: "paused", rowError: "anterior falló"
  }});
  const availZeroCell = ctx.renderInvestorValueCell(
    {{ state: "available", value: "0" }},
    {{ formatValue: (v) => "$ " + v }},
  );
  console.log(JSON.stringify({{
    rows,
    bvnHasToken: Boolean(bvnCloseNode?.querySelector(".mesa-date-diff-token")),
    bvnCloseTitle: bvnCloseNode?.title || "",
    spyDashCount: spyDashNodes.length,
    spyDashAria: spyDashNodes[0]?.getAttribute("aria-label") || "",
    errorText: dom.nodeText(errorPill),
    pausedText: dom.nodeText(pausedPill),
    availZeroText: dom.nodeText(availZeroCell),
  }}));
}})();
"""
    res = _run_node(script)
    assert "AAPL" in res["rows"]["equity:us:aapl"]
    assert "NASDAQ" not in res["rows"]["equity:us:aapl"]
    assert "0.00" in res["rows"]["equity:us:aapl"]
    assert "Otra fecha" not in res["rows"]["equity:us:bvn"]
    assert res["bvnHasToken"] is True
    assert "2026-11-10" in res["bvnCloseTitle"]
    assert res["spyDashCount"] == 2
    assert "No aplica" not in res["rows"]["etf:us:spy"]
    assert "No aplica" in res["spyDashAria"]
    assert res["errorText"] == "Error"
    assert res["pausedText"] == "Carga pausada"
    assert "$ 0" in res["availZeroText"]


def test_a2_quick_panel_screening_cazatiburones_and_honest_research() -> None:
    assert "Sin predictor validado; no hay alertas predictivas operativas" in INDEX_HTML
    research_start = INDEX_HTML.index('id="mesa-quick-research"')
    research_end = INDEX_HTML.index("</section>", research_start)
    research_slice = INDEX_HTML[research_start:research_end]
    assert "0%" not in research_slice

    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const fetchLog = [];
const navRequests = [];
const activated = [];
let activityShouldFail = true;

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
  fetch: async (url) => {{
    const u = String(url);
    fetchLog.push(u);
    if (u.includes("/api/v1/candidate-notifications")) {{
      return {{
        ok: true,
        status: 200,
        json: async () => ({{
          total: 5,
          pending_count: 4,
          items: [1, 2, 3, 4, 5].map((i) => ({{
            status: i === 1 ? "acknowledged" : "pending",
            item: {{
              candidate_id: "cand-" + i,
              asset_id: "equity:us:aapl",
              created_at: "2026-11-1" + i + "T10:00:00Z",
              payload: {{ rule_name_es: "Regla #" + i }},
            }},
          }})),
        }}),
      }};
    }}
    if (u.includes("family=institutional")) {{
      return {{
        ok: true,
        status: 200,
        json: async () => ({{
          enabled: true,
          total: 4,
          pending_count: 3,
          truncated: false,
          items: [
            {{
              status: "pending",
              item: {{
                notification_id: "inst-old",
                family: "institutional",
                asset_id: "equity:us:aapl",
                rule_id: "caza_13f_new_position",
                metric_key: "holding_shares",
                unit: "shares",
                value: "1500",
                manager_cik: "0001067983",
                report_period: "2026-06-30",
                available_at: "2026-08-14T20:00:00Z",
                created_at: "2026-08-15T00:00:00Z",
              }},
            }},
            {{
              status: "pending",
              item: {{
                notification_id: "inst-new",
                family: "institutional",
                asset_id: "equity:us:msft",
                rule_id: "caza_13f_increase_shares_pct",
                metric_key: "shares_change_pct",
                unit: "ratio",
                value: "0.25",
                manager_cik: "0001166559",
                prior_report_period: "2026-03-31",
                report_period: "2026-06-30",
                available_at: "2026-10-20T18:00:00Z",
                created_at: "2026-10-20T18:05:00Z",
              }},
            }},
          ],
        }}),
      }};
    }}
    if (u.includes("family=activity")) {{
      if (activityShouldFail) {{
        return {{
          ok: false,
          status: 503,
          json: async () => ({{ detail: "outbox timeout" }}),
        }};
      }}
      return {{
        ok: true,
        status: 200,
        json: async () => ({{
          enabled: true,
          total: 2,
          pending_count: 2,
          truncated: false,
          items: [
            {{
              status: "pending",
              item: {{
                notification_id: "act-latest",
                family: "activity",
                asset_id: "equity:us:aapl",
                rule_id: "caza_insider_net_shares",
                metric_key: "net_shares",
                unit: "shares",
                value: "5000",
                available_at: "2026-11-11T15:00:00Z",
                created_at: "2026-11-11T15:01:00Z",
              }},
            }},
            {{
              status: "pending",
              item: {{
                notification_id: "act-mid",
                family: "activity",
                asset_id: "equity:us:aapl",
                rule_id: "caza_beneficial_ownership_pct",
                metric_key: "beneficial_ownership_pct",
                unit: "ratio",
                value: "0.062",
                available_at: "2026-10-25T12:00:00Z",
                created_at: "2026-10-25T12:01:00Z",
              }},
            }},
          ],
        }}),
      }};
    }}
    return {{ ok: false, status: 404, json: async () => ({{}}) }};
  }},
}});

vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_OPERATIONS)}, ctx);
vm.runInContext({json.dumps(APP_MESA)}, ctx);

ctx.byId = dom.getEl;
ctx.createElement = (tag, className = "", text = "") => {{
  const el = new dom.FakeNode(tag);
  el.className = className;
  el.textContent = text;
  return el;
}};
ctx.api = async (url) => {{
  const res = await ctx.fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}};
ctx.navigateToContext = (req) => {{
  navRequests.push(req);
  return true;
}};
ctx.createContextualNavigationButton = (label, request, className) => {{
  const btn = ctx.createElement("button", className, label);
  btn.addEventListener("click", () => ctx.navigateToContext(request));
  return btn;
}};
vm.runInContext(`
marketAssets = Object.freeze({{
  "equity:us:aapl": {{ assetId: "equity:us:aapl", symbol: "AAPL", name: "Apple Inc." }},
  "equity:us:msft": {{ assetId: "equity:us:msft", symbol: "MSFT", name: "Microsoft Corp." }},
}});
`, ctx);
ctx.activateBoard = (b) => activated.push(b);

(async () => {{
  await ctx.loadMesaAnalyticalNews();
  const screeningItems = dom.getEl("mesa-news-analytical-list").children;
  const fetchesBeforeRevisar = fetchLog.length;
  await ctx.loadCandidateNotifications();
  const fetchesAfterRevisarReuse = fetchLog.length;
  screeningItems[0].click();

  const instUrl = "/api/v1/cazatiburones/notifications?family=institutional&limit=5";
  const actUrl = "/api/v1/cazatiburones/notifications?family=activity&limit=5";
  await ctx.loadMesaCazatiburonesNews("institutional", () => ctx.api(instUrl));
  await ctx.loadMesaCazatiburonesNews("activity", () => ctx.api(actUrl));
  const instCountPartial = dom.getEl("mesa-news-institutional-count").textContent;
  const actCountError = dom.getEl("mesa-news-activity-count").textContent;
  const actRetryEl = dom.getEl("mesa-news-activity-retry");
  const actRetryHiddenOnError = actRetryEl.classList.contains("hidden");

  activityShouldFail = false;
  await ctx.loadMesaCazatiburonesNews("activity", () => ctx.api(actUrl));
  const cazaQuickBtns = dom.getEl("mesa-cazatiburones-quick-list").querySelectorAll("button");
  const cazaQuickTexts = cazaQuickBtns.map((b) => dom.nodeText(b));
  cazaQuickBtns[0].click();

  console.log(JSON.stringify({{
    screeningCountText: dom.getEl("mesa-news-analytical-count").textContent,
    screeningItemCount: screeningItems.length,
    fetchesBeforeRevisar,
    fetchesAfterRevisarReuse,
    ackPosted: fetchLog.some((u) => u.includes("acknowledge")),
    instCountPartial,
    actCountError,
    actRetryHiddenOnError,
    actCountRecovered: dom.getEl("mesa-news-activity-count").textContent,
    cazaQuickCount: cazaQuickBtns.length,
    cazaQuickTexts,
    navRequests,
  }}));
}})();
"""
    res = _run_node(script)
    assert res["screeningCountText"] == "Bandeja: 5 · 4 sin acuse"
    assert res["screeningItemCount"] == 3
    assert res["fetchesBeforeRevisar"] == res["fetchesAfterRevisarReuse"] == 1
    assert res["ackPosted"] is False
    assert res["navRequests"][0] == {"board": "revisar", "family": "candidate", "id": "cand-1"}

    assert res["instCountPartial"] == "4 · 3 sin acuse"
    assert res["actCountError"] == "Sin consulta"
    assert res["actRetryHiddenOnError"] is False
    assert res["actCountRecovered"] == "2 · 2 sin acuse"
    assert res["cazaQuickCount"] == 3
    # Sorted descending by available_at: act-latest (Nov 11), act-mid (Oct 25), inst-new (Oct 20)
    assert "AAPL" in res["cazaQuickTexts"][0]
    assert "Actividad declarada" in res["cazaQuickTexts"][0]
    assert any(p in res["cazaQuickTexts"][2] for p in ("25,00%", "+25,00%", "25.00%"))
    assert res["navRequests"][1]["board"] == "cazatiburones"
    assert res["navRequests"][1]["context"]["notification_id"] == "act-latest"


def test_a3_cazatiburones_clarity_context_summary_vs_full_and_empty_state() -> None:
    expected_heading = "Quién declaró actividad sobre este activo, qué declaró y cuándo se conoció"
    assert expected_heading in INDEX_HTML
    assert 'id="cazatiburones-evidence-disclosure"' in INDEX_HTML
    caza_code = APP_CAZA[APP_CAZA.index("let cazatiburonesRequestSequence") :]
    assert "portfolio" not in caza_code.lower()

    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
const urls = [];
let mode = "matched";

const ctx = vm.createContext({{
  console, URLSearchParams, Map, Set, Date, Math, Number, String, Array, Object, Promise, JSON,
  AbortController, Symbol, TextEncoder, setTimeout, clearTimeout,
  window: {{ addEventListener() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "#000" }}),
  document: {{
    documentElement: {{}},
    getElementById: dom.getEl,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => new dom.FakeNode(tag),
  }},
}});

vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_CAZA)}, ctx);

ctx.byId = dom.getEl;
ctx.createElement = (tag, className = "", text = "") => {{
  const el = new dom.FakeNode(tag);
  el.className = className;
  el.textContent = text;
  return el;
}};
ctx.contextualNavigationUnavailable = () => false;
vm.runInContext(`
marketAssets = Object.freeze({{
  "equity:us:aapl": {{
    assetId: "equity:us:aapl",
    symbol: "AAPL",
    name: "Apple Inc.",
    hasFundamentals: true,
    fundamentalMode: "corporate",
  }},
}});
`, ctx);
dom.getEl("report-known-at").value = "2026-11-12T00:00:00Z";
ctx.api = async (url) => {{
  const u = String(url);
  urls.push(u);
  if (u.includes("/declared-activity")) {{
    return {{
      total_statements: 0, truncated: false, insider_features: [], beneficial_features: []
    }};
  }}
  if (u.includes("/sec-document-timeline")) {{
    return {{
      state: "missing",
      matched_count: 0,
      returned_count: 0,
      legacy_records_excluded: 0,
      truncated: false,
      entries: [],
    }};
  }}
  if (u.includes("/institutional-observations")) {{
    if (mode === "empty") {{
      return {{ total_matching: 0, truncated: false, observations: [] }};
    }}
    return {{
      total_matching: 1,
      truncated: false,
      observations: [
        {{
          report: {{
            manager_cik: "0001067983",
            report_id: "rep-1",
            report_period: "2026-06-30",
          }},
          row: {{ cusip: "037833100", title_of_class: "COM" }},
          observation: {{
            observation_id: mode === "matched" ? "obs-target-1" : "obs-other-99",
            field_name: "shares_or_principal_amount",
            value: "1500",
            unit: "shares",
            available_at: "2026-08-14T20:00:00Z",
          }},
          compatibility_notes: [],
        }},
      ],
    }};
  }}
  return {{}};
}};

ctx.initializeCazatiburonesUniverseFilters();
dom.getEl("cazatiburones-selected-context").append(
  dom.getEl("cazatiburones-context-titulo"),
  dom.getEl("cazatiburones-context-summary"),
  dom.getEl("cazatiburones-context-body"),
);

(async () => {{
  ctx.selectCazatiburonesNotificationTarget("equity:us:aapl", "institutional", {{
    notification_id: "n-13f",
    family: "institutional",
    asset_id: "equity:us:aapl",
    rule_id: "caza_13f_new_position",
    metric_key: "holding_shares",
    unit: "shares",
    value: "1500",
    manager_cik: "0001067983",
    report_period: "2026-06-30",
    available_at: "2026-08-14T20:00:00Z",
    input_observation_ids: ["obs-target-1"],
  }});
  await new Promise((r) => setTimeout(r, 15));
  const summaryUrls = [...urls];
  const matchedText = dom.nodeText(dom.getEl("cazatiburones-selected-context"));
  const obsRows = dom.getEl("cazatiburones-institutional-observations-rows");
  const matchedRowCount = obsRows.querySelectorAll(".is-target-match").length;
  const fullBtnWasVisible = !dom.getEl("cazatiburones-load-full-btn").classList.contains("hidden");

  await ctx.loadCazatiburonesFullObservations();
  const fullUrlDispatched = urls.some((u) => u.includes("projection=full"));

  mode = "unmatched_cik";
  ctx.selectCazatiburonesNotificationTarget("equity:us:aapl", "institutional", {{
    notification_id: "n-13f-unmatched",
    family: "institutional",
    asset_id: "equity:us:aapl",
    rule_id: "caza_13f_new_position",
    manager_cik: "0009999999",
    available_at: "2026-08-14T20:00:00Z",
    input_observation_ids: ["obs-missing-id"],
  }});
  await new Promise((r) => setTimeout(r, 15));
  const unmatchedText = dom.nodeText(dom.getEl("cazatiburones-selected-context"));

  mode = "empty";
  await ctx.loadCazatiburonesBoard("equity:us:aapl");
  const emptyText = dom.nodeText(dom.getEl("cazatiburones-institutional-observations-rows"));

  console.log(JSON.stringify({{
    summaryUrls,
    matchedText,
    matchedRowCount,
    fullBtnWasVisible,
    fullUrlDispatched,
    unmatchedText,
    emptyText,
  }}));
}})();
"""
    res = _run_node(script)
    assert any("projection=summary" in u and "limit=200" in u for u in res["summaryUrls"])
    assert not any("projection=full" in u for u in res["summaryUrls"])
    assert "0001067983" in res["matchedText"]
    assert "Coincidencia exacta" in res["matchedText"]
    assert res["matchedRowCount"] == 1
    assert res["fullBtnWasVisible"] is True
    assert res["fullUrlDispatched"] is True
    assert "no aísla exactamente" in res["unmatchedText"]
    assert "No hay declaraciones elegibles en este corte" in res["emptyText"]


def test_a4_transversal_no_aplica_and_sec_annual_absence_semantics() -> None:
    assert ".value-absence-slot" in STYLES_ANALYSIS
    absence_rules = re.findall(r"^\.absence-mark\.([a-z-]+)\s*\{", STYLES_ANALYSIS, re.MULTILINE)
    assert set(absence_rules) == {
        "missing",
        "not-evaluable",
        "blocked",
        "not-applicable",
        "overdue",
    }

    script = f"""
{_DOM_PREAMBLE}
const dom = createFakeDom();
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
}});

vm.runInContext({json.dumps(APP_CORE)}, ctx);
vm.runInContext({json.dumps(APP_ANALYSIS)}, ctx);

ctx.byId = dom.getEl;
ctx.nodeText = dom.nodeText;
ctx.createElement = (tag, className = "", text = "") => {{
  const el = new dom.FakeNode(tag);
  el.className = className;
  el.textContent = text;
  return el;
}};

vm.runInContext(`
const naNode = valuationDisplayValue(
  {{ status: "not_applicable", reason_code: "asset_class_has_no_valuation" }},
  {{ key: "price_to_earnings", unit: "multiple" }},
);
const naSlotText = naNode.querySelector(".value-dash-visible")?.textContent || "";
const naSlotTitle = naNode.title || "";
const naMarkAria = naNode.querySelector(".not-applicable")?.getAttribute("aria-label") || "";

const unavailableEl = createElement("div");
renderUnavailable(unavailableEl, "Fundamentales", "No hay diagnóstico elegible en versión v1");
const unavailableText = nodeText(unavailableEl);

console.log(JSON.stringify({{
  naSlotClass: naNode.className,
  naSlotText,
  naSlotTitle,
  naMarkAria,
  unavailableText,
  noDiagLabel: INVESTOR_OVERVIEW_ABSENCE_REASONS.no_annual_diagnostic_at_known_at,
}}));
`, ctx);
"""
    res = _run_node(script)
    assert res["naSlotClass"] == "value-absence-slot"
    assert res["naSlotText"] == "—"
    assert "no aplica" in res["naSlotTitle"].lower()
    assert "no aplica" in res["naMarkAria"].lower()
    assert "Sin diagnóstico anual elegible" in res["unavailableText"]
    assert "Sin diagnóstico anual elegible" in res["noDiagLabel"]
