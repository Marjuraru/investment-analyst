"""Typed normalized observation v2 staging table without duplicated JSON.

Observations live in the same file-backed DuckDB index as the raw v2 staging
under the same writer lock, but in their own ``normalized_observations_v2``
table. Every coordinate of ``NormalizedObservation`` is stored in a typed
column: ``Decimal`` values use their canonical text form and the components of
``SourceReference`` travel in dedicated columns. No ``document_json`` column
exists. Reads rehydrate the strict model and verify the raw reference by
identifier and source; corruption, absence or a divergent projection fails
closed.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import DataFrequency, NormalizedObservation
from investment_analyst.storage.analytical_v2_validation import MAX_CHUNK_SIZE
from investment_analyst.storage.bounded_insert import (
    BoundedInsertTable,
    insert_bounded,
    write_transaction,
)
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)

OBSERVATION_V2_TABLE = "normalized_observations_v2"
_OBSERVATION_V2_COLUMNS = (
    "observation_id",
    "raw_record_id",
    "asset_id",
    "field_name",
    "value_text",
    "unit",
    "frequency",
    "observed_at",
    "period_start",
    "period_end",
    "available_at",
    "normalized_at",
    "source_id",
    "source_record_key",
    "source_retrieved_at",
    "source_raw_uri",
    "source_checksum_sha256",
    "quality",
    "transformation_version",
)
_FULL_OBSERVATION_V2_COLUMNS = (*_OBSERVATION_V2_COLUMNS, "inserted_at")
OBSERVATION_V2_COLUMNS = _OBSERVATION_V2_COLUMNS
_OBSERVATION_V2_BATCH_CHUNK_SIZE = 512
MAX_OBSERVATION_V2_PAGE = 256


class ObservationV2Error(StorageError):
    """Raised when an observation v2 row, reference or table cannot be trusted."""


def _instant_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ObservationV2Error("observation v2 instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant_text(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservationV2Error("observation v2 index instant is not timezone-aware")
    return parsed.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ObservationV2Error("observation v2 value must be a finite Decimal")
    return str(value)


def _parse_decimal_text(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ObservationV2Error("observation v2 value is not a valid Decimal") from error
    if not parsed.is_finite():
        raise ObservationV2Error("observation v2 value must be finite")
    return parsed


def observation_v2_table_exists(connection: DuckDBPyConnection) -> bool:
    """Return whether the typed observation table exists in the index."""
    try:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{OBSERVATION_V2_TABLE}'"
        ).fetchall()
    except Exception:
        return False
    return bool(rows)


def ensure_observation_v2_table(connection: DuckDBPyConnection, *, create: bool) -> None:
    """Require the typed observation table, creating it only when authorized."""
    try:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{OBSERVATION_V2_TABLE}'"
        ).fetchall()
    except Exception as error:
        raise ObservationV2Error("observation v2 index table is missing") from error
    names = {str(row[0]) for row in rows}
    if not names:
        if not create:
            raise ObservationV2Error("observation v2 index table is missing")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {OBSERVATION_V2_TABLE} (
                observation_id VARCHAR PRIMARY KEY,
                raw_record_id VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                field_name VARCHAR NOT NULL,
                value_text VARCHAR NOT NULL,
                unit VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                observed_at VARCHAR,
                period_start VARCHAR,
                period_end VARCHAR,
                available_at VARCHAR NOT NULL,
                normalized_at VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                source_record_key VARCHAR,
                source_retrieved_at VARCHAR NOT NULL,
                source_raw_uri VARCHAR,
                source_checksum_sha256 VARCHAR,
                quality VARCHAR NOT NULL,
                transformation_version VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{OBSERVATION_V2_TABLE}'"
        ).fetchall()
        names = {str(row[0]) for row in rows}
    if names != set(_FULL_OBSERVATION_V2_COLUMNS):
        raise ObservationV2Error("observation v2 index table is incompatible")
    if "document_json" in names:
        raise ObservationV2Error("observation v2 index must not store documents")


def observation_to_row(observation: NormalizedObservation) -> list[object]:
    """Serialize an observation to typed index columns without JSON."""
    return [
        str(observation.observation_id),
        str(observation.raw_record_id),
        observation.asset_id,
        observation.field_name,
        _decimal_text(observation.value),
        observation.unit,
        observation.frequency.value,
        _instant_text(observation.observed_at),
        _instant_text(observation.period_start),
        _instant_text(observation.period_end),
        _instant_text(observation.available_at),
        _instant_text(observation.normalized_at),
        observation.source.source_id,
        observation.source.record_key,
        _instant_text(observation.source.retrieved_at),
        observation.source.raw_uri,
        observation.source.checksum_sha256,
        observation.quality.value,
        observation.transformation_version,
    ]


def row_to_observation(row: tuple[object, ...]) -> NormalizedObservation:
    """Rehydrate the strict model from typed columns, failing closed on drift."""
    if len(row) != len(_OBSERVATION_V2_COLUMNS):
        raise ObservationV2Error("observation v2 index row is malformed")
    (
        observation_id,
        raw_record_id,
        asset_id,
        field_name,
        value_text,
        unit,
        frequency,
        observed_at,
        period_start,
        period_end,
        available_at,
        normalized_at,
        source_id,
        source_record_key,
        source_retrieved_at,
        source_raw_uri,
        source_checksum,
        quality,
        transformation_version,
    ) = row
    try:
        observation = NormalizedObservation.model_validate(
            {
                "observation_id": str(observation_id),
                "raw_record_id": str(raw_record_id),
                "asset_id": asset_id,
                "field_name": field_name,
                "value": _parse_decimal_text(value_text),
                "unit": unit,
                "frequency": frequency,
                "observed_at": _parse_instant_text(observed_at),
                "period_start": _parse_instant_text(period_start),
                "period_end": _parse_instant_text(period_end),
                "available_at": _parse_instant_text(available_at),
                "normalized_at": _parse_instant_text(normalized_at),
                "source": {
                    "source_id": source_id,
                    "record_key": source_record_key,
                    "retrieved_at": _parse_instant_text(source_retrieved_at),
                    "raw_uri": source_raw_uri,
                    "checksum_sha256": source_checksum,
                },
                "quality": quality,
                "transformation_version": transformation_version,
            }
        )
    except (ValueError, TypeError) as error:
        raise ObservationV2Error("observation v2 row does not validate") from error
    if str(observation.value) != str(value_text):
        raise ObservationV2Error("observation v2 Decimal projection diverged")
    if observation.frequency != DataFrequency(str(frequency)):
        raise ObservationV2Error("observation v2 frequency projection diverged")
    return observation


def require_raw_reference(
    connection: DuckDBPyConnection, observation: NormalizedObservation
) -> None:
    """Require the linked raw v2 row to exist with a matching source."""
    require_raw_references(connection, [observation])


def require_raw_references(
    connection: DuckDBPyConnection,
    observations: Collection[NormalizedObservation],
) -> None:
    """Verify raw record references by source in bounded ID pages."""
    expected = {str(item.raw_record_id): item.source.source_id for item in observations}
    ordered_ids = sorted(expected)
    persisted: dict[str, str] = {}
    for start in range(0, len(ordered_ids), MAX_CHUNK_SIZE):
        id_chunk = ordered_ids[start : start + MAX_CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in id_chunk)
        rows = connection.execute(
            f"SELECT record_id, source_id FROM raw_v2_index WHERE record_id IN ({placeholders})",
            id_chunk,
        ).fetchall()
        persisted.update({str(row[0]): str(row[1]) for row in rows})
    for record_id, source_id in expected.items():
        stored_source_id = persisted.get(record_id)
        if stored_source_id is None:
            raise ObservationV2Error(f"observation v2 references a missing raw record {record_id}")
        if stored_source_id != source_id:
            raise ObservationV2Error(f"observation v2 references a foreign raw record {record_id}")


def require_observation_ids(
    observation_ids: tuple[UUID, ...],
) -> tuple[UUID, ...]:
    """Order identifiers deterministically for bounded verification."""
    return tuple(sorted(set(observation_ids), key=str))


def missing_observation_ids(requested: tuple[UUID, ...], found: set[UUID]) -> UUID | None:
    """Return the first missing identifier, if any."""
    for observation_id in requested:
        if observation_id not in found:
            return observation_id
    return None


def raise_missing_observation(observation_id: UUID) -> None:
    """Raise the canonical missing-observation error."""
    raise RecordNotFoundError(f"observation v2 {observation_id} was not found")


class ObservationV2Store:
    """Typed append-only store over the observation v2 staging table."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save_many(
        self, observations: tuple[NormalizedObservation, ...] | list[NormalizedObservation]
    ) -> BatchWriteReceipt:
        """Persist typed rows idempotently; a conflict on one ID fails closed."""
        if not observations:
            return BatchWriteReceipt()
        ordered = sorted(observations, key=lambda item: str(item.observation_id))
        canonical: dict[str, NormalizedObservation] = {}
        for observation in ordered:
            key = str(observation.observation_id)
            if key in canonical and canonical[key] != observation:
                raise RecordConflictError(
                    f"observation v2 identifier {key!r} already has different content"
                )
            canonical[key] = observation
        ordered_keys = tuple(sorted(canonical))
        columns = ", ".join(_OBSERVATION_V2_COLUMNS)
        existing: dict[str, tuple[object, ...]] = {}
        for start in range(0, len(ordered_keys), MAX_CHUNK_SIZE):
            id_chunk = ordered_keys[start : start + MAX_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in id_chunk)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {OBSERVATION_V2_TABLE} "
                f"WHERE observation_id IN ({placeholders})",
                list(id_chunk),
            ).fetchall()
            existing.update({str(row[0]): row for row in rows})
        created: list[UUID] = []
        reused: list[UUID] = []
        inserts: list[list[object]] = []
        references_to_check: list[NormalizedObservation] = []
        seen: set[str] = set()
        for observation in ordered:
            key = str(observation.observation_id)
            row = observation_to_row(observation)
            if key in existing:
                if row_to_observation(existing[key]) != observation:
                    raise RecordConflictError(
                        f"observation v2 identifier {key!r} already has different content"
                    )
                references_to_check.append(row_to_observation(existing[key]))
                if key not in seen:
                    reused.append(observation.observation_id)
                    seen.add(key)
            else:
                references_to_check.append(observation)
                if key not in seen:
                    created.append(observation.observation_id)
                    inserts.append(row)
                    seen.add(key)
                else:
                    reused.append(observation.observation_id)
        require_raw_references(self._connection, references_to_check)
        if inserts:
            with write_transaction(self._connection):
                insert_bounded(
                    self._connection,
                    BoundedInsertTable.OBSERVATIONS_V2,
                    inserts,
                )
        return BatchWriteReceipt(
            created_ids=tuple(created),
            reused_ids=tuple(reused),
            conflicting_ids=(),
        )

    def get_many(
        self, observation_ids: tuple[UUID, ...] | list[UUID]
    ) -> dict[UUID, NormalizedObservation]:
        """Hydrate verified observations in deterministic order."""
        ordered = require_observation_ids(tuple(observation_ids))
        if not ordered:
            return {}
        columns = ", ".join(_OBSERVATION_V2_COLUMNS)
        indexed: dict[UUID, tuple[object, ...]] = {}
        for start in range(0, len(ordered), MAX_CHUNK_SIZE):
            id_chunk = ordered[start : start + MAX_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in id_chunk)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {OBSERVATION_V2_TABLE} "
                f"WHERE observation_id IN ({placeholders})",
                [str(observation_id) for observation_id in id_chunk],
            ).fetchall()
            indexed.update({UUID(str(row[0])): row for row in rows})
        missing = missing_observation_ids(ordered, set(indexed))
        if missing is not None:
            raise_missing_observation(missing)
        hydrated = {
            observation_id: row_to_observation(indexed[observation_id])
            for observation_id in ordered
        }
        require_raw_references(self._connection, hydrated.values())
        return hydrated


__all__ = [
    "MAX_OBSERVATION_V2_PAGE",
    "OBSERVATION_V2_TABLE",
    "ObservationV2Error",
    "ObservationV2Store",
    "ensure_observation_v2_table",
    "observation_to_row",
    "observation_v2_table_exists",
    "require_observation_ids",
    "require_raw_reference",
    "require_raw_references",
    "row_to_observation",
]
