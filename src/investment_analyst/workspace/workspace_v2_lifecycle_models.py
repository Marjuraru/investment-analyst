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


class OperationalStateFamily(StrEnum):
    """Closed operational families eligible for byte-exact cutover transfer."""

    DAILY_RUN = "daily_run"
    DAILY_SCHEDULE = "daily_schedule"
    MULTI_ASSET_SCHEDULE = "multi_asset_schedule"
    ANALYTICAL_SCREENING = "analytical_screening"
    OPERATIONAL_ALERTS = "operational_alerts"
    CANDIDATE_OUTBOX = "candidate_outbox"
    CAZATIBURONES_OUTBOX = "cazatiburones_outbox"
    INSTITUTIONAL_CYCLE = "institutional_cycle"
    INSTITUTIONAL_HISTORY = "institutional_history"
    MANUAL_OPERATIONS = "manual_operations"
    STORAGE_TELEMETRY = "storage_telemetry"
    ACTIVITY_EVENTS = "activity_events"
    INSTITUTIONAL_EVENTS = "institutional_events"
    ASSET_PREFERENCES = "asset_preferences"


class OperationalStateArtifactRole(StrEnum):
    """Physical artifact role; this never changes operational semantics."""

    DOCUMENT = "document"
    JOURNAL = "journal"
    EVENT = "event"
    TELEMETRY = "telemetry"


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


