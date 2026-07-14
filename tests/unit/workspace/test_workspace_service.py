"""Unit tests for workspace resolution and lifecycle behavior."""

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import (
    WorkspaceAccessError,
    WorkspaceConfigurationError,
    WorkspaceLockedError,
    WorkspaceManifestError,
    WorkspaceNotInitializedError,
    WorkspaceService,
    WorkspaceVersionError,
)

_FIXED_TIME = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _service(
    *,
    environ: dict[str, str] | None = None,
    home: Path,
) -> WorkspaceService:
    return WorkspaceService(
        environ={} if environ is None else environ,
        home=home,
        clock=lambda: _FIXED_TIME,
    )


def test_resolution_precedence_cli_environment_xdg_and_fallback(tmp_path) -> None:
    home = tmp_path / "home"
    explicit = tmp_path / "explicit"
    configured = tmp_path / "configured"
    xdg = tmp_path / "xdg"
    service = _service(
        environ={
            "INVESTMENT_ANALYST_WORKSPACE": str(configured),
            "XDG_DATA_HOME": str(xdg),
        },
        home=home,
    )

    assert service.resolve(explicit).root == explicit.resolve()
    assert service.resolve().root == configured.resolve()
    assert (
        _service(environ={"XDG_DATA_HOME": str(xdg)}, home=home).resolve().root
        == (xdg / "investment-analyst" / "workspaces" / "default").resolve()
    )
    assert (
        _service(home=home).resolve().root
        == (home / ".local" / "share" / "investment-analyst" / "workspaces" / "default").resolve()
    )


def test_resolution_expands_tilde_and_rejects_relative_configuration(tmp_path) -> None:
    home = (tmp_path / "home").resolve()
    service = _service(
        environ={"INVESTMENT_ANALYST_WORKSPACE": "~/portfolio"},
        home=home,
    )

    assert service.resolve().root == home / "portfolio"
    with pytest.raises(WorkspaceConfigurationError, match="absolute"):
        _service(
            environ={"INVESTMENT_ANALYST_WORKSPACE": "relative/workspace"},
            home=home,
        ).resolve()
    with pytest.raises(WorkspaceConfigurationError, match="absolute"):
        _service(environ={"XDG_DATA_HOME": "relative/xdg"}, home=home).resolve()
    with pytest.raises(WorkspaceConfigurationError, match="absolute"):
        _service(home=home).resolve(Path("relative/cli"))


def test_resolve_performs_no_writes(tmp_path) -> None:
    root = tmp_path / "workspace"

    paths = _service(home=tmp_path / "home").resolve(root)

    assert paths.root == root.resolve()
    assert not root.exists()


def test_initialize_is_idempotent_canonical_and_preserves_files(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    preserved = root / "keep.txt"
    preserved.write_text("keep", encoding="utf-8")
    service = _service(home=tmp_path / "home")

    first = service.initialize(root)
    manifest_bytes = first.paths.manifest_path.read_bytes()
    second = service.initialize(root)

    assert first.reused is False
    assert second.reused is True
    assert first.manifest.workspace_id == second.manifest.workspace_id
    assert first.manifest.created_at == _FIXED_TIME
    assert manifest_bytes.endswith(b"\n")
    assert manifest_bytes == second.paths.manifest_path.read_bytes()
    assert json.loads(manifest_bytes) == first.manifest.to_json_dict()
    assert preserved.read_text(encoding="utf-8") == "keep"
    assert first.paths.storage_root.is_dir()
    assert first.paths.exports_root.is_dir()
    assert first.paths.state_root.is_dir()
    assert first.storage_initialized


def test_manifest_errors_are_typed_and_never_overwritten(tmp_path) -> None:
    service = _service(home=tmp_path / "home")
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "manifest.json").write_text("not-json\n", encoding="utf-8")
    wrong_application = tmp_path / "wrong-app"
    wrong_application.mkdir()
    (wrong_application / "manifest.json").write_text(
        '{"application":"other","format_version":1}\n',
        encoding="utf-8",
    )
    wrong_version = tmp_path / "wrong-version"
    wrong_version.mkdir()
    version_document = (
        '{"application":"investment-analyst","workspace_id":'
        '"11111111-1111-4111-8111-111111111111","format_version":2,'
        '"created_at":"2026-07-14T12:00:00Z"}\n'
    )
    (wrong_version / "manifest.json").write_text(version_document, encoding="utf-8")

    with pytest.raises(WorkspaceManifestError, match="malformed"):
        service.initialize(malformed)
    with pytest.raises(WorkspaceManifestError, match="another application"):
        service.initialize(wrong_application)
    with pytest.raises(WorkspaceVersionError, match="not supported"):
        service.initialize(wrong_version)
    assert (wrong_version / "manifest.json").read_text(encoding="utf-8") == version_document


def test_read_only_requires_existing_workspace_and_creates_nothing(tmp_path) -> None:
    root = tmp_path / "missing"
    service = _service(home=tmp_path / "home")
    paths = service.resolve(root)

    with pytest.raises(WorkspaceNotInitializedError, match="manifest"):
        service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
    with pytest.raises(WorkspaceNotInitializedError, match="manifest"):
        service.inspect(root)
    assert not root.exists()


def test_read_write_initializes_and_read_only_blocks_file_writes(tmp_path) -> None:
    service = _service(home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace")

    writer = service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE)
    assert writer.is_open
    assert writer.read_only is False
    writer.close()

    reader = service.open_storage(initialized.paths, WorkspaceAccessMode.READ_ONLY)
    try:
        assert reader.read_only is True
        with pytest.raises(StorageError, match="read-only storage"):
            reader.raw_records.save(object())
        with pytest.raises(StorageError, match="read-only storage"):
            reader.parquet.export_table("assets")
    finally:
        reader.close()


def test_lock_and_access_errors_are_mapped_without_secret_values(tmp_path, monkeypatch) -> None:
    service = _service(home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace")
    secret = "super-secret-token"

    def locked_open(_storage) -> None:
        raise duckdb.IOException(f"Could not set lock on file; hidden={secret}")

    monkeypatch.setattr("investment_analyst.workspace.service.LocalStorage.open", locked_open)

    with pytest.raises(WorkspaceLockedError) as captured:
        service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE)
    assert secret not in str(captured.value)

    def inaccessible_open(_storage) -> None:
        raise duckdb.IOException(f"permission denied; hidden={secret}")

    monkeypatch.setattr("investment_analyst.workspace.service.LocalStorage.open", inaccessible_open)
    with pytest.raises(WorkspaceAccessError) as captured:
        service.open_storage(initialized.paths, WorkspaceAccessMode.READ_ONLY)
    assert secret not in str(captured.value)
