"""Unit tests for institutional event snapshot repository."""

from datetime import UTC, datetime
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


def _sample_snapshot(*, snapshot_id_value=None, omissions=()) -> InstitutionalEventSnapshot:
    return InstitutionalEventSnapshot(
        snapshot_id=snapshot_id_value or uuid4(),
        asset_id="equity:us:aapl",
        manager_cik="0001350694",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2025, 1, 2, tzinfo=UTC),
        policy_version="cazatiburones-persisted-institutional-events-v1",
        evaluations=(),
        events=(),
        candidates=(),
        omissions=omissions,
    )


def test_snapshot_directory_separate_from_declared_activity(tmp_path: Path) -> None:
    repo = InstitutionalEventRepository(tmp_path, read_only=False)
    snapshot = _sample_snapshot()

    assert repo.save(snapshot) is True

    # Assert root directory is cazatiburones_institutional_events_v1
    # and NOT cazatiburones_activity_events_v1
    assert (tmp_path / "cazatiburones_institutional_events_v1").is_dir()
    assert not (tmp_path / "cazatiburones_activity_events_v1").exists()


def test_repository_is_append_only_and_reuses_identical_snapshot(tmp_path: Path) -> None:
    snapshot = _sample_snapshot()
    repo = InstitutionalEventRepository(tmp_path, read_only=False)

    assert repo.save(snapshot) is True
    # Second save of identical snapshot returns False (idempotent, no error)
    assert repo.save(snapshot) is False

    retrieved = repo.get(
        asset_id=snapshot.asset_id,
        manager_cik=snapshot.manager_cik,
        known_at=snapshot.known_at.isoformat(),
        snapshot_id=snapshot.snapshot_id,
    )
    assert retrieved == snapshot


def test_divergent_snapshot_conflict_fails_closed(tmp_path: Path) -> None:
    shared_id = uuid4()
    first = _sample_snapshot(snapshot_id_value=shared_id, omissions=())
    second = _sample_snapshot(snapshot_id_value=shared_id, omissions=("missing_persisted_metric",))

    repo = InstitutionalEventRepository(tmp_path, read_only=False)
    assert repo.save(first) is True

    with pytest.raises(
        InstitutionalEventRepositoryError, match="snapshot identity conflicts with existing content"
    ):
        repo.save(second)


def test_read_only_storage_rejects_save(tmp_path: Path) -> None:
    repo = InstitutionalEventRepository(tmp_path, read_only=True)
    snapshot = _sample_snapshot()

    with pytest.raises(
        InstitutionalEventRepositoryError,
        match="institutional event snapshots require writable storage",
    ):
        repo.save(snapshot)


def test_verify_snapshots(tmp_path: Path) -> None:
    repo = InstitutionalEventRepository(tmp_path, read_only=False)
    assert repo.verify() == 0

    repo.save(_sample_snapshot())
    repo.save(_sample_snapshot())
    assert repo.verify() == 2


def test_enumeration_is_additive_and_read_only(tmp_path: Path) -> None:
    read_only = InstitutionalEventRepository(tmp_path, read_only=True)
    assert read_only.list_snapshots() == ()
    assert not (tmp_path / "cazatiburones_institutional_events_v1").exists()

    snapshot = _sample_snapshot()
    writable = InstitutionalEventRepository(tmp_path, read_only=False)
    assert writable.save(snapshot) is True
    assert read_only.list_snapshots() == (snapshot,)
