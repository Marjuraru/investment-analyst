"""Behavioral Node harness for the ML research report disclosure."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

_TECHNICAL = Path(str(files("investment_analyst.frontend").joinpath("static/app-technical.js")))
_CORE = Path(str(files("investment_analyst.frontend").joinpath("static/app-core.js")))

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
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.className = "";
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.title = "";
    this._textContent = "";
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map(textOf).join("");
    return this._textContent;
  }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(typeof node === "string" ? new FakeText(node) : node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); this._textContent = ""; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) {
    if (!this.listeners[name]) this.listeners[name] = [];
    this.listeners[name].push(handler);
  }
  dispatch(name, event = {}) {
    for (const handler of this.listeners[name] || []) handler(event);
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  contains() { return false; }
}

function textOf(node) {
  if (node === null || node === undefined) return "";
  if (typeof node === "string") return node;
  if (node.children && node.children.length) return node.children.map(textOf).join(" ");
  return node.textContent || "";
}

const RESULT_IDS = [
  "ml-research-run-id",
  "ml-research-form",
  "ml-research-status",
  "ml-research-submit",
  "ml-research-results",
  "ml-research-models",
  "ml-research-comparisons",
  "ml-research-folds",
  "ml-research-calibration",
  "ml-research-limitations",
  "ml-research-json",
  "ml-research-range",
  "ml-research-criterion",
  "ml-research-evidence",
  "ml-research-holdout",
  "ml-research-decision",
  "ml-research-availability",
];

const RUN_ID = "fc4ea570-4ffb-55fc-ac49-17d74c328d24";
const OTHER_RUN_ID = "11111111-1111-5111-8111-111111111111";
const ASSET = "crypto:btc-usd";
const MARKET_ASSETS = {
  [ASSET]: { symbol: "BTC", name: "Bitcoin USD" },
  "equity:us:aapl": { symbol: "AAPL", name: "Apple Inc." },
};

function score(scorer, brier) { return { scorer, brier, rows: 633 }; }
function comparison(model, reference, relative, lower, improved) {
  return {
    model,
    reference,
    relative_improvement: relative,
    lower_bound: lower,
    improved_at_least_five_percent: improved,
    lower_bound_positive: improved,
  };
}
const BINS = Array.from({ length: 10 }, (_, index) => ({
  index,
  lower_bound: String(index / 10),
  upper_bound: String((index + 1) / 10),
  includes_upper: index === 9,
  count: index === 5 ? 633 : 0,
  mean_probability: index === 5 ? "0.5457996982914485" : null,
  observed_frequency: index === 5 ? "0.5296052631578947" : null,
}));
const SCORES = [
  score("logistic_regression", "0.24932094730241175"),
  score("hist_gradient_boosting", "0.2517023871592494"),
  score("half_probability", "0.25"),
  score("training_class_prior", "0.24852670913890387"),
];
const COMPARISONS = [
  comparison(
    "logistic_regression", "half_probability",
    "0.002716210790353", "-0.006854522512315087", false,
  ),
  comparison(
    "logistic_regression", "training_class_prior",
    "-0.0031957859429264522", "-0.003212313311785022", false,
  ),
  comparison(
    "hist_gradient_boosting", "half_probability",
    "-0.0068095486369976", "-0.01633022267564781", false,
  ),
  comparison(
    "hist_gradient_boosting", "training_class_prior",
    "-0.012778015012344665", "-0.01693480280613917", false,
  ),
];
function fold(index) {
  return {
    fold: index,
    train_rows: 1000 + index,
    purged_excluded: 10,
    embargo_excluded: 10,
    evaluation_rows: 200,
    evaluation_positives: 100,
    evaluation_negatives: 100,
    scores: SCORES,
    comparisons: COMPARISONS,
    diagnostics: [],
  };
}

function completedPayload() {
  return {
    schema_version: "ml-research-report-view-v1",
    provenance: {
      asset_id: ASSET,
      source_id: null,
      source_id_note: "El protocolo ML sellado no incluye source_id ni venue.",
    },
    artifact: {
      run_id: RUN_ID,
      status: "completed",
      created_at: "2026-11-01T22:25:48.162976Z",
      artifact_bytes: 45176,
      prediction_rows: 2532,
      environment: {
        python_version: "3.12.3",
        scikit_learn_version: "1.9.1",
        numpy_version: "2.5.4",
        scipy_version: "1.18.1",
        threadpoolctl_version: "3.7.0",
        duckdb_version: "1.5.4",
        blas_threads: 1,
      },
    },
    protocol: {
      decision_start: "2020-01-01",
      decision_end: "2025-12-25",
      decision_rows: 2186,
      strict_pit_verified: false,
      limitations: ["Investigación retrospectiva condicional."],
      criteria: {
        min_relative_brier_improvement: "0.05",
        development_lower_quantile: "0.0125",
        holdout_lower_quantile: "0.025",
        tie_tolerance: "1e-12",
        references: ["half_probability", "training_class_prior"],
      },
    },
    development: {
      decision: "no_demonstrated_edge",
      selected_model: null,
      selection_reason: "no candidate reached five percent Brier improvement",
      development_fits: 6,
      folds: [fold(1), fold(2), fold(3)],
      pooled_scores: SCORES,
      pooled_comparisons: COMPARISONS,
      calibration: [
        { scorer: "logistic_regression", bins: BINS },
        { scorer: "hist_gradient_boosting", bins: BINS },
      ],
    },
    holdout: { status: "not_evaluated", holdout_fits: 0 },
    insufficiency_reason: null,
    timings: {
      read_seconds: "1.02",
      fit_seconds: "0.20",
      evaluation_seconds: "0.13",
      total_seconds: "3.66",
    },
    peak_rss_bytes: 256180224,
  };
}

function insufficientPayload() {
  const payload = completedPayload();
  payload.artifact.status = "insufficient_evaluation_sample";
  payload.artifact.prediction_rows = 0;
  payload.development = null;
  payload.holdout = null;
  payload.insufficiency_reason = "<img src=x onerror=alert(1)>";
  payload.protocol.limitations = ["<script>alert('x')</script>"];
  return payload;
}

function createContext({ search = "", assetId = ASSET } = {}) {
  const technical = fs.readFileSync(process.argv[2], "utf8");
  const core = fs.readFileSync(process.argv[3], "utf8");
  // The explorer block owns its own harness; slice this disclosure only.
  const mlSource = technical.slice(
    technical.indexOf("const ML_REPORT_SCHEMA"),
    technical.indexOf("const EXPERIMENT_LIST_SCHEMA"),
  );
  if (!mlSource.includes("function queryMlResearchReport")) {
    throw new Error("ML behavior block was not found");
  }
  const helperSource = [
    extractFunction(core, "createElement"),
    extractFunction(core, "renderAbsenceMark"),
    extractFunction(core, "formatInstant"),
    extractFunction(core, "numericValue"),
    extractFunction(core, "formatNumber"),
    extractFunction(core, "formatInteger"),
  ].join("\n");
  const elements = new Map();
  for (const id of RESULT_IDS) elements.set(id, new FakeElement("div"));
  const document = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => elements.get(id) || null,
  };
  const window = { location: { search } };
  const apiCalls = [];
  let apiImpl = async () => { throw new Error("unexpected fetch"); };
  const api = (path, options) => {
    apiCalls.push({ path, options });
    return apiImpl(path, options);
  };
  const prefix = `
    const LOCALE = "es-PE";
    const DEFAULT_TIME_ZONE = "America/Lima";
    ${helperSource}
    const byId = (id) => document.getElementById(id);
    let selectedMarketAsset = state.assetId;
    let marketAssets = state.marketAssets;
  `;
  const suffix = `
    ; return {
      queryMlResearchReport,
      renderMlResearchReport,
      renderMlResearchModels,
      renderMlResearchComparisons,
      resetMlResearchReport,
      setAsset(value) { selectedMarketAsset = value; },
      setMarketAssets(value) { marketAssets = value; },
    };
  `;
  const factory = new Function(
    "state", "document", "window", "api", prefix + mlSource + suffix,
  );
  const context = factory({ assetId, marketAssets: MARKET_ASSETS }, document, window, api);
  return {
    elements,
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
async function flush() {
  for (let index = 0; index < 5; index += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}
function submit(ctx) {
  ctx.elements.get("ml-research-form").dispatch("submit", { preventDefault() {} });
}
function assertNoUnexpectedTags(node) {
  if (!node || !node.tagName) return;
  if (["img", "script"].includes(node.tagName)) {
    throw new Error("unexpected " + node.tagName + " element");
  }
  for (const child of node.children || []) assertNoUnexpectedTags(child);
}

async function scenarioDirectRender() {
  const ctx = createContext();
  const payload = completedPayload();
  ctx.context.renderMlResearchModels(payload);
  ctx.context.renderMlResearchComparisons(payload);
  ctx.context.renderMlResearchReport(payload, { assetId: ASSET, runId: RUN_ID });
  return "direct_render";
}

async function scenarioSubmitRender() {
  const ctx = createContext();
  ctx.setApi(async () => completedPayload());
  assertEquals(ctx.apiCalls.length, 0, "no fetch on initialization");
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  assertEquals(ctx.apiCalls.length, 1, "one explicit fetch");
  const parameters = new URL("http://local" + ctx.apiCalls[0].path).searchParams;
  assertEquals(parameters.get("asset_id"), ASSET, "asset query");
  assertEquals(parameters.get("run_id"), RUN_ID, "run query");
  assertEquals(
    ctx.hidden("ml-research-results"), false,
    "results visible: " + ctx.text("ml-research-status"),
  );
  assertIncludes(ctx.text("ml-research-status"), "Evaluación verificada", "ready status");
  assertEquals(ctx.text("ml-research-decision"), "No se demostró ventaja", "negative decision");
  assertIncludes(ctx.text("ml-research-holdout"), "Holdout no evaluado", "holdout");
  assertIncludes(ctx.text("ml-research-models"), "Regresión logística", "model row");
  assertIncludes(ctx.text("ml-research-models"), "Brier", "brier heading");
  assertIncludes(ctx.text("ml-research-comparisons"), "Prior de clase", "comparison row");
  assertIncludes(ctx.text("ml-research-criterion"), "El criterio no se cumplió", "criterion");
  assertIncludes(
    ctx.elements.get("ml-research-json").textContent,
    '"0.24932094730241175"',
    "exact decimal",
  );
  return "submit_render";
}

async function scenarioCorruptPayload() {
  const ctx = createContext();
  const payload = completedPayload();
  payload.schema_version = "unknown-view";
  ctx.setApi(async () => payload);
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  assertEquals(ctx.hidden("ml-research-results"), true, "corrupt payload hidden");
  assertIncludes(ctx.text("ml-research-status"), "contrato versionado", "corrupt status");
  return "corrupt_payload";
}

async function scenarioLateResponse() {
  const ctx = createContext();
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  ctx.setApi(() => pending);
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  ctx.context.setAsset("equity:us:aapl");
  ctx.context.resetMlResearchReport();
  release(completedPayload());
  await flush();
  assertEquals(ctx.hidden("ml-research-results"), true, "late response hidden");
  assertIncludes(ctx.text("ml-research-status"), "Sin consultar", "late response discarded");
  return "late_response";
}

async function scenarioEscapingAndNullStates() {
  const ctx = createContext();
  ctx.setApi(async () => insufficientPayload());
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  assertEquals(ctx.text("ml-research-decision"), "Muestra insuficiente", "insufficient decision");
  assertIncludes(ctx.text("ml-research-models"), "Sin evaluación de desarrollo", "absence mark");
  assertIncludes(
    ctx.text("ml-research-limitations"),
    "<script>alert('x')</script>",
    "literal limitation",
  );
  assertIncludes(
    ctx.elements.get("ml-research-json").textContent,
    "<img src=x onerror=alert(1)>",
    "literal json",
  );
  assertNoUnexpectedTags(ctx.elements.get("ml-research-results"));
  return "escaping_null_states";
}

async function scenarioClearOnChanges() {
  const ctx = createContext();
  ctx.setApi(async () => completedPayload());
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  assertEquals(ctx.hidden("ml-research-results"), false, "initial render");
  ctx.elements.get("ml-research-run-id").value = OTHER_RUN_ID;
  ctx.elements.get("ml-research-run-id").dispatch("input", {});
  assertEquals(ctx.hidden("ml-research-results"), true, "run change clears");
  assertEquals(ctx.elements.get("ml-research-models").children.length, 0, "models cleared");
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  ctx.context.setAsset("equity:us:aapl");
  ctx.context.resetMlResearchReport();
  assertEquals(ctx.hidden("ml-research-results"), true, "asset change clears");
  assertIncludes(ctx.text("ml-research-status"), "Sin consultar", "asset reset status");
  return "clear_on_changes";
}

async function scenarioUrlPrefill() {
  const ctx = createContext({ search: "?ml_run_id=" + RUN_ID });
  assertEquals(ctx.elements.get("ml-research-run-id").value, RUN_ID, "url prefill");
  assertEquals(ctx.apiCalls.length, 0, "prefill never executes");
  assertEquals(ctx.hidden("ml-research-results"), true, "prefill stays empty");
  const invalid = createContext({ search: "?ml_run_id=not-a-uuid" });
  assertEquals(invalid.elements.get("ml-research-run-id").value, "", "invalid prefill ignored");
  return "url_prefill";
}

async function scenarioValidation() {
  const ctx = createContext();
  ctx.elements.get("ml-research-run-id").value = "not-a-uuid";
  submit(ctx);
  await flush();
  assertIncludes(ctx.text("ml-research-status"), "UUID canónico", "invalid run id");
  assertEquals(ctx.apiCalls.length, 0, "invalid run id never fetches");
  ctx.context.setAsset(null);
  ctx.elements.get("ml-research-run-id").value = RUN_ID;
  submit(ctx);
  await flush();
  assertIncludes(ctx.text("ml-research-status"), "Selecciona un activo", "missing asset");
  assertEquals(ctx.apiCalls.length, 0, "missing asset never fetches");
  return "validation";
}

(async () => {
  const results = [];
  for (const scenario of [
    scenarioDirectRender,
    scenarioSubmitRender,
    scenarioCorruptPayload,
    scenarioLateResponse,
    scenarioEscapingAndNullStates,
    scenarioClearOnChanges,
    scenarioUrlPrefill,
    scenarioValidation,
  ]) {
    results.push(await scenario());
  }
  console.log(JSON.stringify({ ok: true, scenarios: results }));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def test_ml_report_disclosure_behavior() -> None:
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
        "direct_render",
        "submit_render",
        "corrupt_payload",
        "late_response",
        "escaping_null_states",
        "clear_on_changes",
        "url_prefill",
        "validation",
    ]


def test_ml_report_disclosure_has_no_html_injection_and_one_explicit_route() -> None:
    source = _TECHNICAL.read_text(encoding="utf-8")
    block = source[source.index("const ML_REPORT_SCHEMA") :]
    assert "innerHTML" not in block
    assert block.count("api(`/api/v1/research/ml-report?") == 1
    assert "initializeMlResearchReport();" in block
    assert "https://" not in block
    assert "localStorage" not in block
    assert "sessionStorage" not in block
    index = Path(str(files("investment_analyst.frontend").joinpath("static/index.html"))).read_text(
        encoding="utf-8"
    )
    assert "Investigación retrospectiva; disponibilidad histórica supuesta." in index
    assert 'id="ml-research-panel"' in index
    assert "Consultar evaluación" in index


_EXPLORER_HARNESS = r"""
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
  constructor(value) { this.textContent = String(value); }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.download = "";
    this.href = "";
    this.type = "";
    this.clicked = false;
    this._textContent = "";
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map(textOf).join("");
    return this._textContent;
  }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(typeof node === "string" ? new FakeText(node) : node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this._textContent = ""; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
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
  "experiments-family", "experiments-load", "experiments-next", "experiments-status",
  "experiments-coverage", "experiments-list", "experiments-warnings", "experiments-detail",
  "experiments-detail-title", "experiments-detail-decision", "experiments-detail-context",
  "experiments-detail-reading", "experiments-detail-blocks", "experiments-detail-limitations",
  "experiments-detail-evidence", "experiments-detail-json", "experiments-download",
  "experiments-comparison", "experiments-comparison-status", "experiments-comparison-body",
];

