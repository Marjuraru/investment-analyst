"""Analytical boundary tests for persisted execution reuse."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.analytics.execution_reuse import ExecutionReuseService
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.execution_reuse_v2 import ExecutionReuseKey, canonical_digest
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_MOMENT = datetime(2026, 10, 8, tzinfo=UTC)


def _persisted_metric() -> tuple[RawRecord, NormalizedObservation, MetricResult]:
    source = SourceReference(
        source_id="fixture:market",
        record_key="execution-reuse",
        retrieved_at=_MOMENT,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "execution-reuse-raw"),
        asset_id="equity:us:aapl",
        source=source,
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"close": "200.00"},
        schema_version="execution-reuse-fixture-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, "execution-reuse-observation"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="close",
        value=Decimal("200.00"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_MOMENT,
        available_at=_MOMENT,
        normalized_at=_MOMENT,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="execution-reuse-fixture-v1",
    )
    candidate = MetricResult(
        result_id=uuid5(NAMESPACE_URL, "execution-reuse-placeholder"),
        asset_id=raw.asset_id,
        metric_key="market.fixture.close",
        value=Decimal("200.00"),
        unit="USD",
        as_of=_MOMENT,
        available_at=_MOMENT,
        computed_at=_MOMENT,
        parameters={"window": 1},
        input_observation_ids=[observation.observation_id],
        algorithm_version="execution-reuse-fixture-v1",
        quality=DataQuality.VALID,
    )
    return (
        raw,
        observation,
        candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)}),
    )


def test_v1_storage_is_a_fail_closed_reuse_miss(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "v1")
    key = ExecutionReuseKey(
        family="market",
        asset_id="equity:us:aapl",
        source_id="fixture:market",
        frequency="1d",
        algorithm_version="market-v1",
        evidence_digest="evidence",
        parameters_digest=canonical_digest({"window": 20}),
    )

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        assert ExecutionReuseService(storage).lookup(key, known_at=_MOMENT) is None


def test_v2_reuse_is_pit_visible_and_invalidates_on_evidence_change(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "v2", format_version=2)
    raw, observation, metric = _persisted_metric()
    key = ExecutionReuseKey(
        family="market",
        asset_id=metric.asset_id,
        source_id="fixture:market",
        frequency="1d",
        algorithm_version=metric.algorithm_version,
        evidence_digest="evidence-v1",
        parameters_digest=canonical_digest({"window": 1}),
    )

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save(metric)
        reuse = ExecutionReuseService(storage)
        reuse.remember(key, metric, stored_at=_MOMENT)

        assert reuse.lookup(key, known_at=_MOMENT + timedelta(days=1)) == metric
        assert (
            reuse.lookup(
                key.model_copy(update={"evidence_digest": "evidence-v2"}),
                known_at=_MOMENT + timedelta(days=1),
            )
            is None
        )


def test_v2_bundle_reuse_returns_the_complete_result_set(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "v2", format_version=2)
    raw, observation, metric = _persisted_metric()
    second_candidate = metric.model_copy(
        update={
            "result_id": uuid5(NAMESPACE_URL, "execution-reuse-second-placeholder"),
            "metric_key": "market.fixture.open",
        }
    )
    second = second_candidate.model_copy(
        update={"result_id": metric_result_id_from_model_v2(second_candidate)}
    )
    key = ExecutionReuseKey(
        family="market",
        asset_id=metric.asset_id,
        source_id="fixture:market",
        frequency="1d",
        algorithm_version=metric.algorithm_version,
        evidence_digest="evidence-v1",
        parameters_digest=canonical_digest({"window": 1}),
    )

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save_many((metric, second))
        reuse = ExecutionReuseService(storage)
        reuse.remember_bundle(
            key,
            (metric, second),
            stored_at=_MOMENT,
            outcome_json='{"missing_requirements":[]}',
        )

        assert reuse.lookup_bundle(key, known_at=_MOMENT + timedelta(days=1)) == (metric, second)
        assert reuse.lookup_bundle_outcome(key, known_at=_MOMENT + timedelta(days=1)) == (
            (metric, second),
            '{"missing_requirements":[]}',
        )
        assert reuse.lookup_bundle(key, known_at=_MOMENT - timedelta(microseconds=1)) is None
        assert (
            reuse.lookup_bundle(
                key.model_copy(update={"evidence_digest": "evidence-v2"}),
                known_at=_MOMENT + timedelta(days=1),
            )
            is None
        )
