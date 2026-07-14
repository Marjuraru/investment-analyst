"""Build a deterministic filing index from an Apple SEC submissions snapshot."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from investment_analyst.core.models import RawRecord
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    CIK,
    SUBMISSIONS_SCHEMA_VERSION,
    SUBMISSIONS_SOURCE_ID,
    SecFilingMetadata,
)

_ALLOWED_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "form",
    "primaryDocument",
)


class SecFilingIndexError(ValueError):
    """Base error for invalid SEC filing indexes."""


class MalformedSecSubmissionsError(SecFilingIndexError):
    """Raised when a submissions snapshot lacks required structure."""


class AmbiguousSecFilingError(SecFilingIndexError):
    """Raised when one accession maps to contradictory filing metadata."""


@dataclass(frozen=True, slots=True)
class SecFilingIndex:
    """Immutable, ordered index of supported Apple SEC filings."""

    _filings: tuple[SecFilingMetadata, ...]

    @classmethod
    def from_raw_record(cls, record: RawRecord) -> "SecFilingIndex":
        """Validate a submissions RawRecord and index supported accessions."""
        recent = _validate_record_and_get_recent(record)
        columns = {name: _require_list(recent, name) for name in _COLUMNS}
        lengths = {len(values) for values in columns.values()}
        if len(lengths) != 1:
            raise MalformedSecSubmissionsError(
                "SEC submissions recent columns must have equal lengths"
            )

        by_accession: dict[str, SecFilingMetadata] = {}
        row_count = next(iter(lengths), 0)
        for index in range(row_count):
            metadata = _parse_row(columns, index)
            if metadata is None:
                continue
            existing = by_accession.get(metadata.accession_number)
            if existing is None:
                by_accession[metadata.accession_number] = metadata
            elif existing != metadata:
                raise AmbiguousSecFilingError(
                    f"contradictory filing metadata for accession {metadata.accession_number!r}"
                )

        ordered = tuple(
            sorted(
                by_accession.values(),
                key=lambda item: (item.acceptance_at, item.accession_number),
            )
        )
        return cls(ordered)

    def get(self, accession_number: str) -> SecFilingMetadata | None:
        """Return filing metadata for one accession when present."""
        normalized = accession_number.strip()
        for item in self._filings:
            if item.accession_number == normalized:
                return item
        return None

    def all(self) -> tuple[SecFilingMetadata, ...]:
        """Return all supported filings in deterministic order."""
        return self._filings


def _validate_record_and_get_recent(record: RawRecord) -> Mapping[str, object]:
    if record.asset_id != ASSET_ID:
        raise MalformedSecSubmissionsError("submissions RawRecord must belong to Apple")
    if record.source.source_id != SUBMISSIONS_SOURCE_ID:
        raise MalformedSecSubmissionsError("submissions RawRecord has an unexpected source")
    if record.schema_version != SUBMISSIONS_SCHEMA_VERSION:
        raise MalformedSecSubmissionsError("submissions RawRecord has an unexpected schema")
    payload = _require_mapping(record.payload, "payload")
    required_payload = {"document_type", "cik", "entity_name", "document"}
    if not required_payload.issubset(payload):
        raise MalformedSecSubmissionsError("submissions payload is missing required fields")
    if payload["document_type"] != "submissions":
        raise MalformedSecSubmissionsError("document_type must be submissions")
    if _normalize_cik(payload["cik"]) != CIK:
        raise MalformedSecSubmissionsError("submissions payload CIK does not identify Apple")
    if _require_string(payload["entity_name"], "entity_name").casefold() != "apple inc.":
        raise MalformedSecSubmissionsError("submissions payload entity does not identify Apple")
    document = _require_mapping(payload["document"], "document")
    filings = _require_mapping(document.get("filings"), "document.filings")
    return _require_mapping(filings.get("recent"), "document.filings.recent")


def _parse_row(
    columns: Mapping[str, list[object]],
    index: int,
) -> SecFilingMetadata | None:
    form = _require_string(columns["form"][index], "form")
    if form not in _ALLOWED_FORMS:
        return None

    accession = _require_string(
        columns["accessionNumber"][index],
        "accessionNumber",
    )
    filing_date = _parse_date(columns["filingDate"][index], "filingDate")
    report_date = _parse_date(columns["reportDate"][index], "reportDate")
    acceptance_at = _parse_datetime(columns["acceptanceDateTime"][index])
    primary_document = _require_string(
        columns["primaryDocument"][index],
        "primaryDocument",
    )
    try:
        return SecFilingMetadata(
            accession_number=accession,
            form=form,
            filing_date=filing_date,
            report_date=report_date,
            acceptance_at=acceptance_at,
            primary_document=primary_document,
            is_amendment=form.endswith("/A"),
        )
    except ValueError as error:
        raise MalformedSecSubmissionsError(
            f"invalid SEC submissions row at index {index}: {error}"
        ) from error


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise MalformedSecSubmissionsError(f"{field_name} must be an object")
    return value


def _require_list(mapping: Mapping[str, object], field_name: str) -> list[object]:
    value = mapping.get(field_name)
    if not isinstance(value, list):
        raise MalformedSecSubmissionsError(f"{field_name} must be a list")
    return value


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MalformedSecSubmissionsError(f"{field_name} must be a non-empty string")
    return value.strip()


def _parse_date(value: object, field_name: str) -> date:
    text = _require_string(value, field_name)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise MalformedSecSubmissionsError(f"{field_name} must be an ISO date") from error


def _parse_datetime(value: object) -> datetime:
    text = _require_string(value, "acceptanceDateTime")
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise MalformedSecSubmissionsError(
            "acceptanceDateTime must be an ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MalformedSecSubmissionsError("acceptanceDateTime must include a timezone")
    return parsed.astimezone(UTC)


def _normalize_cik(value: object) -> str:
    if isinstance(value, bool):
        raise MalformedSecSubmissionsError("CIK must be numeric text")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise MalformedSecSubmissionsError("CIK must be numeric text")
    if not text.isdecimal() or len(text) > 10:
        raise MalformedSecSubmissionsError("CIK must contain at most ten digits")
    return text.zfill(10)
