"""Integration tests for institutional event service and application boundary."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
    InstitutionalEventRepository,
    InstitutionalEventRepositoryError,
)
from investment_analyst.application.cazatiburones_institutional_events import (
    CazatiburonesInstitutionalEventsApplication,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    DataFrequency,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_CIK = "0001350694"
_ASSET_ID = "equity:us:aapl"


def _seed_metrics(
    storage: LocalStorage,
    available_at: datetime,
    *,
    persist: bool = True,
) -> list[MetricResult]:
    metrics = [
        MetricResult(
            result_id=uuid4(),
            asset_id=_ASSET_ID,
            metric_key="cazatiburones.institutional.delta_reported_shares",
            value=Decimal("50000"),
            unit="shares",
            as_of=available_at,
            available_at=available_at,
            computed_at=available_at,
            parameters={
                "manager_cik": _CIK,
                "cusip": "037833100",
                "title_of_class": "COM",
                "put_call": None,
                "report_period": "2024-09-30",
                "prior_report_period": "2024-06-30",
            },
            input_observation_ids=[uuid4(), uuid4()],
            algorithm_version="cazatiburones-institutional-metrics-v1",
            quality=DataQuality.VALID,
        ),
        MetricResult(
            result_id=uuid4(),
            asset_id=_ASSET_ID,
            metric_key="cazatiburones.institutional.delta_reported_fair_value",
            value=Decimal("11000000"),
            unit="USD",
            as_of=available_at,
            available_at=available_at,
            computed_at=available_at,
            parameters={
                "manager_cik": _CIK,
                "cusip": "037833100",
                "title_of_class": "COM",
                "put_call": None,
                "report_period": "2024-09-30",
                "prior_report_period": "2024-06-30",
            },
            input_observation_ids=[uuid4(), uuid4()],
            algorithm_version="cazatiburones-institutional-metrics-v1",
            quality=DataQuality.VALID,
        ),
        MetricResult(
            result_id=uuid4(),
            asset_id=_ASSET_ID,
            metric_key="cazatiburones.institutional.reported_shares_delta_ratio",
            value=Decimal("0.05"),
            unit="ratio",
            as_of=available_at,
            available_at=available_at,
            computed_at=available_at,
            parameters={
                "manager_cik": _CIK,
                "cusip": "037833100",
                "title_of_class": "COM",
                "put_call": None,
                "report_period": "2024-09-30",
                "prior_report_period": "2024-06-30",
            },
            input_observation_ids=[uuid4(), uuid4()],
            algorithm_version="cazatiburones-institutional-metrics-v1",
            quality=DataQuality.VALID,
        ),
    ]
    if persist:
        for metric in metrics:
            storage.metric_results.save(metric)
    return metrics


def test_event_reads_only_institutional_metric_family(tmp_path: Path) -> None:
    from investment_analyst.analytics.cazatiburones.institutional_event_service import (
        InstitutionalEventService,
    )
    from investment_analyst.core.models.enums import DataQuality
    from investment_analyst.core.models.metric import MetricResult

    workspace_paths = WorkspaceService().initialize(tmp_path / "workspace").paths
    runtime = ApplicationRuntime.create_default()
    location = StorageLocationRequest(workspace=workspace_paths.root)
    available_at = datetime(2024, 11, 14, 16, 0, 0, tzinfo=UTC)
    known_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        seeded = _seed_metrics(storage, available_at)
        market_metric = MetricResult(
            result_id=uuid4(),
            asset_id=_ASSET_ID,
            metric_key="market.close_copy",
            value=Decimal("210.50"),
            unit="USD",
            as_of=available_at,
            available_at=available_at,
            computed_at=available_at,
            parameters={"window": 1},
            input_observation_ids=[uuid4()],
            algorithm_version="1.0.0",
            quality=DataQuality.VALID,
        )
        storage.metric_results.save(market_metric)
        hydrated: list[object] = []
        original_get_many = storage.metric_results.get_many

        def spy_get_many(result_ids):  # type: ignore[no-untyped-def]
            identifiers = tuple(result_ids)
            hydrated.extend(identifiers)
            return original_get_many(identifiers)

        storage.metric_results.get_many = spy_get_many  # type: ignore[method-assign]
        service = InstitutionalEventService(storage, clock=lambda: known_at)
        summary = service.materialize(asset_id=_ASSET_ID, manager_cik=_CIK, known_at=known_at)

    assert summary.events == 3
    assert set(hydrated) == {metric.result_id for metric in seeded}


def test_rematerialization_is_idempotent_and_divergent_identity_fails_closed(
    tmp_path: Path,
) -> None:
    workspace_paths = WorkspaceService().initialize(tmp_path / "workspace").paths
    runtime = ApplicationRuntime.create_default()
    location = StorageLocationRequest(workspace=workspace_paths.root)

    available_at = datetime(2024, 11, 14, 16, 0, 0, tzinfo=UTC)
    known_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        _seed_metrics(storage, available_at)

    app = CazatiburonesInstitutionalEventsApplication(runtime)

    # First materialization
    first = app.materialize(
        asset_id=_ASSET_ID,
        manager_cik=_CIK,
        known_at=known_at,
        location=location,
    )
    assert first.created is True
    assert first.events == 3
    assert first.candidates == 3

    # Second materialization (idempotent, different execution clock)
    second = app.materialize(
        asset_id=_ASSET_ID,
        manager_cik=_CIK,
        known_at=known_at,
        location=location,
    )
    assert second.created is False
    assert second.snapshot_id == first.snapshot_id
    assert second.events == 3
    assert second.candidates == 3

    # Query snapshot
    queried = app.query(
        asset_id=_ASSET_ID,
        manager_cik=_CIK,
        known_at=known_at,
        snapshot_id=first.snapshot_id,
        location=location,
    )
    assert queried is not None
    assert queried.snapshot_id == first.snapshot_id
    assert len(queried.events) == 3
    assert len(queried.candidates) == 3
    assert {e.rule_id for e in queried.events} == {
        "institutional-delta-reported-shares-increased",
        "institutional-delta-reported-fair-value-increased",
        "institutional-reported-shares-delta-ratio-increased",
    }

    # Verify divergent content under same identity fails closed
    processed_dir = StoragePaths.from_root(workspace_paths.storage_root).processed_dir
    divergent_snapshot = InstitutionalEventSnapshot(
        snapshot_id=first.snapshot_id,
        asset_id=_ASSET_ID,
        manager_cik=_CIK,
        known_at=known_at,
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        policy_version="cazatiburones-persisted-institutional-events-v1",
        evaluations=(),
        events=(),
        candidates=(),
        omissions=("forced_divergent_omission",),
    )
    repo = InstitutionalEventRepository(processed_dir, read_only=False)
    with pytest.raises(
        InstitutionalEventRepositoryError, match="snapshot identity conflicts with existing content"
    ):
        repo.save(divergent_snapshot)


def test_v2_reuse_precedes_event_projection(tmp_path: Path, monkeypatch) -> None:
    from investment_analyst.analytics.cazatiburones import institutional_event_service
    from investment_analyst.analytics.cazatiburones.institutional_event_service import (
        InstitutionalEventService,
    )

    workspace_service = WorkspaceService()
    workspace_paths = workspace_service.initialize(
        tmp_path / "workspace",
        format_version=2,
    ).paths
    available_at = datetime(2024, 11, 14, 16, 0, 0, tzinfo=UTC)
    known_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    calls = 0
    original_project = institutional_event_service.project_institutional_events

    def counting_project(metrics):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original_project(metrics)

    monkeypatch.setattr(
        institutional_event_service,
        "project_institutional_events",
        counting_project,
    )
    with workspace_service.open_storage(
        workspace_paths,
        WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        for metric in _seed_metrics(storage, available_at, persist=False):
            for offset, observation_id in enumerate(metric.input_observation_ids):
                raw_record_id = uuid4()
                source = SourceReference(
                    source_id="fixture:institutional-events",
                    record_key=f"{metric.result_id}:{offset}",
                    retrieved_at=available_at,
                )
                storage.raw_records.save(
                    RawRecord(
                        record_id=raw_record_id,
                        asset_id=metric.asset_id,
                        source=source,
                        event_time=available_at,
                        available_at=available_at,
                        received_at=available_at,
                        payload={"offset": offset},
                        schema_version="institutional-event-reuse-fixture-v1",
                    )
                )
                storage.observations.save(
                    NormalizedObservation(
                        observation_id=observation_id,
                        raw_record_id=raw_record_id,
                        asset_id=metric.asset_id,
                        field_name="institutional.fixture",
                        value=Decimal(offset + 1),
                        unit="shares",
                        frequency=DataFrequency.QUARTERLY,
                        observed_at=available_at,
                        available_at=available_at,
                        normalized_at=available_at,
                        source=source,
                        quality=DataQuality.VALID,
                        transformation_version="institutional-event-reuse-fixture-v1",
                    )
                )
            storage.metric_results.save(metric)
        service = InstitutionalEventService(storage, clock=lambda: known_at)
        first = service.materialize(
            asset_id=_ASSET_ID,
            manager_cik=_CIK,
            known_at=known_at,
        )
        repeated = service.materialize(
            asset_id=_ASSET_ID,
            manager_cik=_CIK,
            known_at=known_at,
        )

    assert first.created is True
    assert repeated.created is False
    assert repeated.snapshot_id == first.snapshot_id
    assert calls == 1
