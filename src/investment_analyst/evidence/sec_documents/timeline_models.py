"""Strict, immutable models for SEC document timeline search and point-in-time enumeration."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import (
    SUPPORTED_SEC_FORMS,
    normalize_cik,
)

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecDocumentTimelineQuery(_FrozenContract):
    """Point-in-time search criteria for official SEC document revisions."""

    known_at: UTCDateTime
    asset_ids: tuple[NonEmptyStr, ...] = ()
    filer_ciks: tuple[NonEmptyStr, ...] = ()
    forms: tuple[NonEmptyStr, ...] = ()
    accession: NonEmptyStr | None = None
    available_from: date | None = None
    available_to: date | None = None
    limit: int | None = Field(default=None, gt=0, le=1000)

    @field_validator("asset_ids", "filer_ciks", "forms", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: object) -> object:
        if isinstance(value, (list, set, frozenset)):
            return tuple(value)
        return value

    @field_validator("asset_ids")
    @classmethod
    def _validate_asset_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    @field_validator("filer_ciks")
    @classmethod
    def _validate_filer_ciks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(normalize_cik(v) for v in values))

    @field_validator("forms")
    @classmethod
    def _validate_forms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for form in values:
            if form not in SUPPORTED_SEC_FORMS:
                raise ValueError(f"form {form!r} is outside the SEC corpus v1 family")
        return tuple(dict.fromkeys(values))

    @field_validator("accession")
    @classmethod
    def _validate_accession(cls, value: str | None) -> str | None:
        if value is not None and not _ACCESSION.fullmatch(value):
            raise ValueError("accession must use the SEC accession format")
        return value

    @model_validator(mode="after")
    def _validate_scope_and_dates(self) -> SecDocumentTimelineQuery:
        if not self.asset_ids and not self.filer_ciks:
            raise ValueError("query requires at least one asset_id or filer_cik scope")
        if (
            self.available_from is not None
            and self.available_to is not None
            and self.available_from > self.available_to
        ):
            raise ValueError("available_from must be on or before available_to")
        return self


class SecDocumentTimelineEntry(_FrozenContract):
    """One immutable revision entry in a point-in-time document timeline."""

    family: Literal["asset_document", "filer_document"]
    revision_id: UUID
    asset_id: NonEmptyStr | None = None
    filer_cik: NonEmptyStr
    form: NonEmptyStr
    accession: NonEmptyStr
    document_name: NonEmptyStr
    filing_date: date
    report_date: date | None = None
    accepted_at: UTCDateTime
    available_at: UTCDateTime
    content_sha256: NonEmptyStr
    content_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    source_url: NonEmptyStr
    is_amendment: bool

    @field_validator("filer_cik")
    @classmethod
    def _validate_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @field_validator("accession")
    @classmethod
    def _validate_accession(cls, value: str) -> str:
        if not _ACCESSION.fullmatch(value):
            raise ValueError("accession must use the SEC accession format")
        return value

    @field_validator("form")
    @classmethod
    def _validate_form(cls, value: str) -> str:
        if value not in SUPPORTED_SEC_FORMS:
            raise ValueError(f"form {value!r} is outside the SEC corpus v1 family")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _validate_checksum(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _validate_family_and_invariants(self) -> SecDocumentTimelineEntry:
        if self.family == "asset_document" and self.asset_id is None:
            raise ValueError("asset_id is required for asset_document family")
        if self.family == "filer_document" and self.asset_id is not None:
            raise ValueError("asset_id must not be present for filer_document family")
        if self.is_amendment != self.form.endswith("/A"):
            raise ValueError("amendment flag must match the SEC form")
        return self


class SecDocumentTimelineResult(_FrozenContract):
    """Deterministic timeline result for a point-in-time document query."""

    state: Literal["found", "missing"]
    known_at: UTCDateTime
    entries: tuple[SecDocumentTimelineEntry, ...] = ()
    matched_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    legacy_records_excluded: int = Field(default=0, ge=0)
    truncated: bool = False

    @field_validator("entries", mode="before")
    @classmethod
    def _coerce_entries(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _validate_state_and_counts(self) -> SecDocumentTimelineResult:
        if self.returned_count != len(self.entries):
            raise ValueError("returned_count must match the number of entries")
        if self.truncated != (self.returned_count < self.matched_count):
            raise ValueError("truncated must be true if and only if returned_count < matched_count")
        if self.state == "missing" and (self.matched_count != 0 or len(self.entries) != 0):
            raise ValueError("missing result cannot contain matched entries")
        if self.state == "found" and (self.matched_count == 0 or len(self.entries) == 0):
            raise ValueError("found result requires at least one matched entry")
        return self
