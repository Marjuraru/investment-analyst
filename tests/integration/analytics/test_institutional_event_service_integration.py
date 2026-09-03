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
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_CIK = "0001350694"
_ASSET_ID = "equity:us:aapl"


def _seed_metrics(storage: LocalStorage, available_at: datetime) -> list[MetricResult]:
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
    for metric in metrics:
        storage.metric_results.save(metric)
    return metrics


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
