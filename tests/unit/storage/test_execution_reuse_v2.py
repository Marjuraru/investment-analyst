"""Strict cache-key tests for workspace execution reuse."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import duckdb
import pytest

from investment_analyst.storage.execution_reuse_v2 import (
    ExecutionReuseBundleEntry,
    ExecutionReuseEntry,
    ExecutionReuseKey,
    ExecutionReuseV2Error,
    ExecutionReuseV2Store,
    canonical_digest,
)

_MOMENT = datetime(2026, 10, 7, tzinfo=UTC)


def _key(*, evidence: str = "evidence") -> ExecutionReuseKey:
    return ExecutionReuseKey(
        family="market",
        asset_id="equity:us:aapl",
        source_id="fixture:market",
        frequency="1d",
        algorithm_version="market-v1",
        evidence_digest=evidence,
        parameters_digest=canonical_digest({"window": 20}),
    )


def test_reuse_requires_exact_key_and_available_cut() -> None:
    connection = duckdb.connect(":memory:")
    store = ExecutionReuseV2Store(connection)
    entry = ExecutionReuseEntry(
        key=_key(),
        result_id=uuid5(NAMESPACE_URL, "execution-reuse-result"),
        available_at=_MOMENT,
        stored_at=_MOMENT,
    )
    store.save(entry)

    assert store.find(_key(), known_at=_MOMENT - timedelta(microseconds=1)) is None
    assert store.find(_key(evidence="revised"), known_at=_MOMENT) is None
    assert store.find(_key(), known_at=_MOMENT) == entry


def test_bundle_reuse_requires_complete_exact_result_set() -> None:
    connection = duckdb.connect(":memory:")
    store = ExecutionReuseV2Store(connection)
    result_ids = (
        uuid5(NAMESPACE_URL, "execution-reuse-bundle-first"),
        uuid5(NAMESPACE_URL, "execution-reuse-bundle-second"),
    )
    entry = ExecutionReuseBundleEntry(
        key=_key(),
        result_ids=result_ids,
        outcome_json='{"missing_requirements":[]}',
        available_at=_MOMENT,
        stored_at=_MOMENT,
    )
    store.save_bundle(entry)

    assert store.find_bundle(_key(), known_at=_MOMENT - timedelta(microseconds=1)) is None
    assert store.find_bundle(_key(evidence="revised"), known_at=_MOMENT) is None
    assert store.find_bundle(_key(), known_at=_MOMENT) == entry

    connection.execute(
        """
        UPDATE workspace_execution_reuse_bundle_outcomes_v2
        SET outcome_json = '{"missing_requirements": []}'
        """
    )
    assert store.find_bundle(_key(), known_at=_MOMENT) is None
    connection.execute(
        """
        UPDATE workspace_execution_reuse_bundle_outcomes_v2
        SET outcome_json = '{"missing_requirements":[]}'
        """
    )

    conflicting = entry.model_copy(update={"available_at": _MOMENT + timedelta(days=1)})
    try:
        store.save_bundle(conflicting)
    except ExecutionReuseV2Error as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("a key cannot bind two different result sets")

    duplicate = entry.model_copy(update={"result_ids": (result_ids[0], result_ids[0])})
    try:
        store.save_bundle(duplicate)
    except ExecutionReuseV2Error as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("a result set cannot contain the same result twice")


def test_find_does_not_initialize_tables_on_a_fresh_connection() -> None:
    connection = duckdb.connect(":memory:")
    store = ExecutionReuseV2Store(connection)

    assert store.find(_key(), known_at=_MOMENT) is None
    assert store.find_bundle(_key(), known_at=_MOMENT) is None
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    }
    assert not any(name.startswith("workspace_execution_reuse") for name in tables)


def test_read_only_absent_layout_is_a_cache_miss_without_ddl(tmp_path: Path) -> None:
    database = tmp_path / "reuse.duckdb"
    writer = duckdb.connect(str(database))
    writer.execute("CREATE TABLE unrelated (value INTEGER)")
    writer.close()
    before = database.read_bytes()

    connection = duckdb.connect(str(database), read_only=True)
    try:
        store = ExecutionReuseV2Store(connection)
        assert store.find(_key(), known_at=_MOMENT) is None
        assert store.find_bundle(_key(), known_at=_MOMENT) is None
        with pytest.raises(ExecutionReuseV2Error, match="writable"):
            store.ensure()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
        assert not any(name.startswith("workspace_execution_reuse") for name in tables)
    finally:
        connection.close()
    assert database.read_bytes() == before


def test_read_only_complete_layout_preserves_pit_order_and_does_not_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reuse.duckdb"
    writer = duckdb.connect(str(database))
    store = ExecutionReuseV2Store(writer)
    entry = ExecutionReuseEntry(
        key=_key(),
        result_id=uuid5(NAMESPACE_URL, "execution-reuse-read-only-result"),
        available_at=_MOMENT,
        stored_at=_MOMENT,
    )
    result_ids = (
        uuid5(NAMESPACE_URL, "execution-reuse-read-only-first"),
        uuid5(NAMESPACE_URL, "execution-reuse-read-only-second"),
    )
    bundle = ExecutionReuseBundleEntry(
        key=_key(),
        result_ids=result_ids,
        outcome_json='{"missing_requirements":[]}',
        available_at=_MOMENT,
        stored_at=_MOMENT,
    )
    store.save(entry)
    store.save_bundle(bundle)
    writer.close()
    before = database.read_bytes()

    connection = duckdb.connect(str(database), read_only=True)
    try:
        store = ExecutionReuseV2Store(connection)
        assert store.find(_key(), known_at=_MOMENT - timedelta(microseconds=1)) is None
        assert store.find(_key(), known_at=_MOMENT) == entry
        assert store.find(_key(), known_at=_MOMENT + timedelta(days=1)) == entry
        assert store.find(_key(evidence="revised"), known_at=_MOMENT) is None
        assert store.find_bundle(_key(), known_at=_MOMENT - timedelta(microseconds=1)) is None
        assert store.find_bundle(_key(), known_at=_MOMENT) == bundle
        assert store.find_bundle(_key(), known_at=_MOMENT + timedelta(days=1)) == bundle
        assert store.find_bundle(_key(evidence="revised"), known_at=_MOMENT) is None
        assert store.find_bundle(_key(), known_at=_MOMENT).result_ids == result_ids
        with pytest.raises(ExecutionReuseV2Error, match="writable"):
            store.ensure()
        with pytest.raises(ExecutionReuseV2Error, match="writable"):
            store.save(entry)
        with pytest.raises(ExecutionReuseV2Error, match="writable"):
            store.save_bundle(bundle)
    finally:
        connection.close()
    assert database.read_bytes() == before


def test_read_only_incomplete_layout_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "reuse.duckdb"
    writer = duckdb.connect(str(database))
    writer.execute("CREATE TABLE workspace_execution_reuse_v2 (family VARCHAR)")
    writer.close()

    connection = duckdb.connect(str(database), read_only=True)
    try:
        store = ExecutionReuseV2Store(connection)
        with pytest.raises(ExecutionReuseV2Error, match="incomplete"):
            store.find(_key(), known_at=_MOMENT)
        with pytest.raises(ExecutionReuseV2Error, match="incomplete"):
            store.find_bundle(_key(), known_at=_MOMENT)
        with pytest.raises(ExecutionReuseV2Error, match="writable"):
            store.ensure()
    finally:
        connection.close()
