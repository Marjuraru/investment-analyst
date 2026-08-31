"""Verified local workspace backup and restore without format migration."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
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
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    SecFilerDocumentRepository,
    verify_filer_document_records,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
    verify_institutional_holding_records,
)
from investment_analyst.evidence.sec_ownership.repository import verify_ownership_records
from investment_analyst.storage import StorageError
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

_RAW_FIRST_PAGE = "SELECT record_id FROM raw_record_index ORDER BY record_id LIMIT ?"
_RAW_NEXT_PAGE = (
    "SELECT record_id FROM raw_record_index WHERE record_id > ? ORDER BY record_id LIMIT ?"
)
_OBSERVATION_FIRST_PAGE = (
    "SELECT observation_id, document_json FROM normalized_observations "
    "ORDER BY observation_id LIMIT ?"
)
_OBSERVATION_NEXT_PAGE = (
    "SELECT observation_id, document_json FROM normalized_observations "
    "WHERE observation_id > ? ORDER BY observation_id LIMIT ?"
)
_METRIC_FIRST_PAGE = (
    "SELECT result_id, document_json FROM metric_results ORDER BY result_id LIMIT ?"
)
_METRIC_NEXT_PAGE = (
    "SELECT result_id, document_json FROM metric_results "
    "WHERE result_id > ? ORDER BY result_id LIMIT ?"
)
_DIAGNOSTIC_FIRST_PAGE = (
    "SELECT diagnostic_id, document_json FROM diagnostic_results ORDER BY diagnostic_id LIMIT ?"
)
_DIAGNOSTIC_NEXT_PAGE = (
    "SELECT diagnostic_id, document_json FROM diagnostic_results "
    "WHERE diagnostic_id > ? ORDER BY diagnostic_id LIMIT ?"
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
) -> None:
    """Read and connect every persisted evidence layer with bounded memory."""
    paths = service.resolve(root)
    try:
        storage = service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
        try:
            _require_counts(storage, expected_counts)
            ActivityEventRepository(storage.paths.processed_dir, read_only=True).verify()
            _require_scan_count(
                _scan_raw_records(storage),
                expected_counts.raw_records,
            )
            _require_scan_count(
                _scan_documents(
                    storage.store.connection,
                    first_query=_OBSERVATION_FIRST_PAGE,
                    next_query=_OBSERVATION_NEXT_PAGE,
                    model_type=NormalizedObservation,
                ),
                expected_counts.observations,
            )
            _require_scan_count(
                _scan_documents(
                    storage.store.connection,
                    first_query=_METRIC_FIRST_PAGE,
                    next_query=_METRIC_NEXT_PAGE,
                    model_type=MetricResult,
                ),
                expected_counts.metric_results,
            )
            _require_scan_count(
                _scan_documents(
                    storage.store.connection,
                    first_query=_DIAGNOSTIC_FIRST_PAGE,
                    next_query=_DIAGNOSTIC_NEXT_PAGE,
                    model_type=DiagnosticResult,
                ),
                expected_counts.diagnostic_results,
            )

            _require_scan_count(
                _verify_observation_raw_lineage(storage.store.connection),
                expected_counts.observations,
            )
            _require_scan_count(
                _verify_metric_observation_lineage(storage.store.connection),
                expected_counts.metric_results,
            )
            _require_scan_count(
                _verify_metric_metric_lineage(storage.store.connection),
                expected_counts.metric_results,
            )
            _require_scan_count(
                _verify_diagnostic_metric_lineage(storage.store.connection),
                expected_counts.diagnostic_results,
            )
            _require_counts(storage, expected_counts)
        finally:
            storage.close()
    except WorkspaceBackupError:
        raise
    except (duckdb.Error, OSError, StorageError, WorkspaceError, ValueError) as error:
        raise WorkspaceBackupError("workspace traceability could not be verified") from error


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
    count = 0
    after_id: str | None = None
    while True:
        record_ids = _load_primary_id_page(
            storage.store.connection,
            first_query=_RAW_FIRST_PAGE,
            next_query=_RAW_NEXT_PAGE,
            after_id=after_id,
        )
        if not record_ids:
            return count
        records = storage.raw_records.get_many(tuple(UUID(value) for value in record_ids))
        if len(records) != len(record_ids):
            raise StorageError("raw record page did not resolve exactly")
        verify_document_records(
            records.values(),
            SecDocumentRepository(storage.raw_records, storage.documents),
        )
        verify_instrument_correspondence_records(records.values())
        verify_ownership_records(
            records.values(),
            SecDocumentRepository(storage.raw_records, storage.documents),
            storage.documents,
        )
        verify_beneficial_ownership_records(
            records.values(),
            SecDocumentRepository(storage.raw_records, storage.documents),
            storage.documents,
        )
        filer_documents = SecFilerDocumentRepository(storage.raw_records, storage.documents)
        verify_filer_document_records(records.values(), filer_documents)
        verify_institutional_holding_records(
            records.values(),
            InstitutionalHoldingsRepository(storage.raw_records),
            filer_documents,
            storage.documents,
        )
        count += len(records)
        after_id = record_ids[-1]
        del records


def _scan_documents[ModelT: BaseModel](
    connection: DuckDBPyConnection,
    *,
    first_query: str,
    next_query: str,
    model_type: type[ModelT],
) -> int:
    count = 0
    after_id: str | None = None
    while True:
        page = _load_document_page(
            connection,
            first_query=first_query,
            next_query=next_query,
            after_id=after_id,
            model_type=model_type,
        )
        if not page:
            return count
        count += len(page)
        after_id = page[-1][0]
        del page


def _verify_observation_raw_lineage(connection: DuckDBPyConnection) -> int:
    count = 0
    after_id: str | None = None
    while True:
        page = _load_document_page(
            connection,
            first_query=_OBSERVATION_FIRST_PAGE,
            next_query=_OBSERVATION_NEXT_PAGE,
            after_id=after_id,
            model_type=NormalizedObservation,
        )
        if not page:
            return count
        for _, observation in page:
            if not _references_exist(
                connection,
                table="raw_record_index",
                primary_id="record_id",
                references=(observation.raw_record_id,),
            ):
                raise WorkspaceBackupError(
                    "workspace contains an observation without its raw record"
                )
        count += len(page)
        after_id = page[-1][0]
        del page


def _verify_metric_observation_lineage(connection: DuckDBPyConnection) -> int:
    count = 0
    after_id: str | None = None
    while True:
        page = _load_document_page(
            connection,
            first_query=_METRIC_FIRST_PAGE,
            next_query=_METRIC_NEXT_PAGE,
            after_id=after_id,
            model_type=MetricResult,
        )
        if not page:
            return count
        for _, metric in page:
            if not _references_exist(
                connection,
                table="normalized_observations",
                primary_id="observation_id",
                references=metric.input_observation_ids,
            ):
                raise WorkspaceBackupError("workspace contains a metric without its observations")
        count += len(page)
        after_id = page[-1][0]
        del page


def _verify_metric_metric_lineage(connection: DuckDBPyConnection) -> int:
    count = 0
    after_id: str | None = None
    while True:
        page = _load_document_page(
            connection,
            first_query=_METRIC_FIRST_PAGE,
            next_query=_METRIC_NEXT_PAGE,
            after_id=after_id,
            model_type=MetricResult,
        )
        if not page:
            return count
        for _, metric in page:
            if not _references_exist(
                connection,
                table="metric_results",
                primary_id="result_id",
                references=metric.input_metric_result_ids,
            ):
                raise WorkspaceBackupError(
                    "workspace contains a metric without its derived metrics"
                )
        count += len(page)
        after_id = page[-1][0]
        del page


def _verify_diagnostic_metric_lineage(connection: DuckDBPyConnection) -> int:
    count = 0
    after_id: str | None = None
    while True:
        page = _load_document_page(
            connection,
            first_query=_DIAGNOSTIC_FIRST_PAGE,
            next_query=_DIAGNOSTIC_NEXT_PAGE,
            after_id=after_id,
            model_type=DiagnosticResult,
        )
        if not page:
            return count
        for _, diagnostic in page:
            references = tuple(
                metric_id
                for component in diagnostic.components
                for metric_id in component.metric_result_ids
            ) + tuple(evidence.metric_result_id for evidence in diagnostic.evidence)
            if not _references_exist(
                connection,
                table="metric_results",
                primary_id="result_id",
                references=references,
            ):
                raise WorkspaceBackupError("workspace contains a diagnostic without its metrics")
        count += len(page)
        after_id = page[-1][0]
        del page


def _load_primary_id_page(
    connection: DuckDBPyConnection,
    *,
    first_query: str,
    next_query: str,
    after_id: str | None,
) -> tuple[str, ...]:
    if after_id is None:
        rows = connection.execute(first_query, [_TRACEABILITY_BATCH_SIZE]).fetchall()
    else:
        rows = connection.execute(
            next_query,
            [after_id, _TRACEABILITY_BATCH_SIZE],
        ).fetchall()
    identifiers = tuple(str(row[0]) for row in rows)
    _validate_page(identifiers, after_id=after_id)
    return identifiers


def _load_document_page[ModelT: BaseModel](
    connection: DuckDBPyConnection,
    *,
    first_query: str,
    next_query: str,
    after_id: str | None,
    model_type: type[ModelT],
) -> tuple[tuple[str, ModelT], ...]:
    if after_id is None:
        rows = connection.execute(first_query, [_TRACEABILITY_BATCH_SIZE]).fetchall()
    else:
        rows = connection.execute(
            next_query,
            [after_id, _TRACEABILITY_BATCH_SIZE],
        ).fetchall()
    identifiers = tuple(str(row[0]) for row in rows)
    _validate_page(identifiers, after_id=after_id)
    return tuple(
        (identifier, model_from_json(model_type, row[1]))
        for identifier, row in zip(identifiers, rows, strict=True)
    )


def _validate_page(identifiers: tuple[str, ...], *, after_id: str | None) -> None:
    if len(identifiers) > _TRACEABILITY_BATCH_SIZE:
        raise StorageError("workspace traceability page exceeded its bound")
    if identifiers != tuple(sorted(identifiers)) or len(identifiers) != len(set(identifiers)):
        raise StorageError("workspace traceability page is not strictly ordered")
    if after_id is not None and identifiers and identifiers[0] <= after_id:
        raise StorageError("workspace traceability keyset did not advance")


def _references_exist(
    connection: DuckDBPyConnection,
    *,
    table: Literal["raw_record_index", "normalized_observations", "metric_results"],
    primary_id: Literal["record_id", "observation_id", "result_id"],
    references: tuple[UUID, ...] | list[UUID],
) -> bool:
    ordered = tuple(sorted(set(references), key=str))
    for offset in range(0, len(ordered), _TRACEABILITY_BATCH_SIZE):
        chunk = ordered[offset : offset + _TRACEABILITY_BATCH_SIZE]
        if _fetch_existing_ids(
            connection,
            table=table,
            primary_id=primary_id,
            references=chunk,
        ) != set(chunk):
            return False
    return True


def _fetch_existing_ids(
    connection: DuckDBPyConnection,
    *,
    table: Literal["raw_record_index", "normalized_observations", "metric_results"],
    primary_id: Literal["record_id", "observation_id", "result_id"],
    references: tuple[UUID, ...],
) -> set[UUID]:
    if not references:
        return set()
    if len(references) > _TRACEABILITY_BATCH_SIZE:
        raise StorageError("workspace traceability reference chunk exceeded its bound")
    placeholders = ", ".join("?" for _ in references)
    rows = connection.execute(
        f"SELECT {primary_id} FROM {table} "
        f"WHERE {primary_id} IN ({placeholders}) "
        f"ORDER BY {primary_id} LIMIT ?",  # noqa: S608
        [*(str(reference) for reference in references), _TRACEABILITY_BATCH_SIZE],
    ).fetchall()
    if len(rows) > _TRACEABILITY_BATCH_SIZE:
        raise StorageError("workspace traceability reference query exceeded its bound")
    return {UUID(str(row[0])) for row in rows}


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "WorkspaceBackupError",
    "WorkspaceBackupFile",
    "WorkspaceBackupManifest",
    "WorkspaceBackupService",
]
