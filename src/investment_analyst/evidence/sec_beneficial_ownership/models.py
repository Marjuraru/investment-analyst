"""Strict contracts for independently persisted beneficial-ownership evidence."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import (
    BENEFICIAL_OWNERSHIP_FORMS,
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    normalize_cik,
)

BENEFICIAL_OWNERSHIP_SOURCE_ID = "sec-edgar:beneficial-ownership-13d-13g"
BENEFICIAL_OWNERSHIP_SCHEMA_VERSION = "sec-beneficial-ownership-statement-v1"
BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION = "sec-beneficial-ownership-outcome-v1"
_STATEMENTS = uuid5(NAMESPACE_URL, "investment-analyst:sec-beneficial-ownership-statement:v1")
_OUTCOMES = uuid5(NAMESPACE_URL, "investment-analyst:sec-beneficial-ownership-outcome:v1")
_RAW = uuid5(NAMESPACE_URL, "investment-analyst:sec-beneficial-ownership-raw:v1")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class BeneficialOwnershipResolutionOutcome(_Strict):
    """Append-only result for one retrieved filing representation."""

    outcome_id: UUID
    raw_record_id: UUID
    asset_id: NonEmptyStr
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
    resolver_version: Literal["sec-beneficial-ownership-resolver-v1"] = (
        "sec-beneficial-ownership-resolver-v1"
    )
    schema_version: Literal["sec-beneficial-ownership-outcome-v1"] = (
        BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION
    )

    @field_validator("content_sha256", "manifest_sha256")
    @classmethod
    def checksum(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("outcome checksum is invalid")
        return value

    @model_validator(mode="after")
    def identity_and_time(self) -> BeneficialOwnershipResolutionOutcome:
        if self.filing.form not in BENEFICIAL_OWNERSHIP_FORMS:
            raise ValueError("outcome form is outside beneficial ownership")
        if self.available_at != self.filing.accepted_at:
            raise ValueError("outcome availability must equal SEC filing acceptance")
        if self.outcome_id != self.expected_id(
            self.filing.accession,
            self.resource_name,
            self.content_sha256,
            self.status,
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
        return uuid5(_RAW, f"{outcome_id}|outcome-raw-record")


class BeneficialOwnershipStatement(_Strict):
    """Declared Schedule 13D/13G ownership without instrument normalization."""

    statement_id: UUID
    raw_record_id: UUID
    asset_id: NonEmptyStr
    document_revision: SecDocumentRevision
    form: NonEmptyStr
    subject_cik: NonEmptyStr
    subject_name: NonEmptyStr
    reporting_person_cik: str | None = None
    reporting_person_name: str | None = None
    event_date: date | None = None
    shares_beneficially_owned: Decimal | None = None
    percent_of_class: Decimal | None = None
    available_at: UTCDateTime
    parsed_at: UTCDateTime
    schema_version: Literal["sec-beneficial-ownership-statement-v1"] = (
        BENEFICIAL_OWNERSHIP_SCHEMA_VERSION
    )

    @field_validator("subject_cik", "reporting_person_cik")
    @classmethod
    def cik(cls, value: str | None) -> str | None:
        return normalize_cik(value) if value is not None else None

    @model_validator(mode="after")
    def identity_and_lineage(self) -> BeneficialOwnershipStatement:
        if self.form not in BENEFICIAL_OWNERSHIP_FORMS:
            raise ValueError("statement form is outside beneficial ownership")
        if self.asset_id != self.document_revision.asset_id:
            raise ValueError("statement asset conflicts with document revision")
        if self.form != self.document_revision.document.filing.form:
            raise ValueError("statement form conflicts with document revision")
        if self.document_revision.revision_schema_version != REVISION_SCHEMA_VERSION_V2:
            raise ValueError("statement requires a v2 document revision")
        if self.available_at != self.document_revision.available_at:
            raise ValueError("statement availability must inherit document")
        if self.statement_id != self.expected_id(self.document_revision.revision_id):
            raise ValueError("statement identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.statement_id):
            raise ValueError("statement raw identity is invalid")
        return self

    @staticmethod
    def expected_id(revision_id: UUID) -> UUID:
        return uuid5(_STATEMENTS, f"{revision_id}|v1")

    @staticmethod
    def expected_raw_record_id(statement_id: UUID) -> UUID:
        return uuid5(_RAW, f"{statement_id}|statement-raw-record")


class BeneficialOwnershipQuery(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    form: str | None = None
    accession: str | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("form")
    @classmethod
    def allowed_form(cls, value: str | None) -> str | None:
        if value is not None and value not in BENEFICIAL_OWNERSHIP_FORMS:
            raise ValueError("form is outside beneficial ownership")
        return value


class BeneficialOwnershipQueryResult(_Strict):
    statements: tuple[BeneficialOwnershipStatement, ...]
    total_matching: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def result_shape(self) -> BeneficialOwnershipQueryResult:
        if self.total_matching < len(self.statements):
            raise ValueError("total_matching cannot be lower than returned statements")
        if self.truncated != (self.total_matching > len(self.statements)):
            raise ValueError("truncated must match total_matching")
        return self
