"""Tests for typed DuckDB repositories and deterministic filters."""

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import DataFrequency, DiagnosticMode
from investment_analyst.storage import (
    LocalStorage,
    RecordConflictError,
    RecordNotFoundError,
    StoragePaths,
)

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


def test_metric_result_key_projection_preserves_order_and_count(storage) -> None:
    observation = make_observation(raw_record_id=make_raw_record().record_id)
    start = datetime(2026, 7, 10, tzinfo=UTC)
    results = (
        make_metric_result(
            observation_id=observation.observation_id,
            result_id=UUID("90000000-0000-4000-8000-000000000003"),
            asset_id="asset:scope",
            as_of=start,
            metric_key="metric:b",
        ),
        make_metric_result(
            observation_id=observation.observation_id,
            result_id=UUID("90000000-0000-4000-8000-000000000001"),
            asset_id="asset:scope",
            as_of=start + timedelta(days=1),
            metric_key="metric:a",
        ),
        make_metric_result(
            observation_id=observation.observation_id,
            result_id=UUID("90000000-0000-4000-8000-000000000002"),
            asset_id="asset:scope",
            as_of=start + timedelta(days=2),
            metric_key="metric:outside",
        ),
    )
    for result in results:
        storage.metric_results.save(result)

    unbounded = storage.metric_results.list(asset_id="asset:scope")
    bounded = storage.metric_results.list(
        asset_id="asset:scope",
        metric_keys=("metric:a", "metric:b", "metric:a"),
    )

    assert bounded == unbounded[:2]
    assert (
        storage.metric_results.count(
            asset_id="asset:scope",
            metric_keys=("metric:a", "metric:b"),
        )
        == 2
    )
    assert storage.metric_results.list(
        asset_id="asset:scope",
        metric_keys=("metric:a",),
    ) == storage.metric_results.list(asset_id="asset:scope", metric_key="metric:a")

    clauses, parameters = storage.metric_results._build_filter_clauses(
        metric_keys=("metric'injection", "metric:a"),
    )
    assert clauses == ["metric_key IN (?, ?)"]
    assert parameters == ["metric'injection", "metric:a"]


@pytest.mark.parametrize("metric_keys", [(), ("",), ("   ",)])
def test_metric_result_key_projection_rejects_empty_or_blank_keys(storage, metric_keys) -> None:
    with pytest.raises(ValueError, match="metric_keys"):
        storage.metric_results.list(metric_keys=metric_keys)
    with pytest.raises(ValueError, match="metric_keys"):
        storage.metric_results.count(metric_keys=metric_keys)


def test_metric_result_key_projection_rejects_conflicting_singular_filter(storage) -> None:
    with pytest.raises(ValueError, match="metric_key and metric_keys"):
        storage.metric_results.list(
            metric_key="metric:a",
            metric_keys=("metric:a",),
        )
    with pytest.raises(ValueError, match="metric_key and metric_keys"):
        storage.metric_results.count(
            metric_key="metric:a",
            metric_keys=("metric:a",),
        )


def test_batch_read_contract_matches_raw_record_shape(storage) -> None:
    raw_sig = inspect.signature(storage.raw_records.get_many)
    raw_params = list(raw_sig.parameters.keys())
    assert len(raw_params) == 1

    obs_sig = inspect.signature(storage.observations.get_many)
    metric_sig = inspect.signature(storage.metric_results.get_many)
    diag_sig = inspect.signature(storage.diagnostics.get_many)

    assert len(obs_sig.parameters) == 1
    assert len(metric_sig.parameters) == 1
    assert len(diag_sig.parameters) == 1

    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)
    obs = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=obs.observation_id)
    diag = make_diagnostic_result(metric_result_id=metric.result_id)

    storage.observations.save_many([obs])
    storage.metric_results.save_many([metric])
    storage.diagnostics.save_many([diag])

    raw_res = storage.raw_records.get_many([raw_record.record_id])
    obs_res = storage.observations.get_many([obs.observation_id])
    metric_res = storage.metric_results.get_many([metric.result_id])
    diag_res = storage.diagnostics.get_many([diag.diagnostic_id])

    assert isinstance(raw_res, dict)
    assert isinstance(obs_res, dict)
    assert isinstance(metric_res, dict)
    assert isinstance(diag_res, dict)

    assert obs_res[obs.observation_id] == obs
    assert metric_res[metric.result_id] == metric
    assert diag_res[diag.diagnostic_id] == diag


