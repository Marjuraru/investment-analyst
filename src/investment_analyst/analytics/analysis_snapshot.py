"""Pure, typed and immutable point-in-time analysis snapshot contract.

Part of DATA-CHASSIS-32.
Declares the single-asset, single-domain AnalysisSnapshot contract, its
canonical preimage, deterministic RFC 9562 UUIDv8 identity, and the canonical
digest of referenced EvidenceSet hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import ConfigDict, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

SNAPSHOT_LABEL: Final[str] = "analysis-snapshot-v1"
_SHA256_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


def _uuid_v8_from_digest(digest: bytes) -> UUID:
    """Construct an RFC 9562 UUIDv8 from a 16+ byte hash digest."""
    raw = bytearray(digest[:16])
    # Set version bits to 8 (0b1000)
    raw[6] = (raw[6] & 0x0F) | 0x80
    # Set variant bits to RFC 4122 (0b10xx)
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def canonical_evidence_set_digest(hashes: Sequence[str] = ()) -> str:
    """Return SHA-256 digest of canonically ordered, deduplicated EvidenceSet hashes.

    When no EvidenceSets are referenced, returns the canonical hash of the
    empty sequence (matching canonical_lineage_hash([])).
    """
    for item in hashes:
        if not isinstance(item, str) or not _SHA256_HEX_PATTERN.fullmatch(item):
            raise ValueError("EvidenceSet hash must be a 64-character lowercase SHA-256 hex string")
    ordered = sorted(set(hashes))
    encoded = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_preimage_dict(
    *,
    asset_id: str,
    domain: str,
    known_at: datetime,
    policy_version: str,
    metric_ids: Sequence[UUID | str] = (),
    diagnostic_ids: Sequence[UUID | str] = (),
    evidence_set_digest: str,
    created_at: datetime | None = None,
    **execution_kwargs: object,
) -> dict[str, object]:
    """Return the exact, canonical semantic coordinate dictionary for snapshot identity.

    Deliberately excludes created_at, clock, execution parameters and runtime state.
    """
    del created_at, execution_kwargs
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise ValueError("known_at must be timezone-aware")
    return {
        "asset_id": asset_id,
        "diagnostic_ids": sorted(str(item) for item in set(diagnostic_ids)),
        "domain": domain,
        "evidence_set_digest": evidence_set_digest,
        "known_at": known_at.astimezone(UTC).isoformat(),
        "metric_ids": sorted(str(item) for item in set(metric_ids)),
        "policy_version": policy_version,
    }


def analysis_snapshot_identity(
    *,
    asset_id: str,
    domain: str,
    known_at: datetime,
    policy_version: str,
    metric_ids: Sequence[UUID | str] = (),
    diagnostic_ids: Sequence[UUID | str] = (),
    evidence_set_digest: str,
    created_at: datetime | None = None,
    **execution_kwargs: object,
) -> UUID:
    """Return deterministic UUIDv8 for the given snapshot semantic coordinates."""
    document = snapshot_preimage_dict(
        asset_id=asset_id,
        domain=domain,
        known_at=known_at,
        policy_version=policy_version,
        metric_ids=metric_ids,
        diagnostic_ids=diagnostic_ids,
        evidence_set_digest=evidence_set_digest,
        created_at=created_at,
        **execution_kwargs,
    )
    encoded = json.dumps(
        {"label": SNAPSHOT_LABEL, **document},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _uuid_v8_from_digest(hashlib.sha256(encoded.encode("utf-8")).digest())


class AnalysisSnapshot(ContractModel):
    """Pure, typed and immutable point-in-time analysis snapshot for one asset and domain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    asset_id: NonEmptyStr
    domain: NonEmptyStr
    known_at: UTCDateTime
    policy_version: NonEmptyStr
    metric_ids: tuple[UUID, ...] = ()
    diagnostic_ids: tuple[UUID, ...] = ()
    evidence_set_digest: NonEmptyStr
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_snapshot(self) -> AnalysisSnapshot:
        """Validate references ordering, uniqueness, digest format and UUIDv8 identity."""
        if len(self.metric_ids) != len(set(self.metric_ids)):
            raise ValueError("metric references must not contain duplicates")
        if tuple(sorted(self.metric_ids)) != self.metric_ids:
            raise ValueError("metric references must be sorted")

        if len(self.diagnostic_ids) != len(set(self.diagnostic_ids)):
            raise ValueError("diagnostic references must not contain duplicates")
        if tuple(sorted(self.diagnostic_ids)) != self.diagnostic_ids:
            raise ValueError("diagnostic references must be sorted")

        if not _SHA256_HEX_PATTERN.fullmatch(self.evidence_set_digest):
            raise ValueError(
                "evidence_set_digest must be a 64-character lowercase SHA-256 hex string"
            )

        expected_id = analysis_snapshot_identity(
            asset_id=self.asset_id,
            domain=self.domain,
            known_at=self.known_at,
            policy_version=self.policy_version,
            metric_ids=self.metric_ids,
            diagnostic_ids=self.diagnostic_ids,
            evidence_set_digest=self.evidence_set_digest,
        )
        if self.snapshot_id != expected_id:
            raise ValueError("snapshot identity is not deterministic")
        return self


def build_analysis_snapshot(
    *,
    asset_id: str,
    domain: str,
    known_at: datetime,
    policy_version: str,
    metric_ids: Sequence[UUID] = (),
    diagnostic_ids: Sequence[UUID] = (),
    evidence_set_hashes: Sequence[str] = (),
    evidence_set_digest: str | None = None,
    created_at: datetime,
) -> AnalysisSnapshot:
    """Build a validated AnalysisSnapshot with ordered references and UUIDv8 identity."""
    sorted_metric_ids = tuple(sorted(set(metric_ids)))
    sorted_diag_ids = tuple(sorted(set(diagnostic_ids)))
    digest = evidence_set_digest or canonical_evidence_set_digest(evidence_set_hashes)
    snapshot_id = analysis_snapshot_identity(
        asset_id=asset_id,
        domain=domain,
        known_at=known_at,
        policy_version=policy_version,
        metric_ids=sorted_metric_ids,
        diagnostic_ids=sorted_diag_ids,
        evidence_set_digest=digest,
        created_at=created_at,
    )
    return AnalysisSnapshot(
        snapshot_id=snapshot_id,
        asset_id=asset_id,
        domain=domain,
        known_at=known_at,
        policy_version=policy_version,
        metric_ids=sorted_metric_ids,
        diagnostic_ids=sorted_diag_ids,
        evidence_set_digest=digest,
        created_at=created_at,
    )


__all__ = [
    "SNAPSHOT_LABEL",
    "AnalysisSnapshot",
    "analysis_snapshot_identity",
    "build_analysis_snapshot",
    "canonical_evidence_set_digest",
    "snapshot_preimage_dict",
]
