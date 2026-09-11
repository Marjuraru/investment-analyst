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

from investment_analyst.core.models.base import ContractModel
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

    def verify_lineage(self, claim: SecInstitutionalRowCorrespondence) -> None:
        """Verify one claim against the persisted universe, filing, semantics and row."""
        snapshot = self._universe.get_snapshot(claim.universe_snapshot_id)
        if snapshot is None:
            raise SecInstitutionalRowCorrespondenceError("row correspondence snapshot is missing")
        revision = self._universe.get_dataset_revision(snapshot.dataset_revision_id)
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
        report = self._holdings.get_report(claim.report_id)
        if (
            report is None
            or report.manager_cik != claim.manager_cik
            or report.report_period != claim.report_period
        ):
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence report is missing or conflicts"
            )
        artifact = self._semantics.get(claim.artifact_id)
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
        expected_available = max(snapshot.available_at, artifact.available_at)
        if claim.available_at != expected_available:
            raise SecInstitutionalRowCorrespondenceError(
                "row correspondence availability is not the exact maximum of its parents"
            )

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


__all__ = [
    "RowCorrespondenceResolution",
    "SecInstitutionalRowCorrespondenceError",
    "SecInstitutionalRowCorrespondenceService",
    "resolve_visible_claims",
]
