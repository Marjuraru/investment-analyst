"""Behavioral Node harness for the PIT readiness panel."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

_TECHNICAL = Path(str(files("investment_analyst.frontend").joinpath("static/app-technical.js")))
_CORE = Path(str(files("investment_analyst.frontend").joinpath("static/app-core.js")))
_INDEX = Path(str(files("investment_analyst.frontend").joinpath("static/index.html")))
_STYLES = Path(str(files("investment_analyst.frontend").joinpath("static/styles-technical.css")))

_HARNESS = r"""
const fs = require("fs");

function extractFunction(source, name) {
  const pattern = new RegExp("function " + name + "\\([^)]*\\) \\{.*?\\n\\}", "s");
  const match = source.match(pattern);
  if (!match) throw new Error("missing function " + name);
  return match[0];
}

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { for (const name of names) this.values.add(name); }
  remove(...names) { for (const name of names) this.values.delete(name); }
  toggle(name, force) {
    if (force === true) this.values.add(name);
    else if (force === false) this.values.delete(name);
    else if (this.values.has(name)) this.values.delete(name);
    else this.values.add(name);
  }
  contains(name) { return this.values.has(name); }
}

class FakeText {
  constructor(value) { this.textContent = String(value); this.children = []; }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.className = "";
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.href = "";
    this.download = "";
    this.clicked = false;
    this._textContent = "";
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map(textOf).join(" ");
    return this._textContent;
  }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(typeof node === "string" ? new FakeText(node) : node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this._textContent = ""; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) {
    if (!this.listeners[name]) this.listeners[name] = [];
    this.listeners[name].push(handler);
  }
  dispatch(name, event = {}) {
    for (const handler of this.listeners[name] || []) handler(event);
  }
  click() { this.clicked = true; }
}

function textOf(node) {
  if (node === null || node === undefined) return "";
  if (typeof node === "string") return node;
  if (node.children && node.children.length) return node.children.map(textOf).join(" ");
  return node.textContent || "";
}

const IDS = [
  "readiness-start", "readiness-end", "readiness-known-at", "readiness-context",
  "readiness-submit", "readiness-download", "readiness-status", "readiness-results",
  "readiness-decision", "readiness-range", "readiness-identity", "readiness-counts",
  "readiness-floors", "readiness-columns", "readiness-limitations", "readiness-json",
];

const BTC = "crypto:btc-usd";
const AAPL = "equity:us:aapl";
const MARKET_ASSETS = {
  [BTC]: { symbol: "BTC", name: "Bitcoin USD", asset_class: "crypto" },
  [AAPL]: { symbol: "AAPL", name: "Apple Inc.", asset_class: "equity" },
};
const DATASET_ID = "dbb83880-c937-5e08-90e6-7eb99c7025ab";

function projection() {
  const states = (available, missing, pending) => [
    { state: "available", samples: available },
    { state: "missing", samples: missing },
    { state: "not_yet_available", samples: pending },
    { state: "incompatible", samples: 0 },
    { state: "ambiguous", samples: 0 },
  ];
  return {
    schema_version: "research-pit-readiness-view-v1",
    workspace_uuid: "70c64668-1738-4dfe-ba1f-7e72026bb7fe",
    reason_scope: "column_reason_occurrences",
    summary: {
      dataset_id: DATASET_ID,
      dataset_content_sha256: "a".repeat(64),
      experiment_spec_id: "crypto-daily-positive-return-5d-v1",
      target_version: "crypto-forward-close-return-5d-v1",
      feature_set_version: "crypto-market-baseline-3-v1",
      asset: { asset_id: BTC, source_id: "coinbase-exchange:btc-usd:daily-candles" },
      request: {
        asset_id: BTC,
        start_date: "2026-08-03",
        end_date: "2026-11-03",
        dataset_known_at: "2026-11-04T19:51:37Z",
      },
      counts: {
        requested: 93,
        feature_available: 1,
        feature_missing: 0,
        feature_not_yet_available: 92,
        feature_incompatible: 0,
        feature_ambiguous: 0,
        label_available: 88,
        label_missing: 0,
        label_not_yet_available: 5,
        label_incompatible: 0,
        label_ambiguous: 0,
        eligible_before_split: 1,
        purged: 22,
      },
      splits: [
        { split: "train", proposed: 55, purged: 11, remaining: 44,
          eligible_after_purge: 0, positive: 0, negative: 0 },
        { split: "validation", proposed: 19, purged: 11, remaining: 8,
          eligible_after_purge: 0, positive: null, negative: null },
        { split: "holdout", proposed: 19, purged: 0, remaining: 19,
          eligible_after_purge: 1, positive: null, negative: null },
      ],
      readiness: {
        status: "insufficient_pit_sample",
        eligible_before_split: 1,
        train_eligible: 0,
        validation_eligible: 0,
        holdout_eligible: 1,
        train_positive: 0,
        train_negative: 0,
        unmet_requirements: [
          "eligible_before_split 1 < 300",
          "train_eligible 0 < 150",
        ],
      },
      limitations: ["El label es un retorno bruto observado."],
    },
    feature_columns: [
      {
        column_name: "close_return_1d",
        algorithm_version: "predictive-close-ratio-1d-v1",
        unit: "ratio",
        requested: 93,
        states: states(1, 0, 92),
        reasons: [{ code: "close_not_yet_available", occurrences: 172 }],
      },
      {
        column_name: "rsi_14_fraction",
        algorithm_version: "market-rsi-incremental-v2-decimal34",
        unit: "ratio",
        requested: 93,
        states: states(1, 0, 92),
        reasons: [{ code: "metric_not_yet_available", occurrences: 92 }],
      },
      {
        column_name: "macd_12_26_9_histogram_close_ratio",
        algorithm_version: "market-macd-incremental-v2-decimal34",
        unit: "ratio",
        requested: 93,
        states: states(1, 0, 92),
        reasons: [
          { code: "close_not_yet_available", occurrences: 92 },
          { code: "metric_not_yet_available", occurrences: 92 },
        ],
      },
    ],
    limitations: [
      "El label es un retorno bruto observado.",
      "Una lectura 'research_ready' describe suficiencia de muestra PIT.",
    ],
  };
}

