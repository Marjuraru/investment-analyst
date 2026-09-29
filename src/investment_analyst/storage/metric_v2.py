"""Typed metric result v2 staging table with verified semantic lineage.

Metrics live in the same file-backed DuckDB index as the raw v2 staging
under the same writer lock, in their own ``metric_results_v2`` table with
ordered observation/metric link tables. Every coordinate of
``MetricResult`` is stored in a typed column: ``Decimal`` values use their
canonical text form, semantic parameters travel as canonical JSON and the
v2 identity is recalculated on every read. No ``document_json`` column
exists and no repeated 720-identifier array is copied per row: hourly
lineage may reference one persisted evidence set verified against its own
tables. Reads rehydrate the strict model, verify the recalculated UUIDv8
identity, confirm inputs by identifier, asset and availability, and fail
closed on conflict, cycle, future or foreign reference.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from duckdb import DuckDBPyConnection
from pydantic import JsonValue

from investment_analyst.analytics.metric_identity_v2 import (
    filter_semantic_parameters,
    metric_result_id_from_model_v2,
    resolve_metric_identity_version,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import MetricResult
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)

METRIC_V2_TABLE = "metric_results_v2"
METRIC_V2_OBSERVATION_LINKS_TABLE = "metric_v2_observation_links"
METRIC_V2_METRIC_LINKS_TABLE = "metric_v2_metric_links"
_METRIC_V2_COLUMNS = (
    "result_id",
    "asset_id",
    "metric_key",
    "value_text",
    "unit",
    "as_of",
    "available_at",
    "computed_at",
    "parameters_json",
    "evidence_set_id",
    "algorithm_version",
    "quality",
)
_FULL_METRIC_V2_COLUMNS = (*_METRIC_V2_COLUMNS, "inserted_at")
_METRIC_V2_BATCH_CHUNK_SIZE = 512
MAX_METRIC_V2_PAGE = 256


class MetricV2Error(StorageError):
    """Raised when a metric v2 row, lineage or table cannot be trusted."""


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MetricV2Error("metric v2 instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant_text(value: object) -> datetime:
    if value is None:
        raise MetricV2Error("metric v2 instant is missing")
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MetricV2Error("metric v2 index instant is not timezone-aware")
    return parsed.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MetricV2Error("metric v2 value must be a finite Decimal")
    return str(value)


def _parse_decimal_text(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise MetricV2Error("metric v2 value is not a valid Decimal") from error
    if not parsed.is_finite():
        raise MetricV2Error("metric v2 value must be finite")
    return parsed


def _parameters_json(parameters: dict[str, JsonValue]) -> str:
    semantic = filter_semantic_parameters(parameters)
    try:
        return json.dumps(semantic, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise MetricV2Error("metric v2 parameters are not JSON-serializable") from error


def _parse_parameters_json(value: object) -> dict[str, JsonValue]:
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except (ValueError, TypeError) as error:
        raise MetricV2Error("metric v2 parameters are corrupt") from error
    if not isinstance(parsed, dict):
        raise MetricV2Error("metric v2 parameters are corrupt")
    return dict(parsed)


def metric_v2_table_exists(connection: DuckDBPyConnection) -> bool:
    """Return whether the typed metric table exists in the index."""
    try:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{METRIC_V2_TABLE}'"
        ).fetchall()
    except Exception:
        return False
    return bool(rows)


def ensure_metric_v2_tables(connection: DuckDBPyConnection, *, create: bool) -> None:
    """Require the typed metric tables, creating them only when authorized."""
    try:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{METRIC_V2_TABLE}'"
        ).fetchall()
    except Exception as error:
        raise MetricV2Error("metric v2 index table is missing") from error
    names = {str(row[0]) for row in rows}
    if not names:
        if not create:
            raise MetricV2Error("metric v2 index table is missing")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {METRIC_V2_TABLE} (
                result_id VARCHAR PRIMARY KEY,
                asset_id VARCHAR NOT NULL,
                metric_key VARCHAR NOT NULL,
                value_text VARCHAR NOT NULL,
                unit VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                computed_at VARCHAR NOT NULL,
                parameters_json VARCHAR NOT NULL,
                evidence_set_id VARCHAR,
                algorithm_version VARCHAR NOT NULL,
                quality VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {METRIC_V2_OBSERVATION_LINKS_TABLE} (
                result_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                observation_id VARCHAR NOT NULL,
                PRIMARY KEY (result_id, position)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {METRIC_V2_METRIC_LINKS_TABLE} (
                result_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                input_result_id VARCHAR NOT NULL,
                PRIMARY KEY (result_id, position)
            )
            """
        )
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{METRIC_V2_TABLE}'"
        ).fetchall()
        names = {str(row[0]) for row in rows}
    if names != set(_FULL_METRIC_V2_COLUMNS):
        raise MetricV2Error("metric v2 index table is incompatible")
    if "document_json" in names:
        raise MetricV2Error("metric v2 index must not store documents")
    for table in (METRIC_V2_OBSERVATION_LINKS_TABLE, METRIC_V2_METRIC_LINKS_TABLE):
        try:
            link_rows = connection.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
            ).fetchall()
        except Exception as error:
            raise MetricV2Error(f"metric v2 link table {table} is missing") from error
        link_names = {str(row[0]) for row in link_rows}
        if not link_names:
            raise MetricV2Error(f"metric v2 link table {table} is missing")


