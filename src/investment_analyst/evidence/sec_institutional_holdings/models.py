"""Strict contracts for institutional Form 13F evidence."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import (
    FILER_REVISION_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_FORMS,
    SecFilerDocumentRevision,
    SecFiling,
    normalize_cik,
)

INSTITUTIONAL_HOLDINGS_SOURCE_ID = "sec-edgar:institutional-holdings-13f"
INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION = "sec-institutional-holdings-outcome-v1"
INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION = "sec-institutional-holdings-report-v1"
INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION = "sec-institutional-holding-position-v1"
_OUTCOMES = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-outcome:v1")
_REPORTS = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-report:v1")
_POSITIONS = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-position:v1")
_RAW = uuid5(NAMESPACE_URL, "investment-analyst:sec-institutional-raw:v1")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CUSIP = re.compile(r"^[0-9A-Z*@#]{9}$")


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalHoldingsResolutionOutcome(_Strict):
    outcome_id: UUID
    raw_record_id: UUID
    filing: SecFiling
    discovery_raw_record_id: UUID
    declared_locator: NonEmptyStr
    resource_name: NonEmptyStr
    resource_url: NonEmptyStr
    content_sha256: NonEmptyStr
    content_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    manifest_url: NonEmptyStr
    manifest_sha256: NonEmptyStr
    available_at: UTCDateTime
    retrieved_at: UTCDateTime
    status: Literal["accepted", "rejected"]
    reason_code: NonEmptyStr
    resolver_version: Literal["sec-institutional-holdings-resolver-v1"] = (
        "sec-institutional-holdings-resolver-v1"
    )
    schema_version: Literal["sec-institutional-holdings-outcome-v1"] = (
        INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION
    )

    @field_validator("content_sha256", "manifest_sha256")
    @classmethod
    def checksum(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("institutional holdings checksum is invalid")
        return value

    @model_validator(mode="after")
    def identity_and_time(self) -> InstitutionalHoldingsResolutionOutcome:
        if self.filing.form not in INSTITUTIONAL_HOLDINGS_FORMS:
            raise ValueError("outcome form is outside institutional holdings")
        if self.available_at != self.filing.accepted_at:
            raise ValueError("outcome availability must equal SEC filing acceptance")
        if self.outcome_id != self.expected_id(
            self.filing.accession, self.resource_name, self.content_sha256, self.status
        ):
            raise ValueError("outcome identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.outcome_id):
            raise ValueError("outcome raw identity is invalid")
        return self

    @staticmethod
    def expected_id(accession: str, name: str, content_sha256: str, status: str) -> UUID:
        return uuid5(_OUTCOMES, f"{accession}|{name}|{content_sha256}|{status}|v1")

    @staticmethod
    def expected_raw_record_id(outcome_id: UUID) -> UUID:
        return uuid5(_RAW, f"{outcome_id}|outcome")


class InstitutionalHoldingsReport(_Strict):
    report_id: UUID
    raw_record_id: UUID
    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    file_number: NonEmptyStr | None = None
    report_period: date | None
    cover_revision: SecFilerDocumentRevision
    information_table_revision: SecFilerDocumentRevision
    declared_entry_total: int | None = Field(default=None, ge=0)
    declared_value_total: Decimal | None = Field(default=None, ge=0)
    parsed_entry_total: int = Field(ge=0)
    parsed_value_total: Decimal = Field(ge=0)
    position_values_complete: bool
    entry_total_matches: bool | None
    value_total_matches: bool | None
    available_at: UTCDateTime
    parsed_at: UTCDateTime
    schema_version: Literal["sec-institutional-holdings-report-v1"] = (
        INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION
    )

    @field_validator("manager_cik")
    @classmethod
    def cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def identity_and_lineage(self) -> InstitutionalHoldingsReport:
        cover_filing = self.cover_revision.document.filing
        table_filing = self.information_table_revision.document.filing
        if (
            self.cover_revision.revision_schema_version != FILER_REVISION_SCHEMA_VERSION
            or self.information_table_revision.revision_schema_version
            != FILER_REVISION_SCHEMA_VERSION
        ):
            raise ValueError("report requires filer document revisions")
        if cover_filing != table_filing or cover_filing.form not in INSTITUTIONAL_HOLDINGS_FORMS:
            raise ValueError("report revisions do not identify one Form 13F filing")
        if self.manager_cik != cover_filing.filer_cik:
            raise ValueError("report manager conflicts with its filing")
        if self.report_period != cover_filing.report_date:
            raise ValueError("report period conflicts with its filing")
        if self.available_at != cover_filing.accepted_at:
            raise ValueError("report availability must inherit filing acceptance")
        expected_entry_match = (
            None
            if self.declared_entry_total is None
            else self.declared_entry_total == self.parsed_entry_total
        )
        expected_value_match = (
            None
            if self.declared_value_total is None or not self.position_values_complete
            else self.declared_value_total == self.parsed_value_total
        )
        if self.entry_total_matches != expected_entry_match:
            raise ValueError("entry total comparison is inconsistent")
        if self.value_total_matches != expected_value_match:
            raise ValueError("value total comparison is inconsistent")
        if self.report_id != self.expected_id(
            self.cover_revision.revision_id, self.information_table_revision.revision_id
        ):
            raise ValueError("report identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.report_id):
            raise ValueError("report raw identity is invalid")
        return self

    @staticmethod
    def expected_id(cover_revision_id: UUID, table_revision_id: UUID) -> UUID:
        return uuid5(_REPORTS, f"{cover_revision_id}|{table_revision_id}|v1")

    @staticmethod
    def expected_raw_record_id(report_id: UUID) -> UUID:
        return uuid5(_RAW, f"{report_id}|report")


class InstitutionalHoldingPosition(_Strict):
    position_id: UUID
    raw_record_id: UUID
    report_id: UUID
    information_table_revision: SecFilerDocumentRevision
    row_number: int = Field(ge=1)
    issuer_name: NonEmptyStr
    title_of_class: NonEmptyStr
    cusip: NonEmptyStr
    value: Decimal | None = Field(default=None, ge=0)
    quantity: Decimal | None = Field(default=None, ge=0)
    quantity_type: NonEmptyStr | None = None
    investment_discretion: NonEmptyStr | None = None
    voting_sole: Decimal | None = Field(default=None, ge=0)
    voting_shared: Decimal | None = Field(default=None, ge=0)
    voting_none: Decimal | None = Field(default=None, ge=0)
    available_at: UTCDateTime
    parsed_at: UTCDateTime
    schema_version: Literal["sec-institutional-holding-position-v1"] = (
        INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION
    )

    @field_validator("cusip")
    @classmethod
    def valid_cusip(cls, value: str) -> str:
        if not _CUSIP.fullmatch(value):
            raise ValueError("CUSIP must contain exactly nine declared characters")
        return value

    @model_validator(mode="after")
    def identity_and_lineage(self) -> InstitutionalHoldingPosition:
        filing = self.information_table_revision.document.filing
        if filing.form not in INSTITUTIONAL_HOLDINGS_FORMS:
            raise ValueError("position form is outside institutional holdings")
        if self.available_at != filing.accepted_at:
            raise ValueError("position availability must inherit filing acceptance")
        if self.position_id != self.expected_id(self.report_id, self.row_number):
            raise ValueError("position identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.position_id):
            raise ValueError("position raw identity is invalid")
        return self

    @staticmethod
    def expected_id(report_id: UUID, row_number: int) -> UUID:
        return uuid5(_POSITIONS, f"{report_id}|{row_number}|v1")

    @staticmethod
    def expected_raw_record_id(position_id: UUID) -> UUID:
        return uuid5(_RAW, f"{position_id}|position")


class InstitutionalHoldingsQuery(_Strict):
    manager_cik: NonEmptyStr
    known_at: UTCDateTime
    period_from: date | None = None
    period_to: date | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("manager_cik")
    @classmethod
    def cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def valid_period(self) -> InstitutionalHoldingsQuery:
        if (
            self.period_from is not None
            and self.period_to is not None
            and self.period_from > self.period_to
        ):
            raise ValueError("period_from must not be after period_to")
        return self


class InstitutionalHoldingsQueryResult(_Strict):
    reports: tuple[InstitutionalHoldingsReport, ...]
    positions: tuple[InstitutionalHoldingPosition, ...]
    total_matching: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def result_shape(self) -> InstitutionalHoldingsQueryResult:
        if self.total_matching < len(self.reports):
            raise ValueError("total_matching cannot be lower than returned reports")
        if self.truncated != (self.total_matching > len(self.reports)):
            raise ValueError("truncated must match total_matching")
        report_ids = {report.report_id for report in self.reports}
        if any(position.report_id not in report_ids for position in self.positions):
            raise ValueError("query positions must belong to returned reports")
        return self
