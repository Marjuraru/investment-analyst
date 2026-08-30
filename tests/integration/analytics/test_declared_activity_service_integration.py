from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.analytics.cazatiburones.declared_activity_service import (
    DeclaredActivityService,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths


def test_service_requires_read_only_storage_and_preserves_empty_evidence(tmp_path: Path) -> None:
    known_at = datetime(2025, 1, 1, tzinfo=UTC)
    with (
        LocalStorage(StoragePaths.from_root(tmp_path)) as writable,
        pytest.raises(StorageError, match="read-only storage"),
    ):
        DeclaredActivityService(writable).query(asset_id="equity:us:aapl", known_at=known_at)
    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        result = DeclaredActivityService(storage).query(
            asset_id="equity:us:aapl", known_at=known_at
        )
    assert result.total_statements == 0
    assert result.insider_features == ()
    assert result.beneficial_features == ()
