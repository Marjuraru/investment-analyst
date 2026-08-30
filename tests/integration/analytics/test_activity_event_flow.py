from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.activity_event_service import ActivityEventService
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths


def test_materialization_is_idempotent_and_readable(tmp_path: Path) -> None:
    known_at = datetime(2025, 1, 2, tzinfo=UTC)
    metric = MetricResult(
        result_id=uuid4(),
        asset_id="equity:us:aapl",
        metric_key="cazatiburones.insider.holding_delta_ratio",
        value=Decimal("0.25"),
        unit="ratio",
        as_of=known_at,
        available_at=known_at,
        computed_at=known_at,
        parameters={"known_at": known_at.isoformat(), "participant_cik": "0000000001"},
        input_observation_ids=[uuid4(), uuid4()],
        algorithm_version="cazatiburones-activity-metrics-v1",
        quality=DataQuality.VALID,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(metric)
        service = ActivityEventService(storage, clock=lambda: known_at)
        first = service.materialize(asset_id=metric.asset_id, known_at=known_at)
        second = service.materialize(asset_id=metric.asset_id, known_at=known_at)

    assert first.created is True
    assert second.created is False
    assert first.snapshot_id == second.snapshot_id
