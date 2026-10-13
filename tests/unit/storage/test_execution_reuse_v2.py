"""Strict cache-key tests for workspace execution reuse."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import duckdb

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
