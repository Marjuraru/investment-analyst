"""Resolution, initialization, inspection, and safe storage opening for workspaces."""

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
from pydantic import ValidationError

from investment_analyst.storage import LocalStorage, StorageError, StoragePaths
from investment_analyst.workspace.models import (
    APPLICATION_NAME,
    WORKSPACE_FORMAT_VERSION,
    WorkspaceAccessMode,
    WorkspaceInitialization,
    WorkspaceInspection,
    WorkspaceManifest,
    WorkspacePaths,
)

_WORKSPACE_ENVIRONMENT_VARIABLE = "INVESTMENT_ANALYST_WORKSPACE"
_XDG_DATA_HOME = "XDG_DATA_HOME"
_TABLES = {
    "raw_records": "raw_record_index",
    "observations": "normalized_observations",
    "metric_results": "metric_results",
    "diagnostic_results": "diagnostic_results",
}


class WorkspaceError(RuntimeError):
    """Base error for workspace operations."""


class WorkspaceConfigurationError(WorkspaceError):
    """Raised when workspace path configuration is invalid."""


class WorkspaceNotInitializedError(WorkspaceError):
    """Raised when an operation requires an initialized workspace."""


class WorkspaceManifestError(WorkspaceError):
    """Raised when a manifest is malformed or belongs to another application."""


class WorkspaceVersionError(WorkspaceManifestError):
    """Raised when the workspace format version is unsupported."""


class WorkspaceAccessError(WorkspaceError):
    """Raised when storage cannot be accessed in the requested mode."""


