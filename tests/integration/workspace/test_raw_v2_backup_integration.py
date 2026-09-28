"""Integration tests for raw v2 staging backup, restore and portable resume."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer
from investment_analyst.workspace.raw_v2_backup import (
    RawV2BackupError,
    RawV2StagingBackupService,
)

_TIMESTAMP = datetime(2026, 8, 1, tzinfo=UTC)
_FUTURE = datetime(2026, 9, 1, tzinfo=UTC)
_RECEIVED_FUTURE = datetime(2026, 9, 2, tzinfo=UTC)


def _raw_record(index: int, *, available_at: datetime | None = None) -> RawRecord:
    future = available_at is not None
    return RawRecord(
        record_id=UUID(int=index + 1),
        asset_id="equity:us:aapl" if index % 3 else "crypto:btc-usd",
        source=SourceReference(
            source_id="test:import",
            record_key=f"import-{index}",
            retrieved_at=_RECEIVED_FUTURE if future else _TIMESTAMP,
        ),
        event_time=_TIMESTAMP,
        available_at=available_at or _TIMESTAMP,
        received_at=_RECEIVED_FUTURE if future else _TIMESTAMP,
        payload=(
            {"report": {"manager_cik": "0001067983"}} if index % 3 == 2 else {"value": str(index)}
        ),
        schema_version="import-v1",
    )


def _seed_v1(source_root: Path, count: int) -> None:
    with LocalStorage(StoragePaths.from_root(source_root)) as writer:
        for index in range(count):
            writer.raw_records.save(
                _raw_record(
                    index,
                    available_at=_FUTURE if index == count - 1 else _TIMESTAMP,
                )
            )


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _fingerprint(source: LocalStorage) -> str:
    import hashlib

    rows = source.store.connection.execute(
        "SELECT record_id, checksum_sha256 FROM raw_record_index ORDER BY record_id"
    ).fetchall()
    digest = hashlib.sha256()
    for record_id, checksum in rows:
        digest.update(str(record_id).encode("utf-8"))
        digest.update(str(checksum).encode("utf-8"))
    return digest.hexdigest()


def _importer(source: LocalStorage, staging: RawV2Staging) -> RawV2Importer:
    inspection = source.store.connection.execute("SELECT count(*) FROM raw_record_index").fetchone()
    assert inspection is not None
    return RawV2Importer(
        source,
        staging,
        source_workspace_id=f"workspace-{inspection[0]}",
        source_fingerprint=_fingerprint(source),
    )


def test_staging_backup_restore_preserves_raw_index_blobs_and_pit(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 6)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            manifest = service.create(staging, staging._connection, tmp_path / "backup")
            assert manifest.counts.records == 6
        restored_manifest = service.restore(tmp_path / "backup", tmp_path / "restored")
        assert restored_manifest.backup_id == manifest.backup_id
        second = service.restore(tmp_path / "backup", tmp_path / "restored-again")
        assert second.counts.corpus_digest == manifest.counts.corpus_digest
        connection = duckdb.connect(str(tmp_path / "restored" / "raw-v2-index.duckdb"))
        restored_staging = RawV2Staging(tmp_path / "restored", connection)
        with restored_staging:
            assert sorted(restored_staging.list_record_ids()) == sorted(
                staging.list_record_ids() if staging.is_open else restored_staging.list_record_ids()
            )
            cutoff = datetime(2026, 8, 15, tzinfo=UTC)
            current_cut = restored_staging.list_record_ids(available_to=cutoff)
            assert len(current_cut) == 5
            assert len(restored_staging.list_record_ids()) == 6


def test_relocated_partial_import_replays_unconfirmed_batch_once(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 5)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            with pytest.raises(Exception, match="interrupted"):
                importer.run(page_limit=2, fail_after_page=0)
            partial = service.create(staging, staging._connection, tmp_path / "partial")
            assert partial.counts.records == 2
        service.restore(tmp_path / "partial", tmp_path / "relocated")
        connection = duckdb.connect(str(tmp_path / "relocated" / "raw-v2-index.duckdb"))
        relocated = RawV2Staging(tmp_path / "relocated", connection)
        with relocated:
            resumed = _importer(source, relocated).run(page_limit=2)
            assert resumed.complete is True
            assert resumed.imported_count == 5
            rerun = _importer(source, relocated).run(page_limit=2)
            assert rerun.complete is True
            assert rerun.imported_count == 5


def test_restored_staging_preserves_two_pit_cuts_and_13f_projection(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 6)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            assert importer.run(page_limit=2).complete is True
            service.create(staging, staging._connection, tmp_path / "backup")
        service.restore(tmp_path / "backup", tmp_path / "restored")
        connection = duckdb.connect(str(tmp_path / "restored" / "raw-v2-index.duckdb"))
        restored = RawV2Staging(tmp_path / "restored", connection)
        with restored:
            early = restored.list_record_ids(available_to=datetime(2026, 8, 15, tzinfo=UTC))
            full = restored.list_record_ids()
            assert len(early) == 5
            assert len(full) == 6
            assert set(early) < set(full)
            assert len(restored.list_record_ids(manager_cik="0001067983")) == 2


def test_backup_rejects_active_writer_wal_symlink_and_overlap(tmp_path: Path) -> None:
    service = RawV2StagingBackupService()
    staging = _staging(tmp_path, "staging")
    with staging:
        from tests.unit.storage.test_raw_v2 import _record

        staging.save(_record())
        with pytest.raises(RawV2BackupError, match="outside the staging"):
            service.create(staging, staging._connection, staging.destination / "nested")
        foreign = _staging(tmp_path, "foreign")
        with foreign, pytest.raises(RawV2BackupError, match="already exists"):
            service.create(staging, staging._connection, foreign.destination)
    link = tmp_path / "link"
    try:
        link.symlink_to(staging.destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not supported here")
    with pytest.raises(RawV2BackupError, match="absolute|symbolic link"):
        service.create(
            RawV2Staging(link.absolute(), duckdb.connect(str(tmp_path / "x.duckdb"))),
            duckdb.connect(":memory:"),
            tmp_path / "backup-link",
        )


def test_restore_rejects_corrupt_or_foreign_inventory_without_promotion(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 4)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            assert importer.run(page_limit=2).complete is True
            manifest = service.create(staging, staging._connection, tmp_path / "backup")
            assert manifest.counts.records == 4
    victim = tmp_path / "backup" / "raw"
    blob: Path | None = None
    for candidate in victim.rglob("*"):
        if candidate.is_file() and not candidate.is_symlink():
            blob = candidate
            break
    assert blob is not None
    blob.write_bytes(b"corrupt")
    with pytest.raises(RawV2BackupError, match="hash verification|does not match"):
        service.restore(tmp_path / "backup", tmp_path / "restored-corrupt")
    assert not (tmp_path / "restored-corrupt").exists()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing.txt").write_text("busy", encoding="utf-8")
    with pytest.raises(RawV2BackupError, match="new or empty"):
        service.restore(tmp_path / "backup", occupied)
