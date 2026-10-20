"""Persistent contracts for the v1-to-v2 source snapshot transition."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.workspace.workspace_v2_lifecycle_models import FrozenSourceCut


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


class HistoricalSealSnapshot(ContractModel):
    """Exact legacy seal metadata retained by a transition companion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_fingerprint: NonEmptyStr
    metric_count: int = Field(ge=0)
    metric_digest: NonEmptyStr
    diagnostic_count: int = Field(ge=0)
    diagnostic_digest: NonEmptyStr
    sealed_at: UTCDateTime


class HistoricalSnapshotSource(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["snapshot"] = "snapshot"
    snapshot: WorkspaceV1MigrationSourceSnapshot


class HistoricalFrozenSource(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["frozen"] = "frozen"
    cut: FrozenSourceCut


class HistoricalTransition(ContractModel):
    """Durable authorization for one inactive, manifest-bound historical append."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["workspace-v2-historical-transition-v1"] = (
        "workspace-v2-historical-transition-v1"
    )
    operation_id: UUID
    phase: Literal["PREPARED", "APPLYING", "VERIFIED", "COMPLETE"]
    source: Annotated[
        HistoricalSnapshotSource | HistoricalFrozenSource, Field(discriminator="kind")
    ]
    source_workspace_id: UUID
    source_created_at: UTCDateTime
    source_root: Path
    source_fingerprint: NonEmptyStr
    destination_root: Path
    destination_workspace_id: UUID
    parent_seal: HistoricalSealSnapshot
    parent_seal_sha256: NonEmptyStr
    parent_membership_path: Path
    parent_membership_sha256: NonEmptyStr
    policy: Literal["historical-append-preserve-v1"] = "historical-append-preserve-v1"
    page_limit: int = Field(ge=1, le=256)
    destination_inactive: Literal[True]
    writer_closed: Literal[True]
    tree_fingerprint: NonEmptyStr
    environment_fingerprint: NonEmptyStr
    prepared_at: UTCDateTime
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_bindings(self) -> HistoricalTransition:
        if isinstance(self.source, HistoricalFrozenSource):
            cut = self.source.cut
            binding = (
                cut.operation_id,
                cut.source_root,
                cut.workspace_id,
                cut.created_at,
                cut.source_fingerprint,
            )
        else:
            snapshot = self.source.snapshot
            binding = (
                snapshot.snapshot_id,
                snapshot.snapshot_root,
                snapshot.source_workspace_id,
                snapshot.source_created_at,
                snapshot.source_fingerprint,
            )
        if binding != (
            self.operation_id,
            self.source_root,
            self.source_workspace_id,
            self.source_created_at,
            self.source_fingerprint,
        ):
            raise ValueError("historical transition source binding differs")
        if (
            not self.source_root.is_absolute()
            or not self.destination_root.is_absolute()
            or self.source_root == self.destination_root
            or self.parent_membership_path.parent != self.destination_root / "state"
        ):
            raise ValueError("historical transition roots are invalid")
        for digest in (
            self.source_fingerprint,
            self.parent_seal_sha256,
            self.parent_membership_sha256,
            self.tree_fingerprint,
            self.environment_fingerprint,
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("historical transition digest is invalid")
        return self


class HistoricalTransitionReceipt(ContractModel):
    """Terminal evidence retaining the complete authorization and parent binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["workspace-v2-historical-transition-receipt-v1"] = (
        "workspace-v2-historical-transition-receipt-v1"
    )
    transition: HistoricalTransition
    parent_preservation_sha256: NonEmptyStr
    final_seal: HistoricalSealSnapshot
    final_seal_sha256: NonEmptyStr
    final_inventory_digest: NonEmptyStr
    metric_created_count: int = Field(ge=0)
    metric_reused_count: int = Field(ge=0)
    diagnostic_created_count: int = Field(ge=0)
    diagnostic_reused_count: int = Field(ge=0)
    state: Literal["COMPLETE"] = "COMPLETE"
    completed_at: UTCDateTime

    @model_validator(mode="after")
    def validate_terminal_binding(self) -> HistoricalTransitionReceipt:
        seal_digest = hashlib.sha256(
            json.dumps(
                self.final_seal.model_dump(mode="json", round_trip=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        inventory_digest = hashlib.sha256(
            json.dumps(
                (
                    self.final_seal.metric_count,
                    self.final_seal.metric_digest,
                    self.final_seal.diagnostic_count,
                    self.final_seal.diagnostic_digest,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        parent = self.transition.parent_seal
        if (
            self.transition.phase != "COMPLETE"
            or self.final_seal.source_fingerprint != self.transition.source_fingerprint
            or self.parent_preservation_sha256 != self.transition.parent_membership_sha256
            or self.final_seal_sha256 != seal_digest
            or self.final_inventory_digest != inventory_digest
            or self.metric_created_count != self.final_seal.metric_count - parent.metric_count
            or self.metric_reused_count != parent.metric_count
            or self.diagnostic_created_count
            != self.final_seal.diagnostic_count - parent.diagnostic_count
            or self.diagnostic_reused_count != parent.diagnostic_count
        ):
            raise ValueError("historical receipt terminal bindings differ")
        return self


class ReconciliationFileReference(ContractModel):
    """An exact physical artifact; its hash is verified by the reconciliation service."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    role: Literal["control", "database", "wal", "inventory", "code", "resource"]

    @model_validator(mode="after")
    def require_absolute_artifact(self) -> ReconciliationFileReference:
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError("reconciliation artifact must have an absolute normalized path")
        return self


class RecoveredProgressInventory(ContractModel):
    """Validated W, never an intermediate historical seal or a completed F0."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    raw_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    metric_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    metric_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    delta_membership: ReconciliationFileReference
    relationships: ReconciliationFileReference
    verified_at: UTCDateTime

    @model_validator(mode="after")
    def require_inventory_proofs(self) -> RecoveredProgressInventory:
        if self.delta_membership.role != "inventory" or self.relationships.role != "control":
            raise ValueError("recovered progress proof roles differ")
        return self


class ReconciliationCodeFile(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    archive: Path
    source: Path
    size_bytes: int = Field(ge=0, validation_alias="bytes")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconciliationCodeArchive(ContractModel):
    """Provenance only: archived bytes never supply implementation authority."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    files: tuple[ReconciliationCodeFile, ...] = Field(min_length=4, max_length=4)
    tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_operation_id: UUID
    reconciliation_operation_id: UUID
    purpose: NonEmptyStr
    recorded_at: UTCDateTime


class SuccessorCoverageProof(ContractModel):
    """Complete identity/content comparison W subset F1, bound to the reserved cut."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    successor: FrozenSourceCut
    baseline_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_metric_count: int = Field(ge=0)
    covered_diagnostic_count: int = Field(ge=0)
    comparison_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: UTCDateTime


class ProgressReconciliation(ContractModel):
    """Exclusive bridge from preserved APPLYING progress to exactly one successor.

    PREPARED includes only physical recovery evidence. BASELINE_VERIFIED requires
    fresh semantic W evidence; a reservation is not coverage. SUCCESSOR_BOUND
    requires the complete W subset F1 proof. The predecessor remains untouched.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-progress-reconciliation-v1"] = (
        "workspace-v2-progress-reconciliation-v1"
    )
    operation_id: UUID
    phase: Literal["PREPARED", "BASELINE_VERIFIED", "SUCCESSOR_BOUND"] = "PREPARED"
    predecessor_operation_id: UUID
    predecessor: ReconciliationFileReference
    predecessor_proofs: tuple[ReconciliationFileReference, ...] = Field(min_length=1)
    parent_membership: ReconciliationFileReference
    parent_seal: HistoricalSealSnapshot
    source_workspace_id: UUID
    source_created_at: UTCDateTime
    source_root: Path
    destination_root: Path
    destination_workspace_id: UUID
    database_before: ReconciliationFileReference
    wal_before: ReconciliationFileReference | None
    database_after: ReconciliationFileReference | None = None
    wal_after: ReconciliationFileReference | None = None
    recovered_at: UTCDateTime | None = None
    recovery_attempts: tuple[ReconciliationFileReference, ...] = ()
    predecessor_code: ReconciliationFileReference
    predecessor_tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_tree_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    current_environment_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verifier_policy: Literal["shared-reference-verifier-v1"] = "shared-reference-verifier-v1"
    resource_receipt: ReconciliationFileReference
    page_limit: int = Field(default=256, ge=1, le=256)
    exclusive: Literal[True] = True
    inventory: RecoveredProgressInventory | None = None
    reserved_successor_id: UUID | None = None
    reserved_successor: FrozenSourceCut | None = None
    coverage: SuccessorCoverageProof | None = None
    prepared_at: UTCDateTime
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def require_exclusive_bridge(self) -> ProgressReconciliation:
        if self.operation_id == self.predecessor_operation_id:
            raise ValueError("reconciliation cannot reuse predecessor operation")
        if not self.destination_root.is_absolute() or ".." in self.destination_root.parts:
            raise ValueError("reconciliation destination must be absolute")
        if (
            not self.source_root.is_absolute()
            or ".." in self.source_root.parts
            or self.source_root == self.destination_root
            or self.source_root.is_relative_to(self.destination_root)
            or self.destination_root.is_relative_to(self.source_root)
        ):
            raise ValueError("reconciliation source and destination must be disjoint")
        if (
            self.predecessor.role != "control"
            or self.parent_membership.role != "inventory"
            or self.predecessor_code.role != "code"
            or self.resource_receipt.role != "resource"
            or self.database_before.role != "database"
            or (self.wal_before is not None and self.wal_before.role != "wal")
        ):
            raise ValueError("reconciliation artifact roles differ")
        expected_db = self.destination_root / "storage/v2/index.duckdb"
        expected_wal = self.destination_root / "storage/v2/index.duckdb.wal"
        for reference in (self.database_before, self.database_after):
            if reference is not None and (
                reference.path != expected_db or reference.role != "database"
            ):
                raise ValueError("reconciliation database binding differs")
        for reference in (self.wal_before, self.wal_after):
            if reference is not None and (
                reference.path != expected_wal or reference.role != "wal"
            ):
                raise ValueError("reconciliation WAL binding differs")
        if (self.database_after is None) != (self.recovered_at is None):
            raise ValueError("recovery evidence is incomplete")
        if self.phase != "PREPARED" and (
            self.inventory is None
            or self.database_after is None
            or self.current_tree_fingerprint is None
            or self.current_environment_fingerprint is None
        ):
            raise ValueError("baseline requires recovered, semantically verified progress")
        if self.phase == "PREPARED" and (
            self.inventory is not None
            or self.reserved_successor_id is not None
            or self.reserved_successor is not None
            or self.coverage is not None
        ):
            raise ValueError("prepared progress cannot reserve or certify a successor")
        if (self.reserved_successor_id is None) != (self.reserved_successor is None):
            raise ValueError("successor reservation bindings are incomplete")
        successor = self.reserved_successor
        if successor is not None and (
            successor.operation_id != self.reserved_successor_id
            or successor.operation_id in {self.operation_id, self.predecessor_operation_id}
            or successor.workspace_id != self.source_workspace_id
            or successor.created_at != self.source_created_at
            or successor.source_root != self.source_root
        ):
            raise ValueError("successor reservation identity differs")
        if self.phase == "BASELINE_VERIFIED" and self.coverage is not None:
            raise ValueError("coverage must be published atomically with successor-bound state")
        if self.phase == "SUCCESSOR_BOUND":
            coverage, inventory = self.coverage, self.inventory
            if (
                coverage is None
                or inventory is None
                or successor is None
                or coverage.successor != successor
                or coverage.baseline_dataset_sha256 != inventory.dataset_sha256
                or coverage.covered_metric_count != inventory.metric_count
                or coverage.covered_diagnostic_count != inventory.diagnostic_count
                or coverage.verifier_tree_fingerprint != self.current_tree_fingerprint
            ):
                raise ValueError("successor requires complete baseline coverage")
        if self.inventory is not None and (
            self.inventory.metric_count < self.parent_seal.metric_count
            or self.inventory.diagnostic_count < self.parent_seal.diagnostic_count
        ):
            raise ValueError("recovered inventory cannot lose parent members")
        return self


class ProgressRecoveryAttempt(ContractModel):
    """Typed reading of the existing durable progress-recovery attempt document."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-progress-recovery-attempt-v1"] = (
        "workspace-v2-progress-recovery-attempt-v1"
    )
    operation_id: UUID
    admission: ReconciliationFileReference
    database: ReconciliationFileReference
    wal: ReconciliationFileReference | None
    recorded_at: UTCDateTime


class ProgressReconciliationReceipt(ContractModel):
    """A stage receipt, with no claim that F0 became a completed source cut."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-progress-reconciliation-receipt-v1"] = (
        "workspace-v2-progress-reconciliation-receipt-v1"
    )
    reconciliation: ProgressReconciliation
    certificate: ReconciliationFileReference
    stage: Literal["recovered", "baseline_verified", "successor_bound"]
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def require_stage_evidence(self) -> ProgressReconciliationReceipt:
        if self.certificate.role != "control" or self.reconciliation.recovered_at is None:
            raise ValueError("reconciliation receipt lacks physical recovery evidence")
        expected = {
            "recovered": "PREPARED",
            "baseline_verified": "BASELINE_VERIFIED",
            "successor_bound": "SUCCESSOR_BOUND",
        }[self.stage]
        if self.reconciliation.phase != expected:
            raise ValueError("reconciliation receipt stage differs from certificate")
        return self


class ProgressRelationshipProof(ContractModel):
    """Full relationship verification of one immutable recovered physical image.

    This proves references/PIT/cycles, not canonical financial content or W subset F1.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-progress-relationships-v1"] = (
        "workspace-v2-progress-relationships-v1"
    )
    operation_id: UUID
    destination_root: Path
    destination_workspace_id: UUID
    reconciliation: ReconciliationFileReference
    database: ReconciliationFileReference
    wal: ReconciliationFileReference | None
    parent_membership: ReconciliationFileReference
    verifier_policy: Literal["shared-reference-verifier-v1"] = "shared-reference-verifier-v1"
    tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_receipt: ReconciliationFileReference
    raw_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    metric_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    verified_at: UTCDateTime

    @model_validator(mode="after")
    def require_physical_generation(self) -> ProgressRelationshipProof:
        if (
            self.database.path != self.destination_root / "storage/v2/index.duckdb"
            or self.database.role != "database"
            or self.reconciliation.role != "control"
            or self.parent_membership.role != "inventory"
            or self.resource_receipt.role != "resource"
            or (
                self.wal is not None
                and (
                    self.wal.role != "wal"
                    or self.wal.path != self.destination_root / "storage/v2/index.duckdb.wal"
                )
            )
        ):
            raise ValueError("relationship proof generation bindings differ")
        return self


class ProgressRelationshipProfile(ContractModel):
    """Sizing evidence only; never accepted as a relationship or baseline proof."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-progress-relationship-profile-v1"] = (
        "workspace-v2-progress-relationship-profile-v1"
    )
    profile_only: Literal[True] = True
    operation_id: UUID
    reconciliation: ReconciliationFileReference
    database: ReconciliationFileReference
    resource_receipt: ReconciliationFileReference
    tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_equal_edges: int = Field(ge=0)
    sequence_equal_edges: int = Field(ge=0)
    segment_equal_edges: int = Field(ge=0)
    largest_metric_time_bucket: int = Field(ge=0)
    metric_dependency_sequences: int = Field(ge=0)
    metric_dependency_segments: int = Field(ge=0)
    metric_dependency_links: int = Field(ge=0)
    sequence_rows: int = Field(ge=0)
    segment_rows: int = Field(ge=0)
    recorded_at: UTCDateTime


class CutOwner(ContractModel):
    """Actual process identity; absent historical fields remain explicitly absent."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    source_session: UUID
    pid: int | None = Field(default=None, gt=0)
    start_ticks: int | None = Field(default=None, ge=0)
    boot_id: UUID | None = None
    cgroup: Path | None = None

    @model_validator(mode="after")
    def coherent_identity(self) -> CutOwner:
        values = (self.pid, self.start_ticks, self.boot_id, self.cgroup)
        if any(v is None for v in values) and not all(v is None for v in values):
            raise ValueError("cut owner identity must be complete or historically absent")
        if self.cgroup is not None and not self.cgroup.is_absolute():
            raise ValueError("cut owner cgroup must be absolute")
        return self


class ReconciliationGenerationReference(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_type: Literal[
        "workspace-v2-progress-reconciliation-v1", "workspace-v2-progress-reconciliation-v2"
    ]
    artifact: ReconciliationFileReference


class CutInvalidation(ContractModel):
    """Revoke operational authority without changing financial history or receipts."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-cut-invalidation-v1"] = "workspace-v2-cut-invalidation-v1"
    event_id: UUID
    generation_id: UUID
    attempt_id: UUID
    previous_event: ReconciliationFileReference | None = None
    expected_head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    certificate: ReconciliationGenerationReference
    reservation: ReconciliationFileReference
    owner: CutOwner
    source_root: Path
    source_workspace_id: UUID
    destination_root: Path
    destination_workspace_id: UUID
    database: ReconciliationFileReference
    wal: ReconciliationFileReference | None
    write_generation: ReconciliationFileReference
    historical_seal: ReconciliationFileReference | None
    historical_receipt: ReconciliationFileReference | None
    reason: Literal["SESSION_ENDED", "MAINTENANCE_ABORTED", "SOURCE_REOPENED", "EVIDENCE_INVALID"]
    owner_absence: ReconciliationFileReference
    exclusivity: ReconciliationFileReference
    no_activation: ReconciliationFileReference
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def coherent_invalidation(self) -> CutInvalidation:
        if not self.source_root.is_absolute() or not self.destination_root.is_absolute():
            raise ValueError("invalidation roots must be absolute")
        if self.source_root == self.destination_root:
            raise ValueError("invalidation roots must differ")
        if self.database.path != self.destination_root / "storage/v2/index.duckdb":
            raise ValueError("invalidation database binding differs")
        if self.database.role != "database" or (
            self.wal is not None
            and (
                self.wal.role != "wal"
                or self.wal.path != self.destination_root / "storage/v2/index.duckdb.wal"
            )
        ):
            raise ValueError("invalidation database/WAL roles differ")
        return self


class ProgressReconciliationV2(ContractModel):
    """Immutable generation certificate; only the separately bound head is mutable."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-progress-reconciliation-v2"] = (
        "workspace-v2-progress-reconciliation-v2"
    )
    generation_id: UUID
    ordinal: int = Field(ge=1)
    phase: Literal["PREPARED", "BASELINE_VERIFIED", "SUCCESSOR_BOUND"] = "PREPARED"
    previous_generation: ReconciliationGenerationReference
    previous_certificate: ReconciliationFileReference | None = None
    invalidation: ReconciliationFileReference
    source_root: Path
    source_workspace_id: UUID
    source_created_at: UTCDateTime
    destination_root: Path
    destination_workspace_id: UUID
    predecessor: ReconciliationFileReference
    previous_attempt: ReconciliationFileReference
    previous_reservation: ReconciliationFileReference
    previous_receipts: tuple[ReconciliationFileReference, ...]
    parent_membership: ReconciliationFileReference
    parent_seal: HistoricalSealSnapshot
    verified_w: ReconciliationFileReference
    database: ReconciliationFileReference
    wal: ReconciliationFileReference | None
    inventory: RecoveredProgressInventory | None = None
    core_proof: ReconciliationFileReference | None = None
    corrective_proofs: tuple[ReconciliationFileReference, ...] = ()
    code: tuple[ReconciliationFileReference, ...] = Field(min_length=1)
    tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_policy: Literal["shared-reference-verifier-v1"] = "shared-reference-verifier-v1"
    resource_receipt: ReconciliationFileReference
    reserved_successor: FrozenSourceCut | None = None
    successor_owner: CutOwner | None = None
    successor_admission: ReconciliationFileReference | None = None
    coverage: SuccessorCoverageProof | None = None
    prepared_at: UTCDateTime
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def coherent_generation(self) -> ProgressReconciliationV2:
        if (
            not self.source_root.is_absolute()
            or not self.destination_root.is_absolute()
            or self.source_root == self.destination_root
            or self.source_root.is_relative_to(self.destination_root)
            or self.destination_root.is_relative_to(self.source_root)
        ):
            raise ValueError("generation roots must be absolute and disjoint")
        if (
            self.database.role != "database"
            or self.database.path != self.destination_root / "storage/v2/index.duckdb"
            or self.parent_membership.role != "inventory"
            or self.resource_receipt.role != "resource"
            or any(ref.role != "code" for ref in self.code)
        ):
            raise ValueError("generation artifact binding differs")
        if self.wal is not None and (
            self.wal.role != "wal"
            or self.wal.path != self.destination_root / "storage/v2/index.duckdb.wal"
        ):
            raise ValueError("generation WAL binding differs")
        if self.phase == "PREPARED" and (
            self.inventory is not None
            or self.core_proof is not None
            or self.corrective_proofs
            or self.reserved_successor is not None
            or self.coverage is not None
        ):
            raise ValueError("prepared generation cannot certify or reserve")
        if self.phase != "PREPARED" and (
            self.inventory is None or self.core_proof is None or not self.corrective_proofs
        ):
            raise ValueError("baseline generation requires complete corrective proofs")
        reservation = (self.reserved_successor, self.successor_owner, self.successor_admission)
        if any(v is None for v in reservation) and not all(v is None for v in reservation):
            raise ValueError("generation reservation identity is incomplete")
        if self.reserved_successor is not None and (
            self.reserved_successor.source_root != self.source_root
            or self.reserved_successor.workspace_id != self.source_workspace_id
            or self.reserved_successor.created_at != self.source_created_at
        ):
            raise ValueError("generation successor source differs")
        if self.phase == "SUCCESSOR_BOUND":
            coverage, inventory = self.coverage, self.inventory
            if (
                coverage is None
                or inventory is None
                or self.reserved_successor is None
                or coverage.successor != self.reserved_successor
                or coverage.baseline_dataset_sha256 != inventory.dataset_sha256
                or coverage.verifier_tree_fingerprint != self.tree_fingerprint
                or coverage.covered_metric_count != inventory.metric_count
                or coverage.covered_diagnostic_count != inventory.diagnostic_count
            ):
                raise ValueError("generation successor coverage differs")
        elif self.coverage is not None:
            raise ValueError("only successor-bound generation can certify coverage")
        return self


class ReconciliationHead(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-reconciliation-head-v1"] = (
        "workspace-v2-reconciliation-head-v1"
    )
    root: Path
    workspace_id: UUID
    generation_id: UUID
    ordinal: int = Field(ge=1)
    certificate: ReconciliationFileReference
    last_event: ReconciliationFileReference
    previous_head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    publication_repair: ReconciliationFileReference | None = None
    recorded_at: UTCDateTime


class LedgerPublicationRepairGeneration(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    head: ReconciliationFileReference
    certificate: ReconciliationGenerationReference

    @model_validator(mode="after")
    def require_repository_roles(self) -> LedgerPublicationRepairGeneration:
        if self.head.role != "control" or self.certificate.artifact.role != "control":
            raise ValueError("publication repair generation roles differ")
        return self


class LedgerPublicationRepairInput(ContractModel):
    """One exact preactivation supersession of an invalid SUCCESSOR_BOUND publication."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-ledger-publication-repair-v1"] = (
        "workspace-v2-ledger-publication-repair-v1"
    )
    repair_id: UUID
    reason: Literal["SUCCESSOR_PUBLICATION_INVALID_NO_DATA_MUTATION"] = (
        "SUCCESSOR_PUBLICATION_INVALID_NO_DATA_MUTATION"
    )
    source_root: Path
    source_workspace_id: UUID
    destination_root: Path
    destination_workspace_id: UUID
    generation_id: UUID
    ordinal: int = Field(ge=1)
    revoked_attempt_id: UUID
    expected_head_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invalid_head: ReconciliationFileReference
    invalid_certificate: ReconciliationGenerationReference
    superseded_branch: tuple[LedgerPublicationRepairGeneration, ...] = Field(min_length=1)
    retained_baseline_head: ReconciliationFileReference
    retained_baseline_certificate: ReconciliationGenerationReference
    reservation: ReconciliationFileReference
    frozen_cut: ReconciliationFileReference
    cut_window: ReconciliationFileReference
    terminal_receipt: ReconciliationFileReference
    owner_absence: ReconciliationFileReference
    no_activation: ReconciliationFileReference
    exclusivity: ReconciliationFileReference
    source_bindings: ReconciliationFileReference
    archived_code: tuple[ReconciliationFileReference, ...] = Field(min_length=1)
    environment: ReconciliationFileReference
    database: ReconciliationFileReference
    wal: ReconciliationFileReference | None
    admission: ReconciliationFileReference
    producer: ReconciliationFileReference
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def coherent_publication_repair(self) -> LedgerPublicationRepairInput:
        roots = (self.source_root, self.destination_root)
        if (
            any(not root.is_absolute() for root in roots)
            or self.source_root == self.destination_root
            or self.source_root.is_relative_to(self.destination_root)
            or self.destination_root.is_relative_to(self.source_root)
        ):
            raise ValueError("publication repair roots must be absolute and disjoint")
        if (
            self.invalid_head.role != "control"
            or self.invalid_certificate.artifact.role != "control"
            or self.retained_baseline_head.role != "control"
            or self.retained_baseline_certificate.artifact.role != "control"
            or self.reservation.role != "control"
            or self.frozen_cut.role != "control"
            or self.cut_window.role != "control"
            or self.terminal_receipt.role != "control"
            or self.owner_absence.role != "control"
            or self.no_activation.role != "control"
            or self.exclusivity.role != "control"
            or self.source_bindings.role != "control"
            or self.environment.role != "control"
            or self.producer.role != "control"
        ):
            raise ValueError("publication repair evidence roles differ")
        if (
            self.invalid_certificate.schema_type != "workspace-v2-progress-reconciliation-v2"
            or self.retained_baseline_certificate.schema_type
            != "workspace-v2-progress-reconciliation-v2"
            or any(
                item.certificate.schema_type != "workspace-v2-progress-reconciliation-v2"
                for item in self.superseded_branch
            )
        ):
            raise ValueError("publication repair certificate type differs")
        if self.database.role != "database" or self.database.path != (
            self.destination_root / "storage/v2/index.duckdb"
        ):
            raise ValueError("publication repair database binding differs")
        if self.wal is not None and (
            self.wal.role != "wal"
            or self.wal.path != self.destination_root / "storage/v2/index.duckdb.wal"
        ):
            raise ValueError("publication repair WAL binding differs")
        if self.admission.role != "resource" or any(
            item.role != "code" for item in self.archived_code
        ):
            raise ValueError("publication repair resource/code roles differ")
        if not any(
            item.head == self.invalid_head and item.certificate == self.invalid_certificate
            for item in self.superseded_branch
        ):
            raise ValueError("publication repair branch omits the invalid publication")
        if any(
            item.head == self.retained_baseline_head
            or item.certificate == self.retained_baseline_certificate
            for item in self.superseded_branch
        ):
            raise ValueError("publication repair branch overlaps the retained baseline")
        return self


class LedgerPublicationRepairReceipt(ContractModel):
    schema_version: Literal["workspace-v2-ledger-publication-repair-v1"] = (
        "workspace-v2-ledger-publication-repair-v1"
    )
    request: LedgerPublicationRepairInput
    intent: ReconciliationFileReference
    created_at: UTCDateTime
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def coherent_receipt(self) -> LedgerPublicationRepairReceipt:
        if self.intent.role != "control" or self.created_at > self.recorded_at:
            raise ValueError("publication repair receipt timing or intent differs")
        return self


class GenerationContentProof(ContractModel):
    """Corrective W subset current evidence, not retrospective source-F2 equivalence."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-generation-content-proof-v1"] = (
        "workspace-v2-generation-content-proof-v1"
    )
    generation_id: UUID
    workspace_id: UUID
    baseline_certificate: ReconciliationFileReference
    baseline_database: ReconciliationFileReference
    database: ReconciliationFileReference
    resource_receipt: ReconciliationFileReference
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preserved_tables: tuple[tuple[str, int], ...]
    raw_count: int = Field(ge=0)
    raw_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=0)
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_count: int = Field(ge=0)
    metric_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostic_count: int = Field(ge=0)
    diagnostic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validated_additions: tuple[tuple[str, int], ...]
    comparison_scope: Literal["preserved-W-subset-current-destination"] = (
        "preserved-W-subset-current-destination"
    )
    verified_at: UTCDateTime


CutWindowLimit = Literal[1800, 5400]
CutWindowAuthorization = Literal["human-dc45-cut-5400-20261019"]
CUT_WINDOW_EXCEPTION_SECONDS = 5400
CUT_WINDOW_5400_AUTHORIZATION = "human-dc45-cut-5400-20261019"


CutWindowPhase = Literal[
    "PREPARED",
    "STOPPING",
    "QUIESCENT",
    "APPLYING",
    "RECOVERY_VERIFIED",
    "ACTIVATING",
    "HEALTHY_V2",
    "RETURNING_V1",
    "HEALTHY_V1",
    "ABORTED",
]


class CutWindowBound(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    step: Literal[
        "freeze",
        "comparison_delta",
        "operational",
        "physical_plan",
        "upload",
        "restore_open",
        "activation_health",
        "cancel_close_restart",
    ]
    upper_seconds: int = Field(gt=0)
    measurement: ReconciliationFileReference
    measured_at: UTCDateTime
    universe: NonEmptyStr
    workload: NonEmptyStr
    changes_since_measurement: NonEmptyStr


class CutWindowGo(ContractModel):
    """Conservative measured admission, published while the source remains active."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    bounds: tuple[CutWindowBound, ...] = Field(min_length=8, max_length=8)
    margin_seconds: int = Field(gt=0)
    baseline: ReconciliationFileReference
    prerequisites: tuple[ReconciliationFileReference, ...] = Field(min_length=1)
    guard: ReconciliationFileReference
    capacity: ReconciliationFileReference
    source_workspace_id: UUID
    destination_workspace_id: UUID
    admitted_at: UTCDateTime
    window_limit_seconds: CutWindowLimit = 1800
    window_authorization: CutWindowAuthorization | None = None
    attempt_id: UUID | None = None

    @model_validator(mode="after")
    def complete_conservative_window(self) -> CutWindowGo:
        if len({bound.step for bound in self.bounds}) != 8:
            raise ValueError("GO requires every measured phase exactly once")
        limit = self.window_limit_seconds
        if limit == CUT_WINDOW_EXCEPTION_SECONDS:
            if self.window_authorization != CUT_WINDOW_5400_AUTHORIZATION:
                raise ValueError("5400-second GO requires its explicit authorization")
            if self.attempt_id is None:
                raise ValueError("5400-second GO requires its explicit F3 attempt")
        elif self.window_authorization is not None or self.attempt_id is not None:
            raise ValueError("legacy 1800-second GO cannot carry the 5400-second exception")
        if sum(bound.upper_seconds for bound in self.bounds) + self.margin_seconds > limit:
            raise ValueError(f"measured global window exceeds {limit} seconds")
        return self

    def admits_prefix(self, prefix_seconds: int) -> bool:
        if prefix_seconds < 0:
            raise ValueError("cut window prefix cannot be negative")
        return (
            sum(bound.upper_seconds for bound in self.bounds) + self.margin_seconds + prefix_seconds
            <= self.window_limit_seconds
        )


class CutWindow(ContractModel):
    """A single boot-bound stop-to-health deadline, never restarted between commands."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-cut-window-v1"] = "workspace-v2-cut-window-v1"
    window_id: UUID
    attempt_id: UUID
    source_root: Path
    source_workspace_id: UUID
    destination_root: Path
    destination_workspace_id: UUID
    release_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    unit: Literal["investment-analyst.service"] = "investment-analyst.service"
    owner: CutOwner
    boot_id: UUID
    started_monotonic_ns: int = Field(ge=0)
    deadline_monotonic_ns: int = Field(gt=0)
    stop_started_at: UTCDateTime
    recovery_reserve_seconds: int = Field(gt=0, lt=1800)
    window_limit_seconds: CutWindowLimit = 1800
    window_authorization: CutWindowAuthorization | None = None
    go: ReconciliationFileReference
    baseline: ReconciliationFileReference
    code: ReconciliationFileReference
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_receipt: ReconciliationFileReference
    phase: CutWindowPhase = "PREPARED"
    step: NonEmptyStr
    reservation: ReconciliationFileReference | None = None
    recovery_receipt: ReconciliationFileReference | None = None
    activation_intent: ReconciliationFileReference | None = None
    binding: ReconciliationFileReference | None = None
    previous_receipt: ReconciliationFileReference | None = None
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def immutable_clock_and_boundary(self) -> CutWindow:
        if self.window_limit_seconds == CUT_WINDOW_EXCEPTION_SECONDS:
            if self.window_authorization != CUT_WINDOW_5400_AUTHORIZATION:
                raise ValueError("5400-second cut window requires its explicit authorization")
        elif self.window_authorization is not None:
            raise ValueError("legacy 1800-second cut window cannot carry the 5400-second exception")
        if (
            self.deadline_monotonic_ns - self.started_monotonic_ns
            != self.window_limit_seconds * 1_000_000_000
        ):
            raise ValueError(
                "cut window must have exactly one global "
                f"{self.window_limit_seconds}-second deadline"
            )
        if not self.source_root.is_absolute() or not self.destination_root.is_absolute():
            raise ValueError("cut window roots must be absolute")
        if self.source_root == self.destination_root:
            raise ValueError("cut window roots must differ")
        if self.owner.boot_id != self.boot_id or self.owner.pid is None:
            raise ValueError("cut window requires complete live owner identity")
        if self.phase in {"ACTIVATING", "HEALTHY_V2"} and (
            self.activation_intent is None or self.recovery_receipt is None
        ):
            raise ValueError("activation requires durable intention and recovery")
        if self.phase == "HEALTHY_V2" and self.binding is None:
            raise ValueError("healthy v2 requires its observed deployment binding")
        return self


class CutWindowReceipt(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-cut-window-receipt-v1"] = (
        "workspace-v2-cut-window-receipt-v1"
    )
    window: ReconciliationFileReference
    phase: Literal["HEALTHY_V2", "HEALTHY_V1", "ABORTED"]
    completed_monotonic_ns: int = Field(ge=0)
    completed_at: UTCDateTime
    identity: ReconciliationFileReference
    health: ReconciliationFileReference