class OperationalStateArtifact(_LifecycleModel):
    """One bounded, sealed operational file; absence is represented by no artifact."""

    family: OperationalStateFamily
    role: OperationalStateArtifactRole
    path: NonEmptyStr
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("operational artifact path must be normalized and relative")
        return value

    @field_validator("sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("operational artifact sha256 is invalid")
        return value


class OperationalStateSeal(_LifecycleModel):
    """Companion seal for state excluded from the financial source fingerprint."""

    schema_version: Literal["workspace-v2-operational-state-seal-v1"] = (
        "workspace-v2-operational-state-seal-v1"
    )
    policy: Literal["default-operational-state-transfer-v1"] = (
        "default-operational-state-transfer-v1"
    )
    operation_id: UUID
    source: FrozenSourceCut
    destination_root: Path
    destination_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    sealed_at: UTCDateTime
    present_families: tuple[OperationalStateFamily, ...]
    absent_families: tuple[OperationalStateFamily, ...]

    @model_validator(mode="after")
    def require_partition(self) -> OperationalStateSeal:
        present = set(self.present_families)
        absent = set(self.absent_families)
        if present & absent or present | absent != set(OperationalStateFamily):
            raise ValueError("operational state families must form a complete partition")
        return self


class OperationalStateTransferReceipt(_LifecycleModel):
    """Durable terminal evidence for an operational-state transfer epoch."""

    schema_version: Literal["workspace-v2-operational-state-transfer-receipt-v1"] = (
        "workspace-v2-operational-state-transfer-receipt-v1"
    )
    seal: OperationalStateSeal
    source_inventory_sha256: NonEmptyStr
    destination_inventory_sha256: NonEmptyStr
    artifact_count: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    completed_families: tuple[OperationalStateFamily, ...]
    completed_at: UTCDateTime

    @model_validator(mode="after")
    def require_terminal_inventory(self) -> OperationalStateTransferReceipt:
        if self.source_inventory_sha256 != self.seal.inventory_sha256:
            raise ValueError("operational receipt source inventory differs from seal")
        if self.destination_inventory_sha256 != self.seal.inventory_sha256:
            raise ValueError("operational receipt destination inventory differs from seal")
        if tuple(sorted(self.completed_families)) != tuple(sorted(self.seal.present_families)):
            raise ValueError("operational receipt families differ from seal")
        return self


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


class ProvenCopyRetirementReceipt(_LifecycleModel):
    """Terminal proof for retiring an inactive v2 copy covered by a sealed recovery."""

    schema_version: Literal["workspace-v2-proven-copy-retirement-receipt-v1"] = (
        "workspace-v2-proven-copy-retirement-receipt-v1"
    )
    operation_id: UUID
    source_root: Path
    retained_root: Path
    backup_root: Path
    backup_id: UUID
    inventory_sha256: NonEmptyStr
    remote_manifest_sha256: NonEmptyStr
    workspace_id: UUID
    files_confirmed: int = Field(ge=0)
    bytes_confirmed: int = Field(ge=0)
    completed_at: UTCDateTime

    @field_validator("inventory_sha256", "remote_manifest_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("proven copy retirement digest is invalid")
        return value


class SealedDirectoryRetirementCheckpoint(_LifecycleModel):
    """Durable retirement cursor for a sealed non-workspace directory."""

    schema_version: Literal["workspace-v2-sealed-directory-retirement-checkpoint-v1"] = (
        "workspace-v2-sealed-directory-retirement-checkpoint-v1"
    )
    operation_id: UUID
    phase: Literal["retiring", "retired"]
    source_root: Path
    retiring_root: Path
    inventory_sha256: NonEmptyStr
    remote_manifest_sha256: NonEmptyStr
    files_confirmed: int = Field(ge=0)
    bytes_confirmed: int = Field(ge=0)

    @field_validator("inventory_sha256", "remote_manifest_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sealed directory retirement digest is invalid")
        return value


class SealedDirectoryRetirementReceipt(_LifecycleModel):
    """Terminal evidence for a remotely verified non-workspace directory retirement."""

    schema_version: Literal["workspace-v2-sealed-directory-retirement-receipt-v1"] = (
        "workspace-v2-sealed-directory-retirement-receipt-v1"
    )
    checkpoint: SealedDirectoryRetirementCheckpoint
    completed_at: UTCDateTime


class PreparatorySlotArtifact(_LifecycleModel):
    """One exact prerequisite of the narrowly authorized APPLYING W retirement."""

    kind: Literal[
        "delta_manifest",
        "files_inventory",
        "remote_upload",
        "remote_parent_restore",
        "remote_parent_manifest",
        "remote_parent_index",
        "remote_locator",
        "restore_receipt",
        "complete_coverage",
        "compatible_readback",
        "retained_certificate",
        "link_witness",
        "independent_cache_binding",
        "operation_receipt",
    ]
    path: Path
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("preparatory evidence path must be absolute")
        return value


class OwnedPayloadRole(StrEnum):
    """Closed payload roles authorized by the DATA-CHASSIS-45 cleanup matrix."""

    DELTA_STAGING_OBJECTS = "DELTA_STAGING_OBJECTS"
    INDEPENDENT_READ_CACHE = "INDEPENDENT_READ_CACHE"
    TRANSFER_SCRATCH = "TRANSFER_SCRATCH"
    REGENERABLE_LOCAL_CACHE = "REGENERABLE_LOCAL_CACHE"


class OwnedPayloadRetirementInput(_LifecycleModel):
    """Exact retirement identity; assertions never substitute for verified receipts."""

    operation_id: UUID
    role: OwnedPayloadRole
    source_root: Path
    retained_controls_root: Path
    consuming_generation: UUID | None
    releasing_stage: Literal["E0", "E5"]
    evidence: tuple[PreparatorySlotArtifact, ...] = ()
    no_users: Literal[True]
    no_unique_data: Literal[True]

    @model_validator(mode="after")
    def require_disjoint_roots(self) -> OwnedPayloadRetirementInput:
        roots = (self.source_root, self.retained_controls_root)
        if any(not p.is_absolute() or ".." in p.parts for p in roots) or (
            roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0])
        ):
            raise ValueError("owned payload roots must be absolute and disjoint")
        return self


class OwnedPayloadRetirementReceipt(_LifecycleModel):
    """Durable inventory and sealed-directory cursor for one exact owned payload."""

    schema_version: Literal["workspace-v2-owned-payload-retirement-v1"] = (
        "workspace-v2-owned-payload-retirement-v1"
    )
    request: OwnedPayloadRetirementInput
    inventory: PreparatorySlotArtifact
    checkpoint: SealedDirectoryRetirementCheckpoint
    allocated_bytes: int = Field(ge=0)
    started_at: UTCDateTime
    completed_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def require_terminal_time(self) -> OwnedPayloadRetirementReceipt:
        if (self.checkpoint.phase == "retired") != (self.completed_at is not None):
            raise ValueError("owned payload terminal phase and timestamp differ")
        return self