class _QueryCountingConnection:
    def __init__(self, target) -> None:
        self._target = target
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._target.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self.execute_count += 1
        return self._target.executemany(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def test_batch_read_uses_a_bounded_query_count(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    observations = [
        make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())
        for _ in range(10)
    ]
    metrics = [
        make_metric_result(observation_id=obs.observation_id, result_id=uuid4())
        for obs in observations
    ]
    diagnostics = [
        make_diagnostic_result(metric_result_id=metric.result_id, diagnostic_id=uuid4())
        for metric in metrics
    ]

    storage.observations.save_many(observations)
    storage.metric_results.save_many(metrics)
    storage.diagnostics.save_many(diagnostics)

    wrapper = _QueryCountingConnection(storage.observations._connection)
    storage.observations._connection = wrapper
    storage.metric_results._connection = wrapper
    storage.diagnostics._connection = wrapper

    wrapper.execute_count = 0
    obs_ids = [obs.observation_id for obs in observations]
    res_obs = storage.observations.get_many(obs_ids)
    assert len(res_obs) == 10
    assert wrapper.execute_count == 1

    wrapper.execute_count = 0
    metric_ids = [m.result_id for m in metrics]
    res_metrics = storage.metric_results.get_many(metric_ids)
    assert len(res_metrics) == 10
    assert wrapper.execute_count == 1

    wrapper.execute_count = 0
    diag_ids = [d.diagnostic_id for d in diagnostics]
    res_diags = storage.diagnostics.get_many(diag_ids)
    assert len(res_diags) == 10
    assert wrapper.execute_count == 1

    # Bounded query count independent of set size: querying 3 items also takes 1 query
    wrapper.execute_count = 0
    res_subset = storage.observations.get_many(obs_ids[:3])
    assert len(res_subset) == 3
    assert wrapper.execute_count == 1


def test_batch_read_matches_per_row_get_and_declares_absence(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    obs = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=obs.observation_id)
    diag = make_diagnostic_result(metric_result_id=metric.result_id)

    storage.observations.save_many([obs])
    storage.metric_results.save_many([metric])
    storage.diagnostics.save_many([diag])

    assert storage.observations.get_many([obs.observation_id])[
        obs.observation_id
    ] == storage.observations.get(obs.observation_id)
    assert storage.metric_results.get_many([metric.result_id])[
        metric.result_id
    ] == storage.metric_results.get(metric.result_id)
    assert storage.diagnostics.get_many([diag.diagnostic_id])[
        diag.diagnostic_id
    ] == storage.diagnostics.get(diag.diagnostic_id)

    absent_obs_id = uuid4()
    with pytest.raises(RecordNotFoundError):
        storage.observations.get(absent_obs_id)
    with pytest.raises(RecordNotFoundError):
        storage.observations.get_many([absent_obs_id])

    absent_metric_id = uuid4()
    with pytest.raises(RecordNotFoundError):
        storage.metric_results.get(absent_metric_id)
    with pytest.raises(RecordNotFoundError):
        storage.metric_results.get_many([absent_metric_id])

    absent_diag_id = uuid4()
    with pytest.raises(RecordNotFoundError):
        storage.diagnostics.get(absent_diag_id)
    with pytest.raises(RecordNotFoundError):
        storage.diagnostics.get_many([absent_diag_id])


def test_batch_write_preserves_every_validation_and_identity(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    obs1 = make_observation(
        raw_record_id=raw_record.record_id, observation_id=uuid4(), value=Decimal("100.25")
    )
    obs2 = make_observation(
        raw_record_id=raw_record.record_id, observation_id=uuid4(), value=Decimal("200.75")
    )

    receipt = storage.observations.save_many([obs1, obs2])
    assert receipt.created_count == 2
    assert receipt.created_ids == (obs1.observation_id, obs2.observation_id)

    recovered1 = storage.observations.get(obs1.observation_id)
    recovered2 = storage.observations.get(obs2.observation_id)

    assert recovered1 == obs1
    assert recovered2 == obs2
    assert recovered1.value == Decimal("100.25")
    assert recovered2.value == Decimal("200.75")
    assert recovered1.observation_id == obs1.observation_id
    assert recovered2.observation_id == obs2.observation_id


def test_in_memory_conflict_detection_fails_before_touching_the_engine(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    shared_id = uuid4()
    obs_a = make_observation(
        raw_record_id=raw_record.record_id, observation_id=shared_id, value=Decimal("100.00")
    )
    obs_b = make_observation(
        raw_record_id=raw_record.record_id, observation_id=shared_id, value=Decimal("999.00")
    )

    wrapper = _QueryCountingConnection(storage.observations._connection)
    storage.observations._connection = wrapper

    with pytest.raises(RecordConflictError, match="different content"):
        storage.observations.save_many([obs_a, obs_b])

    assert wrapper.execute_count == 0


def test_batch_write_returns_typed_receipt_of_created_reused_and_conflicting(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    obs1 = make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())
    obs2 = make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())
    obs3 = make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())

    r1 = storage.observations.save_many([obs1, obs2])
    assert isinstance(r1, BatchWriteReceipt)
    assert r1.created_ids == (obs1.observation_id, obs2.observation_id)
    assert r1.reused_ids == ()
    assert r1.conflicting_ids == ()
    assert r1.created == (obs1.observation_id, obs2.observation_id)
    assert r1.reused == ()
    assert r1.conflicting == ()
    assert r1.created_count == 2
    assert r1.reused_count == 0
    assert r1.conflicting_count == 0
    assert r1.total_count == 2

    r2 = storage.observations.save_many([obs1, obs3])
    assert isinstance(r2, BatchWriteReceipt)
    assert r2.created_ids == (obs3.observation_id,)
    assert r2.reused_ids == (obs1.observation_id,)
    assert r2.conflicting_ids == ()
    assert r2.created_count == 1
    assert r2.reused_count == 1
    assert r2.conflicting_count == 0
    assert r2.total_count == 2

    c_id = uuid4()
    custom_receipt = BatchWriteReceipt(
        created_ids=(obs1.observation_id,),
        reused_ids=(obs2.observation_id,),
        conflicting_ids=(c_id,),
    )
    assert custom_receipt.conflicting == (c_id,)
    assert custom_receipt.conflicting_count == 1
    assert custom_receipt.total_count == 3


def test_failed_batch_preserves_previously_committed_batches(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    obs1 = make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())
    obs2 = make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())
    obs3 = make_observation(raw_record_id=raw_record.record_id, observation_id=uuid4())

    r1 = storage.observations.save_many([obs1, obs2])
    assert r1.created_count == 2
    assert storage.observations.get(obs1.observation_id) == obs1
    assert storage.observations.get(obs2.observation_id) == obs2

    conflicting_obs1 = obs1.model_copy(update={"value": Decimal("9999.99")})
    with pytest.raises(RecordConflictError):
        storage.observations.save_many([obs3, conflicting_obs1])

    assert storage.observations.get(obs1.observation_id) == obs1
    assert storage.observations.get(obs2.observation_id) == obs2
    with pytest.raises(RecordNotFoundError):
        storage.observations.get(obs3.observation_id)


