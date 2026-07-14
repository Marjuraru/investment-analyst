"""Strict models for persistent investment-analyst workspaces."""

from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

APPLICATION_NAME = "investment-analyst"
WORKSPACE_FORMAT_VERSION = 1


class _WorkspaceModel(ContractModel):
    """Frozen strict base for workspace contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )


class WorkspaceAccessMode(StrEnum):
    """Explicit storage access requested by an application service."""

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class WorkspacePaths(_WorkspaceModel):
    """Normalized filesystem layout for one workspace."""

    root: Path
    manifest_path: Path
    storage_root: Path
    exports_root: Path
    state_root: Path

    @model_validator(mode="after")
    def validate_layout(self) -> "WorkspacePaths":
        """Require absolute paths and the fixed workspace layout."""
        values = (
            self.root,
            self.manifest_path,
            self.storage_root,
            self.exports_root,
            self.state_root,
        )
        if not all(path.is_absolute() for path in values):
            raise ValueError("workspace paths must be absolute")
        if self.manifest_path != self.root / "manifest.json":
            raise ValueError("manifest_path must be located at workspace root")
        if self.storage_root != self.root / "storage":
            raise ValueError("storage_root must be the workspace storage directory")
        if self.exports_root != self.root / "exports":
            raise ValueError("exports_root must be the workspace exports directory")
        if self.state_root != self.root / "state":
            raise ValueError("state_root must be the workspace state directory")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact JSON-safe representation."""
        return {
            "root": str(self.root),
            "manifest_path": str(self.manifest_path),
            "storage_root": str(self.storage_root),
            "exports_root": str(self.exports_root),
            "state_root": str(self.state_root),
        }


class WorkspaceManifest(_WorkspaceModel):
    """Versioned identity document stored at workspace root."""

    application: Literal["investment-analyst"] = APPLICATION_NAME
    workspace_id: UUID
    format_version: Literal[1] = WORKSPACE_FORMAT_VERSION
    created_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives without filesystem data."""
        return {
            "application": self.application,
            "created_at": self.created_at.isoformat(),
            "format_version": self.format_version,
            "workspace_id": str(self.workspace_id),
        }


class WorkspaceInitialization(_WorkspaceModel):
    """Outcome of an idempotent workspace initialization."""

    paths: WorkspacePaths
    manifest: WorkspaceManifest
    initialized: Literal[True] = True
    reused: bool
    storage_initialized: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return the script-facing initialization summary."""
        return {
            "workspace": str(self.paths.root),
            "workspace_id": str(self.manifest.workspace_id),
            "format_version": self.manifest.format_version,
            "initialized": self.initialized,
            "reused": self.reused,
            "storage_initialized": self.storage_initialized,
        }


class WorkspaceInspection(_WorkspaceModel):
    """Compact read-only health and record-count report."""

    workspace_root: Path
    manifest_valid: bool
    workspace_id: UUID
    format_version: int
    storage_present: bool
    database_present: bool
    raw_storage_present: bool
    parquet_storage_present: bool
    raw_record_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    metric_result_count: int = Field(ge=0)
    diagnostic_result_count: int = Field(ge=0)
    status: Literal["ready", "incomplete"]
    errors: tuple[NonEmptyStr, ...] = ()
    warnings: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> "WorkspaceInspection":
        """Keep readiness consistent with reported errors and layout."""
        complete_layout = all(
            (
                self.manifest_valid,
                self.storage_present,
                self.database_present,
                self.raw_storage_present,
                self.parquet_storage_present,
            )
        )
        expected = "ready" if complete_layout and not self.errors else "incomplete"
        if self.status != expected:
            raise ValueError("workspace inspection status is inconsistent")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return compact JSON primitives without persisted documents."""
        return {
            "workspace_root": str(self.workspace_root),
            "manifest_valid": self.manifest_valid,
            "workspace_id": str(self.workspace_id),
            "format_version": self.format_version,
            "storage_present": self.storage_present,
            "database_present": self.database_present,
            "raw_storage_present": self.raw_storage_present,
            "parquet_storage_present": self.parquet_storage_present,
            "counts": {
                "raw_records": self.raw_record_count,
                "observations": self.observation_count,
                "metric_results": self.metric_result_count,
                "diagnostic_results": self.diagnostic_result_count,
            },
            "status": self.status,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }
