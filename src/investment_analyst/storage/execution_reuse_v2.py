"""Typed, fail-closed persistence for reusable analytical executions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class ExecutionReuseV2Error(RuntimeError):
    """Raised when a reusable execution cannot be matched exactly."""


class ExecutionReuseKey(ContractModel):
    """Semantic inputs that must all match before reusing a persisted result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family: NonEmptyStr
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: NonEmptyStr
    algorithm_version: NonEmptyStr
    evidence_digest: NonEmptyStr
    parameters_digest: NonEmptyStr


class ExecutionReuseEntry(ContractModel):
    """One available result bound to a complete reusable-execution key."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: ExecutionReuseKey
    result_id: UUID
    available_at: UTCDateTime
    stored_at: UTCDateTime


class ExecutionReuseBundleEntry(ContractModel):
    """One complete ordered result set bound to exact execution semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: ExecutionReuseKey
    result_ids: tuple[UUID, ...]
    outcome_json: NonEmptyStr
    available_at: UTCDateTime
    stored_at: UTCDateTime


def canonical_digest(value: dict[str, object]) -> str:
    """Return a deterministic digest for a JSON-compatible parameter mapping."""
    try:
        payload = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ExecutionReuseV2Error("execution reuse parameters are not canonical JSON") from error
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExecutionReuseV2Store:
    """Store reusable result bindings without making them result identity."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def ensure(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_execution_reuse_v2 (
                family VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                algorithm_version VARCHAR NOT NULL,
                evidence_digest VARCHAR NOT NULL,
                parameters_digest VARCHAR NOT NULL,
                result_id VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                stored_at VARCHAR NOT NULL,
                PRIMARY KEY (
                    family, asset_id, source_id, frequency, algorithm_version,
                    evidence_digest, parameters_digest
                )
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_execution_reuse_bundle_outcomes_v2 (
                family VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                algorithm_version VARCHAR NOT NULL,
                evidence_digest VARCHAR NOT NULL,
                parameters_digest VARCHAR NOT NULL,
                outcome_json VARCHAR NOT NULL,
                PRIMARY KEY (
                    family, asset_id, source_id, frequency, algorithm_version,
                    evidence_digest, parameters_digest
                )
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_execution_reuse_bundles_v2 (
                family VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                algorithm_version VARCHAR NOT NULL,
                evidence_digest VARCHAR NOT NULL,
                parameters_digest VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                stored_at VARCHAR NOT NULL,
                PRIMARY KEY (
                    family, asset_id, source_id, frequency, algorithm_version,
                    evidence_digest, parameters_digest
                )
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_execution_reuse_bundle_results_v2 (
                family VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                algorithm_version VARCHAR NOT NULL,
                evidence_digest VARCHAR NOT NULL,
                parameters_digest VARCHAR NOT NULL,
                ordinal INTEGER NOT NULL,
                result_id VARCHAR NOT NULL,
                PRIMARY KEY (
                    family, asset_id, source_id, frequency, algorithm_version,
                    evidence_digest, parameters_digest, ordinal
                )
            )
            """
        )

    def save(self, entry: ExecutionReuseEntry) -> None:
        self.ensure()
        existing = self.find(entry.key, known_at=entry.available_at)
        if existing is not None and existing != entry:
            raise ExecutionReuseV2Error("execution reuse key conflicts with an existing result")
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO workspace_execution_reuse_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.key.family,
                    entry.key.asset_id,
                    entry.key.source_id,
                    entry.key.frequency,
                    entry.key.algorithm_version,
                    entry.key.evidence_digest,
                    entry.key.parameters_digest,
                    str(entry.result_id),
                    entry.available_at.astimezone(UTC).isoformat(),
                    entry.stored_at.astimezone(UTC).isoformat(),
                ),
            )

    def find(self, key: ExecutionReuseKey, *, known_at: datetime) -> ExecutionReuseEntry | None:
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ExecutionReuseV2Error("execution reuse known_at must be timezone-aware")
        self.ensure()
        row = self._connection.execute(
            """
            SELECT result_id, available_at, stored_at
            FROM workspace_execution_reuse_v2
            WHERE family = ? AND asset_id = ? AND source_id = ? AND frequency = ?
              AND algorithm_version = ? AND evidence_digest = ? AND parameters_digest = ?
              AND available_at <= ?
            """,
            (
                key.family,
                key.asset_id,
                key.source_id,
                key.frequency,
                key.algorithm_version,
                key.evidence_digest,
                key.parameters_digest,
                known_at.astimezone(UTC).isoformat(),
            ),
        ).fetchone()
        if row is None:
            return None
        return ExecutionReuseEntry(
            key=key,
            result_id=UUID(str(row[0])),
            available_at=datetime.fromisoformat(str(row[1])),
            stored_at=datetime.fromisoformat(str(row[2])),
        )

    def save_bundle(self, entry: ExecutionReuseBundleEntry) -> None:
        """Persist one complete result set or reject a conflicting binding."""
        self.ensure()
        if len(set(entry.result_ids)) != len(entry.result_ids):
            raise ExecutionReuseV2Error("execution reuse result set contains duplicate result IDs")
        _validate_canonical_outcome(entry.outcome_json)
        existing = self._find_bundle_exact(entry.key)
        if existing is not None and existing != entry:
            raise ExecutionReuseV2Error("execution reuse key conflicts with an existing result set")
        if existing is not None:
            return
        values = self._key_values(entry.key)
        self._connection.execute(
            """
            INSERT INTO workspace_execution_reuse_bundles_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *values,
                entry.available_at.astimezone(UTC).isoformat(),
                entry.stored_at.astimezone(UTC).isoformat(),
            ),
        )
        for ordinal, result_id in enumerate(entry.result_ids):
            self._connection.execute(
                """
                INSERT INTO workspace_execution_reuse_bundle_results_v2 (
                    family, asset_id, source_id, frequency, algorithm_version,
                    evidence_digest, parameters_digest, ordinal, result_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, ordinal, str(result_id)),
            )
        self._connection.execute(
            """
            INSERT INTO workspace_execution_reuse_bundle_outcomes_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, entry.outcome_json),
        )

    def find_bundle(
        self, key: ExecutionReuseKey, *, known_at: datetime
    ) -> ExecutionReuseBundleEntry | None:
        """Return a PIT-visible complete result set, or ``None`` on a miss."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ExecutionReuseV2Error("execution reuse known_at must be timezone-aware")
        self.ensure()
        values = self._key_values(key)
        row = self._connection.execute(
            """
            SELECT bundle.available_at, bundle.stored_at, outcome.outcome_json
            FROM workspace_execution_reuse_bundles_v2 AS bundle
            INNER JOIN workspace_execution_reuse_bundle_outcomes_v2 AS outcome
              USING (
                  family, asset_id, source_id, frequency, algorithm_version,
                  evidence_digest, parameters_digest
              )
            WHERE bundle.family = ? AND bundle.asset_id = ? AND bundle.source_id = ?
              AND bundle.frequency = ? AND bundle.algorithm_version = ?
              AND bundle.evidence_digest = ? AND bundle.parameters_digest = ?
              AND bundle.available_at <= ?
            """,
            (*values, known_at.astimezone(UTC).isoformat()),
        ).fetchone()
        if row is None:
            return None
        try:
            _validate_canonical_outcome(str(row[2]))
        except ExecutionReuseV2Error:
            return None
        result_rows = self._connection.execute(
            """
            SELECT result_id
            FROM workspace_execution_reuse_bundle_results_v2
            WHERE family = ? AND asset_id = ? AND source_id = ? AND frequency = ?
              AND algorithm_version = ? AND evidence_digest = ? AND parameters_digest = ?
            ORDER BY ordinal
            """,
            values,
        ).fetchall()
        return ExecutionReuseBundleEntry(
            key=key,
            result_ids=tuple(UUID(str(item[0])) for item in result_rows),
            available_at=datetime.fromisoformat(str(row[0])),
            stored_at=datetime.fromisoformat(str(row[1])),
            outcome_json=str(row[2]),
        )

    def _find_bundle_exact(self, key: ExecutionReuseKey) -> ExecutionReuseBundleEntry | None:
        """Return a bundle regardless of its availability cut for conflict detection."""
        values = self._key_values(key)
        row = self._connection.execute(
            """
            SELECT bundle.available_at, bundle.stored_at, outcome.outcome_json
            FROM workspace_execution_reuse_bundles_v2 AS bundle
            INNER JOIN workspace_execution_reuse_bundle_outcomes_v2 AS outcome
              USING (
                  family, asset_id, source_id, frequency, algorithm_version,
                  evidence_digest, parameters_digest
              )
            WHERE bundle.family = ? AND bundle.asset_id = ? AND bundle.source_id = ?
              AND bundle.frequency = ? AND bundle.algorithm_version = ?
              AND bundle.evidence_digest = ? AND bundle.parameters_digest = ?
            """,
            values,
        ).fetchone()
        if row is None:
            return None
        result_rows = self._connection.execute(
            """
            SELECT result_id
            FROM workspace_execution_reuse_bundle_results_v2
            WHERE family = ? AND asset_id = ? AND source_id = ? AND frequency = ?
              AND algorithm_version = ? AND evidence_digest = ? AND parameters_digest = ?
            ORDER BY ordinal
            """,
            values,
        ).fetchall()
        return ExecutionReuseBundleEntry(
            key=key,
            result_ids=tuple(UUID(str(item[0])) for item in result_rows),
            available_at=datetime.fromisoformat(str(row[0])),
            stored_at=datetime.fromisoformat(str(row[1])),
            outcome_json=str(row[2]),
        )

    @staticmethod
    def _key_values(key: ExecutionReuseKey) -> tuple[str, str, str, str, str, str, str]:
        return (
            key.family,
            key.asset_id,
            key.source_id,
            key.frequency,
            key.algorithm_version,
            key.evidence_digest,
            key.parameters_digest,
        )


def _validate_canonical_outcome(outcome_json: str) -> None:
    try:
        outcome = json.loads(outcome_json)
    except json.JSONDecodeError as error:
        raise ExecutionReuseV2Error("execution reuse outcome is not valid JSON") from error
    if not isinstance(outcome, dict):
        raise ExecutionReuseV2Error("execution reuse outcome must be a JSON object")
    canonical = json.dumps(outcome, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if canonical != outcome_json:
        raise ExecutionReuseV2Error("execution reuse outcome is not canonical JSON")


__all__ = [
    "ExecutionReuseEntry",
    "ExecutionReuseBundleEntry",
    "ExecutionReuseKey",
    "ExecutionReuseV2Error",
    "ExecutionReuseV2Store",
    "canonical_digest",
]
