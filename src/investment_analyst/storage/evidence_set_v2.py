"""Typed evidence set v2 staging tables with shared hourly lineage.

Segments and sets live in the same file-backed DuckDB index as the raw v2
staging under the same writer lock, in their own ``evidence_segments_v2``
and ``evidence_sets_v2`` tables with an ordered member table. Storage
reuses the pure ``EvidenceSegment``/``EvidenceSet`` contract byte for byte:
segment identity, canonical hash and shape are recalculated on every read
and every referenced observation must still exist in
``normalized_observations_v2`` with matching asset, source, field and
availability. A metric row references a set only through its parameters;
resolution hydrates and verifies segments, count, order, hash,
availability and observations before rehydrating any metric.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime
from uuid import UUID

from duckdb import DuckDBPyConnection
from pydantic import ValidationError

from investment_analyst.analytics.evidence_set import (
    EvidenceSegment,
    EvidenceSet,
    EvidenceSetVerificationError,
    resolve_evidence_set,
    verify_evidence_set,
)
from investment_analyst.storage.analytical_v2_validation import (
    MAX_CHUNK_SIZE,
    chunked_sequence,
)
from investment_analyst.storage.errors import RecordConflictError, StorageError

EVIDENCE_SEGMENT_V2_TABLE = "evidence_segments_v2"
EVIDENCE_SET_V2_TABLE = "evidence_sets_v2"
EVIDENCE_SET_V2_MEMBERS_TABLE = "evidence_set_v2_members"
_SEGMENT_V2_COLUMNS = (
    "segment_id",
    "asset_id",
    "source_id",
    "field_name",
    "day",
    "observation_ids_json",
    "available_at",
    "canonical_hash",
)
_SET_V2_COLUMNS = (
    "evidence_set_id",
    "asset_id",
    "source_id",
    "field_name",
    "input_count",
    "head_offset",
    "inline_observation_ids_json",
    "inline_available_at",
    "first_observed_at",
    "first_observation_id",
    "last_observed_at",
    "last_observation_id",
    "available_at",
    "canonical_hash",
)
_FULL_SEGMENT_V2_COLUMNS = (*_SEGMENT_V2_COLUMNS, "inserted_at")
_FULL_SET_V2_COLUMNS = (*_SET_V2_COLUMNS, "inserted_at")


class EvidenceSetV2Error(StorageError):
    """Raised when an evidence v2 row or lineage cannot be trusted."""


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceSetV2Error("evidence v2 instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant_text(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceSetV2Error("evidence v2 index instant is not timezone-aware")
    return parsed.astimezone(UTC)


def _parse_optional_instant(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_instant_text(value)


def _ids_text(identifiers: tuple[UUID, ...] | list[UUID]) -> str:
    import json

    return json.dumps(
        [str(identifier) for identifier in identifiers],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _parse_ids_text(value: object) -> tuple[UUID, ...]:
    import json

    try:
        parsed = json.loads(str(value))
    except (ValueError, TypeError) as error:
        raise EvidenceSetV2Error("evidence v2 identifiers are corrupt") from error
    if not isinstance(parsed, list):
        raise EvidenceSetV2Error("evidence v2 identifiers are corrupt")
    try:
        return tuple(UUID(str(item)) for item in parsed)
    except ValueError as error:
        raise EvidenceSetV2Error("evidence v2 identifiers are corrupt") from error


def _parse_day(value: object) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise EvidenceSetV2Error("evidence v2 day is corrupt") from error


def evidence_v2_tables_exist(connection: DuckDBPyConnection) -> bool:
    """Return whether both typed evidence tables exist in the index."""
    try:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('evidence_segments_v2', 'evidence_sets_v2')"
        ).fetchall()
    except Exception:
        return False
    return {str(row[0]) for row in rows} == {"evidence_segments_v2", "evidence_sets_v2"}


def ensure_evidence_v2_tables(connection: DuckDBPyConnection, *, create: bool) -> None:
    """Require the typed evidence tables, creating them only when authorized."""
    try:
        segment_rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SEGMENT_V2_TABLE}'"
        ).fetchall()
        set_rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SET_V2_TABLE}'"
        ).fetchall()
        member_rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SET_V2_MEMBERS_TABLE}'"
        ).fetchall()
    except Exception as error:
        raise EvidenceSetV2Error("evidence v2 index tables are missing") from error
    segment_names = {str(row[0]) for row in segment_rows}
    set_names = {str(row[0]) for row in set_rows}
    member_names = {str(row[0]) for row in member_rows}
    if not segment_names or not set_names:
        if not create:
            raise EvidenceSetV2Error("evidence v2 index tables are missing")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVIDENCE_SEGMENT_V2_TABLE} (
                segment_id VARCHAR PRIMARY KEY,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                field_name VARCHAR NOT NULL,
                day VARCHAR NOT NULL,
                observation_ids_json VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                canonical_hash VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVIDENCE_SET_V2_TABLE} (
                evidence_set_id VARCHAR PRIMARY KEY,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                field_name VARCHAR NOT NULL,
                input_count INTEGER NOT NULL,
                head_offset INTEGER NOT NULL,
                inline_observation_ids_json VARCHAR NOT NULL,
                inline_available_at VARCHAR,
                first_observed_at VARCHAR NOT NULL,
                first_observation_id VARCHAR NOT NULL,
                last_observed_at VARCHAR NOT NULL,
                last_observation_id VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                canonical_hash VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVIDENCE_SET_V2_MEMBERS_TABLE} (
                evidence_set_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                segment_id VARCHAR NOT NULL,
                PRIMARY KEY (evidence_set_id, position)
            )
            """
        )
        segment_rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SEGMENT_V2_TABLE}'"
        ).fetchall()
        set_rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SET_V2_TABLE}'"
        ).fetchall()
        segment_names = {str(row[0]) for row in segment_rows}
        set_names = {str(row[0]) for row in set_rows}
        member_rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SET_V2_MEMBERS_TABLE}'"
        ).fetchall()
        member_names = {str(row[0]) for row in member_rows}
    if segment_names != set(_FULL_SEGMENT_V2_COLUMNS):
        raise EvidenceSetV2Error("evidence segment v2 table is incompatible")
    if set_names != set(_FULL_SET_V2_COLUMNS):
        raise EvidenceSetV2Error("evidence set v2 table is incompatible")
    if not member_names:
        raise EvidenceSetV2Error("evidence set member table is missing")
    if "document_json" in segment_names or "document_json" in set_names:
        raise EvidenceSetV2Error("evidence v2 index must not store documents")


