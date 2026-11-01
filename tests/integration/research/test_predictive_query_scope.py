"""Integration scope, pagination and read-only cost of the predictive dataset."""

from __future__ import annotations

import hashlib
import time
import tracemalloc
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_models import (
    MAX_PAGE_SIZE,
    PredictiveDatasetRequest,
    bar_start_instant,
    decision_instant,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_BTC = "crypto:btc-usd"
_BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_SOL = "crypto:sol-usd"
_SOL_SOURCE = "coinbase-exchange:sol-usd:daily-candles"
_START = date(2026, 6, 1)
_END = date(2026, 8, 29)
_KNOWN_AT = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)
_RSI_VERSION = "market-rsi-incremental-v2-decimal34"
_MACD_VERSION = "market-macd-incremental-v2-decimal34"


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
            f"predictive-raw|{asset_id}|{source_id}|{day.isoformat()}|{revision}",
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
        schema_version="predictive-integration-v1",
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
        result_id=uuid5(NAMESPACE_URL, f"predictive-metric|{seed}"),
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
    with_metrics: bool = True,
) -> tuple[list[RawRecord], list[NormalizedObservation], list[MetricResult]]:
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
    writer.raw_records.save_many(raws)
    writer.observations.save_many(observations)
    if with_metrics:
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
        writer.metric_results.save_many(metrics)
    return raws, observations, metrics


def _tree_state(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _decision_days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _build(
    service: WorkspaceService,
    root: Path,
    request: PredictiveDatasetRequest,
) -> tuple[object, list[MetricResult], list[int]]:
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    hydrated: list[MetricResult] = []
    pages: list[int] = []
    location = StorageLocationRequest(workspace=root)
    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_ONLY) as storage:
        repository = storage.metric_results
        original_get_many = repository.get_many
        original_page = repository.list_metric_index_page

        def recording_get_many(identifiers: tuple[UUID, ...]) -> dict[UUID, MetricResult]:
            result = original_get_many(identifiers)
            hydrated.extend(result.values())
            return result

        def recording_page(query: object) -> tuple[object, ...]:
            result = original_page(query)
            pages.append(len(result))
            return result

        repository.get_many = recording_get_many  # type: ignore[method-assign]
        repository.list_metric_index_page = recording_page  # type: ignore[method-assign]
        dataset = PredictiveDatasetBuilder(
            storage,
            provider_resolver=runtime.provider_resolver,
            catalog=runtime.catalog,
        ).build(request)
    return dataset, hydrated, pages


