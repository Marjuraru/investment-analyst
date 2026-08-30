from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.analytics.cazatiburones.activity_rule_service import ActivityRuleService
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths


def test_service_requires_read_only_storage_and_preserves_empty_evidence(tmp_path: Path) -> None:
    known_at = datetime(2025, 1, 1, tzinfo=UTC)
    with (
        LocalStorage(StoragePaths.from_root(tmp_path)) as writable,
        pytest.raises(StorageError, match="read-only storage"),
    ):
        ActivityRuleService(writable).query_declared_activity(
            asset_id="equity:us:aapl", known_at=known_at
        )
    with (
        LocalStorage(StoragePaths.from_root(tmp_path)) as writable,
        pytest.raises(StorageError, match="read-only storage"),
    ):
        ActivityRuleService(writable).query_institutional(
            manager_cik="0001067983", known_at=known_at
        )

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        declared = ActivityRuleService(storage).query_declared_activity(
            asset_id="equity:us:aapl", known_at=known_at
        )
        institutional = ActivityRuleService(storage).query_institutional(
            manager_cik="0001067983", known_at=known_at
        )
    assert declared.evaluations == ()
    assert declared.total_features_evaluated == 0
    assert institutional.evaluations == ()
    assert institutional.total_features_evaluated == 0
