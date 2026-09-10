"""Strict, immutable domain models for SEC Form 13F manager universe artifacts."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_universe.identity import (
    SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION,
    SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
    SEC_13F_MANAGER_UNIVERSE_SELECTION_POLICY,
    dataset_raw_record_id,
    dataset_revision_id,
    snapshot_id,
    snapshot_raw_record_id,
)
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    ALLOWED_SUBMISSION_FORMS,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class Sec13FDataSetRevision(_Strict):
    """Immutable audit record of one downloaded official Form 13F dataset archive."""

    revision_id: UUID
    raw_record_id: UUID
    dataset_url: NonEmptyStr
    period_start: date
    period_end: date
    content_sha256: NonEmptyStr
    size_bytes: int = Field(gt=0, le=160 * 1024 * 1024)
    retrieved_at: UTCDateTime
    available_at: UTCDateTime
    schema_version: NonEmptyStr = SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION

    @field_validator("content_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        lowered = value.lower().strip()
        if not _SHA256_RE.match(lowered):
            raise ValueError(f"content_sha256 must be a 64-character lowercase hex string: {value}")
        return lowered

    @model_validator(mode="after")
    def validate_revision_invariants(self) -> Sec13FDataSetRevision:
        if self.period_start > self.period_end:
            raise ValueError(
                f"period_start ({self.period_start}) cannot be after period_end ({self.period_end})"
            )
        if self.available_at != self.retrieved_at:
            raise ValueError("available_at must equal retrieved_at for official dataset downloads")
        expected_rev = dataset_revision_id(
            period_start=self.period_start,
            period_end=self.period_end,
            content_sha256=self.content_sha256,
            schema_version=self.schema_version,
        )
        if self.revision_id != expected_rev:
            raise ValueError(
                f"revision_id {self.revision_id} does not match expected {expected_rev}"
            )
        expected_raw = dataset_raw_record_id(self.revision_id)
        if self.raw_record_id != expected_raw:
            raise ValueError(
                f"raw_record_id {self.raw_record_id} does not match expected {expected_raw}"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        dataset_url: str,
        period_start: date,
        period_end: date,
        content_sha256: str,
        size_bytes: int,
        retrieved_at: UTCDateTime,
        schema_version: str = SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION,
    ) -> Sec13FDataSetRevision:
        rev_id = dataset_revision_id(
            period_start=period_start,
            period_end=period_end,
            content_sha256=content_sha256,
            schema_version=schema_version,
        )
        raw_id = dataset_raw_record_id(rev_id)
        return cls(
            revision_id=rev_id,
            raw_record_id=raw_id,
            dataset_url=dataset_url,
            period_start=period_start,
            period_end=period_end,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            retrieved_at=retrieved_at,
            available_at=retrieved_at,
            schema_version=schema_version,
        )


class Sec13FManagerCandidate(_Strict):
    """Operational candidate manager discovered for an authorized catalog CUSIP."""

    candidate_id: UUID
    dataset_revision_id: UUID
    asset_id: NonEmptyStr
    cusip: NonEmptyStr
    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    accession: NonEmptyStr
    form: NonEmptyStr
    filing_date: date
    report_period: date
    value_as_filed: Decimal = Field(ge=0)
    value_unit: Literal["usd_thousands_as_filed"] = "usd_thousands_as_filed"
    is_amendment: bool = False
    is_selected: bool = False
    selection_rank: int | None = Field(default=None, ge=1)
    accession_lineage: tuple[NonEmptyStr, ...] = ()

    @field_validator("manager_cik")
    @classmethod
    def validate_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @field_validator("cusip")
    @classmethod
    def validate_cusip(cls, value: str) -> str:
        upper = value.upper().strip()
        if not _CUSIP_RE.match(upper):
            raise ValueError(f"cusip must be a 9-character alphanumeric identifier: {value}")
        return upper

    @field_validator("form")
    @classmethod
    def validate_form(cls, value: str) -> str:
        upper = value.upper().strip()
        if upper not in ALLOWED_SUBMISSION_FORMS:
            raise ValueError(f"form must be one of {ALLOWED_SUBMISSION_FORMS}: {value}")
        return upper

    @model_validator(mode="after")
    def validate_candidate_invariants(self) -> Sec13FManagerCandidate:
        if self.is_selected and self.selection_rank is None:
            raise ValueError("selection_rank is required when is_selected is True")
        if not self.is_selected and self.selection_rank is not None:
            raise ValueError("selection_rank must be None when is_selected is False")
        return self


class Sec13FManagerUniverseSnapshot(_Strict):
    """Complete, immutable snapshot of discovered and prioritized 13F managers."""

    snapshot_id: UUID
    raw_record_id: UUID
    dataset_revision_id: UUID
    dataset_sha256: NonEmptyStr
    policy_version: NonEmptyStr = SEC_13F_MANAGER_UNIVERSE_SELECTION_POLICY
    period_start: date
    period_end: date
    retrieved_at: UTCDateTime
    available_at: UTCDateTime
    event_time: UTCDateTime
    eligible_asset_count: int = Field(ge=0)
    matched_asset_count: int = Field(ge=0)
    candidate_manager_count: int = Field(ge=0)
    selected_manager_count: int = Field(ge=0)
    unselected_manager_count: int = Field(ge=0)
    max_managers_per_asset: int = Field(default=25, ge=1)
    coverage_complete: bool = True
    covered_cusips: tuple[NonEmptyStr, ...] = ()
    missing_cusips: tuple[NonEmptyStr, ...] = ()
    candidates: tuple[Sec13FManagerCandidate, ...] = ()
    schema_version: NonEmptyStr = SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION

    @model_validator(mode="after")
    def validate_snapshot_invariants(self) -> Sec13FManagerUniverseSnapshot:
        if self.available_at != self.retrieved_at:
            raise ValueError("available_at must equal retrieved_at for snapshot")
        expected_raw = snapshot_raw_record_id(self.snapshot_id)
        if self.raw_record_id != expected_raw:
            raise ValueError(
                f"raw_record_id {self.raw_record_id} does not match expected {expected_raw}"
            )
        if self.matched_asset_count > self.eligible_asset_count:
            raise ValueError("matched_asset_count cannot exceed eligible_asset_count")
        if (
            self.selected_manager_count + self.unselected_manager_count
            != self.candidate_manager_count
        ):
            raise ValueError(
                "selected + unselected manager counts must equal candidate_manager_count"
            )
        if len(self.candidates) != self.candidate_manager_count:
            raise ValueError("candidates length must equal candidate_manager_count")
        return self

    @classmethod
    def create(
        cls,
        *,
        dataset_revision_id: UUID,
        dataset_sha256: str,
        catalog_version: int | str,
        period_start: date,
        period_end: date,
        retrieved_at: UTCDateTime,
        event_time: UTCDateTime,
        eligible_asset_count: int,
        matched_asset_count: int,
        candidate_manager_count: int,
        selected_manager_count: int,
        unselected_manager_count: int,
        max_managers_per_asset: int = 25,
        coverage_complete: bool = True,
        covered_cusips: tuple[str, ...] = (),
        missing_cusips: tuple[str, ...] = (),
        candidates: tuple[Sec13FManagerCandidate, ...] = (),
        policy_version: str = SEC_13F_MANAGER_UNIVERSE_SELECTION_POLICY,
        schema_version: str = SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
    ) -> Sec13FManagerUniverseSnapshot:
        snap_id = snapshot_id(
            dataset_revision_id=dataset_revision_id,
            policy_version=policy_version,
            catalog_version=catalog_version,
            covered_cusips=covered_cusips,
            schema_version=schema_version,
        )
        raw_id = snapshot_raw_record_id(snap_id)
        return cls(
            snapshot_id=snap_id,
            raw_record_id=raw_id,
            dataset_revision_id=dataset_revision_id,
            dataset_sha256=dataset_sha256,
            policy_version=policy_version,
            period_start=period_start,
            period_end=period_end,
            retrieved_at=retrieved_at,
            available_at=retrieved_at,
            event_time=event_time,
            eligible_asset_count=eligible_asset_count,
            matched_asset_count=matched_asset_count,
            candidate_manager_count=candidate_manager_count,
            selected_manager_count=selected_manager_count,
            unselected_manager_count=unselected_manager_count,
            max_managers_per_asset=max_managers_per_asset,
            coverage_complete=coverage_complete,
            covered_cusips=covered_cusips,
            missing_cusips=missing_cusips,
            candidates=candidates,
            schema_version=schema_version,
        )
