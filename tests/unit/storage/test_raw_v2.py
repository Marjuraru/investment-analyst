"""Unit tests for the isolated raw v2 staging substrate."""

import json
from datetime import UTC, datetime, timedelta
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
    RawV2TemporalStamp,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

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


def test_bounded_spool_large_raw_exact_idempotent_conflict_and_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from investment_analyst.storage import raw_v2 as module
    from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

    staging, connection = _staging(tmp_path)
    record = _record(payload={"large": "雪😀\\\n" * 300_000, "integer": 10**80})
    expected = canonical_json_bytes(record)
    with staging:
        with monkeypatch.context() as patch:
            patch.setattr(module, "canonical_json_bytes", lambda _: pytest.fail("full bytes"))
            patch.setattr(Path, "read_bytes", lambda _: pytest.fail("unbounded blob read"))
            assert (
                staging.save_bounded(record, spool_directory=tmp_path / "spool").created_count == 1
            )
            assert (
                staging.save_bounded(record, spool_directory=tmp_path / "spool").reused_count == 1
            )
            assert staging.get_verified_bounded(record.record_id) == record
        assert staging.get(record.record_id) == record
        path = staging._resolve_blob(staging._blob_relative_path(sha256_hex(expected)).as_posix())
        assert path.read_bytes() == expected
        assert list((tmp_path / "spool").iterdir()) == []
        conflict = record.model_copy(update={"schema_version": "changed"})
        with pytest.raises(RecordConflictError):
            staging.save_bounded(conflict, spool_directory=tmp_path / "spool")
        with path.open("ab") as output:
            output.write(b" ")
        with pytest.raises(RawV2StagingError, match="checksum"):
            staging.save_bounded(record, spool_directory=tmp_path / "spool")
        with pytest.raises(RawV2StagingError, match="checksum"):
            staging.get_verified_bounded(record.record_id)
        assert connection.execute("SELECT count(*) FROM raw_v2_index").fetchone() == (1,)


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


def _temporal_stamp(record: RawRecord) -> RawV2TemporalStamp:
    return RawV2TemporalStamp(
        available_at=record.available_at,
        received_at=record.received_at,
        source_retrieved_at=record.source.retrieved_at,
    )


def _corrected(record: RawRecord, after: RawV2TemporalStamp) -> RawRecord:
    return record.model_copy(
        update={
            "available_at": after.available_at,
            "received_at": after.received_at,
            "source": record.source.model_copy(update={"retrieved_at": after.source_retrieved_at}),
        }
    )


def test_temporal_repair_is_narrow_recoverable_and_idempotent(tmp_path: Path) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        record = _record(
            payload={
                "document": {"accessionNumber": "0001140361-26-040126"},
                "body_sha256": "a" * 64,
            }
        )
        staging.save(record)
        before = _temporal_stamp(record)
        corrected_at = record.available_at - timedelta(days=1)
        after = RawV2TemporalStamp(
            available_at=corrected_at,
            received_at=corrected_at,
            source_retrieved_at=corrected_at,
        )
        repair = staging.plan_temporal_repair(record.record_id, before=before, after=after)
        original_row = connection.execute(
            "SELECT relative_path, checksum_sha256 FROM raw_v2_index WHERE record_id = ?",
            [str(record.record_id)],
        ).fetchone()
        assert original_row is not None
        assert repair.before_checksum_sha256 == original_row[1]
        assert repair.after_checksum_sha256 != repair.before_checksum_sha256
        assert repair.after_checksum_sha256 == sha256_hex(
            canonical_json_bytes(_corrected(record, after))
        )

        # Interrupted apply: the corrected blob exists but the index still points
        # to the prior address. The retry must complete without a partial state.
        staging._write_blob(
            repair.after_checksum_sha256,
            canonical_json_bytes(_corrected(record, after)),
        )
        receipt = staging.repair_temporal_metadata(repair)
        assert receipt.changed is True
        assert receipt.before_relative_path == original_row[0]
        assert receipt.after_relative_path != original_row[0]

        repaired = staging.get(record.record_id)
        assert repaired.available_at == corrected_at
        assert repaired.received_at == corrected_at
        assert repaired.source.retrieved_at == corrected_at
        assert repaired.event_time == record.event_time
        assert repaired.payload == record.payload
        assert repaired.source.checksum_sha256 == record.source.checksum_sha256
        assert repaired.asset_id == record.asset_id
        assert repaired.schema_version == record.schema_version
        assert repaired.record_id == record.record_id
        assert receipt.after_checksum_sha256 == sha256_hex(canonical_json_bytes(repaired))

        # The prior blob remains as immutable evidence.
        assert staging._resolve_blob(str(original_row[0])).is_file()
        assert staging.repair_temporal_metadata(repair).changed is False
        assert staging.get(record.record_id) == repaired
        assert connection.execute(
            "SELECT checksum_sha256 FROM raw_v2_index WHERE record_id = ?",
            [str(record.record_id)],
        ).fetchone() == (repair.after_checksum_sha256,)
    staging.close()


def test_temporal_repair_fails_closed_on_unbound_state(tmp_path: Path) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        record = _record()
        staging.save(record)
        before = _temporal_stamp(record)
        corrected_at = record.available_at - timedelta(days=1)
        after = RawV2TemporalStamp(
            available_at=corrected_at,
            received_at=corrected_at,
            source_retrieved_at=corrected_at,
        )
        wrong_before = RawV2TemporalStamp(
            available_at=record.available_at - timedelta(days=2),
            received_at=record.received_at,
            source_retrieved_at=record.source.retrieved_at,
        )
        with pytest.raises(RecordConflictError, match="prior state"):
            staging.plan_temporal_repair(record.record_id, before=wrong_before, after=after)
        with pytest.raises(RecordNotFoundError, match="was not found"):
            staging.plan_temporal_repair(uuid4(), before=before, after=after)

        repair = staging.plan_temporal_repair(record.record_id, before=before, after=after)
        tampered_after = repair.model_copy(update={"after_checksum_sha256": "0" * 64})
        with pytest.raises(RecordConflictError, match="authorized checksum"):
            staging.repair_temporal_metadata(tampered_after)
        tampered_before = repair.model_copy(update={"before_checksum_sha256": "1" * 64})
        with pytest.raises(RecordConflictError, match="prior and corrected state"):
            staging.repair_temporal_metadata(tampered_before)
        assert staging.get(record.record_id) == record
        assert connection.execute(
            "SELECT checksum_sha256 FROM raw_v2_index WHERE record_id = ?",
            [str(record.record_id)],
        ).fetchone() == (repair.before_checksum_sha256,)
    staging.close()
