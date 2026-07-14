"""Select explicit Apple SEC Company Facts and normalize them into observations."""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    CIK,
    COMPANYFACTS_SCHEMA_VERSION,
    COMPANYFACTS_SOURCE_ID,
    SEC_FACT_DEFINITIONS,
    TRANSFORMATION_VERSION,
    SecFactDefinition,
    SecFactPeriodType,
    SecFundamentalFact,
)
from investment_analyst.providers.fundamentals.sec_filing_index import SecFilingIndex

_OBSERVATION_NAMESPACE = UUID("80bb1c78-73b5-5390-b3dd-6da82dcf8a2e")


class SecCompanyFactsNormalizationError(ValueError):
    """Base error for SEC Company Facts normalization."""


class MalformedSecCompanyFactsError(SecCompanyFactsNormalizationError):
    """Raised when a selected Company Facts structure is malformed."""


class ConflictingSecFactError(SecCompanyFactsNormalizationError):
    """Raised when one filing context contains contradictory selected values."""


@dataclass(frozen=True, slots=True)
class SecFactExtractionResult:
    """Immutable summary and selected facts from two SEC raw snapshots."""

    companyfacts_record_id: UUID
    submissions_record_id: UUID
    filings_indexed: int
    facts_examined: int
    facts_selected: int
    facts: tuple[SecFundamentalFact, ...]
    skipped_counts: Mapping[str, int]
    field_counts: Mapping[str, int]
    annual_count: int
    quarterly_count: int
    earliest_period_end: date | None
    latest_period_end: date | None
    traceability_verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "skipped_counts",
            MappingProxyType(dict(sorted(self.skipped_counts.items()))),
        )
        object.__setattr__(
            self,
            "field_counts",
            MappingProxyType(dict(sorted(self.field_counts.items()))),
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "companyfacts_record_id": str(self.companyfacts_record_id),
            "submissions_record_id": str(self.submissions_record_id),
            "filings_indexed": self.filings_indexed,
            "facts_examined": self.facts_examined,
            "facts_selected": self.facts_selected,
            "facts": [fact.to_json_dict() for fact in self.facts],
            "skipped_counts": dict(self.skipped_counts),
            "field_counts": dict(self.field_counts),
            "annual_count": self.annual_count,
            "quarterly_count": self.quarterly_count,
            "earliest_period_end": (
                self.earliest_period_end.isoformat() if self.earliest_period_end else None
            ),
            "latest_period_end": (
                self.latest_period_end.isoformat() if self.latest_period_end else None
            ),
            "traceability_verified": self.traceability_verified,
        }


