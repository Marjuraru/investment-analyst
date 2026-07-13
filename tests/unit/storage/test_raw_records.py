"""Tests for immutable canonical raw-record files."""

from pathlib import Path

import pytest

from investment_analyst.storage import RecordConflictError, StorageError

from .conftest import make_raw_record


def _indexed_path(storage, record_id) -> Path:
    row = storage.store.connection.execute(
        "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
        [str(record_id)],
    ).fetchone()
    assert row is not None
    return storage.paths.raw_dir / row[0]


def test_save_and_recover_raw_record(storage) -> None:
    record = make_raw_record()

    storage.raw_records.save(record)
    recovered = storage.raw_records.get(record.record_id)

    assert recovered == record
    assert _indexed_path(storage, record.record_id).is_file()


def test_raw_path_is_partitioned_and_safe(storage) -> None:
    record = make_raw_record(source_id="../../market/../../../escape")

    storage.raw_records.save(record)
    path = _indexed_path(storage, record.record_id).resolve()
    raw_root = storage.paths.raw_dir.resolve()
    relative = path.relative_to(raw_root)

    assert path.is_relative_to(raw_root)
    assert ".." not in relative.parts
    assert relative.parts[0].startswith("source=")
    assert relative.parts[1] == "received_date=2026-07-10"


def test_checksum_detects_raw_file_modification(storage) -> None:
    record = make_raw_record()
    storage.raw_records.save(record)
    path = _indexed_path(storage, record.record_id)
    path.write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(StorageError, match="checksum mismatch"):
        storage.raw_records.get(record.record_id)


def test_raw_save_is_idempotent_for_identical_content(storage) -> None:
    record = make_raw_record()

    storage.raw_records.save(record)
    storage.raw_records.save(record)
    count = storage.store.connection.execute(
        "SELECT count(*) FROM raw_record_index WHERE record_id = ?",
        [str(record.record_id)],
    ).fetchone()

    assert count == (1,)


def test_raw_save_rejects_same_id_with_different_content(storage) -> None:
    record = make_raw_record()
    storage.raw_records.save(record)
    conflicting = record.model_copy(update={"schema_version": "2"})

    with pytest.raises(RecordConflictError, match="different content"):
        storage.raw_records.save(conflicting)
