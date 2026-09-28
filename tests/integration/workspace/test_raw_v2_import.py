"""Integration tests for raw v1 to v2 import from a restored verified backup."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.raw_v2 import RawV2Staging, RawV2StagingError
from investment_analyst.storage.raw_v2_import import RawV2Importer, RawV2ImportError
from investment_analyst.workspace.backup import WorkspaceBackupService
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_TIMESTAMP = datetime(2026, 8, 1, tzinfo=UTC)


def _service(tmp_path: Path):
    workspace_service = WorkspaceService(
        environ={},
        home=tmp_path / "home",
        clock=lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )
    return workspace_service, WorkspaceBackupService(
        workspace_service,
        clock=lambda: datetime(2026, 8, 1, hour=1, tzinfo=UTC),
    )


def _seed_raw(source: Path, workspace_service: WorkspaceService, count: int) -> list[UUID]:
    raw_ids = [UUID(int=index + 1) for index in range(count)]
    writer = workspace_service.open_storage(
        workspace_service.resolve(source),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        for index, record_id in enumerate(raw_ids):
            writer.raw_records.save(
                RawRecord(
                    record_id=record_id,
                    asset_id="equity:us:aapl" if index % 2 == 0 else "crypto:btc-usd",
                    source=SourceReference(
                        source_id="test:import",
                        record_key=f"import-{index}",
                        retrieved_at=_TIMESTAMP,
                    ),
                    event_time=_TIMESTAMP,
                    available_at=_TIMESTAMP,
                    received_at=_TIMESTAMP,
                    payload={"value": str(index)},
                    schema_version="import-v1",
                )
            )
    finally:
        writer.close()
    return raw_ids


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _fingerprint(source: LocalStorage) -> str:
    rows = source.store.connection.execute(
        "SELECT record_id, checksum_sha256 FROM raw_record_index ORDER BY record_id"
    ).fetchall()
    import hashlib

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


def test_restored_v1_to_raw_v2_preserves_all_raw_ids_bytes_and_pit(tmp_path: Path) -> None:
    workspace_service, backup_service = _service(tmp_path)
    source = tmp_path / "source"
    workspace_service.initialize(source)
    raw_ids = _seed_raw(source, workspace_service, 5)
    backup_dir = tmp_path / "backup"
    backup_service.create(source, backup_dir)
    restored = tmp_path / "restored"
    inspection = backup_service.restore(backup_dir, restored)
    assert inspection.status == "ready"

    with workspace_service.open_storage(
        workspace_service.resolve(restored), WorkspaceAccessMode.READ_ONLY
    ) as source_storage:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source_storage, staging)
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            assert summary.imported_count == 5
            assert summary.max_page_requested == 2
            assert summary.max_page_hydrated == 2
            assert summary.counts_by_source == {"test:import": 5}
            assert summary.traceability_verified is True
            staged = staging.get_many(raw_ids)
            announced = source_storage.raw_records.get_many(raw_ids)
            assert staged == announced
            cutoff = datetime(2026, 8, 1, tzinfo=UTC)
            staged_cut = staging.list_record_ids(available_to=cutoff)
            source_cut = source_storage.raw_records.list_record_ids(available_to=cutoff)
            assert staged_cut == source_cut
            rerun = importer.run(page_limit=2)
            assert rerun.complete is True
            assert rerun.imported_count == 5


def test_corrupt_source_target_or_checkpoint_fails_closed(tmp_path: Path) -> None:
    workspace_service, backup_service = _service(tmp_path)
    source = tmp_path / "source"
    workspace_service.initialize(source)
    raw_ids = _seed_raw(source, workspace_service, 3)
    backup_dir = tmp_path / "backup"
    backup_service.create(source, backup_dir)
    restored = tmp_path / "restored"
    backup_service.restore(backup_dir, restored)

    with workspace_service.open_storage(
        workspace_service.resolve(restored), WorkspaceAccessMode.READ_ONLY
    ) as source_storage:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source_storage, staging)
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            state_path = staging.destination / "raw-v2-import-state.json"
            state_path.write_bytes(state_path.read_bytes() + b"truncated")
            with pytest.raises(RawV2ImportError, match="truncated"):
                importer.run(page_limit=2)

    with workspace_service.open_storage(
        workspace_service.resolve(restored), WorkspaceAccessMode.READ_ONLY
    ) as source_storage:
        fresh = _staging(tmp_path, "fresh")
        with fresh:
            importer = _importer(source_storage, fresh)
            importer.run(page_limit=2)
            row = fresh._connection.execute(
                "SELECT relative_path FROM raw_v2_index WHERE record_id = ?",
                [str(raw_ids[0])],
            ).fetchone()
            assert row is not None
            blob = fresh.destination / "raw" / Path(row[0])
            blob.write_text('{"tampered":true}', encoding="utf-8")
            with pytest.raises(
                (RawV2ImportError, RawV2StagingError), match="differs|checksum|complete"
            ):
                importer.run(page_limit=2)


def test_import_rejects_writable_source_or_overlapping_destination_and_preserves_v1(
    tmp_path: Path,
) -> None:
    workspace_service, backup_service = _service(tmp_path)
    source = tmp_path / "source"
    workspace_service.initialize(source)
    raw_ids = _seed_raw(source, workspace_service, 2)
    tree_before = sorted(
        str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()
    )

    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as writable:
        staging = _staging(tmp_path, "staging")
        with staging, pytest.raises(RawV2ImportError, match="read-only"):
            RawV2Importer(
                writable,
                staging,
                source_workspace_id="workspace-2",
                source_fingerprint="fingerprint-2",
            )

    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_ONLY
    ) as source_storage:
        overlapping = RawV2Staging(source, duckdb.connect(str(tmp_path / "overlapping.duckdb")))
        with pytest.raises(RawV2ImportError, match="disjoint|v1 workspace"):
            _importer(source_storage, overlapping).run(page_limit=2)

    tree_after = sorted(
        str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()
    )
    assert tree_after == tree_before
    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_ONLY
    ) as source_storage:
        assert sorted(source_storage.raw_records.list_record_ids()) == sorted(raw_ids)
