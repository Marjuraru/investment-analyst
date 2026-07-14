"""Strict models for selected Apple SEC fundamental facts."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

ASSET_ID = "equity:us:aapl"
CIK = "0000320193"
SUBMISSIONS_SOURCE_ID = "sec-edgar:aapl:submissions"
COMPANYFACTS_SOURCE_ID = "sec-edgar:aapl:companyfacts"
SUBMISSIONS_SCHEMA_VERSION = "sec-edgar-submissions-snapshot-v1"
COMPANYFACTS_SCHEMA_VERSION = "sec-edgar-companyfacts-snapshot-v1"
TRANSFORMATION_VERSION = "sec-aapl-companyfacts-normalizer-v1"
_ALLOWED_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})


class SecFactPeriodType(StrEnum):
    """Accounting period shape of a selected SEC fact."""

    DURATION = "duration"
    INSTANT = "instant"


class SecFactDefinition(ContractModel):
    """Explicit XBRL concept selected for normalization."""

    model_config = ConfigDict(frozen=True)

    field_name: NonEmptyStr
    taxonomy: NonEmptyStr
    tag: NonEmptyStr
    unit: NonEmptyStr
    period_type: SecFactPeriodType


SEC_FACT_DEFINITIONS = (
    SecFactDefinition(
        field_name="fundamental.revenue",
        taxonomy="us-gaap",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.net_income",
        taxonomy="us-gaap",
        tag="NetIncomeLoss",
        unit="USD",
        period_type=SecFactPeriodType.DURATION,
    ),
    SecFactDefinition(
        field_name="fundamental.assets",
        taxonomy="us-gaap",
        tag="Assets",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.liabilities",
        taxonomy="us-gaap",
        tag="Liabilities",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
    SecFactDefinition(
        field_name="fundamental.stockholders_equity",
        taxonomy="us-gaap",
        tag="StockholdersEquity",
        unit="USD",
        period_type=SecFactPeriodType.INSTANT,
    ),
)
_DEFINITION_BY_FIELD = {item.field_name: item for item in SEC_FACT_DEFINITIONS}


class SecFilingMetadata(ContractModel):
    """Validated metadata for one supported SEC filing accession."""

    model_config = ConfigDict(frozen=True)

    accession_number: NonEmptyStr
    form: NonEmptyStr
    filing_date: date
    report_date: date
    acceptance_at: UTCDateTime
    primary_document: NonEmptyStr
    is_amendment: bool

    @model_validator(mode="after")
    def validate_filing(self) -> "SecFilingMetadata":
        """Validate supported forms, chronology, and amendment semantics."""
        if self.form not in _ALLOWED_FORMS:
            raise ValueError("form must be a supported 10-K or 10-Q filing")
        if self.report_date > self.filing_date:
            raise ValueError("report_date must not be later than filing_date")
        if self.is_amendment != self.form.endswith("/A"):
            raise ValueError("is_amendment must match the filing form")
        return self


class SecFundamentalFact(ContractModel):
    """Selected point-in-time Apple fact before observation persistence."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    companyfacts_record_id: UUID
    submissions_record_id: UUID
    field_name: NonEmptyStr
    taxonomy: NonEmptyStr
    tag: NonEmptyStr
    unit: NonEmptyStr
    value: Decimal
    accession_number: NonEmptyStr
    form: NonEmptyStr
    fiscal_year: int = Field(ge=1900, le=10000)
    fiscal_period: NonEmptyStr
    period_start: date | None = None
    period_end: date
    filed_date: date
    acceptance_at: UTCDateTime
    frequency: DataFrequency
    frame: NonEmptyStr | None = None
    quality: DataQuality

    @field_validator("value", mode="before")
    @classmethod
    def reject_binary_floating_point(cls, value: object) -> object:
        """Reject floats and booleans before Decimal validation."""
        if isinstance(value, (bool, float)):
            raise ValueError("value must be provided without float or bool")
        return value

    @field_validator("fiscal_year", mode="before")
    @classmethod
    def reject_boolean_year(cls, value: object) -> object:
        """Reject booleans masquerading as integers."""
        if isinstance(value, bool):
            raise ValueError("fiscal_year must be an integer")
        return value

    @model_validator(mode="after")
    def validate_fact(self) -> "SecFundamentalFact":
        """Validate the selected concept, period shape, and fixed scope."""
        definition = _DEFINITION_BY_FIELD.get(self.field_name)
        if definition is None:
            raise ValueError("field_name is not one of the selected SEC concepts")
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.taxonomy != definition.taxonomy or self.tag != definition.tag:
            raise ValueError("taxonomy and tag must match the selected field definition")
        if self.unit != "USD" or self.unit != definition.unit:
            raise ValueError("unit must be USD")
        if not self.value.is_finite():
            raise ValueError("value must be a finite Decimal")
        if self.form not in _ALLOWED_FORMS:
            raise ValueError("form must be a supported 10-K or 10-Q filing")
        if self.frequency not in {DataFrequency.QUARTERLY, DataFrequency.ANNUAL}:
            raise ValueError("frequency must be quarterly or annual")
        if self.quality is not DataQuality.VALID:
            raise ValueError("SEC fundamental facts must have VALID quality")
        if definition.period_type is SecFactPeriodType.DURATION:
            if self.period_start is None:
                raise ValueError("duration facts require period_start")
            if self.period_start > self.period_end:
                raise ValueError("period_start must not be later than period_end")
        elif self.period_start is not None:
            raise ValueError("instant facts must not define period_start")
        return self

    @property
    def period_type(self) -> SecFactPeriodType:
        """Return the fixed period type for this selected concept."""
        return _DEFINITION_BY_FIELD[self.field_name].period_type

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "asset_id": self.asset_id,
            "companyfacts_record_id": str(self.companyfacts_record_id),
            "submissions_record_id": str(self.submissions_record_id),
            "field_name": self.field_name,
            "taxonomy": self.taxonomy,
            "tag": self.tag,
            "unit": self.unit,
            "value": str(self.value),
            "accession_number": self.accession_number,
            "form": self.form,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "filed_date": self.filed_date.isoformat(),
            "acceptance_at": self.acceptance_at.isoformat(),
            "frequency": self.frequency.value,
            "frame": self.frame,
            "quality": self.quality.value,
        }


def get_sec_fact_definition(field_name: str) -> SecFactDefinition:
    """Return one explicit definition or raise for an unsupported field."""
    try:
        return _DEFINITION_BY_FIELD[field_name]
    except KeyError as error:
        raise ValueError(f"unsupported SEC fact field: {field_name}") from error
