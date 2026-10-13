"""Persistent contracts for the v1-to-v2 source snapshot transition."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class WorkspaceV2MigrationPhase(StrEnum):
    """Durable phases for the one-way source transition."""

    PREPARED = "PREPARED"
    VERIFYING = "VERIFYING"
    APPLYING = "APPLYING"
    COMPLETE = "COMPLETE"


class WorkspaceV2MigrationWorkPhase(StrEnum):
    """Bounded durable work phases shared by copy, transport and recovery."""

    COPY = "copy"
    IMPORT = "import"
    VERIFY = "verify"
    BACKUP = "backup"
    RESTORE = "restore"


class WorkspaceV1MigrationSourceSnapshot(ContractModel):
    """Physical, hash-bound v1 source copy used as the sole migration source."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v1-migration-source-snapshot-v1"] = (
        "workspace-v1-migration-source-snapshot-v1"
    )
    snapshot_id: UUID
    source_workspace_id: UUID
    source_created_at: UTCDateTime
    source_fingerprint: NonEmptyStr
    snapshot_root: Path
    created_at: UTCDateTime
    physical_file_count: int = Field(ge=1)
    physical_total_bytes: int = Field(ge=0)
    physical_inventory_sha256: NonEmptyStr

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceV2SourceTransitionState(ContractModel):
    """Append-only provenance joining historic source A to snapshot source B."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-migration-state-v2"] = "workspace-v2-migration-state-v2"
    phase: WorkspaceV2MigrationPhase
    source_workspace_id: UUID
    source_created_at: UTCDateTime
    historic_source_fingerprint: NonEmptyStr
    historic_checkpoint_sha256: NonEmptyStr
    snapshot_id: UUID
    snapshot_source_fingerprint: NonEmptyStr
    destination_workspace_id: UUID
    confirmed_equivalent_count: int = Field(ge=0)
    unconfirmed_equivalent_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    conflicting_count: int = Field(ge=0)
    updated_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceV2MigrationWorkCheckpoint(ContractModel):
    """Immutable, hash-bound evidence for one bounded migration work unit."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-migration-work-checkpoint-v1"] = (
        "workspace-v2-migration-work-checkpoint-v1"
    )
    operation_id: UUID
    phase: WorkspaceV2MigrationWorkPhase
    snapshot_id: UUID
    source_fingerprint: NonEmptyStr
    destination_workspace_id: UUID
    source_root: Path
    destination_root: Path
    staging_root: Path | None = None
    policy_version: NonEmptyStr
    reader_policy: NonEmptyStr
    verifier_policy: NonEmptyStr
    tree_fingerprint: NonEmptyStr
    environment_fingerprint: NonEmptyStr
    cursor: NonEmptyStr | None = None
    ordinal: int = Field(ge=0)
    relative_path: NonEmptyStr | None = None
    offset_bytes: int = Field(ge=0)
    page_limit: int = Field(ge=1, le=256)
    confirmed_count: int = Field(ge=0)
    confirmed_bytes: int = Field(ge=0)
    accumulated_digest: NonEmptyStr
    input_references: tuple[NonEmptyStr, ...] = ()
    updated_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceV2MigrationVerificationProof(ContractModel):
    """Immutable evidence that one complete bounded family comparison closed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-migration-verification-proof-v1"] = (
        "workspace-v2-migration-verification-proof-v1"
    )
    operation_id: UUID
    snapshot_id: UUID
    source_workspace_id: UUID
    destination_workspace_id: UUID
    source_root: Path
    destination_root: Path
    source_fingerprint: NonEmptyStr
    inventory_digest: NonEmptyStr
    family: NonEmptyStr
    subphase: NonEmptyStr
    reader_policy: NonEmptyStr
    verifier_policy: NonEmptyStr
    tree_fingerprint: NonEmptyStr
    environment_fingerprint: NonEmptyStr
    coverage_count: int = Field(ge=0)
    source_digest: NonEmptyStr
    destination_digest: NonEmptyStr
    ordered_unit_proof_root: NonEmptyStr
    physical_binding: NonEmptyStr
    verified_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


__all__ = [
    "WorkspaceV1MigrationSourceSnapshot",
    "WorkspaceV2MigrationPhase",
    "WorkspaceV2MigrationWorkCheckpoint",
    "WorkspaceV2MigrationWorkPhase",
    "WorkspaceV2MigrationVerificationProof",
    "WorkspaceV2SourceTransitionState",
]