def _evidence_reference(result: MetricResult) -> UUID | None:
    candidate = result.parameters.get("evidence_set_id")
    if candidate is None:
        return None
    try:
        return UUID(str(candidate))
    except ValueError as error:
        raise MetricV2Error("metric v2 evidence reference is not a UUID") from error


def metric_to_row(result: MetricResult) -> tuple[list[object], list[UUID], list[UUID]]:
    """Serialize a metric to typed columns with ordered link identifiers."""
    expected = metric_result_id_from_model_v2(result)
    if str(expected) != str(result.result_id):
        raise MetricV2Error("metric v2 identity does not match its semantic preimage")
    row = [
        str(expected),
        result.asset_id,
        result.metric_key,
        _decimal_text(result.value),
        result.unit,
        _instant_text(result.as_of),
        _instant_text(result.available_at),
        _instant_text(result.computed_at),
        _parameters_json(result.parameters),
        str(_evidence_reference(result)) if _evidence_reference(result) is not None else None,
        result.algorithm_version,
        result.quality.value,
    ]
    return row, list(result.input_observation_ids), list(result.input_metric_result_ids)


def row_to_metric(
    row: tuple[object, ...],
    *,
    observation_ids: list[UUID],
    metric_ids: list[UUID],
) -> MetricResult:
    """Rehydrate the strict model from typed columns and ordered links."""
    if len(row) != len(_METRIC_V2_COLUMNS):
        raise MetricV2Error("metric v2 index row is malformed")
    (
        result_id,
        asset_id,
        metric_key,
        value_text,
        unit,
        as_of,
        available_at,
        computed_at,
        parameters_json,
        evidence_set_id,
        algorithm_version,
        quality,
    ) = row
    parameters = _parse_parameters_json(parameters_json)
    evidence_reference = str(evidence_set_id) if evidence_set_id is not None else None
    if evidence_reference is not None and parameters.get("evidence_set_id") != evidence_reference:
        parameters = {**parameters, "evidence_set_id": evidence_reference}
    try:
        result = MetricResult.model_validate(
            {
                "result_id": str(result_id),
                "asset_id": asset_id,
                "metric_key": metric_key,
                "value": _parse_decimal_text(value_text),
                "unit": unit,
                "as_of": _parse_instant_text(as_of),
                "available_at": _parse_instant_text(available_at),
                "computed_at": _parse_instant_text(computed_at),
                "parameters": parameters,
                "input_observation_ids": [str(item) for item in observation_ids],
                "input_metric_result_ids": [str(item) for item in metric_ids],
                "algorithm_version": algorithm_version,
                "quality": quality,
            }
        )
    except (ValueError, TypeError) as error:
        raise MetricV2Error("metric v2 row does not validate") from error
    if str(result.value) != str(value_text):
        raise MetricV2Error("metric v2 Decimal projection diverged")
    expected = metric_result_id_from_model_v2(result)
    if str(expected) != str(result_id):
        raise MetricV2Error("metric v2 identity diverged")
    return result


def _same_semantic_content(stored: MetricResult, candidate: MetricResult) -> bool:
    """Compare semantic preimage, value and ordered links; computed_at may differ."""
    if str(metric_result_id_from_model_v2(stored)) != str(
        metric_result_id_from_model_v2(candidate)
    ):
        return False
    if stored.value != candidate.value:
        return False
    if [str(item) for item in stored.input_observation_ids] != [
        str(item) for item in candidate.input_observation_ids
    ]:
        return False
    if [str(item) for item in stored.input_metric_result_ids] != [
        str(item) for item in candidate.input_metric_result_ids
    ]:
        return False
    if stored.asset_id != candidate.asset_id or stored.metric_key != candidate.metric_key:
        return False
    if stored.unit != candidate.unit or stored.quality != candidate.quality:
        return False
    if stored.as_of != candidate.as_of or stored.available_at != candidate.available_at:
        return False
    if stored.algorithm_version != candidate.algorithm_version:
        return False
    return filter_semantic_parameters(stored.parameters) == filter_semantic_parameters(
        candidate.parameters
    )


