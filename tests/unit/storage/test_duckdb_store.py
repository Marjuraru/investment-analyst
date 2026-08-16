"""Tests for DuckDB initialization and connection ownership."""

import duckdb
import pytest

from investment_analyst.storage import (
    DuckDBStore,
    StorageError,
    StoragePaths,
    StorageSchemaError,
)


def test_database_initializes_schema_version_one(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path)

    with DuckDBStore(paths) as store:
        version = store.connection.execute(
            "SELECT metadata_value FROM storage_metadata WHERE metadata_key = ?",
            ["schema_version"],
        ).fetchone()
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }

    assert paths.database_path.is_file()
    assert version == ("1",)
    assert {
        "assets",
        "source_definitions",
        "raw_record_index",
        "normalized_observations",
        "metric_definitions",
        "metric_results",
        "diagnostic_results",
    }.issubset(tables)


def test_store_closes_owned_connection(tmp_path) -> None:
    store = DuckDBStore(StoragePaths.from_root(tmp_path))

    with store:
        connection = store.connection
        assert store.is_open

    assert not store.is_open
    with pytest.raises(StorageError, match="not open"):
        _ = store.connection
    with pytest.raises(duckdb.ConnectionException):
        connection.execute("SELECT 1")


def test_read_only_store_does_not_initialize_an_absent_database(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path / "absent")

    with pytest.raises(StorageError, match="does not exist"):
        DuckDBStore(paths, read_only=True).open()

    assert not paths.root.exists()


def test_store_rejects_incompatible_schema_version(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    paths.processed_dir.mkdir(parents=True)
    connection = duckdb.connect(str(paths.database_path))
    try:
        connection.execute(
            """
            CREATE TABLE storage_metadata (
                metadata_key VARCHAR PRIMARY KEY,
                metadata_value VARCHAR NOT NULL,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO storage_metadata (metadata_key, metadata_value) VALUES (?, ?)",
            ["schema_version", "2"],
        )
    finally:
        connection.close()

    with pytest.raises(StorageSchemaError, match="unsupported storage schema version"):
        DuckDBStore(paths).open()


def test_read_only_store_shares_a_process_writer_snapshot_and_rejects_mutation(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with DuckDBStore(paths) as writer:
        writer.connection.execute("CREATE TABLE concurrent_read_test (value INTEGER PRIMARY KEY)")
        writer.connection.execute("INSERT INTO concurrent_read_test VALUES (1)")
        writer.connection.execute("BEGIN TRANSACTION")
        writer.connection.execute("INSERT INTO concurrent_read_test VALUES (2)")

        reader = DuckDBStore(paths, read_only=True).open()
        try:
            assert reader.connection.execute(
                "SELECT value FROM concurrent_read_test"
            ).fetchall() == [(1,)]
            with pytest.raises(duckdb.Error):
                reader.connection.execute("INSERT INTO concurrent_read_test VALUES (3)")
            with pytest.raises(duckdb.Error):
                reader.connection.execute("CREATE TABLE reader_must_not_write (value INTEGER)")
        finally:
            reader.close()

        writer.connection.execute("COMMIT")
        with DuckDBStore(paths, read_only=True) as after_commit:
            assert after_commit.connection.execute(
                "SELECT value FROM concurrent_read_test ORDER BY value"
            ).fetchall() == [(1,), (2,)]
