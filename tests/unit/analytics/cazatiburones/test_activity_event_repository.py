from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.activity_event_models import ActivityEventSnapshot
from investment_analyst.analytics.cazatiburones.activity_event_repository import (
    ActivityEventRepository,
)


def test_repository_is_append_only_and_reuses_identical_snapshot(tmp_path: Path) -> None:
    snapshot = ActivityEventSnapshot(
        snapshot_id=uuid4(),
        asset_id="equity:us:aapl",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2025, 1, 2, tzinfo=UTC),
        policy_version="test",
        evaluations=(),
        events=(),
        candidates=(),
    )
    repository = ActivityEventRepository(tmp_path, read_only=False)

    assert repository.save(snapshot) is True
    assert repository.save(snapshot) is False
    assert (
        repository.get(
            asset_id=snapshot.asset_id,
            known_at=snapshot.known_at.isoformat(),
            snapshot_id=snapshot.snapshot_id,
        )
        == snapshot
    )
