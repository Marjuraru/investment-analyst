"""Strict, immutable identities for primary SEC filing documents."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

SEC_DOCUMENT_SOURCE_ID = "sec-edgar:primary-documents"
SEC_DOCUMENT_SCHEMA_VERSION = "sec-document-revision-v1"
SEC_DOCUMENT_SCHEMA_VERSION_V2 = "sec-document-revision-v2"
REVISION_SCHEMA_VERSION = "sec-document-revision-v1"
REVISION_SCHEMA_VERSION_V2 = "sec-document-revision-v2"
FINANCIAL_SEC_FORMS = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
)
SUPPORTED_SEC_FORMS = FINANCIAL_SEC_FORMS | frozenset({"3", "3/A", "4", "4/A", "5", "5/A"})
_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_FILING_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-filing:v1")
_DOCUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-document:v1")
_REVISION_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-revision:v1")
_RAW_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-document-raw:v1")


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


def normalize_cik(value: str) -> str:
    if not value.isdecimal() or len(value) > 10:
        raise ValueError("filer_cik must contain at most ten decimal digits")
    return value.zfill(10)


class SecFiling(_FrozenContract):
    filing_id: UUID
    filer_cik: NonEmptyStr
    accession: NonEmptyStr
    form: NonEmptyStr
    filing_date: date
    report_date: date
    accepted_at: UTCDateTime
    is_amendment: bool

    @field_validator("filer_cik")
    @classmethod
    def validate_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @field_validator("accession")
    @classmethod
    def validate_accession(cls, value: str) -> str:
        if not _ACCESSION.fullmatch(value):
            raise ValueError("accession must use the SEC accession format")
        return value

    @field_validator("form")
    @classmethod
    def validate_form(cls, value: str) -> str:
        if value not in SUPPORTED_SEC_FORMS:
            raise ValueError("form is outside the SEC corpus v1 family")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> SecFiling:
        if self.is_amendment != self.form.endswith("/A"):
            raise ValueError("amendment flag must match the SEC form")
        if self.filing_id != self.expected_id(self.filer_cik, self.accession):
            raise ValueError("filing_id does not match the canonical SEC filing identity")
        return self

    @staticmethod
    def expected_id(filer_cik: str, accession: str) -> UUID:
        return uuid5(_FILING_NAMESPACE, f"SEC|{normalize_cik(filer_cik)}|{accession}")


class SecLogicalDocument(_FrozenContract):
    document_id: UUID
    filing: SecFiling
    name: NonEmptyStr
    role: Literal["primary"] = "primary"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if (
            not _DOCUMENT_NAME.fullmatch(value)
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("primary document name is invalid")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> SecLogicalDocument:
        if self.document_id != self.expected_id(self.filing.filing_id, self.name):
            raise ValueError("document_id does not match the canonical document identity")
        return self

    @staticmethod
    def expected_id(filing_id: UUID, name: str) -> UUID:
        return uuid5(_DOCUMENT_NAMESPACE, f"{filing_id}|{name}|primary")


class SecDocumentRevision(_FrozenContract):
    revision_id: UUID
    asset_id: NonEmptyStr
    document: SecLogicalDocument
    raw_record_id: UUID
    discovery_raw_record_id: UUID
    content_sha256: NonEmptyStr
    content_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    available_at: UTCDateTime
    retrieved_at: UTCDateTime
    source_url: NonEmptyStr
    revision_schema_version: Literal["sec-document-revision-v1", "sec-document-revision-v2"] = (
        REVISION_SCHEMA_VERSION
    )

    @field_validator("content_sha256")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> SecDocumentRevision:
        expected_revision = self.expected_id(
            self.document.document_id, self.content_sha256, self.revision_schema_version
        )
        if self.revision_id != expected_revision:
            raise ValueError("revision_id does not match the canonical revision identity")
        if self.raw_record_id != self.expected_raw_record_id(self.revision_id):
            raise ValueError("raw_record_id does not match the revision lineage identity")
        if (
            self.revision_schema_version == REVISION_SCHEMA_VERSION
            and self.available_at != self.retrieved_at
        ):
            raise ValueError("available_at must equal the first demonstrated retrieval time")
        if (
            self.revision_schema_version == REVISION_SCHEMA_VERSION_V2
            and self.available_at != self.document.filing.accepted_at
        ):
            raise ValueError("v2 available_at must equal SEC filing acceptance")
        return self

    @staticmethod
    def expected_id(document_id: UUID, content_sha256: str, schema_version: str) -> UUID:
        return uuid5(_REVISION_NAMESPACE, f"{document_id}|{content_sha256}|{schema_version}")

    @staticmethod
    def expected_raw_record_id(revision_id: UUID) -> UUID:
        return uuid5(_RAW_NAMESPACE, f"{revision_id}|raw-record")


class SecDocumentQuery(_FrozenContract):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    form: NonEmptyStr | None = None
    accession: NonEmptyStr | None = None
    revision_id: UUID | None = None
    include_content: bool = False

    @field_validator("form")
    @classmethod
    def validate_optional_form(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_SEC_FORMS:
            raise ValueError("form is outside the SEC corpus v1 family")
        return value

    @field_validator("accession")
    @classmethod
    def validate_optional_accession(cls, value: str | None) -> str | None:
        if value is not None and not _ACCESSION.fullmatch(value):
            raise ValueError("accession must use the SEC accession format")
        return value


class SecDocumentReplay(_FrozenContract):
    state: Literal["found", "missing"]
    revision: SecDocumentRevision | None = None
    content: bytes | None = None
    legacy_records_excluded: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_state(self) -> SecDocumentReplay:
        if self.state == "missing" and (self.revision is not None or self.content is not None):
            raise ValueError("missing replay cannot contain a revision or content")
        if self.state == "found" and self.revision is None:
            raise ValueError("found replay requires a revision")
        if self.content is not None and self.revision is None:
            raise ValueError("content requires a revision")
        return self