def _require_v2_identity(result: MetricResult) -> UUID:
    expected = metric_result_id_from_model_v2(result)
    try:
        version = resolve_metric_identity_version(result.result_id)
    except (TypeError, ValueError) as error:
        raise MetricV2Error("metric v2 identity is not a v2 UUID") from error
    if str(version) != "v2":
        raise MetricV2Error("metric v2 identity must be a v2 UUID")
    if str(expected) != str(result.result_id):
        raise MetricV2Error("metric v2 identity does not match its semantic preimage")
    return expected


def require_metric_inputs_visible(
    connection: DuckDBPyConnection,
    result: MetricResult,
    *,
    known_ids: Collection[UUID] | None = None,
) -> None:
    """Require every input to exist, share asset and be visible at the result."""
    known = {str(item) for item in (known_ids or ())}
    seen_observations: set[str] = set()
    for observation_id in result.input_observation_ids:
        key = str(observation_id)
        if key in seen_observations:
            raise MetricV2Error("metric v2 observation inputs must be unique")
        seen_observations.add(key)
        rows = connection.execute(
            "SELECT asset_id, available_at FROM normalized_observations_v2 "
            "WHERE observation_id = ?",
            [key],
        ).fetchall()
        if not rows:
            raise MetricV2Error(f"metric v2 references a missing observation {key}")
        if str(rows[0][0]) != result.asset_id:
            raise MetricV2Error(f"metric v2 references a foreign observation {key}")
        available = _parse_instant_text(rows[0][1])
        if available > result.available_at:
            raise MetricV2Error(f"metric v2 references a future observation {key}")
    seen_metrics: set[str] = set()
    for metric_id in result.input_metric_result_ids:
        key = str(metric_id)
        if key in seen_metrics:
            raise MetricV2Error("metric v2 metric inputs must be unique")
        seen_metrics.add(key)
        if key == str(result.result_id):
            raise MetricV2Error("metric v2 dependency cycle is not allowed")
        if key in known:
            continue
        rows = connection.execute(
            f"SELECT asset_id, available_at FROM {METRIC_V2_TABLE} WHERE result_id = ?",
            [key],
        ).fetchall()
        if not rows:
            raise MetricV2Error(f"metric v2 references a missing metric {key}")
        if str(rows[0][0]) != result.asset_id:
            raise MetricV2Error(f"metric v2 references a foreign metric {key}")
        available = _parse_instant_text(rows[0][1])
        if available > result.available_at:
            raise MetricV2Error(f"metric v2 references a future metric {key}")


def raise_missing_metric(result_id: UUID) -> None:
    """Raise the canonical missing-metric error."""
    raise RecordNotFoundError(f"metric v2 {result_id} was not found")


