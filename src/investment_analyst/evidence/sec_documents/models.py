"""Strict, immutable identities for primary SEC filing documents."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

SEC_DOCUMENT_SOURCE_ID = "sec-edgar:primary-documents"
SEC_DOCUMENT_SCHEMA_VERSION = "sec-document-revision-v1"
SEC_DOCUMENT_SCHEMA_VERSION_V2 = "sec-document-revision-v2"
SEC_DOCUMENT_SCHEMA_VERSION_V3 = "sec-document-revision-v3"
SEC_DOCUMENT_SCHEMA_VERSION_V4 = "sec-document-revision-v4"
REVISION_SCHEMA_VERSION = "sec-document-revision-v1"
REVISION_SCHEMA_VERSION_V2 = "sec-document-revision-v2"
METADATA_REVISION_SCHEMA_VERSION = SEC_DOCUMENT_SCHEMA_VERSION_V3
ACQUISITION_REVISION_SCHEMA_VERSION = SEC_DOCUMENT_SCHEMA_VERSION_V4
FINANCIAL_SEC_FORMS = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
)
BENEFICIAL_OWNERSHIP_FORMS = frozenset({"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"})
INSTITUTIONAL_HOLDINGS_FORMS = frozenset({"13F-HR", "13F-HR/A"})
SUPPORTED_SEC_FORMS = (
    FINANCIAL_SEC_FORMS
    | frozenset({"3", "3/A", "4", "4/A", "5", "5/A"})
    | BENEFICIAL_OWNERSHIP_FORMS
    | INSTITUTIONAL_HOLDINGS_FORMS
)
_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_TERMINAL_SCRIPT_ELEMENT = re.compile(
    rb'<script type="text/javascript"  src="([^\"]{1,255})"></script>'
)
_TERMINAL_SCRIPT_PATH = re.compile(r"^/[A-Za-z0-9_/-]{0,254}$")
_BODY_HTML_CLOSING = b"</body></html>"
_ASCII_TRAILING_WHITESPACE = b" \t\r\n\v\f"
_MAX_TERMINAL_SCRIPT_BYTES = 384
_FILING_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-filing:v1")
_DOCUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-document:v1")
_REVISION_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-revision:v1")
_RAW_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-document-raw:v1")
_METADATA_REVISION_NAMESPACE = uuid5(
    NAMESPACE_URL, "investment-analyst:sec-document-metadata-revision:v1"
)
_METADATA_RAW_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-document-metadata-raw:v1")
_ACQUISITION_REVISION_NAMESPACE = uuid5(
    NAMESPACE_URL, "investment-analyst:sec-document-acquisition-revision:v1"
)
_ACQUISITION_RAW_NAMESPACE = uuid5(
    NAMESPACE_URL, "investment-analyst:sec-document-acquisition-raw:v1"
)
FILER_REVISION_SCHEMA_VERSION = "sec-filer-document-revision-v1"
_FILER_REVISION_NAMESPACE = uuid5(
    NAMESPACE_URL, "investment-analyst:sec-filer-document-revision:v1"
)
_FILER_RAW_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-filer-document-raw:v1")


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
    report_date: date | None
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
    def require_report_date_for_existing_families(self) -> SecFiling:
        if (
            self.form in FINANCIAL_SEC_FORMS | frozenset({"3", "3/A", "4", "4/A", "5", "5/A"})
            and self.report_date is None
        ):
            raise ValueError("report_date is required for financial and Section 16 forms")
        return self

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


def sec_document_metadata_sha256(document: SecLogicalDocument) -> str:
    """Hash the complete canonical document metadata, including UTC acceptance time."""
    canonical = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def same_document_metadata_except_accepted_at(
    left: SecLogicalDocument, right: SecLogicalDocument
) -> bool:
    """Return whether two logical documents differ only in the SEC accepted_at field."""
    excluded = {"filing": {"accepted_at"}}
    return left.model_dump(mode="json", exclude=excluded) == right.model_dump(
        mode="json", exclude=excluded
    )


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


class SecDocumentMetadataRevision(_FrozenContract):
    """Append-only correction of filing metadata with unchanged verified document bytes."""

    revision_id: UUID
    asset_id: NonEmptyStr
    document: SecLogicalDocument
    prior_revision_id: UUID
    raw_record_id: UUID
    discovery_raw_record_id: UUID
    content_sha256: NonEmptyStr
    content_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    metadata_sha256: NonEmptyStr
    metadata_observed_at: UTCDateTime
    available_at: UTCDateTime
    retrieved_at: UTCDateTime
    source_url: NonEmptyStr
    revision_schema_version: Literal["sec-document-revision-v3"] = METADATA_REVISION_SCHEMA_VERSION

    @field_validator("content_sha256", "metadata_sha256")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("document digests must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def validate_identity_and_availability(self) -> SecDocumentMetadataRevision:
        expected_revision = self.expected_id(
            self.document.document_id,
            self.content_sha256,
            self.metadata_sha256,
            self.prior_revision_id,
        )
        if self.revision_id != expected_revision:
            raise ValueError("metadata revision_id does not match its canonical identity")
        if self.raw_record_id != self.expected_raw_record_id(self.revision_id):
            raise ValueError("metadata revision raw_record_id does not match its identity")
        if self.prior_revision_id == self.revision_id:
            raise ValueError("metadata revision cannot point to itself")
        if self.metadata_sha256 != sec_document_metadata_sha256(self.document):
            raise ValueError("metadata_sha256 does not match the canonical SEC document")
        expected_available_at = max(
            self.document.filing.accepted_at,
            self.metadata_observed_at,
            self.retrieved_at,
        )
        if self.available_at != expected_available_at:
            raise ValueError("metadata revision availability must preserve all evidence times")
        return self

    @staticmethod
    def expected_id(
        document_id: UUID,
        content_sha256: str,
        metadata_sha256: str,
        prior_revision_id: UUID,
    ) -> UUID:
        identity = {
            "schema_version": METADATA_REVISION_SCHEMA_VERSION,
            "document_id": str(document_id),
            "content_sha256": content_sha256,
            "metadata_sha256": metadata_sha256,
            "prior_revision_id": str(prior_revision_id),
        }
        canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return uuid5(_METADATA_REVISION_NAMESPACE, canonical)

    @staticmethod
    def expected_raw_record_id(revision_id: UUID) -> UUID:
        return uuid5(_METADATA_RAW_NAMESPACE, f"{revision_id}|raw-record")


def _validate_terminal_script_element(value: str) -> str:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("terminal script element must be ASCII") from error
    match = _TERMINAL_SCRIPT_ELEMENT.fullmatch(encoded)
    if match is None:
        raise ValueError("terminal script element does not match the accepted grammar")
    path = match.group(1).decode("ascii")
    if (
        not _TERMINAL_SCRIPT_PATH.fullmatch(path)
        or path.startswith("//")
        or any(segment in {"", ".", ".."} for segment in path[1:].split("/"))
    ):
        raise ValueError("terminal script path is outside the accepted local-path grammar")
    return value


class SecTerminalScriptDifference(_FrozenContract):
    """Byte-exact proof for one changed terminal external-script element."""

    proof_version: Literal["sec-terminal-external-script-v1"] = "sec-terminal-external-script-v1"
    old_script: str | None = Field(default=None, max_length=_MAX_TERMINAL_SCRIPT_BYTES)
    new_script: str | None = Field(default=None, max_length=_MAX_TERMINAL_SCRIPT_BYTES)
    insertion_offset: int = Field(ge=0, le=50 * 1024 * 1024)
    core_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    core_sha256: str

    @field_validator("old_script", "new_script")
    @classmethod
    def validate_script(cls, value: str | None) -> str | None:
        return _validate_terminal_script_element(value) if value is not None else None

    @field_validator("core_sha256")
    @classmethod
    def validate_core_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("core_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_difference(self) -> SecTerminalScriptDifference:
        if self.old_script == self.new_script:
            raise ValueError("terminal script proof must describe a real difference")
        return self


def _terminal_script_core(content: bytes) -> tuple[bytes, str | None, int]:
    if not content or len(content) > 50 * 1024 * 1024:
        raise ValueError("SEC HTML content is empty or exceeds its size bound")
    suffix_start = len(content)
    while suffix_start and content[suffix_start - 1] in _ASCII_TRAILING_WHITESPACE:
        suffix_start -= 1
    whitespace = content[suffix_start:]
    without_whitespace = content[:suffix_start]
    if not without_whitespace.endswith(_BODY_HTML_CLOSING):
        raise ValueError("SEC HTML does not end in the accepted body/document closure")
    before_closing = without_whitespace[: -len(_BODY_HTML_CLOSING)]
    window_start = max(0, len(before_closing) - _MAX_TERMINAL_SCRIPT_BYTES)
    terminal_window = before_closing[window_start:]
    match = _TERMINAL_SCRIPT_ELEMENT.search(terminal_window)
    if match is None or match.end() != len(terminal_window):
        return content, None, len(before_closing)

    element = match.group()
    element_text = _validate_terminal_script_element(element.decode("ascii"))
    insertion_offset = window_start + match.start()
    prior_tail = before_closing[:insertion_offset].rstrip(_ASCII_TRAILING_WHITESPACE)
    if prior_tail.endswith(b"</script>"):
        raise ValueError("multiple adjacent terminal script elements are not accepted")
    core = before_closing[:insertion_offset] + _BODY_HTML_CLOSING + whitespace
    return core, element_text, insertion_offset


def create_sec_terminal_script_difference(
    prior_content: bytes,
    current_content: bytes,
    *,
    document_name: str,
) -> SecTerminalScriptDifference:
    """Prove equality outside one tightly specified terminal script in HTML blobs."""
    if not document_name.lower().endswith((".htm", ".html")):
        raise ValueError("terminal script proof is restricted to HTML documents")
    prior_core, old_script, prior_offset = _terminal_script_core(prior_content)
    current_core, new_script, current_offset = _terminal_script_core(current_content)
    if prior_core != current_core:
        raise ValueError("SEC HTML differs outside the accepted terminal script element")
    if old_script == new_script:
        raise ValueError("SEC HTML does not contain a changed accepted terminal script")
    if prior_offset != current_offset:
        raise ValueError("terminal script insertion offsets do not match")
    return SecTerminalScriptDifference(
        old_script=old_script,
        new_script=new_script,
        insertion_offset=prior_offset,
        core_size_bytes=len(prior_core),
        core_sha256=hashlib.sha256(prior_core).hexdigest(),
    )


def verify_sec_terminal_script_difference(
    prior_content: bytes,
    current_content: bytes,
    *,
    document_name: str,
    proof: SecTerminalScriptDifference,
) -> None:
    """Recompute the complete proof from both full blobs and reject altered fields."""
    expected = create_sec_terminal_script_difference(
        prior_content,
        current_content,
        document_name=document_name,
    )
    if expected != proof:
        raise ValueError("terminal script proof does not match the verified document blobs")


class SecDocumentAcquisitionRevision(_FrozenContract):
    """Append-only record of a new byte-exact SEC response linked to its prior revision."""

    revision_id: UUID
    asset_id: NonEmptyStr
    document: SecLogicalDocument
    prior_revision_id: UUID
    raw_record_id: UUID
    discovery_raw_record_id: UUID
    content_sha256: NonEmptyStr
    content_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    prior_content_sha256: NonEmptyStr
    metadata_sha256: NonEmptyStr
    metadata_observed_at: UTCDateTime
    available_at: UTCDateTime
    retrieved_at: UTCDateTime
    source_url: NonEmptyStr
    terminal_script_difference: SecTerminalScriptDifference
    revision_schema_version: Literal["sec-document-revision-v4"] = (
        ACQUISITION_REVISION_SCHEMA_VERSION
    )

    @field_validator("content_sha256", "prior_content_sha256", "metadata_sha256")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("acquisition digests must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def validate_identity_and_availability(self) -> SecDocumentAcquisitionRevision:
        expected_revision = self.expected_id(
            self.document.document_id,
            self.content_sha256,
            self.metadata_sha256,
            self.prior_revision_id,
            self.prior_content_sha256,
        )
        if self.revision_id != expected_revision:
            raise ValueError("acquisition revision_id does not match its canonical identity")
        if self.raw_record_id != self.expected_raw_record_id(self.revision_id):
            raise ValueError("acquisition raw_record_id does not match its identity")
        if self.prior_revision_id == self.revision_id:
            raise ValueError("acquisition revision cannot point to itself")
        if self.metadata_sha256 != sec_document_metadata_sha256(self.document):
            raise ValueError("acquisition metadata_sha256 does not match the SEC document")
        if not self.document.name.lower().endswith((".htm", ".html")):
            raise ValueError("acquisition script proof requires an HTML document")
        if self.metadata_observed_at > self.retrieved_at:
            raise ValueError("SEC acquisition cannot precede its Submissions observation")
        minimum_available_at = max(
            self.document.filing.accepted_at,
            self.metadata_observed_at,
            self.retrieved_at,
        )
        if self.available_at < minimum_available_at:
            raise ValueError("acquisition availability omits an evidence time")
        if self.terminal_script_difference.insertion_offset > max(
            self.content_size_bytes, self.terminal_script_difference.core_size_bytes
        ):
            raise ValueError("terminal script offset exceeds the proven content size")
        return self

    @staticmethod
    def expected_id(
        document_id: UUID,
        content_sha256: str,
        metadata_sha256: str,
        prior_revision_id: UUID,
        prior_content_sha256: str,
    ) -> UUID:
        identity = {
            "schema_version": ACQUISITION_REVISION_SCHEMA_VERSION,
            "document_id": str(document_id),
            "content_sha256": content_sha256,
            "metadata_sha256": metadata_sha256,
            "prior_revision_id": str(prior_revision_id),
            "prior_content_sha256": prior_content_sha256,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return uuid5(_ACQUISITION_REVISION_NAMESPACE, canonical)

    @staticmethod
    def expected_raw_record_id(revision_id: UUID) -> UUID:
        return uuid5(_ACQUISITION_RAW_NAMESPACE, f"{revision_id}|raw-record")


SecAssetDocumentRevision = (
    SecDocumentRevision | SecDocumentMetadataRevision | SecDocumentAcquisitionRevision
)


class SecFilerDocumentRevision(_FrozenContract):
    """Document revision linked to a filer rather than a catalog asset."""

    revision_id: UUID
    filer_cik: NonEmptyStr
    document: SecLogicalDocument
    raw_record_id: UUID
    discovery_raw_record_id: UUID
    content_sha256: NonEmptyStr
    content_size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    available_at: UTCDateTime
    retrieved_at: UTCDateTime
    source_url: NonEmptyStr
    revision_schema_version: Literal["sec-filer-document-revision-v1"] = (
        FILER_REVISION_SCHEMA_VERSION
    )

    @field_validator("filer_cik")
    @classmethod
    def validate_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @field_validator("content_sha256")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> SecFilerDocumentRevision:
        expected_revision = self.expected_id(self.document.document_id, self.content_sha256)
        if self.revision_id != expected_revision:
            raise ValueError("filer revision identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.revision_id):
            raise ValueError("filer revision raw identity is invalid")
        if self.filer_cik != self.document.filing.filer_cik:
            raise ValueError("filer revision CIK conflicts with its filing")
        if self.available_at != self.document.filing.accepted_at:
            raise ValueError("filer revision availability must equal SEC filing acceptance")
        return self

    @staticmethod
    def expected_id(document_id: UUID, content_sha256: str) -> UUID:
        return uuid5(
            _FILER_REVISION_NAMESPACE,
            f"{document_id}|{content_sha256}|{FILER_REVISION_SCHEMA_VERSION}",
        )

    @staticmethod
    def expected_raw_record_id(revision_id: UUID) -> UUID:
        return uuid5(_FILER_RAW_NAMESPACE, f"{revision_id}|raw-record")


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
    revision: SecAssetDocumentRevision | None = None
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
