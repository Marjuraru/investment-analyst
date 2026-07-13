"""Immutable canonical JSON storage for original records."""

import re
from datetime import datetime
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
    canonical_json_text,
    model_from_json,
    sha256_hex,
)

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_source_component(source_id: str) -> str:
    slug = _SAFE_COMPONENT.sub("_", source_id).strip("._-") or "source"
    digest = sha256_hex(source_id.encode("utf-8"))[:12]
    return f"{slug[:64]}-{digest}"


def _ensure_within(base: Path, candidate: Path) -> Path:
    base_resolved = base.resolve()
    candidate_resolved = candidate.resolve()
    if not candidate_resolved.is_relative_to(base_resolved):
        raise StorageError("raw record path escapes the configured raw directory")
    return candidate_resolved


class JsonRawRecordRepository:
    """Store canonical RawRecord documents as immutable JSON files."""

    def __init__(self, paths: StoragePaths, connection: DuckDBPyConnection) -> None:
        self._paths = paths
        self._connection = connection

    def save(self, record: RawRecord) -> RawRecord:
        document_bytes = canonical_json_bytes(record)
        document_text = document_bytes.decode("utf-8")
        checksum = sha256_hex(document_bytes)
        existing = self._index_row(record.record_id)
        if existing is not None:
            if existing[2] != document_text:
                raise RecordConflictError(
                    f"raw record {record.record_id} already has different content"
                )
            self._verify_file(existing[0], existing[1], existing[2])
            return record

        relative_path = self._relative_path(record)
        target = _ensure_within(self._paths.raw_dir, self._paths.raw_dir / relative_path)
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
        data = self._verify_file(row[0], row[1], row[2])
        record = model_from_json(RawRecord, data)
        if record.record_id != record_id:
            raise StorageError("stored raw record identifier does not match its index")
        return record

    def list(
        self,
        *,
        source_id: str | None = None,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
    ) -> list[RawRecord]:
        clauses: list[str] = []
        parameters: list[object] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            parameters.append(source_id)
        if received_from is not None:
            clauses.append("received_at >= ?")
            parameters.append(received_from)
        if received_to is not None:
            clauses.append("received_at <= ?")
            parameters.append(received_to)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT record_id FROM raw_record_index{where} ORDER BY received_at, record_id",
            parameters,
        ).fetchall()
        return [self.get(UUID(row[0])) for row in rows]

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

    def _verify_file(self, relative_path: str, checksum: str, document_json: str) -> bytes:
        target = _ensure_within(self._paths.raw_dir, self._paths.raw_dir / relative_path)
        if not target.is_file():
            raise StorageError(f"indexed raw record file is missing: {relative_path}")
        data = target.read_bytes()
        if sha256_hex(data) != checksum:
            raise StorageError(f"checksum mismatch for raw record file: {relative_path}")
        record = model_from_json(RawRecord, data)
        if canonical_json_text(record) != document_json:
            raise StorageError(f"raw record index does not match file: {relative_path}")
        return data