def test_batch_path_issues_at_least_ten_times_fewer_queries(tmp_path) -> None:
    raw_record = make_raw_record()
    observations = [
        make_observation(
            raw_record_id=raw_record.record_id,
            observation_id=uuid4(),
            value=Decimal(f"{100 + i}.00"),
        )
        for i in range(20)
    ]
    obs_ids = [obs.observation_id for obs in observations]

    with LocalStorage(StoragePaths.from_root(tmp_path / "row")) as storage_row:
        storage_row.raw_records.save(raw_record)
        row_wrapper = _QueryCountingConnection(storage_row.observations._connection)
        storage_row.observations._connection = row_wrapper

        for obs in observations:
            storage_row.observations.save(obs)
        for obs_id in obs_ids:
            storage_row.observations.get(obs_id)
        row_queries = row_wrapper.execute_count

    with LocalStorage(StoragePaths.from_root(tmp_path / "batch")) as storage_batch:
        storage_batch.raw_records.save(raw_record)
        batch_wrapper = _QueryCountingConnection(storage_batch.observations._connection)
        storage_batch.observations._connection = batch_wrapper

        storage_batch.observations.save_many(observations)
        storage_batch.observations.get_many(obs_ids)
        batch_queries = batch_wrapper.execute_count

    assert batch_queries > 0
    assert row_queries >= 10 * batch_queries, (
        f"Expected >= 10x fewer queries, got row={row_queries} vs batch={batch_queries}"
    )


def test_existing_single_row_methods_are_unchanged(storage) -> None:
    raw_record = make_raw_record()
    obs = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=obs.observation_id)
    diag = make_diagnostic_result(metric_result_id=metric.result_id)

    assert storage.observations.save(obs) == obs
    assert storage.metric_results.save(metric) == metric
    assert storage.diagnostics.save(diag) == diag

    assert storage.observations.get(obs.observation_id) == obs
    assert storage.metric_results.get(metric.result_id) == metric
    assert storage.diagnostics.get(diag.diagnostic_id) == diag

    assert storage.observations.list(asset_id=obs.asset_id) == [obs]
    assert storage.metric_results.list(asset_id=metric.asset_id) == [metric]
    assert storage.diagnostics.list(asset_id=diag.asset_id) == [diag]

    assert storage.metric_results.count(asset_id=metric.asset_id) == 1
    assert storage.diagnostics.count(asset_id=diag.asset_id) == 1


