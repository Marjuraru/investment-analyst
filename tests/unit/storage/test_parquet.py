"""Tests for controlled Parquet exports."""

import duckdb
import pytest

from investment_analyst.storage import RecordConflictError, StorageError

from .conftest import make_asset


def test_export_allowed_table_and_query_parquet(storage) -> None:
    asset = make_asset()
    storage.assets.upsert(asset)

    path = storage.parquet.export_table("assets")
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT asset_id, symbol FROM read_parquet(?)",
            [str(path)],
        ).fetchone()
    finally:
        connection.close()

    assert path.is_file()
    assert row == (asset.asset_id, asset.symbol)


def test_export_rejects_unlisted_table(storage) -> None:
    with pytest.raises(StorageError, match="not allowed"):
        storage.parquet.export_table("storage_metadata")


def test_export_does_not_overwrite_without_permission(storage) -> None:
    storage.assets.upsert(make_asset())
    path = storage.parquet.export_table("assets")

    with pytest.raises(RecordConflictError, match="already exists"):
        storage.parquet.export_table("assets", path)

    assert storage.parquet.export_table("assets", path, overwrite=True) == path
