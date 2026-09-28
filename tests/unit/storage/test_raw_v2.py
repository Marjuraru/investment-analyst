"""Unit tests for the isolated raw v2 staging substrate."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.storage.errors import RecordConflictError, RecordNotFoundError
from investment_analyst.storage.raw_v2 import (
    STAGING_FORMAT,
    RawV2Staging,
    RawV2StagingError,
    RawV2StagingMarker,
)

_AVAILABLE = datetime(2026, 7, 10, 16, 1, tzinfo=UTC)
_RECEIVED = datetime(2026, 7, 10, 16, 3, tzinfo=UTC)


def _record(*, record_id=None, payload=None, schema_version="unit-v1") -> RawRecord:
    return RawRecord(
        record_id=record_id or uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="test:staging",
            record_key="staging-fixture",
            retrieved_at=_RECEIVED,
        ),
        event_time=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        available_at=_AVAILABLE,
        received_at=_RECEIVED,
        payload=payload if payload is not None else {"close": "210.50"},
        schema_version=schema_version,
    )


def _staging(tmp_path: Path, name: str = "staging") -> tuple[RawV2Staging, object]:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection), connection


def _read_only_staging(destination: Path) -> RawV2Staging:
    return RawV2Staging(
        destination, duckdb.connect(str(destination / "raw-v2-index.duckdb")), read_only=True
    )


def test_staging_save_many_is_idempotent_and_bounded(tmp_path: Path) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        records = [_record() for _ in range(3)]
        receipt = staging.save_many(records)
        assert receipt.created_count == 3
        assert receipt.reused_count == 0
        assert staging.get_many([record.record_id for record in records]) == {
            record.record_id: record for record in records
        }
        repeated = staging.save_many(records)
        assert repeated.created_count == 0
        assert repeated.reused_count == 3
        assert staging.save_many([]).total_count == 0
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'raw_v2_index'"
            ).fetchall()
        }
        assert "document_json" not in columns
        conflict = records[0].model_copy(update={"schema_version": "other-v1"})
        with pytest.raises(RecordConflictError, match="different content"):
            staging.save(conflict)
        assert staging.get(records[0].record_id) == records[0]
    staging.close()


def test_staging_read_only_never_initializes_or_writes(tmp_path: Path) -> None:
    destination = (tmp_path / "staging").absolute()
    read_only = RawV2Staging(destination, duckdb.connect(":memory:"), read_only=True)
    with pytest.raises(RawV2StagingError, match="marker"):
        read_only.open()
    assert not (destination / "raw-v2-staging.json").exists()

    staging, _ = _staging(tmp_path)
    with staging:
        record = _record()
        staging.save(record)
        marker_before = (destination / "raw-v2-staging.json").read_bytes()
    staging.close()

    reopened = _read_only_staging(destination)
    with reopened:
        assert reopened.get(record.record_id) == record
        with pytest.raises(RawV2StagingError, match="read-only"):
            reopened.save(_record())
        assert (destination / "raw-v2-staging.json").read_bytes() == marker_before
    reopened.close()


def test_staging_rejects_foreign_destinations_and_second_writer(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "storage").mkdir(parents=True)
    (tmp_path / "workspace" / "manifest.json").write_text("{}", encoding="utf-8")
    foreign = RawV2Staging(
        (tmp_path / "workspace").absolute(),
        duckdb.connect(str(tmp_path / "foreign.duckdb")),
    )
    with pytest.raises(RawV2StagingError, match="v1 workspace"):
        foreign.open()

    with pytest.raises(RawV2StagingError, match="absolute"):
        RawV2Staging(Path("relative/staging"), duckdb.connect(":memory:"))

    staging, _ = _staging(tmp_path)
    with staging:
        duplicate = RawV2Staging(
            staging._destination,
            duckdb.connect(str(tmp_path / "duplicate.duckdb")),
        )
        with pytest.raises(RawV2StagingError, match="already has a writer"):
            duplicate.open()
        marker = RawV2StagingMarker.model_validate(
            json.loads((staging._destination / "raw-v2-staging.json").read_text())
        )
        assert marker.format == STAGING_FORMAT
    staging.close()


def test_staging_missing_and_tampered_records_fail_closed(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    with staging:
        with pytest.raises(RecordNotFoundError, match="was not found"):
            staging.get(uuid4())
        with pytest.raises(RecordNotFoundError, match="was not found"):
            staging.get_many([uuid4()])
        record = _record(payload={"report": {"manager_cik": "0001067983"}})
        staging.save(record)
        row = staging._connection.execute(
            "SELECT relative_path, checksum_sha256 FROM raw_v2_index WHERE record_id = ?",
            [str(record.record_id)],
        ).fetchone()
        assert row is not None
        blob = staging._destination / "raw" / Path(row[0])
        blob.write_text('{"tampered":true}', encoding="utf-8")
        with pytest.raises(RawV2StagingError, match="checksum mismatch"):
            staging.get(record.record_id)
        staging._connection.execute(
            "UPDATE raw_v2_index SET projected_manager_cik = ? WHERE record_id = ?",
            ["0000000000", str(record.record_id)],
        )
        with pytest.raises(RawV2StagingError, match="checksum mismatch"):
            staging.get(record.record_id)
    staging.close()


def test_import_inventory_pages_are_bounded_and_verified(tmp_path: Path) -> None:
    from investment_analyst.storage.raw_v2 import RawV2StagingError

    staging, _ = _staging(tmp_path)
    with staging:
        records = [_record() for _ in range(3)]
        staging.save_many(records)
        ordered = sorted((record.record_id for record in records), key=str)
        assert staging.list_inventory_page(limit=256) == ordered
        assert staging.list_inventory_page(limit=2) == ordered[:2]
        last = staging.get(ordered[1])
        assert (
            staging.list_inventory_page(
                limit=2, after_received_at=last.received_at, after_record_id=ordered[1]
            )
            == ordered[2:]
        )
        assert (
            staging.list_inventory_page(
                limit=256,
                after_received_at=staging.get(ordered[2]).received_at,
                after_record_id=ordered[2],
            )
            == []
        )
        with pytest.raises(RawV2StagingError, match="between 1 and 256"):
            staging.list_inventory_page(limit=257)
        with pytest.raises(RawV2StagingError, match="together"):
            staging.list_inventory_page(limit=2, after_received_at=last.received_at)
    staging.close()
