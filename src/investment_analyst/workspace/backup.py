"""Verified local workspace backup and restore without format migration."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import duckdb
from duckdb import DuckDBPyConnection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.cazatiburones.activity_event_repository import (
    ActivityEventRepository,
)
from investment_analyst.core.models import (
    DiagnosticResult,
    MetricResult,
    NormalizedObservation,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.instrument_correspondence.repository import (
    verify_instrument_correspondence_records,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    verify_beneficial_ownership_records,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    verify_document_records,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    verify_sec_institutional_row_correspondence_records,
)
from investment_analyst.evidence.sec_institutional_correspondence.service import (
    SecInstitutionalRowCorrespondenceService,
)
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    SecFilerDocumentRepository,
    SecFilerDocumentRevision,
    verify_filer_document_records,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
    verify_institutional_holding_records,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    verify_institutional_semantics_records,
)
from investment_analyst.evidence.sec_ownership.repository import verify_ownership_records
from investment_analyst.providers.institutional_holdings.sec_institutional_semantics_parser import (
    parse_institutional_semantics,
)
from investment_analyst.storage import StorageError
from investment_analyst.storage.document_content import DocumentContentStore
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.serialization import model_from_json
from investment_analyst.workspace.models import (
    WORKSPACE_FORMAT_VERSION,
    WorkspaceAccessMode,
    WorkspaceInspection,
)
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

BACKUP_MANIFEST_NAME = "backup_manifest.json"
_TRACEABILITY_BATCH_SIZE = 256

_OBSERVATION_RAW_QUERY = (
    "SELECT o.raw_record_id "
    "FROM normalized_observations o "
    "ANTI JOIN raw_record_index r ON o.raw_record_id = r.record_id "
    "LIMIT 1"
)
_METRIC_OBSERVATION_QUERY = (
    "WITH extracted_refs AS ("
    "    SELECT unnest("
    "        COALESCE("
    "            from_json("
    "                json_extract(document_json, '$.input_observation_ids'),"
    "                '[\"VARCHAR\"]'"
    "            ),"
    "            []"
    "        )"
    "    ) AS ref_id "
    "    FROM metric_results"
    ") "
    "SELECT ref_id "
    "FROM extracted_refs e "
    "ANTI JOIN normalized_observations o ON e.ref_id = o.observation_id "
    "WHERE ref_id IS NOT NULL "
    "LIMIT 1"
)
_METRIC_METRIC_QUERY = (
    "WITH extracted_refs AS ("
    "    SELECT unnest("
    "        COALESCE("
    "            from_json("
    "                json_extract(document_json, '$.input_metric_result_ids'),"
    "                '[\"VARCHAR\"]'"
    "            ),"
    "            []"
    "        )"
    "    ) AS ref_id "
    "    FROM metric_results"
    ") "
    "SELECT ref_id "
    "FROM extracted_refs e "
    "ANTI JOIN metric_results m ON e.ref_id = m.result_id "
    "WHERE ref_id IS NOT NULL "
    "LIMIT 1"
)
_DIAGNOSTIC_METRIC_QUERY = (
    "WITH extracted_refs AS ("
    "    SELECT unnest("
    "        list_concat("
    "            COALESCE("
    "                from_json("
    "                    json_extract(document_json, '$.components[*].metric_result_ids[*]'),"
    "                    '[\"VARCHAR\"]'"
    "                ),"
    "                []"
    "            ),"
    "            COALESCE("
    "                from_json("
    "                    json_extract(document_json, '$.evidence[*].metric_result_id'),"
    "                    '[\"VARCHAR\"]'"
    "                ),"
    "                []"
    "            )"
    "        )"
    "    ) AS ref_id "
    "    FROM diagnostic_results"
    ") "
    "SELECT ref_id "
    "FROM extracted_refs e "
    "ANTI JOIN metric_results m ON e.ref_id = m.result_id "
    "WHERE ref_id IS NOT NULL "
    "LIMIT 1"
)


class WorkspaceBackupError(WorkspaceError):
    """Raised when a backup or restore cannot be proven complete."""


class WorkspaceBackupFile(ContractModel):
    """One exact regular file included in a workspace backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: NonEmptyStr
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("backup file path must be normalized and relative")
        if value == BACKUP_MANIFEST_NAME:
            raise ValueError("backup manifest cannot inventory itself")
        return value

    @field_validator("sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("backup file sha256 is invalid")
        return value


class WorkspaceBackupCounts(ContractModel):
    """Analytical row counts verified before backup and after restore."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_records: int = Field(ge=0)
    observations: int = Field(ge=0)
    metric_results: int = Field(ge=0)
    diagnostic_results: int = Field(ge=0)


class WorkspaceBackupManifest(ContractModel):
    """Versioned inventory used to verify a backup before activation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-backup-manifest-v1"] = "workspace-backup-manifest-v1"
    backup_id: UUID
    source_workspace_id: UUID
    workspace_format_version: Literal[1] = WORKSPACE_FORMAT_VERSION
    created_at: UTCDateTime
    files: tuple[WorkspaceBackupFile, ...]
    counts: WorkspaceBackupCounts
    traceability_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_inventory(self) -> WorkspaceBackupManifest:
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("backup inventory must be non-empty, unique, and sorted")
        required = {
            "manifest.json",
            "storage/data/processed/investment_analyst.duckdb",
        }
        if not required.issubset(paths):
            raise ValueError("backup inventory is missing required workspace files")
        expected_id = _backup_id(self.source_workspace_id, self.files, self.counts)
        if self.backup_id != expected_id:
            raise ValueError("backup identity does not match its inventory")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceBackupService:
    """Create and restore filesystem snapshots coordinated with one writer mutex."""

    def __init__(
        self,
        workspace_service: WorkspaceService,
        *,
        writer_lock: threading.RLock | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace_service = workspace_service
        self._writer_lock = writer_lock or threading.RLock()
        self._clock = clock

    def create(self, source: Path, destination: Path) -> WorkspaceBackupManifest:
        """Publish one complete backup directory only after every hash verifies."""
        source_path = source.expanduser()
        destination_path = destination.expanduser()
        if source_path.is_symlink() or destination_path.is_symlink():
            raise WorkspaceBackupError("workspace backup paths must not be symbolic links")
        source_root = source_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if destination_root.exists():
            raise WorkspaceBackupError("backup destination already exists")
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        if source_root == destination_root or source_root in destination_root.parents:
            raise WorkspaceBackupError("backup destination must be outside the source workspace")
        try:
            _reject_symlinks(source_root)
            with _workspace_process_guard(source_root), self._writer_lock:
                _reject_symlinks(source_root)
                inspection = self._workspace_service.inspect(source_root)
                if inspection.status != "ready":
                    raise WorkspaceBackupError("source workspace must be ready for backup")
                _verify_workspace_traceability(
                    self._workspace_service,
                    source_root,
                    expected_counts=_counts(inspection),
                    spill_parent=destination_root.parent,
                )
                files = _inventory(source_root)
                counts = _counts(inspection)
                manifest = WorkspaceBackupManifest(
                    backup_id=_backup_id(inspection.workspace_id, files, counts),
                    source_workspace_id=inspection.workspace_id,
                    created_at=self._now(),
                    files=files,
                    counts=counts,
                )
                temporary.mkdir(parents=True)
                _copy_inventory(source_root, temporary, files)
                _write_manifest(temporary / BACKUP_MANIFEST_NAME, manifest)
                _verify_backup_directory(temporary, manifest)
            os.replace(temporary, destination_root)
            return manifest
        except WorkspaceBackupError:
            raise
        except (OSError, ValueError) as error:
            raise WorkspaceBackupError("workspace backup could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def restore(self, backup: Path, destination: Path) -> WorkspaceInspection:
        """Verify then activate a backup only into a new or empty destination."""
        backup_path = backup.expanduser()
        destination_path = destination.expanduser()
        if backup_path.is_symlink() or destination_path.is_symlink():
            raise WorkspaceBackupError("workspace restore paths must not be symbolic links")
        backup_root = backup_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if destination_root.exists() and any(destination_root.iterdir()):
            raise WorkspaceBackupError("restore destination must be new or empty")
        if backup_root == destination_root or backup_root in destination_root.parents:
            raise WorkspaceBackupError("restore destination must be outside the backup")
        _reject_symlinks(backup_root)
        manifest = _load_manifest(backup_root / BACKUP_MANIFEST_NAME)
        _verify_backup_directory(backup_root, manifest)
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=True)
            _create_required_layout(temporary)
            _copy_inventory(backup_root, temporary, manifest.files)
            inspection = self._workspace_service.inspect(temporary)
            if inspection.status != "ready":
                raise WorkspaceBackupError("restored workspace layout is incomplete")
            _verify_workspace_traceability(
                self._workspace_service,
                temporary,
                expected_counts=_counts(inspection),
                spill_parent=destination_root.parent,
            )
            if inspection.workspace_id != manifest.source_workspace_id:
                raise WorkspaceBackupError("restored workspace identity does not match backup")
            if _counts(inspection) != manifest.counts:
                raise WorkspaceBackupError("restored analytical counts do not match backup")
            if destination_root.exists():
                destination_root.rmdir()
            os.replace(temporary, destination_root)
            return inspection.model_copy(update={"workspace_root": destination_root})
        except WorkspaceBackupError:
            raise
        except (OSError, ValueError) as error:
            raise WorkspaceBackupError("workspace restore could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceBackupError("backup clock must be timezone-aware")
        return value.astimezone(UTC)


def _inventory(root: Path) -> tuple[WorkspaceBackupFile, ...]:
    _reject_symlinks(root)
    files: list[WorkspaceBackupFile] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        if path.name.endswith(".lock") or ".tmp" in path.name:
            continue
        files.append(
            WorkspaceBackupFile(
                path=relative,
                size_bytes=path.stat().st_size,
                sha256=_sha256(path),
            )
        )
    return tuple(files)


def _counts(inspection: WorkspaceInspection) -> WorkspaceBackupCounts:
    return WorkspaceBackupCounts(
        raw_records=inspection.raw_record_count,
        observations=inspection.observation_count,
        metric_results=inspection.metric_result_count,
        diagnostic_results=inspection.diagnostic_result_count,
    )


def _backup_id(
    workspace_id: UUID,
    files: tuple[WorkspaceBackupFile, ...],
    counts: WorkspaceBackupCounts,
) -> UUID:
    document = json.dumps(
        {
            "workspace_id": str(workspace_id),
            "files": [item.model_dump(mode="json") for item in files],
            "counts": counts.model_dump(mode="json"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, document)


def _copy_inventory(
    source: Path,
    destination: Path,
    files: tuple[WorkspaceBackupFile, ...],
) -> None:
    for item in files:
        source_file = source / item.path
        target_file = destination / item.path
        if source_file.is_symlink() or not source_file.is_file():
            raise WorkspaceBackupError("backup inventory must contain only regular files")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target_file)
        if target_file.stat().st_size != item.size_bytes or _sha256(target_file) != item.sha256:
            raise WorkspaceBackupError("copied backup file failed verification")


def _create_required_layout(root: Path) -> None:
    """Recreate only empty directories required by workspace/storage format v1."""
    for relative in (
        "exports",
        "state",
        "storage/data/exports",
        "storage/data/processed",
        "storage/data/raw",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _verify_backup_directory(root: Path, manifest: WorkspaceBackupManifest) -> None:
    _reject_symlinks(root)
    expected = {item.path: item for item in manifest.files}
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name != BACKUP_MANIFEST_NAME
    }
    if set(actual) != set(expected):
        raise WorkspaceBackupError("backup file inventory does not match manifest")
    for relative, item in expected.items():
        path = actual[relative]
        if path.stat().st_size != item.size_bytes or _sha256(path) != item.sha256:
            raise WorkspaceBackupError("backup file hash verification failed")


def _load_manifest(path: Path) -> WorkspaceBackupManifest:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceBackupError("backup manifest must be a regular file")
    try:
        return WorkspaceBackupManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceBackupError("backup manifest is malformed or unavailable") from error


def _write_manifest(path: Path, manifest: WorkspaceBackupManifest) -> None:
    document = json.dumps(
        manifest.to_json_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{document}\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_symlinks(root: Path) -> None:
    try:
        if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
            raise WorkspaceBackupError("workspace backups must not contain symbolic links")
    except OSError as error:
        raise WorkspaceBackupError("workspace backup tree could not be inspected") from error


@contextmanager
def _workspace_process_guard(root: Path):
    """Exclude the resident service while copying its writer-owned workspace."""
    paths = (
        root / "state" / "aapl_daily_run.lock",
        root / "state" / "aapl_local_service.lock",
    )
    descriptors: list[int] = []
    try:
        for path in paths:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            descriptors.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkspaceBackupError(
                    "workspace writers must be stopped before creating a backup"
                ) from error
        yield
    except OSError as error:
        raise WorkspaceBackupError("workspace backup lock could not be acquired") from error
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _verify_workspace_traceability(
    service: WorkspaceService,
    root: Path,
    *,
    expected_counts: WorkspaceBackupCounts,
    spill_parent: Path | None = None,
) -> None:
    """Read and connect every persisted evidence layer with bounded memory."""
    paths = service.resolve(root)
    effective_spill_parent = (
        spill_parent if spill_parent is not None else Path(tempfile.gettempdir())
    )
    spill_dir = (effective_spill_parent / f".duckdb_spill_{uuid4().hex}").resolve()
    spill_dir.mkdir(parents=True, exist_ok=True)
    try:
        storage = service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
        try:
            escaped_spill = str(spill_dir).replace("'", "''")
            storage.store.connection.execute(f"SET temp_directory = '{escaped_spill}'")
            storage.store.connection.execute("SET memory_limit = '1GiB'")
            storage.store.connection.execute("SET preserve_insertion_order = false")
            storage.store.connection.execute("SET threads = 1")

            _require_counts(storage, expected_counts)
            ActivityEventRepository(storage.paths.processed_dir, read_only=True).verify()
            _require_scan_count(
                _scan_raw_records(storage),
                expected_counts.raw_records,
            )
            _require_scan_count(
                _scan_documents(
                    storage.store.connection,
                    table="normalized_observations",
                    primary_id="observation_id",
                    model_type=NormalizedObservation,
                ),
                expected_counts.observations,
            )
            _require_scan_count(
                _scan_documents(
                    storage.store.connection,
                    table="metric_results",
                    primary_id="result_id",
                    model_type=MetricResult,
                ),
                expected_counts.metric_results,
            )
            _require_scan_count(
                _scan_documents(
                    storage.store.connection,
                    table="diagnostic_results",
                    primary_id="diagnostic_id",
                    model_type=DiagnosticResult,
                ),
                expected_counts.diagnostic_results,
            )

            _verify_observation_raw_lineage(storage.store.connection)
            _verify_metric_observation_lineage(storage.store.connection)
            _verify_metric_metric_lineage(storage.store.connection)
            _verify_diagnostic_metric_lineage(storage.store.connection)
            _require_counts(storage, expected_counts)
        finally:
            storage.close()
    except WorkspaceBackupError:
        raise
    except (duckdb.Error, OSError, StorageError, WorkspaceError, ValueError) as error:
        raise WorkspaceBackupError("workspace traceability could not be verified") from error
    finally:
        shutil.rmtree(spill_dir, ignore_errors=True)


class _TraceabilityContentStore:
    """Delegate document content operations while caching successfully verified digests."""

    def __init__(self, inner: DocumentContentStore) -> None:
        self._inner = inner
        self._verified: set[tuple[str, int | None]] = set()

    def verify(self, checksum: str, *, size_bytes: int | None = None) -> None:
        key = (checksum, size_bytes)
        if key in self._verified:
            return
        self._inner.verify(checksum, size_bytes=size_bytes)
        self._verified.add(key)

    def read(self, checksum: str) -> bytes:
        return self._inner.read(checksum)


class _TraceabilityFilerDocumentRepository:
    """Delegate filer document operations while caching successfully verified revisions."""

    def __init__(self, inner: SecFilerDocumentRepository) -> None:
        self._inner = inner
        self._verified: set[UUID] = set()

    def verify_revision(self, revision: SecFilerDocumentRevision) -> None:
        if revision.revision_id in self._verified:
            return
        self._inner.verify_revision(revision)
        self._verified.add(revision.revision_id)

    def get_revision(self, revision_id: UUID) -> SecFilerDocumentRevision | None:
        return self._inner.get_revision(revision_id)


def _require_counts(storage: LocalStorage, expected: WorkspaceBackupCounts) -> None:
    actual = WorkspaceBackupCounts(
        raw_records=storage.raw_records.count(),
        observations=storage.observations.count(),
        metric_results=storage.metric_results.count(),
        diagnostic_results=storage.diagnostics.count(),
    )
    if actual != expected:
        raise StorageError("workspace traceability counts changed during verification")


def _require_scan_count(actual: int, expected: int) -> None:
    if actual != expected:
        raise StorageError("workspace traceability scan did not match its count")


def _scan_raw_records(storage: LocalStorage) -> int:
    connection = storage.store.connection
    bounds = connection.execute(
        "SELECT MIN(rowid), MAX(rowid), count(*) FROM raw_record_index"
    ).fetchone()
    if bounds is None or bounds[2] == 0:
        return 0
    min_rowid, max_rowid, total_rows = bounds
    if min_rowid is None or max_rowid is None:
        return 0

    cached_documents = _TraceabilityContentStore(storage.documents)
    filer_documents = _TraceabilityFilerDocumentRepository(
        SecFilerDocumentRepository(storage.raw_records, cached_documents)
    )
    sec_documents = SecDocumentRepository(storage.raw_records, cached_documents)
    holdings_repository = InstitutionalHoldingsRepository(storage.raw_records)
    correspondence_service = SecInstitutionalRowCorrespondenceService(storage)

    total_scanned = 0
    current_start = min_rowid
    while current_start <= max_rowid:
        current_end = current_start + _TRACEABILITY_BATCH_SIZE - 1
        record_ids = _load_raw_record_ids_page(
            connection,
            start_rowid=current_start,
            end_rowid=current_end,
        )
        if record_ids:
            records = storage.raw_records.get_many(tuple(UUID(value) for value in record_ids))
            if len(records) != len(record_ids):
                raise StorageError("raw record page did not resolve exactly")
            verify_document_records(
                records.values(),
                sec_documents,
            )
            verify_instrument_correspondence_records(records.values())
            verify_sec_institutional_row_correspondence_records(
                records.values(),
                service=correspondence_service,
            )
            verify_ownership_records(
                records.values(),
                sec_documents,
                cached_documents,
            )
            verify_beneficial_ownership_records(
                records.values(),
                sec_documents,
                cached_documents,
            )
            verify_filer_document_records(records.values(), filer_documents)
            verify_institutional_holding_records(
                records.values(),
                holdings_repository,
                filer_documents,
                cached_documents,
            )
            verify_institutional_semantics_records(
                records.values(),
                holdings_repository=holdings_repository,
                filer_documents=filer_documents,
                content_store=cached_documents,
                parser=parse_institutional_semantics,
            )
            total_scanned += len(records)
            del records
        current_start = current_end + 1

    if total_scanned != total_rows:
        raise StorageError("workspace traceability scan did not match its count")
    return total_scanned


def _load_raw_record_ids_page(
    connection: DuckDBPyConnection,
    *,
    start_rowid: int,
    end_rowid: int,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT record_id FROM raw_record_index WHERE rowid >= ? AND rowid <= ?",
        [start_rowid, end_rowid],
    ).fetchall()
    if len(rows) > _TRACEABILITY_BATCH_SIZE:
        raise StorageError("workspace traceability page exceeded its bound")
    return tuple(str(row[0]) for row in rows)


def _scan_documents[ModelT: BaseModel](
    connection: DuckDBPyConnection,
    *,
    table: Literal["normalized_observations", "metric_results", "diagnostic_results"],
    primary_id: Literal["observation_id", "result_id", "diagnostic_id"],
    model_type: type[ModelT],
) -> int:
    bounds = connection.execute(
        f"SELECT MIN(rowid), MAX(rowid), count(*) FROM {table}"  # noqa: S608
    ).fetchone()
    if bounds is None or bounds[2] == 0:
        return 0
    min_rowid, max_rowid, total_rows = bounds
    if min_rowid is None or max_rowid is None:
        return 0

    total_scanned = 0
    current_start = min_rowid
    while current_start <= max_rowid:
        current_end = current_start + _TRACEABILITY_BATCH_SIZE - 1
        page = _load_document_rowid_page(
            connection,
            table=table,
            primary_id=primary_id,
            start_rowid=current_start,
            end_rowid=current_end,
            model_type=model_type,
        )
        if page:
            total_scanned += len(page)
            del page
        current_start = current_end + 1

    if total_scanned != total_rows:
        raise StorageError("workspace traceability scan did not match its count")
    return total_scanned


def _load_document_rowid_page[ModelT: BaseModel](
    connection: DuckDBPyConnection,
    *,
    table: Literal["normalized_observations", "metric_results", "diagnostic_results"],
    primary_id: Literal["observation_id", "result_id", "diagnostic_id"],
    start_rowid: int,
    end_rowid: int,
    model_type: type[ModelT],
) -> tuple[tuple[str, ModelT], ...]:
    rows = connection.execute(
        f"SELECT {primary_id}, document_json FROM {table} WHERE rowid >= ? AND rowid <= ?",  # noqa: S608
        [start_rowid, end_rowid],
    ).fetchall()
    if len(rows) > _TRACEABILITY_BATCH_SIZE:
        raise StorageError("workspace traceability page exceeded its bound")
    return tuple((str(row[0]), model_from_json(model_type, row[1])) for row in rows)


def _verify_observation_raw_lineage(connection: DuckDBPyConnection) -> None:
    row = connection.execute(_OBSERVATION_RAW_QUERY).fetchone()
    if row is not None:
        raise WorkspaceBackupError("workspace contains an observation without its raw record")


def _verify_metric_observation_lineage(connection: DuckDBPyConnection) -> None:
    row = connection.execute(_METRIC_OBSERVATION_QUERY).fetchone()
    if row is not None:
        raise WorkspaceBackupError("workspace contains a metric without its observations")


def _verify_metric_metric_lineage(connection: DuckDBPyConnection) -> None:
    row = connection.execute(_METRIC_METRIC_QUERY).fetchone()
    if row is not None:
        raise WorkspaceBackupError("workspace contains a metric without its derived metrics")


def _verify_diagnostic_metric_lineage(connection: DuckDBPyConnection) -> None:
    row = connection.execute(_DIAGNOSTIC_METRIC_QUERY).fetchone()
    if row is not None:
        raise WorkspaceBackupError("workspace contains a diagnostic without its metrics")


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "WorkspaceBackupError",
    "WorkspaceBackupFile",
    "WorkspaceBackupManifest",
    "WorkspaceBackupService",
]
