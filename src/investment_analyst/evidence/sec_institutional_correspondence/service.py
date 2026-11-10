"""Lineage verification and deterministic resolution for row-scoped 13F claims.

Every claim is verified against the complete persisted evidence chain
``universe snapshot → candidate → report → semantic artifact → information-table row`` before it is
trusted, and against the live catalog SEC/CUSIP binding before it is created. Nothing is inferred
from a ticker, issuer name, FIGI, ISIN or text similarity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.storage import StorageError


class SecInstitutionalRowCorrespondenceError(StorageError):
    """A row-scoped claim cannot preserve its evidence contract."""


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class RowCorrespondenceResolution(_Strict):
    """Deterministic outcome of resolving the visible claims of one information-table row."""

    state: Literal["resolved", "absent", "ambiguous_asset", "conflicting_content"]
    correspondence: SecInstitutionalRowCorrespondence | None = None
    considered: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def shape(self) -> RowCorrespondenceResolution:
        if (self.state == "resolved") != (self.correspondence is not None):
            raise ValueError("only a resolved row correspondence carries a claim")
        if self.state == "absent" and self.considered != 0:
            raise ValueError("an absent resolution cannot consider evidence")
        return self


class ClaimLineageClassification(_Strict):
    """Strict content verification plus an explicit temporal classification.

    ``verify_lineage`` remains strict: only ``verified`` is accepted as persisted authority. A
    ``stale_availability`` claim has complete, verified parents and content but an availability
    earlier than the exact maximum its *current* recovered snapshot declares; it is evidence of the
    temporal defect, never a historical certification.
    """

    state: Literal["verified", "stale_availability", "invalid"]
    expected_available_at: UTCDateTime | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def shape(self) -> ClaimLineageClassification:
        if self.state == "verified" and self.reason is not None:
            raise ValueError("verified lineage cannot carry a failure reason")
        if self.state == "invalid" and self.reason is None:
            raise ValueError("invalid lineage requires a reason")
        if self.state != "invalid" and self.expected_available_at is None:
            raise ValueError("verified or stale lineage requires its expected availability")
        return self


class VerifiedRowCorrespondenceResolution(_Strict):
    """Selection of one strictly verified claim among equivalent persisted proofs."""

    state: Literal["verified", "stale_only", "absent", "conflicting_content", "invalid"]
    selected: SecInstitutionalRowCorrespondence | None = None
    verified: tuple[SecInstitutionalRowCorrespondence, ...] = ()
    stale: tuple[SecInstitutionalRowCorrespondence, ...] = ()
    considered: int = Field(default=0, ge=0)
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def shape(self) -> VerifiedRowCorrespondenceResolution:
        if (self.state == "verified") != (self.selected is not None):
            raise ValueError("only a verified resolution carries the selected claim")
        if self.selected is not None and self.selected not in self.verified:
            raise ValueError("selected claim must belong to the verified set")
        if self.state == "absent" and self.considered != 0:
            raise ValueError("an absent resolution cannot consider evidence")
        if self.state == "stale_only" and not self.stale:
            raise ValueError("stale_only requires at least one stale claim")
        if self.state in {"conflicting_content", "invalid"} and self.reason is None:
            raise ValueError("a conflict or invalid resolution requires a reason")
        identifier_sets = (
            {item.correspondence_id for item in self.verified},
            {item.correspondence_id for item in self.stale},
        )
        if any(
            len(values) != len(items)
            for values, items in zip(identifier_sets, (self.verified, self.stale), strict=True)
        ):
            raise ValueError("resolution claims must be unique")
        if identifier_sets[0] & identifier_sets[1]:
            raise ValueError("verified and stale claim sets must be disjoint")
        return self


def _order(item: SecInstitutionalRowCorrespondence) -> tuple[datetime, str]:
    return (item.available_at, str(item.correspondence_id))


def resolve_visible_claims(
    claims: Sequence[SecInstitutionalRowCorrespondence],
) -> RowCorrespondenceResolution:
    """Resolve equivalent claims deterministically and keep conflicting ones ambiguous.

    The single owner of this rule: several equivalent proofs for one row and asset resolve by
    ``(available_at, correspondence_id)`` ascending, while claims that disagree on the asset or on
    the declared content stay explicitly ambiguous and are never normalized.
    """
    if not claims:
        return RowCorrespondenceResolution(state="absent")
    if len({item.asset_id for item in claims}) > 1:
        return RowCorrespondenceResolution(state="ambiguous_asset", considered=len(claims))
    if len({(item.cusip, item.title_of_class, item.report_period) for item in claims}) > 1:
        return RowCorrespondenceResolution(state="conflicting_content", considered=len(claims))
    return RowCorrespondenceResolution(
        state="resolved", correspondence=min(claims, key=_order), considered=len(claims)
    )


class SecInstitutionalRowCorrespondenceService:
    """Verify and resolve row-scoped claims without writing anything."""

    def __init__(self, storage) -> None:
        self._storage = storage
        self._repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        self._universe = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
        self._holdings = InstitutionalHoldingsRepository(storage.raw_records)
        self._semantics = InstitutionalSemanticsRepository(storage.raw_records)
        self._snapshot_cache: dict[UUID, object] = {}
        self._revision_cache: dict[UUID, object] = {}
        self._report_cache: dict[UUID, object] = {}
        self._artifact_cache: dict[UUID, object] = {}
        self._content_cache: dict[UUID, object] = {}

    def verify_lineage(self, claim: SecInstitutionalRowCorrespondence) -> None:
        """Verify one claim against the persisted universe, filing, semantics and row."""
        _, expected_available = self._verify_content(claim)
        if claim.available_at != expected_available:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence availability is not the exact maximum of its parents"
            )

    def classify_lineage(
        self, claim: SecInstitutionalRowCorrespondence
    ) -> ClaimLineageClassification:
        """Classify complete content as verified, temporally stale, or invalid.

        The strict ``verify_lineage`` contract is unchanged: only ``verified`` is authority. A stale
        claim is retained solely to explain an already-persisted observation; it never certifies the
        old availability or replaces a current verified proof.
        """
        try:
            artifact, expected_available = self._verify_content(claim)
        except SecInstitutionalRowCorrespondenceError as error:
            return ClaimLineageClassification(state="invalid", reason=str(error))
        if claim.available_at == expected_available:
            return ClaimLineageClassification(
                state="verified", expected_available_at=expected_available
            )
        if artifact.available_at <= claim.available_at < expected_available:
            return ClaimLineageClassification(
                state="stale_availability", expected_available_at=expected_available
            )
        return ClaimLineageClassification(
            state="invalid",
            expected_available_at=expected_available,
            reason="row correspondence availability is inconsistent with its parents",
        )

    def resolve_verified_claims(
        self, claims: Sequence[SecInstitutionalRowCorrespondence]
    ) -> VerifiedRowCorrespondenceResolution:
        """Select one strictly verified proof from equivalent claims for the same row.

        Different assets, classes, periods, managers, reports, artifacts, rows, datasets or
        candidates remain conflicts. Claims from different snapshots may be equivalent only when
        their dataset and candidate identities coincide and every parent/content field matches.
        """
        ordered = tuple(claims)
        if not ordered:
            return VerifiedRowCorrespondenceResolution(state="absent")
        identities = {
            (
                item.asset_id,
                item.cusip,
                item.title_of_class,
                item.report_period,
                item.manager_cik,
                item.report_id,
                item.artifact_id,
                item.row_id,
            )
            for item in ordered
        }
        if len(identities) != 1:
            return VerifiedRowCorrespondenceResolution(
                state="conflicting_content",
                considered=len(ordered),
                reason="row correspondence claims disagree on row identity or content",
            )
        if len({(item.dataset_revision_id, item.candidate_id) for item in ordered}) != 1:
            return VerifiedRowCorrespondenceResolution(
                state="conflicting_content",
                considered=len(ordered),
                reason="row correspondence claims disagree on dataset or candidate identity",
            )
        classifications = {item.correspondence_id: self.classify_lineage(item) for item in ordered}
        invalid = [
            (item, classifications[item.correspondence_id])
            for item in ordered
            if classifications[item.correspondence_id].state == "invalid"
        ]
        if invalid:
            return VerifiedRowCorrespondenceResolution(
                state="invalid",
                considered=len(ordered),
                reason=invalid[0][1].reason or "row correspondence claim is invalid",
            )
        verified = tuple(
            sorted(
                (
                    item
                    for item in ordered
                    if classifications[item.correspondence_id].state == "verified"
                ),
                key=_order,
            )
        )
        stale = tuple(
            sorted(
                (
                    item
                    for item in ordered
                    if classifications[item.correspondence_id].state == "stale_availability"
                ),
                key=_order,
            )
        )
        if verified:
            return VerifiedRowCorrespondenceResolution(
                state="verified",
                selected=verified[0],
                verified=verified,
                stale=stale,
                considered=len(ordered),
            )
        if stale:
            return VerifiedRowCorrespondenceResolution(
                state="stale_only", stale=stale, considered=len(ordered)
            )
        return VerifiedRowCorrespondenceResolution(
            state="invalid",
            considered=len(ordered),
            reason="row correspondence claims have no verified or stale lineage",
        )

    def _verify_content(self, claim: SecInstitutionalRowCorrespondence):
        """Verify every parent and content field, reusing execution-scoped parents."""
        cached = self._content_cache.get(claim.correspondence_id)
        if cached is not None:
            if isinstance(cached, SecInstitutionalRowCorrespondenceError):
                raise cached
            return cached
        try:
            result = self._verify_content_uncached(claim)
        except SecInstitutionalRowCorrespondenceError as error:
            self._content_cache[claim.correspondence_id] = error
            raise
        self._content_cache[claim.correspondence_id] = result
        return result

    def _verify_content_uncached(self, claim: SecInstitutionalRowCorrespondence):
        snapshot = self._snapshot_cache.get(claim.universe_snapshot_id)
        if snapshot is None:
            snapshot = self._universe.get_snapshot(claim.universe_snapshot_id)
            if snapshot is None:
                raise SecInstitutionalRowCorrespondenceError(
                    "row correspondence snapshot is missing"
                )
            self._snapshot_cache[claim.universe_snapshot_id] = snapshot
        revision = self._revision_cache.get(snapshot.dataset_revision_id)
        if revision is None:
            revision = self._universe.get_dataset_revision(snapshot.dataset_revision_id)
            if revision is not None:
                self._revision_cache[snapshot.dataset_revision_id] = revision
        if revision is None or revision.content_sha256 != snapshot.dataset_sha256:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence dataset lineage is invalid"
            )
        if (revision.period_start, revision.period_end) != (
            snapshot.period_start,
            snapshot.period_end,
        ):
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence dataset period conflicts"
            )
        if claim.dataset_revision_id != snapshot.dataset_revision_id:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence dataset revision differs from its snapshot"
            )
        candidates = [
            item for item in snapshot.candidates if item.candidate_id == claim.candidate_id
        ]
        if len(candidates) != 1:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence candidate is absent or ambiguous in its snapshot"
            )
        candidate = candidates[0]
        if (
            not candidate.is_selected
            or candidate.dataset_revision_id != claim.dataset_revision_id
            or candidate.manager_cik != claim.manager_cik
            or candidate.asset_id != claim.asset_id
            or candidate.cusip != claim.cusip
            or candidate.report_period != claim.report_period
        ):
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence candidate conflicts with the claim"
            )
        report = self._cached_report(claim.report_id)
        if (
            report is None
            or report.manager_cik != claim.manager_cik
            or report.report_period != claim.report_period
        ):
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence report is missing or conflicts"
            )
        artifact = self._cached_artifact(claim.artifact_id)
        if (
            artifact is None
            or artifact.parent_report_id != claim.report_id
            or artifact.manager_cik != claim.manager_cik
            or artifact.report_period != claim.report_period
        ):
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence artifact is missing or conflicts"
            )
        rows = [row for row in artifact.rows if row.row_id == claim.row_id]
        if len(rows) != 1:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence row is absent or ambiguous in its artifact"
            )
        row = rows[0]
        if row.cusip != claim.cusip or row.title_of_class != claim.title_of_class:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence row content conflicts with the claim"
            )
        if claim.available_at < artifact.available_at:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence availability precedes its artifact"
            )
        return artifact, max(snapshot.available_at, artifact.available_at)

    def _cached_report(self, report_id: UUID):
        if report_id not in self._report_cache:
            self._report_cache[report_id] = self._holdings.get_report(report_id)
        return self._report_cache[report_id]

    def _cached_artifact(self, artifact_id: UUID):
        if artifact_id not in self._artifact_cache:
            self._artifact_cache[artifact_id] = self._semantics.get(artifact_id)
        return self._artifact_cache[artifact_id]

    def verify_catalog_binding(
        self, claim: SecInstitutionalRowCorrespondence, *, catalog_cusips: Mapping[str, str]
    ) -> None:
        """Require that the live catalog still binds this exact CUSIP to this exact asset."""
        if catalog_cusips.get(claim.cusip) != claim.asset_id:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence catalog binding no longer matches"
            )

    def resolve(
        self,
        *,
        artifact_id: UUID,
        row_id: UUID,
        known_at: datetime,
        asset_id: str | None = None,
    ) -> RowCorrespondenceResolution:
        """Resolve the claims of one row deterministically, never merging conflicting content."""
        return resolve_visible_claims(
            self._repository.list(
                known_at=known_at, asset_id=asset_id, artifact_id=artifact_id, row_id=row_id
            )
        )

    def resolve_verified(
        self,
        *,
        artifact_id: UUID,
        row_id: UUID,
        known_at: datetime,
        asset_id: str | None = None,
    ) -> VerifiedRowCorrespondenceResolution:
        """Select the strictly verified proof for one row at one cut."""
        return self.resolve_verified_claims(
            self._repository.list(
                known_at=known_at, asset_id=asset_id, artifact_id=artifact_id, row_id=row_id
            )
        )


__all__ = [
    "ClaimLineageClassification",
    "RowCorrespondenceResolution",
    "SecInstitutionalRowCorrespondenceError",
    "SecInstitutionalRowCorrespondenceService",
    "VerifiedRowCorrespondenceResolution",
    "resolve_visible_claims",
]
