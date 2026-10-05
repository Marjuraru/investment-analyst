"""Append-only persistence for daily evidence prefix nodes in RawV2Staging."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models.enums import DataFrequency
from investment_analyst.storage.analytical_v2_validation import chunked_sequence
from investment_analyst.storage.bounded_insert import (
    BoundedInsertTable,
    insert_bounded,
    write_transaction,
)
from investment_analyst.storage.errors import RecordConflictError
from investment_analyst.storage.observation_v2 import ensure_observation_v2_table

DAILY_PREFIX_TABLE = "market_daily_prefixes_v2"
DAILY_PREFIX_OBSERVATION_LINKS_TABLE = "market_daily_prefix_observation_links_v2"
MAX_DAILY_PREFIX_PAGE = 256

_PREFIX_COLUMNS = (
    "prefix_id",
    "policy_version",
    "asset_id",
    "source_id",
    "frequency",
    "field_group",
    "timestamp",
    "observation_digest",
    "parent_prefix_id",
    "parent_hash",
    "length",
    "available_at",
    "quality",
    "prefix_hash",
)
_LINK_COLUMNS = ("prefix_id", "position", "observation_id")


class DailyEvidenceV2Error(ValueError):
    """Raised when the daily evidence tables or links are corrupt or incompatible."""


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DailyEvidenceV2Error("daily evidence timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise DailyEvidenceV2Error("daily evidence timestamp column is malformed")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise DailyEvidenceV2Error("daily evidence timestamp column is malformed") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise DailyEvidenceV2Error("daily evidence timestamp column is timezone-naive")
    return result.astimezone(UTC)


def _columns(connection: DuckDBPyConnection, table: str) -> set[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [table],
    ).fetchall()
    return {str(row[0]) for row in rows}


def daily_evidence_v2_tables_exist(connection: DuckDBPyConnection) -> bool:
    """Return whether either daily evidence table exists, including partial schemas."""
    return bool(
        _columns(connection, DAILY_PREFIX_TABLE)
        or _columns(connection, DAILY_PREFIX_OBSERVATION_LINKS_TABLE)
    )


def ensure_daily_evidence_v2_tables(
    connection: DuckDBPyConnection,
    *,
    create: bool,
) -> None:
    """Require the full compatible append-only daily evidence schema."""
    prefix_names = _columns(connection, DAILY_PREFIX_TABLE)
    link_names = _columns(connection, DAILY_PREFIX_OBSERVATION_LINKS_TABLE)
    present = (bool(prefix_names), bool(link_names))
    if not any(present):
        if not create:
            raise DailyEvidenceV2Error("daily evidence v2 tables are missing")
        connection.execute(
            f"""
            CREATE TABLE {DAILY_PREFIX_TABLE} (
                prefix_id VARCHAR PRIMARY KEY,
                policy_version VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                field_group VARCHAR NOT NULL,
                timestamp VARCHAR NOT NULL,
                observation_digest VARCHAR NOT NULL,
                parent_prefix_id VARCHAR,
                parent_hash VARCHAR,
                length INTEGER NOT NULL,
                available_at VARCHAR NOT NULL,
                quality VARCHAR NOT NULL,
                prefix_hash VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE {DAILY_PREFIX_OBSERVATION_LINKS_TABLE} (
                prefix_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                observation_id VARCHAR NOT NULL,
                PRIMARY KEY (prefix_id, position)
            )
            """
        )
        prefix_names = _columns(connection, DAILY_PREFIX_TABLE)
        link_names = _columns(connection, DAILY_PREFIX_OBSERVATION_LINKS_TABLE)
    elif not all(present):
        raise DailyEvidenceV2Error("daily evidence v2 schema is partially present")
    if prefix_names != set((*_PREFIX_COLUMNS, "inserted_at")):
        raise DailyEvidenceV2Error("daily evidence v2 table is incompatible")
    if link_names != set((*_LINK_COLUMNS,)):
        raise DailyEvidenceV2Error("daily evidence v2 observation links are incompatible")
    if "document_json" in prefix_names or "document_json" in link_names:
        raise DailyEvidenceV2Error("daily evidence v2 tables must not store documents")


class DailyEvidenceV2Store:
    """Typed append-only node and observation-link store."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save_many(
        self,
        prefixes: Collection[DailyEvidencePrefix],
    ) -> BatchWriteReceipt:
        """Insert verified nodes in bounded, independently durable batches."""
        ensure_daily_evidence_v2_tables(self._connection, create=True)
        ordered = tuple(
            sorted(prefixes, key=lambda item: (item.length, item.timestamp, str(item.prefix_id)))
        )
        if not ordered:
            return BatchWriteReceipt()
        canonical: dict[UUID, DailyEvidencePrefix] = {}
        for prefix in ordered:
            existing = canonical.get(prefix.prefix_id)
            if existing is not None and existing != prefix:
                raise RecordConflictError(
                    f"daily evidence identifier {prefix.prefix_id} has conflicting content"
                )
            canonical[prefix.prefix_id] = prefix
        unique = tuple(canonical.values())
        if len(unique) > MAX_DAILY_PREFIX_PAGE:
            raise DailyEvidenceV2Error("daily evidence save batch must not exceed 256 nodes")
        existing = self.get_many(tuple(canonical))
        created = tuple(prefix.prefix_id for prefix in unique if prefix.prefix_id not in existing)
        reused = tuple(prefix_id for prefix_id in canonical if prefix_id in existing)
        for prefix_id in reused:
            if existing[prefix_id] != canonical[prefix_id]:
                raise RecordConflictError(
                    f"daily evidence identifier {prefix_id} already has different content"
                )
        pending = tuple(prefix for prefix in unique if prefix.prefix_id not in existing)
        if not pending:
            return BatchWriteReceipt(created_ids=(), reused_ids=reused, conflicting_ids=())

        pending_by_id = {item.prefix_id: item for item in pending}
        parents_to_load = {
            item.parent_prefix_id
            for item in pending
            if item.parent_prefix_id is not None and item.parent_prefix_id not in pending_by_id
        }
        stored_parents = self.get_many(tuple(parents_to_load)) if parents_to_load else {}
        parents = {**stored_parents, **pending_by_id}
        for prefix in pending:
            if prefix.parent_prefix_id is not None:
                parent = parents.get(prefix.parent_prefix_id)
                if parent is None or not _is_parent(prefix, parent):
                    raise DailyEvidenceV2Error("daily evidence parent is missing or incompatible")

        self._verify_observations(pending)
        prefix_rows = [_prefix_row(item) for item in pending]
        link_rows = [
            [str(item.prefix_id), position, str(observation_id)]
            for item in pending
            for position, observation_id in enumerate(item.observation_ids)
        ]
        with write_transaction(self._connection):
            insert_bounded(self._connection, BoundedInsertTable.DAILY_PREFIXES_V2, prefix_rows)
            insert_bounded(
                self._connection,
                BoundedInsertTable.DAILY_PREFIX_OBSERVATION_LINKS_V2,
                link_rows,
            )
        return BatchWriteReceipt(created_ids=created, reused_ids=reused, conflicting_ids=())

    def get_many(
        self,
        prefix_ids: Collection[UUID],
    ) -> dict[UUID, DailyEvidencePrefix]:
        """Hydrate and verify requested nodes with bounded key queries."""
        ensure_daily_evidence_v2_tables(self._connection, create=False)
        requested = tuple(sorted(set(prefix_ids), key=str))
        if not requested:
            return {}
        indexed: dict[UUID, tuple[object, ...]] = {}
        columns = ", ".join(_PREFIX_COLUMNS)
        for chunk in chunked_sequence(requested, MAX_DAILY_PREFIX_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {DAILY_PREFIX_TABLE} WHERE prefix_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            indexed.update({UUID(str(row[0])): row for row in rows})
        if set(indexed) != set(requested):
            output = self._hydrate(indexed)
            self._verify_observations(tuple(output.values()))
            return output
        output = self._hydrate(indexed)
        self._verify_observations(tuple(output.values()))
        return output

    def list_ids_page(
        self,
        *,
        limit: int,
        after_prefix_id: UUID | None = None,
    ) -> list[UUID]:
        """Return one stable page of prefix IDs without hydration."""
        _validate_page(limit)
        ensure_daily_evidence_v2_tables(self._connection, create=False)
        where = " WHERE prefix_id > ?" if after_prefix_id is not None else ""
        parameters: list[object] = [str(after_prefix_id)] if after_prefix_id is not None else []
        rows = self._connection.execute(
            f"SELECT prefix_id FROM {DAILY_PREFIX_TABLE}{where} ORDER BY prefix_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def list_scope_page(
        self,
        *,
        asset_id: str,
        source_id: str,
        field_group: DailyEvidenceFieldGroup,
        known_at: datetime,
        limit: int,
        after_timestamp: datetime | None = None,
        after_prefix_id: UUID | None = None,
    ) -> tuple[DailyEvidencePrefix, ...]:
        """Read one bounded PIT chain page in timestamp and identity order."""
        _validate_page(limit)
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise DailyEvidenceV2Error("daily evidence known_at must be timezone-aware")
        if (after_timestamp is None) != (after_prefix_id is None):
            raise DailyEvidenceV2Error("daily evidence cursor requires both fields together")
        clauses = [
            "asset_id = ?",
            "source_id = ?",
            "field_group = ?",
            "available_at <= ?",
        ]
        parameters: list[object] = [
            asset_id,
            source_id,
            field_group.value,
            _instant_text(known_at),
        ]
        if after_timestamp is not None and after_prefix_id is not None:
            if after_timestamp.tzinfo is None or after_timestamp.utcoffset() is None:
                raise DailyEvidenceV2Error("daily evidence cursor must be timezone-aware")
            clauses.append("(timestamp, prefix_id) > (?, ?)")
            parameters.extend([_instant_text(after_timestamp), str(after_prefix_id)])
        rows = self._connection.execute(
            f"SELECT prefix_id FROM {DAILY_PREFIX_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp, prefix_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        if not rows:
            return ()
        loaded = self.get_many(tuple(UUID(str(row[0])) for row in rows))
        if len(loaded) != len(rows):
            raise DailyEvidenceV2Error("daily evidence scope page lost a prefix")
        return tuple(
            sorted(loaded.values(), key=lambda item: (item.timestamp, str(item.prefix_id)))
        )

    def _hydrate(
        self,
        indexed: dict[UUID, tuple[object, ...]],
    ) -> dict[UUID, DailyEvidencePrefix]:
        links: dict[UUID, list[tuple[int, UUID]]] = {key: [] for key in indexed}
        for chunk in chunked_sequence(tuple(indexed), MAX_DAILY_PREFIX_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT prefix_id, position, observation_id "
                f"FROM {DAILY_PREFIX_OBSERVATION_LINKS_TABLE} "
                f"WHERE prefix_id IN ({placeholders}) ORDER BY prefix_id, position",
                [str(item) for item in chunk],
            ).fetchall()
            for prefix_id, position, observation_id in rows:
                key = UUID(str(prefix_id))
                if key not in links or isinstance(position, bool) or not isinstance(position, int):
                    raise DailyEvidenceV2Error("daily evidence observation link is malformed")
                links[key].append((position, UUID(str(observation_id))))
        output: dict[UUID, DailyEvidencePrefix] = {}
        for prefix_id, row in indexed.items():
            ordered_links = links[prefix_id]
            if [position for position, _ in ordered_links] != list(range(len(ordered_links))):
                raise DailyEvidenceV2Error("daily evidence observation link positions are invalid")
            if len(row) != len(_PREFIX_COLUMNS):
                raise DailyEvidenceV2Error("daily evidence row is malformed")
            try:
                prefix = DailyEvidencePrefix.model_validate(
                    {
                        "prefix_id": prefix_id,
                        "policy_version": row[1],
                        "asset_id": row[2],
                        "source_id": row[3],
                        "frequency": row[4],
                        "field_group": row[5],
                        "timestamp": _parse_instant(row[6]),
                        "observation_ids": [str(identifier) for _, identifier in ordered_links],
                        "observation_digest": row[7],
                        "parent_prefix_id": row[8],
                        "parent_hash": row[9],
                        "length": row[10],
                        "available_at": _parse_instant(row[11]),
                        "quality": row[12],
                        "prefix_hash": row[13],
                    }
                )
            except (ValueError, TypeError) as error:
                raise DailyEvidenceV2Error("daily evidence row does not validate") from error
            if prefix.prefix_id != UUID(str(row[0])):
                raise DailyEvidenceV2Error("daily evidence row identity diverged")
            output[prefix_id] = prefix
        return output

    def _verify_observations(self, prefixes: Sequence[DailyEvidencePrefix]) -> None:
        ensure_observation_v2_table(self._connection, create=False)
        expected: dict[UUID, tuple[DailyEvidencePrefix, int]] = {}
        for prefix in prefixes:
            for position, observation_id in enumerate(prefix.observation_ids):
                if observation_id in expected:
                    previous, previous_position = expected[observation_id]
                    if previous != prefix or previous_position != position:
                        continue
                expected[observation_id] = (prefix, position)
        observations: dict[UUID, tuple[object, ...]] = {}
        for chunk in chunked_sequence(tuple(expected), MAX_DAILY_PREFIX_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT o.observation_id, o.raw_record_id, o.asset_id, o.field_name, "
                "o.frequency, o.observed_at, o.available_at, o.source_id, "
                "r.asset_id, r.source_id, r.event_time, r.available_at "
                "FROM normalized_observations_v2 AS o "
                "JOIN raw_v2_index AS r ON r.record_id = o.raw_record_id "
                f"WHERE o.observation_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            observations.update({UUID(str(row[0])): row for row in rows})
        if set(observations) != set(expected):
            raise DailyEvidenceV2Error("daily evidence references a missing observation")
        for observation_id, (prefix, position) in expected.items():
            row = observations[observation_id]
            group_fields = (
                ("close",)
                if prefix.field_group is DailyEvidenceFieldGroup.CLOSE
                else ("high", "low", "close")
            )
            if (
                row[2] != prefix.asset_id
                or row[3] != group_fields[position]
                or row[4] != DataFrequency.DAY_1.value
                or _parse_instant(row[5]) != prefix.timestamp
                or _parse_instant(row[6]) > prefix.available_at
                or row[7] != prefix.source_id
                or row[8] != prefix.asset_id
                or row[9] != prefix.source_id
                or _parse_instant(row[10]) != prefix.timestamp
                or _parse_instant(row[11]) != _parse_instant(row[6])
            ):
                raise DailyEvidenceV2Error("daily evidence observation is outside its prefix")


def _prefix_row(prefix: DailyEvidencePrefix) -> list[object]:
    return [
        str(prefix.prefix_id),
        prefix.policy_version,
        prefix.asset_id,
        prefix.source_id,
        prefix.frequency.value,
        prefix.field_group.value,
        _instant_text(prefix.timestamp),
        prefix.observation_digest,
        str(prefix.parent_prefix_id) if prefix.parent_prefix_id is not None else None,
        prefix.parent_hash,
        prefix.length,
        _instant_text(prefix.available_at),
        prefix.quality.value,
        prefix.prefix_hash,
    ]


def _is_parent(child: DailyEvidencePrefix, parent: DailyEvidencePrefix) -> bool:
    return (
        child.parent_prefix_id == parent.prefix_id
        and child.parent_hash == parent.prefix_hash
        and child.length == parent.length + 1
        and child.asset_id == parent.asset_id
        and child.source_id == parent.source_id
        and child.frequency is parent.frequency
        and child.field_group is parent.field_group
        and child.timestamp > parent.timestamp
        and child.available_at >= parent.available_at
    )


def _validate_page(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise DailyEvidenceV2Error("daily evidence page limit must be an integer")
    if limit < 1 or limit > MAX_DAILY_PREFIX_PAGE:
        raise DailyEvidenceV2Error("daily evidence page limit must be between 1 and 256")


__all__ = [
    "DAILY_PREFIX_OBSERVATION_LINKS_TABLE",
    "DAILY_PREFIX_TABLE",
    "MAX_DAILY_PREFIX_PAGE",
    "DailyEvidenceV2Error",
    "DailyEvidenceV2Store",
    "daily_evidence_v2_tables_exist",
    "ensure_daily_evidence_v2_tables",
]
