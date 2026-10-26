"""Strict operational contracts for storage-retirement planning and execution."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

OPERATIONAL_RETENTION_PLAN_SCHEMA = "operational-retention-plan-v1"
OPERATIONAL_RETENTION_CHECKPOINT_SCHEMA = "operational-retention-checkpoint-v1"
OPERATIONAL_RETENTION_RECEIPT_SCHEMA = "operational-retention-receipt-v1"
OPERATIONAL_RETENTION_REMOTE_RECEIPT_SCHEMA = "operational-retention-remote-receipt-v1"


class RetentionClass(StrEnum):
    """Closed operational artifact classes eligible for this contract."""

    FAILED_RESTORE_STAGING = "failed_restore_staging"
    RESTORE_STAGING = "restore_staging"
    BACKUP_GENERATION = "backup_generation"
    PACKED_BACKUP = "packed_backup"
    REMOTE_STAGING = "remote_staging"
    REHEARSAL_WORKSPACE = "rehearsal_workspace"
    LEGACY_WORKSPACE_PAYLOAD = "legacy_workspace_payload"
    INSTALLED_RELEASE = "installed_release"
    OPERATIONAL_CONTROL = "operational_control"


class RetentionDecision(StrEnum):
    """Explicit disposition; only registered decisions can be executed."""

    KEEP_LOCAL = "KEEP_LOCAL"
    REMOTE_ONLY = "REMOTE_ONLY"
    RETIRED = "RETIRED"
    RETIRE_AFTER = "RETIRE_AFTER"
    UNKNOWN = "UNKNOWN"


class RetentionEntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"


class _RetentionModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _require_relative_normalized(value: str, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise ValueError(f"{label} must be normalized and relative")
    if path.parts and path.parts[0] == "":
        raise ValueError(f"{label} must be normalized and relative")
    return value


def _require_absolute_normalized(value: Path, label: str) -> Path:
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError(f"{label} must be absolute and normalized")
    return value


def _require_digest(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} is invalid")
    return value


class RetentionRequestEntry(_RetentionModel):
    """Classification supplied by the operator for one exact root-relative path."""

    path: NonEmptyStr
    kind: RetentionEntryKind
    retention_class: RetentionClass
    decision: RetentionDecision
    reason: NonEmptyStr
    last_consumer: NonEmptyStr
    dependencies: tuple[NonEmptyStr, ...] = ()
    remote_evidence: tuple[NonEmptyStr, ...] = ()
    retire_after: NonEmptyStr | None = None

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        return _require_relative_normalized(value, "retention request path")

    @model_validator(mode="after")
    def require_decision_evidence(self) -> RetentionRequestEntry:
        if self.dependencies != tuple(sorted(set(self.dependencies))):
            raise ValueError("retention dependencies must be unique and sorted")
        if self.remote_evidence != tuple(sorted(set(self.remote_evidence))):
            raise ValueError("retention remote evidence must be unique and sorted")
        if any(not item.startswith("/") for item in self.dependencies):
            raise ValueError("retention dependencies must be absolute paths")
        if self.decision is RetentionDecision.RETIRE_AFTER and self.retire_after is None:
            raise ValueError("RETIRE_AFTER requires an explicit event")
        if self.decision is RetentionDecision.RETIRE_AFTER and not self.remote_evidence:
            raise ValueError("RETIRE_AFTER requires remote evidence")
        if self.decision is RetentionDecision.REMOTE_ONLY and not self.remote_evidence:
            raise ValueError("REMOTE_ONLY requires remote evidence")
        if (
            self.decision in (RetentionDecision.KEEP_LOCAL, RetentionDecision.UNKNOWN)
            and self.retire_after is not None
        ):
            raise ValueError("retained decisions cannot declare a retirement event")
        return self


class RetentionPlanRequest(_RetentionModel):
    """Operator input that never contains measured filesystem facts."""

    schema_version: Literal["operational-retention-request-v1"] = "operational-retention-request-v1"
    operation_id: UUID
    root: Path
    entries: tuple[RetentionRequestEntry, ...]

    @field_validator("root")
    @classmethod
    def require_absolute_root(cls, value: Path) -> Path:
        return _require_absolute_normalized(value, "retention request root")

    @model_validator(mode="after")
    def require_unique_sorted_entries(self) -> RetentionPlanRequest:
        paths = tuple(item.path for item in self.entries)
        if not paths or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("retention request entries must be non-empty, unique, and sorted")
        return self


class RetentionEntry(_RetentionModel):
    """One measured path whose identity is bound into the sealed plan."""

    path: NonEmptyStr
    kind: RetentionEntryKind
    retention_class: RetentionClass
    decision: RetentionDecision
    reason: NonEmptyStr
    last_consumer: NonEmptyStr
    dependencies: tuple[NonEmptyStr, ...] = ()
    remote_evidence: tuple[NonEmptyStr, ...] = ()
    retire_after: NonEmptyStr | None = None
    file_count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    exclusive_bytes: int = Field(ge=0)
    inventory_sha256: NonEmptyStr

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        return _require_relative_normalized(value, "retention entry path")

    @field_validator("inventory_sha256")
    @classmethod
    def require_inventory_digest(cls, value: str) -> str:
        return _require_digest(value, "retention inventory sha256")

    @model_validator(mode="after")
    def require_consistent_measurement(self) -> RetentionEntry:
        if self.exclusive_bytes > self.allocated_bytes:
            raise ValueError("retention exclusive bytes cannot exceed allocated bytes")
        if self.kind is RetentionEntryKind.FILE and self.file_count != 1:
            raise ValueError("retention file entry requires exactly one file")
        if self.kind is RetentionEntryKind.DIRECTORY and self.size_bytes == 0:
            if self.file_count != 0:
                raise ValueError("empty retention directory cannot declare files")
            if self.allocated_bytes != 0 or self.exclusive_bytes != 0:
                raise ValueError("empty retention directory cannot declare allocated bytes")
        if self.decision is RetentionDecision.RETIRE_AFTER and self.retire_after is None:
            raise ValueError("RETIRE_AFTER requires an explicit event")
        if (
            self.decision in (RetentionDecision.REMOTE_ONLY, RetentionDecision.RETIRE_AFTER)
            and not self.remote_evidence
        ):
            raise ValueError("executable retention requires remote evidence")
        if (
            self.decision in (RetentionDecision.KEEP_LOCAL, RetentionDecision.UNKNOWN)
            and self.retire_after is not None
        ):
            raise ValueError("retained decisions cannot declare a retirement event")
        return self


class OperationalRetentionPlan(_RetentionModel):
    """Sealed, measured inventory; a plan can only select entries it contains."""

    schema_version: Literal["operational-retention-plan-v1"] = OPERATIONAL_RETENTION_PLAN_SCHEMA
    operation_id: UUID
    root: Path
    created_at: UTCDateTime
    entries: tuple[RetentionEntry, ...]
    logical_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    exclusive_bytes: int = Field(ge=0)
    retain_bytes: int = Field(ge=0)
    retire_bytes: int = Field(ge=0)
    host_free_bytes: int = Field(ge=0)
    wsl_free_bytes: int = Field(ge=0)
    manifest_sha256: NonEmptyStr

    @field_validator("root")
    @classmethod
    def require_absolute_root(cls, value: Path) -> Path:
        return _require_absolute_normalized(value, "retention plan root")

    @field_validator("manifest_sha256")
    @classmethod
    def require_manifest_digest(cls, value: str) -> str:
        return _require_digest(value, "retention plan manifest sha256")

    @model_validator(mode="after")
    def require_complete_partition(self) -> OperationalRetentionPlan:
        paths = tuple(item.path for item in self.entries)
        if not paths or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("retention plan entries must be non-empty, unique, and sorted")
        if sum(item.size_bytes for item in self.entries) != self.logical_bytes:
            raise ValueError("retention plan logical bytes do not match entries")
        if sum(item.allocated_bytes for item in self.entries) != self.allocated_bytes:
            raise ValueError("retention plan allocated bytes do not match entries")
        if sum(item.exclusive_bytes for item in self.entries) != self.exclusive_bytes:
            raise ValueError("retention plan exclusive bytes do not match entries")
        retained = sum(
            item.size_bytes
            for item in self.entries
            if item.decision in (RetentionDecision.KEEP_LOCAL, RetentionDecision.UNKNOWN)
        )
        retired = sum(
            item.size_bytes
            for item in self.entries
            if item.decision
            in (
                RetentionDecision.REMOTE_ONLY,
                RetentionDecision.RETIRE_AFTER,
                RetentionDecision.RETIRED,
            )
        )
        if retained != self.retain_bytes or retired != self.retire_bytes:
            raise ValueError("retention plan decision totals differ")
        if retained + retired != self.logical_bytes:
            raise ValueError("retention plan decisions do not partition bytes")
        return self


class RetentionCheckpoint(_RetentionModel):
    """Durable per-plan execution cursor; completed paths never repeat."""

    schema_version: Literal["operational-retention-checkpoint-v1"] = (
        OPERATIONAL_RETENTION_CHECKPOINT_SCHEMA
    )
    operation_id: UUID
    plan_sha256: NonEmptyStr
    state: Literal["retiring", "retired"]
    completed_paths: tuple[NonEmptyStr, ...] = ()
    completed_logical_bytes: int = Field(ge=0)
    completed_allocated_bytes: int = Field(ge=0)
    completed_exclusive_bytes: int = Field(ge=0)
    started_at: UTCDateTime
    updated_at: UTCDateTime

    @field_validator("plan_sha256")
    @classmethod
    def require_plan_digest(cls, value: str) -> str:
        return _require_digest(value, "retention checkpoint plan sha256")

    @model_validator(mode="after")
    def require_monotone_paths(self) -> RetentionCheckpoint:
        if self.completed_paths != tuple(sorted(set(self.completed_paths))):
            raise ValueError("retention checkpoint paths must be unique and sorted")
        if self.state == "retired" and not self.completed_paths:
            raise ValueError("retired retention checkpoint requires completed paths")
        return self


class RetentionReceipt(_RetentionModel):
    """Terminal evidence for one fully verified local retirement."""

    schema_version: Literal["operational-retention-receipt-v1"] = (
        OPERATIONAL_RETENTION_RECEIPT_SCHEMA
    )
    operation_id: UUID
    plan_sha256: NonEmptyStr
    root: Path
    retired_paths: tuple[NonEmptyStr, ...]
    retired_logical_bytes: int = Field(ge=0)
    retired_allocated_bytes: int = Field(ge=0)
    exclusive_bytes_freed: int = Field(ge=0)
    remote_evidence: tuple[NonEmptyStr, ...]
    host_free_before: int = Field(ge=0)
    host_free_after: int = Field(ge=0)
    wsl_free_before: int = Field(ge=0)
    wsl_free_after: int = Field(ge=0)
    idempotent_replay: bool
    completed_at: UTCDateTime

    @field_validator("plan_sha256")
    @classmethod
    def require_plan_digest(cls, value: str) -> str:
        return _require_digest(value, "retention receipt plan sha256")

    @field_validator("root")
    @classmethod
    def require_absolute_root(cls, value: Path) -> Path:
        return _require_absolute_normalized(value, "retention receipt root")

    @model_validator(mode="after")
    def require_sorted_paths_and_evidence(self) -> RetentionReceipt:
        if self.retired_paths != tuple(sorted(set(self.retired_paths))):
            raise ValueError("retention receipt paths must be unique and sorted")
        if self.remote_evidence != tuple(sorted(set(self.remote_evidence))):
            raise ValueError("retention receipt evidence must be unique and sorted")
        return self


class RemoteRetentionArtifact(_RetentionModel):
    """One immutable remote control object with verified readback."""

    object_name: NonEmptyStr
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr

    @field_validator("sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        return _require_digest(value, "remote retention artifact sha256")


class RetentionRemoteReceipt(_RetentionModel):
    """Readback-verified publication of the plan and its control inventory."""

    schema_version: Literal["operational-retention-remote-receipt-v1"] = (
        OPERATIONAL_RETENTION_REMOTE_RECEIPT_SCHEMA
    )
    operation_id: UUID
    plan_sha256: NonEmptyStr
    remote_prefix: NonEmptyStr
    artifacts: tuple[RemoteRetentionArtifact, ...]
    readback_verified: bool
    completed_at: UTCDateTime

    @field_validator("plan_sha256")
    @classmethod
    def require_plan_digest(cls, value: str) -> str:
        return _require_digest(value, "remote retention receipt plan sha256")

    @model_validator(mode="after")
    def require_verified_sorted_artifacts(self) -> RetentionRemoteReceipt:
        names = tuple(item.object_name for item in self.artifacts)
        if not self.readback_verified or not names:
            raise ValueError("remote retention receipt requires verified artifacts")
        if names != tuple(sorted(set(names))):
            raise ValueError("remote retention artifacts must be unique and sorted")
        return self


__all__ = [
    "OPERATIONAL_RETENTION_CHECKPOINT_SCHEMA",
    "OPERATIONAL_RETENTION_PLAN_SCHEMA",
    "OPERATIONAL_RETENTION_RECEIPT_SCHEMA",
    "OPERATIONAL_RETENTION_REMOTE_RECEIPT_SCHEMA",
    "OperationalRetentionPlan",
    "RemoteRetentionArtifact",
    "RetentionCheckpoint",
    "RetentionClass",
    "RetentionDecision",
    "RetentionEntry",
    "RetentionEntryKind",
    "RetentionPlanRequest",
    "RetentionReceipt",
    "RetentionRemoteReceipt",
    "RetentionRequestEntry",
]