class SecCompanyFactsNormalizer:
    """Extract only the five explicit Apple us-gaap facts required by this step."""

    def extract(
        self,
        companyfacts_record: RawRecord,
        submissions_record: RawRecord,
        *,
        normalized_at: datetime,
    ) -> SecFactExtractionResult:
        """Select annual and discrete quarterly facts point-in-time."""
        normalized_at = _utc_datetime(normalized_at, "normalized_at")
        document = _validate_companyfacts_record(companyfacts_record)
        filing_index = SecFilingIndex.from_raw_record(submissions_record)
        us_gaap = _require_mapping(
            _require_mapping(document.get("facts"), "document.facts").get("us-gaap"),
            "document.facts.us-gaap",
        )

        examined = 0
        skipped: Counter[str] = Counter()
        by_context: dict[tuple[object, ...], SecFundamentalFact] = {}

        for definition in SEC_FACT_DEFINITIONS:
            concept = us_gaap.get(definition.tag)
            if concept is None:
                skipped[f"missing_concept:{definition.field_name}"] += 1
                continue
            concept_mapping = _require_mapping(
                concept,
                f"document.facts.us-gaap.{definition.tag}",
            )
            units = _require_mapping(
                concept_mapping.get("units"),
                f"document.facts.us-gaap.{definition.tag}.units",
            )
            usd_facts = units.get("USD")
            if usd_facts is None:
                skipped[f"missing_usd_unit:{definition.field_name}"] += 1
                continue
            if not isinstance(usd_facts, list):
                raise MalformedSecCompanyFactsError(
                    f"USD facts for {definition.tag} must be a list"
                )
            for position, raw_fact in enumerate(usd_facts):
                examined += 1
                candidate, reason = _parse_candidate(
                    definition,
                    raw_fact,
                    filing_index,
                    companyfacts_record,
                    submissions_record,
                    normalized_at=normalized_at,
                    position=position,
                )
                if candidate is None:
                    skipped[reason] += 1
                    continue
                context = _fact_context(candidate)
                existing = by_context.get(context)
                if existing is None:
                    by_context[context] = candidate
                elif existing.value != candidate.value:
                    raise ConflictingSecFactError(
                        "contradictory values for SEC fact context "
                        f"{candidate.field_name} {candidate.accession_number}"
                    )
                else:
                    skipped["duplicate_identical"] += 1

        facts = tuple(
            sorted(
                by_context.values(),
                key=lambda item: (
                    item.period_end,
                    item.frequency.value,
                    item.field_name,
                    item.acceptance_at,
                    item.accession_number,
                ),
            )
        )
        field_counts = {definition.field_name: 0 for definition in SEC_FACT_DEFINITIONS}
        for fact in facts:
            field_counts[fact.field_name] += 1
        annual_count = sum(fact.frequency is DataFrequency.ANNUAL for fact in facts)
        quarterly_count = sum(fact.frequency is DataFrequency.QUARTERLY for fact in facts)
        period_ends = [fact.period_end for fact in facts]
        return SecFactExtractionResult(
            companyfacts_record_id=companyfacts_record.record_id,
            submissions_record_id=submissions_record.record_id,
            filings_indexed=len(filing_index.all()),
            facts_examined=examined,
            facts_selected=len(facts),
            facts=facts,
            skipped_counts=dict(skipped),
            field_counts=field_counts,
            annual_count=annual_count,
            quarterly_count=quarterly_count,
            earliest_period_end=min(period_ends) if period_ends else None,
            latest_period_end=max(period_ends) if period_ends else None,
            traceability_verified=True,
        )


