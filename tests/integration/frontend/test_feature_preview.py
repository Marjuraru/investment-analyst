"""Socket-level feature preview contract, reuse, CLI parity and panel behavior."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application import facade as facade_module
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.frontend.local_web import (
    AaplLocalController,
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.research.feature_preview_models import (
    MAX_FEATURE_PREVIEW_BYTES,
    FeaturePreviewIntegrityError,
    FeaturePreviewRequest,
    ResearchFeaturePreview,
)
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_models import (
    PredictiveDatasetRequest,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.storage.repositories import DuckDBMetricResultRepository
from investment_analyst.storage.workspace_v2_repositories import WorkspaceV2MetricResultRepository
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInitialization
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).resolve().parents[3]
_BTC = "crypto:btc-usd"
_BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_SOL = "crypto:sol-usd"
_SOL_SOURCE = "coinbase-exchange:sol-usd:daily-candles"
_DECISION = date(2026, 6, 10)
_DECISION_AT = decision_instant(_DECISION)
_RSI_VERSION = "market-rsi-incremental-v2-decimal34"
_MACD_VERSION = "market-macd-incremental-v2-decimal34"


def _days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _raw(
    *,
    asset_id: str,
    source_id: str,
    day: date,
    value: Decimal,
    revision: str,
    available_at: datetime,
) -> RawRecord:
    return RawRecord(
        record_id=uuid5(
            NAMESPACE_URL,
            f"feature-preview-raw|{asset_id}|{source_id}|{day.isoformat()}|{revision}",
        ),
        asset_id=asset_id,
        source=SourceReference(
            source_id=source_id,
            retrieved_at=available_at,
            record_key=f"{day.isoformat()}|{revision}",
        ),
        event_time=bar_start_instant(day),
        available_at=available_at,
        received_at=available_at,
        payload={"close": str(value)},
        schema_version="feature-preview-integration-v1",
    )


def _observation(
    *,
    raw: RawRecord,
    asset_id: str,
    source_id: str,
    day: date,
    value: Decimal,
    available_at: datetime,
) -> NormalizedObservation:
    start = bar_start_instant(day)
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{raw.record_id}|close"),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name="close",
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=start,
        period_start=start,
        period_end=start + timedelta(days=1),
        available_at=available_at,
        normalized_at=available_at,
        source=SourceReference(source_id=source_id, retrieved_at=available_at),
        quality=DataQuality.VALID,
        transformation_version="coinbase-candle-normalizer-v1",
    )


def _metric(
    *,
    asset_id: str,
    source_id: str,
    day: date,
    key: str,
    value: Decimal,
    available_at: datetime,
    input_observation_id: UUID,
    seed: str,
    version: str,
    unit: str,
    parameters: dict[str, object],
) -> MetricResult:
    placeholder = MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"feature-preview-metric|{seed}"),
        asset_id=asset_id,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=bar_start_instant(day),
        available_at=available_at,
        computed_at=available_at,
        parameters={"source_id": source_id, **parameters},
        input_observation_ids=[input_observation_id],
        algorithm_version=version,
        quality=DataQuality.VALID,
    )
    return placeholder.model_copy(update={"result_id": metric_result_id_from_model_v2(placeholder)})


def _seed_series(
    writer: object,
    *,
    asset_id: str,
    source_id: str,
    days: tuple[date, ...],
    start_value: int,
    suffix: str,
) -> None:
    raws: list[RawRecord] = []
    observations: list[NormalizedObservation] = []
    metrics: list[MetricResult] = []
    observation_by_day: dict[date, NormalizedObservation] = {}
    for index, day in enumerate(days):
        available = bar_start_instant(day) + timedelta(days=1, hours=1)
        value = Decimal(start_value + index)
        raw = _raw(
            asset_id=asset_id,
            source_id=source_id,
            day=day,
            value=value,
            revision=f"base|{suffix}",
            available_at=available,
        )
        observation = _observation(
            raw=raw,
            asset_id=asset_id,
            source_id=source_id,
            day=day,
            value=value,
            available_at=available,
        )
        raws.append(raw)
        observations.append(observation)
        observation_by_day[day] = observation
    writer.raw_records.save_many(raws)  # type: ignore[attr-defined]
    writer.observations.save_many(observations)  # type: ignore[attr-defined]
    for index, day in enumerate(days):
        decision = day + timedelta(days=1)
        available = decision_instant(decision) - timedelta(hours=11)
        observation = observation_by_day[day]
        metrics.append(
            _metric(
                asset_id=asset_id,
                source_id=source_id,
                day=day,
                key="market.technical.rsi",
                value=Decimal(40 + (index % 20)),
                available_at=available,
                input_observation_id=observation.observation_id,
                seed=f"{asset_id}|rsi|{day.isoformat()}|{suffix}",
                version=_RSI_VERSION,
                unit="index",
                parameters={"window": 14},
            )
        )
        metrics.append(
            _metric(
                asset_id=asset_id,
                source_id=source_id,
                day=day,
                key="market.technical.macd.histogram",
                value=Decimal("2"),
                available_at=available,
                input_observation_id=observation.observation_id,
                seed=f"{asset_id}|macd|{day.isoformat()}|{suffix}",
                version=_MACD_VERSION,
                unit="USD",
                parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
            )
        )
    writer.metric_results.save_many(metrics)  # type: ignore[attr-defined]


def _seed_workspace(tmp_path: Path) -> tuple[WorkspaceService, WorkspaceInitialization]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace", format_version=2)
    close_days = _days(_DECISION - timedelta(days=2), _DECISION)
    writer = service.open_storage(initialization.paths, WorkspaceAccessMode.READ_WRITE)
    try:
        _seed_series(
            writer,
            asset_id=_BTC,
            source_id=_BTC_SOURCE,
            days=close_days,
            start_value=100,
            suffix="btc",
        )
        _seed_series(
            writer,
            asset_id=_SOL,
            source_id=_SOL_SOURCE,
            days=_days(_DECISION - timedelta(days=1), _DECISION),
            start_value=200,
            suffix="sol-1x",
        )
    finally:
        writer.close()
    return service, initialization


def _tree_state(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _db_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".duckdb", ".wal", ".db"}
    )


@contextmanager
def _server(application: object) -> Iterator[str]:
    server = AaplLocalHttpServer(("127.0.0.1", 0), cast(AaplLocalWebApplication, application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _application(root: Path) -> AaplLocalWebApplication:
    controller = AaplLocalController.create_default(
        workspace=root,
        alpaca_credentials=AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        sec_identity=SecEdgarIdentity(user_agent="investment-analyst test test@example.com"),
    )
    return AaplLocalWebApplication(controller, None)


def _get(
    root: str,
    parameters: dict[str, str] | list[tuple[str, str]],
) -> tuple[int, dict[str, object]]:
    request = Request(f"{root}/api/v1/research/feature-preview?{urlencode(parameters)}")
    try:
        response = urlopen(request, timeout=60)
    except HTTPError as error:
        return error.code, json.loads(error.read())
    with response:
        return response.status, json.loads(response.read())


def _parameters(
    *,
    asset_id: str = _BTC,
    decision_date: date = _DECISION,
    known_at: datetime = _DECISION_AT,
) -> dict[str, str]:
    return {
        "asset_id": asset_id,
        "decision_date": decision_date.isoformat(),
        "known_at": known_at.isoformat(),
    }


def _preview_equals_dataset(
    service: WorkspaceService,
    initialization: WorkspaceInitialization,
) -> None:
    request = FeaturePreviewRequest(
        asset_id=_BTC,
        decision_date=_DECISION,
        known_at=_DECISION_AT,
    )
    dataset_request = PredictiveDatasetRequest(
        asset_id=_BTC,
        start_date=_DECISION,
        end_date=_DECISION,
        dataset_known_at=_DECISION_AT,
    )
    runtime = ApplicationRuntime.create_default()
    with runtime.open_storage(
        StorageLocationRequest(workspace=initialization.paths.root),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        builder = PredictiveDatasetBuilder(
            storage,
            provider_resolver=runtime.provider_resolver,
            catalog=runtime.catalog,
        )
        preview = builder.preview_features(request)
        dataset = builder.build(dataset_request)
    sample = dataset.samples[0]
    assert preview.features == sample.features
    assert preview.sample_key == sample.sample_key
    assert preview.feature_hash == sample.feature_hash


def test_feature_preview_http_contract_cold_warm_foreign_corpus_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, initialization = _seed_workspace(tmp_path)
    root = initialization.paths.root
    calls: list[FeaturePreviewRequest] = []
    hydrated: list[MetricResult] = []
    pages: list[int] = []
    original_preview = facade_module.PredictiveDatasetBuilder.preview_features

    def counting_preview(
        builder: PredictiveDatasetBuilder,
        request: FeaturePreviewRequest,
    ) -> ResearchFeaturePreview:
        calls.append(request)
        return original_preview(builder, request)

    monkeypatch.setattr(
        facade_module.PredictiveDatasetBuilder,
        "preview_features",
        counting_preview,
    )
    for repository_class in (DuckDBMetricResultRepository, WorkspaceV2MetricResultRepository):
        original_get_many = repository_class.get_many
        original_page = repository_class.list_metric_index_page

        def recording_get_many(
            repository: object,
            result_ids: Collection[UUID],
            _original: object = original_get_many,
        ) -> dict[UUID, MetricResult]:
            result = _original(repository, result_ids)  # type: ignore[operator]
            hydrated.extend(result.values())
            return result

        def recording_page(
            repository: object,
            query: object,
            _original: object = original_page,
        ) -> object:
            result = _original(repository, query)  # type: ignore[operator]
            pages.append(len(result))  # type: ignore[arg-type]
            return result

        monkeypatch.setattr(repository_class, "get_many", recording_get_many)
        monkeypatch.setattr(repository_class, "list_metric_index_page", recording_page)

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    before = _tree_state(root)
    bytes_before = _db_bytes(root)

    with _server(_application(root)) as base:
        tracemalloc.start()
        started = time.perf_counter()
        status, cold = _get(base, _parameters())
        cold_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert status == 200, cold
        assert len(calls) == 1
        cold_bytes = len(json.dumps(cold, separators=(",", ":")).encode("utf-8"))
        assert cold_bytes <= MAX_FEATURE_PREVIEW_BYTES
        assert cold["schema_version"] == "research-feature-preview-v1"
        assert cold["status"] == "features_available"
        assert cold["decision_at"] == "2026-06-10T12:00:00Z"
        assert cold["request"] == {
            "asset_id": _BTC,
            "decision_date": "2026-06-10",
            "known_at": "2026-06-10T12:00:00Z",
        }
        assert len(cast(list[object], cold["features"])) == 3
        assert len(cast(list[object], cold["close_references"])) == 2
        assert hydrated and {metric.asset_id for metric in hydrated} == {_BTC}
        assert {metric.parameters.get("source_id") for metric in hydrated} == {_BTC_SOURCE}
        assert pages and all(0 < size <= 256 for size in pages)

        started = time.perf_counter()
        warm_status, warm = _get(base, _parameters())
        warm_seconds = time.perf_counter() - started
        assert warm_status == 200
        assert warm == cold
        assert len(calls) == 1, "a valid hit must not rebuild the preview"
        assert warm_seconds <= max(cold_seconds * 5, 1.0)

        _preview_equals_dataset(service, initialization)

    after = _tree_state(root)
    assert after == before, "a feature preview read must not write the workspace"
    assert _db_bytes(root) - bytes_before == 0

    writer = service.open_storage(initialization.paths, WorkspaceAccessMode.READ_WRITE)
    try:
        for index in range(9):
            _seed_series(
                writer,
                asset_id=_SOL,
                source_id=_SOL_SOURCE,
                days=_days(_DECISION - timedelta(days=1), _DECISION),
                start_value=300 + index * 100,
                suffix=f"sol-10x-{index}",
            )
    finally:
        writer.close()

    calls.clear()
    hydrated.clear()
    pages.clear()
    fresh_before = _tree_state(root)
    with _server(_application(root)) as base:
        foreign_status, foreign = _get(base, _parameters())
        assert foreign_status == 200
        assert foreign == cold
        assert len(calls) == 1
        assert hydrated and {metric.asset_id for metric in hydrated} == {_BTC}
        assert pages and all(0 < size <= 256 for size in pages)
    assert _tree_state(root) == fresh_before

    cli_before = _tree_state(root)
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts/preview_predictive_features.py"),
            "--workspace",
            str(root),
            "--asset-id",
            _BTC,
            "--decision-date",
            _DECISION.isoformat(),
            "--known-at",
            _DECISION_AT.isoformat(),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(_ROOT / "src")},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == cold
    assert _tree_state(root) == cli_before

    evidence = {
        "cold_seconds": round(cold_seconds, 4),
        "warm_seconds": round(warm_seconds, 4),
        "response_bytes": cold_bytes,
        "tracemalloc_peak_bytes": peak,
        "max_rss_growth_bytes": max(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before, 0
        )
        * 1024,
    }
    assert evidence["cold_seconds"] > 0
    assert evidence["warm_seconds"] > 0
    assert evidence["response_bytes"] <= MAX_FEATURE_PREVIEW_BYTES
    assert evidence["tracemalloc_peak_bytes"] > 0


def test_feature_preview_http_rejects_invalid_cuts_assets_and_integrity_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, initialization = _seed_workspace(tmp_path)
    with _server(_application(initialization.paths.root)) as base:
        status, payload = _get(base, _parameters(asset_id="equity:us:aapl"))
        assert status == 422
        assert cast(dict[str, object], payload["error"])["code"] == "asset_incompatible"

        status, payload = _get(base, {**_parameters(), "unknown": "1"})
        assert status == 400
        assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

        status, payload = _get(
            base,
            {key: value for key, value in _parameters().items() if key != "known_at"},
        )
        assert status == 400
        assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

        status, payload = _get(
            base,
            [
                ("asset_id", _BTC),
                ("asset_id", _BTC),
                ("decision_date", _DECISION.isoformat()),
                ("known_at", _DECISION_AT.isoformat()),
            ],
        )
        assert status == 400
        assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

        before_decision = bar_start_instant(_DECISION) - timedelta(seconds=1)
        status, _ = _get(base, _parameters(known_at=before_decision))
        assert status == 400
        status, _ = _get(base, _parameters(known_at=_DECISION_AT + timedelta(seconds=1)))
        assert status == 400
        status, _ = _get(base, _parameters(known_at=datetime(2026, 6, 10, 10, 0)))
        assert status == 400
        status, _ = _get(base, {**_parameters(), "decision_date": "2026-06-10T00:00:00Z"})
        assert status == 400

        def failing_preview(
            builder: PredictiveDatasetBuilder,
            request: FeaturePreviewRequest,
        ) -> ResearchFeaturePreview:
            raise FeaturePreviewIntegrityError("metric index contradicts its projection")

        monkeypatch.setattr(
            facade_module.PredictiveDatasetBuilder,
            "preview_features",
            failing_preview,
        )
        status, payload = _get(base, _parameters())
        assert status == 503
        assert cast(dict[str, object], payload["error"])["code"] == "operational_error"
        assert "features_available" not in json.dumps(payload)


def test_feature_preview_cli_help_does_not_import_ml_dependencies() -> None:
    blocker = r"""
