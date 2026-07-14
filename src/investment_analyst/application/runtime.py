"""Central application composition for workspace, storage, and provider resolution."""

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models.base import ContractModel
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspacePaths
from investment_analyst.workspace.service import WorkspaceService


class ApplicationRuntimeError(RuntimeError):
    """Base error for centralized application composition."""


class StorageLocationError(ApplicationRuntimeError):
    """Raised when a requested storage location cannot be opened safely."""


class StorageLocationConflictError(StorageLocationError):
    """Raised when workspace and legacy storage roots are requested together."""


class StorageLocationKind(StrEnum):
    """Supported application storage layouts."""

    WORKSPACE = "workspace"
    LEGACY_ROOT = "legacy_root"


class _RuntimeModel(ContractModel):
    """Frozen strict base for application-runtime contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class StorageLocationRequest(_RuntimeModel):
    """Mutually exclusive workspace or legacy-root location request."""

    workspace: Path | None = None
    legacy_root: Path | None = None

    @field_validator("workspace", "legacy_root", mode="before")
    @classmethod
    def normalize_path(cls, value: object) -> object:
        """Expand and resolve explicit paths without creating filesystem entries."""
        if value is None:
            return None
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("storage path must not be empty")
            path = Path(value)
        elif isinstance(value, Path):
            path = value
        else:
            return value
        return path.expanduser().resolve(strict=False)

    @model_validator(mode="after")
    def validate_exclusive_location(self) -> "StorageLocationRequest":
        """Reject ambiguous requests before storage is opened."""
        if self.workspace is not None and self.legacy_root is not None:
            raise ValueError("workspace and legacy_root are mutually exclusive")
        return self


class ResolvedStorageLocation(_RuntimeModel):
    """Normalized application storage location without secrets or credentials."""

    kind: StorageLocationKind
    root: Path
    storage_root: Path
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "ResolvedStorageLocation":
        """Keep workspace and legacy layouts explicit and absolute."""
        if not self.root.is_absolute() or not self.storage_root.is_absolute():
            raise ValueError("resolved storage paths must be absolute")
        if self.kind is StorageLocationKind.LEGACY_ROOT:
            if self.storage_root != self.root:
                raise ValueError("legacy storage_root must equal root")
            if self.workspace_id is not None:
                raise ValueError("legacy storage cannot have a workspace_id")
        elif self.workspace_id is None:
            raise ValueError("workspace storage requires a workspace_id")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return compact location metadata."""
        return {
            "kind": self.kind.value,
            "root": str(self.root),
            "storage_root": str(self.storage_root),
            "workspace_id": str(self.workspace_id) if self.workspace_id else None,
        }


class ApplicationRuntime:
    """Compose workspace, storage, catalog, and provider resolution once per command."""

    def __init__(
        self,
        workspace_service: WorkspaceService,
        catalog: AssetCatalogService,
        provider_resolver: ProviderAssetContextResolver,
    ) -> None:
        self._workspace_service = workspace_service
        self._catalog = catalog
        self._provider_resolver = provider_resolver

    @classmethod
    def create_default(
        cls,
        *,
        workspace_service: WorkspaceService | None = None,
    ) -> "ApplicationRuntime":
        """Build one independent runtime with one catalog load and one resolver."""
        catalog = AssetCatalogService.load_default()
        return cls(
            workspace_service or WorkspaceService(),
            catalog,
            ProviderAssetContextResolver(catalog),
        )

    @property
    def workspace_service(self) -> WorkspaceService:
        """Return the injected workspace lifecycle service."""
        return self._workspace_service

    @property
    def catalog(self) -> AssetCatalogService:
        """Return the catalog loaded once for this runtime."""
        return self._catalog

    @property
    def provider_resolver(self) -> ProviderAssetContextResolver:
        """Return the provider resolver created once for this runtime."""
        return self._provider_resolver

    def resolve_storage(
        self,
        request: StorageLocationRequest,
        *,
        access_mode: WorkspaceAccessMode,
    ) -> ResolvedStorageLocation:
        """Resolve workspace or legacy storage without opening it."""
        location, _ = self._resolve_location(request, access_mode=access_mode)
        return location

    @contextmanager
    def open_storage(
        self,
        request: StorageLocationRequest,
        *,
        access_mode: WorkspaceAccessMode,
    ) -> Iterator[LocalStorage]:
        """Open one storage facade in the explicitly requested access mode."""
        location, workspace_paths = self._resolve_location(request, access_mode=access_mode)
        if workspace_paths is not None:
            storage = self._workspace_service.open_storage(workspace_paths, access_mode)
        else:
            storage_paths = StoragePaths.from_root(location.storage_root)
            if (
                access_mode is WorkspaceAccessMode.READ_ONLY
                and not storage_paths.database_path.is_file()
            ):
                raise StorageLocationError("legacy storage database is not initialized")
            storage = LocalStorage(
                storage_paths,
                read_only=access_mode is WorkspaceAccessMode.READ_ONLY,
            ).open()
        try:
            yield storage
        finally:
            storage.close()

    def _resolve_location(
        self,
        request: StorageLocationRequest,
        *,
        access_mode: WorkspaceAccessMode,
    ) -> tuple[ResolvedStorageLocation, WorkspacePaths | None]:
        if not isinstance(access_mode, WorkspaceAccessMode):
            raise StorageLocationError("storage access mode is invalid")
        if request.workspace is not None and request.legacy_root is not None:
            raise StorageLocationConflictError(
                "workspace and legacy storage root cannot be used together"
            )
        if request.legacy_root is not None:
            storage_paths = StoragePaths.from_root(request.legacy_root)
            return (
                ResolvedStorageLocation(
                    kind=StorageLocationKind.LEGACY_ROOT,
                    root=storage_paths.root,
                    storage_root=storage_paths.root,
                ),
                None,
            )
        workspace_paths = self._workspace_service.resolve(request.workspace)
        inspection = self._workspace_service.inspect(workspace_paths.root)
        return (
            ResolvedStorageLocation(
                kind=StorageLocationKind.WORKSPACE,
                root=workspace_paths.root,
                storage_root=workspace_paths.storage_root,
                workspace_id=inspection.workspace_id,
            ),
            workspace_paths,
        )