class MetricV2Store:
    """Typed append-only store over the metric v2 staging tables."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save_many(
        self, results: tuple[MetricResult, ...] | list[MetricResult]
    ) -> BatchWriteReceipt:
        """Persist typed rows idempotently in topological order; keep prior lots."""
        if not results:
            return BatchWriteReceipt()
        ordered = sorted(results, key=lambda item: str(item.result_id))
        canonical: dict[str, MetricResult] = {}
        for result in ordered:
            key = str(result.result_id)
            if key in canonical and canonical[key] != result:
                raise RecordConflictError(
                    f"metric v2 identifier {key!r} already has different content"
                )
            canonical[key] = result
        ordered_keys = tuple(sorted(canonical))
        placeholders = ", ".join("?" for _ in ordered_keys)
        columns = ", ".join(_METRIC_V2_COLUMNS)
        existing = {
            str(row[0]): row
            for row in self._connection.execute(
                f"SELECT {columns} FROM {METRIC_V2_TABLE} WHERE result_id IN ({placeholders})",
                list(ordered_keys),
            ).fetchall()
        }
        created: list[UUID] = []
        reused: list[UUID] = []
        seen: set[str] = set()
        pending = [canonical[key] for key in ordered_keys]
        settled_known: set[str] = set(existing)
        for result in pending:
            _require_v2_identity(result)
        dependency_ids = sorted(
            {str(item) for result in pending for item in result.input_metric_result_ids}
            - settled_known
        )
        if dependency_ids:
            placeholders_dep = ", ".join("?" for _ in dependency_ids)
            persisted = {
                str(row[0])
                for row in self._connection.execute(
                    f"SELECT result_id FROM {METRIC_V2_TABLE} "
                    f"WHERE result_id IN ({placeholders_dep})",
                    dependency_ids,
                ).fetchall()
            }
            settled_known |= persisted
        progress = True
        while pending and progress:
            progress = False
            remaining: list[MetricResult] = []
            pending_known = {str(item.result_id) for item in pending}
            for result in pending:
                key = str(result.result_id)
                dependencies = {str(item) for item in result.input_metric_result_ids}
                if not dependencies.issubset(settled_known | pending_known):
                    require_metric_inputs_visible(self._connection, result)
                    remaining.append(result)
                    continue
                if not dependencies.issubset(settled_known):
                    remaining.append(result)
                    continue
                row, observation_ids, metric_ids = metric_to_row(result)
                if key in existing:
                    stored_observations = self._link_observation_ids(UUID(key))
                    stored_metrics = self._link_metric_ids(UUID(key))
                    stored = row_to_metric(
                        existing[key],
                        observation_ids=stored_observations,
                        metric_ids=stored_metrics,
                    )
                    if not _same_semantic_content(stored, result):
                        raise RecordConflictError(
                            f"metric v2 identifier {key!r} already has different content"
                        )
                    if key not in seen:
                        reused.append(result.result_id)
                        seen.add(key)
                    settled_known.add(key)
                    progress = True
                    continue
                require_metric_inputs_visible(
                    self._connection, result, known_ids=[UUID(item) for item in settled_known]
                )
                if key not in seen:
                    if not dependencies.issubset(settled_known):
                        remaining.append(result)
                        continue
                    self._insert_one(row, observation_ids, metric_ids)
                    created.append(result.result_id)
                    seen.add(key)
                    settled_known.add(key)
                    progress = True
                else:
                    reused.append(result.result_id)
            pending = remaining
        if pending:
            unresolved = sorted(
                {str(item) for result in pending for item in result.input_metric_result_ids}
                - settled_known
            )
            missing = unresolved[0] if unresolved else str(pending[0].result_id)
            raise MetricV2Error(f"metric v2 dependency {missing} is not persisted")
        return BatchWriteReceipt(
            created_ids=tuple(created),
            reused_ids=tuple(reused),
            conflicting_ids=(),
        )

    def get_many(self, result_ids: tuple[UUID, ...] | list[UUID]) -> dict[UUID, MetricResult]:
        """Hydrate verified metrics in deterministic order."""
        ordered = tuple(sorted(set(result_ids), key=str))
        if not ordered:
            return {}
        columns = ", ".join(_METRIC_V2_COLUMNS)
        placeholders = ", ".join("?" for _ in ordered)
        rows = self._connection.execute(
            f"SELECT {columns} FROM {METRIC_V2_TABLE} WHERE result_id IN ({placeholders})",
            [str(result_id) for result_id in ordered],
        ).fetchall()
        indexed = {UUID(row[0]): row for row in rows}
        for result_id in ordered:
            if result_id not in indexed:
                raise_missing_metric(result_id)
        return {
            result_id: row_to_metric(
                indexed[result_id],
                observation_ids=self._link_observation_ids(result_id),
                metric_ids=self._link_metric_ids(result_id),
            )
            for result_id in ordered
        }

    def _link_observation_ids(self, result_id: UUID) -> list[UUID]:
        rows = self._connection.execute(
            f"SELECT observation_id FROM {METRIC_V2_OBSERVATION_LINKS_TABLE} "
            "WHERE result_id = ? ORDER BY position",
            [str(result_id)],
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def _link_metric_ids(self, result_id: UUID) -> list[UUID]:
        rows = self._connection.execute(
            f"SELECT input_result_id FROM {METRIC_V2_METRIC_LINKS_TABLE} "
            "WHERE result_id = ? ORDER BY position",
            [str(result_id)],
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def _insert_one(
        self, row: list[object], observation_ids: list[UUID], metric_ids: list[UUID]
    ) -> None:
        columns = ", ".join(_METRIC_V2_COLUMNS)
        placeholders = ", ".join("?" for _ in row)
        self._connection.execute(
            f"INSERT INTO {METRIC_V2_TABLE} ({columns}) VALUES ({placeholders})",
            row,
        )
        for position, observation_id in enumerate(observation_ids):
            self._connection.execute(
                f"INSERT INTO {METRIC_V2_OBSERVATION_LINKS_TABLE} "
                "(result_id, position, observation_id) VALUES (?, ?, ?)",
                [str(row[0]), position, str(observation_id)],
            )
        for position, metric_id in enumerate(metric_ids):
            self._connection.execute(
                f"INSERT INTO {METRIC_V2_METRIC_LINKS_TABLE} "
                "(result_id, position, input_result_id) VALUES (?, ?, ?)",
                [str(row[0]), position, str(metric_id)],
            )


__all__ = [
    "MAX_METRIC_V2_PAGE",
    "METRIC_V2_METRIC_LINKS_TABLE",
    "METRIC_V2_OBSERVATION_LINKS_TABLE",
    "METRIC_V2_TABLE",
    "MetricV2Error",
    "MetricV2Store",
    "ensure_metric_v2_tables",
    "metric_to_row",
    "metric_v2_table_exists",
    "require_metric_inputs_visible",
    "row_to_metric",
]