def _segment_row(segment: EvidenceSegment) -> list[object]:
    return [
        str(segment.segment_id),
        segment.asset_id,
        segment.source_id,
        segment.field_name,
        segment.day.isoformat(),
        _ids_text(segment.observation_ids),
        _instant_text(segment.available_at),
        segment.canonical_hash,
    ]


def _set_row(evidence_set: EvidenceSet) -> list[object]:
    return [
        str(evidence_set.evidence_set_id),
        evidence_set.asset_id,
        evidence_set.source_id,
        evidence_set.field_name,
        evidence_set.input_count,
        evidence_set.head_offset,
        _ids_text(evidence_set.inline_observation_ids),
        _instant_text(evidence_set.inline_available_at)
        if evidence_set.inline_available_at is not None
        else None,
        _instant_text(evidence_set.first_observed_at),
        str(evidence_set.first_observation_id),
        _instant_text(evidence_set.last_observed_at),
        str(evidence_set.last_observation_id),
        _instant_text(evidence_set.available_at),
        evidence_set.canonical_hash,
    ]


def row_to_segment(row: tuple[object, ...]) -> EvidenceSegment:
    """Rehydrate the strict segment model, failing closed on drift."""
    if len(row) != len(_SEGMENT_V2_COLUMNS):
        raise EvidenceSetV2Error("evidence segment v2 row is malformed")
    (
        segment_id,
        asset_id,
        source_id,
        field_name,
        day,
        observation_ids_json,
        available_at,
        canonical_hash,
    ) = row
    identifiers = _parse_ids_text(observation_ids_json)
    try:
        return EvidenceSegment.model_validate(
            {
                "segment_id": str(segment_id),
                "asset_id": asset_id,
                "source_id": source_id,
                "field_name": field_name,
                "day": _parse_day(day).isoformat(),
                "observation_ids": [str(item) for item in identifiers],
                "available_at": _parse_instant_text(available_at),
                "canonical_hash": canonical_hash,
            }
        )
    except (ValueError, ValidationError, TypeError) as error:
        raise EvidenceSetV2Error("evidence segment v2 row does not validate") from error


