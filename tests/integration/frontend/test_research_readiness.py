"""Socket-level readiness contract, cold/warm reuse and structural cost."""

from __future__ import annotations

import hashlib
import json
import resource
import threading
import time
import tracemalloc
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
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
from investment_analyst.research import readiness_service as readiness_module
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_models import (
    PredictiveDatasetRequest,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.research.readiness_models import MAX_READINESS_VIEW_BYTES
from investment_analyst.storage.repositories import DuckDBMetricResultRepository
from investment_analyst.storage.workspace_v2_repositories import WorkspaceV2MetricResultRepository
from investment_analyst.workspace.models import (
    WorkspaceAccessMode,
    WorkspaceInitialization,
)
from investment_analyst.workspace.service import WorkspaceService

_BTC = "crypto:btc-usd"
_BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_SOL = "crypto:sol-usd"
_SOL_SOURCE = "coinbase-exchange:sol-usd:daily-candles"
_DECISION_START = date(2026, 6, 1)
_DECISION_END = date(2026, 6, 10)
_KNOWN_AT = datetime(2026, 6, 20, 3, 0, tzinfo=UTC)
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
            f"readiness-raw|{asset_id}|{source_id}|{day.isoformat()}|{revision}",
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
        schema_version="readiness-integration-v1",
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
        result_id=uuid5(NAMESPACE_URL, f"readiness-metric|{seed}"),
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


def _get(root: str, parameters: dict[str, str]) -> tuple[int, dict[str, object]]:
    request = Request(f"{root}/api/v1/research/readiness?{urlencode(parameters)}")
    try:
        response = urlopen(request, timeout=60)
    except HTTPError as error:
        return error.code, json.loads(error.read())
    with response:
        return response.status, json.loads(response.read())


def _parameters(
    *,
    asset_id: str = _BTC,
    start: date = _DECISION_START,
    end: date = _DECISION_END,
    known_at: datetime = _KNOWN_AT,
) -> dict[str, str]:
    return {
        "asset_id": asset_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "known_at": known_at.isoformat(),
    }


def _seed_workspace(tmp_path: Path) -> tuple[WorkspaceService, WorkspaceInitialization]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace", format_version=2)
    close_days = _days(_DECISION_START - timedelta(days=2), _DECISION_END + timedelta(days=5))
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
            days=_days(_DECISION_START - timedelta(days=1), _DECISION_END),
            start_value=200,
            suffix="sol-1x",
        )
    finally:
        writer.close()
    return service, initialization


