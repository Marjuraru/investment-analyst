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

import fcntl
import json
import os
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from duckdb import DuckDBPyConnection
from pydantic import ValidationError

from investment_analyst.analytics.analysis_snapshot import AnalysisSnapshot
from investment_analyst.analytics.evidence_set import EvidenceSegment, EvidenceSet
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import (
    DiagnosticMode,
    DiagnosticResult,
    MetricResult,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.storage.analysis_snapshot_v2 import (
    AnalysisSnapshotV2Store,
    ensure_analysis_snapshot_v2_tables,
)
from investment_analyst.storage.diagnostic_v2 import (
    DiagnosticV2Store,
    ensure_diagnostic_v2_tables,
)
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.evidence_set_v2 import (
    EvidenceSetV2Error,
    EvidenceSetV2Store,
    ensure_evidence_v2_tables,
)
from investment_analyst.storage.metric_v2 import (
    MAX_METRIC_V2_PAGE,
    MetricV2Error,
    MetricV2Store,
    ensure_metric_v2_tables,
)
from investment_analyst.storage.observation_v2 import (
    MAX_OBSERVATION_V2_PAGE,
    ObservationV2Error,
    ObservationV2Store,
    ensure_observation_v2_table,
    observation_to_row,
    row_to_observation,
)
from investment_analyst.storage.serialization import (
    canonical_json_bytes,
    model_from_json,
    sha256_hex,
)

_STAGING_FORMAT = "raw-v2-staging-v1"
STAGING_FORMAT = _STAGING_FORMAT
_STAGING_MARKER_FILENAME = "raw-v2-staging.json"
_STAGING_LOCK_FILENAME = "raw-v2-staging.lock"
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
    staging_id: str | None = None
    created_at: UTCDateTime

    @classmethod
    def validate_legacy_marker(cls, marker_path: Path) -> RawV2StagingMarker:
        """Read a legacy marker byte-identically compatible with the prior contract."""
        if marker_path.is_symlink() or not marker_path.is_file():
            raise RawV2StagingError("raw v2 staging marker is missing or not a regular file")
        try:
            document = json.loads(marker_path.read_bytes().decode("utf-8"))
            return cls.model_validate(document)
        except (ValueError, ValidationError, UnicodeDecodeError) as error:
            raise RawV2StagingError("raw v2 staging marker is incompatible") from error


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
            ensure_observation_v2_table(self._connection, create=False)
            self._ensure_optional_analytical_tables(create=False)
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
        ensure_observation_v2_table(self._connection, create=True)
        self._ensure_optional_analytical_tables(create=True)
        self._lock_path = destination / _STAGING_LOCK_FILENAME
        try:
            descriptor = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as error:
            raise RawV2StagingError("raw v2 staging lock could not be acquired") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise RawV2StagingError("raw v2 staging destination already has a writer") from error
        except OSError as error:
            os.close(descriptor)
            raise RawV2StagingError("raw v2 staging lock could not be acquired") from error
        self._lock_descriptor: int | None = descriptor
        _OPEN_WRITERS.add(key)
        self._is_open = True
        return self

    def close(self) -> None:
        """Release the writer slot without touching staged bytes."""
        if not self._is_open:
            return
        if not self._read_only:
            _OPEN_WRITERS.discard(str(self._destination.absolute()))
            descriptor = getattr(self, "_lock_descriptor", None)
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                self._lock_descriptor = None
        self._is_open = False

    @property
    def staging_id(self) -> str | None:
        """Return the stable staging identity recorded in the marker, if any."""
        marker_path = self._destination / _STAGING_MARKER_FILENAME
        if not marker_path.is_file() or marker_path.is_symlink():
            return None
        try:
            return self._read_marker(marker_path).staging_id
        except RawV2StagingError:
            return None

    def require_open_for_import(self) -> None:
        """Require an open writable staging before an import reads its identity."""
        self._require_writable()

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
                RawV2StagingMarker(created_at=datetime.now(UTC), staging_id=uuid4().hex).model_dump(
                    mode="json"
                ),
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

    def save_observations(
        self, observations: Collection[NormalizedObservation]
    ) -> BatchWriteReceipt:
        """Persist typed normalized observations under the same writer lock.

        The typed observation table shares the staging connection and writer
        slot; callers provide full ``NormalizedObservation`` models whose raw
        reference must already be staged completely.
        """
        typed = tuple(observations)
        for observation in typed:
            if not isinstance(observation, NormalizedObservation):
                raise ObservationV2Error("observation v2 save requires NormalizedObservation")
        self._require_writable()
        ensure_observation_v2_table(self._connection, create=True)
        return ObservationV2Store(self._connection).save_many(typed)

    def get_observations(
        self, observation_ids: Collection[UUID]
    ) -> dict[UUID, NormalizedObservation]:
        """Hydrate verified typed observations in deterministic order."""
        self._require_open()
        ensure_observation_v2_table(self._connection, create=False)
        hydrated = ObservationV2Store(self._connection).get_many(tuple(observation_ids))
        for key, observation in hydrated.items():
            if (
                not isinstance(observation, NormalizedObservation)
                or observation.observation_id != key
            ):
                raise ObservationV2Error("observation v2 identity diverged on read")
        return hydrated

    def list_observation_inventory_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_observation_id: UUID | None = None,
    ) -> list[UUID]:
        """Return one bounded keyset page of observation IDs without hydration."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ObservationV2Error("observation inventory page limit must be an integer")
        if limit < 1 or limit > MAX_OBSERVATION_V2_PAGE:
            raise ObservationV2Error("observation inventory page limit must be between 1 and 256")
        if (after_available_at is None) != (after_observation_id is None):
            raise ObservationV2Error("observation inventory cursor requires both fields together")
        self._require_open()
        ensure_observation_v2_table(self._connection, create=False)
        clauses: list[str] = []
        parameters: list[object] = []
        if after_available_at is not None and after_observation_id is not None:
            if after_available_at.tzinfo is None or after_available_at.utcoffset() is None:
                raise ObservationV2Error("observation cursor available_at must be timezone-aware")
            clauses.append("(available_at, observation_id) > (?, ?)")
            parameters.extend([_instant_text(after_available_at), str(after_observation_id)])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT observation_id FROM normalized_observations_v2"
            f"{where} ORDER BY available_at, observation_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def list_observations(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        frequency: object | None = None,
        available_to: datetime | None = None,
    ) -> list[NormalizedObservation]:
        """Hydrate typed PIT observations in stable order with verification."""
        self._require_open()
        ensure_observation_v2_table(self._connection, create=False)
        frequency_value = frequency.value if hasattr(frequency, "value") else frequency
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            parameters.append(source_id)
        if frequency_value is not None:
            clauses.append("frequency = ?")
            parameters.append(str(frequency_value))
        if available_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(_instant_text(available_to))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT observation_id FROM normalized_observations_v2"
            f"{where} ORDER BY available_at, observation_id",
            parameters,
        ).fetchall()
        return [
            observation
            for _, observation in sorted(
                self.get_observations([UUID(row[0]) for row in rows]).items(),
                key=lambda pair: str(pair[0]),
            )
            if isinstance(observation, NormalizedObservation)
        ]

    def verify_observation_row(self, observation_id: UUID) -> NormalizedObservation:
        """Hydrate and verify one observation row with its raw reference."""
        hydrated = self.get_observations([observation_id])
        if observation_id not in hydrated:
            raise RecordNotFoundError(f"observation v2 {observation_id} was not found")
        return hydrated[observation_id]

    def observation_projection_text(self, observation: NormalizedObservation) -> str:
        """Expose the canonical typed row used by verifiers without JSON."""
        return ",".join(str(part) for part in observation_to_row(observation))

    def hydrate_observation_row(self, row: tuple[object, ...]) -> NormalizedObservation:
        """Rehydrate one typed row and confirm its raw linkage."""
        observation = row_to_observation(row)
        self._require_open()
        rows = self._connection.execute(
            "SELECT source_id FROM raw_v2_index WHERE record_id = ?",
            [str(observation.raw_record_id)],
        ).fetchall()
        if not rows or str(rows[0][0]) != observation.source.source_id:
            raise ObservationV2Error("observation v2 raw reference is missing or foreign")
        return observation

    def save_metrics(self, results: Collection[MetricResult]) -> BatchWriteReceipt:
        """Persist typed metric results with verified lineage under the writer lock."""
        from investment_analyst.core.models import MetricResult as MetricResultModel

        typed = tuple(results)
        for result in typed:
            if not isinstance(result, MetricResultModel):
                raise MetricV2Error("metric v2 save requires MetricResult")
        self._require_writable()
        ensure_metric_v2_tables(self._connection, create=True)
        ensure_observation_v2_table(self._connection, create=False)
        receipt = MetricV2Store(self._connection).save_many(typed)
        self._ensure_optional_analytical_tables(create=True)
        return receipt

    def get_metrics(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        """Hydrate verified typed metrics with resolved lineage in order."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        hydrated = MetricV2Store(self._connection).get_many(tuple(result_ids))
        return self._resolve_metrics_lineage_batch(hydrated)

    def _resolve_metrics_lineage_batch(
        self, hydrated: dict[UUID, MetricResult]
    ) -> dict[UUID, MetricResult]:
        """Verify inputs and shared evidence for a batch of metrics without N+1 queries."""
        if not hydrated:
            return {}

        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)

        # 1. Collect all observation IDs, metric dependency IDs, and evidence set IDs
        all_obs_ids: set[str] = set()
        all_dep_ids: set[str] = set()
        all_ev_ids: set[UUID] = set()

        for result in hydrated.values():
            seen_obs: set[str] = set()
            for obs_id in result.input_observation_ids:
                key = str(obs_id)
                if key in seen_obs:
                    raise MetricV2Error("metric v2 observation inputs must be unique")
                seen_obs.add(key)
                all_obs_ids.add(key)

            seen_deps: set[str] = set()
            for dep_id in result.input_metric_result_ids:
                key = str(dep_id)
                if key in seen_deps:
                    raise MetricV2Error("metric v2 metric inputs must be unique")
                seen_deps.add(key)
                if key == str(result.result_id):
                    raise MetricV2Error("metric v2 dependency cycle is not allowed")
                all_dep_ids.add(key)

            reference = result.parameters.get("evidence_set_id")
            if reference is not None:
                all_ev_ids.add(UUID(str(reference)))

        # 2. Batch fetch observation inputs in chunks <= 256
        obs_map: dict[str, tuple[str, datetime]] = {}
        if all_obs_ids:
            from investment_analyst.storage.analytical_v2_validation import chunked_sequence
            from investment_analyst.storage.diagnostic_v2 import _parse_instant_text

            for chunk in chunked_sequence(list(all_obs_ids), 256):
                placeholders = ", ".join("?" for _ in chunk)
                rows = self._connection.execute(
                    "SELECT observation_id, asset_id, available_at FROM normalized_observations_v2 "
                    f"WHERE observation_id IN ({placeholders})",
                    list(chunk),
                ).fetchall()
                for r in rows:
                    obs_map[str(r[0])] = (str(r[1]), _parse_instant_text(r[2]))

        # 3. Batch fetch metric dependencies in chunks <= 256
        dep_map: dict[str, tuple[str, datetime]] = {}
        if all_dep_ids:
            from investment_analyst.storage.analytical_v2_validation import chunked_sequence
            from investment_analyst.storage.diagnostic_v2 import _parse_instant_text
            from investment_analyst.storage.metric_v2 import METRIC_V2_TABLE

            for chunk in chunked_sequence(list(all_dep_ids), 256):
                placeholders = ", ".join("?" for _ in chunk)
                rows = self._connection.execute(
                    f"SELECT result_id, asset_id, available_at FROM {METRIC_V2_TABLE} "
                    f"WHERE result_id IN ({placeholders})",
                    list(chunk),
                ).fetchall()
                for r in rows:
                    dep_map[str(r[0])] = (str(r[1]), _parse_instant_text(r[2]))

        # 4. Batch fetch evidence sets if any
        ev_map: dict[UUID, tuple[list[str], datetime]] = {}
        if all_ev_ids:
            ensure_evidence_v2_tables(self._connection, create=False)
            ev_store = EvidenceSetV2Store(self._connection)
            for ev_id in all_ev_ids:
                stored = ev_store.get_set(ev_id)
                ordered = ev_store.verify_set_lineage(stored)
                ev_map[ev_id] = ([str(item) for item in ordered], stored.available_at)

        # 5. Verify each metric against pre-fetched lookup maps
        resolved: dict[UUID, MetricResult] = {}
        for rid, result in hydrated.items():
            for obs_id in result.input_observation_ids:
                key = str(obs_id)
                if key not in obs_map:
                    raise MetricV2Error(f"metric v2 references a missing observation {key}")
                obs_asset, obs_avail = obs_map[key]
                if obs_asset != result.asset_id:
                    raise MetricV2Error(f"metric v2 references a foreign observation {key}")
                if obs_avail > result.available_at:
                    raise MetricV2Error(f"metric v2 references a future observation {key}")

            for dep_id in result.input_metric_result_ids:
                key = str(dep_id)
                if key not in dep_map:
                    raise MetricV2Error(f"metric v2 references a missing metric {key}")
                dep_asset, dep_avail = dep_map[key]
                if dep_asset != result.asset_id:
                    raise MetricV2Error(f"metric v2 references a foreign metric {key}")
                if dep_avail > result.available_at:
                    raise MetricV2Error(f"metric v2 references a future metric {key}")

            reference = result.parameters.get("evidence_set_id")
            if reference is not None:
                ev_id = UUID(str(reference))
                ordered_text, stored_available_at = ev_map[ev_id]
                input_text = [str(item) for item in result.input_observation_ids]
                if ordered_text != input_text and (
                    set(ordered_text) != set(input_text) or len(ordered_text) != len(input_text)
                ):
                    raise MetricV2Error("metric v2 evidence lineage does not match its inputs")
                if stored_available_at > result.available_at:
                    raise MetricV2Error("metric v2 evidence is not visible at the result")

            resolved[rid] = result

        return resolved

    def resolve_metric_lineage(self, result: MetricResult) -> MetricResult:
        """Verify inputs and shared evidence before returning one metric."""
        return self._resolve_metrics_lineage_batch({result.result_id: result})[result.result_id]

    def list_metric_inventory_page(
        self,
        *,
        limit: int,
        after_available_at: datetime | None = None,
        after_result_id: UUID | None = None,
    ) -> list[UUID]:
        """Return one bounded keyset page of metric IDs without hydration."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise MetricV2Error("metric inventory page limit must be an integer")
        if limit < 1 or limit > MAX_METRIC_V2_PAGE:
            raise MetricV2Error("metric inventory page limit must be between 1 and 256")
        if (after_available_at is None) != (after_result_id is None):
            raise MetricV2Error("metric inventory cursor requires both fields together")
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        clauses: list[str] = []
        parameters: list[object] = []
        if after_available_at is not None and after_result_id is not None:
            if after_available_at.tzinfo is None or after_available_at.utcoffset() is None:
                raise MetricV2Error("metric cursor available_at must be timezone-aware")
            clauses.append("(available_at, result_id) > (?, ?)")
            parameters.extend([_instant_text(after_available_at), str(after_result_id)])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT result_id FROM metric_results_v2"
            f"{where} ORDER BY available_at, result_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def list_metrics(
        self,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        as_of: datetime | None = None,
        available_to: datetime | None = None,
    ) -> list[MetricResult]:
        """Hydrate typed PIT metrics in stable order with lineage verification."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        clauses: list[str] = []
        parameters: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            parameters.append(asset_id)
        if metric_key is not None:
            clauses.append("metric_key = ?")
            parameters.append(metric_key)
        if as_of is not None:
            clauses.append("as_of <= ?")
            parameters.append(_instant_text(as_of))
        if available_to is not None:
            clauses.append("available_at <= ?")
            parameters.append(_instant_text(available_to))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT result_id FROM metric_results_v2{where} ORDER BY available_at, result_id",
            parameters,
        ).fetchall()
        ordered = [UUID(row[0]) for row in rows]
        return [self.get_metrics([result_id])[result_id] for result_id in ordered]

    def save_evidence_segments(self, segments: Collection[EvidenceSegment]) -> int:
        """Persist shared hourly segments idempotently under the writer lock."""
        from investment_analyst.analytics.evidence_set import EvidenceSegment as SegmentModel

        typed = tuple(segments)
        for segment in typed:
            if not isinstance(segment, SegmentModel):
                raise EvidenceSetV2Error("evidence v2 save requires EvidenceSegment")
        self._require_writable()
        ensure_evidence_v2_tables(self._connection, create=True)
        created = EvidenceSetV2Store(self._connection).save_segments(typed)
        self._ensure_optional_analytical_tables(create=True)
        return created

    def save_evidence_set(self, evidence_set: EvidenceSet) -> bool:
        """Persist one shared set after verifying its lineage against observations."""
        from investment_analyst.analytics.evidence_set import EvidenceSet as SetModel

        if not isinstance(evidence_set, SetModel):
            raise EvidenceSetV2Error("evidence v2 save requires EvidenceSet")
        self._require_writable()
        ensure_evidence_v2_tables(self._connection, create=True)
        ensure_observation_v2_table(self._connection, create=False)
        created = EvidenceSetV2Store(self._connection).save_set(evidence_set)
        self._ensure_optional_analytical_tables(create=True)
        return created

    def get_evidence_set(self, evidence_set_id: UUID) -> EvidenceSet:
        """Hydrate and verify one shared set with every observation."""
        self._require_open()
        ensure_evidence_v2_tables(self._connection, create=False)
        return EvidenceSetV2Store(self._connection).get_set(evidence_set_id)

    def save_diagnostics(self, diagnostics: Collection[DiagnosticResult]) -> BatchWriteReceipt:
        """Persist typed diagnostics idempotently under the writer lock."""
        self._require_writable()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_diagnostic_v2_tables(self._connection, create=True)
        receipt = DiagnosticV2Store(self._connection).save_diagnostics(diagnostics)
        self._ensure_optional_analytical_tables(create=True)
        return receipt

    def get_diagnostics(self, diagnostic_ids: Collection[UUID]) -> dict[UUID, DiagnosticResult]:
        """Hydrate typed diagnostics with verified components and evidence."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_diagnostic_v2_tables(self._connection, create=False)
        return DiagnosticV2Store(self._connection).get_diagnostics(diagnostic_ids)

    def list_diagnostics(
        self,
        *,
        asset_id: str | None = None,
        mode: DiagnosticMode | None = None,
        as_of: datetime | None = None,
        available_to: datetime | None = None,
        cursor_at: datetime | str | None = None,
        cursor_id: UUID | str | None = None,
        limit: int | None = None,
    ) -> list[DiagnosticResult]:
        """Hydrate typed PIT diagnostics in stable order."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_diagnostic_v2_tables(self._connection, create=False)
        return DiagnosticV2Store(self._connection).list_diagnostics(
            asset_id=asset_id,
            mode=mode,
            as_of=as_of,
            available_to=available_to,
            cursor_at=cursor_at,
            cursor_id=cursor_id,
            limit=limit,
        )

    def save_analysis_snapshots(self, snapshots: Collection[AnalysisSnapshot]) -> BatchWriteReceipt:
        """Persist typed analysis snapshots idempotently under the writer lock."""
        self._require_writable()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_analysis_snapshot_v2_tables(self._connection, create=True)
        receipt = AnalysisSnapshotV2Store(self._connection).save_snapshots(snapshots)
        self._ensure_optional_analytical_tables(create=True)
        return receipt

    def get_analysis_snapshot(self, snapshot_id: UUID) -> AnalysisSnapshot:
        """Hydrate and verify one analysis snapshot."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_analysis_snapshot_v2_tables(self._connection, create=False)
        return AnalysisSnapshotV2Store(self._connection).get_snapshot(snapshot_id)

    def get_analysis_snapshots(
        self, snapshot_ids: Collection[UUID]
    ) -> dict[UUID, AnalysisSnapshot]:
        """Hydrate typed analysis snapshots with verified links and cited references."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_analysis_snapshot_v2_tables(self._connection, create=False)
        return AnalysisSnapshotV2Store(self._connection).get_snapshots(snapshot_ids)

    def list_analysis_snapshots(
        self,
        *,
        asset_id: str | None = None,
        domain: str | None = None,
        known_to: datetime | None = None,
        cursor_at: datetime | str | None = None,
        cursor_id: UUID | str | None = None,
        limit: int | None = None,
    ) -> list[AnalysisSnapshot]:
        """Hydrate typed PIT analysis snapshots in stable order."""
        self._require_open()
        ensure_metric_v2_tables(self._connection, create=False)
        ensure_analysis_snapshot_v2_tables(self._connection, create=False)
        return AnalysisSnapshotV2Store(self._connection).list_snapshots(
            asset_id=asset_id,
            domain=domain,
            known_to=known_to,
            cursor_at=cursor_at,
            cursor_id=cursor_id,
            limit=limit,
        )

    def _ensure_optional_analytical_tables(self, *, create: bool) -> None:
        """Require analytical tables only when the staging already has them."""
        from investment_analyst.storage.analysis_snapshot_v2 import (
            analysis_snapshot_v2_tables_exist,
            ensure_analysis_snapshot_v2_tables,
        )
        from investment_analyst.storage.diagnostic_v2 import (
            diagnostic_v2_tables_exist,
            ensure_diagnostic_v2_tables,
        )
        from investment_analyst.storage.evidence_set_v2 import evidence_v2_tables_exist
        from investment_analyst.storage.metric_v2 import metric_v2_table_exists

        has_metrics = metric_v2_table_exists(self._connection)
        has_evidence = evidence_v2_tables_exist(self._connection)
        has_diagnostics = diagnostic_v2_tables_exist(self._connection)
        has_snapshots = analysis_snapshot_v2_tables_exist(self._connection)
        if create:
            if has_metrics:
                ensure_metric_v2_tables(self._connection, create=True)
            if has_evidence:
                ensure_evidence_v2_tables(self._connection, create=True)
            if has_diagnostics:
                ensure_diagnostic_v2_tables(self._connection, create=True)
            if has_snapshots:
                ensure_analysis_snapshot_v2_tables(self._connection, create=True)
            return
        if has_metrics:
            ensure_metric_v2_tables(self._connection, create=False)
        if has_evidence:
            ensure_evidence_v2_tables(self._connection, create=False)
        if has_diagnostics:
            ensure_diagnostic_v2_tables(self._connection, create=False)
        if has_snapshots:
            ensure_analysis_snapshot_v2_tables(self._connection, create=False)

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


def index_database_path(destination: Path) -> Path | None:
    """Return the file-backed DuckDB index inside a staging destination, if unique."""
    if not destination.is_absolute() or destination.is_symlink():
        return None
    candidates = [
        entry
        for entry in destination.iterdir()
        if entry.is_file() and not entry.is_symlink() and entry.suffix == ".duckdb"
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]
