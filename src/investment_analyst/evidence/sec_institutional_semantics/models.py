"""Strict contracts for a lossless, as-filed Form 13F representation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import (
    INSTITUTIONAL_HOLDINGS_FORMS,
    SecFilerDocumentRevision,
    normalize_cik,
)
from investment_analyst.evidence.sec_institutional_semantics.identity import (
    artifact_id,
    raw_record_id,
    row_id,
)

SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID = "sec-edgar:institutional-holdings-semantics"
SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION = "sec-institutional-holdings-semantics-v2"
SEC_INSTITUTIONAL_SEMANTICS_PARSER_VERSION = "sec-institutional-semantics-parser-v1"


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalOtherManager(_Strict):
    """One literally declared manager reference; names never establish identity."""

    sequence_number: NonEmptyStr | None = None
    name: NonEmptyStr | None = None
    cik: NonEmptyStr | None = None
    file_number: NonEmptyStr | None = None

    @field_validator("cik")
    @classmethod
    def normalize_optional_cik(cls, value: str | None) -> str | None:
        return normalize_cik(value) if value is not None else None


class InstitutionalSemanticsRow(_Strict):
    """A single original informationTable row, never aggregated by CUSIP/class."""

    row_id: UUID
    row_number: int = Field(ge=1, le=100_000)
    issuer_name: NonEmptyStr
    title_of_class: NonEmptyStr
    cusip: NonEmptyStr
    figi: NonEmptyStr | None = None
    value_as_reported: Decimal = Field(ge=0)
    quantity: Decimal = Field(ge=0)
    quantity_type: NonEmptyStr | None = None
    put_call: NonEmptyStr | None = None
    investment_discretion: NonEmptyStr | None = None
    other_manager: NonEmptyStr | None = None
    other_manager_sequence_references: tuple[NonEmptyStr, ...] = ()
    voting_sole: Decimal | None = Field(default=None, ge=0)
    voting_shared: Decimal | None = Field(default=None, ge=0)
    voting_none: Decimal | None = Field(default=None, ge=0)
    limitations: tuple[
        Literal[
            "optional_not_reported",
            "unsupported_code",
            "unresolved_manager_reference",
        ],
        ...,
    ] = ()


class InstitutionalHoldingsSemantics(_Strict):
    """A complete enriched report bundle stored in one RawRecord."""

    artifact_id: UUID
    raw_record_id: UUID
    parent_report_id: UUID
    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    cover_revision: SecFilerDocumentRevision
    information_table_revision: SecFilerDocumentRevision
    accession: NonEmptyStr
    form: NonEmptyStr
    report_period: date | None
    xml_schema_version: NonEmptyStr | None = None
    report_type: NonEmptyStr | None = None
    is_amendment: bool
    amendment_number: NonEmptyStr | None = None
    amendment_type: NonEmptyStr | None = None
    confidential_omitted: bool | None = None
    declared_entry_total: int | None = Field(default=None, ge=0)
    declared_value_total: Decimal | None = Field(default=None, ge=0)
    other_managers_included: tuple[InstitutionalOtherManager, ...] = ()
    reporting_managers: tuple[InstitutionalOtherManager, ...] = ()
    rows: tuple[InstitutionalSemanticsRow, ...] = ()
    value_unit: Literal["sec_13f_as_reported"] = "sec_13f_as_reported"
    monetary_scale_status: Literal["unresolved"] = "unresolved"
    available_at: UTCDateTime
    parsed_at: UTCDateTime
    parser_version: Literal["sec-institutional-semantics-parser-v1"] = (
        SEC_INSTITUTIONAL_SEMANTICS_PARSER_VERSION
    )
    schema_version: Literal["sec-institutional-holdings-semantics-v2"] = (
        SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION
    )

    @field_validator("manager_cik")
    @classmethod
    def normalize_manager_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def validate_lineage_and_identity(self) -> InstitutionalHoldingsSemantics:
        cover_filing = self.cover_revision.document.filing
        table_filing = self.information_table_revision.document.filing
        if cover_filing != table_filing or cover_filing.form not in INSTITUTIONAL_HOLDINGS_FORMS:
            raise ValueError("semantics revisions do not identify one Form 13F filing")
        if (
            self.manager_cik != cover_filing.filer_cik
            or self.accession != cover_filing.accession
            or self.form != cover_filing.form
            or self.report_period != cover_filing.report_date
            or self.available_at != cover_filing.accepted_at
        ):
            raise ValueError("semantics header conflicts with filing lineage")
        expected = self.expected_id(
            self.parent_report_id,
            self.cover_revision.revision_id,
            self.information_table_revision.revision_id,
        )
        if self.artifact_id != expected or self.raw_record_id != self.expected_raw_record_id(
            expected
        ):
            raise ValueError("semantics identity is invalid")
        if len(self.rows) > 100_000:
            raise ValueError("semantics report exceeds row limit")
        expected_rows = tuple(range(1, len(self.rows) + 1))
        if tuple(item.row_number for item in self.rows) != expected_rows:
            raise ValueError("semantics rows must retain consecutive original ordinals")
        if any(
            item.row_id != self.expected_row_id(self.artifact_id, item.row_number)
            for item in self.rows
        ):
            raise ValueError("semantics row identity is invalid")
        return self

    @staticmethod
    def expected_id(
        parent_report_id: UUID, cover_revision_id: UUID, information_table_revision_id: UUID
    ) -> UUID:
        return artifact_id(
            parent_report_id,
            cover_revision_id,
            information_table_revision_id,
            SEC_INSTITUTIONAL_SEMANTICS_PARSER_VERSION,
            SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
        )

    @staticmethod
    def expected_row_id(artifact_id: UUID, row_number: int) -> UUID:
        return row_id(artifact_id, row_number)

    @staticmethod
    def expected_raw_record_id(artifact_id: UUID) -> UUID:
        return raw_record_id(artifact_id)

    def semantic_document(self) -> dict[str, object]:
        """Return identity-stable content, deliberately excluding the derivation clock."""
        document = self.model_dump(mode="json")
        document.pop("parsed_at")
        return document


class InstitutionalSemanticsHeader(_Strict):
    """Report metadata suitable for a paginated response without its complete rows."""

    artifact_id: UUID
    parent_report_id: UUID
    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    accession: NonEmptyStr
    form: NonEmptyStr
    report_period: date | None
    is_amendment: bool
    amendment_number: NonEmptyStr | None = None
    amendment_type: NonEmptyStr | None = None
    value_unit: Literal["sec_13f_as_reported"]
    monetary_scale_status: Literal["unresolved"]
    available_at: UTCDateTime

    @classmethod
    def from_artifact(cls, item: InstitutionalHoldingsSemantics) -> InstitutionalSemanticsHeader:
        return cls(
            artifact_id=item.artifact_id,
            parent_report_id=item.parent_report_id,
            manager_cik=item.manager_cik,
            manager_name=item.manager_name,
            accession=item.accession,
            form=item.form,
            report_period=item.report_period,
            is_amendment=item.is_amendment,
            amendment_number=item.amendment_number,
            amendment_type=item.amendment_type,
            value_unit=item.value_unit,
            monetary_scale_status=item.monetary_scale_status,
            available_at=item.available_at,
        )


class InstitutionalHoldingsSemanticsQuery(_Strict):
    manager_cik: NonEmptyStr
    report_ids: tuple[UUID, ...]
    known_at: UTCDateTime
    cusip: NonEmptyStr | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("manager_cik")
    @classmethod
    def normalize_query_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def validate_ids(self) -> InstitutionalHoldingsSemanticsQuery:
        if (
            not self.report_ids
            or len(set(self.report_ids)) != len(self.report_ids)
            or len(self.report_ids) > 20
        ):
            raise ValueError("report_ids must contain one to twenty unique values")
        return self
