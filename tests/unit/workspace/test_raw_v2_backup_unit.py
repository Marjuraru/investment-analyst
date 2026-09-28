"""Unit tests for the raw v2 staging backup inventory bounds."""

from pathlib import Path

import duckdb
import pytest

from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.workspace.raw_v2_backup import (
    RawV2BackupError,
    RawV2StagingBackupService,
)


def _staging(tmp_path: Path, name: str = "staging") -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def test_backup_inventory_pages_are_bounded(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    with staging:
        from tests.unit.storage.test_raw_v2 import _record

        records = [_record() for _ in range(5)]
        staging.save_many(records)
        connection = staging._connection
        service = RawV2StagingBackupService()
        manifest = service.create(staging, connection, tmp_path / "backup")
    assert manifest.counts.records == 5
    blob_entries = [item for item in manifest.files if "/sha256/" in item.path]
    assert len(blob_entries) == 5
    assert tuple(item.path for item in manifest.files) == tuple(
        sorted(item.path for item in manifest.files)
    )


def test_legacy_raw_only_manifest_remains_readable(tmp_path: Path) -> None:
    import json

    staging = _staging(tmp_path)
    with staging:
        from tests.unit.storage.test_raw_v2 import _record

        staging.save(_record())
        manifest = RawV2StagingBackupService().create(
            staging, staging._connection, tmp_path / "backup"
        )
    assert manifest.schema_version == "raw-v2-staging-backup-manifest-v1"
    assert manifest.observation_counts is None
    document = json.loads((tmp_path / "backup" / "raw-v2-staging-backup-manifest.json").read_text())
    assert document["schema_version"] == "raw-v2-staging-backup-manifest-v1"
    assert "observation_counts" not in document or document["observation_counts"] is None


def test_backup_rejects_external_or_memory_index(tmp_path: Path) -> None:
    from investment_analyst.workspace.raw_v2_backup import _require_snapshot_consistent

    staging = _staging(tmp_path)
    with staging:
        from tests.unit.storage.test_raw_v2 import _record

        staging.save(_record())
        external = duckdb.connect(":memory:")
        with pytest.raises(RawV2BackupError, match="single file|connected database"):
            RawV2StagingBackupService().create(staging, external, tmp_path / "backup")
        external.close()
        foreign = duckdb.connect(str(tmp_path / "foreign.duckdb"))
        try:
            with pytest.raises(RawV2BackupError, match="connected database"):
                RawV2StagingBackupService().create(staging, foreign, tmp_path / "backup")
        finally:
            foreign.close()
        index_names = [
            entry.name for entry in staging.destination.iterdir() if entry.suffix == ".duckdb"
        ]
        assert len(index_names) == 1
        index_path = staging.destination / index_names[0]
        (staging.destination / f"{index_names[0]}.wal").write_bytes(b"pending")
        try:
            with pytest.raises(RawV2BackupError, match="write-ahead log"):
                _require_snapshot_consistent(
                    staging._connection, index_path, allow_checkpoint=False
                )
        finally:
            (staging.destination / f"{index_names[0]}.wal").unlink(missing_ok=True)