class WorkspaceLockedError(WorkspaceAccessError):
    """Raised when another process prevents writable access."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _is_lock_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(
        token in message
        for token in (
            "lock",
            "conflicting lock",
            "could not set lock",
            "database is locked",
        )
    )


class WorkspaceService:
    """Central application service for persistent workspace lifecycle operations."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._configured_workspace = environment.get(_WORKSPACE_ENVIRONMENT_VARIABLE)
        self._xdg_data_home = environment.get(_XDG_DATA_HOME)
        self._home = (Path.home() if home is None else Path(home)).expanduser().resolve()
        self._clock = clock

    def resolve(self, explicit_path: Path | None = None) -> WorkspacePaths:
        """Resolve workspace paths without creating or modifying filesystem entries."""
        if explicit_path is not None:
            root = self._normalize_configured_path(
                explicit_path,
                label="explicit workspace path",
            )
        elif self._configured_workspace is not None:
            root = self._normalize_configured_path(
                Path(self._configured_workspace),
                label=_WORKSPACE_ENVIRONMENT_VARIABLE,
            )
        elif self._xdg_data_home is not None:
            data_home = self._normalize_configured_path(
                Path(self._xdg_data_home),
                label=_XDG_DATA_HOME,
            )
            root = data_home / APPLICATION_NAME / "workspaces" / "default"
        else:
            root = self._home / ".local" / "share" / APPLICATION_NAME / "workspaces" / "default"
        normalized = root.resolve(strict=False)
        return WorkspacePaths(
            root=normalized,
            manifest_path=normalized / "manifest.json",
            storage_root=normalized / "storage",
            exports_root=normalized / "exports",
            state_root=normalized / "state",
        )

    def initialize(self, explicit_path: Path | None = None) -> WorkspaceInitialization:
        """Create or reuse a compatible workspace and initialize existing storage APIs."""
        paths = self.resolve(explicit_path)
        reused = paths.manifest_path.exists()
        if reused:
            manifest = self._load_manifest(paths)
        else:
            manifest = WorkspaceManifest(
                workspace_id=uuid4(),
                created_at=self._clock(),
            )

        paths.root.mkdir(parents=True, exist_ok=True)
        paths.storage_root.mkdir(parents=True, exist_ok=True)
        paths.exports_root.mkdir(parents=True, exist_ok=True)
        paths.state_root.mkdir(parents=True, exist_ok=True)

        storage_paths = StoragePaths.from_root(paths.storage_root)
        storage = self._open_backend(storage_paths, WorkspaceAccessMode.READ_WRITE)
        storage.close()

        if not reused:
            self._write_manifest_atomically(paths.manifest_path, manifest)

        return WorkspaceInitialization(
            paths=paths,
            manifest=manifest,
            reused=reused,
            storage_initialized=storage_paths.database_path.is_file(),
        )

    def inspect(self, explicit_path: Path | None = None) -> WorkspaceInspection:
        """Inspect a workspace without creating files, directories, tables, or checkpoints."""
        paths = self.resolve(explicit_path)
        manifest = self._load_manifest(paths)
        storage_paths = StoragePaths.from_root(paths.storage_root)

        storage_present = paths.storage_root.is_dir()
        database_present = storage_paths.database_path.is_file()
        raw_storage_present = storage_paths.raw_dir.is_dir()
        parquet_storage_present = storage_paths.exports_dir.is_dir()
        errors: list[str] = []
        warnings: list[str] = []

        if not storage_present:
            errors.append("workspace storage directory is missing")
        if not database_present:
            errors.append("workspace database is missing")
        if not raw_storage_present:
            warnings.append("raw storage directory is missing")
        if not parquet_storage_present:
            warnings.append("Parquet export directory is missing")

        counts = {name: 0 for name in _TABLES}
        if database_present:
            storage = self.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
            try:
                for name, table in _TABLES.items():
                    try:
                        row = storage.store.connection.execute(
                            f"SELECT COUNT(*) FROM {table}"  # noqa: S608
                        ).fetchone()
                    except duckdb.Error as error:
                        raise WorkspaceAccessError(
                            "workspace storage tables could not be inspected"
                        ) from error
                    if row is None:
                        raise WorkspaceAccessError("workspace count query returned no row")
                    counts[name] = int(row[0])
            finally:
                storage.close()

        status = "ready" if not errors and not warnings else "incomplete"
        return WorkspaceInspection(
            workspace_root=paths.root,
            manifest_valid=True,
            workspace_id=manifest.workspace_id,
            format_version=manifest.format_version,
            storage_present=storage_present,
            database_present=database_present,
            raw_storage_present=raw_storage_present,
            parquet_storage_present=parquet_storage_present,
            raw_record_count=counts["raw_records"],
            observation_count=counts["observations"],
            metric_result_count=counts["metric_results"],
            diagnostic_result_count=counts["diagnostic_results"],
            status=status,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def open_storage(
        self,
        paths: WorkspacePaths,
        mode: WorkspaceAccessMode,
    ) -> LocalStorage:
        """Open initialized storage in an explicit read-only or read-write mode."""
        self._load_manifest(paths)
        storage_paths = StoragePaths.from_root(paths.storage_root)
        if mode is WorkspaceAccessMode.READ_ONLY and not storage_paths.database_path.is_file():
            raise WorkspaceNotInitializedError("workspace database is not initialized")
        return self._open_backend(storage_paths, mode)

    def _normalize_configured_path(self, path: Path, *, label: str) -> Path:
        expanded = self._expand_user(path)
        if not expanded.is_absolute():
            raise WorkspaceConfigurationError(f"{label} must be an absolute path")
        return expanded

    def _expand_user(self, path: Path) -> Path:
        value = str(path)
        if value == "~":
            return self._home
        prefix = f"~{os.sep}"
        if value.startswith(prefix):
            return self._home / value[len(prefix) :]
        return path.expanduser()

    def _load_manifest(self, paths: WorkspacePaths) -> WorkspaceManifest:
        if not paths.root.is_dir() or not paths.manifest_path.is_file():
            raise WorkspaceNotInitializedError("workspace manifest was not found")
        try:
            text = paths.manifest_path.read_text(encoding="utf-8")
            raw = json.loads(text, parse_constant=_reject_json_constant)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise WorkspaceManifestError("workspace manifest is malformed") from error
        if not isinstance(raw, dict):
            raise WorkspaceManifestError("workspace manifest must be a JSON object")
        application = raw.get("application")
        if application != APPLICATION_NAME:
            raise WorkspaceManifestError("workspace manifest belongs to another application")
        version = raw.get("format_version")
        if isinstance(version, bool) or version != WORKSPACE_FORMAT_VERSION:
            raise WorkspaceVersionError("workspace format version is not supported")
        try:
            return WorkspaceManifest.model_validate_json(text)
        except ValidationError as error:
            raise WorkspaceManifestError("workspace manifest is malformed") from error

    def _write_manifest_atomically(
        self,
        target: Path,
        manifest: WorkspaceManifest,
    ) -> None:
        if target.exists():
            raise WorkspaceManifestError("workspace manifest already exists")
        document = json.dumps(
            manifest.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{document}\n")
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                raise WorkspaceManifestError("workspace manifest was created concurrently")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _open_backend(
        self,
        storage_paths: StoragePaths,
        mode: WorkspaceAccessMode,
    ) -> LocalStorage:
        storage = LocalStorage(
            storage_paths,
            read_only=mode is WorkspaceAccessMode.READ_ONLY,
        )
        try:
            return storage.open()
        except (duckdb.Error, OSError, StorageError) as error:
            storage.close()
            if mode is WorkspaceAccessMode.READ_WRITE and _is_lock_error(error):
                raise WorkspaceLockedError(
                    "workspace storage is locked by another process"
                ) from error
            raise WorkspaceAccessError(
                f"workspace storage could not be opened in {mode.value} mode"
            ) from error