import runpy
import sys

class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in {"sklearn", "numpy", "scipy", "threadpoolctl"}:
            raise ImportError("blocked ML dependency: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
sys.argv = ["preview_predictive_features.py", "--help"]
runpy.run_path("scripts/preview_predictive_features.py", run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        capture_output=True,
        text=True,
        check=False,
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": str(_ROOT / "src")},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "blocked ML dependency" not in result.stderr
    assert "usage:" in result.stdout


_HARNESS = r"""
const fs = require("fs");

function extractBlock(source) {
  const start = source.indexOf("const FEATURE_PREVIEW_SCHEMA");
  const marker = "initializeFeaturePreviewPanel();";
  const end = source.indexOf(marker, start);
  if (start < 0 || end < 0) throw new Error("feature preview block was not found");
  return source.slice(start, end + marker.length);
}

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { for (const name of names) this.values.add(name); }
  remove(...names) { for (const name of names) this.values.delete(name); }
  contains(name) { return this.values.has(name); }
}

class FakeText {
  constructor(value) { this.textContent = String(value); this.children = []; }
}

class FakeElement {
  constructor(tagName, className = "") {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.className = className;
    this.value = "";
    this.disabled = false;
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
  "feature-preview-date", "feature-preview-known-at", "feature-preview-submit",
  "feature-preview-download", "feature-preview-status", "feature-preview-results",
  "feature-preview-decision", "feature-preview-summary", "feature-preview-identity",
  "feature-preview-columns", "feature-preview-refs", "feature-preview-limitations",
  "feature-preview-json",
];

const BTC = "crypto:btc-usd";
const AAPL = "equity:us:aapl";
const MARKET_ASSETS = {
  [BTC]: { symbol: "BTC", name: "Bitcoin USD", asset_class: "crypto" },
  [AAPL]: { symbol: "AAPL", name: "Apple Inc.", asset_class: "equity" },
};
const PREVIEW_ID = "3f6f1f0e-6d6a-5a6f-9a3e-2b1f0c9d8e7a";

function projection(overrides = {}) {
  return {
    schema_version: "research-feature-preview-v1",
    preview_id: PREVIEW_ID,
    workspace_uuid: "70c64668-1738-4dfe-ba1f-7e72026bb7fe",
    asset: { asset_id: BTC, source_id: "coinbase-exchange:btc-usd:daily-candles" },
    experiment_spec_id: "crypto-daily-positive-return-5d-v1",
    feature_set_version: "crypto-market-baseline-3-v1",
    request: { asset_id: BTC, decision_date: "2026-11-09", known_at: "2026-11-09T01:23:00Z" },
    decision_at: "2026-11-09T12:00:00Z",
    status: "features_available",
    sample_key: "846e0f2e-0d37-5f6d-b0f3-80b6f4bbf6b5",
    feature_hash: "c0d8f1e9-0f1f-5c7f-9d6a-7f3d2b1a0c9e",
    features: [
      { column_name: "close_return_1d", state: "available", value: "0.0123",
        as_of: "2026-11-08T00:00:00Z", available_at: "2026-11-09T00:01:00Z",
        algorithm_version: "predictive-close-ratio-1d-v1", unit: "ratio",
        quality: "valid", reasons: [] },
      { column_name: "rsi_14_fraction", state: "available", value: "0.55",
        as_of: "2026-11-08T00:00:00Z", available_at: "2026-11-09T00:02:00Z",
        computed_at: "2026-11-09T00:02:00Z",
        algorithm_version: "market-rsi-incremental-v2-decimal34",
        unit: "ratio", quality: "valid", reasons: [] },
      { column_name: "macd_12_26_9_histogram_close_ratio", state: "available", value: "0.002",
        as_of: "2026-11-08T00:00:00Z", available_at: "2026-11-09T00:03:00Z",
        algorithm_version: "market-macd-incremental-v2-decimal34", unit: "ratio",
        quality: "valid", reasons: [] },
    ],
    close_references: [
      { observation_id: "8b4f1d2e-3c5a-4e6f-8a9b-0c1d2e3f4a5b",
        period_start: "2026-11-07T00:00:00Z", period_end: "2026-11-08T00:00:00Z",
        available_at: "2026-11-08T01:00:00Z" },
      { observation_id: "9c5e2f3a-4b6c-5d7e-9f0a-1b2c3d4e5f6a",
        period_start: "2026-11-08T00:00:00Z", period_end: "2026-11-09T00:00:00Z",
        available_at: "2026-11-09T01:00:00Z" },
    ],
    limitations: [
      "Disponibilidad de inputs; no mide suficiencia de muestra ni capacidad predictiva.",
    ],
    ...overrides,
  };
}

function createContext({ assetId = BTC } = {}) {
  const technical = fs.readFileSync(process.argv[2], "utf8");
  const block = extractBlock(technical);
  const elements = new Map();
  for (const id of IDS) elements.set(id, new FakeElement("div"));
  const created = [];
  const document = {
    createElement: (tag, className) => {
      const element = new FakeElement(tag, className || "");
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
    let selectedMarketAsset = state.assetId;
    let marketAssets = state.marketAssets;
    const byId = (id) => document.getElementById(id);
    function createElement(tag, className, text) {
      const element = document.createElement(tag, className);
      if (text !== undefined && text !== null) element.textContent = text;
      return element;
    }
  `;
  const suffix = `
    ; return {
      featurePreviewQuery,
      featurePreviewRender,
      featurePreviewDownload,
      resetFeaturePreviewPanel,
      featurePreviewKey,
      setAsset(value) { selectedMarketAsset = value; },
    };
  `;
  const factory = new Function(
    "state", "document", "URL", "Blob", "api", prefix + block + suffix,
  );
  const context = factory(
    { assetId, marketAssets: MARKET_ASSETS }, document, URL, Blob, api,
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
function todayUtc() { return new Date().toISOString().slice(0, 10); }

async function scenarioDefaultsNeverFetch() {
  const ctx = createContext();
  assertEquals(ctx.apiCalls.length, 0, "no fetch on initialization");
  assertEquals(ctx.elements.get("feature-preview-date").value, todayUtc(), "current UTC date");
  const cut = ctx.elements.get("feature-preview-known-at").value;
  assertIncludes(cut, "Z", "visible UTC cut");
  const parsed = Date.parse(cut);
  if (!Number.isFinite(parsed)) throw new Error("default cut is not parseable");
  if (parsed > Date.now() + 1000) throw new Error("default cut is in the future");
  const noon = Date.parse(`${todayUtc()}T12:00:00Z`);
  if (parsed > noon) throw new Error("default cut exceeds the visible 12:00 UTC decision");
  assertEquals(ctx.hidden("feature-preview-results"), true, "no results before a query");
  return "defaults_never_fetch";
}

async function scenarioSubmitRendersProjection() {
  const ctx = createContext();
  const payload = projection();
  ctx.setApi(async () => payload);
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.apiCalls.length, 1, "one explicit fetch");
  const url = new URL("http://local" + ctx.apiCalls[0].path);
  assertEquals(url.pathname, "/api/v1/research/feature-preview", "route");
  assertEquals(url.searchParams.get("asset_id"), BTC, "asset");
  assertEquals(url.searchParams.get("decision_date"), "2026-11-09", "decision date");
  assertEquals(url.searchParams.get("known_at"), "2026-11-09T01:23:00Z", "cut");
  assertEquals(ctx.hidden("feature-preview-results"), false, "results visible");
  assertEquals(ctx.text("feature-preview-decision"), "Inputs disponibles", "state label");
  assertIncludes(ctx.text("feature-preview-summary"), "2026-11-09T12:00:00Z", "decision echo");
  assertIncludes(ctx.text("feature-preview-summary"), PREVIEW_ID, "preview identity");
  assertIncludes(ctx.text("feature-preview-columns"), "close_return_1d", "column identity");
  assertIncludes(ctx.text("feature-preview-columns"), "market-rsi-incremental-v2-decimal34",
    "version");
  assertIncludes(ctx.text("feature-preview-columns"), "0.0123", "exact decimal text");
  assertIncludes(ctx.text("feature-preview-refs"), "2026-11-08T01:00:00Z", "close availability");
  assertHides(ctx.text("feature-preview-limitations"), "research_ready", "no readiness status");
  const rendered = JSON.parse(ctx.elements.get("feature-preview-json").textContent);
  assertEquals(JSON.stringify(rendered), JSON.stringify(payload), "exact projection");
  assertEquals(ctx.elements.get("feature-preview-download").disabled, false, "download enabled");
  ctx.elements.get("feature-preview-download").dispatch("click", {});
  assertEquals(ctx.blobs.length, 1, "one blob");
  const anchor = ctx.created[ctx.created.length - 1];
  assertEquals(anchor.download, "feature-preview-" + PREVIEW_ID + ".json", "preview-based name");
  assertEquals(ctx.blobs[0].text(), JSON.stringify(payload), "byte-identical projection");
  return "submit_renders_projection";
}

async function scenarioInvalidInputNeverFetches() {
  const ctx = createContext();
  ctx.elements.get("feature-preview-date").value = "not-a-date";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.apiCalls.length, 0, "invalid date never fetches");
  assertIncludes(ctx.text("feature-preview-status"), "YYYY-MM-DD", "date reason");
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "not-a-cut";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.apiCalls.length, 0, "invalid cut never fetches");
  assertIncludes(ctx.text("feature-preview-status"), "ISO 8601", "cut reason");
  return "invalid_input";
}

async function scenarioIncompatibleAssetShowsReasonWithoutResidualData() {
  const ctx = createContext();
  ctx.setApi(async () => projection());
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.hidden("feature-preview-results"), false, "first render visible");
  ctx.context.setAsset(AAPL);
  ctx.context.resetFeaturePreviewPanel();
  const failure = new Error("El experimento registrado sólo admite cripto spot USD.");
  failure.code = "asset_incompatible";
  failure.status = 422;
  ctx.setApi(async () => { throw failure; });
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.hidden("feature-preview-results"), true, "results hidden on incompatibility");
  assertIncludes(ctx.text("feature-preview-status"), "cripto spot USD", "explicit reason");
  assertHides(ctx.text("feature-preview-summary"), PREVIEW_ID, "no residual BTC identity");
  assertEquals(ctx.elements.get("feature-preview-download").disabled, true, "download disabled");
  return "incompatible_asset";
}

async function scenarioLateResponseAndSelectionChangeInvalidate() {
  const ctx = createContext();
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  ctx.setApi(() => pending);
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  ctx.context.setAsset(AAPL);
  ctx.context.resetFeaturePreviewPanel();
  release(projection());
  await flush();
  assertEquals(ctx.hidden("feature-preview-results"), true, "late response hidden");
  assertHides(ctx.text("feature-preview-summary"), PREVIEW_ID, "late identity discarded");

  ctx.setApi(async () => projection());
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  assertEquals(ctx.hidden("feature-preview-results"), false, "render before change");
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T02:00:00Z";
  ctx.elements.get("feature-preview-known-at").dispatch("input", {});
  assertEquals(ctx.hidden("feature-preview-results"), true, "cut change clears");
  assertEquals(ctx.elements.get("feature-preview-download").disabled, true, "download cleared");
  assertIncludes(ctx.text("feature-preview-status"), "La selección cambió", "invalidation status");
  return "late_and_selection_change";
}

async function scenarioOversizedDownloadIsRefused() {
  const ctx = createContext();
  const payload = projection({ limitations: ["x".repeat(40000)] });
  ctx.setApi(async () => payload);
  ctx.elements.get("feature-preview-date").value = "2026-11-09";
  ctx.elements.get("feature-preview-known-at").value = "2026-11-09T01:23:00Z";
  ctx.elements.get("feature-preview-submit").dispatch("click", {});
  await flush();
  ctx.elements.get("feature-preview-download").dispatch("click", {});
  assertEquals(ctx.blobs.length, 0, "oversized projection is not downloaded");
  assertIncludes(ctx.text("feature-preview-status"), "32 KiB", "explicit limit");
  return "oversized_download";
}

(async () => {
  const results = [];
  for (const scenario of [
    scenarioDefaultsNeverFetch,
    scenarioSubmitRendersProjection,
    scenarioInvalidInputNeverFetches,
    scenarioIncompatibleAssetShowsReasonWithoutResidualData,
    scenarioLateResponseAndSelectionChangeInvalidate,
    scenarioOversizedDownloadIsRefused,
  ]) {
    results.push(await scenario());
  }
  console.log(JSON.stringify({ ok: true, scenarios: results }));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


def test_feature_preview_panel_behavior() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real JavaScript behavior harness")
    technical = Path(str(files("investment_analyst.frontend").joinpath("static/app-technical.js")))
    result = subprocess.run(
        [node, "-", str(technical)],
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
        "invalid_input",
        "incompatible_asset",
        "late_and_selection_change",
        "oversized_download",
    ]


def test_feature_preview_panel_static_contract() -> None:
    frontend = files("investment_analyst.frontend")
    technical = Path(str(frontend.joinpath("static/app-technical.js"))).read_text(encoding="utf-8")
    index = Path(str(frontend.joinpath("static/index.html"))).read_text(encoding="utf-8")
    start = technical.index("const FEATURE_PREVIEW_SCHEMA")
    end = technical.index("initializeFeaturePreviewPanel();", start)
    block = technical[start:end]
    assert "innerHTML" not in block
    assert "https://" not in block
    assert "localStorage" not in block
    assert "sessionStorage" not in block
    assert block.count("/api/v1/research/feature-preview?") == 1
    assert "AbortController" in block
    assert "URL.revokeObjectURL" in block
    assert "feature-preview-${featurePreviewPayload.preview_id}.json" in block
    assert 'id="feature-preview-title"' in index
    assert "Inputs antes de la decisión" in index
    assert "Disponibilidad de" in index
    assert "capacidad predictiva" in index
    assert index.count('id="feature-preview-date"') == 1
    assert index.count('id="feature-preview-known-at"') == 1
    assert index.count('id="feature-preview-submit"') == 1
    assert index.count('id="feature-preview-download"') == 1
