"""Immutable canonical JSON storage for original records."""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from duckdb import DuckDBPyConnection

from investment_analyst.core.models import RawRecord
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.paths import StoragePaths
from investment_analyst.storage.serialization import (
    canonical_json_bytes,
    model_from_json,
    sha256_hex,
)

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_INDEX_INTEGRITY_RECORDS = 1_000


def _safe_source_component(source_id: str) -> str:
    slug = _SAFE_COMPONENT.sub("_", source_id).strip("._-") or "source"
    digest = sha256_hex(source_id.encode("utf-8"))[:12]
    return f"{slug[:64]}-{digest}"


def _ensure_within(base: Path, candidate: Path) -> Path:
    candidate_resolved = candidate.resolve()
    if not candidate_resolved.is_relative_to(base):
        raise StorageError("raw record path escapes the configured raw directory")
    return candidate_resolved


class JsonRawRecordRepository:
    """Store canonical RawRecord documents as immutable JSON files."""

    def __init__(
        self,
        paths: StoragePaths,
        connection: DuckDBPyConnection,
        *,
        read_only: bool = False,
    ) -> None:
        self._paths = paths
        self._raw_root = paths.raw_dir.resolve()
        self._connection = connection
        self._read_only = read_only

    def save(self, record: RawRecord) -> RawRecord:
        if self._read_only:
            raise StorageError("raw records cannot be saved through read-only storage")
        document_bytes = canonical_json_bytes(record)
        document_text = document_bytes.decode("utf-8")
        checksum = sha256_hex(document_bytes)
        existing = self._index_row(record.record_id)
        if existing is not None:
            if existing[2] != document_text:
                raise RecordConflictError(
                    f"raw record {record.record_id} already has different content"
                )
            self._verify_file(existing[0], existing[1])
            return record

        relative_path = self._relative_path(record)
        target = _ensure_within(self._raw_root, self._raw_root / relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_bytes = target.read_bytes()
            if existing_bytes != document_bytes:
                raise RecordConflictError(
                    f"raw record path for {record.record_id} already contains different content"
                )
        else:
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(document_bytes)
                if target.exists():
                    existing_bytes = target.read_bytes()
                    if existing_bytes != document_bytes:
                        raise RecordConflictError(
                            f"raw record {record.record_id} was created concurrently"
                        )
                else:
                    temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

        self._connection.execute(
            """
            INSERT INTO raw_record_index (
                record_id, asset_id, source_id, event_time, available_at, received_at,
                relative_path, checksum_sha256, schema_version, document_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(record.record_id),
                record.asset_id,
                record.source.source_id,
                record.event_time,
                record.available_at,
                record.received_at,
                relative_path.as_posix(),
                checksum,
                record.schema_version,
                document_text,
            ],
        )
        return record

    def get(self, record_id: UUID) -> RawRecord:
        row = self._index_row(record_id)
        if row is None:
            raise RecordNotFoundError(f"raw record {record_id} was not found")
        record = self._verify_file(row[0], row[1])
        if record.record_id != record_id:
            raise StorageError("stored raw record identifier does not match its index")
        return record

    def get_many(self, record_ids: Collection[UUID]) -> dict[UUID, RawRecord]:
        ordered_ids = tuple(sorted(set(record_ids), key=str))
        if not ordered_ids:
            return {}
        placeholders = ", ".join("?" for _ in ordered_ids)
        rows = self._connection.execute(
            f"""
            SELECT record_id, relative_path, checksum_sha256
            FROM raw_record_index
            WHERE record_id IN ({placeholders})
            """,  # noqa: S608
            [str(record_id) for record_id in ordered_ids],
        ).fetchall()
        indexed = {UUID(row[0]): (row[1], row[2]) for row in rows}
        missing = [record_id for record_id in ordered_ids if record_id not in indexed]
        if missing:
            raise RecordNotFoundError(f"raw record {missing[0]} was not found")

        records: dict[UUID, RawRecord] = {}
        for record_id in ordered_ids:
            record = self._verify_file(*indexed[record_id])
            if record.record_id != record_id:
                raise StorageError("stored raw record identifier does not match its index")
            records[record_id] = record
        return records

    def list(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[RawRecord]:
        record_ids = self.list_record_ids(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
            received_from=received_from,
            received_to=received_to,
        )
        records = self.get_many(record_ids)
        return [records[record_id] for record_id in record_ids]

    def list_record_ids(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[UUID]:
        """Return indexed identifiers in stable order without loading JSON documents."""
        clauses, parameters = self._build_filter_clauses(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
            received_from=received_from,
            received_to=received_to,
        )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT record_id FROM raw_record_index{where} ORDER BY received_at, record_id",
            parameters,
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def verify_index_integrity(self, record_ids: Collection[UUID]) -> int:
        """Verify a bounded index/file document equality check on demand.

        The durable SHA-256 check remains part of every materialized read. This
        deliberately more expensive comparison is for maintenance and audit
        operations, where it can detect a divergent duplicate index document.
        """
        ordered_ids = tuple(sorted(set(record_ids), key=str))
        if len(ordered_ids) > _MAX_INDEX_INTEGRITY_RECORDS:
            raise StorageError(
                "raw record index integrity verification exceeds the bounded record limit"
            )
        for record_id in ordered_ids:
            row = self._index_row(record_id)
            if row is None:
                raise RecordNotFoundError(f"raw record {record_id} was not found")
            data = self._verified_file_bytes(row[0], row[1])
            if data.decode("utf-8") != row[2]:
                raise StorageError(f"raw record index does not match file: {row[0]}")
            record = model_from_json(RawRecord, data)
            if record.record_id != record_id:
                raise StorageError("stored raw record identifier does not match its index")
        return len(ordered_ids)

    def count(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> int:
        """Count indexed records without loading their JSON documents."""
        clauses, parameters = self._build_filter_clauses(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
            available_to=available_to,
            received_from=received_from,
            received_to=received_to,
        )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            f"SELECT count(*) FROM raw_record_index{where}",  # noqa: S608
            parameters,
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def available_at_bounds(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        """Return exact indexed availability edges without loading raw documents."""
        clauses, parameters = self._build_filter_clauses(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
        )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._connection.execute(
            "SELECT cast(MIN(available_at) AS VARCHAR), cast(MAX(available_at) AS VARCHAR) "
            f"FROM raw_record_index{where}",  # noqa: S608
            parameters,
        ).fetchone()
        if row is None:
            return None, None
        return self._parse_timestamp(row[0]), self._parse_timestamp(row[1])

    def _build_filter_clauses(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> tuple[list[str], list[object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            parameters.append(source_id)
        if schema_version is not None:
            clauses.append("schema_version = ?")
            parameters.append(schema_version)
        if available_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(available_to)
        if received_from is not None:
            clauses.append("received_at >= ?")
            parameters.append(received_from)
        if received_to is not None:
            clauses.append("received_at <= ?")
            parameters.append(received_to)
        return clauses, parameters

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def _relative_path(self, record: RawRecord) -> Path:
        received_date = record.received_at.date().isoformat()
        return (
            Path(f"source={_safe_source_component(record.source.source_id)}")
            / f"received_date={received_date}"
            / f"{record.record_id}.json"
        )

    def _index_row(self, record_id: UUID) -> tuple[str, str, str] | None:
        row = self._connection.execute(
            """
            SELECT relative_path, checksum_sha256, document_json
            FROM raw_record_index
            WHERE record_id = ?
            """,
            [str(record_id)],
        ).fetchone()
        if row is None:
            return None
        return row[0], row[1], row[2]

    def _verify_file(
        self,
        relative_path: str,
        checksum: str,
    ) -> RawRecord:
        return model_from_json(RawRecord, self._verified_file_bytes(relative_path, checksum))

    def _verified_file_bytes(self, relative_path: str, checksum: str) -> bytes:
        target = _ensure_within(self._raw_root, self._raw_root / relative_path)
        if not target.is_file():
            raise StorageError(f"indexed raw record file is missing: {relative_path}")
        data = target.read_bytes()
        if sha256_hex(data) != checksum:
            raise StorageError(f"checksum mismatch for raw record file: {relative_path}")
        return data
