"""Unit tests for institutional event service."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.institutional_event_service import (
    InstitutionalEventService,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths


def _metric(
    *,
    metric_key: str = "cazatiburones.institutional.delta_reported_shares",
    value: Decimal = Decimal("500"),
    available_at: datetime,
    manager_cik: str = "0001350694",
    algorithm_version: str = "cazatiburones-institutional-metrics-v1",
) -> MetricResult:
    return MetricResult(
        result_id=uuid4(),
        asset_id="equity:us:aapl",
        metric_key=metric_key,
        value=value,
        unit="shares",
        as_of=available_at,
        available_at=available_at,
        computed_at=available_at,
        parameters={
            "manager_cik": manager_cik,
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
            "report_period": "2024-09-30",
            "prior_report_period": "2024-06-30",
        },
        input_observation_ids=[uuid4(), uuid4()],
        algorithm_version=algorithm_version,
        quality=DataQuality.VALID,
    )


def test_materialization_requires_writable_storage(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths, read_only=False) as storage:
        pass

    with LocalStorage(paths, read_only=True) as storage:
        service = InstitutionalEventService(storage)
        with pytest.raises(
            StorageError, match="institutional event materialization requires writable storage"
        ):
            service.materialize(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=datetime(2025, 1, 1, tzinfo=UTC),
            )


def test_query_uses_read_only_storage(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    known_at = datetime(2025, 1, 1, tzinfo=UTC)
    metric = _metric(available_at=datetime(2024, 11, 14, tzinfo=UTC))

    with LocalStorage(paths, read_only=False) as storage:
        storage.metric_results.save(metric)
        service = InstitutionalEventService(storage, clock=lambda: known_at)
        summary = service.materialize(
            asset_id=metric.asset_id,
            manager_cik="0001350694",
            known_at=known_at,
        )
        snapshot_id = summary.snapshot_id

    # Read-only storage allows querying
    with LocalStorage(paths, read_only=True) as storage:
        read_service = InstitutionalEventService(storage)
        queried = read_service.query(
            asset_id=metric.asset_id,
            manager_cik="0001350694",
            known_at=known_at,
            snapshot_id_value=snapshot_id,
        )
        assert queried is not None
        assert queried.snapshot_id == snapshot_id
        assert len(queried.events) == 1


def test_query_requires_explicit_snapshot_id(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    known_at = datetime(2025, 1, 1, tzinfo=UTC)

    with LocalStorage(paths, read_only=False) as storage:
        pass

    with LocalStorage(paths, read_only=True) as storage:
        service = InstitutionalEventService(storage)
        # Random explicit snapshot_id returns None (not found) without error or selecting by clock
        random_id = uuid4()
        result = service.query(
            asset_id="equity:us:aapl",
            manager_cik="0001350694",
            known_at=known_at,
            snapshot_id_value=random_id,
        )
        assert result is None


def test_service_filters_by_algorithm_manager_and_pit(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    known_at = datetime(2025, 1, 1, tzinfo=UTC)

    # Eligible metric
    eligible = _metric(available_at=datetime(2024, 11, 14, tzinfo=UTC))
    # Future metric available after known_at -> must be excluded
    future_metric = _metric(available_at=known_at + timedelta(days=1))
    # Different manager -> must be excluded
    other_mgr = _metric(available_at=datetime(2024, 11, 14, tzinfo=UTC), manager_cik="0009999999")
    # Different algorithm version -> must be excluded
    other_algo = _metric(
        available_at=datetime(2024, 11, 14, tzinfo=UTC),
        algorithm_version="cazatiburones-activity-metrics-v1",
    )

    with LocalStorage(paths, read_only=False) as storage:
        storage.metric_results.save(eligible)
        storage.metric_results.save(future_metric)
        storage.metric_results.save(other_mgr)
        storage.metric_results.save(other_algo)

        service = InstitutionalEventService(storage, clock=lambda: known_at)
        summary = service.materialize(
            asset_id=eligible.asset_id,
            manager_cik="0001350694",
            known_at=known_at,
        )

        assert summary.events == 1
        assert summary.created is True
