"""Typed, append-only evidence of one preserved SEC acquisition."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_analyst.core.models import ContractModel, RawRecord, SourceReference, UTCDateTime
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

ACQUISITION_SCHEMA = "sec-acquisition-provenance-v1"
ACQUISITION_DOCUMENT_SCHEMA = "sec-acquisition-provenance-document-v1"
BINDING_SCHEMA = "sec-acquisition-provenance-binding-v1"
EVIDENCE_PLAN_SCHEMA = "sec-provenance-evidence-plan-v1"
EVIDENCE_RECEIPT_SCHEMA = "sec-provenance-evidence-receipt-v1"
EVIDENCE_VERIFICATION_SCHEMA = "sec-provenance-evidence-verification-v1"
_SHA256 = r"^[0-9a-f]{64}$"


class SecProvenanceError(RuntimeError):
    """Base error for SEC acquisition provenance evidence."""


class SecProvenanceUnavailableError(SecProvenanceError):
    """Raised when evidence is absent, corrupt, unbound or outside this workspace."""


class SecProvenanceClassification(StrEnum):
    """Closed disposition of one SEC raw inside the declared issuer universe."""

    CURRENT_VALID = "CURRENT_VALID"
    HISTORICAL_PROVENANCE_REQUIRED = "HISTORICAL_PROVENANCE_REQUIRED"
    UNREFERENCED_OR_INDIRECT = "UNREFERENCED_OR_INDIRECT"
    UNRESOLVED = "UNRESOLVED"


class SecProvenanceObservationBinding(ContractModel):
    """One immutable observation bound to the acquisition that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    observation_id: UUID
    observation_sha256: str = Field(pattern=_SHA256)
    submissions_record_id: UUID
    submissions_payload_checksum_sha256: str = Field(pattern=_SHA256)
    normalized_at: UTCDateTime
    transformation_version: str = Field(min_length=1)


class SecAcquisitionProvenance(ContractModel):
    """Original acquisition metadata of a raw whose payload is still persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-acquisition-provenance-v1"] = ACQUISITION_SCHEMA
    destination_workspace_id: UUID
    source_workspace_id: UUID
    raw_record_id: UUID
    asset_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    raw_schema_version: str = Field(min_length=1)
    payload_checksum_sha256: str = Field(pattern=_SHA256)
    payload_digest_sha256: str = Field(pattern=_SHA256)
    original_raw_sha256: str = Field(pattern=_SHA256)
    original_source: SourceReference
    original_event_time: UTCDateTime
    original_available_at: UTCDateTime
    original_received_at: UTCDateTime
    bindings: tuple[SecProvenanceObservationBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_source(self) -> SecAcquisitionProvenance:
        if self.original_source.source_id != self.source_id:
            raise ValueError("acquisition source does not match its raw source")
        if self.original_source.checksum_sha256 != self.payload_checksum_sha256:
            raise ValueError("acquisition payload checksum does not match its source reference")
        return self

    @property
    def identity_digest(self) -> str:
        """Return the canonical identity of this evidence group."""
        document = json.dumps(
            {
                "schema_version": self.schema_version,
                "destination_workspace_id": str(self.destination_workspace_id),
                "raw_record_id": str(self.raw_record_id),
                "original_raw_sha256": self.original_raw_sha256,
                "bindings": [
                    {
                        "observation_id": str(item.observation_id),
                        "observation_sha256": item.observation_sha256,
                        "submissions_record_id": str(item.submissions_record_id),
                        "submissions_payload_checksum_sha256": (
                            item.submissions_payload_checksum_sha256
                        ),
                        "normalized_at": item.normalized_at.isoformat(),
                        "transformation_version": item.transformation_version,
                    }
                    for item in self.bindings
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256_hex(document.encode("utf-8"))

    @property
    def content_digest(self) -> str:
        """Return the canonical digest of the complete evidence document."""
        return sha256_hex(canonical_json_bytes(self))

    def binding_for(self, observation_id: UUID) -> SecProvenanceObservationBinding | None:
        """Return the exact binding of one observation, if this group carries it."""
        for item in self.bindings:
            if item.observation_id == observation_id:
                return item
        return None

    def original_record(self, payload: Mapping[str, object]) -> RawRecord:
        """Rebuild the original raw in memory using the payload already persisted."""
        return RawRecord(
            record_id=self.raw_record_id,
            asset_id=self.asset_id,
            source=self.original_source,
            event_time=self.original_event_time,
            available_at=self.original_available_at,
            received_at=self.original_received_at,
            payload=dict(payload),
            schema_version=self.raw_schema_version,
        )


class SecAcquisitionProvenanceDocument(ContractModel):
    """Hash-bound envelope that keeps the artifact digest verifiable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-acquisition-provenance-document-v1"] = ACQUISITION_DOCUMENT_SCHEMA
    artifact: SecAcquisitionProvenance
    content_digest_sha256: str = Field(pattern=_SHA256)

    @classmethod
    def seal(cls, artifact: SecAcquisitionProvenance) -> SecAcquisitionProvenanceDocument:
        """Return a sealed envelope for one artifact."""
        return cls(artifact=artifact, content_digest_sha256=artifact.content_digest)

    def verified_artifact(self) -> SecAcquisitionProvenance:
        """Return the artifact only when its canonical digest still matches."""
        if self.artifact.content_digest != self.content_digest_sha256:
            raise SecProvenanceUnavailableError("acquisition evidence digest does not match")
        return self.artifact


