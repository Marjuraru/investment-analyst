"""Strict models for point-in-time SEC fundamental queries."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    SEC_FACT_DEFINITIONS,
)

_ALLOWED_FIELDS = tuple(definition.field_name for definition in SEC_FACT_DEFINITIONS)
_ALLOWED_FIELD_SET = frozenset(_ALLOWED_FIELDS)
_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


class SecFundamentalQuery(ContractModel):
    """Read-only point-in-time query for selected Apple SEC facts."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr = ASSET_ID
    known_at: UTCDateTime
    frequency: DataFrequency
    start_period_end: date | None = None
    end_period_end: date | None = None
    limit: int | None = Field(default=None, ge=1, le=500)

    @field_validator("limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value: object) -> object:
        """Reject booleans masquerading as integers."""
        if isinstance(value, bool):
            raise ValueError("limit must be an integer between 1 and 500")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "SecFundamentalQuery":
        """Validate the fixed Apple scope and inclusive period range."""
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if (
            self.start_period_end is not None
            and self.end_period_end is not None
            and self.start_period_end > self.end_period_end
        ):
            raise ValueError("start_period_end must not be later than end_period_end")
        return self


class SecSelectedFundamentalFact(ContractModel):
    """One selected SEC observation with its audit metadata."""

    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    raw_record_id: UUID
    field_name: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    frequency: DataFrequency
    period_start: UTCDateTime | None = None
    period_end: UTCDateTime
    available_at: UTCDateTime
    normalized_at: UTCDateTime
    accession_number: NonEmptyStr
    taxonomy: NonEmptyStr
    tag: NonEmptyStr
    form: NonEmptyStr | None = None
    fiscal_year: NonEmptyStr | None = None
    fiscal_period: NonEmptyStr | None = None
    source_id: NonEmptyStr
    record_key: NonEmptyStr
    superseded_count: int = Field(ge=0)

    @field_validator("value", mode="before")
    @classmethod
    def reject_binary_floating_point(cls, value: object) -> object:
        """Reject floats and booleans before Decimal parsing."""
        if isinstance(value, (bool, float)):
            raise ValueError("value must be provided without float or bool")
        return value

    @field_validator("superseded_count", mode="before")
    @classmethod
    def reject_boolean_count(cls, value: object) -> object:
        """Reject booleans masquerading as revision counts."""
        if isinstance(value, bool):
            raise ValueError("superseded_count must be an integer")
        return value

    @model_validator(mode="after")
    def validate_fact(self) -> "SecSelectedFundamentalFact":
        """Validate selected field, unit, frequency, and finite value."""
        if self.field_name not in _ALLOWED_FIELD_SET:
            raise ValueError("field_name is not an allowed SEC fundamental field")
        if self.unit != "USD":
            raise ValueError("unit must be USD")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if not self.value.is_finite():
            raise ValueError("value must be a finite Decimal")
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start must not be later than period_end")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "observation_id": str(self.observation_id),
            "raw_record_id": str(self.raw_record_id),
            "field_name": self.field_name,
            "value": str(self.value),
            "unit": self.unit,
            "frequency": self.frequency.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "normalized_at": self.normalized_at.isoformat(),
            "accession_number": self.accession_number,
            "taxonomy": self.taxonomy,
            "tag": self.tag,
            "form": self.form,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "source_id": self.source_id,
            "record_key": self.record_key,
            "superseded_count": self.superseded_count,
        }