def test_batch_paths_create_no_schema_object_or_migration(storage) -> None:
    tables_query = (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    )
    initial_tables = storage.store.connection.execute(tables_query).fetchall()

    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)
    obs = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=obs.observation_id)
    diag = make_diagnostic_result(metric_result_id=metric.result_id)

    storage.observations.save_many([obs])
    storage.metric_results.save_many([metric])
    storage.diagnostics.save_many([diag])

    storage.observations.get_many([obs.observation_id])
    storage.metric_results.get_many([metric.result_id])
    storage.diagnostics.get_many([diag.diagnostic_id])

    after_tables = storage.store.connection.execute(tables_query).fetchall()

    assert initial_tables == after_tables


def test_batch_write_uses_a_single_writer_connection(storage, monkeypatch) -> None:
    import duckdb

    def fail_connect(*args, **kwargs):
        raise RuntimeError("No new connection allowed")

    monkeypatch.setattr(duckdb, "connect", fail_connect)

    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)
    obs = make_observation(raw_record_id=raw_record.record_id)
    metric = make_metric_result(observation_id=obs.observation_id)
    diag = make_diagnostic_result(metric_result_id=metric.result_id)

    storage.observations.save_many([obs])
    storage.metric_results.save_many([metric])
    storage.diagnostics.save_many([diag])


def test_result_order_never_defines_semantics(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    obs_a = make_observation(
        raw_record_id=raw_record.record_id, observation_id=uuid4(), value=Decimal("10.00")
    )
    obs_b = make_observation(
        raw_record_id=raw_record.record_id, observation_id=uuid4(), value=Decimal("20.00")
    )

    storage.observations.save_many([obs_a, obs_b])

    res_forward = storage.observations.get_many([obs_a.observation_id, obs_b.observation_id])
    res_reverse = storage.observations.get_many([obs_b.observation_id, obs_a.observation_id])

    assert res_forward == res_reverse
    assert res_forward[obs_a.observation_id] == obs_a
    assert res_forward[obs_b.observation_id] == obs_b


def test_decimal_utc_and_available_at_are_preserved_in_batches(storage) -> None:
    raw_record = make_raw_record()
    storage.raw_records.save(raw_record)

    utc_avail = datetime(2026, 7, 10, 16, 0, tzinfo=UTC)
    obs = make_observation(
        raw_record_id=raw_record.record_id,
        observation_id=uuid4(),
        value=Decimal("123.456700"),
        available_at=utc_avail,
    )
    metric = make_metric_result(
        observation_id=obs.observation_id,
        result_id=uuid4(),
    ).model_copy(
        update={
            "value": Decimal("987.654321"),
            "available_at": utc_avail,
            "computed_at": utc_avail + timedelta(minutes=5),
        }
    )
    diag = make_diagnostic_result(
        metric_result_id=metric.result_id,
        diagnostic_id=uuid4(),
    ).model_copy(
        update={
            "available_at": utc_avail,
            "computed_at": utc_avail + timedelta(minutes=5),
        }
    )

    storage.observations.save_many([obs])
    storage.metric_results.save_many([metric])
    storage.diagnostics.save_many([diag])

    rec_obs = storage.observations.get_many([obs.observation_id])[obs.observation_id]
    rec_metric = storage.metric_results.get_many([metric.result_id])[metric.result_id]
    rec_diag = storage.diagnostics.get_many([diag.diagnostic_id])[diag.diagnostic_id]

    assert rec_obs.value == Decimal("123.456700")
    assert isinstance(rec_obs.value, Decimal)
    assert rec_obs.available_at == utc_avail
    assert rec_obs.available_at.tzinfo is UTC

    assert rec_metric.value == Decimal("987.654321")
    assert isinstance(rec_metric.value, Decimal)
    assert rec_metric.available_at == utc_avail
    assert rec_metric.available_at.tzinfo is UTC

    assert rec_diag.final_score == Decimal("80")
    assert isinstance(rec_diag.final_score, Decimal)
    assert rec_diag.available_at == utc_avail
    assert rec_diag.available_at.tzinfo is UTC


def test_no_validation_becomes_optional_sampled_or_configurable(storage) -> None:
    for repo in (storage.observations, storage.metric_results, storage.diagnostics):
        save_params = inspect.signature(repo.save_many).parameters
        get_params = inspect.signature(repo.get_many).parameters
        assert len(save_params) == 1
        assert len(get_params) == 1
        param_name = next(iter(save_params.keys()))
        assert param_name in {"observations", "results"}