class SecProvenanceBindingReference(ContractModel):
    """Small secondary index entry so one observation resolves without a scan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-acquisition-provenance-binding-v1"] = BINDING_SCHEMA
    observation_id: UUID
    raw_record_id: UUID
    identity_digest: str = Field(pattern=_SHA256)


def observation_digest(observation: BaseModel) -> str:
    """Return the canonical digest of one immutable normalized observation."""
    return sha256_hex(canonical_json_bytes(observation))


def payload_digest(payload: object) -> str:
    """Return the canonical digest of a persisted raw payload without duplicating it."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return sha256_hex(encoded.encode("utf-8"))


class SecProvenanceEvidenceGroup(ContractModel):
    """Read-only classification of one SEC raw inside the declared universe."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_record_id: UUID
    asset_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    raw_schema_version: str = Field(min_length=1)
    classification: SecProvenanceClassification
    dependent_observations: int = Field(ge=0)
    current_reconstructing: int = Field(ge=0)
    historical_required: int = Field(ge=0)
    indirect_references: int = Field(ge=0)
    detail: str = ""


class SecProvenanceEvidenceEntry(ContractModel):
    """One artifact admitted by the sealed plan with its exact reconstruction proof."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact: SecAcquisitionProvenance
    evidence_present_before: bool
    dependent_observations: int = Field(ge=1)
    reconstructed: int = Field(ge=1)


class SecProvenanceEvidenceConflict(ContractModel):
    """One group whose historical provenance could not be proven."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_record_id: UUID
    asset_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    raw_schema_version: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    dependent_observations: int = Field(ge=1)
    current_reconstructing: int = Field(ge=0)
    field_differences: tuple[str, ...] = ()


class SecProvenanceEvidenceBudget(ContractModel):
    """Declared physical footprint of one sealed evidence plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifacts: int = Field(ge=0)
    bindings: int = Field(ge=0)
    bytes_new: int = Field(ge=0)


class SecProvenanceEvidencePlan(ContractModel):
    """Sealed, append-only evidence plan for the declared SEC universe."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-provenance-evidence-plan-v1"] = EVIDENCE_PLAN_SCHEMA
    generated_at: UTCDateTime
    executor_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    destination_workspace_id: UUID
    source_workspace_id: UUID
    catalog_assets: tuple[str, ...] = Field(min_length=1)
    groups: tuple[SecProvenanceEvidenceGroup, ...] = ()
    entries: tuple[SecProvenanceEvidenceEntry, ...] = ()
    conflicts: tuple[SecProvenanceEvidenceConflict, ...] = ()
    budget: SecProvenanceEvidenceBudget

    def digest(self) -> str:
        """Return the canonical digest that identifies this operation."""
        return sha256_hex(canonical_json_bytes(self))

    def to_json_dict(self) -> dict[str, object]:
        """Return the JSON representation including the operation digest."""
        payload = self.model_dump(mode="json")
        payload["plan_digest"] = self.digest()
        return payload


class SecProvenanceEvidenceReceiptEntry(ContractModel):
    """Per-artifact result so an interrupted publication can resume."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity_digest: str = Field(pattern=_SHA256)
    raw_record_id: UUID
    artifact_created: bool
    bindings_created: int = Field(ge=0)


class SecProvenanceEvidenceReceipt(ContractModel):
    """Append-once receipt of one applied evidence plan."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-provenance-evidence-receipt-v1"] = EVIDENCE_RECEIPT_SCHEMA
    plan_digest: str = Field(pattern=_SHA256)
    destination_workspace_id: UUID
    executor_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    completed_at: UTCDateTime
    entries: tuple[SecProvenanceEvidenceReceiptEntry, ...] = ()
    files_created: int = Field(ge=0)
    bytes_written: int = Field(ge=0)


class SecProvenanceEvidenceVerification(ContractModel):
    """Post-publication proof computed from the destination workspace alone."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sec-provenance-evidence-verification-v1"] = (
        EVIDENCE_VERIFICATION_SCHEMA
    )
    plan_digest: str = Field(pattern=_SHA256)
    verified_at: UTCDateTime
    artifacts: int = Field(ge=0)
    bindings: int = Field(ge=0)
    reconstructed: int = Field(ge=0)
    divergent: int = Field(ge=0)
    verified: bool


__all__ = [
    "ACQUISITION_SCHEMA",
    "BINDING_SCHEMA",
    "EVIDENCE_PLAN_SCHEMA",
    "EVIDENCE_RECEIPT_SCHEMA",
    "EVIDENCE_VERIFICATION_SCHEMA",
    "SecAcquisitionProvenance",
    "SecAcquisitionProvenanceDocument",
    "SecProvenanceBindingReference",
    "SecProvenanceClassification",
    "SecProvenanceError",
    "SecProvenanceEvidenceBudget",
    "SecProvenanceEvidenceConflict",
    "SecProvenanceEvidenceEntry",
    "SecProvenanceEvidenceGroup",
    "SecProvenanceEvidencePlan",
    "SecProvenanceEvidenceReceipt",
    "SecProvenanceEvidenceReceiptEntry",
    "SecProvenanceEvidenceVerification",
    "SecProvenanceObservationBinding",
    "SecProvenanceUnavailableError",
    "observation_digest",
    "payload_digest",
]
