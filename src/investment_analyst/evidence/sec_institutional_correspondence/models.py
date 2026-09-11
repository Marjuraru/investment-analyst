"""Strict contracts for a verifiable, row-scoped Form 13F correspondence claim."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_correspondence.identity import (
    correspondence_id,
    correspondence_raw_record_id,
)

ROW_CORRESPONDENCE_SOURCE_ID = "sec-edgar:institutional-row-correspondence"
ROW_CORRESPONDENCE_SCHEMA_VERSION = "sec-institutional-row-correspondence-v1"
ROW_CORRESPONDENCE_POLICY_VERSION = "sec-institutional-row-correspondence-policy-v1"

_CUSIP = re.compile(r"^[0-9A-Z*@#]{9}$")


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalRowCorrespondence(_Strict):
    """One exact proof that an as-filed 13F row matches one catalog asset for its period.

    The validity window is closed and not configurable: it covers exactly the reported period, so
    the claim never asserts that the CUSIP or the title of class is valid before, after, or for any
    other filing.
    """

    correspondence_id: UUID
    raw_record_id: UUID
    asset_id: NonEmptyStr
    cusip: NonEmptyStr
    title_of_class: NonEmptyStr
    report_period: date
    effective_from: date
    effective_to: date
    manager_cik: NonEmptyStr
    report_id: UUID
    artifact_id: UUID
    row_id: UUID
    universe_snapshot_id: UUID
    dataset_revision_id: UUID
    candidate_id: UUID
    available_at: UTCDateTime
    recorded_at: UTCDateTime
    policy_version: Literal["sec-institutional-row-correspondence-policy-v1"] = (
        ROW_CORRESPONDENCE_POLICY_VERSION
    )
    schema_version: Literal["sec-institutional-row-correspondence-v1"] = (
        ROW_CORRESPONDENCE_SCHEMA_VERSION
    )

    @field_validator("cusip")
    @classmethod
    def valid_cusip(cls, value: str) -> str:
        if not _CUSIP.fullmatch(value):
            raise ValueError("CUSIP must contain exactly nine declared characters")
        return value

    @field_validator("manager_cik")
    @classmethod
    def valid_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def valid(self) -> SecInstitutionalRowCorrespondence:
        if self.effective_from != self.report_period:
            raise ValueError("effective_from must equal the reported period")
        if self.effective_to != self.report_period + timedelta(days=1):
            raise ValueError("effective_to must be exactly the day after the reported period")
        if self.recorded_at < self.available_at:
            raise ValueError("recorded_at must not precede available_at")
        if self.correspondence_id != self.expected_id(
            asset_id=self.asset_id,
            cusip=self.cusip,
            title_of_class=self.title_of_class,
            report_period=self.report_period,
            available_at=self.available_at,
            universe_snapshot_id=self.universe_snapshot_id,
            candidate_id=self.candidate_id,
            artifact_id=self.artifact_id,
            row_id=self.row_id,
            policy_version=self.policy_version,
            schema_version=self.schema_version,
        ):
            raise ValueError("row correspondence identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.correspondence_id):
            raise ValueError("row correspondence raw identity is invalid")
        return self

    @staticmethod
    def expected_id(**values: object) -> UUID:
        return correspondence_id(**values)  # type: ignore[arg-type]

    @staticmethod
    def expected_raw_record_id(value: UUID) -> UUID:
        return correspondence_raw_record_id(value)

    @classmethod
    def claim(
        cls,
        *,
        asset_id: str,
        cusip: str,
        title_of_class: str,
        report_period: date,
        manager_cik: str,
        report_id: UUID,
        artifact_id: UUID,
        row_id: UUID,
        universe_snapshot_id: UUID,
        dataset_revision_id: UUID,
        candidate_id: UUID,
        available_at: datetime,
        recorded_at: datetime,
    ) -> SecInstitutionalRowCorrespondence:
        """Build one claim with deterministic identity and the closed validity window."""
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("row correspondence available_at must include timezone")
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("row correspondence recorded_at must include timezone")
        normalized_available = available_at.astimezone(UTC)
        identifier = correspondence_id(
            asset_id=asset_id,
            cusip=cusip,
            title_of_class=title_of_class,
            report_period=report_period,
            available_at=normalized_available,
            universe_snapshot_id=universe_snapshot_id,
            candidate_id=candidate_id,
            artifact_id=artifact_id,
            row_id=row_id,
            policy_version=ROW_CORRESPONDENCE_POLICY_VERSION,
            schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION,
        )
        return cls(
            correspondence_id=identifier,
            raw_record_id=correspondence_raw_record_id(identifier),
            asset_id=asset_id,
            cusip=cusip,
            title_of_class=title_of_class,
            report_period=report_period,
            effective_from=report_period,
            effective_to=report_period + timedelta(days=1),
            manager_cik=manager_cik,
            report_id=report_id,
            artifact_id=artifact_id,
            row_id=row_id,
            universe_snapshot_id=universe_snapshot_id,
            dataset_revision_id=dataset_revision_id,
            candidate_id=candidate_id,
            available_at=normalized_available,
            recorded_at=recorded_at.astimezone(UTC),
        )

    @property
    def event_time(self) -> datetime:
        return datetime.combine(self.report_period, time.min, tzinfo=UTC)

    def covers(self, report_period: date | None) -> bool:
        """Return whether the closed window covers exactly this reported period."""
        return report_period is not None and report_period == self.report_period


def same_evidence(
    first: SecInstitutionalRowCorrespondence, second: SecInstitutionalRowCorrespondence
) -> bool:
    """Compare two claims by their evidence content, excluding the recording clock.

    ``recorded_at`` describes when the proof was written, not what it proves, so an equivalent
    re-materialization reuses the persisted claim instead of rewriting it. Any other difference is
    a real conflict and fails closed.
    """
    return first.model_dump(exclude={"recorded_at"}) == second.model_dump(exclude={"recorded_at"})


__all__ = [
    "ROW_CORRESPONDENCE_POLICY_VERSION",
    "ROW_CORRESPONDENCE_SCHEMA_VERSION",
    "ROW_CORRESPONDENCE_SOURCE_ID",
    "SecInstitutionalRowCorrespondence",
    "same_evidence",
]