const ASSET = "crypto:btc-usd";
const FUNDING_RUN = "8dbc6330-1ff2-51bc-b701-a65611afa495";

function listPayload() {
  return {
    schema_version: "research-experiment-list-v1",
    context: { asset_id: ASSET, family: "all", limit: 20 },
    items: [{
      family: "funding",
      run_id: FUNDING_RUN,
      asset_id: ASSET,
      protocol_id: "crypto-historical-5d-funding-increment-v1",
      artifact_status: "completed",
      created_at: "2026-11-03T12:00:00Z",
      manifest_sha256: "0".repeat(64),
      protocol_sha256: "1".repeat(64),
      report_sha256: "2".repeat(64),
      artifact_bytes: 56159,
    }],
    next_cursor: null,
    roots: [
      { family: "baseline", status: "available", entries: 2 },
      { family: "funding", status: "available", entries: 3 },
    ],
    warnings: [],
    examined_candidates: 1,
  };
}

function fundingView() {
  return {
    schema_version: "research-experiment-report-view-v1",
    context: {
      asset_id: ASSET,
      family: "funding",
      run_id: FUNDING_RUN,
      manifest_sha256: "0".repeat(64),
      protocol_sha256: "1".repeat(64),
      report_sha256: "2".repeat(64),
      artifact_bytes: 56159,
      created_at: "2026-11-03T12:00:00Z",
    },
    payload: {
      family: "funding",
      protocol: {
        hypothesis: "Causal funding adds incremental information.",
        decision_start: "2020-01-01",
        decision_end: "2025-12-25",
        decision_rows: 2186,
        technical_feature_columns: ["a", "b", "c"],
        funding_feature_columns: ["d", "e"],
        feature_columns: ["a", "b", "c", "d", "e"],
        target: { target_version: "v1", horizon_intervals: 5, positive_rule: "r > 0" },
        criteria: { min_relative_brier_improvement: "0.05" },
        availability_basis: "historical_reconstruction_assumed",
        strict_pit_verified: false,
        dataset_id: "d927c7f1-794a-571c-b729-6bb68d311115",
        dataset_content_sha256: "3".repeat(64),
        bundle_id: "fca740b8-8f38-5703-8006-e6a467bfb3ba",
        bundle_content_sha256: "4".repeat(64),
        limitations: ["Retrospectiva."],
      },
      provenance: {
        dataset_manifest_sha256: "5".repeat(64),
        bundle_manifest_sha256: "6".repeat(64),
        code_material_sha256: "7".repeat(64),
        code_material_files: ["ml_funding_models.py"],
        source_id: null,
        source_id_note: "Sin source_id.",
      },
      environment: { python_version: "3.12.3", scikit_learn_version: "1.9.1" },
      decision: "no_demonstrated_increment",
      decision_rows: 2186,
      prediction_rows: 2532,
      development_fits: 6,
      folds: [],
      pooled_scores: [],
      pooled_comparisons: [],
      calibration: [],
      holdout: { status: "not_evaluated", accesses: 0, fits: 0 },
      timings: {},
      peak_rss_bytes: null,
    },
  };
}