def row_to_evidence_set(row: tuple[object, ...], *, segment_ids: list[UUID]) -> EvidenceSet:
    """Rehydrate the strict set model with its ordered member references."""
    if len(row) != len(_SET_V2_COLUMNS):
        raise EvidenceSetV2Error("evidence set v2 row is malformed")
    (
        evidence_set_id,
        asset_id,
        source_id,
        field_name,
        input_count,
        head_offset,
        inline_json,
        inline_available_at,
        first_observed_at,
        first_observation_id,
        last_observed_at,
        last_observation_id,
        available_at,
        canonical_hash,
    ) = row
    inline_ids = _parse_ids_text(inline_json)
    try:
        return EvidenceSet.model_validate(
            {
                "evidence_set_id": str(evidence_set_id),
                "asset_id": asset_id,
                "source_id": source_id,
                "field_name": field_name,
                "input_count": int(input_count),
                "segment_ids": [str(item) for item in segment_ids],
                "head_offset": int(head_offset),
                "inline_observation_ids": [str(item) for item in inline_ids],
                "inline_available_at": _parse_optional_instant(inline_available_at),
                "first_observed_at": _parse_instant_text(first_observed_at),
                "first_observation_id": str(first_observation_id),
                "last_observed_at": _parse_instant_text(last_observed_at),
                "last_observation_id": str(last_observation_id),
                "available_at": _parse_instant_text(available_at),
                "canonical_hash": canonical_hash,
            }
        )
    except (ValueError, ValidationError, TypeError) as error:
        raise EvidenceSetV2Error("evidence set v2 row does not validate") from error