def test_readiness_http_contract_cold_warm_and_foreign_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, initialization = _seed_workspace(tmp_path)
    root = initialization.paths.root
    builds: list[int] = []
    hydrated: list[MetricResult] = []
    pages: list[int] = []

    class _CountingBuilder(PredictiveDatasetBuilder):
        def build(self, request: PredictiveDatasetRequest) -> object:
            builds.append(1)
            return super().build(request)

    monkeypatch.setattr(readiness_module, "PredictiveDatasetBuilder", _CountingBuilder)
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

    with _server(_application(root)) as base:
        tracemalloc.start()
        started = time.perf_counter()
        status, cold = _get(base, _parameters())
        cold_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert status == 200, cold
        assert len(builds) == 1
        cold_bytes = len(json.dumps(cold, separators=(",", ":")).encode("utf-8"))
        assert cold_bytes <= MAX_READINESS_VIEW_BYTES
        assert hydrated, "the cold read hydrates the metric evidence it projects"
        assert {metric.asset_id for metric in hydrated} == {_BTC}
        assert {metric.parameters.get("source_id") for metric in hydrated} == {_BTC_SOURCE}
        assert pages and all(0 < size <= 256 for size in pages)

        started = time.perf_counter()
        warm_status, warm = _get(base, _parameters())
        warm_seconds = time.perf_counter() - started
        assert warm_status == 200
        assert warm == cold
        assert len(builds) == 1, "a valid hit must not rebuild the dataset"
        assert warm_seconds <= max(cold_seconds * 5, 1.0)

    summary = cast(dict[str, object], cold["summary"])
    request_payload = cast(dict[str, object], summary["request"])
    assert cold["schema_version"] == "research-pit-readiness-view-v1"
    assert request_payload["asset_id"] == _BTC
    assert request_payload["start_date"] == _DECISION_START.isoformat()
    assert request_payload["end_date"] == _DECISION_END.isoformat()
    assert (
        datetime.fromisoformat(str(request_payload["dataset_known_at"]).replace("Z", "+00:00"))
        == _KNOWN_AT
    )
    counts = cast(dict[str, object], summary["counts"])
    assert counts["requested"] == len(_days(_DECISION_START, _DECISION_END))
    for split in cast(list[dict[str, object]], summary["splits"]):
        if split["split"] != "train":
            assert split["positive"] is None
            assert split["negative"] is None

    writer = service.open_storage(initialization.paths, WorkspaceAccessMode.READ_WRITE)
    try:
        for index in range(9):
            _seed_series(
                writer,
                asset_id=_SOL,
                source_id=_SOL_SOURCE,
                days=_days(_DECISION_START - timedelta(days=1), _DECISION_END),
                start_value=300 + index * 100,
                suffix=f"sol-10x-{index}",
            )
    finally:
        writer.close()

    builds.clear()
    hydrated.clear()
    pages.clear()
    before = _tree_state(root)
    bytes_before = _db_bytes(root)
    with _server(_application(root)) as base:
        # A fresh controller starts with an empty read cache: the projection must
        # be rebuilt from the 10x foreign corpus and still match exactly.
        started = time.perf_counter()
        status, after_foreign = _get(base, _parameters())
        foreign_seconds = time.perf_counter() - started
        assert status == 200, after_foreign
        assert len(builds) == 1
        assert after_foreign == cold
        assert hydrated and {metric.asset_id for metric in hydrated} == {_BTC}
        assert pages and all(0 < size <= 256 for size in pages)

    after = _tree_state(root)
    evidence = {
        "cold_seconds": round(cold_seconds, 4),
        "warm_seconds": round(warm_seconds, 4),
        "foreign_corpus_seconds": round(foreign_seconds, 4),
        "response_bytes": cold_bytes,
        "tracemalloc_peak_bytes": peak,
        "db_bytes_delta": _db_bytes(root) - bytes_before,
        "max_rss_growth_bytes": max(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before, 0
        )
        * 1024,
    }
    assert evidence["cold_seconds"] > 0
    assert evidence["warm_seconds"] > 0
    assert evidence["foreign_corpus_seconds"] > 0
    assert evidence["response_bytes"] <= MAX_READINESS_VIEW_BYTES
    assert evidence["tracemalloc_peak_bytes"] > 0
    assert evidence["db_bytes_delta"] == 0
    assert after == before, "a readiness read must not write the workspace"


def test_readiness_http_rejects_invalid_parameters_and_incompatible_assets(
    tmp_path: Path,
) -> None:
    _, initialization = _seed_workspace(tmp_path)
    with _server(_application(initialization.paths.root)) as base:
        status, payload = _get(base, _parameters(asset_id="equity:us:aapl"))
        assert status == 422
        error = cast(dict[str, object], payload["error"])
        assert error["code"] == "asset_incompatible"
        assert "crypto spot" in str(error["message"])

        status, payload = _get(base, {**_parameters(), "unknown": "1"})
        assert status == 400
        assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

        status, payload = _get(
            base, {key: value for key, value in _parameters().items() if key != "known_at"}
        )
        assert status == 400
        assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

        status, _ = _get(base, _parameters(end=_DECISION_START - timedelta(days=1)))
        assert status == 400

        status, _ = _get(
            base,
            _parameters(
                start=_DECISION_START,
                end=_DECISION_START + timedelta(days=366),
                known_at=datetime(2028, 1, 1, tzinfo=UTC),
            ),
        )
        assert status == 400

        status, _ = _get(
            base,
            _parameters(known_at=decision_instant(_DECISION_END) - timedelta(seconds=1)),
        )
        assert status == 400

        status, payload = _get(base, _parameters(asset_id="unknown:asset"))
        assert status == 400
        assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"


def test_readiness_cut_excludes_later_evidence(tmp_path: Path) -> None:
    _, initialization = _seed_workspace(tmp_path)
    decision_cut = decision_instant(_DECISION_START)
    request = PredictiveDatasetRequest(
        asset_id=_BTC,
        start_date=_DECISION_START,
        end_date=_DECISION_START,
        dataset_known_at=decision_cut,
    )
    runtime = ApplicationRuntime.create_default()
    with runtime.open_storage(
        StorageLocationRequest(workspace=initialization.paths.root),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        dataset = PredictiveDatasetBuilder(
            storage,
            provider_resolver=runtime.provider_resolver,
            catalog=runtime.catalog,
        ).build(request)
    sample = dataset.samples[0]
    assert dataset.summary.request.dataset_known_at == decision_cut
    assert dataset.summary.counts.feature_available == 1
    assert dataset.summary.counts.label_not_yet_available == 1
    assert sample.label.state.value == "not_yet_available"
    assert sample.label.label is None
    assert all(
        feature.available_at is not None and feature.available_at <= decision_cut
        for feature in sample.features
    )
