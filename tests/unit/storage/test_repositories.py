"""Tests for typed DuckDB repositories and deterministic filters."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_analyst.core.models import DiagnosticMode
from investment_analyst.storage import RecordConflictError

from .conftest import (
    make_asset,
    make_diagnostic_result,
    make_metric_definition,
    make_metric_result,
    make_observation,
    make_raw_record,
    make_source_definition,
)


def test_asset_and_source_definition_round_trip(storage) -> None:
    asset = make_asset()
    source = make_source_definition()

    storage.assets.upsert(asset)
    storage.sources.upsert(source)

    assert storage.assets.get(asset.asset_id) == asset
    assert storage.sources.get(source.source_id) == source


def test_observation_and_metric_definition_round_trip(storage) -> None:
    raw_record = make_raw_record()
    observation = make_observation(raw_record_id=raw_record.record_id)
    definition = make_metric_definition()

    storage.observations.save(observation)
    storage.metric_definitions.upsert(definition)

    recovered = storage.observations.get(observation.observation_id)
    assert recovered == observation
    assert recovered.value == Decimal("210.50")
    assert recovered.available_at.tzinfo is UTC
    assert storage.metric_definitions.get(definition.metric_key) == definition


def test_metric_and_diagnostic_round_trip(storage) -> None:
    raw_record = make_raw_record()
    observation = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=observation.observation_id)
    diagnostic = make_diagnostic_result(metric_result_id=metric.result_id)

    storage.metric_results.save(metric)
    storage.diagnostics.save(diagnostic)

    recovered_metric = storage.metric_results.get(metric.result_id)
    recovered_diagnostic = storage.diagnostics.get(diagnostic.diagnostic_id)
    assert recovered_metric == metric
    assert recovered_metric.input_observation_ids == [observation.observation_id]
    assert recovered_metric.value == Decimal("210.50")
    assert recovered_diagnostic == diagnostic
    assert recovered_diagnostic.final_score == Decimal("80")


def test_append_only_repositories_are_idempotent_and_detect_conflicts(storage) -> None:
    raw_record = make_raw_record()
    observation = make_observation(raw_record_id=raw_record.record_id)

    storage.observations.save(observation)
    storage.observations.save(observation)
    conflicting = observation.model_copy(update={"value": Decimal("999.00")})

    with pytest.raises(RecordConflictError, match="different content"):
        storage.observations.save(conflicting)


def test_filters_observations_metrics_and_diagnostics(storage) -> None:
    start = datetime(2026, 7, 10, 12, tzinfo=UTC)
    middle = start + timedelta(hours=1)
    end = start + timedelta(hours=2)
    raw_record = make_raw_record()

    observations = [
        make_observation(
            raw_record_id=raw_record.record_id,
            asset_id="asset:a",
            available_at=start,
        ),
        make_observation(
            raw_record_id=raw_record.record_id,
            asset_id="asset:a",
            available_at=middle,
        ),
        make_observation(
            raw_record_id=raw_record.record_id,
            asset_id="asset:b",
            available_at=end,
        ),
    ]
    for observation in observations:
        storage.observations.save(observation)

    metrics = [
        make_metric_result(
            observation_id=observations[0].observation_id,
            asset_id="asset:a",
            as_of=start,
            metric_key="metric:a",
        ),
        make_metric_result(
            observation_id=observations[1].observation_id,
            asset_id="asset:a",
            as_of=middle,
            metric_key="metric:a",
        ),
        make_metric_result(
            observation_id=observations[2].observation_id,
            asset_id="asset:b",
            as_of=end,
            metric_key="metric:b",
        ),
    ]
    for metric in metrics:
        storage.metric_results.save(metric)

    diagnostics = [
        make_diagnostic_result(
            metric_result_id=metrics[0].result_id,
            asset_id="asset:a",
            as_of=start,
            mode=DiagnosticMode.MARKET,
        ),
        make_diagnostic_result(
            metric_result_id=metrics[1].result_id,
            asset_id="asset:a",
            as_of=middle,
            mode=DiagnosticMode.MARKET,
        ),
        make_diagnostic_result(
            metric_result_id=metrics[2].result_id,
            asset_id="asset:b",
            as_of=end,
            mode=DiagnosticMode.FUNDAMENTAL,
        ),
    ]
    for diagnostic in diagnostics:
        storage.diagnostics.save(diagnostic)

    observation_ids = [
        item.observation_id
        for item in storage.observations.list(
            asset_id="asset:a",
            available_from=middle,
            available_to=end,
        )
    ]
    metric_ids = [
        item.result_id
        for item in storage.metric_results.list(
            asset_id="asset:a",
            metric_key="metric:a",
            as_of_from=start,
            as_of_to=middle,
        )
    ]
    diagnostic_ids = [
        item.diagnostic_id
        for item in storage.diagnostics.list(
            asset_id="asset:a",
            mode=DiagnosticMode.MARKET,
            as_of_from=start,
            as_of_to=middle,
        )
    ]

    assert observation_ids == [observations[1].observation_id]
    assert metric_ids == [metrics[0].result_id, metrics[1].result_id]
    assert diagnostic_ids == [diagnostics[0].diagnostic_id, diagnostics[1].diagnostic_id]