class EvidenceSetV2Store:
    """Typed append-only store over the evidence v2 staging tables."""

    def __init__(self, connection: DuckDBPyConnection) -> None:
        self._connection = connection

    def save_segments(self, segments: Collection[EvidenceSegment]) -> int:
        """Persist segments idempotently; content drift fails closed."""
        ordered = sorted(segments, key=lambda item: str(item.segment_id))
        if not ordered:
            return 0
        keys = tuple(str(item.segment_id) for item in ordered)
        columns = ", ".join(_SEGMENT_V2_COLUMNS)
        existing: dict[str, tuple[object, ...]] = {}
        for chunk in chunked_sequence(keys, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in chunk)
            chunk_rows = self._connection.execute(
                f"SELECT {columns} FROM {EVIDENCE_SEGMENT_V2_TABLE} "
                f"WHERE segment_id IN ({placeholders})",
                list(chunk),
            ).fetchall()
            for row in chunk_rows:
                existing[str(row[0])] = row
        created = 0
        for segment in ordered:
            key = str(segment.segment_id)
            row = _segment_row(segment)
            if key in existing:
                if row_to_segment(existing[key]) != segment:
                    raise RecordConflictError(
                        f"evidence segment v2 {key!r} already has different content"
                    )
                continue
            placeholders_row = ", ".join("?" for _ in row)
            self._connection.execute(
                f"INSERT INTO {EVIDENCE_SEGMENT_V2_TABLE} ({columns}) VALUES ({placeholders_row})",
                row,
            )
            created += 1
        return created

    def save_set(self, evidence_set: EvidenceSet) -> bool:
        """Persist one set with ordered members; content drift fails closed."""
        segments_map = self.get_segments(evidence_set.segment_ids)
        try:
            verify_evidence_set(
                evidence_set,
                [segments_map[segment_id] for segment_id in evidence_set.segment_ids],
            )
        except EvidenceSetVerificationError as error:
            raise EvidenceSetV2Error("evidence set v2 lineage does not verify") from error
        key = str(evidence_set.evidence_set_id)
        columns = ", ".join(_SET_V2_COLUMNS)
        rows = self._connection.execute(
            f"SELECT {columns} FROM {EVIDENCE_SET_V2_TABLE} WHERE evidence_set_id = ?",
            [key],
        ).fetchall()
        members = self._connection.execute(
            f"SELECT position, segment_id FROM {EVIDENCE_SET_V2_MEMBERS_TABLE} "
            "WHERE evidence_set_id = ? ORDER BY position",
            [key],
        ).fetchall()
        positions = [int(row[0]) for row in members]
        if positions != list(range(len(positions))):
            raise EvidenceSetV2Error(
                f"evidence set v2 {key!r} members have non-contiguous positions: {positions}"
            )
        stored_members = [UUID(row[1]) for row in members]
        if rows:
            if row_to_evidence_set(rows[0], segment_ids=stored_members) != evidence_set:
                raise RecordConflictError(f"evidence set v2 {key!r} already has different content")
            return False
        row = _set_row(evidence_set)
        placeholders_row = ", ".join("?" for _ in row)
        self._connection.execute(
            f"INSERT INTO {EVIDENCE_SET_V2_TABLE} ({columns}) VALUES ({placeholders_row})",
            row,
        )
        for position, segment_id in enumerate(evidence_set.segment_ids):
            self._connection.execute(
                f"INSERT INTO {EVIDENCE_SET_V2_MEMBERS_TABLE} "
                "(evidence_set_id, position, segment_id) VALUES (?, ?, ?)",
                [key, position, str(segment_id)],
            )
        return True

    def get_segments(self, segment_ids: Collection[UUID]) -> dict[UUID, EvidenceSegment]:
        """Hydrate and verify unique segments in bounded chunks <= 256."""
        ordered = sorted(set(segment_ids), key=str)
        if not ordered:
            return {}
        columns = ", ".join(_SEGMENT_V2_COLUMNS)
        segments: dict[UUID, EvidenceSegment] = {}
        for chunk in chunked_sequence(ordered, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in chunk)
            chunk_rows = self._connection.execute(
                f"SELECT {columns} FROM {EVIDENCE_SEGMENT_V2_TABLE} "
                f"WHERE segment_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            for row in chunk_rows:
                seg = row_to_segment(row)
                segments[seg.segment_id] = seg
        for sid in ordered:
            if sid not in segments:
                raise EvidenceSetV2Error(f"evidence segment v2 {sid} was not found")
        return segments

    def get_segment(self, segment_id: UUID) -> EvidenceSegment:
        """Hydrate and verify one segment with its strict identity."""
        return self.get_segments([segment_id])[segment_id]

    def get_sets_and_lineages(
        self, evidence_set_ids: Collection[UUID]
    ) -> tuple[dict[UUID, EvidenceSet], dict[UUID, tuple[UUID, ...]]]:
        """Batch load unique sets, members, and segments without observation fetch."""
        ordered = sorted(set(evidence_set_ids), key=str)
        if not ordered:
            return {}, {}
        columns = ", ".join(_SET_V2_COLUMNS)
        set_rows: dict[str, tuple[object, ...]] = {}
        for chunk in chunked_sequence(ordered, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {EVIDENCE_SET_V2_TABLE} "
                f"WHERE evidence_set_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            for r in rows:
                set_rows[str(r[0])] = r

        for sid in ordered:
            if str(sid) not in set_rows:
                raise EvidenceSetV2Error(f"evidence set v2 {sid} was not found")

        members_by_set: dict[UUID, list[tuple[int, UUID]]] = {sid: [] for sid in ordered}
        all_segment_ids: set[UUID] = set()
        for chunk in chunked_sequence(ordered, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT evidence_set_id, position, segment_id "
                f"FROM {EVIDENCE_SET_V2_MEMBERS_TABLE} "
                f"WHERE evidence_set_id IN ({placeholders}) ORDER BY evidence_set_id, position",
                [str(item) for item in chunk],
            ).fetchall()
            for r in rows:
                s_id = UUID(str(r[0]))
                pos = int(r[1])
                seg_id = UUID(str(r[2]))
                members_by_set[s_id].append((pos, seg_id))
                all_segment_ids.add(seg_id)

        for sid, member_list in members_by_set.items():
            positions = [p for p, _ in member_list]
            if positions != list(range(len(member_list))):
                raise EvidenceSetV2Error(
                    f"evidence set v2 {sid} members have non-contiguous positions: {positions}"
                )

        segments_map = self.get_segments(all_segment_ids)

        sets_map: dict[UUID, EvidenceSet] = {}
        lineages_map: dict[UUID, tuple[UUID, ...]] = {}

        for sid in ordered:
            seg_ids = [seg_id for _, seg_id in members_by_set[sid]]
            evidence_set = row_to_evidence_set(set_rows[str(sid)], segment_ids=seg_ids)
            segs = [segments_map[seg_id] for seg_id in seg_ids]
            try:
                identifiers = resolve_evidence_set(evidence_set, segs)
            except EvidenceSetVerificationError as error:
                raise EvidenceSetV2Error("evidence set v2 lineage does not verify") from error
            sets_map[sid] = evidence_set
            lineages_map[sid] = identifiers

        return sets_map, lineages_map

    def verify_observation_lineages(
        self,
        evidence_sets: Collection[EvidenceSet],
        lineages: Mapping[UUID, tuple[UUID, ...]],
        observation_cache: Mapping[str, tuple[str, str, str, datetime]],
    ) -> None:
        """Verify that observations for each set match asset, source, field, and visibility."""
        for evidence_set in evidence_sets:
            identifiers = lineages[evidence_set.evidence_set_id]
            for identifier in identifiers:
                row = observation_cache.get(str(identifier))
                if row is None:
                    raise EvidenceSetV2Error(
                        f"evidence set v2 references a missing observation {identifier}"
                    )
                obs_asset, obs_source, obs_field, obs_avail = row
                if (
                    obs_asset != evidence_set.asset_id
                    or obs_source != evidence_set.source_id
                    or obs_field != evidence_set.field_name
                ):
                    raise EvidenceSetV2Error(
                        f"evidence set v2 references a foreign observation {identifier}"
                    )
                if obs_avail > evidence_set.available_at:
                    raise EvidenceSetV2Error(
                        f"evidence set v2 references a future observation {identifier}"
                    )

    def get_sets(
        self,
        evidence_set_ids: Collection[UUID],
        *,
        observation_cache: Mapping[str, tuple[str, str, str, datetime]] | None = None,
    ) -> dict[UUID, EvidenceSet]:
        """Hydrate and verify multiple sets against members and observations in bounded chunks."""
        sets_map, lineages_map = self.get_sets_and_lineages(evidence_set_ids)
        if not sets_map:
            return {}
        if observation_cache is None:
            all_obs: set[UUID] = set()
            for identifiers in lineages_map.values():
                all_obs.update(identifiers)
            ordered_obs = sorted(all_obs, key=str)
            obs_map: dict[str, tuple[str, str, str, datetime]] = {}
            for chunk in chunked_sequence(ordered_obs, MAX_CHUNK_SIZE):
                placeholders = ", ".join("?" for _ in chunk)
                chunk_rows = self._connection.execute(
                    "SELECT observation_id, asset_id, source_id, field_name, available_at "
                    "FROM normalized_observations_v2 "
                    f"WHERE observation_id IN ({placeholders})",
                    [str(item) for item in chunk],
                ).fetchall()
                for r in chunk_rows:
                    obs_map[str(r[0])] = (
                        str(r[1]),
                        str(r[2]),
                        str(r[3]),
                        _parse_instant_text(r[4]),
                    )
            self.verify_observation_lineages(sets_map.values(), lineages_map, obs_map)
        else:
            self.verify_observation_lineages(sets_map.values(), lineages_map, observation_cache)
        return sets_map

    def get_set(self, evidence_set_id: UUID) -> EvidenceSet:
        """Hydrate and verify one set against its members and observations."""
        return self.get_sets([evidence_set_id])[evidence_set_id]

    def verify_set_lineage(
        self,
        evidence_set: EvidenceSet,
        *,
        observation_cache: Mapping[str, tuple[str, str, str, datetime]] | None = None,
    ) -> tuple[UUID, ...]:
        """Verify segments, hash, shape, availability and every observation."""
        segments_map = self.get_segments(evidence_set.segment_ids)
        segments = [segments_map[segment_id] for segment_id in evidence_set.segment_ids]
        try:
            identifiers = resolve_evidence_set(evidence_set, segments)
        except EvidenceSetVerificationError as error:
            raise EvidenceSetV2Error("evidence set v2 lineage does not verify") from error
        ordered = tuple(sorted(set(identifiers), key=str))
        if not ordered:
            return identifiers
        if observation_cache is not None:
            self.verify_observation_lineages(
                [evidence_set], {evidence_set.evidence_set_id: identifiers}, observation_cache
            )
            return identifiers
        obs_map: dict[str, tuple[str, str, str, datetime]] = {}
        for chunk in chunked_sequence(ordered, MAX_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in chunk)
            chunk_rows = self._connection.execute(
                "SELECT observation_id, asset_id, source_id, field_name, available_at "
                "FROM normalized_observations_v2 "
                f"WHERE observation_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            for row in chunk_rows:
                obs_map[str(row[0])] = (
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    _parse_instant_text(row[4]),
                )
        self.verify_observation_lineages(
            [evidence_set], {evidence_set.evidence_set_id: identifiers}, obs_map
        )
        return identifiers


__all__ = [
    "EVIDENCE_SEGMENT_V2_TABLE",
    "EVIDENCE_SET_V2_MEMBERS_TABLE",
    "EVIDENCE_SET_V2_TABLE",
    "EvidenceSet",
    "EvidenceSetV2Error",
    "EvidenceSetV2Store",
    "ensure_evidence_v2_tables",
    "evidence_v2_tables_exist",
    "row_to_evidence_set",
    "row_to_segment",
]