class SecFundamentalPeriodView(ContractModel):
    """Selected facts for one reporting period without cross-period filling."""

    model_config = ConfigDict(frozen=True)

    period_end: UTCDateTime
    frequency: DataFrequency
    facts: tuple[SecSelectedFundamentalFact, ...]
    missing_fields: tuple[NonEmptyStr, ...]
    available_fields: tuple[NonEmptyStr, ...]
    is_complete: bool
    latest_available_at: UTCDateTime

    @model_validator(mode="after")
    def validate_period(self) -> "SecFundamentalPeriodView":
        """Validate deterministic field partitioning and fact membership."""
        fact_fields = tuple(fact.field_name for fact in self.facts)
        if fact_fields != tuple(sorted(fact_fields)):
            raise ValueError("facts must be ordered by field_name")
        if len(fact_fields) != len(set(fact_fields)):
            raise ValueError("a period cannot contain duplicate field names")
        available = set(self.available_fields)
        missing = set(self.missing_fields)
        if available & missing:
            raise ValueError("available_fields and missing_fields must be disjoint")
        if available | missing != _ALLOWED_FIELD_SET:
            raise ValueError("field partitions must cover exactly the five selected fields")
        if available != set(fact_fields):
            raise ValueError("available_fields must match the facts")
        if self.is_complete != (not missing):
            raise ValueError("is_complete must match the missing field set")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if any(fact.frequency is not self.frequency for fact in self.facts):
            raise ValueError("all facts must match the period frequency")
        if any(fact.period_end != self.period_end for fact in self.facts):
            raise ValueError("all facts must match the period end")
        if not self.facts:
            raise ValueError("a returned period must contain at least one fact")
        if self.latest_available_at != max(fact.available_at for fact in self.facts):
            raise ValueError("latest_available_at must match the selected facts")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "period_end": self.period_end.isoformat(),
            "frequency": self.frequency.value,
            "facts": [fact.to_json_dict() for fact in self.facts],
            "missing_fields": list(self.missing_fields),
            "available_fields": list(self.available_fields),
            "is_complete": self.is_complete,
            "latest_available_at": self.latest_available_at.isoformat(),
        }


class SecFundamentalPointInTimeResult(ContractModel):
    """Auditable point-in-time view of locally stored Apple SEC observations."""

    model_config = ConfigDict(frozen=True)

    query: SecFundamentalQuery
    periods: tuple[SecFundamentalPeriodView, ...]
    observations_examined: int = Field(ge=0)
    observations_eligible: int = Field(ge=0)
    observations_selected: int = Field(ge=0)
    observations_superseded: int = Field(ge=0)
    periods_returned: int = Field(ge=0)
    earliest_period_end: UTCDateTime | None = None
    latest_period_end: UTCDateTime | None = None
    latest_period_complete: bool
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_result(self) -> "SecFundamentalPointInTimeResult":
        """Validate deterministic ordering and summary counts."""
        period_ends = tuple(period.period_end for period in self.periods)
        if period_ends != tuple(sorted(period_ends)):
            raise ValueError("periods must be ordered chronologically")
        if self.periods_returned != len(self.periods):
            raise ValueError("periods_returned must match periods")
        if self.observations_selected != sum(len(period.facts) for period in self.periods):
            raise ValueError("observations_selected must match returned facts")
        expected_earliest = period_ends[0] if period_ends else None
        expected_latest = period_ends[-1] if period_ends else None
        if self.earliest_period_end != expected_earliest:
            raise ValueError("earliest_period_end is inconsistent")
        if self.latest_period_end != expected_latest:
            raise ValueError("latest_period_end is inconsistent")
        expected_complete = self.periods[-1].is_complete if self.periods else False
        if self.latest_period_complete != expected_complete:
            raise ValueError("latest_period_complete is inconsistent")
        if not self.traceability_verified:
            raise ValueError("point-in-time results must have verified traceability")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""
        return {
            "query": {
                "asset_id": self.query.asset_id,
                "known_at": self.query.known_at.isoformat(),
                "frequency": self.query.frequency.value,
                "start_period_end": (
                    self.query.start_period_end.isoformat()
                    if self.query.start_period_end is not None
                    else None
                ),
                "end_period_end": (
                    self.query.end_period_end.isoformat()
                    if self.query.end_period_end is not None
                    else None
                ),
                "limit": self.query.limit,
            },
            "periods": [period.to_json_dict() for period in self.periods],
            "observations_examined": self.observations_examined,
            "observations_eligible": self.observations_eligible,
            "observations_selected": self.observations_selected,
            "observations_superseded": self.observations_superseded,
            "periods_returned": self.periods_returned,
            "earliest_period_end": (
                self.earliest_period_end.isoformat() if self.earliest_period_end else None
            ),
            "latest_period_end": (
                self.latest_period_end.isoformat() if self.latest_period_end else None
            ),
            "latest_period_complete": self.latest_period_complete,
            "traceability_verified": self.traceability_verified,
        }


def allowed_sec_fundamental_fields() -> tuple[str, ...]:
    """Return the five supported fields in deterministic order."""
    return _ALLOWED_FIELDS