function createExplorerContext() {
  const technical = fs.readFileSync(process.argv[2], "utf8");
  const core = fs.readFileSync(process.argv[3], "utf8");
  // The readiness panel owns its own harness; slice this explorer only.
  const explorer = technical.slice(
    technical.indexOf("const EXPERIMENT_LIST_SCHEMA"),
    technical.indexOf("const READINESS_VIEW_SCHEMA"),
  );
  if (!explorer.includes("function initializeExperimentsExplorer")) {
    throw new Error("explorer block was not found");
  }
  const ml = technical.slice(
    technical.indexOf("const ML_REPORT_SCHEMA"),
    technical.indexOf("const EXPERIMENT_LIST_SCHEMA"),
  );
  const helperSource = [
    extractFunction(core, "createElement"),
    extractFunction(core, "renderAbsenceMark"),
    extractFunction(core, "formatInstant"),
    extractFunction(core, "numericValue"),
    extractFunction(core, "formatNumber"),
    extractFunction(core, "formatInteger"),
    extractFunction(ml, "mlResearchScorerLabel"),
    extractFunction(ml, "mlResearchPercent"),
    extractFunction(ml, "mlResearchFigure"),
    extractFunction(ml, "mlResearchTable"),
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
  const objectUrls = [];
  const URL = {
    createObjectURL: (blob) => { objectUrls.push(blob); return "blob:" + objectUrls.length; },
    revokeObjectURL: () => {},
  };
  const apiCalls = [];
  let apiImpl = async () => { throw new Error("unexpected fetch"); };
  const api = (path, options) => {
    apiCalls.push({ path, options });
    return apiImpl(path, options);
  };
  const prefix = `
    const LOCALE = "es-PE";
    const DEFAULT_TIME_ZONE = "America/Lima";
    ${helperSource}
    const byId = (id) => document.getElementById(id);
    let selectedMarketAsset = state.assetId;
    let marketAssets = state.marketAssets;
  `;
  const suffix = `
    ; return {
      experimentsLoadPage,
      experimentsToggleSelection,
      experimentsComparisonRow,
      experimentsDecision,
      experimentsDownload,
      resetExperimentsExplorer,
      rememberDetail: experimentsRememberDetail,
      selectedKeys: () => Array.from(experimentsSelected.keys()),
      setAsset(value) { selectedMarketAsset = value; },
    };
  `;
  const factory = new Function(
    "state", "document", "URL", "api", prefix + explorer + suffix,
  );
  const context = factory({ assetId: ASSET, marketAssets: {} }, document, URL, api);
  return {
    elements,
    created,
    objectUrls,
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
async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

async function scenarioSelectionLimit() {
  const ctx = createExplorerContext();
  ctx.context.resetExperimentsExplorer();
  const entries = ["1", "2", "3"].map((suffix) => ({
    family: "baseline",
    run_id: "00000000-0000-4000-8000-00000000000" + suffix,
    asset_id: ASSET,
  }));
  ctx.context.experimentsToggleSelection(entries[0], true);
  ctx.context.experimentsToggleSelection(entries[1], true);
  ctx.context.experimentsToggleSelection(entries[2], true);
  assertEquals(ctx.context.selectedKeys().length, 2, "only two studies are contrasted");
  assertIncludes(ctx.text("experiments-status"), "dos estudios", "limit message");
  ctx.context.experimentsToggleSelection(entries[0], false);
  assertEquals(ctx.context.selectedKeys().length, 1, "deselection removes");
  return "selection_limit";
}

async function scenarioDecisionLiterals() {
  const ctx = createExplorerContext();
  const baseline = {
    payload: {
      family: "baseline",
      report: {
        artifact: { status: "completed" },
        development: { decision: "no_demonstrated_edge" },
      },
    },
  };
  const funding = { payload: { family: "funding", decision: "no_demonstrated_increment" } };
  const volatility = { payload: { family: "volatility", decision: "no_demonstrated_increment" } };
  const volatilityEvidence = {
    payload: { family: "volatility", decision: "development_evidence" },
  };
  const unknown = { payload: { family: "funding", decision: null } };
  assertEquals(
    ctx.context.experimentsDecision(baseline).label, "No se demostró ventaja", "baseline",
  );
  assertEquals(
    ctx.context.experimentsDecision(funding).label, "No se demostró incremento", "funding",
  );
  assertEquals(
    ctx.context.experimentsDecision(volatility).label, "No se demostró incremento", "volatility",
  );
  assertEquals(
    ctx.context.experimentsDecision(volatilityEvidence).label,
    "Evidencia de desarrollo; no promovida",
    "volatility evidence is not promoted",
  );
  assertEquals(ctx.context.experimentsDecision(unknown).tone, "bad", "unknown is explicit");
  return "decision_literals";
}

async function scenarioComparisonDifferences() {
  const ctx = createExplorerContext();
  assertEquals(ctx.context.experimentsComparisonRow("X", "1", "1")[3], "Igual", "equal");
  assertEquals(ctx.context.experimentsComparisonRow("X", "1", "2")[3], "Distinto", "different");
  assertEquals(ctx.context.experimentsComparisonRow("X", "—", "2")[3], "Desconocido", "unknown");
  return "comparison_differences";
}

async function scenarioDownload() {
  const ctx = createExplorerContext();
  const view = fundingView();
  ctx.context.rememberDetail(view);
  ctx.context.experimentsDownload();
  const anchor = ctx.created[ctx.created.length - 1];
  assertEquals(anchor.download, "funding-" + FUNDING_RUN + ".json", "deterministic name");
  assertEquals(ctx.objectUrls.length, 1, "one object url");
  const oversized = fundingView();
  oversized.payload.protocol.hypothesis = "x".repeat(1200000);
  ctx.context.rememberDetail(oversized);
  ctx.context.experimentsDownload();
  assertEquals(ctx.objectUrls.length, 1, "oversized export is refused");
  assertIncludes(ctx.text("experiments-status"), "1 MiB", "explicit size error");
  return "download";
}

async function scenarioLateListResponseIsDiscarded() {
  const ctx = createExplorerContext();
  let resolveFirst;
  ctx.setApi(() => new Promise((resolve) => { resolveFirst = resolve; }));
  const loading = ctx.context.experimentsLoadPage(true);
  ctx.context.setAsset("equity:us:aapl");
  resolveFirst(listPayload());
  await loading;
  await flush();
  assertEquals(
    ctx.elements.get("experiments-list").children.length,
    0,
    "late response is discarded",
  );
  assertEquals(ctx.hidden("experiments-comparison"), true, "comparison stays hidden");
  return "late_list_response";
}

(async () => {
  const results = [];
  for (const scenario of [
    scenarioSelectionLimit,
    scenarioDecisionLiterals,
    scenarioComparisonDifferences,
    scenarioDownload,
    scenarioLateListResponseIsDiscarded,
  ]) {
    results.push(await scenario());
  }
  console.log(JSON.stringify({ ok: true, scenarios: results }));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def test_experiment_explorer_behavior() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real JavaScript behavior harness")
    result = subprocess.run(
        [node, "-", str(_TECHNICAL), str(_CORE)],
        input=_EXPLORER_HARNESS,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["scenarios"] == [
        "selection_limit",
        "decision_literals",
        "comparison_differences",
        "download",
        "late_list_response",
    ]


def test_experiment_explorer_has_no_html_injection_and_no_aggregate_scoring() -> None:
    source = _TECHNICAL.read_text(encoding="utf-8")
    block = source[source.index("const EXPERIMENT_LIST_SCHEMA") :]
    assert "innerHTML" not in block
    assert "https://" not in block
    assert "localStorage" not in block
    assert "sessionStorage" not in block
    assert block.count("/api/v1/research/experiments?") == 1
    assert block.count("/api/v1/research/experiment-report?") == 2
    assert "initializeExperimentsExplorer();" in block
    assert "No es una comparación pareada entre familias" in block
    assert "no se calcula delta, significancia cruzada," in block
    assert block.count("mejor estudio") <= 2
