"""Isolated raw v2 staging substrate with content-addressed blobs and a queryable index.

Pure staging contract for the DATA-CHASSIS stage 7 raw subphase. A staging
destination is an explicit, absolute, new directory that is neither a v1
workspace nor a symlink. Blobs live content-addressed by canonical SHA-256
under ``raw/sha256/``; a dedicated DuckDB connection holds the ``raw_v2_index``
table without any ``document_json`` column. Typed 13F projections (manager and
report) are extracted from the validated ``RawRecord`` at insert time and
re-verified against the file on every read. ``available_at`` governs
point-in-time selection; order and identifiers follow the v1 contract. Only
test scratch destinations are authorized writers; the permanent workspace,
providers, credentials and installed runtimes stay out of reach.
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from duckdb import DuckDBPyConnection
from pydantic import ValidationError

from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import RawRecord
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.serialization import (
    canonical_json_bytes,
    model_from_json,
    sha256_hex,
)

STAGING_FORMAT = "raw-v2-staging-v1"
_STAGING_MARKER_FILENAME = "raw-v2-staging.json"
_BLOB_DIR_PARTS = ("raw", "sha256")
_INDEX_TABLE = "raw_v2_index"
_RAW_V2_BATCH_CHUNK_SIZE = 512
_DUCKDB_FILE_SUFFIXES = frozenset({".duckdb", ".wal"})
_INDEX_COLUMNS = (
    "record_id",
    "relative_path",
    "checksum_sha256",
    "asset_id",
    "source_id",
    "event_time",
    "available_at",
    "received_at",
    "schema_version",
    "projected_manager_cik",
    "projected_report_id",
)
_FULL_INDEX_COLUMNS = (*_INDEX_COLUMNS, "inserted_at")

_OPEN_WRITERS: set[str] = set()


class RawV2StagingError(StorageError):
    """Raised when a raw v2 staging destination, marker or blob cannot be trusted."""


class RawV2StagingMarker(ContractModel):
    """Typed versioned marker of one raw v2 staging destination."""

    format: Literal["raw-v2-staging-v1"] = STAGING_FORMAT
    created_at: UTCDateTime


def _project_13f_fields(record: RawRecord) -> tuple[str | None, str | None]:
    """Extract typed 13F projections from a validated record payload.

    Report payloads project their manager and outcome payloads project their
    filer, mirroring the v1 report-only and outcome-only selections;
    positions project their report UUID.
    """
    payload = record.payload
    if not isinstance(payload, dict):
        return None, None
    manager_cik: str | None = None
    report = payload.get("report")
    if isinstance(report, dict):
        candidate = report.get("manager_cik")
        if isinstance(candidate, str) and candidate.strip():
            manager_cik = candidate
    if manager_cik is None:
        outcome = payload.get("outcome")
        if isinstance(outcome, dict):
            filing = outcome.get("filing")
            if isinstance(filing, dict):
                candidate = filing.get("filer_cik")
                if isinstance(candidate, str) and candidate.strip():
                    manager_cik = candidate
    report_id: str | None = None
    position = payload.get("position")
    if isinstance(position, dict):
        candidate = position.get("report_id")
        if isinstance(candidate, str) and candidate.strip():
            report_id = candidate
    return manager_cik, report_id


def _instant_text(value: datetime | None) -> str | None:
    """Store instants as canonical ISO text; DuckDB TIMESTAMPTZ needs pytz to bind."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise RawV2StagingError("raw v2 instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant_text(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RawV2StagingError("raw v2 index instant is not timezone-aware")
    return parsed.astimezone(UTC)


class RawV2Staging:
    """Stage canonical RawRecord blobs with a queryable, JSON-free index."""

    def __init__(
        self,
        destination: Path,
        connection: DuckDBPyConnection,
        *,
        read_only: bool = False,
    ) -> None:
        if not destination.is_absolute():
            raise RawV2StagingError("raw v2 staging destination must be absolute")
        self._destination = destination
        self._connection = connection
        self._read_only = read_only
        self._raw_root = destination / _BLOB_DIR_PARTS[0]
        self._is_open = False

    @property
    def is_open(self) -> bool:
        """Return whether this staging currently owns its writer slot."""
        return self._is_open

    @property
    def destination(self) -> Path:
        """Return the absolute staging destination supplied at construction."""
        return self._destination

    def open(self) -> RawV2Staging:
        """Validate the destination and marker without trusting prior bytes."""
        if self._is_open:
            return self
        destination = self._destination
        if destination.is_symlink():
            raise RawV2StagingError("raw v2 staging destination cannot be a symbolic link")
        if (destination / "manifest.json").exists() or (destination / "storage").exists():
            raise RawV2StagingError("raw v2 staging destination cannot be a v1 workspace")
        marker_path = destination / _STAGING_MARKER_FILENAME
        if self._read_only:
            self._read_marker(marker_path)
            self._require_index_table(marker_path, create=False)
            self._is_open = True
            return self
        key = str(destination.absolute())
        if key in _OPEN_WRITERS:
            raise RawV2StagingError("raw v2 staging destination already has a writer")
        if marker_path.exists() or marker_path.is_symlink():
            self._read_marker(marker_path)
        elif destination.exists():
            unexpected = [
                entry.name
                for entry in destination.iterdir()
                if not (
                    entry.is_file()
                    and not entry.is_symlink()
                    and entry.suffix in _DUCKDB_FILE_SUFFIXES
                )
            ]
            if unexpected:
                raise RawV2StagingError(
                    "raw v2 staging destination is not new and has no staging marker"
                )
        else:
            destination.mkdir(parents=True, exist_ok=False)
        self._raw_root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlinks(self._raw_root)
        if not marker_path.exists():
            self._write_marker(marker_path)
        self._require_index_table(marker_path, create=True)
        _OPEN_WRITERS.add(key)
        self._is_open = True
        return self

    def close(self) -> None:
        """Release the writer slot without touching staged bytes."""
        if not self._is_open:
            return
        if not self._read_only:
            _OPEN_WRITERS.discard(str(self._destination.absolute()))
        self._is_open = False

    def __enter__(self) -> RawV2Staging:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        self.close()

    def save(self, record: RawRecord) -> RawRecord:
        """Persist one record idempotently; conflicts fail closed."""
        self._require_writable()
        created, reused = self._save_chunk([record])
        if len(created) + len(reused) != 1:
            raise RawV2StagingError("raw v2 staging save did not settle exactly one record")
        return record

    def save_many(self, records: Collection[RawRecord]) -> BatchWriteReceipt:
        """Persist batches idempotently; a failure confirms no partial batch."""
        self._require_writable()
        if not records:
            return BatchWriteReceipt()
        created_ids: list[UUID] = []
        reused_ids: list[UUID] = []
        records_list = list(records)
        for start in range(0, len(records_list), _RAW_V2_BATCH_CHUNK_SIZE):
            chunk_created, chunk_reused = self._save_chunk(
                records_list[start : start + _RAW_V2_BATCH_CHUNK_SIZE]
            )
            created_ids.extend(chunk_created)
            reused_ids.extend(chunk_reused)
        return BatchWriteReceipt(
            created_ids=tuple(created_ids),
            reused_ids=tuple(reused_ids),
            conflicting_ids=(),
        )

    def get(self, record_id: UUID) -> RawRecord:
        """Hydrate one record after hash, identity and projection verification."""
        rows = self._select_rows([record_id])
        if not rows:
            raise RecordNotFoundError(f"raw v2 record {record_id} was not found")
        return self._hydrate(record_id, rows[0])

    def get_many(self, record_ids: Collection[UUID]) -> dict[UUID, RawRecord]:
        """Hydrate verified records in deterministic order."""
        ordered_ids = tuple(sorted(set(record_ids), key=str))
        if not ordered_ids:
            return {}
        rows = self._select_rows(ordered_ids)
        indexed = {UUID(row[0]): row for row in rows}
        missing = [record_id for record_id in ordered_ids if record_id not in indexed]
        if missing:
            raise RecordNotFoundError(f"raw v2 record {missing[0]} was not found")
        return {
            record_id: self._hydrate(record_id, indexed[record_id]) for record_id in ordered_ids
        }

    def list_record_ids(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        manager_cik: str | None = None,
        report_id: str | None = None,
    ) -> list[UUID]:
        """Select indexed identifiers in stable order without loading blobs."""
        self._require_open()
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
            parameters.append(_instant_text(available_to))
        if manager_cik is not None:
            clauses.append("projected_manager_cik = ?")
            parameters.append(manager_cik)
        if report_id is not None:
            clauses.append("projected_report_id = ?")
            parameters.append(report_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT record_id FROM {_INDEX_TABLE}{where} ORDER BY received_at, record_id",
            parameters,
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def list_inventory_page(
        self,
        *,
        limit: int,
        after_received_at: datetime | None = None,
        after_record_id: UUID | None = None,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
        available_to: datetime | None = None,
        manager_cik: str | None = None,
        report_id: str | None = None,
    ) -> list[UUID]:
        """Return one bounded keyset page of staged IDs without loading blobs.

        Pages follow the stable ``(received_at, record_id)`` cursor in the same
        order as the v1 import pages, so a verifier can walk both inventories
        side by side without materializing either corpus.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise RawV2StagingError("inventory page limit must be an integer")
        if limit < 1 or limit > 256:
            raise RawV2StagingError("inventory page limit must be between 1 and 256")
        if (after_received_at is None) != (after_record_id is None):
            raise RawV2StagingError("inventory cursor requires received_at and record_id together")
        self._require_open()
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
            parameters.append(_instant_text(available_to))
        if manager_cik is not None:
            clauses.append("projected_manager_cik = ?")
            parameters.append(manager_cik)
        if report_id is not None:
            clauses.append("projected_report_id = ?")
            parameters.append(report_id)
        if after_received_at is not None and after_record_id is not None:
            if after_received_at.tzinfo is None or after_received_at.utcoffset() is None:
                raise RawV2StagingError("inventory cursor received_at must be timezone-aware")
            clauses.append("(received_at, record_id) > (?, ?)")
            parameters.extend([_instant_text(after_received_at), str(after_record_id)])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT record_id FROM {_INDEX_TABLE}{where} ORDER BY received_at, record_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def _save_chunk(self, chunk: list[RawRecord]) -> tuple[list[UUID], list[UUID]]:
        chunk_created: list[UUID] = []
        chunk_reused: list[UUID] = []
        serialized: dict[UUID, tuple[bytes, str]] = {}
        for record in chunk:
            document = canonical_json_bytes(record)
            checksum = sha256_hex(document)
            if record.record_id in serialized:
                if serialized[record.record_id] != (document, checksum):
                    raise RecordConflictError(
                        f"raw v2 record {record.record_id} already has different content"
                    )
            else:
                serialized[record.record_id] = (document, checksum)
        unique_ids = tuple(serialized.keys())
        placeholders = ", ".join("?" for _ in unique_ids)
        columns = ", ".join(_INDEX_COLUMNS)
        rows = self._connection.execute(
            f"SELECT {columns} FROM {_INDEX_TABLE} WHERE record_id IN ({placeholders})",
            [str(record_id) for record_id in unique_ids],
        ).fetchall()
        existing = {UUID(row[0]): row for row in rows}
        insert_rows: list[list[object]] = []
        seen_chunk_keys: set[UUID] = set()
        for record in chunk:
            record_id = record.record_id
            document, checksum = serialized[record_id]
            if record_id in existing:
                if existing[record_id][2] != checksum:
                    raise RecordConflictError(
                        f"raw v2 record {record_id} already has different content"
                    )
                self._hydrate(record_id, existing[record_id])
                chunk_reused.append(record_id)
                seen_chunk_keys.add(record_id)
            elif record_id not in seen_chunk_keys:
                relative_path = self._write_blob(checksum, document)
                manager_cik, report_uuid = _project_13f_fields(record)
                insert_rows.append(
                    [
                        str(record_id),
                        relative_path.as_posix(),
                        checksum,
                        record.asset_id,
                        record.source.source_id,
                        _instant_text(record.event_time),
                        _instant_text(record.available_at),
                        _instant_text(record.received_at),
                        record.schema_version,
                        manager_cik,
                        report_uuid,
                    ]
                )
                chunk_created.append(record_id)
                seen_chunk_keys.add(record_id)
            else:
                chunk_reused.append(record_id)
        if insert_rows:
            row_placeholder = "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            values_clause = ", ".join(row_placeholder for _ in insert_rows)
            params = [value for row_data in insert_rows for value in row_data]
            self._connection.execute(
                f"INSERT INTO {_INDEX_TABLE} ({columns}) VALUES {values_clause}",
                params,
            )
        return chunk_created, chunk_reused

    def _select_rows(self, record_ids: Sequence[UUID]) -> list[tuple[object, ...]]:
        self._require_open()
        columns = ", ".join(_INDEX_COLUMNS)
        placeholders = ", ".join("?" for _ in record_ids)
        return self._connection.execute(
            f"SELECT {columns} FROM {_INDEX_TABLE} WHERE record_id IN ({placeholders})",
            [str(record_id) for record_id in record_ids],
        ).fetchall()

    def _hydrate(self, record_id: UUID, row: tuple[object, ...]) -> RawRecord:
        (
            _indexed_id,
            relative_path,
            checksum,
            asset_id,
            source_id,
            event_time,
            available_at,
            received_at,
            schema_version,
            projected_manager_cik,
            projected_report_id,
        ) = row
        if not isinstance(relative_path, str) or not isinstance(checksum, str):
            raise RawV2StagingError("raw v2 index row is malformed")
        data = self._read_blob(relative_path, checksum)
        record = model_from_json(RawRecord, data)
        if record.record_id != record_id:
            raise RawV2StagingError("stored raw v2 record identifier does not match its index")
        if (
            record.asset_id != asset_id
            or record.source.source_id != source_id
            or record.schema_version != schema_version
            or _parse_instant_text(event_time) != record.event_time
            or _parse_instant_text(available_at) != record.available_at
            or _parse_instant_text(received_at) != record.received_at
        ):
            raise RawV2StagingError("raw v2 index metadata does not match its file")
        manager_cik, report_uuid = _project_13f_fields(record)
        if manager_cik != projected_manager_cik or report_uuid != projected_report_id:
            raise RawV2StagingError("raw v2 index projection does not match its file")
        return record

    def _blob_relative_path(self, checksum: str) -> Path:
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise RawV2StagingError("raw v2 blob checksum is invalid")
        return Path(*_BLOB_DIR_PARTS) / checksum[:2] / checksum[2:4] / checksum

    def _resolve_blob(self, relative_path: str) -> Path:
        candidate = self._raw_root / Path(relative_path)
        self._assert_no_symlinks(candidate)
        resolved = candidate.resolve()
        raw_root = self._raw_root.resolve()
        if not resolved.is_relative_to(raw_root):
            raise RawV2StagingError("raw v2 blob path escapes the staging destination")
        return resolved

    def _write_blob(self, checksum: str, document: bytes) -> Path:
        relative_path = self._blob_relative_path(checksum)
        target = self._resolve_blob(relative_path.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlinks(target)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise RawV2StagingError("raw v2 blob is missing or not a regular file")
            existing = target.read_bytes()
            if existing != document:
                raise RawV2StagingError("raw v2 blob hash collision or conflicting bytes")
            return relative_path
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(document)
            if target.exists():
                existing = target.read_bytes()
                if existing != document:
                    raise RawV2StagingError(f"raw v2 blob for {checksum} was created concurrently")
            else:
                os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self._read_blob(relative_path.as_posix(), checksum)
        return relative_path

    def _read_blob(self, relative_path: str, checksum: str) -> bytes:
        target = self._resolve_blob(relative_path)
        if target.is_symlink() or not target.is_file():
            raise RawV2StagingError(f"indexed raw v2 blob is missing: {relative_path}")
        data = target.read_bytes()
        if sha256_hex(data) != checksum:
            raise RawV2StagingError(f"checksum mismatch for raw v2 blob: {relative_path}")
        return data

    def _assert_no_symlinks(self, target: Path) -> None:
        for candidate in (self._destination, self._raw_root, target.parent, target):
            if candidate.is_symlink():
                raise RawV2StagingError("raw v2 staging cannot use symbolic links")

    def _read_marker(self, marker_path: Path) -> RawV2StagingMarker:
        if marker_path.is_symlink() or not marker_path.is_file():
            raise RawV2StagingError("raw v2 staging marker is missing or not a regular file")
        try:
            document = json.loads(marker_path.read_bytes().decode("utf-8"))
            return RawV2StagingMarker.model_validate(document)
        except (ValueError, ValidationError, UnicodeDecodeError) as error:
            raise RawV2StagingError("raw v2 staging marker is incompatible") from error

    def _write_marker(self, marker_path: Path) -> None:
        if marker_path.is_symlink() or marker_path.exists():
            raise RawV2StagingError("raw v2 staging marker already exists")
        document = (
            json.dumps(
                RawV2StagingMarker(created_at=datetime.now(UTC)).model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        temporary = marker_path.with_name(f".{marker_path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(document, encoding="utf-8")
            os.replace(temporary, marker_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _index_columns(self) -> set[str]:
        try:
            rows = self._connection.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{_INDEX_TABLE}'"
            ).fetchall()
        except Exception as error:
            raise RawV2StagingError("raw v2 index table is missing") from error
        return {str(row[0]) for row in rows}

    def _require_index_table(self, marker_path: Path, *, create: bool) -> None:
        names = self._index_columns()
        if not names:
            if not create:
                raise RawV2StagingError("raw v2 index table is missing")
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_INDEX_TABLE} (
                    record_id VARCHAR PRIMARY KEY,
                    asset_id VARCHAR,
                    source_id VARCHAR NOT NULL,
                    event_time VARCHAR,
                    available_at VARCHAR NOT NULL,
                    received_at VARCHAR NOT NULL,
                    relative_path VARCHAR NOT NULL,
                    checksum_sha256 VARCHAR NOT NULL,
                    schema_version VARCHAR NOT NULL,
                    projected_manager_cik VARCHAR,
                    projected_report_id VARCHAR,
                    inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
                )
                """
            )
            names = self._index_columns()
        if names != set(_FULL_INDEX_COLUMNS):
            raise RawV2StagingError(f"raw v2 index table is incompatible near {marker_path}")
        if not create:
            return
        count = self._connection.execute(f"SELECT count(*) FROM {_INDEX_TABLE}").fetchone()
        if count is not None and int(count[0]) > 0 and not self._marker_matches(marker_path):
            raise RawV2StagingError("raw v2 index holds rows without a staging marker")

    def _marker_matches(self, marker_path: Path) -> bool:
        try:
            self._read_marker(marker_path)
        except RawV2StagingError:
            return False
        return True

    def _require_open(self) -> None:
        if not self._is_open:
            raise RawV2StagingError("raw v2 staging is not open")

    def _require_writable(self) -> None:
        self._require_open()
        if self._read_only:
            raise RawV2StagingError("raw v2 staging cannot be written through read-only access")


__all__ = [
    "STAGING_FORMAT",
    "RawV2Staging",
    "RawV2StagingError",
    "RawV2StagingMarker",
]
