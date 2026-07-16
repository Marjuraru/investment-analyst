"""Tests for atomic operational state and process locking."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunState,
    AaplDailyRunStatus,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunAlreadyRunningError,
    AaplDailyRunLock,
    AaplDailyRunStateStore,
    AaplOperationalStateError,
)
from investment_analyst.core.models import DataFrequency


def _running(root: Path) -> AaplDailyRunState:
    return AaplDailyRunState(
        run_id=uuid4(),
        status=AaplDailyRunStatus.RUNNING,
        workspace_root=root,
        request=AaplWorkspaceBootstrapRequest(
            market_start=date(2025, 1, 1),
            market_end=date(2026, 7, 15),
            fundamental_frequency=DataFrequency.QUARTERLY,
        ),
        started_at=datetime(2026, 7, 16, 15, tzinfo=UTC),
    )


def test_state_store_replaces_atomically_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "latest.json"
    store = AaplDailyRunStateStore(path)
    state = _running(tmp_path)

    assert store.load() is None
    store.write(state)

    assert store.load() == state
    assert path.is_file()
    assert list(path.parent.glob(".*.tmp")) == []


def test_state_store_rejects_malformed_documents(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    path.write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(AaplOperationalStateError, match="malformed"):
        AaplDailyRunStateStore(path).load()


def test_lock_rejects_concurrent_holder_and_releases_after_exit(tmp_path: Path) -> None:
    path = tmp_path / "daily.lock"
    run_id = uuid4()
    first = AaplDailyRunLock(path, run_id=run_id, started_at="2026-07-16T15:00:00Z")

    with first:
        assert AaplDailyRunLock.is_held(path)
        with (
            pytest.raises(AaplDailyRunAlreadyRunningError, match="already holds"),
            AaplDailyRunLock(
                path,
                run_id=uuid4(),
                started_at="2026-07-16T15:01:00Z",
            ),
        ):
            raise AssertionError("a second holder must never enter")

    assert not AaplDailyRunLock.is_held(path)
