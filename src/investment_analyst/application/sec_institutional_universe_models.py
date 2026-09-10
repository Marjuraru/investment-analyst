"""Application models for refreshing and querying the SEC 13F manager universe."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalUniverseRefreshRequest(_Strict):
    """Request to refresh the official Form 13F manager universe from sec.gov."""


class SecInstitutionalUniverseRefreshResult(_Strict):
    """Summary of the refreshed dataset revision and universe snapshot."""

    revision_id: UUID
    snapshot_id: UUID
    period_start: date
    period_end: date
    dataset_url: NonEmptyStr
    dataset_sha256: NonEmptyStr
    size_bytes: int = Field(gt=0)
    retrieved_at: UTCDateTime
    available_at: UTCDateTime
    eligible_asset_count: int = Field(ge=0)
    matched_asset_count: int = Field(ge=0)
    candidate_manager_count: int = Field(ge=0)
    selected_manager_count: int = Field(ge=0)
    unselected_manager_count: int = Field(ge=0)
    coverage_complete: bool = True
    created: bool


class SecInstitutionalUniverseQueryRequest(_Strict):
    """Point-in-time query request for the manager universe snapshot."""

    known_at: UTCDateTime
    asset_id: NonEmptyStr | None = None
    cik: NonEmptyStr | None = None


class SecInstitutionalUniverseQueryResult(_Strict):
    """Result of a point-in-time manager universe query with optional candidate filtering."""

    snapshot: Sec13FManagerUniverseSnapshot
    filtered_candidates: tuple[Sec13FManagerCandidate, ...]