def test_foreign_corpus_never_changes_identity_or_hydration(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace")
    decisions = _decision_days(date(2026, 6, 1), date(2026, 6, 10))
    close_days = _decision_days(
        decisions[0] - timedelta(days=2),
        decisions[-1] + timedelta(days=5),
    )
    foreign_days = _decision_days(
        decisions[0] - timedelta(days=1),
        decisions[-1],
    )
    writer = service.open_storage(
        initialization.paths,
        WorkspaceAccessMode.READ_WRITE,
    )
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
            days=foreign_days,
            start_value=200,
            suffix="sol-1x",
        )
    finally:
        writer.close()

    request = PredictiveDatasetRequest(
        asset_id=_BTC,
        start_date=decisions[0],
        end_date=decisions[-1],
        dataset_known_at=_KNOWN_AT,
    )
    first, hydrated, pages = _build(service, tmp_path / "workspace", request)
    assert first.summary.counts.requested == 10
    assert hydrated
    assert all(metric.asset_id == _BTC for metric in hydrated)
    assert all(metric.parameters.get("source_id") == _BTC_SOURCE for metric in hydrated)
    assert all(0 < size <= MAX_PAGE_SIZE for size in pages)

    writer = service.open_storage(
        initialization.paths,
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        for index in range(9):
            _seed_series(
                writer,
                asset_id=_SOL,
                source_id=_SOL_SOURCE,
                days=foreign_days,
                start_value=300 + index * 100,
                suffix=f"sol-10x-{index}",
            )
    finally:
        writer.close()

    before = _tree_state(initialization.paths.root)
    tracemalloc.start()
    started = time.perf_counter()
    second, hydrated_second, pages_second = _build(
        service,
        tmp_path / "workspace",
        request,
    )
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after = _tree_state(initialization.paths.root)

    assert first.dataset_id == second.dataset_id
    assert first.dataset_content_sha256 == second.dataset_content_sha256
    assert first.samples == second.samples
    assert hydrated_second
    assert all(metric.asset_id == _BTC for metric in hydrated_second)
    assert all(0 < size <= MAX_PAGE_SIZE for size in pages_second)
    assert before == after
    assert elapsed < 60
    assert peak > 0


def test_query_count_does_not_grow_with_requested_decisions(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace")
    all_decisions = _decision_days(_START, _END)
    close_days = _decision_days(
        all_decisions[0] - timedelta(days=2),
        all_decisions[-1] + timedelta(days=5),
    )
    writer = service.open_storage(
        initialization.paths,
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        _seed_series(
            writer,
            asset_id=_BTC,
            source_id=_BTC_SOURCE,
            days=close_days,
            start_value=100,
            suffix="btc",
        )
    finally:
        writer.close()

    short = PredictiveDatasetRequest(
        asset_id=_BTC,
        start_date=all_decisions[0],
        end_date=all_decisions[9],
        dataset_known_at=_KNOWN_AT,
    )
    long = PredictiveDatasetRequest(
        asset_id=_BTC,
        start_date=all_decisions[0],
        end_date=all_decisions[-1],
        dataset_known_at=_KNOWN_AT,
    )
    short_dataset, _, short_pages = _build(service, tmp_path / "workspace", short)
    long_dataset, _, long_pages = _build(service, tmp_path / "workspace", long)
    assert short_dataset.summary.counts.requested == 10
    assert long_dataset.summary.counts.requested == len(all_decisions)
    assert short_pages == [20]
    assert long_pages == [len(all_decisions) * 2]


def test_reads_every_metric_page_without_truncating_revisions(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace")
    decision = date(2026, 8, 10)
    close_days = _decision_days(decision - timedelta(days=2), decision + timedelta(days=5))
    period = decision - timedelta(days=1)
    writer = service.open_storage(
        initialization.paths,
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        _, observations, _ = _seed_series(
            writer,
            asset_id=_BTC,
            source_id=_BTC_SOURCE,
            days=close_days,
            start_value=100,
            suffix="btc",
            with_metrics=False,
        )
        observation = next(
            item for item in observations if item.observed_at == bar_start_instant(period)
        )
        revision_base = decision_instant(decision) - timedelta(hours=11)
        metrics = [
            _metric(
                asset_id=_BTC,
                source_id=_BTC_SOURCE,
                day=period,
                key="market.technical.rsi",
                value=Decimal(index),
                available_at=revision_base + timedelta(minutes=index),
                input_observation_id=observation.observation_id,
                seed=f"rsi-revision-{index}",
                version=_RSI_VERSION,
                unit="index",
                parameters={"window": 14},
            )
            for index in range(300)
        ]
        metrics.append(
            _metric(
                asset_id=_BTC,
                source_id=_BTC_SOURCE,
                day=period,
                key="market.technical.macd.histogram",
                value=Decimal("2"),
                available_at=revision_base,
                input_observation_id=observation.observation_id,
                seed="macd-revision",
                version=_MACD_VERSION,
                unit="USD",
                parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
            )
        )
        writer.metric_results.save_many(metrics)
        _seed_series(
            writer,
            asset_id=_SOL,
            source_id=_SOL_SOURCE,
            days=_decision_days(period, period),
            start_value=900,
            suffix="sol-foreign",
        )
    finally:
        writer.close()

    request = PredictiveDatasetRequest(
        asset_id=_BTC,
        start_date=decision,
        end_date=decision,
        dataset_known_at=_KNOWN_AT,
    )
    dataset, hydrated, pages = _build(service, tmp_path / "workspace", request)
    assert pages == [256, 45]
    assert len(hydrated) == 301
    assert all(metric.asset_id == _BTC for metric in hydrated)
    sample = dataset.samples[0]
    assert sample.features[1].value == Decimal("299") / Decimal(100)
    assert sample.features[1].metric_result_id is not None
    assert sample.feature_state.value == "available"
