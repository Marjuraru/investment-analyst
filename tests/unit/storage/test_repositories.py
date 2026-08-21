"""Tests for typed DuckDB repositories and deterministic filters."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_analyst.core.models import DataFrequency, DiagnosticMode
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


def test_metric_result_round_trip_preserves_derived_lineage(storage) -> None:
    raw_record = make_raw_record()
    observation = make_observation(raw_record_id=raw_record.record_id)
    seed = make_metric_result(observation_id=observation.observation_id)
    derived = make_metric_result(
        observation_id=observation.observation_id,
        as_of=seed.as_of + timedelta(days=1),
    ).model_copy(
        update={
            "input_metric_result_ids": [seed.result_id],
            "available_at": seed.available_at,
        }
    )

    storage.metric_results.save(seed)
    storage.metric_results.save(derived)

    assert storage.metric_results.get(derived.result_id).input_metric_result_ids == [seed.result_id]


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


def test_observation_filters_include_frequency_and_half_open_observed_range(storage) -> None:
    observed_start = datetime(2026, 7, 9, tzinfo=UTC)
    observed_end = datetime(2026, 7, 11, tzinfo=UTC)
    raw_record = make_raw_record()
    observations = [
        make_observation(
            raw_record_id=raw_record.record_id,
            observed_at=observed_start - timedelta(seconds=1),
        ),
        make_observation(
            raw_record_id=raw_record.record_id,
            observed_at=observed_start,
        ),
        make_observation(
            raw_record_id=raw_record.record_id,
            observed_at=observed_end - timedelta(seconds=1),
        ),
        make_observation(
            raw_record_id=raw_record.record_id,
            observed_at=observed_end,
        ),
        make_observation(
            raw_record_id=raw_record.record_id,
            frequency=DataFrequency.HOUR_1,
            observed_at=observed_start,
        ),
    ]
    for observation in observations:
        storage.observations.save(observation)

    result = storage.observations.list(
        frequency=DataFrequency.DAY_1,
        observed_from=observed_start,
        observed_before=observed_end,
    )

    assert {item.observation_id for item in result} == {
        observations[1].observation_id,
        observations[2].observation_id,
    }


def test_observation_pushdown_filters_field_names_quality_and_period_range(storage) -> None:
    raw_record = make_raw_record()
    period_1 = datetime(2025, 9, 27, tzinfo=UTC)
    period_2 = datetime(2025, 12, 31, tzinfo=UTC)
    period_3 = datetime(2026, 3, 31, tzinfo=UTC)

    obs_rev = make_observation(
        raw_record_id=raw_record.record_id,
    ).model_copy(
        update={
            "field_name": "fundamental.revenue",
            "period_end": period_1,
        }
    )
    obs_inc = make_observation(
        raw_record_id=raw_record.record_id,
    ).model_copy(
        update={
            "field_name": "fundamental.net_income",
            "period_end": period_2,
        }
    )
    obs_assets = make_observation(
        raw_record_id=raw_record.record_id,
    ).model_copy(
        update={
            "field_name": "fundamental.assets",
            "period_end": period_3,
        }
    )
    for obs in (obs_rev, obs_inc, obs_assets):
        storage.observations.save(obs)

    # Filter by field_names
    res_fields = storage.observations.list(
        field_names=["fundamental.revenue", "fundamental.assets"],
    )
    assert {item.observation_id for item in res_fields} == {
        obs_rev.observation_id,
        obs_assets.observation_id,
    }

    # Filter by period_end range
    res_period = storage.observations.list(
        period_end_from=period_1.date(),
        period_end_to=period_2.date(),
    )
    assert {item.observation_id for item in res_period} == {
        obs_rev.observation_id,
        obs_inc.observation_id,
    }

    # Empty field_names returns empty list
    assert storage.observations.list(field_names=[]) == []


def test_observation_count_and_minimum_available_at(storage) -> None:
    raw_record = make_raw_record()
    t1 = datetime(2026, 7, 10, 10, tzinfo=UTC)
    t2 = datetime(2026, 7, 10, 12, tzinfo=UTC)

    obs1 = make_observation(
        raw_record_id=raw_record.record_id,
        asset_id="asset:count_test",
        available_at=t2,
    )
    obs2 = make_observation(
        raw_record_id=raw_record.record_id,
        asset_id="asset:count_test",
        available_at=t1,
    )
    storage.observations.save(obs1)
    storage.observations.save(obs2)

    assert storage.observations.count(asset_id="asset:count_test") == 2
    assert storage.observations.count(asset_id="asset:nonexistent") == 0

    min_avail = storage.observations.minimum_available_at(asset_id="asset:count_test")
    assert min_avail == t1

    assert storage.observations.minimum_available_at(asset_id="asset:nonexistent") is None


def test_observation_edges_and_source_filter_are_sql_aggregates(storage) -> None:
    raw_record = make_raw_record()
    first_at = datetime(2026, 7, 9, 16, tzinfo=UTC)
    last_at = datetime(2026, 7, 11, 16, tzinfo=UTC)
    first = make_observation(
        raw_record_id=raw_record.record_id,
        asset_id="asset:edges",
        observed_at=first_at,
        available_at=first_at + timedelta(hours=1),
    )
    last = make_observation(
        raw_record_id=raw_record.record_id,
        asset_id="asset:edges",
        observed_at=last_at,
        available_at=datetime(2026, 7, 10, 16, 3, tzinfo=UTC),
    )
    foreign = make_observation(
        raw_record_id=raw_record.record_id,
        asset_id="asset:edges",
        observed_at=datetime(2026, 7, 12, 16, tzinfo=UTC),
        available_at=datetime(2026, 7, 10, 16, 3, tzinfo=UTC),
    ).model_copy(
        update={
            "source": first.source.model_copy(update={"source_id": "other:source"}),
        }
    )
    for observation in (first, last, foreign):
        storage.observations.save(observation)

    assert (
        storage.observations.count(
            asset_id="asset:edges",
            source_id="alpaca:bars",
            frequency=DataFrequency.DAY_1,
        )
        == 2
    )
    assert storage.observations.observed_at_bounds(
        asset_id="asset:edges",
        source_id="alpaca:bars",
        frequency=DataFrequency.DAY_1,
    ) == (first_at, last_at)
    assert storage.observations.maximum_available_at(
        asset_id="asset:edges",
        source_id="alpaca:bars",
    ) == datetime(2026, 7, 10, 16, 3, tzinfo=UTC)
    assert storage.observations.observed_at_bounds(asset_id="asset:missing") == (None, None)


def test_metric_and_diagnostic_counts(storage) -> None:
    raw_record = make_raw_record()
    observation = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=observation.observation_id, asset_id="asset:c")
    diagnostic = make_diagnostic_result(
        metric_result_id=metric.result_id,
        asset_id="asset:c",
        mode=DiagnosticMode.MARKET,
    )

    storage.metric_results.save(metric)
    storage.diagnostics.save(diagnostic)

    assert storage.metric_results.count(asset_id="asset:c") == 1
    assert storage.metric_results.count(asset_id="asset:other") == 0
    assert storage.diagnostics.count(asset_id="asset:c", mode=DiagnosticMode.MARKET) == 1
    assert storage.diagnostics.count(asset_id="asset:c", mode=DiagnosticMode.FUNDAMENTAL) == 0