function createContext({ assetId = BTC, search = "" } = {}) {
  const technical = fs.readFileSync(process.argv[2], "utf8");
  const core = fs.readFileSync(process.argv[3], "utf8");
  const block = technical.slice(technical.indexOf("const READINESS_VIEW_SCHEMA"));
  if (!block.includes("function initializeReadinessPanel")) {
    throw new Error("readiness block was not found");
  }
  const helperSource = [
    extractFunction(core, "createElement"),
    extractFunction(core, "renderAbsenceMark"),
    extractFunction(core, "numericValue"),
    extractFunction(core, "formatNumber"),
    extractFunction(core, "formatInteger"),
  ].join("\n");
  const elements = new Map();
  for (const id of IDS) elements.set(id, new FakeElement("div"));
  const created = [];
  const document = {
    createElement: (tag) => {
      const element = new FakeElement(tag);
      created.push(element);
      return element;
    },
    getElementById: (id) => elements.get(id) || null,
  };
  const blobs = [];
  const URL = {
    createObjectURL: (blob) => { blobs.push(blob); return "blob:" + blobs.length; },
    revokeObjectURL: () => {},
  };
  class Blob {
    constructor(parts, options) { this.parts = parts; this.options = options; }
    text() { return this.parts.join(""); }
  }
  const apiCalls = [];
  let apiImpl = async () => { throw new Error("unexpected fetch"); };
  const api = (path, options) => {
    apiCalls.push({ path, options });
    return apiImpl(path, options);
  };
  const prefix = `
    const LOCALE = "es-PE";
    ${helperSource}
    const byId = (id) => document.getElementById(id);
    let selectedMarketAsset = state.assetId;
    let marketAssets = state.marketAssets;
  `;
  const suffix = `
    ; return {
      readinessQuery,
      readinessRender,
      readinessDownload,
      resetReadinessPanel,
      readinessKey,
      readinessContext,
      setAsset(value) { selectedMarketAsset = value; readinessContext(); },
    };
  `;
  const factory = new Function(
    "state", "document", "URL", "Blob", "api", "window", prefix + block + suffix,
  );
  const context = factory(
    { assetId, marketAssets: MARKET_ASSETS }, document, URL, Blob, api, { location: { search } },
  );
  return {
    elements,
    created,
    blobs,
    apiCalls,
    context,
    setApi(implementation) { apiImpl = implementation; },
    text(id) { return textOf(elements.get(id)); },
    hidden(id) { return elements.get(id).classList.contains("hidden"); },
  };
}

