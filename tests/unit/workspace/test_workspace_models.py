"""Unit tests for strict workspace contracts."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.workspace.models import (
    WorkspaceAccessMode,
    WorkspaceInspection,
    WorkspaceManifest,
    WorkspacePaths,
)

_WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
_CREATED_AT = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        root=root,
        manifest_path=root / "manifest.json",
        storage_root=root / "storage",
        exports_root=root / "exports",
        state_root=root / "state",
    )


def test_workspace_paths_require_absolute_fixed_layout(tmp_path) -> None:
    paths = _paths(tmp_path.resolve())

    assert paths.storage_root == paths.root / "storage"
    with pytest.raises(ValidationError, match="absolute"):
        _paths(Path("relative"))
    with pytest.raises(ValidationError, match="manifest_path"):
        WorkspacePaths(
            root=tmp_path.resolve(),
            manifest_path=tmp_path.resolve() / "other.json",
            storage_root=tmp_path.resolve() / "storage",
            exports_root=tmp_path.resolve() / "exports",
            state_root=tmp_path.resolve() / "state",
        )


def test_manifest_is_strict_frozen_and_utc() -> None:
    manifest = WorkspaceManifest(workspace_id=_WORKSPACE_ID, created_at=_CREATED_AT)

    assert manifest.application == "investment-analyst"
    assert manifest.format_version == 1
    assert manifest.created_at.tzinfo is UTC
    with pytest.raises(ValidationError, match="timezone"):
        WorkspaceManifest(
            workspace_id=_WORKSPACE_ID,
            created_at=datetime(2026, 7, 14, 12, 0),
        )
    with pytest.raises(ValidationError, match="extra"):
        WorkspaceManifest(
            workspace_id=_WORKSPACE_ID,
            created_at=_CREATED_AT,
            secret="forbidden",
        )
    with pytest.raises(ValidationError, match="frozen"):
        manifest.created_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_manifest_json_contains_no_paths_or_credentials() -> None:
    document = WorkspaceManifest(
        workspace_id=_WORKSPACE_ID,
        created_at=_CREATED_AT,
    ).to_json_dict()

    assert document == {
        "application": "investment-analyst",
        "created_at": "2026-07-14T12:00:00+00:00",
        "format_version": 1,
        "workspace_id": str(_WORKSPACE_ID),
    }
    assert "path" not in str(document).casefold()
    assert "credential" not in str(document).casefold()


def test_inspection_serialization_is_compact_and_deterministic(tmp_path) -> None:
    inspection = WorkspaceInspection(
        workspace_root=tmp_path.resolve(),
        manifest_valid=True,
        workspace_id=_WORKSPACE_ID,
        format_version=1,
        storage_present=True,
        database_present=True,
        raw_storage_present=True,
        parquet_storage_present=True,
        raw_record_count=1,
        observation_count=2,
        metric_result_count=3,
        diagnostic_result_count=4,
        status="ready",
    )

    first = inspection.to_json_dict()
    second = inspection.to_json_dict()

    assert first == second
    assert first["counts"] == {
        "raw_records": 1,
        "observations": 2,
        "metric_results": 3,
        "diagnostic_results": 4,
    }
    assert "documents" not in str(first)
    invalid = inspection.model_dump()
    invalid["raw_record_count"] = True
    with pytest.raises(ValidationError):
        WorkspaceInspection.model_validate(invalid)


def test_access_modes_are_explicit_strings() -> None:
    assert WorkspaceAccessMode.READ_ONLY.value == "read_only"
    assert WorkspaceAccessMode.READ_WRITE.value == "read_write"
