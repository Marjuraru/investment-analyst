"""Strict operational contracts for workspace-v2 preservation and retirement."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class LifecycleErrorReason(StrEnum):
    """Explicit reason an operation did not advance."""

    ACTIVE_REFERENCE = "active_reference"
    CAPACITY = "capacity"
    CONTENT_UNKNOWN = "content_unknown"
    INVALID_BINDING = "invalid_binding"
    UNSAFE_PATH = "unsafe_path"


class LifecycleContentRole(StrEnum):
    """Physical role assigned during the single metadata pass over a source."""

    MIGRATED_RECOVERABLE = "migrated_recoverable"
    OPERATIONAL_HISTORIC = "operational_historic"
    REGENERABLE = "regenerable"
    UNKNOWN = "unknown"


class LifecyclePhase(StrEnum):
    """Durable lifecycle phases; a phase never moves backwards."""

    INVENTORIED = "inventoried"
    COMPLEMENT_PUBLISHED = "complement_published"
    RETIRING = "retiring"
    RETIRED = "retired"


class _LifecycleModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FrozenSourceCut(_LifecycleModel):
    """Identity of a physically frozen source inspected through a read-only handle."""

    schema_version: Literal["workspace-v2-frozen-source-cut-v1"] = (
        "workspace-v2-frozen-source-cut-v1"
    )
    operation_id: UUID
    source_root: Path
    workspace_id: UUID
    workspace_format_version: Literal[1, 2]
    created_at: UTCDateTime
    source_fingerprint: NonEmptyStr
    writer_closed: Literal[True] = True
    frozen_at: UTCDateTime

    @field_validator("source_root")
    @classmethod
    def require_absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("frozen source root must be absolute")
        return value

    @field_validator("source_fingerprint")
    @classmethod
    def require_fingerprint(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("frozen source fingerprint is invalid")
        return value


class LifecycleFile(_LifecycleModel):
    """One hashed file retained by a complement or an inventory result."""

    path: NonEmptyStr
    role: LifecycleContentRole
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("lifecycle file path must be normalized and relative")
        return value

    @field_validator("sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("lifecycle file sha256 is invalid")
        return value


class LifecycleInventory(_LifecycleModel):
    """Bounded summary of a source metadata pass, without a global file tree."""

    schema_version: Literal["workspace-v2-lifecycle-inventory-v1"] = (
        "workspace-v2-lifecycle-inventory-v1"
    )
    frozen_source: FrozenSourceCut
    file_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    roles: dict[LifecycleContentRole, int]
    unknown_file_count: int = Field(ge=0)

    @model_validator(mode="after")
    def require_role_total(self) -> LifecycleInventory:
        if sum(self.roles.values()) != self.file_count:
            raise ValueError("lifecycle role counts do not match file count")
        if self.roles.get(LifecycleContentRole.UNKNOWN, 0) != self.unknown_file_count:
            raise ValueError("lifecycle unknown count does not match role count")
        return self


class LocalComplementManifest(_LifecycleModel):
    """Sealed small preservation set outside the source tree scheduled for retirement."""

    schema_version: Literal["workspace-v2-local-complement-v1"] = "workspace-v2-local-complement-v1"
    source: FrozenSourceCut
    created_at: UTCDateTime
    files: tuple[LifecycleFile, ...]
    total_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def require_complete_sorted_inventory(self) -> LocalComplementManifest:
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("complement files must be non-empty, unique, and sorted")
        if sum(item.size_bytes for item in self.files) != self.total_bytes:
            raise ValueError("complement total bytes does not match files")
        return self


class LifecycleCheckpoint(_LifecycleModel):
    """Durable operation cursor bound to a frozen source and exact parameters."""

    schema_version: Literal["workspace-v2-lifecycle-checkpoint-v1"] = (
        "workspace-v2-lifecycle-checkpoint-v1"
    )
    operation_id: UUID
    phase: LifecyclePhase
    source: FrozenSourceCut
    complement_sha256: str | None = None
    retirement_root: Path | None = None
    files_confirmed: int = Field(ge=0)
    bytes_confirmed: int = Field(ge=0)
    reason: LifecycleErrorReason | None = None

    @field_validator("complement_sha256")
    @classmethod
    def require_optional_digest(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("lifecycle complement sha256 is invalid")
        return value


class LifecycleReceipt(_LifecycleModel):
    """Terminal receipt for a service-owned lifecycle operation."""

    schema_version: Literal["workspace-v2-lifecycle-receipt-v1"] = (
        "workspace-v2-lifecycle-receipt-v1"
    )
    operation_id: UUID
    phase: LifecyclePhase
    source_root: Path
    complement_root: Path | None = None
    files_confirmed: int = Field(ge=0)
    bytes_confirmed: int = Field(ge=0)
    completed_at: UTCDateTime