function assertEquals(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(
      `${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );
  }
}
function assertIncludes(text, fragment, label) {
  if (!String(text).includes(fragment)) {
    throw new Error(`${label}: ${JSON.stringify(fragment)} not found in ${JSON.stringify(text)}`);
  }
}
function assertHides(text, fragment, label) {
  if (String(text).includes(fragment)) {
    throw new Error(`${label}: residual ${JSON.stringify(fragment)} in ${JSON.stringify(text)}`);
  }
}
async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}
function isoDay(offset) {
  const day = new Date();
  day.setUTCHours(0, 0, 0, 0);
  day.setUTCDate(day.getUTCDate() + offset);
  return day.toISOString().slice(0, 10);
}

async function scenarioDefaultsNeverFetch() {
  const ctx = createContext();
  assertEquals(ctx.apiCalls.length, 0, "no fetch on initialization");
  assertEquals(ctx.elements.get("readiness-end").value, isoDay(-1), "default end is yesterday");
  assertEquals(ctx.elements.get("readiness-start").value, isoDay(-366), "366 UTC days");
  assertIncludes(ctx.elements.get("readiness-known-at").value, "T00:00:00Z", "visible cut");
  assertEquals(ctx.hidden("readiness-results"), true, "no results before a query");
  assertIncludes(ctx.text("readiness-context"), "BTC", "current asset context");
  return "defaults_never_fetch";
}

async function scenarioSubmitRendersProjection() {
  const ctx = createContext();
  const payload = projection();
  ctx.setApi(async () => payload);
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-04T19:51:37Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.apiCalls.length, 1, "one explicit fetch");
  const url = new URL("http://local" + ctx.apiCalls[0].path);
  assertEquals(url.pathname, "/api/v1/research/readiness", "route");
  assertEquals(url.searchParams.get("asset_id"), BTC, "asset");
  assertEquals(url.searchParams.get("start_date"), "2026-08-03", "start");
  assertEquals(url.searchParams.get("end_date"), "2026-11-03", "end");
  assertEquals(url.searchParams.get("known_at"), "2026-11-04T19:51:37Z", "cut");
  assertEquals(ctx.hidden("readiness-results"), false, "results visible");
  assertEquals(ctx.text("readiness-decision"), "Muestra PIT insuficiente", "decision label");
  assertIncludes(ctx.text("readiness-range"), "2026-08-03", "range echo");
  assertIncludes(ctx.text("readiness-identity"), DATASET_ID, "dataset identity");
  assertIncludes(ctx.text("readiness-counts"), "Elegibles antes del split", "admission counts");
  assertIncludes(ctx.text("readiness-counts"), "93", "requested count");
  assertIncludes(ctx.text("readiness-floors"), "eligible_before_split 1 < 300", "floor");
  assertIncludes(ctx.text("readiness-columns"), "close_return_1d", "column identity");
  assertIncludes(ctx.text("readiness-columns"), "close_not_yet_available", "reason code");
  assertIncludes(ctx.text("readiness-columns"), "172", "overlapping occurrences");
  assertIncludes(ctx.text("readiness-counts"), "No se expone fuera de train", "heldout absence");
  assertIncludes(ctx.text("readiness-limitations"), "research_ready", "limitation");
  const rendered = JSON.parse(ctx.elements.get("readiness-json").textContent);
  assertEquals(JSON.stringify(rendered), JSON.stringify(payload), "exact projection");
  assertEquals(ctx.elements.get("readiness-download").disabled, false, "download enabled");
  return "submit_renders_projection";
}

async function scenarioIncompatibleAssetShowsReasonWithoutResidualData() {
  const ctx = createContext();
  ctx.setApi(async () => projection());
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-04T19:51:37Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.hidden("readiness-results"), false, "first render visible");
  ctx.context.setAsset(AAPL);
  ctx.context.resetReadinessPanel();
  const failure = new Error(
    "El experimento registrado no admite este activo: sólo cubre el perfil cripto spot USD.",
  );
  failure.code = "asset_incompatible";
  failure.status = 422;
  ctx.setApi(async () => { throw failure; });
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-04T19:51:37Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.hidden("readiness-results"), true, "results hidden on incompatibility");
  assertIncludes(ctx.text("readiness-status"), "cripto spot USD", "explicit reason");
  assertHides(ctx.text("readiness-identity"), DATASET_ID, "no residual BTC identity");
  assertEquals(ctx.elements.get("readiness-download").disabled, true, "download disabled");
  return "incompatible_asset";
}

async function scenarioLateResponseIsDiscarded() {
  const ctx = createContext();
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  ctx.setApi(() => pending);
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-04T19:51:37Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  ctx.context.setAsset(AAPL);
  ctx.context.resetReadinessPanel();
  release(projection());
  await flush();
  assertEquals(ctx.hidden("readiness-results"), true, "late response hidden");
  assertHides(ctx.text("readiness-identity"), DATASET_ID, "late identity discarded");
  assertIncludes(ctx.text("readiness-status"), "Sin consultar", "reset status");
  return "late_response";
}

async function scenarioSelectionChangeInvalidates() {
  const ctx = createContext();
  ctx.setApi(async () => projection());
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-04T19:51:37Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.hidden("readiness-results"), false, "initial render");
  ctx.elements.get("readiness-end").value = "2026-11-02";
  ctx.elements.get("readiness-end").dispatch("input", {});
  assertEquals(ctx.hidden("readiness-results"), true, "range change clears");
  assertEquals(ctx.elements.get("readiness-download").disabled, true, "download cleared");
  assertIncludes(ctx.text("readiness-status"), "La selección cambió", "invalidation status");
  ctx.context.setAsset(AAPL);
  ctx.context.resetReadinessPanel();
  assertIncludes(ctx.text("readiness-context"), "AAPL", "asset context changes");
  return "selection_change_invalidates";
}

async function scenarioDownloadIsTheReceivedProjection() {
  const ctx = createContext();
  const payload = projection();
  ctx.setApi(async () => payload);
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-04T19:51:37Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  ctx.elements.get("readiness-download").dispatch("click", {});
  assertEquals(ctx.blobs.length, 1, "one blob");
  const anchor = ctx.created[ctx.created.length - 1];
  assertEquals(anchor.download, "readiness-" + DATASET_ID + ".json", "dataset-based name");
  assertEquals(anchor.href, "blob:1", "blob url");
  assertEquals(ctx.blobs[0].text(), JSON.stringify(payload), "byte-identical projection");
  ctx.elements.get("readiness-known-at").value = "2026-11-04T20:00:00Z";
  ctx.elements.get("readiness-known-at").dispatch("input", {});
  ctx.elements.get("readiness-download").dispatch("click", {});
  assertEquals(ctx.blobs.length, 1, "cleared download is refused");
  return "download_exact_projection";
}

async function scenarioCutValidationNeverMutatesTheSelection() {
  const ctx = createContext();
  ctx.elements.get("readiness-start").value = "2026-08-03";
  ctx.elements.get("readiness-end").value = "2026-11-03";
  ctx.elements.get("readiness-known-at").value = "2026-11-03T00:00:00Z";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.apiCalls.length, 0, "cut before the final decision never fetches");
  assertIncludes(ctx.text("readiness-status"), "12:00 UTC", "explicit cut reason");
  assertEquals(ctx.elements.get("readiness-end").value, "2026-11-03", "selection unchanged");
  assertEquals(ctx.elements.get("readiness-known-at").value, "2026-11-03T00:00:00Z", "cut kept");
  ctx.elements.get("readiness-known-at").value = "not-a-cut";
  ctx.elements.get("readiness-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.apiCalls.length, 0, "invalid cut never fetches");
  return "cut_validation";
}

(async () => {
  const results = [];
  for (const scenario of [
    scenarioDefaultsNeverFetch,
    scenarioSubmitRendersProjection,
    scenarioIncompatibleAssetShowsReasonWithoutResidualData,
    scenarioLateResponseIsDiscarded,
    scenarioSelectionChangeInvalidates,
    scenarioDownloadIsTheReceivedProjection,
    scenarioCutValidationNeverMutatesTheSelection,
  ]) {
    results.push(await scenario());
  }
  console.log(JSON.stringify({ ok: true, scenarios: results }));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def test_readiness_panel_behavior() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real JavaScript behavior harness")
    result = subprocess.run(
        [node, "-", str(_TECHNICAL), str(_CORE)],
        input=_HARNESS,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["scenarios"] == [
        "defaults_never_fetch",
        "submit_renders_projection",
        "incompatible_asset",
        "late_response",
        "selection_change_invalidates",
        "download_exact_projection",
        "cut_validation",
    ]


def test_readiness_panel_has_no_html_injection_and_one_explicit_route() -> None:
    source = _TECHNICAL.read_text(encoding="utf-8")
    block = source[source.index("const READINESS_VIEW_SCHEMA") :]
    assert "innerHTML" not in block
    assert "https://" not in block
    assert "localStorage" not in block
    assert "sessionStorage" not in block
    assert block.count("/api/v1/research/readiness?") == 1
    assert "initializeReadinessPanel();" in block
    assert "readinessInvalidate" in block
    assert "AbortController" in block
    assert "URL.revokeObjectURL" in block
    index = _INDEX.read_text(encoding="utf-8")
    assert 'id="research-readiness-panel"' in index
    assert "Preparación PIT de datos" in index
    assert "no es un predictor validado" in index
    assert index.count('id="readiness-known-at"') == 1
    assert index.count('id="readiness-submit"') == 1
    assert index.count('id="readiness-download"') == 1
    manifest_reset = 'if (typeof resetReadinessPanel === "function") resetReadinessPanel();'
    core = _CORE.read_text(encoding="utf-8")
    assert core.count(manifest_reset) == 1
    assert '"asset_incompatible"' not in block
    assert "asset_incompatible:" in core
    styles = _STYLES.read_text(encoding="utf-8")
    assert ".readiness-table" in styles
    assert ".readiness-columns" in styles
    assert "box-shadow" not in styles[styles.index(".readiness-section") :]
    assert "border-radius" not in styles[styles.index(".readiness-section") :]
