"""Strict Section 16 ownership contracts."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    normalize_cik,
)

OWNERSHIP_FORMS = frozenset({"3", "3/A", "4", "4/A", "5", "5/A"})
OWNERSHIP_SOURCE_ID = "sec-edgar:section16-ownership"
OWNERSHIP_SCHEMA_VERSION = "sec-ownership-statement-v1"
OWNERSHIP_SCHEMA_VERSION_V2 = "sec-ownership-statement-v2"
OWNERSHIP_OUTCOME_SCHEMA_VERSION = "sec-ownership-outcome-v1"
OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2 = "sec-ownership-outcome-v2"
_STATEMENTS = uuid5(NAMESPACE_URL, "investment-analyst:sec-ownership-statement:v1")
_OWNERS = uuid5(NAMESPACE_URL, "investment-analyst:sec-reporting-owner:v1")
_ENTRIES = uuid5(NAMESPACE_URL, "investment-analyst:sec-ownership-entry:v1")
_RAW = uuid5(NAMESPACE_URL, "investment-analyst:sec-ownership-raw:v1")
_OUTCOMES = uuid5(NAMESPACE_URL, "investment-analyst:sec-ownership-outcome:v1")


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class ReportingOwner(_Strict):
    reporting_owner_id: UUID
    cik: NonEmptyStr
    name: NonEmptyStr
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent_owner: bool = False
    is_other: bool = False
    officer_title: str | None = None
    other_text: str | None = None

    @field_validator("cik")
    @classmethod
    def normalized_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def identity(self) -> ReportingOwner:
        if self.reporting_owner_id != self.expected_id(self.cik):
            raise ValueError("reporting owner identity is invalid")
        return self

    @staticmethod
    def expected_id(cik: str) -> UUID:
        return uuid5(_OWNERS, normalize_cik(cik))


class OwnershipEntry(_Strict):
    entry_id: UUID
    table: Literal["non_derivative", "derivative"]
    kind: Literal["holding", "transaction"]
    ordinal: int = Field(ge=0)
    owner_cik: NonEmptyStr
    security_title: NonEmptyStr
    transaction_date: date | None = None
    transaction_code: str | None = None
    acquired_disposed: Literal["A", "D"] | None = None
    shares: Decimal | None = None
    price_per_share: Decimal | None = None
    shares_owned_following: Decimal | None = None
    ownership_nature: Literal["D", "I"] | None = None
    footnote_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("owner_cik")
    @classmethod
    def normalized_owner(cls, value: str) -> str:
        return normalize_cik(value)

    @staticmethod
    def expected_id(statement_id: UUID, table: str, kind: str, ordinal: int) -> UUID:
        return uuid5(_ENTRIES, f"{statement_id}|{table}|{kind}|{ordinal}")


class OwnershipStatement(_Strict):
    statement_id: UUID
    raw_record_id: UUID
    asset_id: NonEmptyStr
    document_revision: SecDocumentRevision
    form: NonEmptyStr
    period_of_report: date
    issuer_cik: NonEmptyStr
    issuer_name: NonEmptyStr
    issuer_trading_symbol: str | None = None
    not_subject_to_section16: bool = False
    no_securities_owned: bool = False
    remarks: str | None = None
    reporting_owners: tuple[ReportingOwner, ...]
    entries: tuple[OwnershipEntry, ...]
    footnotes: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    available_at: UTCDateTime
    parsed_at: UTCDateTime
    schema_version: Literal["sec-ownership-statement-v1", "sec-ownership-statement-v2"] = (
        OWNERSHIP_SCHEMA_VERSION
    )

    @field_validator("form")
    @classmethod
    def allowed_form(cls, value: str) -> str:
        if value not in OWNERSHIP_FORMS:
            raise ValueError("form is outside Section 16")
        return value

    @field_validator("issuer_cik")
    @classmethod
    def normalized_issuer(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def identity_and_lineage(self) -> OwnershipStatement:
        if self.form != self.document_revision.document.filing.form:
            raise ValueError("statement form conflicts with document")
        if self.issuer_cik != self.document_revision.document.filing.filer_cik:
            raise ValueError("statement issuer conflicts with document")
        if self.available_at != self.document_revision.available_at:
            raise ValueError("statement availability must inherit document")
        revision_schema = self.document_revision.revision_schema_version
        statement_is_v2 = self.schema_version == OWNERSHIP_SCHEMA_VERSION_V2
        revision_is_v2 = revision_schema == REVISION_SCHEMA_VERSION_V2
        if statement_is_v2 != revision_is_v2:
            raise ValueError("statement schema_version must match its document revision schema")
        if self.statement_id != self.expected_id(
            self.document_revision.revision_id, self.schema_version
        ):
            raise ValueError("statement identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.statement_id):
            raise ValueError("statement raw identity is invalid")
        owners = {owner.cik for owner in self.reporting_owners}
        for entry in self.entries:
            if entry.owner_cik not in owners or entry.entry_id != OwnershipEntry.expected_id(
                self.statement_id, entry.table, entry.kind, entry.ordinal
            ):
                raise ValueError("ownership entry lineage is invalid")
            if any(reference not in self.footnotes for reference in entry.footnote_ids):
                raise ValueError("ownership entry has dangling footnote")
        return self

    @staticmethod
    def expected_id(revision_id: UUID, schema_version: str = OWNERSHIP_SCHEMA_VERSION) -> UUID:
        return uuid5(_STATEMENTS, f"{revision_id}|{schema_version}")

    @staticmethod
    def expected_raw_record_id(statement_id: UUID) -> UUID:
        return uuid5(_RAW, f"{statement_id}|raw-record")


class OwnershipResolutionOutcome(_Strict):
    """Versioned evidence of one exact locator or semantic resource evaluation."""

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
    resolver_version: Literal["sec-ownership-resolver-v1", "sec-ownership-resolver-v2"] = (
        "sec-ownership-resolver-v1"
    )
    schema_version: Literal["sec-ownership-outcome-v1", "sec-ownership-outcome-v2"] = (
        OWNERSHIP_OUTCOME_SCHEMA_VERSION
    )

    @field_validator("content_sha256", "manifest_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("outcome checksum is invalid")
        return value

    @model_validator(mode="after")
    def validate_identity_and_time(self) -> OwnershipResolutionOutcome:
        is_v2 = self.resolver_version == "sec-ownership-resolver-v2"
        if is_v2 != (self.schema_version == OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2):
            raise ValueError("outcome schema_version must match resolver_version")
        if not is_v2 and self.available_at != self.retrieved_at:
            raise ValueError("outcome availability must equal retrieval")
        if is_v2 and self.available_at != self.filing.accepted_at:
            raise ValueError("v2 outcome availability must equal SEC filing acceptance")
        if self.outcome_id != self.expected_id(
            self.filing.accession,
            self.resource_name,
            self.content_sha256,
            self.status,
            self.schema_version,
        ):
            raise ValueError("outcome identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.outcome_id):
            raise ValueError("outcome raw identity is invalid")
        return self

    @staticmethod
    def expected_id(
        accession: str,
        name: str,
        content_sha256: str,
        status: str,
        schema_version: str = OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    ) -> UUID:
        # The v1 identity is byte-exact to the historical formula integrated in main: it embeds
        # the literal "v1" marker regardless of any newer field, so already-persisted v1 outcomes
        # keep decoding, verifying, and coexisting without rewriting their identity.
        marker = "v1" if schema_version == OWNERSHIP_OUTCOME_SCHEMA_VERSION else schema_version
        return uuid5(_OUTCOMES, f"{accession}|{name}|{content_sha256}|{status}|{marker}")

    @staticmethod
    def expected_raw_record_id(outcome_id: UUID) -> UUID:
        return uuid5(_RAW, f"{outcome_id}|outcome-raw-record")


class OwnershipQuery(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    form: str | None = None
    accession: str | None = None
    reporting_owner_cik: str | None = None
    transaction_code: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class OwnershipQueryResult(_Strict):
    statements: tuple[OwnershipStatement, ...]
    total_matching: int = Field(ge=0)
    truncated: bool
    legacy_records_excluded: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> OwnershipQueryResult:
        if self.total_matching < len(self.statements):
            raise ValueError("total_matching cannot be lower than returned statements")
        if self.truncated != (self.total_matching > len(self.statements)):
            raise ValueError("truncated must match total_matching")
        return self
