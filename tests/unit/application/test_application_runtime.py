"""Unit coverage for centralized application runtime composition."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ResolvedStorageLocation,
    StorageLocationError,
    StorageLocationKind,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import (
    WorkspaceManifestError,
    WorkspaceNotInitializedError,
    WorkspaceService,
    WorkspaceVersionError,
)


def _runtime(workspace_service: WorkspaceService) -> ApplicationRuntime:
    catalog = AssetCatalogService.load_default()
    return ApplicationRuntime(
        workspace_service,
        catalog,
        ProviderAssetContextResolver(catalog),
    )


def test_storage_location_request_normalizes_paths_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    empty = StorageLocationRequest()
    workspace = StorageLocationRequest(workspace=tmp_path / "workspace")
    legacy = StorageLocationRequest(legacy_root=tmp_path / "legacy")

    assert empty.workspace is None
    assert workspace.workspace == (tmp_path / "workspace").resolve()
    assert legacy.legacy_root == (tmp_path / "legacy").resolve()
    assert workspace.workspace.is_absolute()
    with pytest.raises(ValidationError, match="mutually exclusive"):
        StorageLocationRequest(workspace=tmp_path, legacy_root=tmp_path)
    with pytest.raises(ValidationError):
        StorageLocationRequest(workspace="")
    with pytest.raises(ValidationError):
        StorageLocationRequest(extra="forbidden")


def test_resolved_location_is_strict_and_compact(tmp_path: Path) -> None:
    location = ResolvedStorageLocation(
        kind=StorageLocationKind.LEGACY_ROOT,
        root=tmp_path,
        storage_root=tmp_path,
    )

    assert location.to_json_dict() == {
        "kind": "legacy_root",
        "root": str(tmp_path),
        "storage_root": str(tmp_path),
        "workspace_id": None,
    }
    with pytest.raises(ValidationError):
        location.root = tmp_path / "other"


def test_default_runtime_loads_one_catalog_and_one_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AssetCatalogService.load_default
    calls = 0

    def counted_load() -> AssetCatalogService:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(AssetCatalogService, "load_default", counted_load)
    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=Path.home())
    )

    assert calls == 1
    assert runtime.catalog is runtime.catalog
    assert runtime.provider_resolver is runtime.provider_resolver


def test_legacy_read_write_and_read_only_are_explicit(tmp_path: Path) -> None:
    runtime = _runtime(WorkspaceService(environ={}, home=tmp_path))
    request = StorageLocationRequest(legacy_root=tmp_path / "legacy")

    with runtime.open_storage(request, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        assert storage.is_open
        assert not storage.read_only
        opened = storage
    assert not opened.is_open

    with runtime.open_storage(request, access_mode=WorkspaceAccessMode.READ_ONLY) as storage:
        assert storage.is_open
        assert storage.read_only


def test_legacy_read_only_does_not_initialize_missing_database(tmp_path: Path) -> None:
    runtime = _runtime(WorkspaceService(environ={}, home=tmp_path))
    root = tmp_path / "missing"

    with (
        pytest.raises(StorageLocationError, match="not initialized"),
        runtime.open_storage(
            StorageLocationRequest(legacy_root=root),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ),
    ):
        raise AssertionError("storage must not open")

    assert not root.exists()


def test_workspace_modes_use_workspace_service(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path)
    workspace = tmp_path / "workspace"
    initialization = service.initialize(workspace)
    runtime = _runtime(service)
    request = StorageLocationRequest(workspace=workspace)

    location = runtime.resolve_storage(request, access_mode=WorkspaceAccessMode.READ_ONLY)
    assert location.kind is StorageLocationKind.WORKSPACE
    assert location.root == workspace.resolve()
    assert location.storage_root == workspace.resolve() / "storage"
    assert location.workspace_id == initialization.manifest.workspace_id

    with runtime.open_storage(request, access_mode=WorkspaceAccessMode.READ_ONLY) as reader:
        assert reader.read_only
    with runtime.open_storage(request, access_mode=WorkspaceAccessMode.READ_WRITE) as writer:
        assert not writer.read_only


def test_default_workspace_uses_workspace_service_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "configured"
    service = WorkspaceService(
        environ={"INVESTMENT_ANALYST_WORKSPACE": str(workspace)},
        home=tmp_path,
    )
    service.initialize()
    runtime = _runtime(service)

    location = runtime.resolve_storage(
        StorageLocationRequest(),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    )
    assert location.root == workspace.resolve()
    with runtime.open_storage(
        StorageLocationRequest(),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        assert storage.read_only


def test_resolve_storage_rejects_missing_corrupt_and_incompatible_workspaces(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path)
    runtime = _runtime(service)
    missing = tmp_path / "missing"

    with pytest.raises(WorkspaceNotInitializedError):
        runtime.resolve_storage(
            StorageLocationRequest(workspace=missing),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        )
    assert not missing.exists()

    corrupt = tmp_path / "corrupt"
    service.initialize(corrupt)
    (corrupt / "manifest.json").write_text("{not-json\n", encoding="utf-8")
    with pytest.raises(WorkspaceManifestError):
        runtime.resolve_storage(
            StorageLocationRequest(workspace=corrupt),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        )

    incompatible = tmp_path / "incompatible"
    service.initialize(incompatible)
    manifest_path = incompatible / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["format_version"] = 999
    manifest_path.write_text(
        f"{json.dumps(document, sort_keys=True, separators=(',', ':'))}\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceVersionError):
        runtime.resolve_storage(
            StorageLocationRequest(workspace=incompatible),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        )


def test_legacy_resolution_has_no_workspace_id(tmp_path: Path) -> None:
    runtime = _runtime(WorkspaceService(environ={}, home=tmp_path))
    location = runtime.resolve_storage(
        StorageLocationRequest(legacy_root=tmp_path / "legacy"),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    )

    assert location.kind is StorageLocationKind.LEGACY_ROOT
    assert location.workspace_id is None


def test_workspace_error_is_preserved(tmp_path: Path) -> None:
    runtime = _runtime(WorkspaceService(environ={}, home=tmp_path))

    with (
        pytest.raises(WorkspaceNotInitializedError),
        runtime.open_storage(
            StorageLocationRequest(workspace=tmp_path / "missing"),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ),
    ):
        raise AssertionError("storage must not open")


def test_legacy_runtime_preserves_existing_storage_identity(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    with LocalStorage(StoragePaths.from_root(root)) as original:
        database = original.paths.database_path

    runtime = _runtime(WorkspaceService(environ={}, home=tmp_path))
    with runtime.open_storage(
        StorageLocationRequest(legacy_root=root),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as reopened:
        assert reopened.paths.database_path == database