def sec_fact_to_observation(
    fact: SecFundamentalFact,
    companyfacts_record: RawRecord,
    submissions_record: RawRecord,
    *,
    normalized_at: datetime,
) -> NormalizedObservation:
    """Convert one selected SEC fact into a stable normalized observation."""
    normalized_at = _utc_datetime(normalized_at, "normalized_at")
    _validate_companyfacts_record(companyfacts_record)
    SecFilingIndex.from_raw_record(submissions_record)
    if fact.companyfacts_record_id != companyfacts_record.record_id:
        raise SecCompanyFactsNormalizationError(
            "fact does not reference the supplied Company Facts RawRecord"
        )
    if fact.submissions_record_id != submissions_record.record_id:
        raise SecCompanyFactsNormalizationError(
            "fact does not reference the supplied Submissions RawRecord"
        )
    if fact.acceptance_at > normalized_at:
        raise SecCompanyFactsNormalizationError(
            "normalized_at must not precede the filing acceptance time"
        )

    identity = json.dumps(
        {
            "source_id": COMPANYFACTS_SOURCE_ID,
            "asset_id": fact.asset_id,
            "field_name": fact.field_name,
            "taxonomy": fact.taxonomy,
            "tag": fact.tag,
            "unit": fact.unit,
            "value": str(fact.value),
            "accession_number": fact.accession_number,
            "form": fact.form,
            "fiscal_year": fact.fiscal_year,
            "fiscal_period": fact.fiscal_period,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat(),
            "acceptance_at": fact.acceptance_at.isoformat(),
            "transformation_version": TRANSFORMATION_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    record_key = json.dumps(
        {
            "accession_number": fact.accession_number,
            "taxonomy": fact.taxonomy,
            "tag": fact.tag,
            "unit": fact.unit,
            "period": fact.period_end.isoformat(),
            "companyfacts_record_id": str(companyfacts_record.record_id),
            "submissions_record_id": str(submissions_record.record_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return NormalizedObservation(
        observation_id=uuid5(_OBSERVATION_NAMESPACE, identity),
        raw_record_id=companyfacts_record.record_id,
        asset_id=ASSET_ID,
        field_name=fact.field_name,
        value=fact.value,
        unit="USD",
        frequency=fact.frequency,
        observed_at=_date_at_utc_midnight(fact.period_end),
        period_start=(
            _date_at_utc_midnight(fact.period_start) if fact.period_start is not None else None
        ),
        period_end=_date_at_utc_midnight(fact.period_end),
        available_at=fact.acceptance_at,
        normalized_at=normalized_at,
        source=SourceReference(
            source_id=COMPANYFACTS_SOURCE_ID,
            record_key=record_key,
            retrieved_at=companyfacts_record.source.retrieved_at,
            raw_uri=companyfacts_record.source.raw_uri,
            checksum_sha256=companyfacts_record.source.checksum_sha256,
        ),
        quality=DataQuality.VALID,
        transformation_version=TRANSFORMATION_VERSION,
    )


def _parse_candidate(
    definition: SecFactDefinition,
    raw_fact: object,
    filing_index: SecFilingIndex,
    companyfacts_record: RawRecord,
    submissions_record: RawRecord,
    *,
    normalized_at: datetime,
    position: int,
) -> tuple[SecFundamentalFact | None, str]:
    fact = _require_mapping(raw_fact, f"{definition.tag}.USD[{position}]")
    value = _parse_decimal(fact.get("val"), f"{definition.tag}.USD[{position}].val")
    accession = _require_string(fact.get("accn"), "accn")

    fiscal_year_value = fact.get("fy")
    if fiscal_year_value is None or (
        isinstance(fiscal_year_value, str) and not fiscal_year_value.strip()
    ):
        return None, "missing_fiscal_year"

    fiscal_year = _parse_year(fiscal_year_value)
    fiscal_period = _require_string(fact.get("fp"), "fp")
    form = _require_string(fact.get("form"), "form")
    filed_date = _parse_date(fact.get("filed"), "filed")
    period_end = _parse_date(fact.get("end"), "end")
    frame = _optional_string(fact.get("frame"), "frame")
    metadata = filing_index.get(accession)
    if metadata is None:
        return None, "missing_filing_metadata"
    if metadata.form != form:
        return None, "form_mismatch"
    if metadata.filing_date != filed_date:
        return None, "filed_date_mismatch"
    if metadata.report_date != period_end:
        return None, "report_date_mismatch"
    if metadata.acceptance_at > normalized_at:
        return None, "future_acceptance"

    frequency = _frequency(form, fiscal_period)
    if frequency is None:
        return None, "unsupported_fiscal_period"

    period_start: date | None = None
    if definition.period_type is SecFactPeriodType.DURATION:
        if fact.get("start") is None:
            return None, "missing_period_start"
        period_start = _parse_date(fact.get("start"), "start")
        if period_start > period_end:
            return None, "inverted_period"
        duration_days = (period_end - period_start).days + 1
        if frequency is DataFrequency.ANNUAL and not 330 <= duration_days <= 400:
            return None, "non_annual_duration"
        if frequency is DataFrequency.QUARTERLY and not 70 <= duration_days <= 110:
            return None, "non_discrete_quarter_duration"
    elif fact.get("start") is not None:
        return None, "unexpected_period_start"

    return (
        SecFundamentalFact(
            asset_id=ASSET_ID,
            companyfacts_record_id=companyfacts_record.record_id,
            submissions_record_id=submissions_record.record_id,
            field_name=definition.field_name,
            taxonomy=definition.taxonomy,
            tag=definition.tag,
            unit=definition.unit,
            value=value,
            accession_number=accession,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            period_start=period_start,
            period_end=period_end,
            filed_date=filed_date,
            acceptance_at=metadata.acceptance_at,
            frequency=frequency,
            frame=frame,
            quality=DataQuality.VALID,
        ),
        "",
    )


def _validate_companyfacts_record(record: RawRecord) -> Mapping[str, object]:
    if record.asset_id != ASSET_ID:
        raise MalformedSecCompanyFactsError("Company Facts RawRecord must belong to Apple")
    if record.source.source_id != COMPANYFACTS_SOURCE_ID:
        raise MalformedSecCompanyFactsError("Company Facts RawRecord has an unexpected source")
    if record.schema_version != COMPANYFACTS_SCHEMA_VERSION:
        raise MalformedSecCompanyFactsError("Company Facts RawRecord has an unexpected schema")
    payload = _require_mapping(record.payload, "payload")
    required = {"document_type", "cik", "entity_name", "document"}
    if not required.issubset(payload):
        raise MalformedSecCompanyFactsError("Company Facts payload is missing required fields")
    if payload["document_type"] != "company_facts":
        raise MalformedSecCompanyFactsError("document_type must be company_facts")
    if _normalize_cik(payload["cik"]) != CIK:
        raise MalformedSecCompanyFactsError("Company Facts CIK does not identify Apple")
    if _require_string(payload["entity_name"], "entity_name").casefold() != "apple inc.":
        raise MalformedSecCompanyFactsError("Company Facts entity does not identify Apple")
    document = _require_mapping(payload["document"], "document")
    if _normalize_cik(document.get("cik")) != CIK:
        raise MalformedSecCompanyFactsError("Company Facts document CIK is invalid")
    entity_name = _require_string(document.get("entityName"), "document.entityName")
    if entity_name.casefold() != "apple inc.":
        raise MalformedSecCompanyFactsError("Company Facts document entity is invalid")
    if not isinstance(document.get("facts"), Mapping):
        raise MalformedSecCompanyFactsError("document.facts must be an object")
    return document


def _fact_context(fact: SecFundamentalFact) -> tuple[object, ...]:
    return (
        fact.field_name,
        fact.taxonomy,
        fact.tag,
        fact.unit,
        fact.accession_number,
        fact.period_start,
        fact.period_end,
        fact.form,
        fact.fiscal_year,
        fact.fiscal_period,
        fact.acceptance_at,
    )


def _frequency(form: str, fiscal_period: str) -> DataFrequency | None:
    if form in {"10-K", "10-K/A"} and fiscal_period == "FY":
        return DataFrequency.ANNUAL
    if form in {"10-Q", "10-Q/A"} and fiscal_period in {"Q1", "Q2", "Q3"}:
        return DataFrequency.QUARTERLY
    return None


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise MalformedSecCompanyFactsError(f"{field_name} must be an object")
    return value


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MalformedSecCompanyFactsError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _parse_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise MalformedSecCompanyFactsError(f"{field_name} must not be float or bool")
    if not isinstance(value, (str, int, Decimal)):
        raise MalformedSecCompanyFactsError(f"{field_name} must be a decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise MalformedSecCompanyFactsError(f"{field_name} is not a valid Decimal") from error
    if not parsed.is_finite():
        raise MalformedSecCompanyFactsError(f"{field_name} must be finite")
    return parsed


def _parse_year(value: object) -> int:
    if isinstance(value, bool):
        raise MalformedSecCompanyFactsError("fy must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdecimal():
        result = int(value.strip())
    else:
        raise MalformedSecCompanyFactsError("fy must be an integer")
    if not 1900 <= result <= 10000:
        raise MalformedSecCompanyFactsError("fy is outside the accepted range")
    return result


def _parse_date(value: object, field_name: str) -> date:
    text = _require_string(value, field_name)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise MalformedSecCompanyFactsError(f"{field_name} must be an ISO date") from error


def _normalize_cik(value: object) -> str:
    if isinstance(value, bool):
        raise MalformedSecCompanyFactsError("CIK must be numeric text")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise MalformedSecCompanyFactsError("CIK must be numeric text")
    if not text.isdecimal() or len(text) > 10:
        raise MalformedSecCompanyFactsError("CIK must contain at most ten digits")
    return text.zfill(10)


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecCompanyFactsNormalizationError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


def _date_at_utc_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)
