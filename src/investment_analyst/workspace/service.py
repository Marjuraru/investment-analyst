"""Resolution, initialization, inspection, and safe storage opening for workspaces."""

import hashlib
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
    SUPPORTED_WORKSPACE_FORMAT_VERSIONS,
    WORKSPACE_FORMAT_VERSION,
    WorkspaceAccessMode,
    WorkspaceInitialization,
    WorkspaceInspection,
    WorkspaceManifest,
    WorkspaceMigrationIdentity,
    WorkspacePaths,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV1MigrationSourceSnapshot,
    WorkspaceV2SourceTransitionState,
)

_WORKSPACE_ENVIRONMENT_VARIABLE = "INVESTMENT_ANALYST_WORKSPACE"
_XDG_DATA_HOME = "XDG_DATA_HOME"


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

    def initialize(
        self,
        explicit_path: Path | None = None,
        *,
        format_version: int | None = None,
    ) -> WorkspaceInitialization:
        """Create or reuse a compatible workspace and initialize existing storage APIs."""
        requested_version = WORKSPACE_FORMAT_VERSION if format_version is None else format_version
        if (
            isinstance(requested_version, bool)
            or requested_version not in SUPPORTED_WORKSPACE_FORMAT_VERSIONS
        ):
            raise WorkspaceVersionError("workspace format version is not supported")
        paths = self.resolve(explicit_path)
        reused = paths.manifest_path.exists()
        if reused:
            manifest = self._load_manifest(paths)
            if format_version is not None and manifest.format_version != requested_version:
                raise WorkspaceVersionError(
                    "requested workspace format does not match its manifest"
                )
        else:
            manifest = WorkspaceManifest(
                workspace_id=uuid4(),
                created_at=self._clock(),
                format_version=requested_version,
            )

        paths.root.mkdir(parents=True, exist_ok=True)
        paths.storage_root.mkdir(parents=True, exist_ok=True)
        paths.exports_root.mkdir(parents=True, exist_ok=True)
        paths.state_root.mkdir(parents=True, exist_ok=True)

        storage_paths = StoragePaths.from_workspace_root(
            paths.root,
            format_version=manifest.format_version,
            workspace_id=manifest.workspace_id,
        )
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
        storage_paths = StoragePaths.from_workspace_root(
            paths.root,
            format_version=manifest.format_version,
            workspace_id=manifest.workspace_id,
        )

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

        counts = {
            "raw_records": 0,
            "observations": 0,
            "metric_results": 0,
            "diagnostic_results": 0,
        }
        if database_present:
            storage = self.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
            try:
                counts["raw_records"] = storage.raw_records.count()
                counts["observations"] = storage.observations.count()
                counts["metric_results"] = storage.metric_results.count()
                counts["diagnostic_results"] = storage.diagnostics.count()
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

    def manifest(self, explicit_path: Path | None = None) -> WorkspaceManifest:
        """Read one validated workspace manifest without opening its storage."""
        return self._load_manifest(self.resolve(explicit_path))

    def bind_v1_migration_identity(
        self,
        *,
        source_paths: WorkspacePaths,
        destination_paths: WorkspacePaths,
        source_fingerprint: str,
    ) -> WorkspaceMigrationIdentity:
        """Bind the v1 logical identity to a v2 target through workspace APIs.

        The v2 target retains its operational UUID; source UUID and creation
        instant are persisted in database metadata and a recoverable state file.
        A retry can repair only a missing state side after exact verification.
        """
        if not source_fingerprint.strip():
            raise WorkspaceConfigurationError("migration source fingerprint must be explicit")
        source_manifest = self._load_manifest(source_paths)
        destination_manifest = self._load_manifest(destination_paths)
        if source_manifest.format_version != 1 or destination_manifest.format_version != 2:
            raise WorkspaceVersionError(
                "migration identity requires a v1 source and v2 destination"
            )
        identity = WorkspaceMigrationIdentity(
            source_workspace_id=source_manifest.workspace_id,
            source_created_at=source_manifest.created_at,
            source_fingerprint=source_fingerprint,
            destination_workspace_id=destination_manifest.workspace_id,
        )
        state_path = destination_paths.state_root / "workspace-v2-migration-identity.json"
        if state_path.is_symlink():
            raise WorkspaceAccessError("migration identity state is unsafe")
        expected = {
            "source_workspace_id": str(identity.source_workspace_id),
            "source_created_at": identity.source_created_at.isoformat(),
            "source_fingerprint": identity.source_fingerprint,
            "destination_workspace_id": str(identity.destination_workspace_id),
        }
        with self.open_storage(destination_paths, WorkspaceAccessMode.READ_WRITE) as storage:
            connection = storage.store.connection
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workspace_v2_migration_identity ("
                "identity_key VARCHAR PRIMARY KEY, identity_value VARCHAR NOT NULL)"
            )
            rows = connection.execute(
                "SELECT identity_key, identity_value FROM workspace_v2_migration_identity"
            ).fetchall()
            found = {str(key): str(value) for key, value in rows}
            if found and found != expected:
                raise WorkspaceAccessError("migration identity database binding does not match")
            if not found:
                connection.executemany(
                    "INSERT INTO workspace_v2_migration_identity "
                    "(identity_key, identity_value) VALUES (?, ?)",
                    list(expected.items()),
                )
        if state_path.exists():
            try:
                persisted = WorkspaceMigrationIdentity.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as error:
                raise WorkspaceAccessError("migration identity state is incompatible") from error
            if persisted != identity:
                raise WorkspaceAccessError("migration identity state binding does not match")
        else:
            self._write_json_atomically(state_path, identity.to_json_dict())
        return identity

    def publish_v1_migration_source_snapshot(
        self,
        *,
        snapshot_paths: WorkspacePaths,
        descriptor: WorkspaceV1MigrationSourceSnapshot,
    ) -> WorkspaceV1MigrationSourceSnapshot:
        """Persist the descriptor for an already verified immutable v1 copy."""
        manifest = self._load_manifest(snapshot_paths)
        if (
            manifest.format_version != 1
            or manifest.workspace_id != descriptor.source_workspace_id
            or snapshot_paths.root != descriptor.snapshot_root
        ):
            raise WorkspaceConfigurationError("migration source snapshot does not match workspace")
        self._write_json_atomically(
            snapshot_paths.state_root / "workspace-v1-migration-source-snapshot.json",
            descriptor.to_json_dict(),
        )
        return descriptor

    def publish_v2_source_transition(
        self,
        *,
        destination_paths: WorkspacePaths,
        state: WorkspaceV2SourceTransitionState,
    ) -> WorkspaceV2SourceTransitionState:
        """Publish one immutable A-to-B provenance record through the workspace API."""
        manifest = self._load_manifest(destination_paths)
        if manifest.format_version != 2 or manifest.workspace_id != state.destination_workspace_id:
            raise WorkspaceConfigurationError("migration transition does not match destination")
        identity_path = destination_paths.state_root / "workspace-v2-migration-identity.json"
        if identity_path.is_symlink() or not identity_path.is_file():
            raise WorkspaceAccessError("migration identity is missing or unsafe")
        try:
            identity = WorkspaceMigrationIdentity.model_validate_json(
                identity_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WorkspaceAccessError("migration identity is incompatible") from error
        if (
            identity.source_workspace_id != state.source_workspace_id
            or identity.source_created_at != state.source_created_at
            or identity.source_fingerprint != state.historic_source_fingerprint
        ):
            raise WorkspaceAccessError("migration transition does not preserve historic identity")
        self._write_json_atomically(
            destination_paths.state_root / "workspace-v2-source-transition.json",
            state.to_json_dict(),
        )
        return state

    def v1_migration_identity(
        self, destination_paths: WorkspacePaths
    ) -> WorkspaceMigrationIdentity:
        """Read the immutable historic v1 identity associated with a v2 target."""
        path = destination_paths.state_root / "workspace-v2-migration-identity.json"
        if path.is_symlink() or not path.is_file():
            raise WorkspaceAccessError("migration identity is missing or unsafe")
        try:
            return WorkspaceMigrationIdentity.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkspaceAccessError("migration identity is incompatible") from error

    def v2_source_transition(
        self, destination_paths: WorkspacePaths
    ) -> WorkspaceV2SourceTransitionState | None:
        """Read the optional immutable A-to-B transition record."""
        path = destination_paths.state_root / "workspace-v2-source-transition.json"
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise WorkspaceAccessError("migration transition is unsafe")
        try:
            return WorkspaceV2SourceTransitionState.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WorkspaceAccessError("migration transition is incompatible") from error

    def advance_v2_source_transition(
        self,
        *,
        destination_paths: WorkspacePaths,
        state: WorkspaceV2SourceTransitionState,
    ) -> WorkspaceV2SourceTransitionState:
        """Atomically advance transition state while retaining prior bytes as history."""
        path = destination_paths.state_root / "workspace-v2-source-transition.json"
        previous = self.v2_source_transition(destination_paths)
        if previous is None:
            raise WorkspaceAccessError("migration transition is missing")
        immutable = (
            "source_workspace_id",
            "source_created_at",
            "historic_source_fingerprint",
            "historic_checkpoint_sha256",
            "snapshot_id",
            "snapshot_source_fingerprint",
            "destination_workspace_id",
        )
        if any(getattr(previous, field) != getattr(state, field) for field in immutable):
            raise WorkspaceAccessError("migration transition immutable provenance changed")
        order = {"PREPARED": 0, "VERIFYING": 1, "APPLYING": 2, "COMPLETE": 3}
        if order[state.phase.value] < order[previous.phase.value]:
            raise WorkspaceAccessError("migration transition phase cannot move backwards")
        original = path.read_bytes()
        history = destination_paths.state_root / "workspace-v2-source-transition-history"
        history.mkdir(parents=True, exist_ok=True)
        archive = history / f"{hashlib.sha256(original).hexdigest()}.json"
        if not archive.exists():
            self._write_json_atomically(
                archive,
                json.loads(original.decode("utf-8")),
            )
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(state.to_json_dict(), handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return state

    def open_storage(
        self,
        paths: WorkspacePaths,
        mode: WorkspaceAccessMode,
    ) -> LocalStorage:
        """Open initialized storage in an explicit read-only or read-write mode."""
        manifest = self._load_manifest(paths)
        storage_paths = StoragePaths.from_workspace_root(
            paths.root,
            format_version=manifest.format_version,
            workspace_id=manifest.workspace_id,
        )
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
        if isinstance(version, bool) or version not in SUPPORTED_WORKSPACE_FORMAT_VERSIONS:
            raise WorkspaceVersionError("workspace format version is not supported")
        try:
            return WorkspaceManifest.model_validate_json(text)
        except ValidationError as error:
            raise WorkspaceManifestError("workspace manifest is malformed") from error

    def _write_json_atomically(self, target: Path, document: Mapping[str, object]) -> None:
        """Write a new state document without following a pre-existing link."""
        if target.exists() or target.is_symlink():
            raise WorkspaceManifestError("workspace state document already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                raise WorkspaceManifestError("workspace state document was created concurrently")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

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