class PreparatorySlotRetirementReceipt(_LifecycleModel):
    """Retiring W is independent from, and never authorizes, cut activation."""

    schema_version: Literal["workspace-v2-preparatory-slot-retirement-receipt-v1"] = (
        "workspace-v2-preparatory-slot-retirement-receipt-v1"
    )
    operation_id: UUID
    phase: Literal["retiring", "retired"]
    source_root: Path
    retained_root: Path
    workspace_id: UUID
    retained_generation_id: UUID
    recovery_epoch: UUID
    historical_state: Literal["APPLYING"] = "APPLYING"
    evidence: tuple[PreparatorySlotArtifact, ...] = Field(min_length=12, max_length=12)
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files_confirmed: int = Field(ge=0)
    bytes_confirmed: int = Field(ge=0)
    no_unique_content: Literal[True] = True
    writer_closed: Literal[True] = True
    unlink_witness: PreparatorySlotArtifact | None = None
    verified_at: UTCDateTime
    completed_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def exact_preparatory_retirement(self) -> PreparatorySlotRetirementReceipt:
        if (
            not self.source_root.is_absolute()
            or not self.retained_root.is_absolute()
            or self.source_root == self.retained_root
            or self.source_root.is_relative_to(self.retained_root)
            or self.retained_root.is_relative_to(self.source_root)
            or len({item.kind for item in self.evidence}) != 12
            or any(item.path.is_relative_to(self.source_root) for item in self.evidence)
        ):
            raise ValueError("preparatory retirement roots or evidence differ")
        if self.phase == "retired" and (self.unlink_witness is None or self.completed_at is None):
            raise ValueError("retired preparatory slot requires completed unlink evidence")
        if self.phase == "retiring" and (
            self.unlink_witness is not None or self.completed_at is not None
        ):
            raise ValueError("preparatory proposal cannot claim terminal evidence")
        if self.unlink_witness is not None and (
            self.unlink_witness.kind != "link_witness"
            or self.unlink_witness.path.is_relative_to(self.source_root)
        ):
            raise ValueError("unlink evidence must survive preparatory retirement")
        return self


class WorkspaceV2RetirementProof(_LifecycleModel):
    """Physical recovery evidence for the exact inactive root being retired."""

    schema_version: Literal["workspace-v2-retirement-proof-v1"] = "workspace-v2-retirement-proof-v1"
    operation_id: UUID
    role: Literal["physical_delta_recovery_slot"]
    source_root: Path
    source_workspace_id: UUID
    retained_root: Path
    recovery_epoch: UUID
    recovery_manifest_sha256: NonEmptyStr
    recovery_receipt_sha256: NonEmptyStr
    inventory_sha256: NonEmptyStr
    files_confirmed: int = Field(ge=0)
    bytes_confirmed: int = Field(ge=0)
    writer_closed: Literal[True] = True
    verified_at: UTCDateTime

    @model_validator(mode="after")
    def require_closed_roots(self) -> WorkspaceV2RetirementProof:
        if (
            not self.source_root.is_absolute()
            or not self.retained_root.is_absolute()
            or self.source_root == self.retained_root
            or self.source_root in self.retained_root.parents
            or self.retained_root in self.source_root.parents
        ):
            raise ValueError("retirement roots must be absolute and disjoint")
        for digest in (
            self.recovery_manifest_sha256,
            self.recovery_receipt_sha256,
            self.inventory_sha256,
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("retirement evidence digest is invalid")
        return self


class BackupDirectoryRetirementProof(_LifecycleModel):
    """Sealed physical directory identity; it is not a workspace or a fabricated UUID."""

    schema_version: Literal["workspace-v2-retirement-proof-v1"] = "workspace-v2-retirement-proof-v1"
    operation_id: UUID
    role: Literal["packed_backup", "restore_staging"] = "packed_backup"
    source_root: Path
    retained_controls: Path
    parent_index: Path
    parent_index_sha256: NonEmptyStr
    remote_manifest_sha256: NonEmptyStr
    recovery_receipt_sha256: NonEmptyStr
    inventory_sha256: NonEmptyStr
    files_confirmed: int = Field(ge=1)
    bytes_confirmed: int = Field(ge=0)
    writer_closed: Literal[True] = True
    verified_at: UTCDateTime

    @model_validator(mode="after")
    def require_directory_binding(self) -> BackupDirectoryRetirementProof:
        for path in (self.source_root, self.retained_controls, self.parent_index):
            if not path.is_absolute():
                raise ValueError("backup retirement paths must be absolute")
        if self.retained_controls.is_relative_to(
            self.source_root
        ) or self.parent_index.is_relative_to(self.source_root):
            raise ValueError("backup retirement dependencies must survive outside the root")
        for digest in (
            self.parent_index_sha256,
            self.remote_manifest_sha256,
            self.recovery_receipt_sha256,
            self.inventory_sha256,
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("backup retirement digest is invalid")
        return self
